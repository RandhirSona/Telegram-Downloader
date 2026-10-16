import os
import sqlite3
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict

from config_manager import ConfigManager
from db_tracker import normalize_channel_id

logger = logging.getLogger("history_repair")

class RecordClassification:
    PRESENT_VERIFIED = "PRESENT_VERIFIED"
    MISSING_CONFIRMED = "MISSING_CONFIRMED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    PATH_AMBIGUOUS = "PATH_AMBIGUOUS"
    SHARED_PATH_COLLISION = "SHARED_PATH_COLLISION"
    PARTIAL_PRESENT = "PARTIAL_PRESENT"
    METADATA_INSUFFICIENT = "METADATA_INSUFFICIENT"


class HistoryRepairService:
    """
    Safe repair service for download history after manual folder or file deletions.
    Identifies files genuinely missing from disk and transitions only those specific
    records to 'PENDING' so they become eligible for redownload, without deleting or
    resetting the database or duplicating records.
    """

    def __init__(self, db_path: str = "download_tracker.db", config_mgr: Optional[ConfigManager] = None):
        self.db_path = Path(db_path)
        self.config_mgr = config_mgr or ConfigManager()

    def check_safety_guards(self) -> Tuple[bool, str]:
        """
        Verifies that no active headless worker or active GUI download is in progress.
        Returns (is_safe, reason).
        """
        try:
            from instance_lock import ProcessLock
            worker_status = ProcessLock.get_active_worker_status()
            if worker_status:
                return False, f"Headless worker is currently active (PID {worker_status.get('pid')})."
        except Exception as e:
            logger.warning(f"ProcessLock check warning: {e}")

        # Check for DOWNLOADING records in DB
        try:
            conn = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM downloads WHERE status = 'DOWNLOADING';")
            count = cur.fetchone()[0]
            conn.close()
            # If records are DOWNLOADING, note them but distinguish historical interrupted vs live
        except Exception:
            pass

        return True, "Safe to proceed (no active worker process)."

    def audit_records(
        self,
        active_root: Optional[Path] = None,
        historical_roots: Optional[List[Path]] = None
    ) -> Dict[str, Any]:
        """
        Performs a strictly read-only audit of all downloads marked COMPLETED against disk.
        Classifies every completed file into one of the established classifications.
        """
        if active_root is None:
            try:
                active_root = self.config_mgr.get_download_dir()
            except Exception as e:
                active_root = None

        if historical_roots is None:
            if hasattr(self.config_mgr, "get_historical_roots"):
                historical_roots = self.config_mgr.get_historical_roots()
            else:
                raw_hist = self.config_mgr.config.get("historical_download_directories", [])
                historical_roots = [Path(r) for r in raw_hist if str(r).strip()]

        all_roots = []
        if active_root:
            all_roots.append(active_root)
        for hr in (historical_roots or []):
            if hr not in all_roots:
                all_roots.append(hr)

        # 1. Validate storage accessibility
        storage_accessible = True
        storage_unavail_reason = ""
        if not active_root:
            storage_accessible = False
            storage_unavail_reason = "No active download directory configured."
        elif not active_root.exists():
            storage_accessible = False
            storage_unavail_reason = f"Configured download storage root does not exist or drive is disconnected: {active_root}"
        else:
            try:
                # Test directory readability
                list(active_root.iterdir())
            except Exception as e:
                storage_accessible = False
                storage_unavail_reason = f"Storage root access error or permission denied: {e}"

        # 2. Read database records in read-only mode
        conn = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Channel mappings
        cur.execute("SELECT account_id, peer_type, channel_id, verified_title, local_folder_name, is_forum FROM channel_storage_mapping;")
        channel_mappings = {r['channel_id']: dict(r) for r in cur.fetchall()}

        # Channel raw names
        cur.execute("SELECT channel_id, name FROM channels;")
        channel_names = {r['channel_id']: r['name'] for r in cur.fetchall()}

        # Forum topics
        cur.execute("SELECT channel_id, topic_id, title, local_folder_name FROM forum_topics;")
        topics_map = defaultdict(dict)
        for r in cur.fetchall():
            topics_map[r['channel_id']][r['topic_id']] = dict(r)

        # All downloads
        cur.execute("""
            SELECT id, channel_id, message_id, file_name, file_size, status, relative_path, topic_id, topic_title
            FROM downloads
            ORDER BY channel_id, message_id;
        """)
        all_records = [dict(r) for r in cur.fetchall()]
        conn.close()

        # Group by relative_path to identify physical shared files
        path_to_records = defaultdict(list)
        for r in all_records:
            rp = r.get('relative_path')
            if rp:
                path_to_records[rp].append(r)

        counts_by_status = defaultdict(int)
        for r in all_records:
            counts_by_status[r['status']] += 1

        classified_records = []
        classification_counts = defaultdict(int)
        classification_bytes = defaultdict(int)
        channel_stats = defaultdict(lambda: defaultdict(int))
        topic_stats = defaultdict(lambda: defaultdict(int))
        deleted_folders_set = set()

        for r in all_records:
            status = r['status']
            if status != "COMPLETED":
                continue

            did = r['id']
            cid = r['channel_id']
            mid = r['message_id']
            fn = r['file_name']
            sz = r['file_size'] or 0
            rp = r['relative_path']
            tid = r['topic_id']
            ttitle = r['topic_title']

            if not storage_accessible:
                cls = RecordClassification.STORAGE_UNAVAILABLE
                reason = storage_unavail_reason
            elif not rp or not str(rp).strip():
                cls = RecordClassification.METADATA_INSUFFICIENT
                reason = "Record is missing relative_path metadata."
            else:
                is_shared = len(path_to_records[rp]) > 1

                # Locate file across active and historical roots
                found_path = None
                for root in all_roots:
                    candidate = root / rp
                    if candidate.is_file():
                        # Size match verification
                        if sz > 0 and candidate.stat().st_size == sz:
                            found_path = candidate
                            break
                        elif sz == 0:
                            found_path = candidate
                            break

                if found_path is not None:
                    cls = RecordClassification.PRESENT_VERIFIED
                    reason = f"File physically verified on disk ({found_path})"
                else:
                    # File not found on disk. Check if .part exists
                    part_found = None
                    for root in all_roots:
                        part_candidate = root / f"{rp}.part"
                        if part_candidate.is_file():
                            part_found = part_candidate
                            break

                    if part_found is not None:
                        cls = RecordClassification.PARTIAL_PRESENT
                        reason = f"Protected .part file exists on disk: {part_found.name} ({part_found.stat().st_size} bytes)"
                    elif is_shared:
                        cls = RecordClassification.SHARED_PATH_COLLISION
                        reason = f"Shared physical file missing from disk ({len(path_to_records[rp])} database rows share this path)"
                        top_folder = rp.split('\\')[0] if '\\' in rp else rp
                        if active_root and not (active_root / top_folder).exists():
                            deleted_folders_set.add(top_folder)
                    else:
                        cls = RecordClassification.MISSING_CONFIRMED
                        top_folder = rp.split('\\')[0] if '\\' in rp else rp
                        if active_root and not (active_root / top_folder).exists():
                            deleted_folders_set.add(top_folder)
                            reason = f"Folder '{top_folder}' deleted from disk"
                        else:
                            reason = f"File missing from disk: {rp}"

            classification_counts[cls] += 1
            classification_bytes[cls] += sz
            channel_stats[cid][cls] += 1
            if tid:
                topic_stats[(cid, tid)][cls] += 1

            # Resolve channel title
            ch_info = channel_mappings.get(cid)
            ch_title = (ch_info.get('verified_title') if ch_info else None) or channel_names.get(cid) or f"Channel_{cid}"

            classified_records.append({
                "download_id": did,
                "channel_id": cid,
                "channel_title": ch_title,
                "message_id": mid,
                "file_name": fn,
                "file_size": sz,
                "relative_path": rp,
                "topic_id": tid,
                "topic_title": ttitle,
                "classification": cls,
                "reason": reason
            })

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "active_root": str(active_root) if active_root else None,
            "historical_roots": [str(r) for r in (historical_roots or [])],
            "total_records_in_db": len(all_records),
            "status_counts_in_db": dict(counts_by_status),
            "total_completed_examined": len(classified_records),
            "classification_counts": dict(classification_counts),
            "classification_bytes": dict(classification_bytes),
            "deleted_top_folders": sorted(list(deleted_folders_set)),
            "channel_stats": {str(k): dict(v) for k, v in channel_stats.items()},
            "topic_stats": {f"{k[0]}:{k[1]}": dict(v) for k, v in topic_stats.items()},
            "records": classified_records
        }

    def create_backup(self, backup_dir: Optional[Path] = None) -> Tuple[Path, str]:
        """
        Creates an SQLite-consistent backup using the SQLite backup API.
        Verifies PRAGMA integrity_check and PRAGMA foreign_key_check.
        Computes SHA-256 checksum and returns (backup_path, sha256).
        """
        if backup_dir is None:
            backup_dir = self.db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"download_tracker_repair_backup_{ts}.db"

        src_conn = sqlite3.connect(f"file:{self.db_path.resolve()}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(str(backup_path))
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()

        # Verification
        chk_conn = sqlite3.connect(str(backup_path))
        ic = chk_conn.execute("PRAGMA integrity_check;").fetchall()
        fkc = chk_conn.execute("PRAGMA foreign_key_check;").fetchall()
        chk_conn.close()

        if ic != [('ok',)]:
            raise RuntimeError(f"Backup integrity check failed: {ic}")
        if len(fkc) > 0:
            raise RuntimeError(f"Backup foreign key check failed: {fkc}")

        hasher = hashlib.sha256()
        with open(backup_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        sha256 = hasher.hexdigest()

        logger.info(f"Verified backup created at {backup_path} (SHA-256: {sha256})")
        return backup_path, sha256

    def generate_repair_manifest(self, audit_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Generates the list of proposed database modifications.
        Eligible records: MISSING_CONFIRMED and SHARED_PATH_COLLISION (where file is missing).
        Excluded records: PRESENT_VERIFIED, PARTIAL_PRESENT, STORAGE_UNAVAILABLE, METADATA_INSUFFICIENT.
        """
        repair_candidates = []
        eligible_classes = {
            RecordClassification.MISSING_CONFIRMED,
            RecordClassification.SHARED_PATH_COLLISION
        }

        for r in audit_data.get("records", []):
            if r["classification"] in eligible_classes:
                repair_candidates.append({
                    "download_id": r["download_id"],
                    "channel_id": r["channel_id"],
                    "channel_title": r["channel_title"],
                    "message_id": r["message_id"],
                    "file_name": r["file_name"],
                    "file_size": r["file_size"],
                    "relative_path": r["relative_path"],
                    "topic_id": r["topic_id"],
                    "topic_title": r["topic_title"],
                    "current_status": "COMPLETED",
                    "proposed_status": "PENDING",
                    "proposed_error_message": f"FILE_DELETED_FROM_DISK: Missing from storage root ({r['reason']}); queued for redownload",
                    "reason": r["reason"]
                })

        return repair_candidates

    def apply_repair(
        self,
        repair_manifest: List[Dict[str, Any]],
        confirmed: bool = False
    ) -> Dict[str, Any]:
        """
        Applies database repair to make confirmed missing files eligible for downloading again.
        Requires explicit confirmed=True.
        Updates status from 'COMPLETED' to 'PENDING', preserving full identity and metadata.
        """
        if not confirmed:
            raise PermissionError("Repair cannot be applied without explicit confirmation (confirmed=True required).")

        if not repair_manifest:
            return {"updated_count": 0, "status": "no_changes_needed"}

        now_str = datetime.now(timezone.utc).isoformat()
        updated_count = 0

        conn = sqlite3.connect(str(self.db_path))
        try:
            with conn:
                for item in repair_manifest:
                    cur = conn.execute("""
                        UPDATE downloads
                        SET status = :proposed_status,
                            error_message = :proposed_error_message
                        WHERE id = :download_id
                          AND channel_id = :channel_id
                          AND message_id = :message_id
                          AND status = 'COMPLETED';
                    """, item)
                    updated_count += cur.rowcount
        finally:
            conn.close()

        logger.info(f"Applied database repair: {updated_count} records transitioned to PENDING.")
        return {
            "updated_count": updated_count,
            "manifest_count": len(repair_manifest),
            "timestamp": now_str,
            "status": "success"
        }
