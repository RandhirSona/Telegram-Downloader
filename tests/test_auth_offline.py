import os
import sys
import unittest
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import ConfigManager
from db_tracker import DownloadTrackerDB
from telegram_engine import TelegramDownloaderEngine
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    PhoneNumberInvalidError,
    ApiIdInvalidError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberBannedError
)
from telethon.tl.types.auth import SentCodeTypeApp, SentCodeTypeSms

class TestTelegramAuthOffline(unittest.TestCase):

    def setUp(self):
        self.events = []
        self.engine = TelegramDownloaderEngine(
            config_mgr=ConfigManager(),
            db_tracker=DownloadTrackerDB(":memory:"),
            event_callback=lambda evt, data: self.events.append((evt, data))
        )

    def test_missing_api_credentials(self):
        """Verify missing API_ID/API_HASH emits auth_error without crashing with AttributeError."""
        with patch.dict(os.environ, {"API_ID": "", "API_HASH": ""}, clear=True), \
             patch("pathlib.Path.exists", return_value=False):
            res = asyncio.run(self.engine.init_client())
            self.assertFalse(res)
            self.assertIsNone(self.engine.client)

            # Test send_login_code with missing credentials
            asyncio.run(self.engine.send_login_code("1234567890"))
            auth_errors = [d for evt, d in self.events if evt == "auth_error"]
            self.assertTrue(len(auth_errors) > 0)
            self.assertIn("API_ID or API_HASH is missing", auth_errors[0])

    def test_invalid_api_id_type(self):
        """Verify non-numeric API_ID is caught and emits auth_error."""
        with patch.dict(os.environ, {"API_ID": "not_a_number", "API_HASH": "1234567890abcdef"}, clear=True), \
             patch("pathlib.Path.exists", return_value=False):
            res = asyncio.run(self.engine.init_client())
            self.assertFalse(res)
            auth_errors = [d for evt, d in self.events if evt == "auth_error"]
            self.assertTrue(any("API_ID must be a numeric integer" in err for err in auth_errors))

    def test_api_id_invalid_error_on_connect(self):
        """Verify Telegram rejecting API credentials emits clear auth_error."""
        with patch.dict(os.environ, {"API_ID": "12345", "API_HASH": "abcdef1234567890"}, clear=True), \
             patch("pathlib.Path.exists", return_value=False):
            mock_client = MagicMock()
            mock_client.is_connected.return_value = False
            mock_client.connect = AsyncMock(side_effect=ApiIdInvalidError(request=None))
            self.engine.client = mock_client

            res = asyncio.run(self.engine.init_client())
            self.assertFalse(res)
            auth_errors = [d for evt, d in self.events if evt == "auth_error"]
            self.assertTrue(any("Telegram rejected API_ID / API_HASH" in err for err in auth_errors))

    def test_invalid_phone_number_format(self):
        """Verify malformed phone number (<5 digits) is caught prior to Telegram call."""
        with patch.object(self.engine, "init_client", new_callable=AsyncMock) as mock_init:
            mock_init.return_value = True
            asyncio.run(self.engine.send_login_code("123"))  # Too short
            auth_errors = [d for evt, d in self.events if evt == "auth_error"]
            self.assertTrue(any("Invalid phone number format" in err for err in auth_errors))

    def test_telegram_phone_number_invalid_error(self):
        """Verify Telegram PhoneNumberInvalidError emits helpful guidance."""
        with patch.object(self.engine, "init_client", new_callable=AsyncMock) as mock_init:
            mock_init.return_value = True
            mock_client = MagicMock()
            mock_client.send_code_request = AsyncMock(side_effect=PhoneNumberInvalidError(request=None))
            self.engine.client = mock_client

            asyncio.run(self.engine.send_login_code("99999999999999"))
            auth_errors = [d for evt, d in self.events if evt == "auth_error"]
            self.assertTrue(any("not recognized by Telegram" in err for err in auth_errors))

    def test_telegram_flood_wait_error(self):
        """Verify Telegram FloodWaitError emits exact retry seconds."""
        with patch.object(self.engine, "init_client", new_callable=AsyncMock) as mock_init:
            mock_init.return_value = True
            mock_client = MagicMock()
            mock_client.send_code_request = AsyncMock(side_effect=FloodWaitError(request=None, capture=120))
            self.engine.client = mock_client

            asyncio.run(self.engine.send_login_code("1234567890"))
            auth_errors = [d for evt, d in self.events if evt == "auth_error"]
            self.assertTrue(any("wait 120 seconds" in err for err in auth_errors))

    def test_telegram_network_timeout(self):
        """Verify network timeout during code request emits network failure message."""
        with patch.object(self.engine, "init_client", new_callable=AsyncMock) as mock_init:
            mock_init.return_value = True
            mock_client = MagicMock()
            mock_client.send_code_request = AsyncMock(side_effect=ConnectionError("Connection timed out"))
            self.engine.client = mock_client

            asyncio.run(self.engine.send_login_code("1234567890"))
            auth_errors = [d for evt, d in self.events if evt == "auth_error"]
            self.assertTrue(any("Network connection failed" in err for err in auth_errors))

    def test_successful_code_delivery(self):
        """Verify successful code delivery saves phone_code_hash and emits auth_code_sent."""
        with patch.object(self.engine, "init_client", new_callable=AsyncMock) as mock_init:
            mock_init.return_value = True
            mock_client = MagicMock()
            mock_sent = MagicMock()
            mock_sent.phone_code_hash = "mock_hash_abc_123"
            mock_sent.type = SentCodeTypeApp(length=5)
            mock_client.send_code_request = AsyncMock(return_value=mock_sent)
            self.engine.client = mock_client

            asyncio.run(self.engine.send_login_code("1234567890"))
            self.assertEqual(self.engine.phone, "1234567890")
            self.assertEqual(self.engine.phone_code_hash, "mock_hash_abc_123")

            code_sent_events = [d for evt, d in self.events if evt == "auth_code_sent"]
            self.assertEqual(len(code_sent_events), 1)
            self.assertIn("Login code sent via Telegram app", code_sent_events[0])

    def test_successful_sign_in_with_hash(self):
        """Verify sign_in passes phone_code_hash and emits auth_success."""
        with patch.object(self.engine, "init_client", new_callable=AsyncMock) as mock_init:
            mock_init.return_value = True
            mock_client = MagicMock()
            mock_client.sign_in = AsyncMock(return_value=MagicMock())
            self.engine.client = mock_client
            self.engine.phone = "1234567890"
            self.engine.phone_code_hash = "mock_hash_abc_123"

            asyncio.run(self.engine.sign_in_code("1234567890", "54321"))
            mock_client.sign_in.assert_called_once_with("1234567890", "54321", phone_code_hash="mock_hash_abc_123")
            self.assertTrue(any(evt == "auth_success" for evt, d in self.events))

    def test_2fa_password_needed(self):
        """Verify SessionPasswordNeededError emits password_needed."""
        with patch.object(self.engine, "init_client", new_callable=AsyncMock) as mock_init:
            mock_init.return_value = True
            mock_client = MagicMock()
            mock_client.sign_in = AsyncMock(side_effect=SessionPasswordNeededError(request=None))
            self.engine.client = mock_client

            asyncio.run(self.engine.sign_in_code("1234567890", "54321"))
            self.assertTrue(any(evt == "password_needed" for evt, d in self.events))

    def test_phone_code_invalid_error(self):
        """Verify invalid OTP code emits user-friendly error."""
        with patch.object(self.engine, "init_client", new_callable=AsyncMock) as mock_init:
            mock_init.return_value = True
            mock_client = MagicMock()
            mock_client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(request=None))
            self.engine.client = mock_client

            asyncio.run(self.engine.sign_in_code("1234567890", "99999"))
            auth_errors = [d for evt, d in self.events if evt == "auth_error"]
            self.assertTrue(any("OTP code you entered is invalid" in err for err in auth_errors))

    def test_existing_authorized_session(self):
        """Verify check_auth recognizes an already authorized session."""
        with patch.object(self.engine, "init_client", new_callable=AsyncMock) as mock_init:
            mock_init.return_value = True
            mock_client = MagicMock()
            mock_client.is_user_authorized = AsyncMock(return_value=True)
            self.engine.client = mock_client

            res = asyncio.run(self.engine.check_auth())
            self.assertTrue(res)
            self.assertTrue(any(evt == "auth_success" for evt, d in self.events))

if __name__ == "__main__":
    unittest.main()
