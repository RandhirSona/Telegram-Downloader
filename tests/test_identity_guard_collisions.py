"""
Isolated mock tests for Composite Identity Guard and cross-account/cross-peer collisions.
Evaluates enforced composite identity (account_id, peer_type, channel_id) and proves
that cross-account and cross-peer collisions are strictly blocked.
"""

import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from telethon.tl.types import Channel, Chat
from config_manager import ConfigManager
from db_tracker import DownloadTrackerDB
from telegram_engine import TelegramDownloaderEngine, normalize_channel_id


class TestIdentityGuardCollisions(unittest.TestCase):
    def setUp(self):
        self.cfg = MagicMock(spec=ConfigManager)
        self.cfg.validate_storage.return_value = (True, "OK", 100.0)
        self.cfg.config = {"media_filters": {}, "cutoff_date": ""}
        self.db = MagicMock(spec=DownloadTrackerDB)
        self.engine = TelegramDownloaderEngine(self.cfg, self.db)
        self.account_id = 123456789
        self.channel_id = 4450272939
        self.peer_type = "channel"
        self.engine.account_id = self.account_id

        # Setup mock DB queries to recognize selection and topics
        self.db.get_selection.return_value = {"channel_id": self.channel_id}
        self.db.get_channel_storage_mapping.return_value = {"is_forum": True}
        self.db.get_forum_topics.return_value = [{"topic_id": 150}]

        # Create mock forum supergroup entity
        self.forum_entity = MagicMock(spec=Channel)
        self.forum_entity.id = self.channel_id
        self.forum_entity.forum = True
        self.forum_entity.megagroup = True

    def test_enforced_composite_guard_success(self):
        """Full composite identity passes validation."""
        loop = asyncio.new_event_loop()
        mock_scan = AsyncMock(return_value=0)
        self.engine.scan_and_download_channel = mock_scan

        dialogs = [{
            "id": self.channel_id,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]

        loop.run_until_complete(
            self.engine.run_batch(
                dialogs,
                target_topic_ids={150},
                expected_channel_id=self.channel_id,
                expected_peer_type=self.peer_type,
                expected_account_id=self.account_id
            )
        )
        self.assertEqual(mock_scan.call_count, 1)

        # Mismatched numeric ID raises ValueError
        with self.assertRaises(ValueError):
            loop.run_until_complete(
                self.engine.run_batch(
                    dialogs,
                    target_topic_ids={150},
                    expected_channel_id=9999999999,
                    expected_peer_type=self.peer_type,
                    expected_account_id=self.account_id
                )
            )

        loop.close()

    def test_cross_peer_collision_blocked(self):
        """
        Proves cross-peer collision defense:
        A basic group chat (peer_type='chat') sharing numeric ID with a channel
        is strictly rejected when expected_peer_type='channel'.
        """
        loop = asyncio.new_event_loop()
        colliding_chat_entity = MagicMock(spec=Chat)
        colliding_chat_entity.id = self.channel_id

        dialogs = [{
            "id": self.channel_id,
            "name": "Basic Group Chat Colliding",
            "peer_type": "chat",
            "entity": colliding_chat_entity
        }]

        with self.assertRaises(ValueError) as ctx:
            loop.run_until_complete(
                self.engine.run_batch(
                    dialogs,
                    target_topic_ids={150},
                    expected_channel_id=self.channel_id,
                    expected_peer_type="channel",
                    expected_account_id=self.account_id
                )
            )
        self.assertIn("entity peer type 'chat' does not match expected peer type 'channel'", str(ctx.exception))
        loop.close()

    def test_cross_account_collision_blocked(self):
        """
        Proves cross-account collision defense:
        If caller specifies expected_account_id=888888888, but engine is authenticated
        as Account 123456789, request is strictly rejected.
        """
        loop = asyncio.new_event_loop()
        dialogs = [{
            "id": self.channel_id,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]

        with self.assertRaises(ValueError) as ctx:
            loop.run_until_complete(
                self.engine.run_batch(
                    dialogs,
                    target_topic_ids={150},
                    expected_channel_id=self.channel_id,
                    expected_peer_type=self.peer_type,
                    expected_account_id=888888888 # Foreign account ID
                )
            )
        self.assertIn("authenticated engine account ID 123456789 does not match expected account ID 888888888", str(ctx.exception))
        loop.close()


if __name__ == "__main__":
    unittest.main()
