"""
Comprehensive Regression Tests for Hardened Composite Identity & Scope Guard:
(account_id, peer_type, channel_id)

Targeted test scenarios:
1. Empty topic catalog rejects the request.
2. Unknown topic ID rejects the request.
3. Missing Telegram entity rejects the request.
4. Non-forum entity rejects the request.
5. Account mismatch rejects the request.
6. Peer-type mismatch rejects the request.
7. Cross-account topic-cache collision is prevented.
8. Valid existing topic selection succeeds.
9. Valid multiple-topic selection succeeds.
10. Normal non-topic downloading remains backward compatible.
11. Empty topic selection set rejects the request.
12. Multi-channel batch rejects the request.
13. No fallback to all saved channels.
"""

import unittest
import asyncio
import tempfile
import os
import shutil
from unittest.mock import MagicMock, AsyncMock

from telethon.tl.types import Channel, Chat, User
from config_manager import ConfigManager
from db_tracker import DownloadTrackerDB
from telegram_engine import TelegramDownloaderEngine, normalize_channel_id


class TestCompositeIdentityGuard(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_composite_guard_")
        self.db_path = os.path.join(self.test_dir, "test_guard.db")
        self.download_dir = os.path.join(self.test_dir, "downloads")
        os.makedirs(self.download_dir, exist_ok=True)

        self.cfg = ConfigManager(config_path=os.path.join(self.test_dir, "config.json"))
        self.cfg.config["download_directory"] = self.download_dir
        self.cfg.config["organize_forum_topics"] = True
        self.cfg.save_config()

        self.db = DownloadTrackerDB(db_path=self.db_path)
        self.db.migrate_to_stage_b()

        self.account_id = 123456789
        self.forum_cid = 4450272939
        self.peer_type = "channel"

        self.other_forum_cid = 4412184534
        self.broadcast_cid = 3061094461

        # Seed selections in DB
        self.db.add_selection(self.account_id, self.forum_cid, "Premium 2027", self.peer_type)
        self.db.add_selection(self.account_id, self.other_forum_cid, "UPPCS TEST 2026", "channel")
        self.db.add_selection(self.account_id, self.broadcast_cid, "FORUM PTS 2026", "channel")

        # Seed storage mappings
        self.db.set_channel_storage_mapping(self.account_id, self.forum_cid, "Premium 2027", "Premium 2027", is_forum=True)
        self.db.set_channel_storage_mapping(self.account_id, self.other_forum_cid, "UPPCS TEST 2026", "UPPCS TEST 2026", is_forum=True)
        self.db.set_channel_storage_mapping(self.account_id, self.broadcast_cid, "FORUM PTS 2026", "FORUM PTS 2026", is_forum=False)

        # Seed forum topics for Premium 2027
        self.db.save_forum_topic(self.forum_cid, 150, "DAILY NEWSPAPER", "DAILY NEWSPAPER", account_id=self.account_id, peer_type=self.peer_type)
        self.db.save_forum_topic(self.forum_cid, 2, "Vision Prelims 2027", "Vision Prelims 2027", account_id=self.account_id, peer_type=self.peer_type)

        # Seed forum topics for UPPCS TEST 2026
        self.db.save_forum_topic(self.other_forum_cid, 20, "GS Paper 1", "GS Paper 1", account_id=self.account_id, peer_type="channel")

        # Create engine
        self.engine = TelegramDownloaderEngine(self.cfg, self.db)
        self.engine.account_id = self.account_id
        self.engine.scan_and_download_channel = AsyncMock(return_value=0)

        # Create mock Telethon Channel entity with forum flags
        self.forum_entity = MagicMock(spec=Channel)
        self.forum_entity.id = self.forum_cid
        self.forum_entity.title = "Premium 2027"
        self.forum_entity.forum = True
        self.forum_entity.megagroup = True

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # 1. Empty topic catalog rejects the request
    def test_01_empty_topic_catalog_rejects_request(self):
        # Empty the forum_topics in DB for this channel
        conn = self.db._get_connection()
        conn.execute("DELETE FROM forum_topics WHERE channel_id = ?;", (self.forum_cid,))
        conn.commit()
        conn.close()
        self.engine._forum_topics_cache.clear()

        dialogs = [{
            "id": self.forum_cid,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_ids={150},
                expected_channel_id=self.forum_cid,
                expected_peer_type=self.peer_type,
                expected_account_id=self.account_id
            ))
        self.assertIn("verified topic catalog", str(ctx.exception))
        self.assertIn("missing or empty", str(ctx.exception))

    # 2. Unknown topic ID rejects the request
    def test_02_unknown_topic_id_rejects_request(self):
        dialogs = [{
            "id": self.forum_cid,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]
        # Topic 99999 does not exist in Premium 2027 catalog
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_ids={99999},
                expected_channel_id=self.forum_cid,
                expected_peer_type=self.peer_type,
                expected_account_id=self.account_id
            ))
        self.assertIn("do not belong to verified forum supergroup", str(ctx.exception))

    # 3. Missing Telegram entity rejects the request
    def test_03_missing_telegram_entity_rejects_request(self):
        dialogs = [{
            "id": self.forum_cid,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": None # Missing live entity
        }]
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_ids={150},
                expected_channel_id=self.forum_cid,
                expected_peer_type=self.peer_type,
                expected_account_id=self.account_id
            ))
        self.assertIn("missing or unresolvable Telegram entity", str(ctx.exception))

    # 4. Non-forum entity rejects the request
    def test_04_non_forum_entity_rejects_request(self):
        non_forum_entity = MagicMock(spec=Channel)
        non_forum_entity.id = self.forum_cid
        non_forum_entity.title = "Non Forum Supergroup"
        non_forum_entity.forum = False
        non_forum_entity.megagroup = True

        dialogs = [{
            "id": self.forum_cid,
            "name": "Non Forum Supergroup",
            "peer_type": self.peer_type,
            "entity": non_forum_entity
        }]
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_ids={150},
                expected_channel_id=self.forum_cid,
                expected_peer_type=self.peer_type,
                expected_account_id=self.account_id
            ))
        self.assertIn("not a genuine Telegram forum supergroup", str(ctx.exception))

    # 5. Account mismatch rejects the request
    def test_05_account_mismatch_rejects_request(self):
        dialogs = [{
            "id": self.forum_cid,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_ids={150},
                expected_channel_id=self.forum_cid,
                expected_peer_type=self.peer_type,
                expected_account_id=888888888 # Mismatched account ID
            ))
        self.assertIn("authenticated engine account ID", str(ctx.exception))

    # 6. Peer-type mismatch rejects the request
    def test_06_peer_type_mismatch_rejects_request(self):
        chat_entity = MagicMock(spec=Chat)
        chat_entity.id = self.forum_cid
        chat_entity.title = "Colliding Chat"

        dialogs = [{
            "id": self.forum_cid,
            "name": "Colliding Chat",
            "peer_type": "chat", # Chat instead of channel
            "entity": chat_entity
        }]
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_ids={150},
                expected_channel_id=self.forum_cid,
                expected_peer_type="chat", # Peer type is not channel
                expected_account_id=self.account_id
            ))
        self.assertIn("topic-scoped downloads strictly require expected_peer_type='channel'", str(ctx.exception))

    # 7. Cross-account topic-cache collision is prevented
    def test_07_cross_account_topic_cache_collision_prevented(self):
        acc_a = 111111111
        acc_b = 222222222
        shared_cid = 4450272939

        # Seed account A topics in cache
        key_a = (acc_a, "channel", shared_cid)
        self.engine._forum_topics_cache[key_a] = {
            10: {"id": 10, "title": "Account A Exclusive Topic"}
        }

        # Seed account B topics in cache
        key_b = (acc_b, "channel", shared_cid)
        self.engine._forum_topics_cache[key_b] = {
            20: {"id": 20, "title": "Account B Exclusive Topic"}
        }

        # Verify key_a and key_b are completely isolated
        self.assertIn(10, self.engine._forum_topics_cache[key_a])
        self.assertNotIn(20, self.engine._forum_topics_cache[key_a])
        self.assertIn(20, self.engine._forum_topics_cache[key_b])
        self.assertNotIn(10, self.engine._forum_topics_cache[key_b])

    # 8. Valid existing topic selection succeeds
    def test_08_valid_existing_topic_selection_succeeds(self):
        dialogs = [{
            "id": self.forum_cid,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]
        asyncio.run(self.engine.run_batch(
            dialogs,
            target_topic_ids={150},
            expected_channel_id=self.forum_cid,
            expected_peer_type=self.peer_type,
            expected_account_id=self.account_id
        ))
        self.assertEqual(self.engine.scan_and_download_channel.call_count, 1)
        call_kwargs = self.engine.scan_and_download_channel.call_args[1]
        self.assertEqual(call_kwargs["target_topic_ids"], {150})

    # 9. Valid multiple-topic selection succeeds
    def test_09_valid_multiple_topic_selection_succeeds(self):
        dialogs = [{
            "id": self.forum_cid,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]
        asyncio.run(self.engine.run_batch(
            dialogs,
            target_topic_ids={150, 2},
            expected_channel_id=self.forum_cid,
            expected_peer_type=self.peer_type,
            expected_account_id=self.account_id
        ))
        self.assertEqual(self.engine.scan_and_download_channel.call_count, 1)
        call_kwargs = self.engine.scan_and_download_channel.call_args[1]
        self.assertEqual(call_kwargs["target_topic_ids"], {150, 2})

    # 10. Normal non-topic downloading remains backward compatible
    def test_10_normal_non_topic_downloading_backward_compatible(self):
        dialogs = [
            {"id": self.forum_cid, "name": "Premium 2027", "entity": self.forum_entity},
            {"id": self.broadcast_cid, "name": "FORUM PTS 2026", "entity": MagicMock(spec=Channel)}
        ]
        asyncio.run(self.engine.run_batch(dialogs))
        self.assertEqual(self.engine.scan_and_download_channel.call_count, 2)

    # 11. Empty topic selection set rejects the request
    def test_11_empty_topic_selection_rejects(self):
        dialogs = [{
            "id": self.forum_cid,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_ids=set(),
                expected_channel_id=self.forum_cid,
                expected_peer_type=self.peer_type,
                expected_account_id=self.account_id
            ))
        self.assertIn("empty topic selection set", str(ctx.exception))

    # 12. Multiple channel batch rejects the request
    def test_12_multiple_channel_batch_rejects(self):
        dialogs = [
            {"id": self.forum_cid, "name": "Premium 2027", "peer_type": self.peer_type, "entity": self.forum_entity},
            {"id": self.other_forum_cid, "name": "UPPCS TEST 2026", "peer_type": "channel", "entity": MagicMock(spec=Channel)}
        ]
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_ids={150},
                expected_channel_id=self.forum_cid,
                expected_peer_type=self.peer_type,
                expected_account_id=self.account_id
            ))
        self.assertIn("strictly 1 required", str(ctx.exception))

    # 13. No fallback to all saved channels
    def test_13_no_fallback_to_all_saved_channels(self):
        dialogs = [{
            "id": self.forum_cid,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]
        asyncio.run(self.engine.run_batch(
            dialogs,
            target_topic_ids={150},
            expected_channel_id=self.forum_cid,
            expected_peer_type=self.peer_type,
            expected_account_id=self.account_id
        ))
        self.assertEqual(self.engine.scan_and_download_channel.call_count, 1)
        self.assertEqual(self.engine.scan_and_download_channel.call_args[0][1], self.forum_cid)

    # 14. Singular target_topic_id activates composite guard identically
    def test_14_singular_target_topic_id_activates_guard(self):
        dialogs = [{
            "id": self.forum_cid,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]
        # A: Valid singular target_topic_id succeeds
        asyncio.run(self.engine.run_batch(
            dialogs,
            target_topic_id=150, # Singular parameter
            expected_channel_id=self.forum_cid,
            expected_peer_type=self.peer_type,
            expected_account_id=self.account_id
        ))
        self.assertEqual(self.engine.scan_and_download_channel.call_count, 1)

        # B: Mismatched account with singular parameter rejects
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_id=150,
                expected_channel_id=self.forum_cid,
                expected_peer_type=self.peer_type,
                expected_account_id=999999999
            ))
        self.assertIn("authenticated engine account ID", str(ctx.exception))

        # C: Unknown topic with singular parameter rejects
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_id=99999,
                expected_channel_id=self.forum_cid,
                expected_peer_type=self.peer_type,
                expected_account_id=self.account_id
            ))
        self.assertIn("do not belong to verified forum supergroup", str(ctx.exception))

        # D: Mismatched channel ID with singular parameter rejects
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(self.engine.run_batch(
                dialogs,
                target_topic_id=150,
                expected_channel_id=123456789,
                expected_peer_type=self.peer_type,
                expected_account_id=self.account_id
            ))
        self.assertIn("does not match expected channel ID", str(ctx.exception))

    # 15. Forwarding bounded limits: allowed_message_ids, max_completed_files, max_total_bytes
    def test_15_forwarding_bounded_limits(self):
        dialogs = [{
            "id": self.forum_cid,
            "name": "Premium 2027",
            "peer_type": self.peer_type,
            "entity": self.forum_entity
        }]
        asyncio.run(self.engine.run_batch(
            dialogs,
            target_topic_ids={150},
            expected_channel_id=self.forum_cid,
            expected_peer_type=self.peer_type,
            expected_account_id=self.account_id,
            max_completed_files=2,
            allowed_message_ids=[1613, 1607],
            max_total_bytes=15000000
        ))
        self.assertEqual(self.engine.scan_and_download_channel.call_count, 1)
        kwargs = self.engine.scan_and_download_channel.call_args[1]
        self.assertEqual(kwargs["max_completed_files"], 2)
        self.assertEqual(kwargs["allowed_message_ids"], [1613, 1607])
        self.assertEqual(kwargs["max_total_bytes"], 15000000)


if __name__ == "__main__":
    unittest.main()
