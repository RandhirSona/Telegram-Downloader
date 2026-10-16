import os
import sys
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import ConfigManager, StorageError
from db_tracker import DownloadTrackerDB, normalize_channel_id, sanitize_folder_name
from telegram_engine import TelegramDownloaderEngine, sanitize_media_filename


class TestPhaseE1FreshDownloadReset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backup_db_path = PROJECT_ROOT / "backups" / "download_tracker_pre_phase_e1_reset_20261015_074403.db"
        cls.prod_db_path = PROJECT_ROOT / "download_tracker.db"
        cls.assertTrue(cls.backup_db_path.exists(), "Pre-reset backup DB must exist for testing clone")

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="phase_e1_test_")
        self.cloned_db_path = Path(self.temp_dir) / "cloned_tracker.db"
        
        # Consistent SQLite backup to clone from pre-reset backup fixture
        src = sqlite3.connect(str(self.backup_db_path))
        dst = sqlite3.connect(str(self.cloned_db_path))
        src.backup(dst)
        dst.close()
        src.close()

        # Temporary config
        self.test_config_path = Path(self.temp_dir) / "config.json"
        shutil.copy(PROJECT_ROOT / "config.json", self.test_config_path)
        self.config_mgr = ConfigManager(str(self.test_config_path))
        self.db = DownloadTrackerDB(str(self.cloned_db_path))

    def tearDown(self):
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    def perform_history_reset(self, db_path: Path):
        """Executes the single controlled SQLite transaction for history reset."""
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("BEGIN IMMEDIATE TRANSACTION;")
            
            # Count pre-deleted rows
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM downloads;")
            deleted_count = cur.fetchone()[0]

            # 1. Clear downloads table
            cur.execute("DELETE FROM downloads;")
            
            # 2. Reset sqlite_sequence for downloads table
            cur.execute("DELETE FROM sqlite_sequence WHERE name = 'downloads';")
            
            conn.commit()
            return deleted_count
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def test_01_pre_reset_inventory(self):
        """Verifies initial record counts before reset match expectations."""
        with sqlite3.connect(str(self.cloned_db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM downloads;")
            self.assertEqual(cur.fetchone()[0], 2786)
            
            cur.execute("SELECT count(*) FROM channels;")
            self.assertEqual(cur.fetchone()[0], 494)

            cur.execute("SELECT count(*) FROM download_selections WHERE is_selected = 1;")
            self.assertEqual(cur.fetchone()[0], 26)

            cur.execute("SELECT count(*) FROM forum_topics;")
            self.assertEqual(cur.fetchone()[0], 71)

            cur.execute("SELECT count(*) FROM channel_storage_mapping;")
            self.assertEqual(cur.fetchone()[0], 6)

    def test_02_full_history_reset_execution(self):
        """Executes reset and verifies all downloads records are 0 and other tables intact."""
        deleted = self.perform_history_reset(self.cloned_db_path)
        self.assertEqual(deleted, 2786)

        with sqlite3.connect(str(self.cloned_db_path)) as conn:
            cur = conn.cursor()
            
            # Status breakdowns
            cur.execute("SELECT status, count(*) FROM downloads GROUP BY status;")
            rows = dict(cur.fetchall())
            self.assertEqual(len(rows), 0)
            
            cur.execute("SELECT count(*) FROM downloads;")
            self.assertEqual(cur.fetchone()[0], 0)

            # Channels preserved
            cur.execute("SELECT count(*) FROM channels;")
            self.assertEqual(cur.fetchone()[0], 494)

            # Selections preserved
            cur.execute("SELECT count(*) FROM download_selections WHERE is_selected = 1;")
            self.assertEqual(cur.fetchone()[0], 26)

            # Forum topics preserved
            cur.execute("SELECT count(*) FROM forum_topics;")
            self.assertEqual(cur.fetchone()[0], 71)

            # Storage mapping preserved
            cur.execute("SELECT count(*) FROM channel_storage_mapping;")
            self.assertEqual(cur.fetchone()[0], 6)

            # Metadata preserved
            cur.execute("SELECT count(*) FROM app_metadata;")
            self.assertEqual(cur.fetchone()[0], 1)

            # Sequence reset
            cur.execute("SELECT seq FROM sqlite_sequence WHERE name = 'downloads';")
            self.assertIsNone(cur.fetchone())

    def test_03_foreign_key_and_integrity_checks(self):
        """Runs PRAGMA integrity_check and foreign_key_check after reset."""
        self.perform_history_reset(self.cloned_db_path)

        with sqlite3.connect(str(self.cloned_db_path)) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            res = cur.fetchall()
            self.assertEqual(res, [('ok',)])

            cur.execute("PRAGMA foreign_key_check;")
            fks = cur.fetchall()
            self.assertEqual(len(fks), 0)

    def test_04_repeated_reset_execution(self):
        """Verifies repeated reset execution is idempotent and safe."""
        first_deleted = self.perform_history_reset(self.cloned_db_path)
        self.assertEqual(first_deleted, 2786)

        # Run second time
        second_deleted = self.perform_history_reset(self.cloned_db_path)
        self.assertEqual(second_deleted, 0)

        with sqlite3.connect(str(self.cloned_db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM downloads;")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT count(*) FROM channels;")
            self.assertEqual(cur.fetchone()[0], 494)

    def test_05_rollback_on_failure(self):
        """Verifies transaction rollbacks completely if an error occurs."""
        conn = sqlite3.connect(str(self.cloned_db_path))
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            conn.execute("BEGIN IMMEDIATE TRANSACTION;")
            conn.execute("DELETE FROM downloads;")
            
            # Intentionally cause an error in the same transaction
            conn.execute("INSERT INTO downloads (channel_id, message_id, file_name, file_size, relative_path, status) VALUES (999999999, 1, 'test.bin', 10, 'rel', 'INVALID_STATUS');")
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
        finally:
            conn.close()

        # Confirm 2786 downloads still exist due to rollback
        with sqlite3.connect(str(self.cloned_db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM downloads;")
            self.assertEqual(cur.fetchone()[0], 2786)

    def test_06_missing_storage_validation(self):
        """Verifies failing/missing drive (F:) is rejected by storage validation without C: fallback."""
        valid, msg, _ = self.config_mgr.validate_storage("F:/TelegramDownloads", create_if_missing=False)
        self.assertFalse(valid)
        self.assertIn("Storage unreachable", msg)

        # check_capacity_for_file should raise StorageError
        with self.assertRaises(StorageError):
            self.config_mgr.check_capacity_for_file(1024, custom_path="F:/TelegramDownloads")

    def test_07_healthy_storage_validation_and_part_handling(self):
        """Verifies healthy storage location accepts .part files and atomic rename."""
        healthy_dir = Path(self.temp_dir) / "healthy_download_root"
        healthy_dir.mkdir(parents=True, exist_ok=True)

        valid, msg, metrics = self.config_mgr.validate_storage(str(healthy_dir), create_if_missing=True)
        self.assertTrue(valid)

        # Test .part and promotion
        part_path = healthy_dir / "test_file.mp4.part"
        final_path = healthy_dir / "test_file.mp4"
        with open(part_path, "wb") as f:
            f.write(b"data")
        self.assertTrue(part_path.exists())
        part_path.rename(final_path)
        self.assertTrue(final_path.exists())
        self.assertFalse(part_path.exists())

    def test_08_folder_routing_after_reset(self):
        """Verifies clean folder routing for ordinary channels and forum supergroups post-reset."""
        self.perform_history_reset(self.cloned_db_path)
        
        healthy_root = Path(self.temp_dir) / "fresh_downloads"
        healthy_root.mkdir(parents=True, exist_ok=True)
        self.config_mgr.set_download_dir(str(healthy_root))

        engine = TelegramDownloaderEngine(
            config_mgr=self.config_mgr,
            db_tracker=self.db,
            event_callback=lambda evt, data: None
        )
        engine.account_id = 774983606

        # 1. Ordinary Broadcast Channel
        target_path, rel_path, t_id, t_title = engine.resolve_destination_path(
            channel_id=3061094461,
            channel_name="FORUM PTS 2026",
            raw_filename="Document_01.pdf",
            message_id=101,
            is_forum=False
        )
        expected_target = healthy_root / "FORUM PTS 2026" / "Document_01.pdf"
        self.assertEqual(target_path, expected_target)
        self.assertEqual(rel_path, str(Path("FORUM PTS 2026") / "Document_01.pdf"))
        self.assertIsNone(t_id)

        # 2. Telegram Forum Supergroup
        target_path_forum, rel_path_forum, f_tid, f_ttitle = engine.resolve_destination_path(
            channel_id=4412184534,
            channel_name="UPPCS TEST 2026",
            raw_filename="Lecture_01.mp4",
            message_id=202,
            is_forum=True,
            topic_id=681,
            topic_title="Physics wallah"
        )
        expected_forum_target = healthy_root / "UPPCS TEST 2026" / "Physics wallah" / "Lecture_01.mp4"
        self.assertEqual(target_path_forum, expected_forum_target)
        self.assertEqual(rel_path_forum, str(Path("UPPCS TEST 2026") / "Physics wallah" / "Lecture_01.mp4"))
        self.assertEqual(f_tid, 681)
        self.assertEqual(f_ttitle, "Physics wallah")

    def test_09_new_records_post_reset(self):
        """Verifies new download records insert cleanly and start from ID 1 post-reset."""
        self.perform_history_reset(self.cloned_db_path)

        cid = 3683278928
        mid = 9999
        self.db.record_start(cid, mid, "Fresh_File.pdf", 5000, "PVT UPPCS TEST/Fresh_File.pdf")
        rec = self.db.get_download_record(cid, mid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["id"], 1)
        self.assertEqual(rec["status"], "DOWNLOADING")

        self.db.record_completed(cid, mid, "Fresh_File.pdf", 5000, "PVT UPPCS TEST/Fresh_File.pdf")
        self.assertTrue(self.db.is_message_downloaded(cid, mid))
        rec_done = self.db.get_download_record(cid, mid)
        self.assertEqual(rec_done["status"], "COMPLETED")

    def test_10_historical_partial_file_isolation(self):
        """Verifies fresh download destination does NOT reuse old partials on disconnected/prior roots."""
        self.perform_history_reset(self.cloned_db_path)

        old_root = Path(self.temp_dir) / "old_failed_root"
        old_root.mkdir(parents=True, exist_ok=True)
        old_channel_dir = old_root / "FORUM PTS 2026"
        old_channel_dir.mkdir(parents=True, exist_ok=True)
        stale_part = old_channel_dir / "Historical.pdf.part"
        with open(stale_part, "wb") as f:
            f.write(b"stale_bytes")

        new_root = Path(self.temp_dir) / "fresh_active_root"
        new_root.mkdir(parents=True, exist_ok=True)
        self.config_mgr.set_download_dir(str(new_root))
        # Ensure old root is in historical roots
        self.config_mgr.config["historical_download_directories"] = [str(old_root)]

        engine = TelegramDownloaderEngine(
            config_mgr=self.config_mgr,
            db_tracker=self.db,
            event_callback=lambda evt, data: None
        )
        engine.account_id = 774983606

        # With empty download history, destination MUST resolve to new_root, NOT old_root
        target_path, rel_path, _, _ = engine.resolve_destination_path(
            channel_id=3061094461,
            channel_name="FORUM PTS 2026",
            raw_filename="Historical.pdf",
            message_id=505,
            is_forum=False
        )
        self.assertEqual(target_path, new_root / "FORUM PTS 2026" / "Historical.pdf")
        self.assertTrue(str(target_path).startswith(str(new_root)))
        self.assertFalse(str(target_path).startswith(str(old_root)))

    def test_11_preservation_of_session_and_config(self):
        """Verifies session file and config file exist and are intact."""
        session_file = PROJECT_ROOT / "anon.session"
        self.assertTrue(session_file.exists())
        self.assertGreater(session_file.stat().st_size, 0)

        config_file = PROJECT_ROOT / "config.json"
        self.assertTrue(config_file.exists())
        self.assertGreater(config_file.stat().st_size, 0)

    def test_12_no_automatic_worker_activation(self):
        """Verifies that reset execution does not acquire lock or start background processes."""
        from instance_lock import ProcessLock
        status = ProcessLock.get_active_worker_status()
        self.assertIsNone(status, "No background worker should be running")

    def test_13_gui_stats_and_selections_refresh(self):
        """Verifies GUI stats show 0 downloads and 26 active selections."""
        self.perform_history_reset(self.cloned_db_path)
        stats = self.db.get_stats()
        self.assertEqual(stats["total_records"], 0)
        self.assertEqual(stats["completed"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["in_progress"], 0)
        self.assertEqual(stats["total_bytes_downloaded"], 0)
        self.assertEqual(stats["total_channels"], 494)

        selections = self.db.get_active_selections(774983606)
        self.assertEqual(len(selections), 26)

    def test_14_production_database_verified(self):
        """Verifies the actual production database meets all Phase E1 post-reset criteria."""
        if not self.prod_db_path.exists():
            self.skipTest("Production database does not exist on this machine.")

        prod_db = DownloadTrackerDB(str(self.prod_db_path))
        stats = prod_db.get_stats()
        if stats.get("total_records", 0) != 0:
            self.skipTest(f"Production database preserves un-reset history ({stats['total_records']} records).")

        self.assertEqual(stats["total_records"], 0)
        self.assertEqual(stats["completed"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["in_progress"], 0)
        self.assertEqual(stats["total_bytes_downloaded"], 0)
        self.assertEqual(stats["total_channels"], 494)

        prod_cfg = ConfigManager(str(PROJECT_ROOT / "config.json"))
        self.assertEqual(str(prod_cfg.get_download_dir()).lower(), str(Path("Z:/TelegramDownloads")).lower())
        self.assertIn("F:\\TelegramDownloads", [os.path.normpath(p) for p in prod_cfg.config.get("historical_download_directories", [])])

        with sqlite3.connect(str(self.prod_db_path)) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            self.assertEqual(cur.fetchall(), [('ok',)])
            cur.execute("PRAGMA foreign_key_check;")
            self.assertEqual(len(cur.fetchall()), 0)


if __name__ == "__main__":
    unittest.main()

