import os
import re
import sqlite3
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

def normalize_channel_id(cid) -> int:
    """
    Normalizes a Telegram peer or channel ID to a canonical positive integer.
    Supports int, string, Telethon entity objects, and marked peer IDs:
      - Channel / Supergroup: -100xxxxxxxxxx -> xxxxxxxxxx
      - Basic Chat: -xxxxxxxx -> xxxxxxxx
      - Canonical positive bare integer: preserved as-is.
    """
    if cid is None:
        raise ValueError("channel_id cannot be None")
    if hasattr(cid, "id"):
        return int(cid.id)
    try:
        s = str(cid).strip()
        if s.startswith("-100"):
            return int(s[4:])
        elif s.startswith("-"):
            return int(s[1:])
        int_val = int(s)
        if int_val > 1000000000000:
            return int_val - 1000000000000
        return int_val
    except Exception as e:
        raise ValueError(f"Invalid channel_id {cid}: {e}")

def sanitize_folder_name(name: str) -> str:
    """
    Sanitizes directory/topic names to avoid invalid Windows characters, reserved names, or path traversal.
    Preserves Unicode characters cleanly.
    """
    s = re.sub(r'[\\/*?:"<>|]', "_", str(name or "")).strip()
    s = re.sub(r'\.{2,}', '_', s)
    s = re.sub(r'^\.+', '', s)
    s = re.sub(r'\.+$', '', s)
    s = re.sub(r'_+', '_', s).strip(' _')
    # Windows reserved device names
    reserved = {"CON", "PRN", "AUX", "NUL",
                "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}
    if s.upper() in reserved:
        s = f"{s}_dir"
    return s[:150] or "Unnamed"

class DownloadTrackerDB:
    def __init__(self, db_path="download_tracker.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    last_scanned_id INTEGER DEFAULT 0,
                    last_scanned_at TEXT,
                    is_active INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    mime_type TEXT,
                    relative_path TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'DOWNLOADING', 'COMPLETED', 'FAILED', 'SKIPPED')),
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    started_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY(channel_id) REFERENCES channels(channel_id),
                    UNIQUE(channel_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_downloads_lookup ON downloads(channel_id, message_id);
                CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);

                CREATE TABLE IF NOT EXISTS legacy_channel_history (
                    channel_id INTEGER PRIMARY KEY,
                    raw_data TEXT NOT NULL,
                    migrated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS download_selections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    peer_type TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    access_hash INTEGER DEFAULT NULL,
                    title TEXT NOT NULL,
                    username TEXT DEFAULT '',
                    access_status TEXT DEFAULT 'OK',
                    is_selected INTEGER DEFAULT 1,
                    date_added TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    FOREIGN KEY(channel_id) REFERENCES channels(channel_id),
                    UNIQUE(account_id, peer_type, channel_id)
                );

                CREATE INDEX IF NOT EXISTS idx_selections_account_active 
                ON download_selections(account_id, is_selected);

                CREATE TABLE IF NOT EXISTS channel_storage_mapping (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    peer_type TEXT NOT NULL DEFAULT 'channel',
                    channel_id INTEGER NOT NULL,
                    verified_title TEXT NOT NULL,
                    local_folder_name TEXT NOT NULL,
                    is_forum INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id, peer_type, channel_id)
                );

                CREATE TABLE IF NOT EXISTS forum_topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    topic_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    local_folder_name TEXT NOT NULL,
                    is_closed INTEGER DEFAULT 0,
                    is_hidden INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(channel_id, topic_id)
                );

                CREATE INDEX IF NOT EXISTS idx_channel_storage_mapping 
                ON channel_storage_mapping(account_id, peer_type, channel_id);

                CREATE INDEX IF NOT EXISTS idx_forum_topics 
                ON forum_topics(channel_id, topic_id);
            """)

    def migrate_to_stage_b(self, conn=None) -> bool:
        """
        Applies non-destructive additive schema migrations for Stage B (topic support and persistent folder mappings).
        Ensures composite identity (account_id, peer_type, channel_id, topic_id) for forum_topics.
        Can be called on live or cloned databases safely without affecting existing rows or constraints.
        """
        def _apply(c):
            # 1. Create channel_storage_mapping table if not exists
            c.execute("""
                CREATE TABLE IF NOT EXISTS channel_storage_mapping (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    peer_type TEXT NOT NULL DEFAULT 'channel',
                    channel_id INTEGER NOT NULL,
                    verified_title TEXT NOT NULL,
                    local_folder_name TEXT NOT NULL,
                    is_forum INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id, peer_type, channel_id)
                );
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_channel_storage_mapping ON channel_storage_mapping(account_id, peer_type, channel_id);")

            # 2. Check forum_topics table
            t_check = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forum_topics'").fetchone()
            if not t_check:
                c.execute("""
                    CREATE TABLE forum_topics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id INTEGER NOT NULL DEFAULT 0,
                        peer_type TEXT NOT NULL DEFAULT 'channel',
                        channel_id INTEGER NOT NULL,
                        topic_id INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        local_folder_name TEXT NOT NULL,
                        is_closed INTEGER DEFAULT 0,
                        is_hidden INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(account_id, peer_type, channel_id, topic_id)
                    );
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_forum_topics_scoped ON forum_topics(account_id, peer_type, channel_id, topic_id);")
            else:
                c_info = c.execute("PRAGMA table_info(forum_topics)").fetchall()
                cols = {r[1] for r in c_info}
                if "account_id" not in cols:
                    # Account-scoped table swap migration
                    c.execute("""
                        CREATE TABLE forum_topics_scoped_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            account_id INTEGER NOT NULL DEFAULT 0,
                            peer_type TEXT NOT NULL DEFAULT 'channel',
                            channel_id INTEGER NOT NULL,
                            topic_id INTEGER NOT NULL,
                            title TEXT NOT NULL,
                            local_folder_name TEXT NOT NULL,
                            is_closed INTEGER DEFAULT 0,
                            is_hidden INTEGER DEFAULT 0,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE(account_id, peer_type, channel_id, topic_id)
                        );
                    """)
                    c.execute("""
                        INSERT INTO forum_topics_scoped_new
                        (account_id, peer_type, channel_id, topic_id, title, local_folder_name, is_closed, is_hidden, created_at, updated_at)
                        SELECT
                            COALESCE(
                                (SELECT csm.account_id FROM channel_storage_mapping csm WHERE csm.channel_id = ft.channel_id LIMIT 1),
                                (SELECT ds.account_id FROM download_selections ds WHERE ds.channel_id = ft.channel_id LIMIT 1),
                                0
                            ) AS account_id,
                            COALESCE(
                                (SELECT csm.peer_type FROM channel_storage_mapping csm WHERE csm.channel_id = ft.channel_id LIMIT 1),
                                'channel'
                            ) AS peer_type,
                            ft.channel_id,
                            ft.topic_id,
                            ft.title,
                            ft.local_folder_name,
                            ft.is_closed,
                            ft.is_hidden,
                            ft.created_at,
                            ft.updated_at
                        FROM forum_topics ft;
                    """)
                    c.execute("DROP TABLE forum_topics;")
                    c.execute("ALTER TABLE forum_topics_scoped_new RENAME TO forum_topics;")
                    c.execute("CREATE INDEX IF NOT EXISTS idx_forum_topics_scoped ON forum_topics(account_id, peer_type, channel_id, topic_id);")

            # 3. Downloads table topic columns
            cursor = c.execute("PRAGMA table_info(downloads)")
            cols = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in cursor.fetchall()}
            if "topic_id" not in cols:
                c.execute("ALTER TABLE downloads ADD COLUMN topic_id INTEGER DEFAULT NULL")
            if "topic_title" not in cols:
                c.execute("ALTER TABLE downloads ADD COLUMN topic_title TEXT DEFAULT NULL")
            c.execute("CREATE INDEX IF NOT EXISTS idx_downloads_topic ON downloads(channel_id, topic_id);")
            c.commit()

        if conn is not None:
            _apply(conn)
        else:
            with self._get_connection() as c:
                _apply(c)
        return True

    def get_channel_storage_mapping(self, account_id: int, channel_id: int, peer_type: str = "channel") -> Optional[Dict[str, Any]]:
        """Retrieves persistent local folder mapping for a channel."""
        if account_id is None:
            return None
        norm_cid = normalize_channel_id(channel_id)
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM channel_storage_mapping
                WHERE account_id = ? AND peer_type = ? AND channel_id = ?
            """, (account_id, peer_type, norm_cid))
            row = cursor.fetchone()
            return dict(row) if row else None

    def set_channel_storage_mapping(
        self,
        account_id: int,
        channel_id: int,
        verified_title: str,
        local_folder_name: str,
        is_forum: bool = False,
        peer_type: str = "channel"
    ) -> Dict[str, Any]:
        """Sets or updates persistent local folder mapping for a channel."""
        if account_id is None:
            raise ValueError("account_id cannot be None")
        norm_cid = normalize_channel_id(channel_id)
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO channel_storage_mapping
                (account_id, peer_type, channel_id, verified_title, local_folder_name, is_forum, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, peer_type, channel_id) DO UPDATE SET
                    verified_title = excluded.verified_title,
                    local_folder_name = excluded.local_folder_name,
                    is_forum = excluded.is_forum,
                    updated_at = excluded.updated_at
            """, (account_id, peer_type, norm_cid, verified_title, local_folder_name, 1 if is_forum else 0, now_str, now_str))
            conn.commit()
        return {
            "account_id": account_id,
            "peer_type": peer_type,
            "channel_id": norm_cid,
            "verified_title": verified_title,
            "local_folder_name": local_folder_name,
            "is_forum": bool(is_forum)
        }

    def resolve_unique_channel_folder_name(
        self,
        account_id: int,
        channel_id: int,
        base_title: str,
        peer_type: str = "channel"
    ) -> str:
        """
        Ensures stable and collision-free local folder name for a channel.
        If an existing mapping exists for (account_id, peer_type, channel_id), retains it.
        If another channel in the same account already claims base_title, disambiguates as 'base_title [channel_id]'.
        """
        norm_cid = normalize_channel_id(channel_id)
        existing = self.get_channel_storage_mapping(account_id, norm_cid, peer_type)
        if existing and existing.get("local_folder_name"):
            return existing["local_folder_name"]

        safe_name = sanitize_folder_name(base_title)
        with self._get_connection() as conn:
            # Check if channel_storage_mapping table exists
            t_check = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_storage_mapping'").fetchone()
            if t_check:
                cursor = conn.execute("""
                    SELECT channel_id FROM channel_storage_mapping
                    WHERE account_id = ? AND local_folder_name = ? AND (channel_id != ? OR peer_type != ?)
                """, (account_id, safe_name, norm_cid, peer_type))
                if cursor.fetchone():
                    safe_name = f"{safe_name} [{norm_cid}]"
        return safe_name

    def save_forum_topic(
        self,
        channel_id: int,
        topic_id: int,
        title: str,
        local_folder_name: str,
        is_closed: bool = False,
        is_hidden: bool = False,
        account_id: int = None,
        peer_type: str = "channel"
    ) -> Dict[str, Any]:
        """Caches or updates forum topic metadata and folder mapping scoped to account and peer_type."""
        norm_cid = normalize_channel_id(channel_id)
        eff_acc_id = account_id if account_id is not None else 0
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            c_info = conn.execute("PRAGMA table_info(forum_topics)").fetchall()
            cols = {r[1] for r in c_info}
            if "account_id" in cols:
                conn.execute("""
                    INSERT INTO forum_topics
                    (account_id, peer_type, channel_id, topic_id, title, local_folder_name, is_closed, is_hidden, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, peer_type, channel_id, topic_id) DO UPDATE SET
                        title = excluded.title,
                        local_folder_name = excluded.local_folder_name,
                        is_closed = excluded.is_closed,
                        is_hidden = excluded.is_hidden,
                        updated_at = excluded.updated_at
                """, (eff_acc_id, peer_type, norm_cid, topic_id, title, local_folder_name, 1 if is_closed else 0, 1 if is_hidden else 0, now_str, now_str))
            else:
                conn.execute("""
                    INSERT INTO forum_topics
                    (channel_id, topic_id, title, local_folder_name, is_closed, is_hidden, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id, topic_id) DO UPDATE SET
                        title = excluded.title,
                        local_folder_name = excluded.local_folder_name,
                        is_closed = excluded.is_closed,
                        is_hidden = excluded.is_hidden,
                        updated_at = excluded.updated_at
                """, (norm_cid, topic_id, title, local_folder_name, 1 if is_closed else 0, 1 if is_hidden else 0, now_str, now_str))
            conn.commit()
        return {
            "account_id": eff_acc_id,
            "peer_type": peer_type,
            "channel_id": norm_cid,
            "topic_id": topic_id,
            "title": title,
            "local_folder_name": local_folder_name,
            "is_closed": bool(is_closed),
            "is_hidden": bool(is_hidden)
        }

    def get_forum_topics(self, channel_id: int, account_id: int = None, peer_type: str = "channel") -> List[Dict[str, Any]]:
        """Returns all cached forum topics for a channel, scoped by account_id and peer_type if provided."""
        norm_cid = normalize_channel_id(channel_id)
        with self._get_connection() as conn:
            t_check = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forum_topics'").fetchone()
            if not t_check:
                return []
            c_info = conn.execute("PRAGMA table_info(forum_topics)").fetchall()
            cols = {r[1] for r in c_info}
            if "account_id" in cols and account_id is not None:
                cursor = conn.execute("""
                    SELECT * FROM forum_topics
                    WHERE channel_id = ? AND account_id = ? AND peer_type = ?
                    ORDER BY topic_id ASC
                """, (norm_cid, account_id, peer_type))
            else:
                cursor = conn.execute("""
                    SELECT * FROM forum_topics
                    WHERE channel_id = ?
                    ORDER BY topic_id ASC
                """, (norm_cid,))
            return [dict(r) for r in cursor.fetchall()]

    def get_forum_topic(self, channel_id: int, topic_id: int, account_id: int = None, peer_type: str = "channel") -> Optional[Dict[str, Any]]:
        """Returns single cached forum topic by channel_id and topic_id, scoped by account_id and peer_type if provided."""
        norm_cid = normalize_channel_id(channel_id)
        with self._get_connection() as conn:
            t_check = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forum_topics'").fetchone()
            if not t_check:
                return None
            c_info = conn.execute("PRAGMA table_info(forum_topics)").fetchall()
            cols = {r[1] for r in c_info}
            if "account_id" in cols and account_id is not None:
                cursor = conn.execute("""
                    SELECT * FROM forum_topics
                    WHERE channel_id = ? AND topic_id = ? AND account_id = ? AND peer_type = ?
                """, (norm_cid, topic_id, account_id, peer_type))
            else:
                cursor = conn.execute("""
                    SELECT * FROM forum_topics
                    WHERE channel_id = ? AND topic_id = ?
                """, (norm_cid, topic_id))
            row = cursor.fetchone()
            return dict(row) if row else None

    def resolve_unique_topic_folder_name(
        self,
        channel_id: int,
        topic_id: int,
        base_title: str,
        account_id: int = None,
        peer_type: str = "channel"
    ) -> str:
        """
        Ensures stable and collision-free local folder name for a topic within a channel.
        If an existing mapping exists for (account_id, peer_type, channel_id, topic_id), retains it.
        If another topic in the same channel claims base_title, disambiguates as 'base_title [topic_id]'.
        """
        norm_cid = normalize_channel_id(channel_id)
        existing = self.get_forum_topic(norm_cid, topic_id, account_id=account_id, peer_type=peer_type)
        if existing and existing.get("local_folder_name"):
            return existing["local_folder_name"]

        safe_name = sanitize_folder_name(base_title)
        with self._get_connection() as conn:
            t_check = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='forum_topics'").fetchone()
            if t_check:
                c_info = conn.execute("PRAGMA table_info(forum_topics)").fetchall()
                cols = {r[1] for r in c_info}
                if "account_id" in cols and account_id is not None:
                    cursor = conn.execute("""
                        SELECT topic_id FROM forum_topics
                        WHERE account_id = ? AND peer_type = ? AND channel_id = ? AND local_folder_name = ? AND topic_id != ?
                    """, (account_id, peer_type, norm_cid, safe_name, topic_id))
                else:
                    cursor = conn.execute("""
                        SELECT topic_id FROM forum_topics
                        WHERE channel_id = ? AND local_folder_name = ? AND topic_id != ?
                    """, (norm_cid, safe_name, topic_id))
                if cursor.fetchone():
                    safe_name = f"{safe_name} [{topic_id}]"
        return safe_name

    def migrate_legacy_history_if_needed(self, legacy_json_path="download_history.json"):
        legacy_file = Path(legacy_json_path)
        if not legacy_file.exists():
            return 0
        try:
            with open(legacy_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load legacy history for migration: {e}")
            return 0

        groups = data.get("groups", {})
        if not groups:
            return 0

        migrated_count = 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for group_id_str, g_data in groups.items():
                try:
                    cid = normalize_channel_id(group_id_str)
                except ValueError:
                    continue

                cursor.execute("SELECT 1 FROM legacy_channel_history WHERE channel_id = ?", (cid,))
                if cursor.fetchone():
                    continue

                name = g_data.get("name", f"Group_{cid}")
                last_msg_id = g_data.get("last_msg_id", 0)
                now_str = datetime.now(timezone.utc).isoformat()

                cursor.execute("""
                    INSERT OR IGNORE INTO channels (channel_id, name, last_scanned_id, last_scanned_at, is_active)
                    VALUES (?, ?, ?, ?, 1)
                """, (cid, name, last_msg_id, now_str))

                cursor.execute("""
                    INSERT INTO legacy_channel_history (channel_id, raw_data, migrated_at)
                    VALUES (?, ?, ?)
                """, (cid, json.dumps(g_data), now_str))
                migrated_count += 1
            conn.commit()

        if migrated_count > 0:
            logger.info(f"Successfully migrated {migrated_count} legacy channel summaries into SQLite.")
        return migrated_count

    def ensure_channel(self, channel_id: int, name: str = None) -> int:
        """
        Ensures a parent channel record exists in the channels table before download insertion.
        Returns the normalized channel ID.
        """
        norm_id = normalize_channel_id(channel_id)
        channel_name = name or f"Channel_{norm_id}"
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO channels (channel_id, name, is_active)
                VALUES (?, ?, 1)
                ON CONFLICT(channel_id) DO NOTHING
            """, (norm_id, channel_name))
            conn.commit()
        return norm_id

    def update_channel(self, channel_id: int, name: str, last_scanned_id: int = None, is_active: int = 1):
        norm_id = normalize_channel_id(channel_id)
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            if last_scanned_id is not None:
                conn.execute("""
                    INSERT INTO channels (channel_id, name, last_scanned_id, last_scanned_at, is_active)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        name = excluded.name,
                        last_scanned_id = MAX(channels.last_scanned_id, excluded.last_scanned_id),
                        last_scanned_at = excluded.last_scanned_at,
                        is_active = excluded.is_active
                """, (norm_id, name, last_scanned_id, now_str, is_active))
            else:
                conn.execute("""
                    INSERT INTO channels (channel_id, name, is_active)
                    VALUES (?, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        name = excluded.name,
                        is_active = excluded.is_active
                """, (norm_id, name, is_active))
            conn.commit()

    def get_channel(self, channel_id: int):
        norm_id = normalize_channel_id(channel_id)
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (norm_id,))
            return cursor.fetchone()

    def get_all_active_channels(self):
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM channels WHERE is_active = 1 ORDER BY name ASC")
            return [dict(r) for r in cursor.fetchall()]

    def is_message_downloaded(self, channel_id: int, message_id: int) -> bool:
        norm_id = normalize_channel_id(channel_id)
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT status FROM downloads 
                WHERE channel_id = ? AND message_id = ?
            """, (norm_id, message_id))
            row = cursor.fetchone()
            if row and row["status"] == "COMPLETED":
                return True
            return False

    def get_download_record(self, channel_id: int, message_id: int):
        norm_id = normalize_channel_id(channel_id)
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM downloads 
                WHERE channel_id = ? AND message_id = ?
            """, (norm_id, message_id))
            return cursor.fetchone()

    def record_start(
        self,
        channel_id: int,
        message_id: int,
        file_name: str,
        file_size: int,
        relative_path: str,
        mime_type: str = "",
        topic_id: Optional[int] = None,
        topic_title: Optional[str] = None
    ):
        norm_id = normalize_channel_id(channel_id)
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("PRAGMA table_info(downloads)")
            cols = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in cursor.fetchall()}
            if "topic_id" in cols:
                conn.execute("""
                    INSERT INTO downloads (channel_id, message_id, file_name, file_size, relative_path, mime_type, status, started_at, topic_id, topic_title)
                    VALUES (?, ?, ?, ?, ?, ?, 'DOWNLOADING', ?, ?, ?)
                    ON CONFLICT(channel_id, message_id) DO UPDATE SET
                        file_name = excluded.file_name,
                        file_size = excluded.file_size,
                        relative_path = excluded.relative_path,
                        mime_type = excluded.mime_type,
                        status = 'DOWNLOADING',
                        started_at = excluded.started_at,
                        topic_id = COALESCE(excluded.topic_id, downloads.topic_id),
                        topic_title = COALESCE(excluded.topic_title, downloads.topic_title),
                        error_message = NULL
                """, (norm_id, message_id, file_name, file_size, relative_path, mime_type, now_str, topic_id, topic_title))
            else:
                conn.execute("""
                    INSERT INTO downloads (channel_id, message_id, file_name, file_size, relative_path, mime_type, status, started_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'DOWNLOADING', ?)
                    ON CONFLICT(channel_id, message_id) DO UPDATE SET
                        file_name = excluded.file_name,
                        file_size = excluded.file_size,
                        relative_path = excluded.relative_path,
                        mime_type = excluded.mime_type,
                        status = 'DOWNLOADING',
                        started_at = excluded.started_at,
                        error_message = NULL
                """, (norm_id, message_id, file_name, file_size, relative_path, mime_type, now_str))
            conn.commit()

    def record_completed(
        self,
        channel_id: int,
        message_id: int,
        file_name: str,
        file_size: int,
        relative_path: str,
        topic_id: Optional[int] = None,
        topic_title: Optional[str] = None
    ):
        norm_id = normalize_channel_id(channel_id)
        now_str = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("PRAGMA table_info(downloads)")
            cols = {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in cursor.fetchall()}
            if "topic_id" in cols:
                conn.execute("""
                    INSERT INTO downloads (channel_id, message_id, file_name, file_size, relative_path, status, completed_at, topic_id, topic_title)
                    VALUES (?, ?, ?, ?, ?, 'COMPLETED', ?, ?, ?)
                    ON CONFLICT(channel_id, message_id) DO UPDATE SET
                        file_name = excluded.file_name,
                        file_size = excluded.file_size,
                        relative_path = excluded.relative_path,
                        status = 'COMPLETED',
                        completed_at = excluded.completed_at,
                        topic_id = COALESCE(excluded.topic_id, downloads.topic_id),
                        topic_title = COALESCE(excluded.topic_title, downloads.topic_title),
                        error_message = NULL
                """, (norm_id, message_id, file_name, file_size, relative_path, now_str, topic_id, topic_title))
            else:
                conn.execute("""
                    INSERT INTO downloads (channel_id, message_id, file_name, file_size, relative_path, status, completed_at)
                    VALUES (?, ?, ?, ?, ?, 'COMPLETED', ?)
                    ON CONFLICT(channel_id, message_id) DO UPDATE SET
                        file_name = excluded.file_name,
                        file_size = excluded.file_size,
                        relative_path = excluded.relative_path,
                        status = 'COMPLETED',
                        completed_at = excluded.completed_at,
                        error_message = NULL
                """, (norm_id, message_id, file_name, file_size, relative_path, now_str))
            conn.commit()

    def record_failed(self, channel_id: int, message_id: int, error_msg: str):
        norm_id = normalize_channel_id(channel_id)
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE downloads 
                SET status = 'FAILED', 
                    error_message = ?, 
                    retry_count = retry_count + 1
                WHERE channel_id = ? AND message_id = ?
            """, (error_msg, norm_id, message_id))
            conn.commit()

    def get_failed_messages(self, channel_id: int = None, max_retries: int = 5):
        with self._get_connection() as conn:
            if channel_id is not None:
                norm_id = normalize_channel_id(channel_id)
                cursor = conn.execute("""
                    SELECT * FROM downloads 
                    WHERE channel_id = ? AND status = 'FAILED' AND retry_count < ?
                    ORDER BY message_id ASC
                """, (norm_id, max_retries))
            else:
                cursor = conn.execute("""
                    SELECT * FROM downloads 
                    WHERE status = 'FAILED' AND retry_count < ?
                    ORDER BY channel_id ASC, message_id ASC
                """, (max_retries,))
            return [dict(r) for r in cursor.fetchall()]

    def get_stats(self):
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    COUNT(*) as total_records,
                    SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) as failed,
                    SUM(CASE WHEN status = 'DOWNLOADING' THEN 1 ELSE 0 END) as in_progress,
                    SUM(CASE WHEN status = 'COMPLETED' THEN file_size ELSE 0 END) as total_bytes_downloaded
                FROM downloads
            """)
            row = dict(cursor.fetchone() or {})
            channel_count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
            row["total_channels"] = channel_count
            return row

    def backup_database(self, backup_dir: Path = None) -> Path:
        """Safely creates an online WAL-checkpointed backup of the database."""
        if backup_dir is None:
            backup_dir = self.db_path.parent / "backups"
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"{self.db_path.stem}_backup_{timestamp}.db"

        src = sqlite3.connect(str(self.db_path), timeout=30.0)
        try:
            src.execute("PRAGMA wal_checkpoint(PASSIVE);")
            dst = sqlite3.connect(str(backup_path))
            try:
                src.backup(dst, pages=100)
            finally:
                dst.close()
        finally:
            src.close()

        verify_conn = sqlite3.connect(str(backup_path))
        try:
            res = verify_conn.execute("PRAGMA integrity_check;").fetchone()
            if not res or res[0] != "ok":
                raise RuntimeError(f"Database backup integrity check failed: {res}")
        finally:
            verify_conn.close()

        logger.info(f"Verified database backup created at {backup_path}")
        return backup_path

    def migrate_legacy_active_channels(self, account_id: int, legacy_channel_ids: list, config_mgr=None) -> int:
        """
        Account-aware, one-time migration of active_channels from config.json into download_selections.
        Once recorded as completed for this account_id, never repopulates.
        """
        if account_id is None:
            raise ValueError("account_id is required for account-aware migration")

        flag_key = f"legacy_active_channels_migrated_{account_id}"
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT value FROM app_metadata WHERE key = ?", (flag_key,))
            row = cursor.fetchone()
            if row and row["value"] == "true":
                return 0  # Already migrated for this account

            migrated_count = 0
            now_str = datetime.now(timezone.utc).isoformat()

            for raw_cid in (legacy_channel_ids or []):
                try:
                    norm_cid = normalize_channel_id(raw_cid)
                except ValueError:
                    continue

                # Ensure parent channel exists in channels table for foreign key constraint
                conn.execute("""
                    INSERT INTO channels (channel_id, name, is_active)
                    VALUES (?, ?, 1)
                    ON CONFLICT(channel_id) DO NOTHING
                """, (norm_cid, f"Channel_{norm_cid}"))

                conn.execute("""
                    INSERT INTO download_selections 
                    (account_id, peer_type, channel_id, title, is_selected, date_added, last_updated)
                    VALUES (?, 'channel', ?, ?, 1, ?, ?)
                    ON CONFLICT(account_id, peer_type, channel_id) DO UPDATE SET
                        is_selected = 1,
                        last_updated = excluded.last_updated
                """, (account_id, norm_cid, f"Channel_{norm_cid}", now_str, now_str))
                migrated_count += 1

            conn.execute("""
                INSERT INTO app_metadata (key, value, updated_at)
                VALUES (?, 'true', ?)
                ON CONFLICT(key) DO UPDATE SET value = 'true', updated_at = excluded.updated_at
            """, (flag_key, now_str))
            conn.commit()

        logger.info(f"Account {account_id}: Migrated {migrated_count} legacy channels into download_selections.")
        return migrated_count

    def add_selection(
        self,
        account_id: int,
        channel_id: int,
        title: str,
        peer_type: str = "channel",
        access_hash: int = None,
        username: str = "",
        config_mgr = None
    ) -> bool:
        """
        Idempotently adds a channel/group to download_selections for the given account.
        Ensures foreign key constraint is satisfied by calling ensure_channel first.
        """
        if account_id is None:
            raise ValueError("account_id cannot be None")
        norm_cid = normalize_channel_id(channel_id)
        now_str = datetime.now(timezone.utc).isoformat()
        
        # 1. Ensure foreign key in channels table
        self.ensure_channel(norm_cid, title)

        # 2. Upsert selection
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO download_selections
                (account_id, peer_type, channel_id, access_hash, title, username, is_selected, date_added, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(account_id, peer_type, channel_id) DO UPDATE SET
                    access_hash = COALESCE(excluded.access_hash, download_selections.access_hash),
                    title = excluded.title,
                    username = excluded.username,
                    is_selected = 1,
                    last_updated = excluded.last_updated
            """, (account_id, peer_type, norm_cid, access_hash, title, username or "", now_str, now_str))
            conn.commit()

        if config_mgr is not None:
            self.sync_to_legacy_config(account_id, config_mgr)
        return True

    def update_selection_title(
        self,
        account_id: int,
        channel_id: int,
        peer_type: str,
        title: str,
        access_hash: int = None,
        username: str = None,
        access_status: str = None
    ) -> bool:
        """
        Safely updates the title and metadata of a saved channel selection in download_selections.
        Also keeps channels.name updated if title is a verified real name.
        Leaves account_id, peer_type, channel_id, is_selected, and date_added unchanged.
        """
        if account_id is None:
            raise ValueError("account_id cannot be None")
        if not title:
            return False

        norm_cid = normalize_channel_id(channel_id)
        now_str = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            # 1. Update download_selections
            conn.execute("""
                UPDATE download_selections
                SET title = ?,
                    access_hash = COALESCE(?, access_hash),
                    username = COALESCE(?, username),
                    access_status = COALESCE(?, access_status),
                    last_updated = ?
                WHERE account_id = ? AND peer_type = ? AND channel_id = ?
            """, (title, access_hash, username, access_status, now_str, account_id, peer_type, norm_cid))

            # 2. Update channels table name if title is real (not a placeholder)
            if not title.startswith("Channel_") and not title.startswith("[Unresolved]"):
                conn.execute("""
                    UPDATE channels
                    SET name = ?
                    WHERE channel_id = ?
                """, (title, norm_cid))

            conn.commit()
        return True


    def remove_selection(
        self,
        account_id: int,
        channel_id: int,
        peer_type: str = "channel",
        config_mgr = None
    ) -> bool:
        """
        Removes a selection by marking is_selected = 0.
        Does NOT delete downloaded files or download history.
        """
        if account_id is None:
            raise ValueError("account_id cannot be None")
        norm_cid = normalize_channel_id(channel_id)
        now_str = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute("""
                UPDATE download_selections
                SET is_selected = 0, last_updated = ?
                WHERE account_id = ? AND peer_type = ? AND channel_id = ?
            """, (now_str, account_id, peer_type, norm_cid))
            conn.commit()

        if config_mgr is not None:
            self.sync_to_legacy_config(account_id, config_mgr)
        return True

    def clear_selections(self, account_id: int, config_mgr = None) -> int:
        """
        Clears all selections for the specified account by setting is_selected = 0.
        Preserves all downloaded files and history.
        """
        if account_id is None:
            raise ValueError("account_id cannot be None")
        now_str = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE download_selections
                SET is_selected = 0, last_updated = ?
                WHERE account_id = ? AND is_selected = 1
            """, (now_str, account_id))
            cleared_count = cursor.rowcount
            conn.commit()

        if config_mgr is not None:
            self.sync_to_legacy_config(account_id, config_mgr)
        return cleared_count

    def get_active_selections(self, account_id: int) -> List[Dict[str, Any]]:
        """
        Returns list of all active selections for the specified account.
        Raises sqlite3.Error on database failure so callers fail safely.
        """
        if account_id is None:
            raise ValueError("account_id cannot be None")
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM download_selections
                WHERE account_id = ? AND is_selected = 1
                ORDER BY title ASC
            """, (account_id,))
            return [dict(r) for r in cursor.fetchall()]

    def is_selected(self, account_id: int, channel_id: int, peer_type: str = "channel") -> bool:
        """Checks if a specific channel is actively selected for the given account."""
        if account_id is None:
            return False
        norm_cid = normalize_channel_id(channel_id)
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT is_selected FROM download_selections
                WHERE account_id = ? AND peer_type = ? AND channel_id = ?
            """, (account_id, peer_type, norm_cid))
            row = cursor.fetchone()
            return bool(row and row["is_selected"] == 1)

    def get_selection(self, account_id: int, channel_id: int, peer_type: str = "channel") -> Optional[Dict[str, Any]]:
        """Returns single selection record by account_id, channel_id, peer_type."""
        if account_id is None:
            return None
        norm_cid = normalize_channel_id(channel_id)
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM download_selections
                WHERE account_id = ? AND peer_type = ? AND channel_id = ?
            """, (account_id, peer_type, norm_cid))
            row = cursor.fetchone()
            return dict(row) if row else None

    def sync_to_legacy_config(self, account_id: int, config_mgr) -> int:
        """Atomically synchronizes active channel IDs from SQLite into config.json for backward compatibility."""
        if account_id is None or config_mgr is None:
            return 0
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT channel_id FROM download_selections
                WHERE account_id = ? AND is_selected = 1
            """, (account_id,))
            active_ids = [r["channel_id"] for r in cursor.fetchall()]

        config_mgr.save_config({"active_channels": active_ids})
        return len(active_ids)

