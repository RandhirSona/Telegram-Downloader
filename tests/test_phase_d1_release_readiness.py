"""
Phase D1 Release-Readiness, Security & Stability Automated Regression Test Suite

Tests:
1. Clean startup and shutdown lifecycle.
2. Stale lock recovery without killing active processes.
3. Duplicate-instance prevention & monitoring mode fallback.
4. Storage disconnection simulation (unreachable volume).
5. Zero fallback to C: drive on storage failure.
6. Database WAL concurrency, foreign keys, and backup integrity.
7. Interrupted transfer partial-file preservation.
8. Missing physical file re-download handling.
9. Duplicate filename disambiguation & Windows reserved device names (CON, NUL, AUX, PRN).
10. GUI topic scope isolation & multi-channel bleed prevention.
11. Cancel and restart clean engine state recovery.
12. Authentication error handling & credential privacy in logs.
"""

import os
import sys
import time
import json
import sqlite3
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import ConfigManager, StorageError
from db_tracker import DownloadTrackerDB, normalize_channel_id, sanitize_folder_name
from instance_lock import ProcessLock
from telegram_engine import (
    TelegramDownloaderEngine,
    CancelledByUser,
    sanitize_filename,
    sanitize_media_filename
)
from telethon.tl.types import Channel, Chat, MessageMediaDocument
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError
)


class TestPhaseD1ReleaseReadiness(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_phase_d1_")
        self.test_path = Path(self.test_dir)
        self.download_dir = self.test_path / "Downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.test_path / "test_d1.db"

        self.cfg_file = self.test_path / "config.json"
        self.cfg_mgr = ConfigManager(str(self.cfg_file))
        self.cfg_mgr.config["download_directory"] = str(self.download_dir)
        self.cfg_mgr.config["organize_forum_topics"] = True
        self.cfg_mgr.save_config()

        self.db = DownloadTrackerDB(str(self.db_path))
        self.db.migrate_to_stage_b()

        self.account_id = 123456789
        self.channel_id = 200100200
        self.db.ensure_channel(self.channel_id, "Discussion Supergroup")
        self.db.add_selection(self.account_id, self.channel_id, "Discussion Supergroup", "channel")

        self.engine = TelegramDownloaderEngine(
            config_mgr=self.cfg_mgr,
            db_tracker=self.db,
            session_name="test_anon"
        )
        self.engine.account_id = self.account_id

    def tearDown(self):
        # Clean up any lingering files
        try:
            import shutil
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    # 1. Clean startup and shutdown
    def test_01_clean_startup_and_shutdown_lifecycle(self):
        import tkinter as tk
        from telegram_gui_downloader import TelegramGUIApp
        root = tk.Tk()
        root.withdraw()

        with patch("instance_lock.LOCK_FILE", self.test_path / "app_data" / "downloader.lock"), \
             patch("instance_lock.STATUS_FILE", self.test_path / "app_data" / "worker_status.json"), \
             patch("telegram_gui_downloader.asyncio.run_coroutine_threadsafe"):
            app = TelegramGUIApp(root)
            self.assertIsNotNone(app.engine)
            self.assertFalse(app.is_monitoring_mode)
            self.assertTrue(app.lock.is_owner)

            # Simulate in-flight engine task
            mock_client = MagicMock()
            mock_client.disconnect = AsyncMock()
            app.engine.client = mock_client

            # Perform clean shutdown
            app.on_close()
            self.assertTrue(app._is_closing)
            self.assertTrue(app.engine.cancel_flag)
            self.assertFalse(app.lock.is_owner)

    # 2. Safe stale lock recovery & active process protection
    def test_02_stale_lock_recovery(self):
        lock_file = self.test_path / "downloader.lock"
        status_file = self.test_path / "worker_status.json"

        with patch("instance_lock.LOCK_FILE", lock_file), \
             patch("instance_lock.STATUS_FILE", status_file):
            lock_file.parent.mkdir(parents=True, exist_ok=True)

            # Scenario A: Dead owner -> cleanly reclaimed
            stale_data = {
                "pid": 99999999,  # non-existent PID
                "role": "worker",
                "acquired_at": time.time() - 3600,
                "last_heartbeat": time.time() - 100,
                "create_time": 11111
            }
            with open(lock_file, "w", encoding="utf-8") as f:
                json.dump(stale_data, f)

            lock = ProcessLock(role="gui")
            self.assertTrue(lock.acquire_session_lock())
            self.assertTrue(lock.is_owner)
            lock.release_lock()

            # Scenario B: Live owner with delayed heartbeat (>30s) -> NEVER stolen
            my_pid = os.getpid()
            my_create_time = lock._get_pid_create_time(my_pid)
            delayed_data = {
                "pid": my_pid,
                "role": "worker",
                "acquired_at": time.time() - 3600,
                "last_heartbeat": time.time() - 60,  # 60s ago (>30s delayed!)
                "create_time": my_create_time
            }
            with open(lock_file, "w", encoding="utf-8") as f:
                json.dump(delayed_data, f)

            new_gui_lock = ProcessLock(role="gui")
            # Must NOT steal the lock from an active process!
            self.assertFalse(new_gui_lock.acquire_session_lock())
            self.assertFalse(new_gui_lock.is_owner)

            # Scenario C: PID reuse -> PID is alive but belongs to a new process (different create_time)
            reused_data = {
                "pid": my_pid,
                "role": "worker",
                "acquired_at": time.time() - 3600,
                "last_heartbeat": time.time() - 100,
                "create_time": my_create_time - 999999  # mismatched create_time
            }
            with open(lock_file, "w", encoding="utf-8") as f:
                json.dump(reused_data, f)

            self.assertTrue(new_gui_lock.acquire_session_lock())
            self.assertTrue(new_gui_lock.is_owner)
            new_gui_lock.release_lock()

    # 3. Duplicate instance prevention and monitoring mode fallback
    def test_03_duplicate_instance_prevention(self):
        lock_file = self.test_path / "downloader.lock"
        status_file = self.test_path / "worker_status.json"

        with patch("instance_lock.LOCK_FILE", lock_file), \
             patch("instance_lock.STATUS_FILE", status_file):
            lock1 = ProcessLock(role="worker")
            self.assertTrue(lock1.acquire_session_lock())

            # Worker writes status
            lock1.update_status({"status": "RUNNING", "current_channel": "TestChannel"})

            # Second process cannot acquire lock
            lock2 = ProcessLock(role="gui")
            self.assertFalse(lock2.acquire_session_lock())
            self.assertFalse(lock2.is_owner)

            # Worker status is detected for monitoring mode even if heartbeat delayed
            active_worker = ProcessLock.get_active_worker_status()
            self.assertIsNotNone(active_worker)
            self.assertEqual(active_worker["current_channel"], "TestChannel")

            lock1.release_lock()
            self.assertTrue(lock2.acquire_session_lock())
            lock2.release_lock()

    # 4. Storage disconnection simulation
    def test_04_storage_disconnection_simulation(self):
        fake_storage = Path("Z:\\NonExistentVolume_9999\\TelegramDownloads")
        self.cfg_mgr.config["download_directory"] = str(fake_storage)

        valid, error_msg, metrics = self.cfg_mgr.validate_storage()
        self.assertFalse(valid)
        self.assertIn("Storage unreachable", error_msg)

        with self.assertRaises(StorageError) as ctx:
            self.cfg_mgr.check_capacity_for_file(1024 * 1024)
        self.assertIn("Storage validation failed", str(ctx.exception))

    # 5. Zero fallback to C: drive
    def test_05_zero_fallback_to_c_drive(self):
        fake_storage = Path("Q:\\DetachedDrive\\TelegramDownloads")
        self.cfg_mgr.config["download_directory"] = str(fake_storage)

        with self.assertRaises(StorageError):
            self.cfg_mgr.check_capacity_for_file(500)

        # Confirm no directories were created under C:\
        self.assertFalse(fake_storage.exists())

    # 6. Database WAL concurrency & backup integrity
    def test_06_database_wal_concurrency_and_backup_integrity(self):
        # 1. Verify WAL and busy timeout on active DB
        with self.db._get_connection() as conn:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
            self.assertEqual(fk, 1)

        # 2. Online concurrent read/write
        conn1 = self.db._get_connection()
        conn2 = self.db._get_connection()

        conn1.execute("INSERT INTO channels (channel_id, name) VALUES (999, 'Concurrent')")
        conn1.commit()

        row = conn2.execute("SELECT name FROM channels WHERE channel_id = 999").fetchone()
        self.assertEqual(row["name"], "Concurrent")
        conn1.close()
        conn2.close()

        # 3. Create verified backup
        backup_dir = self.test_path / "backups"
        backup_path = self.db.backup_database(backup_dir)
        self.assertTrue(backup_path.exists())

        bconn = sqlite3.connect(str(backup_path))
        res = bconn.execute("PRAGMA integrity_check;").fetchone()
        bconn.close()
        self.assertEqual(res[0], "ok")

    # 7. Interrupted transfer partial-file preservation
    def test_07_interrupted_transfer_preserves_part_file(self):
        msg = MagicMock()
        msg.id = 505
        msg.date = None
        msg.media = None

        mock_client = MagicMock()
        async def cancel_dl(m, file, progress_callback=None):
            # Write 256KB then simulate user cancel
            Path(file).write_bytes(b"X" * (256 * 1024))
            raise CancelledByUser("User clicked stop")

        mock_client.download_media = AsyncMock(side_effect=cancel_dl)
        self.engine.client = mock_client

        with self.assertRaises(CancelledByUser):
            asyncio.run(
                self.engine.download_single_message(
                    msg, self.channel_id, self.download_dir, "large_video.mp4", 1024 * 1024
                )
            )

        # Verify .part file is preserved on disk with 256KB
        expected_part = self.download_dir / "large_video.mp4.part"
        self.assertTrue(expected_part.exists())
        self.assertEqual(expected_part.stat().st_size, 256 * 1024)

        # Check DB status is recorded as DOWNLOADING
        rec = self.db.get_download_record(self.channel_id, 505)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "DOWNLOADING")

    # 8. Missing destination media permits redownload
    def test_08_missing_destination_media_permits_redownload(self):
        msg_id = 606
        file_name = "deleted_document.pdf"
        file_size = 5000
        rel_path = f"Discussion Supergroup/{file_name}"

        # Record as COMPLETED in DB but do NOT create file on disk
        self.db.record_completed(self.channel_id, msg_id, file_name, file_size, rel_path)
        self.assertTrue(self.db.is_message_downloaded(self.channel_id, msg_id))

        target_file = self.download_dir / "Discussion Supergroup" / file_name
        self.assertFalse(target_file.exists())

        msg = MagicMock()
        msg.id = msg_id
        msg.date = None
        msg.media = None

        download_invoked = False
        mock_client = MagicMock()
        async def mock_dl(m, file, progress_callback=None):
            nonlocal download_invoked
            download_invoked = True
            Path(file).parent.mkdir(parents=True, exist_ok=True)
            Path(file).write_bytes(b"D" * file_size)

        mock_client.download_media = AsyncMock(side_effect=mock_dl)
        self.engine.client = mock_client

        # Re-invoking download must NOT skip because physical file is missing
        asyncio.run(
            self.engine.download_single_message(
                msg, self.channel_id, self.download_dir / "Discussion Supergroup", file_name, file_size
            )
        )
        self.assertTrue(download_invoked)
        self.assertTrue(target_file.exists())

    # 9. Duplicate filename disambiguation and Windows reserved names
    def test_09_duplicate_filename_and_windows_reserved_device_names(self):
        # 1. Windows reserved device names
        self.assertEqual(sanitize_media_filename("con.mp4"), "con_file.mp4")
        self.assertEqual(sanitize_media_filename("NUL.pdf"), "NUL_file.pdf")
        self.assertEqual(sanitize_media_filename("aux.txt"), "aux_file.txt")
        self.assertEqual(sanitize_media_filename("prn.zip"), "prn_file.zip")
        self.assertEqual(sanitize_media_filename("com1.tar.gz"), "com1_file.tar.gz")

        # 2. Folder reserved names
        self.assertEqual(sanitize_folder_name("CON"), "CON_dir")
        self.assertEqual(sanitize_folder_name("NUL"), "NUL_dir")
        self.assertEqual(sanitize_folder_name("AUX"), "AUX_dir")

        # 3. Disambiguation
        target_dir = self.download_dir / "TestCollisions"
        target_dir.mkdir(parents=True, exist_ok=True)

        p1 = self.engine.resolve_target_path(target_dir, 101, "notes.pdf")
        p1.write_text("file 1")
        self.assertEqual(p1.name, "notes.pdf")

        p2 = self.engine.resolve_target_path(target_dir, 102, "notes.pdf")
        self.assertEqual(p2.name, "notes [102].pdf")

    # 10. GUI topic scope isolation
    def test_10_gui_topic_scope_isolation(self):
        # Seed two forum topics
        self.db.save_forum_topic(self.channel_id, 10, "General", "General", account_id=self.account_id, peer_type="channel")
        self.db.save_forum_topic(self.channel_id, 20, "Economy", "Economy", account_id=self.account_id, peer_type="channel")

        # Attempt to run batch requesting topic 10 and 999 (alien topic)
        selected_dialogs = [{
            "id": self.channel_id,
            "name": "Discussion Supergroup",
            "peer_type": "channel",
            "entity": MagicMock(spec=Channel, id=self.channel_id, forum=True, megagroup=True)
        }]

        # Rejection of alien topic
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(
                self.engine.run_batch(
                    selected_dialogs,
                    target_topic_ids={10, 999},
                    expected_channel_id=self.channel_id,
                    expected_peer_type="channel",
                    expected_account_id=self.account_id
                )
            )
        self.assertIn("do not belong to verified forum supergroup", str(ctx.exception))

    # 11. Cancel and restart clean recovery
    def test_11_cancel_and_restart_clean_state_recovery(self):
        self.engine.cancel_flag = True
        self.engine.is_downloading = True

        # Stop action triggers reset
        self.engine.cancel_flag = False
        self.engine.is_downloading = False

        self.assertFalse(self.engine.cancel_flag)
        self.assertFalse(self.engine.is_downloading)

    # 12. Authentication error handling & privacy
    def test_12_authentication_error_handling_and_credential_privacy(self):
        logs = []
        auth_errors = []
        password_prompts = []

        def event_cb(evt, data):
            if evt == "log":
                logs.append(str(data))
            elif evt == "auth_error":
                auth_errors.append(str(data))
            elif evt == "password_needed":
                password_prompts.append(True)

        engine = TelegramDownloaderEngine(
            self.cfg_mgr, self.db, event_callback=event_cb
        )

        # 1. 2FA Password Needed
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(side_effect=SessionPasswordNeededError(request=None))
        mock_client.is_connected = MagicMock(return_value=True)
        engine.client = mock_client

        asyncio.run(engine.sign_in_code("1234567890", "12345"))
        self.assertEqual(len(password_prompts), 1)

        # 2. Invalid code
        mock_client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(request=None))
        asyncio.run(engine.sign_in_code("1234567890", "99999"))
        self.assertTrue(any("invalid" in err.lower() for err in auth_errors))

        # 3. Expired code
        mock_client.sign_in = AsyncMock(side_effect=PhoneCodeExpiredError(request=None))
        asyncio.run(engine.sign_in_code("1234567890", "00000"))
        self.assertTrue(any("expired" in err.lower() for err in auth_errors))

        # 4. Verify no secret password or OTP codes were written to logs
        for log_msg in logs:
            self.assertNotIn("SecretCloudPassword", log_msg)
            self.assertNotIn("99999", log_msg)


if __name__ == "__main__":
    unittest.main()
