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
        shutil.rmtree(self.tmpdir, ignore_errors=True)

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
        from telegram_gui_downloader import TelegramGUIApp
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        app = None
        try:
            app = TelegramGUIApp(root)
            app.config_mgr = self.cfg_mgr
            app.engine = self.engine

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
        from telegram_gui_downloader import TelegramGUIApp
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        app = None
        try:
            app = TelegramGUIApp(root)
            app.config_mgr = self.cfg_mgr
            app.engine = self.engine

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
        from telegram_gui_downloader import TelegramGUIApp
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        app = None
        try:
            app = TelegramGUIApp(root)
            app.config_mgr = self.cfg_mgr
            app.engine = self.engine

            # Simulate an active download in progress
            self.engine.is_downloading = True

            with patch("tkinter.messagebox.showwarning") as mock_warn:
                app.on_save_storage()
                mock_warn.assert_called_once()
                self.assertIn("active", mock_warn.call_args[0][1].lower())

            # Verify active root did not change
            self.assertEqual(self.cfg_mgr.get_download_dir(), self.initial_storage)
        finally:
            self.engine.is_downloading = False
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


if __name__ == "__main__":
    unittest.main()
