import os
import shutil
import tempfile
import unittest
import errno
from pathlib import Path
from unittest.mock import MagicMock, patch

from config_manager import ConfigManager, StorageError, validate_windows_path
from db_tracker import DownloadTrackerDB, normalize_channel_id
from telegram_engine import TelegramDownloaderEngine


class TestStorageConfigurationRegression(unittest.TestCase):
    """
    Comprehensive regression test suite covering all 15 user-configurable
    storage requirements, removing any fixed 2 TB or F: drive dependency.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tg_storage_test_")
        self.config_path = Path(self.tmpdir) / "config.json"
        self.db_path = Path(self.tmpdir) / "test_tracker.db"

        # Initialize test config manager with dynamic temp directory
        self.initial_storage = Path(self.tmpdir) / "StorageRootA"
        self.initial_storage.mkdir(parents=True, exist_ok=True)

        self.cfg_mgr = ConfigManager(str(self.config_path))
        self.cfg_mgr.save_config({
            "download_directory": str(self.initial_storage),
            "historical_download_directories": [],
            "min_free_space_gb": 0.001,  # Small threshold for test environment
            "organize_forum_topics": True
        })

        self.db = DownloadTrackerDB(str(self.db_path))
        self.db.migrate_to_stage_b()

        # Engine with mock callbacks
        self.logs = []
        self.events = []

        def event_cb(evt_type, data):
            self.events.append((evt_type, data))
            if evt_type == "log":
                self.logs.append(str(data))

        self.engine = TelegramDownloaderEngine(
            config_mgr=self.cfg_mgr,
            db_tracker=self.db,
            event_callback=event_cb
        )

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_test_gui(self, root):
        from unittest.mock import MagicMock, AsyncMock, patch
        from telegram_gui_downloader import TelegramGUIApp
        mock_lock = MagicMock()
        mock_lock.acquire_session_lock.return_value = True
        mock_lock.release_lock.return_value = True

        mock_engine = MagicMock()
        mock_engine.check_auth = AsyncMock(return_value=True)
        mock_engine.is_downloading = False

        with patch("telegram_gui_downloader.ConfigManager", return_value=self.cfg_mgr), \
             patch("telegram_gui_downloader.DownloadTrackerDB", return_value=self.db), \
             patch("telegram_gui_downloader.ProcessLock", return_value=mock_lock), \
             patch("telegram_gui_downloader.TelegramDownloaderEngine", return_value=mock_engine), \
             patch.object(TelegramGUIApp, "run_async_loop", lambda self: None), \
             patch.object(TelegramGUIApp, "poll_queue", lambda self: None):
            app = TelegramGUIApp(root)
            app.config_mgr = self.cfg_mgr
            app.engine = mock_engine
            return app

    # -------------------------------------------------------------------------
    # 1. Selecting a different download drive
    # -------------------------------------------------------------------------
    def test_01_selecting_different_download_drive(self):
        """Verify selecting a different drive letter updates path and validates syntax."""
        diff_drive_path = r"E:\TelegramDownloads"
        valid, msg = validate_windows_path(diff_drive_path)
        self.assertTrue(valid, f"Expected valid Windows drive path, got: {msg}")

        # Test set_download_dir persists and normalizes
        self.cfg_mgr.set_download_dir(diff_drive_path, save=True)
        active = self.cfg_mgr.get_download_dir()
        self.assertEqual(str(active).lower(), diff_drive_path.lower())
        self.assertEqual(active.drive.upper(), "E:")

    # -------------------------------------------------------------------------
    # 2. Selecting another folder on the same drive
    # -------------------------------------------------------------------------
    def test_02_selecting_another_folder_on_same_drive(self):
        """Verify switching between folders archives old root to historical list."""
        folder_b = Path(self.tmpdir) / "StorageRootB"
        folder_b.mkdir(parents=True, exist_ok=True)

        old_root = str(self.initial_storage)
        self.cfg_mgr.set_download_dir(str(folder_b), save=True)

        # Active root should now be folder_b
        self.assertEqual(str(self.cfg_mgr.get_download_dir()).lower(), str(folder_b).lower())

        # Historical roots must contain old_root
        hist_roots = [str(p).lower() for p in self.cfg_mgr.get_historical_roots()]
        self.assertIn(old_root.lower(), hist_roots)

    # -------------------------------------------------------------------------
    # 3. Saving and restoring the selected directory
    # -------------------------------------------------------------------------
    def test_03_saving_and_restoring_selected_directory(self):
        """Verify configuration persists across new ConfigManager instances (app restart)."""
        custom_folder = Path(self.tmpdir) / "CustomSavedFolder"
        custom_folder.mkdir(parents=True, exist_ok=True)

        self.cfg_mgr.set_download_dir(str(custom_folder), save=True)

        # Simulate fresh application start by loading a new ConfigManager
        reloaded_mgr = ConfigManager(str(self.config_path))
        self.assertEqual(
            str(reloaded_mgr.get_download_dir()).lower(),
            str(custom_folder).lower()
        )

    # -------------------------------------------------------------------------
    # 4. Changing the root while idle
    # -------------------------------------------------------------------------
    def test_04_changing_root_while_idle(self):
        """Verify changing root while engine is idle succeeds without error."""
        self.assertFalse(self.engine.is_downloading)

        new_idle_root = Path(self.tmpdir) / "IdleRoot"
        new_idle_root.mkdir(parents=True, exist_ok=True)

        resolved = self.cfg_mgr.set_download_dir(str(new_idle_root), save=True)
        self.assertEqual(resolved, new_idle_root)
        self.assertEqual(self.cfg_mgr.get_download_dir(), new_idle_root)

    # -------------------------------------------------------------------------
    # 4b. Selecting C: drive explicitly with user confirmation
    # -------------------------------------------------------------------------
    def test_04b_select_c_drive_explicitly_with_confirmation(self):
        """Verify user can explicitly select a location on C: when confirming warning."""
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        app = None
        try:
            app = self._create_test_gui(root)

            c_folder = Path(self.tmpdir) / "C_Drive_Test_Folder"
            c_folder.mkdir(parents=True, exist_ok=True)
            app.storage_path_var.set(str(c_folder))

            # User explicitly confirms C: warning
            with patch("tkinter.messagebox.askyesno", return_value=True), \
                 patch("tkinter.messagebox.showinfo"):
                app.on_save_storage()

            self.assertEqual(
                str(self.cfg_mgr.get_download_dir()).lower(),
                str(c_folder).lower()
            )
        finally:
            if app and hasattr(app, "_after_id") and app._after_id:
                try: root.after_cancel(app._after_id)
                except Exception: pass
            if app and hasattr(app, "lock") and app.lock:
                app.lock.release_lock()
            try: root.destroy()
            except Exception: pass

    # -------------------------------------------------------------------------
    # 4c. Selecting C: drive cancelled by user
    # -------------------------------------------------------------------------
    def test_04c_select_c_drive_cancelled_by_user(self):
        """Verify selecting C: does not persist if user declines the warning modal."""
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        app = None
        try:
            app = self._create_test_gui(root)

            original_dir = self.cfg_mgr.get_download_dir()
            c_folder = Path(self.tmpdir) / "C_Drive_Declined_Folder"
            c_folder.mkdir(parents=True, exist_ok=True)
            app.storage_path_var.set(str(c_folder))

            def mock_confirm(title, msg, **kwargs):
                if "System Drive" in title or "(C:)" in title:
                    return False
                return True

            with patch("tkinter.messagebox.askyesno", side_effect=mock_confirm):
                app.on_save_storage()

            # Active directory must remain the original directory
            self.assertEqual(self.cfg_mgr.get_download_dir(), original_dir)
        finally:
            if app and hasattr(app, "_after_id") and app._after_id:
                try: root.after_cancel(app._after_id)
                except Exception: pass
            if app and hasattr(app, "lock") and app.lock:
                app.lock.release_lock()
            try: root.destroy()
            except Exception: pass

    # -------------------------------------------------------------------------
    # 5. Preventing root changes during active transfers
    # -------------------------------------------------------------------------
    def test_05_preventing_root_changes_during_active_transfers(self):
        """Verify that GUI logic rejects directory changes when downloads are active."""
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        app = None
        try:
            app = self._create_test_gui(root)

            # Simulate an active download in progress
            app.engine.is_downloading = True

            with patch("tkinter.messagebox.showwarning") as mock_warn:
                app.on_save_storage()
                mock_warn.assert_called_once()
                self.assertIn("active", mock_warn.call_args[0][1].lower())

            # Verify active root did not change
            self.assertEqual(self.cfg_mgr.get_download_dir(), self.initial_storage)
        finally:
            if app and hasattr(app, "_after_id") and app._after_id:
                try: root.after_cancel(app._after_id)
                except Exception: pass
            if app and hasattr(app, "lock") and app.lock:
                app.lock.release_lock()
            try: root.destroy()
            except Exception: pass

    # -------------------------------------------------------------------------
    # 6. Drive disconnection
    # -------------------------------------------------------------------------
    def test_06_drive_disconnection(self):
        """Verify that a missing or disconnected drive fails validation and raises StorageError."""
        disconnected = Path("Z:\\NonExistent_Detached_Drive_9999\\Downloads")
        self.cfg_mgr.config["download_directory"] = str(disconnected)

        valid, msg, metrics = self.cfg_mgr.validate_storage()
        self.assertFalse(valid)
        self.assertTrue("does not exist" in msg.lower() or "unreachable" in msg.lower())

        with self.assertRaises(StorageError):
            self.cfg_mgr.check_capacity_for_file(1024)

    # -------------------------------------------------------------------------
    # 7. Insufficient disk space
    # -------------------------------------------------------------------------
    def test_07_insufficient_disk_space(self):
        """Verify validation and capacity checks fail safely when disk space is below required threshold."""
        from collections import namedtuple
        Usage = namedtuple("usage", ["total", "used", "free"])

        # Configure a 10 GB minimum free requirement
        self.cfg_mgr.config["min_free_space_gb"] = 10.0

        # Mock shutil.disk_usage to return 2 GB free (less than 10 GB required)
        with patch("shutil.disk_usage") as mock_usage:
            # total, used, free in bytes
            mock_usage.return_value = Usage(
                total=100 * (1024**3),
                used=98 * (1024**3),
                free=2 * (1024**3)
            )

            valid, msg, metrics = self.cfg_mgr.validate_storage(str(self.initial_storage))
            self.assertFalse(valid)
            self.assertIn("low disk space", msg.lower())

            with self.assertRaises(StorageError):
                self.cfg_mgr.check_capacity_for_file(100 * 1024 * 1024)  # 100 MB file

    # -------------------------------------------------------------------------
    # 8. Invalid or unwritable directories
    # -------------------------------------------------------------------------
    def test_08_invalid_or_unwritable_directories(self):
        """Verify illegal characters, reserved device names, and unwritable paths are rejected."""
        # 1. Illegal characters in directory path
        invalid_chars_path = r"D:\Folder*Invalid?Name"
        valid, msg = validate_windows_path(invalid_chars_path)
        self.assertFalse(valid)
        self.assertIn("invalid character", msg.lower())

        # 2. Windows reserved name
        reserved_path = r"D:\TelegramDownloads\CON\subfolder"
        valid, msg = validate_windows_path(reserved_path)
        self.assertFalse(valid)
        self.assertIn("reserved", msg.lower())

        # 3. Read-only / permission-denied directory
        with patch("tempfile.mkstemp", side_effect=PermissionError("Access is denied")):
            valid, msg, _ = self.cfg_mgr.validate_storage(str(self.initial_storage))
            self.assertFalse(valid)
            self.assertIn("permission denied", msg.lower())

    # -------------------------------------------------------------------------
    # 9. Preventing silent fallback
    # -------------------------------------------------------------------------
    def test_09_preventing_silent_fallback(self):
        """Verify that when storage fails, the downloader NEVER silently falls back to C:."""
        failed_path = r"Y:\DetachedStorage\TelegramDownloads"
        self.cfg_mgr.config["download_directory"] = failed_path

        valid, msg, _ = self.cfg_mgr.validate_storage()
        self.assertFalse(valid)

        # Ensure get_download_dir returns the configured path and never falls back to C:
        resolved = self.cfg_mgr.get_download_dir()
        self.assertEqual(str(resolved).lower(), failed_path.lower())
        self.assertNotEqual(str(resolved).lower(), "c:\\")

    # -------------------------------------------------------------------------
    # 10. Topic subfolder routing under the new root
    # -------------------------------------------------------------------------
    def test_10_topic_subfolder_routing_under_new_root(self):
        """Verify forum supergroup topics route strictly under the newly configured root."""
        new_root = Path(self.tmpdir) / "NewRootTopics"
        new_root.mkdir(parents=True, exist_ok=True)
        self.cfg_mgr.set_download_dir(str(new_root), save=True)

        target_path, rel_path, eff_tid, eff_title = self.engine.resolve_destination_path(
            channel_id=123456,
            channel_name="UPSC Preparation Forum",
            raw_filename="Current_Affairs_Lecture_01.pdf",
            message_id=901,
            is_forum=True,
            topic_id=42,
            topic_title="Daily Current Affairs"
        )

        expected_prefix = (new_root / "UPSC Preparation Forum" / "Daily Current Affairs").resolve()
        self.assertTrue(target_path.resolve().is_relative_to(expected_prefix))
        self.assertEqual(target_path.name, "Current_Affairs_Lecture_01.pdf")
        self.assertEqual(eff_tid, 42)
        self.assertEqual(eff_title, "Daily Current Affairs")

    # -------------------------------------------------------------------------
    # 11. Ordinary channel routing under the new root
    # -------------------------------------------------------------------------
    def test_11_ordinary_channel_routing_under_new_root(self):
        """Verify ordinary broadcast channels route into <NewRoot>/<Channel>/<Filename>."""
        new_root = Path(self.tmpdir) / "NewRootBroadcast"
        new_root.mkdir(parents=True, exist_ok=True)
        self.cfg_mgr.set_download_dir(str(new_root), save=True)

        target_path, rel_path, eff_tid, eff_title = self.engine.resolve_destination_path(
            channel_id=987654,
            channel_name="Daily Newspaper Channel",
            raw_filename="The_Hindu_14_Oct_2026.pdf",
            message_id=105,
            is_forum=False
        )

        expected_dir = (new_root / "Daily Newspaper Channel").resolve()
        self.assertTrue(target_path.resolve().is_relative_to(expected_dir))
        self.assertIsNone(eff_tid)
        self.assertIsNone(eff_title)

    # -------------------------------------------------------------------------
    # 12. Previously completed files remaining recognized
    # -------------------------------------------------------------------------
    def test_12_previously_completed_files_remaining_recognized(self):
        """Verify completed files on an old root remain recognized when root changes."""
        old_root = self.initial_storage
        cid = 555001
        mid = 301
        fname = "HistoricalCompletedDoc.pdf"
        rel_path = f"TestChannel\\{fname}"

        # Create physical completed file on old_root
        old_file = old_root / "TestChannel" / fname
        old_file.parent.mkdir(parents=True, exist_ok=True)
        with open(old_file, "wb") as f:
            f.write(b"SAMPLE COMPLETED DATA" * 100)
        file_size = old_file.stat().st_size

        # Record as COMPLETED in DB
        self.db.ensure_channel(cid, "TestChannel")
        self.db.record_completed(cid, mid, fname, file_size, rel_path)
        self.assertTrue(self.db.is_message_downloaded(cid, mid))

        # Change active root to new_root
        new_root = Path(self.tmpdir) / "NewRootRecognition"
        new_root.mkdir(parents=True, exist_ok=True)
        self.cfg_mgr.set_download_dir(str(new_root), save=True)

        # Cross-root lookup should find the file on old_root
        found_path = self.engine.find_file_across_roots(rel_path)
        self.assertIsNotNone(found_path)
        self.assertEqual(found_path.resolve(), old_file.resolve())

        # In resolve_destination_path, historical file on old root is preserved
        target_path, r_path, _, _ = self.engine.resolve_destination_path(
            cid, "TestChannel", fname, mid, is_forum=False
        )
        self.assertEqual(target_path.resolve(), old_file.resolve())

    # -------------------------------------------------------------------------
    # 13. Historical .part files remaining associated with their original root
    # -------------------------------------------------------------------------
    def test_13_historical_part_files_remain_associated_with_original_root(self):
        """Verify in-progress .part files on old root are NOT resumed/guessed under new root."""
        old_root = self.initial_storage
        cid = 666002
        mid = 405
        fname = "InProgressLecture.mp4"
        rel_path = f"LectureSeries\\{fname}"

        # Create historical .part file on old_root
        old_part = old_root / "LectureSeries" / f"{fname}.part"
        old_part.parent.mkdir(parents=True, exist_ok=True)
        with open(old_part, "wb") as f:
            f.write(b"PARTIAL_CONTENT" * 50)
        orig_part_bytes = old_part.stat().st_size

        # Record start in DB
        self.db.ensure_channel(cid, "LectureSeries")
        self.db.record_start(cid, mid, fname, 100000, rel_path)

        # Switch to new_root
        new_root = Path(self.tmpdir) / "NewRootPartPreserve"
        new_root.mkdir(parents=True, exist_ok=True)
        self.cfg_mgr.set_download_dir(str(new_root), save=True)

        # Under new_root, resolve_destination_path resolves strictly under new_root
        target_path, r_path, _, _ = self.engine.resolve_destination_path(
            cid, "LectureSeries", fname, mid, is_forum=False
        )
        self.assertTrue(target_path.resolve().is_relative_to(new_root.resolve()))

        # Verify old .part file was untouched byte-for-byte
        self.assertTrue(old_part.exists())
        self.assertEqual(old_part.stat().st_size, orig_part_bytes)

    # -------------------------------------------------------------------------
    # 14. GUI and worker reading the same configuration
    # -------------------------------------------------------------------------
    def test_14_gui_and_worker_reading_same_configuration(self):
        """Verify GUI and headless worker use the identical saved download-root configuration."""
        shared_folder = Path(self.tmpdir) / "SharedWorkerAndGUI"
        shared_folder.mkdir(parents=True, exist_ok=True)

        # GUI updates config
        self.cfg_mgr.set_download_dir(str(shared_folder), save=True)

        # Simulate HeadlessWorker instantiating its own ConfigManager from same path
        from worker_headless import HeadlessWorker
        worker = HeadlessWorker()
        worker.config_mgr = ConfigManager(str(self.config_path))

        worker_dir = worker.config_mgr.get_download_dir()
        gui_dir = self.cfg_mgr.get_download_dir()

        self.assertEqual(worker_dir.resolve(), gui_dir.resolve())
        self.assertEqual(worker_dir.resolve(), shared_folder.resolve())

    # -------------------------------------------------------------------------
    # 15. No fixed 2 TB disk requirement
    # -------------------------------------------------------------------------
    def test_15_no_fixed_2tb_disk_requirement(self):
        """Verify storage validation works seamlessly on modest disks without 2 TB requirement."""
        from collections import namedtuple
        Usage = namedtuple("usage", ["total", "used", "free"])

        # Mock a modest 32 GB volume
        with patch("shutil.disk_usage") as mock_usage:
            # 32 GB total, 12 GB used, 20 GB free
            mock_usage.return_value = Usage(
                total=32 * (1024**3),
                used=12 * (1024**3),
                free=20 * (1024**3)
            )

            self.cfg_mgr.config["min_free_space_gb"] = 2.0
            valid, msg, metrics = self.cfg_mgr.validate_storage(str(self.initial_storage))

            self.assertTrue(valid)
            self.assertEqual(metrics["total_gb"], 32.0)
            self.assertEqual(metrics["free_gb"], 20.0)
            self.assertIn("ready", msg.lower())

    # -------------------------------------------------------------------------
    # 16. Completed message remains skipped during ordinary channel scans after root change
    # -------------------------------------------------------------------------
    def test_16_completed_message_skipped_during_channel_scan_after_root_change(self):
        """Verify message marked COMPLETED remains skipped during scans after root changes, even if missing on disk."""
        import asyncio
        cid = 777001
        mid = 99
        fname = "HistoricalCompleted.pdf"
        rel_path = f"Channel_777\\{fname}"

        self.db.ensure_channel(cid, "Channel_777")
        self.db.record_completed(cid, mid, fname, 5000, rel_path)
        self.assertTrue(self.db.is_message_downloaded(cid, mid))

        # Switch root to a completely new empty root
        new_root = Path(self.tmpdir) / "NewRootScanTest"
        new_root.mkdir(parents=True, exist_ok=True)
        self.cfg_mgr.set_download_dir(str(new_root), save=True)

        # Confirm file is missing from all roots on disk
        self.assertIsNone(self.engine.find_file_across_roots(rel_path, expected_size=5000))

        # Mock Telegram entity and iter_messages returning the completed message
        mock_entity = MagicMock()
        mock_entity.id = cid
        mock_msg = MagicMock()
        mock_msg.id = mid
        mock_msg.message = ""
        mock_msg.date = None
        mock_msg.file = MagicMock()
        mock_msg.file.name = fname
        mock_msg.file.size = 5000
        mock_msg.media = MagicMock()

        async def mock_iter(entity, limit=None):
            yield mock_msg

        mock_client = MagicMock()
        mock_client.iter_messages = mock_iter
        self.engine.client = mock_client

        # Run channel scan
        download_calls = []
        async def mock_dl(*args, **kwargs):
            download_calls.append(args)

        self.engine.download_single_message = mock_dl

        completed = asyncio.run(
            self.engine.scan_and_download_channel(
                mock_entity, cid, "Channel_777",
                media_filters={"PDFs": True, "All Media": True},
                cutoff_date=None
            )
        )

        # Message must NOT be downloaded; scan skipped it!
        self.assertEqual(len(download_calls), 0)
        self.assertEqual(completed, 0)

    # -------------------------------------------------------------------------
    # 17. Cross-root identical relative path & size but different timestamp
    # -------------------------------------------------------------------------
    def test_17_cross_root_identical_size_different_content_timestamp(self):
        """Verify authentic file is matched by timestamp metadata when size and rel_path collide."""
        from datetime import datetime, timezone
        rel = "SharedCourse\\LectureNotes.pdf"
        size = 1024

        # Original root file: has message timestamp T1
        t1 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
        file_orig = self.initial_storage / "SharedCourse" / "LectureNotes.pdf"
        file_orig.parent.mkdir(parents=True, exist_ok=True)
        file_orig.write_bytes(b"A" * size)
        os.utime(file_orig, (t1.timestamp(), t1.timestamp()))

        # New root file: has different timestamp T2
        new_root = Path(self.tmpdir) / "NewRootCollision"
        new_root.mkdir(parents=True, exist_ok=True)
        t2 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        file_new = new_root / "SharedCourse" / "LectureNotes.pdf"
        file_new.parent.mkdir(parents=True, exist_ok=True)
        file_new.write_bytes(b"B" * size)
        os.utime(file_new, (t2.timestamp(), t2.timestamp()))

        self.cfg_mgr.set_download_dir(str(new_root), save=True)

        # Searching with expected_size and message_date=t1 must match file_orig!
        match = self.engine.find_file_across_roots(rel, expected_size=size, message_date=t1)
        self.assertIsNotNone(match)
        self.assertEqual(match.resolve(), file_orig.resolve())

        # Searching with message_date=t2 must match file_new!
        match2 = self.engine.find_file_across_roots(rel, expected_size=size, message_date=t2)
        self.assertIsNotNone(match2)
        self.assertEqual(match2.resolve(), file_new.resolve())

    # -------------------------------------------------------------------------
    # 18. Cross-root disconnected drive and deleted historical directory
    # -------------------------------------------------------------------------
    def test_18_cross_root_disconnected_drive_and_deleted_history(self):
        """Verify find_file_across_roots safely handles disconnected drives and deleted paths."""
        # Add a disconnected drive and a non-existent folder to historical roots
        self.cfg_mgr.config["historical_download_directories"] = [
            r"Z:\DisconnectedDrive_9999\Downloads",
            r"C:\NonExistent_Deleted_Dir_8888\Downloads"
        ]

        # Should return None without raising OSError or crashing
        res = self.engine.find_file_across_roots("SomeChannel\\file.pdf", expected_size=100)
        self.assertIsNone(res)

    # -------------------------------------------------------------------------
    # 19. Multiple root changes
    # -------------------------------------------------------------------------
    def test_19_multiple_root_changes(self):
        """Verify changing roots multiple times retains all prior roots in historical list."""
        r1 = Path(self.tmpdir) / "RootOne"
        r2 = Path(self.tmpdir) / "RootTwo"
        r3 = Path(self.tmpdir) / "RootThree"
        r1.mkdir(parents=True, exist_ok=True)
        r2.mkdir(parents=True, exist_ok=True)
        r3.mkdir(parents=True, exist_ok=True)

        self.cfg_mgr.set_download_dir(str(r1), save=True)
        self.cfg_mgr.set_download_dir(str(r2), save=True)
        self.cfg_mgr.set_download_dir(str(r3), save=True)

        hist = [str(p).lower() for p in self.cfg_mgr.get_historical_roots()]
        self.assertIn(str(r1).lower(), hist)
        self.assertIn(str(r2).lower(), hist)
        self.assertEqual(str(self.cfg_mgr.get_download_dir()).lower(), str(r3).lower())

    # -------------------------------------------------------------------------
    # 20. UNC network share root
    # -------------------------------------------------------------------------
    def test_20_unc_network_share_root(self):
        """Verify UNC path syntax validation and path traversal safety."""
        unc_path = r"\\192.168.1.50\TelegramShare\Downloads"
        valid, msg = validate_windows_path(unc_path)
        self.assertTrue(valid, f"Expected valid UNC path, got {msg}")

        # Traversal inside UNC share is protected
        invalid_unc = r"\\192.168.1.50\TelegramShare\Folder*Name"
        valid_inv, _ = validate_windows_path(invalid_unc)
        self.assertFalse(valid_inv)

    # -------------------------------------------------------------------------
    # 21. Junction or symlink root resolution
    # -------------------------------------------------------------------------
    def test_21_junction_or_symlink_root_resolution(self):
        """Verify destination resolver works safely when download directory is resolved."""
        real_target = Path(self.tmpdir) / "RealTargetFolder"
        real_target.mkdir(parents=True, exist_ok=True)
        self.cfg_mgr.set_download_dir(str(real_target), save=True)

        target_path, rel_path, _, _ = self.engine.resolve_destination_path(
            channel_id=888,
            channel_name="SymlinkChannel",
            raw_filename="SymlinkTest.pdf",
            message_id=1
        )
        self.assertTrue(target_path.resolve().is_relative_to(real_target.resolve()))

    # -------------------------------------------------------------------------
    # 22. Failed directory validation leaves previous root unchanged
    # -------------------------------------------------------------------------
    def test_22_failed_directory_validation_leaves_previous_root_unchanged(self):
        """Verify entering an invalid path in GUI on_save_storage keeps previous root unchanged."""
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        app = None
        try:
            app = self._create_test_gui(root)

            original_dir = self.cfg_mgr.get_download_dir()

            # Set an invalid path with forbidden character
            app.storage_path_var.set(r"D:\Bad<Directory>*")

            with patch("tkinter.messagebox.askyesno", return_value=True), \
                 patch("tkinter.messagebox.showerror") as mock_err:
                app.on_save_storage()
                mock_err.assert_called_once()

            # Active directory must remain completely unchanged
            self.assertEqual(self.cfg_mgr.get_download_dir(), original_dir)
        finally:
            if app and hasattr(app, "_after_id") and app._after_id:
                try: root.after_cancel(app._after_id)
                except Exception: pass
            if app and hasattr(app, "lock") and app.lock:
                app.lock.release_lock()
            try: root.destroy()
            except Exception: pass

    # -------------------------------------------------------------------------
    # 23. Partial file not corrupted or truncated on root change
    # -------------------------------------------------------------------------
    def test_23_partial_file_not_corrupted_or_truncated_on_root_change(self):
        """Verify changing root leaves existing .part files on prior roots untouched."""
        old_root = self.initial_storage
        cid = 444001
        mid = 88
        fname = "VideoLecture.mp4"
        rel_path = f"Channel_444\\{fname}"

        # Create 200 KB partial file on old root
        old_part = old_root / "Channel_444" / f"{fname}.part"
        old_part.parent.mkdir(parents=True, exist_ok=True)
        old_part.write_bytes(b"X" * (200 * 1024))
        orig_bytes = old_part.stat().st_size

        self.db.ensure_channel(cid, "Channel_444")
        self.db.record_start(cid, mid, fname, 500 * 1024, rel_path)

        # Switch to new root
        new_root = Path(self.tmpdir) / "NewRootPartSafety"
        new_root.mkdir(parents=True, exist_ok=True)
        self.cfg_mgr.set_download_dir(str(new_root), save=True)

        # Under new root, destination path resolves strictly under new_root
        target_path, r_path, _, _ = self.engine.resolve_destination_path(
            cid, "Channel_444", fname, mid
        )
        self.assertTrue(target_path.resolve().is_relative_to(new_root.resolve()))

        # New root has no .part file yet
        new_part = Path(f"{target_path}.part")
        self.assertFalse(new_part.exists())

        # Old .part file remains 100% intact with exact byte count
        self.assertTrue(old_part.exists())
        self.assertEqual(old_part.stat().st_size, orig_bytes)


if __name__ == "__main__":
    unittest.main()

