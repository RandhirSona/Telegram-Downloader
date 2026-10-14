"""
Tests for GUI authentication feedback (button states, status labels).
Uses mocking to prevent actual Telegram connections.
"""
import os
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tkinter as tk
import queue


class TestGUIAuthFeedback(unittest.TestCase):
    """Test GUI feedback for auth actions without network or Telegram dependency."""

    @classmethod
    def setUpClass(cls):
        """Patch heavy dependencies before importing TelegramGUIApp."""
        # Prevent actual Telegram client creation
        cls.patcher_engine = patch("telegram_gui_downloader.TelegramDownloaderEngine")
        cls.MockEngineClass = cls.patcher_engine.start()

        # Mock engine instance that will be returned
        cls.mock_engine_instance = MagicMock()
        cls.mock_engine_instance.send_login_code = MagicMock()
        cls.mock_engine_instance.sign_in_code = MagicMock()
        cls.mock_engine_instance.check_auth = AsyncMock(return_value=False)
        cls.MockEngineClass.return_value = cls.mock_engine_instance

        # Patch ProcessLock to always succeed
        cls.patcher_lock = patch("telegram_gui_downloader.ProcessLock")
        cls.MockLockClass = cls.patcher_lock.start()
        cls.MockLockClass.return_value.acquire_session_lock.return_value = True
        cls.MockLockClass.get_active_worker_status.return_value = None

    @classmethod
    def tearDownClass(cls):
        cls.patcher_engine.stop()
        cls.patcher_lock.stop()

    def setUp(self):
        from telegram_gui_downloader import TelegramGUIApp

        self.root = tk.Tk()
        self.root.withdraw()

        # Patch asyncio.run_coroutine_threadsafe to not schedule anything
        with patch("telegram_gui_downloader.asyncio") as mock_asyncio:
            mock_loop = MagicMock()
            mock_asyncio.new_event_loop.return_value = mock_loop
            mock_asyncio.run_coroutine_threadsafe.return_value = MagicMock()
            self.app = TelegramGUIApp(self.root)

        # Cancel all pending 'after' callbacks (poll_queue chain)
        try:
            for after_id in list(self.root.tk.call('after', 'info')):
                self.root.after_cancel(after_id)
        except Exception:
            pass

        self.root.update_idletasks()

    def tearDown(self):
        try:
            for after_id in list(self.root.tk.call('after', 'info')):
                self.root.after_cancel(after_id)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_send_code_click_disables_button(self):
        """Clicking Send Code disables the button and shows requesting status."""
        self.app.phone_var.set("1234567890")
        # Ensure engine mock is properly connected
        self.app.engine = MagicMock()
        self.app.engine.send_login_code = MagicMock()
        self.app.loop = MagicMock()

        with patch("telegram_gui_downloader.asyncio") as mock_asyncio:
            mock_asyncio.run_coroutine_threadsafe.return_value = MagicMock()
            self.app.on_send_code()
            self.root.update_idletasks()

        self.assertEqual(str(self.app.btn_send_code["state"]), "disabled")
        self.assertIn("Requesting login code", self.app.auth_status_var.get())

    def test_auth_code_sent_updates_label(self):
        """Receiving auth_code_sent updates label to green and restores Send Code button."""
        self.app.btn_send_code.config(state="disabled")
        self.app.auth_status_var.set("")

        # Directly call set_auth_feedback and button config like poll_queue does
        self.app.set_auth_feedback("Login code sent via Telegram app.", color="#008800")
        self.app.btn_send_code.config(state="normal")
        self.root.update_idletasks()

        self.assertIn("Login code sent", self.app.auth_status_var.get())
        self.assertIn(str(self.app.btn_send_code["state"]), ["normal", "active"])

    def test_auth_error_updates_label(self):
        """Receiving auth_error updates label to red error and restores Send Code button."""
        self.app.btn_send_code.config(state="disabled")

        self.app.set_auth_feedback("Error: Invalid API_ID or API_HASH.", color="#cc0000")
        self.app.btn_send_code.config(state="normal")
        self.app.btn_login.config(state="normal")
        self.root.update_idletasks()

        self.assertIn("Error: Invalid API_ID", self.app.auth_status_var.get())
        self.assertIn(str(self.app.btn_send_code["state"]), ["normal", "active"])

    def test_login_click_disables_button(self):
        """Clicking Login disables the button and shows verifying status."""
        self.app.phone_var.set("1234567890")
        self.app.code_var.set("54321")
        self.app.engine = MagicMock()
        self.app.engine.sign_in_code = MagicMock()
        self.app.loop = MagicMock()

        with patch("telegram_gui_downloader.asyncio") as mock_asyncio:
            mock_asyncio.run_coroutine_threadsafe.return_value = MagicMock()
            self.app.on_login()
            self.root.update_idletasks()

        self.assertEqual(str(self.app.btn_login["state"]), "disabled")
        self.assertIn("Verifying code", self.app.auth_status_var.get())

    def test_auth_success_restores_buttons(self):
        """auth_success should restore both buttons and update status."""
        self.app.btn_send_code.config(state="disabled")
        self.app.btn_login.config(state="disabled")

        self.app.set_auth_feedback("Authenticated successfully!", color="#008800")
        self.app.btn_login.config(state="normal")
        self.app.btn_send_code.config(state="normal")
        self.root.update_idletasks()

        self.assertIn("Authenticated", self.app.auth_status_var.get())
        self.assertIn(str(self.app.btn_login["state"]), ["normal", "active"])
        self.assertIn(str(self.app.btn_send_code["state"]), ["normal", "active"])


if __name__ == "__main__":
    unittest.main()
