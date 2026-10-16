import os
import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import ConfigManager, StorageError, validate_windows_path
from db_tracker import DownloadTrackerDB
from telegram_engine import TelegramDownloaderEngine


class TestStorageRootPropagationAndPersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="storage_prop_test_")
        self.config_path = Path(self.temp_dir) / "config.json"
        self.db_path = Path(self.temp_dir) / "test_tracker.db"

        # Start with removed F: drive in config
        self.initial_old_root = r"F:\TelegramDownloads"
        initial_cfg = {
            "download_directory": self.initial_old_root,
            "historical_download_directories": [],
            "min_free_space_gb": 0.001,
            "organize_forum_topics": True
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(initial_cfg, f, indent=2)

        self.config_mgr = ConfigManager(str(self.config_path))
        self.db = DownloadTrackerDB(str(self.db_path))

    def tearDown(self):
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    def _create_mock_gui_app(self, root):
        from telegram_gui_downloader import TelegramGUIApp
        mock_lock = MagicMock()
        mock_lock.acquire_session_lock.return_value = True
        mock_lock.release_lock.return_value = True

        mock_engine = MagicMock()
        mock_engine.check_auth = AsyncMock(return_value=True)
        mock_engine.is_downloading = False
        mock_engine.config_mgr = self.config_mgr

        with patch("telegram_gui_downloader.ConfigManager", return_value=self.config_mgr), \
             patch("telegram_gui_downloader.DownloadTrackerDB", return_value=self.db), \
             patch("telegram_gui_downloader.ProcessLock", return_value=mock_lock), \
             patch("telegram_gui_downloader.TelegramDownloaderEngine", return_value=mock_engine), \
             patch.object(TelegramGUIApp, "run_async_loop", lambda self: None), \
             patch.object(TelegramGUIApp, "poll_queue", lambda self: None):
            app = TelegramGUIApp(root)
            app.config_mgr = self.config_mgr
            app.engine = mock_engine
            return app

    def test_01_applying_new_download_root_after_old_drive_removed(self):
        """1. Applying a new download root after old drive is removed."""
        new_healthy_root = Path(self.temp_dir) / "HealthyNewRoot"
        new_healthy_root.mkdir(parents=True, exist_ok=True)

        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            app = self._create_mock_gui_app(root)
            app.storage_path_var.set(str(new_healthy_root))

            with patch("tkinter.messagebox.showinfo"), \
                 patch("tkinter.messagebox.askyesno", return_value=True):
                success = app.on_save_storage()
                self.assertTrue(success)

            active = self.config_mgr.get_download_dir()
            self.assertEqual(str(active).lower(), str(new_healthy_root).lower())
            
            # Verify old removed F: drive was preserved in historical roots
            hist = [os.path.normpath(str(h)).lower() for h in self.config_mgr.get_historical_roots()]
            self.assertIn(os.path.normpath(self.initial_old_root).lower(), hist)
        finally:
            if hasattr(app, "lock") and app.lock: app.lock.release_lock()
            root.destroy()

    def test_02_active_path_label_updating_correctly(self):
        """2. Active Path label updating correctly upon save."""
        new_healthy_root = Path(self.temp_dir) / "HealthyRootLabel"
        new_healthy_root.mkdir(parents=True, exist_ok=True)

        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            app = self._create_mock_gui_app(root)
            app.storage_path_var.set(str(new_healthy_root))

            with patch("tkinter.messagebox.showinfo"), \
                 patch("tkinter.messagebox.askyesno", return_value=True):
                app.on_save_storage()

            self.assertEqual(app.active_path_var.get(), f"Active Path: {new_healthy_root}")
        finally:
            if hasattr(app, "lock") and app.lock: app.lock.release_lock()
            root.destroy()

    def test_03_engine_immediately_using_new_root(self):
        """3. Engine immediately using the new root for destination resolution."""
        new_healthy_root = Path(self.temp_dir) / "EngineImmediateRoot"
        new_healthy_root.mkdir(parents=True, exist_ok=True)

        engine = TelegramDownloaderEngine(
            config_mgr=self.config_mgr,
            db_tracker=self.db,
            event_callback=lambda evt, data: None
        )
        engine.account_id = 774983606

        # Switch root in config_mgr
        self.config_mgr.set_download_dir(str(new_healthy_root), save=True)

        # Engine immediately resolves destination under new_healthy_root
        target_path, rel_path, _, _ = engine.resolve_destination_path(
            channel_id=12345,
            channel_name="TestChannel",
            raw_filename="lecture.mp4",
            message_id=10
        )
        self.assertTrue(target_path.resolve().is_relative_to(new_healthy_root.resolve()))
        self.assertEqual(target_path, new_healthy_root / "TestChannel" / "lecture.mp4")

    def test_04_check_space_validating_new_root(self):
        """4. Check Space validating the newly active root."""
        new_healthy_root = Path(self.temp_dir) / "CheckSpaceRoot"
        new_healthy_root.mkdir(parents=True, exist_ok=True)

        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            app = self._create_mock_gui_app(root)
            app.storage_path_var.set(str(new_healthy_root))
            with patch("tkinter.messagebox.showinfo"), \
                 patch("tkinter.messagebox.askyesno", return_value=True):
                app.on_save_storage()

            # Calling update_storage_display (Check Space callback)
            app.update_storage_display()
            self.assertIn("(Ready)", app.storage_status_var.get())
        finally:
            if hasattr(app, "lock") and app.lock: app.lock.release_lock()
            root.destroy()

    def test_05_save_failure_retaining_previous_root(self):
        """5. Save failure retaining the previous root."""
        original_root = Path(self.temp_dir) / "OriginalWorkingRoot"
        original_root.mkdir(parents=True, exist_ok=True)
        self.config_mgr.set_download_dir(str(original_root), save=True)

        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            app = self._create_mock_gui_app(root)
            # User cancels directory creation for non-existent path
            app.storage_path_var.set(r"E:\NonExistent_Never_Created_Dir_999")

            with patch("tkinter.messagebox.askyesno", return_value=False):
                success = app.on_save_storage()
                self.assertFalse(success)

            # Active directory must remain original_root
            self.assertEqual(self.config_mgr.get_download_dir(), original_root)
        finally:
            if hasattr(app, "lock") and app.lock: app.lock.release_lock()
            root.destroy()

    def test_06_restart_preserving_new_root(self):
        """6. Restart preserving the new root from persisted config.json."""
        persisted_root = Path(self.temp_dir) / "PersistedRoot"
        persisted_root.mkdir(parents=True, exist_ok=True)

        self.config_mgr.set_download_dir(str(persisted_root), save=True)

        # Simulate restart with fresh ConfigManager reading config.json
        fresh_config_mgr = ConfigManager(str(self.config_path))
        self.assertEqual(str(fresh_config_mgr.get_download_dir()).lower(), str(persisted_root).lower())

    def test_07_gui_and_engine_disagreement_blocking_download(self):
        """7. GUI and engine disagreement blocking download initiation."""
        active_root = Path(self.temp_dir) / "GuiRoot"
        active_root.mkdir(parents=True, exist_ok=True)
        self.config_mgr.set_download_dir(str(active_root), save=True)

        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            app = self._create_mock_gui_app(root)

            # Artificially desynchronize engine config_mgr
            desync_cfg = ConfigManager(str(self.config_path))
            desync_cfg.config["download_directory"] = r"Z:\DesynchronizedRoot"
            app.engine.config_mgr = desync_cfg

            with patch("tkinter.messagebox.showerror") as mock_err:
                ready = app.verify_storage_ready_for_download()
                self.assertFalse(ready)
                mock_err.assert_called_once()
                self.assertIn("Storage Desynchronization", mock_err.call_args[0][0])
        finally:
            if hasattr(app, "lock") and app.lock: app.lock.release_lock()
            root.destroy()

    def test_08_historical_disconnected_roots_ignored_for_new_destinations(self):
        """8. Historical disconnected roots being ignored for new destinations."""
        active_root = Path(self.temp_dir) / "ActiveHealthyRoot"
        active_root.mkdir(parents=True, exist_ok=True)
        self.config_mgr.set_download_dir(str(active_root), save=True)

        # Place disconnected F: in historical roots
        self.config_mgr.config["historical_download_directories"] = [r"F:\TelegramDownloads"]

        engine = TelegramDownloaderEngine(
            config_mgr=self.config_mgr,
            db_tracker=self.db,
            event_callback=lambda evt, data: None
        )
        engine.account_id = 774983606

        target_path, rel_path, _, _ = engine.resolve_destination_path(
            channel_id=98765,
            channel_name="ForumGroup",
            raw_filename="guide.pdf",
            message_id=42,
            is_forum=False
        )
        self.assertEqual(target_path, active_root / "ForumGroup" / "guide.pdf")
        self.assertTrue(str(target_path).startswith(str(active_root)))
        self.assertFalse(str(target_path).startswith(r"F:\TelegramDownloads"))

    def test_09_no_fallback_to_f_or_c(self):
        """9. No fallback to removed F: or system C: on storage error."""
        # Unset storage
        self.config_mgr.config["download_directory"] = ""

        with self.assertRaises(StorageError):
            self.config_mgr.get_download_dir()

        # Inaccessible F:
        valid, msg, _ = self.config_mgr.validate_storage(r"F:\TelegramDownloads")
        self.assertFalse(valid)
        self.assertNotIn("c:", self.config_mgr.config.get("download_directory", "").lower())

    def test_10_changing_directory_while_idle(self):
        """10. Changing directory while idle succeeds normally."""
        idle_root = Path(self.temp_dir) / "IdleRoot"
        idle_root.mkdir(parents=True, exist_ok=True)

        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            app = self._create_mock_gui_app(root)
            app.engine.is_downloading = False
            app.storage_path_var.set(str(idle_root))

            with patch("tkinter.messagebox.showinfo"), \
                 patch("tkinter.messagebox.askyesno", return_value=True):
                success = app.on_save_storage()
                self.assertTrue(success)
                self.assertEqual(self.config_mgr.get_download_dir(), idle_root)
        finally:
            if hasattr(app, "lock") and app.lock: app.lock.release_lock()
            root.destroy()

    def test_11_preventing_directory_changes_during_active_transfers(self):
        """11. Preventing directory changes during active transfers."""
        current_root = Path(self.temp_dir) / "ActiveTransferRoot"
        current_root.mkdir(parents=True, exist_ok=True)
        self.config_mgr.set_download_dir(str(current_root), save=True)

        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            app = self._create_mock_gui_app(root)
            # Simulate active download running
            app.engine.is_downloading = True
            app.storage_path_var.set(r"D:\AttemptedDuringDownload")

            with patch("tkinter.messagebox.showwarning") as mock_warn:
                success = app.on_save_storage()
                self.assertFalse(success)
                mock_warn.assert_called_once()
                self.assertIn("Download In Progress", mock_warn.call_args[0][0])

            # Root was not mutated
            self.assertEqual(self.config_mgr.get_download_dir(), current_root)
        finally:
            if hasattr(app, "lock") and app.lock: app.lock.release_lock()
            root.destroy()

    def test_12_gui_worker_configuration_agreement(self):
        """12. GUI and headless worker configuration agreement via dynamic reload."""
        initial_root = Path(self.temp_dir) / "WorkerInitRoot"
        initial_root.mkdir(parents=True, exist_ok=True)
        self.config_mgr.set_download_dir(str(initial_root), save=True)

        # Worker instance with its own ConfigManager instance
        worker_cfg_mgr = ConfigManager(str(self.config_path))
        self.assertEqual(worker_cfg_mgr.get_download_dir(), initial_root)

        # Now GUI modifies config on disk
        updated_root = Path(self.temp_dir) / "WorkerUpdatedRoot"
        updated_root.mkdir(parents=True, exist_ok=True)
        self.config_mgr.set_download_dir(str(updated_root), save=True)

        # Worker's config_mgr dynamically reloads and sees the updated root without restarting
        self.assertEqual(worker_cfg_mgr.get_download_dir(), updated_root)


if __name__ == "__main__":
    unittest.main()
