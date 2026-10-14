import os
import sys
import unittest
import tempfile
import json
import sqlite3
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import ConfigManager, StorageError
from db_tracker import DownloadTrackerDB
from instance_lock import ProcessLock
from telegram_engine import (
    sanitize_filename,
    sanitize_media_filename,
    TelegramDownloaderEngine,
    CancelledByUser
)
from telethon.errors import FloodWaitError

class TestTelegramDownloaderSuite(unittest.TestCase):

    def test_config_active_channel_restoration(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg_file = Path(tmpdir) / "config.json"
            cfg_mgr = ConfigManager(str(cfg_file))
            cfg_mgr.save_config({
                "download_directory": "Z:\\TelegramData\\TelegramDownloads",
                "active_channels": [12345, 67890],
                "cutoff_date": "2026-05-01"
            })

            # Reload fresh instance
            reloaded = ConfigManager(str(cfg_file))
            self.assertEqual(reloaded.config.get("active_channels"), [12345, 67890])
            self.assertEqual(reloaded.config.get("cutoff_date"), "2026-05-01")
            self.assertEqual(reloaded.config.get("download_directory"), "Z:\\TelegramData\\TelegramDownloads")

    def test_db_init_and_crud(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = DownloadTrackerDB(str(db_path))

            # Foreign-key check: orphan insert raises IntegrityError
            with self.assertRaises(sqlite3.IntegrityError):
                db.record_start(channel_id=9999, message_id=1, file_name="orphan.pdf", file_size=100, relative_path="orphan.pdf")

            # Create channel and insert
            db.update_channel(channel_id=100, name="Channel 100", last_scanned_id=50)
            ch = db.get_channel(100)
            self.assertEqual(ch["name"], "Channel 100")
            self.assertEqual(ch["last_scanned_id"], 50)

            # Start and Complete download
            db.record_start(100, 1, "doc.pdf", 2048, "Channel 100/doc.pdf")
            self.assertFalse(db.is_message_downloaded(100, 1))

            db.record_completed(100, 1, "doc.pdf", 2048, "Channel 100/doc.pdf")
            self.assertTrue(db.is_message_downloaded(100, 1))

            stats = db.get_stats()
            self.assertEqual(stats["total_records"], 1)
            self.assertEqual(stats["completed"], 1)

    def test_failed_message_retry_queue(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = DownloadTrackerDB(str(db_path))
            db.update_channel(channel_id=100, name="Channel 100")

            db.record_start(100, 10, "fail.pdf", 1024, "Channel 100/fail.pdf")
            db.record_failed(100, 10, "Simulated network timeout")

            failed = db.get_failed_messages(channel_id=100, max_retries=5)
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["message_id"], 10)
            self.assertEqual(failed[0]["retry_count"], 1)

            # Fail up to max_retries limit
            for _ in range(4):
                db.record_failed(100, 10, "Repeated failure")

            failed_after = db.get_failed_messages(channel_id=100, max_retries=5)
            self.assertEqual(len(failed_after), 0)

    def test_filename_sanitization_and_disambiguation(self):
        dirty = "../../Invalid: Channel*Name? <Cool> | "
        clean = sanitize_filename(dirty)
        self.assertEqual(clean, "Invalid_ Channel_Name_ _Cool")
        self.assertNotIn("..", clean)
        self.assertNotIn(":", clean)
        self.assertNotIn("*", clean)

        dirty_file = 'doc/with:special?.pdf'
        clean_file = sanitize_media_filename(dirty_file)
        self.assertEqual(clean_file, "doc_with_special_.pdf")

        # Disambiguation
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            folder = Path(tmpdir)
            cfg = ConfigManager()
            db = DownloadTrackerDB(str(folder / "test.db"))
            engine = TelegramDownloaderEngine(cfg, db)

            p1 = engine.resolve_target_path(folder, 1, "video.mp4")
            self.assertEqual(p1.name, "video.mp4")
            p1.write_text("dummy")

            p2 = engine.resolve_target_path(folder, 2, "video.mp4")
            self.assertEqual(p2.name, "video [2].mp4")

    def test_flood_wait_error_handling(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg = ConfigManager()
            cfg.config["download_directory"] = str(Path(tmpdir))
            db = DownloadTrackerDB(str(Path(tmpdir) / "test.db"))
            db.update_channel(1, "Test Channel")
            logs = []
            engine = TelegramDownloaderEngine(cfg, db, event_callback=lambda evt, d: logs.append((evt, d)))

            mock_client = MagicMock()
            mock_client.download_media = AsyncMock(side_effect=FloodWaitError(request=None, capture=0))
            engine.client = mock_client

            msg = MagicMock()
            msg.id = 1
            msg.date = None

            async def run_dl():
                with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                    with self.assertRaises(FloodWaitError):
                        await engine.download_single_message(
                            msg, 1, Path(tmpdir), "test.pdf", 100
                        )
                    mock_sleep.assert_called_once_with(0)

            asyncio.run(run_dl())
            self.assertTrue(any("FloodWait" in str(l) for l in logs))

    def test_legacy_history_migration(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            legacy_file = Path(tmpdir) / "download_history.json"
            legacy_data = {
                "groups": {
                    "501": {"name": "History Channel 1", "last_msg_id": 120},
                    "502": {"name": "History Channel 2", "last_msg_id": 85}
                }
            }
            with open(legacy_file, "w", encoding="utf-8") as f:
                json.dump(legacy_data, f)

            db = DownloadTrackerDB(str(Path(tmpdir) / "test.db"))
            migrated = db.migrate_legacy_history_if_needed(str(legacy_file))
            self.assertEqual(migrated, 2)

            ch1 = db.get_channel(501)
            self.assertEqual(ch1["name"], "History Channel 1")
            self.assertEqual(ch1["last_scanned_id"], 120)

            # Idempotent
            self.assertEqual(db.migrate_legacy_history_if_needed(str(legacy_file)), 0)

    def test_part_download_interruption(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            folder = Path(tmpdir)
            cfg = ConfigManager()
            cfg.config["download_directory"] = str(folder)
            db = DownloadTrackerDB(str(folder / "test.db"))
            db.update_channel(1, "Chan")
            engine = TelegramDownloaderEngine(cfg, db)

            part_file = folder / "interrupted.pdf.part"
            part_file.write_text("corrupted partial data")

            # In the engine, if a stale .part exists before download, it is removed
            msg = MagicMock()
            msg.id = 77
            msg.date = None

            mock_client = MagicMock()
            async def mock_dl(m, file, progress_callback=None):
                file.write_text("fresh complete data")
            mock_client.download_media = AsyncMock(side_effect=mock_dl)
            engine.client = mock_client

            asyncio.run(engine.download_single_message(msg, 1, folder, "interrupted.pdf", 19))
            final_file = folder / "interrupted.pdf"
            self.assertTrue(final_file.exists())
            self.assertFalse(part_file.exists())
            self.assertEqual(final_file.read_text(), "fresh complete data")

    def test_process_lock_mutual_exclusion(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            lock_path = Path(tmpdir) / "app_data" / "downloader.lock"
            status_path = Path(tmpdir) / "app_data" / "worker_status.json"

            with patch("instance_lock.LOCK_FILE", lock_path), \
                 patch("instance_lock.STATUS_FILE", status_path):

                lock1 = ProcessLock(role="worker")
                self.assertTrue(lock1.acquire_session_lock())

                lock2 = ProcessLock(role="gui")
                self.assertFalse(lock2.acquire_session_lock())

                lock1.release_lock()
                self.assertTrue(lock2.acquire_session_lock())
                lock2.release_lock()

    def test_storage_validation_and_no_c_fallback(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            cfg_file = Path(tmpdir) / "config.json"
            cfg = ConfigManager(str(cfg_file))

            # 1. Valid local temp dir
            valid, msg, metrics = cfg.validate_storage(tmpdir)
            self.assertTrue(valid)
            self.assertIn("free_gb", metrics)

            # 2. Invalid non-existent path
            invalid_path = "X:\\NonExistentDrive\\Telegram"
            valid, msg, _ = cfg.validate_storage(invalid_path)
            self.assertFalse(valid)

            # 3. No fallback to C: when invalid
            cfg.config["download_directory"] = invalid_path
            with self.assertRaises(StorageError):
                cfg.check_capacity_for_file(1024)

if __name__ == "__main__":
    unittest.main()
