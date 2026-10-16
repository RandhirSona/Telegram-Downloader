import unittest
import os
import shutil
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, r"C:\TelegramData")

from history_repair_service import HistoryRepairService, RecordClassification
from config_manager import ConfigManager
from db_tracker import DownloadTrackerDB


class TestHistoryRepairService(unittest.TestCase):
    """
    Isolated test suite for HistoryRepairService covering all 15 required scenarios.
    Operates strictly on disposable temporary directories and cloned SQLite databases.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.db_path = self.base_path / "test_tracker.db"
        self.active_root = self.base_path / "active_downloads"
        self.active_root.mkdir(parents=True, exist_ok=True)
        self.hist_root = self.base_path / "historical_downloads"
        self.hist_root.mkdir(parents=True, exist_ok=True)

        self._init_mock_db()
        self.config_mgr = MagicMock(spec=ConfigManager)
        self.config_mgr.get_download_dir.return_value = self.active_root
        self.config_mgr.get_historical_roots.return_value = [self.hist_root]
        self.config_mgr.config = {
            "download_directory": str(self.active_root),
            "historical_download_directories": [str(self.hist_root)]
        }

        self.service = HistoryRepairService(
            db_path=str(self.db_path),
            config_mgr=self.config_mgr
        )

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except OSError:
            pass

    def _init_mock_db(self):
        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL UNIQUE,
                name TEXT,
                access_hash INTEGER,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE channel_storage_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                peer_type TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                verified_title TEXT NOT NULL,
                local_folder_name TEXT NOT NULL,
                is_forum INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, peer_type, channel_id)
            );
            CREATE TABLE forum_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                peer_type TEXT,
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
            CREATE TABLE downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                mime_type TEXT,
                status TEXT NOT NULL CHECK(status IN ('PENDING', 'DOWNLOADING', 'COMPLETED', 'FAILED', 'SKIPPED')),
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                downloaded_bytes INTEGER DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                relative_path TEXT,
                topic_id INTEGER,
                topic_title TEXT,
                UNIQUE(channel_id, message_id)
            );
        """)
        conn.commit()
        conn.close()

    def _insert_download(self, cid, mid, fname, size, rel_path, status="COMPLETED", topic_id=None, topic_title=None):
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.execute("""
                INSERT INTO downloads (channel_id, message_id, file_name, file_size, relative_path, status, topic_id, topic_title)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (cid, mid, fname, size, rel_path, status, topic_id, topic_title))
        conn.close()

    def _create_physical_file(self, root, rel_path, size=1024):
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            f.write(b"X" * size)
        return target

    def test_01_entire_channel_directory_deleted(self):
        """Scenario 1: Entire channel directory deleted on disk."""
        ch_folder = "Channel_100"
        self._insert_download(100, 1, "doc1.pdf", 500, f"{ch_folder}\\doc1.pdf")
        self._insert_download(100, 2, "doc2.pdf", 600, f"{ch_folder}\\doc2.pdf")

        # Do not create the files on disk (channel folder deleted)
        audit = self.service.audit_records()
        self.assertEqual(audit["classification_counts"][RecordClassification.MISSING_CONFIRMED], 2)
        self.assertIn(ch_folder, audit["deleted_top_folders"])

        manifest = self.service.generate_repair_manifest(audit)
        self.assertEqual(len(manifest), 2)

        res = self.service.apply_repair(manifest, confirmed=True)
        self.assertEqual(res["updated_count"], 2)

        # Verify DB records are now PENDING
        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()
        cur.execute("SELECT status, error_message FROM downloads WHERE channel_id = 100;")
        rows = cur.fetchall()
        conn.close()
        for status, err in rows:
            self.assertEqual(status, "PENDING")
            self.assertIn("FILE_DELETED_FROM_DISK", err)

    def test_02_single_forum_topic_directory_deleted(self):
        """Scenario 2: Single forum-topic directory deleted while others remain."""
        # Topic A files physically present
        self._insert_download(200, 1, "topicA_1.pdf", 100, r"Channel_200\Topic_A\topicA_1.pdf", topic_id=1, topic_title="Topic A")
        self._create_physical_file(self.active_root, r"Channel_200\Topic_A\topicA_1.pdf", size=100)

        # Topic B files deleted on disk
        self._insert_download(200, 2, "topicB_1.pdf", 200, r"Channel_200\Topic_B\topicB_1.pdf", topic_id=2, topic_title="Topic B")

        audit = self.service.audit_records()
        self.assertEqual(audit["classification_counts"][RecordClassification.PRESENT_VERIFIED], 1)
        self.assertEqual(audit["classification_counts"][RecordClassification.MISSING_CONFIRMED], 1)

        # Only Topic B record in repair manifest
        manifest = self.service.generate_repair_manifest(audit)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["message_id"], 2)

    def test_03_single_completed_file_deleted(self):
        """Scenario 3: Single completed file deleted from existing folder."""
        self._insert_download(300, 1, "keep.pdf", 100, r"Channel_300\keep.pdf")
        self._insert_download(300, 2, "deleted.pdf", 200, r"Channel_300\deleted.pdf")

        self._create_physical_file(self.active_root, r"Channel_300\keep.pdf", size=100)
        # deleted.pdf is not created

        audit = self.service.audit_records()
        self.assertEqual(audit["classification_counts"][RecordClassification.PRESENT_VERIFIED], 1)
        self.assertEqual(audit["classification_counts"][RecordClassification.MISSING_CONFIRMED], 1)

    def test_04_completed_file_present_under_historical_root(self):
        """Scenario 4: Completed file present under historical root must NOT be marked missing."""
        self._insert_download(400, 1, "hist.pdf", 300, r"Channel_400\hist.pdf")
        # Place only in historical root
        self._create_physical_file(self.hist_root, r"Channel_400\hist.pdf", size=300)

        audit = self.service.audit_records()
        self.assertEqual(audit["classification_counts"].get(RecordClassification.MISSING_CONFIRMED, 0), 0)
        self.assertEqual(audit["classification_counts"][RecordClassification.PRESENT_VERIFIED], 1)

    def test_05_configured_drive_disconnected(self):
        """Scenario 5: Configured drive disconnected or missing -> STORAGE_UNAVAILABLE."""
        disconnected_path = Path("Z:\\DisconnectedDrive\\Downloads")
        self.config_mgr.get_download_dir.return_value = disconnected_path

        self._insert_download(500, 1, "doc.pdf", 100, r"Channel_500\doc.pdf")

        audit = self.service.audit_records(active_root=disconnected_path)
        self.assertEqual(audit["classification_counts"][RecordClassification.STORAGE_UNAVAILABLE], 1)
        self.assertEqual(audit["classification_counts"].get(RecordClassification.MISSING_CONFIRMED, 0), 0)

        # Manifest must be empty
        manifest = self.service.generate_repair_manifest(audit)
        self.assertEqual(len(manifest), 0)

    def test_06_permission_denied_folder(self):
        """Scenario 6: Inaccessible / permission denied folder -> STORAGE_UNAVAILABLE."""
        self._insert_download(600, 1, "doc.pdf", 100, r"Channel_600\doc.pdf")

        with patch.object(Path, "iterdir", side_effect=PermissionError("Access is denied")):
            audit = self.service.audit_records()
            self.assertEqual(audit["classification_counts"][RecordClassification.STORAGE_UNAVAILABLE], 1)
            manifest = self.service.generate_repair_manifest(audit)
            self.assertEqual(len(manifest), 0)

    def test_07_two_database_records_sharing_one_physical_file(self):
        """Scenario 7: Two DB records sharing one physical file."""
        rel = r"Channel_700\shared.pdf"
        self._insert_download(700, 10, "shared.pdf", 500, rel)
        self._insert_download(700, 11, "shared.pdf", 500, rel)

        # Case A: File is physically present -> both PRESENT_VERIFIED
        self._create_physical_file(self.active_root, rel, size=500)
        audit_present = self.service.audit_records()
        self.assertEqual(audit_present["classification_counts"][RecordClassification.PRESENT_VERIFIED], 2)

        # Case B: File is deleted -> both SHARED_PATH_COLLISION and in manifest
        (self.active_root / rel).unlink()
        audit_missing = self.service.audit_records()
        self.assertEqual(audit_missing["classification_counts"][RecordClassification.SHARED_PATH_COLLISION], 2)

        manifest = self.service.generate_repair_manifest(audit_missing)
        self.assertEqual(len(manifest), 2)
        res = self.service.apply_repair(manifest, confirmed=True)
        self.assertEqual(res["updated_count"], 2)

    def test_08_missing_completed_media_with_verified_topic_id(self):
        """Scenario 8: Missing completed media with verified topic ID preserves topic association."""
        self._insert_download(800, 1, "math.pdf", 400, r"School\Math\math.pdf", topic_id=42, topic_title="Mathematics")

        audit = self.service.audit_records()
        manifest = self.service.generate_repair_manifest(audit)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["topic_id"], 42)
        self.assertEqual(manifest[0]["topic_title"], "Mathematics")

        self.service.apply_repair(manifest, confirmed=True)

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM downloads WHERE channel_id = 800 AND message_id = 1;").fetchone()
        conn.close()

        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["topic_id"], 42)
        self.assertEqual(row["topic_title"], "Mathematics")
        self.assertEqual(row["file_size"], 400)

    def test_09_missing_media_with_ambiguous_or_missing_metadata(self):
        """Scenario 9: Missing relative_path -> METADATA_INSUFFICIENT, excluded from repair."""
        self._insert_download(900, 1, "corrupt.pdf", 100, rel_path="")

        audit = self.service.audit_records()
        self.assertEqual(audit["classification_counts"][RecordClassification.METADATA_INSUFFICIENT], 1)

        manifest = self.service.generate_repair_manifest(audit)
        self.assertEqual(len(manifest), 0)

    def test_10_existing_part_file_protection(self):
        """Scenario 10: Existing .part file -> PARTIAL_PRESENT, strictly protected and excluded."""
        rel = r"Channel_1000\video.mp4"
        self._insert_download(1000, 1, "video.mp4", 10000, rel)
        self._create_physical_file(self.active_root, f"{rel}.part", size=5000)

        audit = self.service.audit_records()
        self.assertEqual(audit["classification_counts"][RecordClassification.PARTIAL_PRESENT], 1)

        manifest = self.service.generate_repair_manifest(audit)
        self.assertEqual(len(manifest), 0)

    def test_11_repeated_repair_execution_without_duplicate_changes(self):
        """Scenario 11: Repeated repair execution is idempotent and makes 0 duplicate changes."""
        self._insert_download(1100, 1, "doc.pdf", 100, r"Channel_1100\doc.pdf")

        audit1 = self.service.audit_records()
        manifest1 = self.service.generate_repair_manifest(audit1)
        res1 = self.service.apply_repair(manifest1, confirmed=True)
        self.assertEqual(res1["updated_count"], 1)

        # Second audit: row is now PENDING, so total completed examined is 0
        audit2 = self.service.audit_records()
        self.assertEqual(audit2["total_completed_examined"], 0)
        manifest2 = self.service.generate_repair_manifest(audit2)
        self.assertEqual(len(manifest2), 0)

        res2 = self.service.apply_repair(manifest2, confirmed=True)
        self.assertEqual(res2["updated_count"], 0)

    def test_12_retry_followed_by_successful_database_completion(self):
        """Scenario 12: Retry lifecycle from PENDING -> DOWNLOADING -> COMPLETED."""
        tracker = DownloadTrackerDB(str(self.db_path))
        self._insert_download(1200, 1, "book.pdf", 500, r"Channel_1200\book.pdf")

        # 1. Repair marks it PENDING
        audit = self.service.audit_records()
        manifest = self.service.generate_repair_manifest(audit)
        self.service.apply_repair(manifest, confirmed=True)

        self.assertFalse(tracker.is_message_downloaded(1200, 1))

        # 2. Downloader starts
        tracker.record_start(1200, 1, "book.pdf", 500, r"Channel_1200\book.pdf")
        rec_start = tracker.get_download_record(1200, 1)
        self.assertEqual(rec_start["status"], "DOWNLOADING")

        # 3. Downloader completes
        tracker.record_completed(1200, 1, "book.pdf", 500, r"Channel_1200\book.pdf")
        rec_comp = tracker.get_download_record(1200, 1)
        self.assertEqual(rec_comp["status"], "COMPLETED")
        self.assertTrue(tracker.is_message_downloaded(1200, 1))

    def test_13_previously_completed_files_not_being_downloaded_again(self):
        """Scenario 13: Files physically present remain COMPLETED and are skipped."""
        tracker = DownloadTrackerDB(str(self.db_path))
        rel = r"Channel_1300\existing.pdf"
        self._insert_download(1300, 1, "existing.pdf", 100, rel)
        self._create_physical_file(self.active_root, rel, size=100)

        audit = self.service.audit_records()
        self.assertEqual(audit["classification_counts"][RecordClassification.PRESENT_VERIFIED], 1)
        manifest = self.service.generate_repair_manifest(audit)
        self.assertEqual(len(manifest), 0)

        # Database remains COMPLETED
        self.assertTrue(tracker.is_message_downloaded(1300, 1))

    def test_14_user_changes_download_root(self):
        """Scenario 14: User changes download root; old root in historical roots retains PRESENT_VERIFIED."""
        new_active = self.base_path / "new_active"
        new_active.mkdir(parents=True, exist_ok=True)
        old_root = self.active_root

        rel = r"Channel_1400\file.pdf"
        self._insert_download(1400, 1, "file.pdf", 250, rel)
        self._create_physical_file(old_root, rel, size=250)

        # Audit with new active root and old_root in historical roots
        audit = self.service.audit_records(active_root=new_active, historical_roots=[old_root])
        self.assertEqual(audit["classification_counts"][RecordClassification.PRESENT_VERIFIED], 1)
        self.assertEqual(audit["classification_counts"].get(RecordClassification.MISSING_CONFIRMED, 0), 0)

    def test_15_backup_creation_and_integrity(self):
        """Scenario 15: Verified SQLite backup creation, integrity check and hash verification."""
        self._insert_download(1500, 1, "data.pdf", 100, r"Channel_1500\data.pdf")

        backup_dir = self.base_path / "test_backups"
        backup_path, sha256 = self.service.create_backup(backup_dir=backup_dir)

        self.assertTrue(backup_path.exists())
        self.assertEqual(len(sha256), 64)

        # Verify integrity of backup
        conn = sqlite3.connect(str(backup_path))
        ic = conn.execute("PRAGMA integrity_check;").fetchall()
        fkc = conn.execute("PRAGMA foreign_key_check;").fetchall()
        row = conn.execute("SELECT count(*) FROM downloads;").fetchone()
        conn.close()

        self.assertEqual(ic, [('ok',)])
        self.assertEqual(fkc, [])
        self.assertEqual(row[0], 1)

    def test_16_active_worker_safety_guard(self):
        """Scenario 16: Active worker process prevents unsafe execution."""
        from instance_lock import ProcessLock
        with patch.object(ProcessLock, "get_active_worker_status", return_value={"pid": 9999, "role": "worker"}):
            is_safe, reason = self.service.check_safety_guards()
            self.assertFalse(is_safe)
            self.assertIn("Headless worker is currently active", reason)

        with patch.object(ProcessLock, "get_active_worker_status", return_value=None):
            is_safe, reason = self.service.check_safety_guards()
            self.assertTrue(is_safe)


if __name__ == "__main__":
    unittest.main()
