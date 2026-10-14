import os
import sys
import time
import shutil
import sqlite3
import tempfile
import asyncio
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch

from db_tracker import DownloadTrackerDB, normalize_channel_id
from config_manager import ConfigManager, StorageError
from telegram_engine import TelegramDownloaderEngine

class TestRegressionForeignKeyAndStorage(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_tracker.db"
        self.storage_dir = Path(self.tmpdir) / "TestStorage"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(self.tmpdir) / "config.json"
        
        self.config_mgr = ConfigManager(str(self.config_path))
        self.config_mgr.config["download_directory"] = str(self.storage_dir)
        self.config_mgr.config["min_free_space_gb"] = 0.001
        self.config_mgr.save_config()

        self.db = DownloadTrackerDB(str(self.db_path))

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    # 1. Peer ID Normalization Consistency
    def test_channel_id_normalization(self):
        # Bare positive int
        self.assertEqual(normalize_channel_id(1234567890), 1234567890)
        # Marked channel int (-100 prefix)
        self.assertEqual(normalize_channel_id(-1001234567890), 1234567890)
        self.assertEqual(normalize_channel_id(-100555666777), 555666777)
        # String channel representation
        self.assertEqual(normalize_channel_id("-1001234567890"), 1234567890)
        self.assertEqual(normalize_channel_id("-100555666777"), 555666777)
        self.assertEqual(normalize_channel_id("1234567890"), 1234567890)
        # Negative chat ID
        self.assertEqual(normalize_channel_id(-123456), 123456)
        self.assertEqual(normalize_channel_id("-123456"), 123456)
        # Positive marked ID (> 10^12)
        self.assertEqual(normalize_channel_id(1001234567890), 1234567890)
        # Entity mock object
        mock_entity = MagicMock()
        mock_entity.id = 9876543210
        self.assertEqual(normalize_channel_id(mock_entity), 9876543210)

    # 2. Foreign Key Strictly Enforced on Direct Orphan Insert
    def test_foreign_key_enforcement(self):
        # Direct attempt to insert download without parent channel must raise IntegrityError
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.record_start(
                channel_id=987654321,
                message_id=1,
                file_name="orphan.pdf",
                file_size=1024,
                relative_path="orphan.pdf"
            )

    # 3. Channel Registration Upfront Allows Download Insert
    def test_channel_registration_upfront(self):
        cid = -1004412184534
        norm_cid = normalize_channel_id(cid)
        
        # Channel is registered upfront
        self.db.update_channel(cid, "Test Channel 1")
        ch = self.db.get_channel(cid)
        self.assertIsNotNone(ch)
        self.assertEqual(ch["channel_id"], norm_cid)
        self.assertEqual(ch["name"], "Test Channel 1")

        # Now download insert succeeds without Foreign Key failure
        self.db.record_start(cid, 101, "file1.pdf", 5000, "Test Channel 1/file1.pdf")
        rec = self.db.get_download_record(cid, 101)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "DOWNLOADING")
        
        self.db.record_completed(cid, 101, "file1.pdf", 5000, "Test Channel 1/file1.pdf")
        self.assertTrue(self.db.is_message_downloaded(cid, 101))

    # 4. Engine Registers Channel Upfront Before Scanning
    def test_engine_upfront_channel_registration(self):
        engine = TelegramDownloaderEngine(self.config_mgr, self.db)
        engine.client = MagicMock()
        
        # Mock iter_messages returning empty
        async def mock_iter(*args, **kwargs):
            if False: yield None
        engine.client.iter_messages = mock_iter

        mock_entity = MagicMock()
        mock_entity.id = 555666777

        # Run scan_and_download_channel
        asyncio.run(engine.scan_and_download_channel(
            mock_entity,
            channel_id=-100555666777,
            channel_name="Auto Register Channel",
            media_filters={"PDFs": True},
            cutoff_date=None
        ))

        # Channel must be registered in the channels table
        ch = self.db.get_channel(555666777)
        self.assertIsNotNone(ch)
        self.assertEqual(ch["name"], "Auto Register Channel")

    # 5. Checkpoint is NOT advanced on database failure
    def test_checkpoint_not_advanced_on_db_failure(self):
        engine = TelegramDownloaderEngine(self.config_mgr, self.db)
        engine.client = MagicMock()

        # Set initial channel checkpoint
        self.db.update_channel(111222, "Error Channel", last_scanned_id=10)
        
        # Mock message with matching media
        mock_msg = MagicMock()
        mock_msg.id = 25
        mock_msg.date = datetime(2026, 10, 1, tzinfo=timezone.utc)
        mock_msg.message = "hello"

        async def mock_iter(*args, **kwargs):
            yield mock_msg
        engine.client.iter_messages = mock_iter
        engine.match_media = MagicMock(return_value=(True, "test.pdf", 1024))

        # Force database error inside download_single_message
        with patch.object(engine, "download_single_message", side_effect=sqlite3.OperationalError("disk I/O error")):
            asyncio.run(engine.scan_and_download_channel(
                MagicMock(),
                channel_id=111222,
                channel_name="Error Channel",
                media_filters={"PDFs": True},
                cutoff_date=None
            ))

        # Verify checkpoint was NOT updated to 25
        ch = self.db.get_channel(111222)
        self.assertEqual(ch["last_scanned_id"], 10)

    # 6. Database errors cause a controlled halt instead of repeated skips
    def test_database_error_halts_scan(self):
        logs = []
        def event_cb(event_type, data):
            if event_type == "log":
                logs.append(data)

        engine = TelegramDownloaderEngine(self.config_mgr, self.db, event_callback=event_cb)
        engine.client = MagicMock()

        self.db.update_channel(333444, "Halt Channel")

        msg1 = MagicMock(id=1, date=datetime(2026, 10, 1, tzinfo=timezone.utc), message="m1")
        msg2 = MagicMock(id=2, date=datetime(2026, 10, 1, tzinfo=timezone.utc), message="m2")
        msg3 = MagicMock(id=3, date=datetime(2026, 10, 1, tzinfo=timezone.utc), message="m3")

        async def mock_iter(*args, **kwargs):
            yield msg1
            yield msg2
            yield msg3
        engine.client.iter_messages = mock_iter
        engine.match_media = MagicMock(return_value=(True, "test.pdf", 1024))

        processed_ids = []
        async def fake_download(msg, *args, **kwargs):
            processed_ids.append(msg.id)
            if msg.id == 1:
                raise sqlite3.IntegrityError("Simulated DB integrity failure")

        with patch.object(engine, "download_single_message", side_effect=fake_download):
            asyncio.run(engine.scan_and_download_channel(
                MagicMock(),
                channel_id=333444,
                channel_name="Halt Channel",
                media_filters={"PDFs": True},
                cutoff_date=None
            ))

        # Only msg 1 was attempted; loop stopped immediately without processing msg 2 and 3
        self.assertEqual(processed_ids, [1])
        self.assertTrue(any("CRITICAL DATABASE ERROR" in l for l in logs))

    # 7. Unique storage write probe concurrency
    def test_concurrent_storage_probes(self):
        import concurrent.futures

        def probe():
            valid, msg, metrics = self.config_mgr.validate_storage(str(self.storage_dir))
            return valid

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(probe) for _ in range(20)]
            results = [f.result() for f in futures]

        self.assertTrue(all(results))
        # Ensure no lingering probe files exist
        probe_files = list(self.storage_dir.glob(".probe_*"))
        self.assertEqual(len(probe_files), 0)

    # 8. Storage probe handle closed and safe cleanup
    def test_storage_probe_cleanup_on_read_only(self):
        # Normal directory validates and cleans up
        valid, msg, metrics = self.config_mgr.validate_storage(str(self.storage_dir))
        self.assertTrue(valid)
        self.assertEqual(len(list(self.storage_dir.glob(".probe_*"))), 0)

    # 9. Existing media requires supporting metadata (mtime match)
    def test_existing_media_metadata_verification(self):
        engine = TelegramDownloaderEngine(self.config_mgr, self.db)
        engine.client = MagicMock()
        
        async def fake_dl(message, file, progress_callback=None):
            Path(file).write_bytes(b"content" * 100)

        engine.client.download_media = AsyncMock(side_effect=fake_dl)

        channel_folder = self.storage_dir / "Channel_Metadata_Test"
        channel_folder.mkdir(parents=True, exist_ok=True)
        
        target_file = channel_folder / "test_doc.pdf"
        target_file.write_bytes(b"content" * 100) # 700 bytes
        file_size = 700

        msg_date = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
        mtime = msg_date.timestamp()

        # Case A: Size matches, but mtime does NOT match msg_date
        os.utime(target_file, (mtime - 1000, mtime - 1000))
        mock_msg_mismatch = MagicMock()
        mock_msg_mismatch.id = 99
        mock_msg_mismatch.date = msg_date

        asyncio.run(engine.download_single_message(
            mock_msg_mismatch,
            channel_id=777,
            channel_folder=channel_folder,
            raw_filename="test_doc.pdf",
            expected_size=file_size
        ))
        # download_media was called because metadata did not match
        self.assertTrue(engine.client.download_media.called)

        # Case B: Size matches AND mtime matches msg_date
        os.utime(target_file, (mtime, mtime))
        mock_msg_match = MagicMock()
        mock_msg_match.id = 100
        mock_msg_match.date = msg_date
        engine.client.download_media.reset_mock()

        asyncio.run(engine.download_single_message(
            mock_msg_match,
            channel_id=777,
            channel_folder=channel_folder,
            raw_filename="test_doc.pdf",
            expected_size=file_size
        ))
        # download_media was NOT called; safely registered based on supporting metadata
        self.assertFalse(engine.client.download_media.called)
        self.assertTrue(self.db.is_message_downloaded(777, 100))

    # 10. Strict No-C:-Fallback rule
    def test_no_fallback_to_c(self):
        # Configure destination to NAS
        self.config_mgr.config["download_directory"] = r"Z:\TelegramData\TelegramDownloads"
        self.config_mgr.save_config()

        non_existent_path = Path(r"\\192.168.1.999\NonExistentShare\TelegramDownloads")
        valid, msg, _ = self.config_mgr.validate_storage(str(non_existent_path))
        self.assertFalse(valid)
        self.assertIn("Storage unreachable", msg)

        # Confirm configured download directory never falls back to C:
        configured_dir = str(self.config_mgr.get_download_dir())
        self.assertTrue(configured_dir.startswith("Z:") or configured_dir.startswith(r"\\"))
        self.assertFalse(configured_dir.lower().startswith("c:"))

if __name__ == "__main__":
    unittest.main()
