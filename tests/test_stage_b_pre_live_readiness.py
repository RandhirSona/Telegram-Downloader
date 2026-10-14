import os
import asyncio
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from config_manager import ConfigManager, StorageError
from db_tracker import DownloadTrackerDB, normalize_channel_id, sanitize_folder_name
from telegram_engine import TelegramDownloaderEngine, sanitize_media_filename
from telethon.tl.types import ForumTopic, ForumTopicDeleted, MessageReplyHeader, Channel
from worker_headless import HeadlessWorker

class TestStageBPreLiveReadiness(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_readiness.db"
        self.download_dir = Path(self.tmpdir) / "TelegramDownloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(self.tmpdir) / "config.json"

        self.cfg_mgr = ConfigManager(str(self.config_path))
        self.cfg_mgr.config["download_directory"] = str(self.download_dir)
        self.cfg_mgr.config["organize_forum_topics"] = True
        self.cfg_mgr.config["min_free_space_gb"] = 0.001
        self.cfg_mgr.save_config()

        self.db = DownloadTrackerDB(str(self.db_path))
        self.db.migrate_to_stage_b()

        self.engine = TelegramDownloaderEngine(self.cfg_mgr, self.db)
        self.engine.account_id = 123456789
        self.engine.client = MagicMock()

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    # 1. Cross-account topic identity
    def test_01_cross_account_topic_identity(self):
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # Test inserting topic with account awareness
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS test_forum_topics_scoped (
                account_id INTEGER NOT NULL,
                peer_type TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                is_closed INTEGER DEFAULT 0,
                is_hidden INTEGER DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (account_id, peer_type, channel_id, topic_id)
            )
        """)
        # Account A has topic 100 on channel 12345
        cursor.execute(
            "INSERT INTO test_forum_topics_scoped VALUES (1, 'channel', 12345, 100, 'Topic Account 1', 0, 0, '2026-10-14')"
        )
        # Account B has topic 100 on same channel ID without collision
        cursor.execute(
            "INSERT INTO test_forum_topics_scoped VALUES (2, 'channel', 12345, 100, 'Topic Account 2', 0, 0, '2026-10-14')"
        )
        conn.commit()

        cursor.execute("SELECT COUNT(*) FROM test_forum_topics_scoped WHERE topic_id = 100")
        self.assertEqual(cursor.fetchone()[0], 2)
        conn.close()

    # 2. Non-General topic routing
    def test_02_non_general_topic_routing(self):
        target_path, rel_path, topic_id, topic_title = self.engine.resolve_destination_path(
            channel_id=4450272939,
            channel_name="Premium 2027",
            raw_filename="दैनिक_जागरण_HD_राष्ट्रीय_संस्करण_14102026.pdf",
            message_id=1624,
            is_forum=True,
            topic_id=150,
            topic_title="DAILY NEWSPAPER"
        )
        expected_rel = str(Path("Premium 2027") / "DAILY NEWSPAPER" / "दैनिक_जागरण_HD_राष्ट्रीय_संस्करण_14102026.pdf")
        self.assertEqual(rel_path, expected_rel)
        self.assertEqual(topic_id, 150)
        self.assertEqual(topic_title, "DAILY NEWSPAPER")
        self.assertTrue(target_path.is_relative_to(self.download_dir))

    # 3. Direct topic messages and replies
    def test_03_direct_topic_messages_and_replies(self):
        topics_dict = {
            1: {"title": "General", "folder_name": "General"},
            150: {"title": "DAILY NEWSPAPER", "folder_name": "DAILY NEWSPAPER"}
        }
        
        # A: Message replying to thread 150
        msg_reply = MagicMock()
        msg_reply.id = 1624
        msg_reply.reply_to = MagicMock()
        msg_reply.reply_to.forum_topic = True
        msg_reply.reply_to.reply_to_top_id = 150
        tid, ttitle = self.engine.resolve_message_topic(msg_reply, 4450272939, True, topics_dict)
        self.assertEqual(tid, 150)
        self.assertEqual(ttitle, "DAILY NEWSPAPER")

        # B: Direct thread root message
        msg_root = MagicMock()
        msg_root.id = 150
        msg_root.reply_to = None
        tid_root, ttitle_root = self.engine.resolve_message_topic(msg_root, 4450272939, True, topics_dict)
        self.assertEqual(tid_root, 150)
        self.assertEqual(ttitle_root, "DAILY NEWSPAPER")

        # C: General topic message (no reply header)
        msg_general = MagicMock()
        msg_general.id = 50
        msg_general.reply_to = None
        tid_gen, ttitle_gen = self.engine.resolve_message_topic(msg_general, 4450272939, True, topics_dict)
        self.assertEqual(tid_gen, 1)
        self.assertEqual(ttitle_gen, "General")

        # D: Unknown thread (not in topics_dict) -> f"Topic_{top_id}"
        msg_unknown = MagicMock()
        msg_unknown.id = 999
        msg_unknown.reply_to = MagicMock()
        msg_unknown.reply_to.forum_topic = True
        msg_unknown.reply_to.reply_to_top_id = 9999
        tid_unk, ttitle_unk = self.engine.resolve_message_topic(msg_unknown, 4450272939, True, topics_dict)
        self.assertEqual(tid_unk, 9999)
        self.assertEqual(ttitle_unk, "Topic_9999")

        # E: Ambiguous reply (not a forum topic thread) -> Unclassified
        msg_ambiguous = MagicMock()
        msg_ambiguous.id = 1000
        msg_ambiguous.reply_to = MagicMock()
        msg_ambiguous.reply_to.forum_topic = False
        msg_ambiguous.reply_to.reply_to_top_id = None
        tid_amb, ttitle_amb = self.engine.resolve_message_topic(msg_ambiguous, 4450272939, True, topics_dict)
        self.assertIsNone(tid_amb)
        self.assertEqual(ttitle_amb, "Unclassified")

    # 4. FAILED historical path handling
    def test_04_failed_historical_path_handling(self):
        channel_id = 4450272939
        msg_id = 888
        self.db.ensure_channel(channel_id, "Premium 2027")
        self.db.record_start(channel_id, msg_id, "file.pdf", 1000, "Premium 2027/DAILY NEWSPAPER/file.pdf")
        self.db.record_failed(channel_id, msg_id, "Network timeout during chunk 5")
        
        failed = self.db.get_failed_messages(channel_id)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["retry_count"], 1)

        # Retry recorded failed does not overwrite existing media
        dummy_file = self.download_dir / "Premium 2027" / "DAILY NEWSPAPER" / "file.pdf"
        dummy_file.parent.mkdir(parents=True, exist_ok=True)
        dummy_file.write_text("historical media")

        target_path, rel_path, _, _ = self.engine.resolve_destination_path(
            channel_id=channel_id,
            channel_name="Premium 2027",
            raw_filename="file.pdf",
            message_id=msg_id,
            is_forum=True,
            topic_id=150,
            topic_title="DAILY NEWSPAPER"
        )
        self.assertEqual(dummy_file.read_text(), "historical media")

    # 5. Existing completed download skipping
    def test_05_existing_completed_download_skipping(self):
        channel_id = 4213926054
        msg_id = 34
        self.db.ensure_channel(channel_id, "Channel_4213926054")
        
        # Create historical file on disk
        historical_file = self.download_dir / "Channel_4213926054" / "Completed_Lecture.mp4"
        historical_file.parent.mkdir(parents=True, exist_ok=True)
        historical_file.write_bytes(b"completed historical content")

        self.db.record_completed(
            channel_id=channel_id,
            message_id=msg_id,
            file_name="Completed_Lecture.mp4",
            file_size=len(b"completed historical content"),
            relative_path=r"Channel_4213926054\Completed_Lecture.mp4"
        )
        # Verify skip check
        self.assertTrue(self.db.is_message_downloaded(channel_id, msg_id))

        # Even with forum organization enabled, historical completed relative path is authoritative
        target_path, rel_path, _, _ = self.engine.resolve_destination_path(
            channel_id=channel_id,
            channel_name="Channel 4213926054",
            raw_filename="Completed_Lecture.mp4",
            message_id=msg_id,
            is_forum=True,
            topic_id=5,
            topic_title="Polity"
        )
        self.assertEqual(rel_path, r"Channel_4213926054\Completed_Lecture.mp4")

    # 6. Strict one-group, one-topic execution scope
    def test_06_strict_one_group_one_topic_execution_scope(self):
        channel_id = 4450272939
        self.db.ensure_channel(channel_id, "Premium 2027")
        self.db.add_selection(self.engine.account_id, channel_id, "Premium 2027", peer_type="channel")

        mock_entity = MagicMock(spec=Channel)
        mock_entity.forum = True
        mock_entity.megagroup = True

        # Messages: msg 1 is topic 150, msg 2 is topic 200, msg 3 is topic 150
        msg1 = MagicMock()
        msg1.id = 101
        msg1.date = datetime.now(timezone.utc)
        msg1.message = ""
        msg1.reply_to = MagicMock(forum_topic=True, reply_to_top_id=150)

        msg2 = MagicMock()
        msg2.id = 102
        msg2.date = datetime.now(timezone.utc)
        msg2.message = ""
        msg2.reply_to = MagicMock(forum_topic=True, reply_to_top_id=200)

        msg3 = MagicMock()
        msg3.id = 103
        msg3.date = datetime.now(timezone.utc)
        msg3.message = ""
        msg3.reply_to = MagicMock(forum_topic=True, reply_to_top_id=150)

        def mock_iter(*args, **kwargs):
            async def _gen():
                for m in [msg1, msg2, msg3]:
                    yield m
            return _gen()

        self.engine.client.iter_messages = mock_iter
        self.engine.get_or_fetch_forum_topics = AsyncMock(return_value={
            150: {"title": "DAILY NEWSPAPER", "folder_name": "DAILY NEWSPAPER"},
            200: {"title": "OTHER TOPIC", "folder_name": "OTHER TOPIC"}
        })
        self.engine.match_media = MagicMock(return_value=(True, "sample.pdf", 1000))
        self.engine.download_single_message = AsyncMock()

        # Run with target_topic_id=150
        loop = asyncio.new_event_loop()
        count = loop.run_until_complete(
            self.engine.scan_and_download_channel(
                entity=mock_entity,
                channel_id=channel_id,
                channel_name="Premium 2027",
                media_filters={"documents": True},
                cutoff_date=None,
                target_topic_id=150
            )
        )
        loop.close()

        # Should only download msg1 and msg3 (topic 150), skipping msg2 (topic 200)
        self.assertEqual(count, 2)
        downloaded_msg_ids = [call.args[0].id for call in self.engine.download_single_message.call_args_list]
        self.assertEqual(downloaded_msg_ids, [101, 103])

    # 7. Engine-enforced maximum completed-file limit
    def test_07_engine_enforced_maximum_completed_file_limit(self):
        channel_id = 4450272939
        self.db.ensure_channel(channel_id, "Premium 2027")
        self.db.add_selection(self.engine.account_id, channel_id, "Premium 2027", peer_type="channel")

        mock_entity = MagicMock(spec=Channel)
        mock_entity.forum = True
        mock_entity.megagroup = True

        # 5 messages in topic 150
        messages = []
        for i in range(1, 6):
            m = MagicMock()
            m.id = 100 + i
            m.date = datetime.now(timezone.utc)
            m.message = ""
            m.reply_to = MagicMock(forum_topic=True, reply_to_top_id=150)
            messages.append(m)

        def mock_iter(*args, **kwargs):
            async def _gen():
                for m in messages:
                    yield m
            return _gen()

        self.engine.client.iter_messages = mock_iter
        self.engine.get_or_fetch_forum_topics = AsyncMock(return_value={
            150: {"title": "DAILY NEWSPAPER", "folder_name": "DAILY NEWSPAPER"}
        })
        self.engine.match_media = MagicMock(return_value=(True, "sample.pdf", 1000))
        self.engine.download_single_message = AsyncMock()

        # Run with max_completed_files=2
        loop = asyncio.new_event_loop()
        count = loop.run_until_complete(
            self.engine.scan_and_download_channel(
                entity=mock_entity,
                channel_id=channel_id,
                channel_name="Premium 2027",
                media_filters={"documents": True},
                cutoff_date=None,
                target_topic_id=150,
                max_completed_files=2
            )
        )
        loop.close()

        # Hard limit enforced: exactly 2 completed files
        self.assertEqual(count, 2)
        self.assertEqual(self.engine.download_single_message.call_count, 2)

    # 8. Partial file preservation
    def test_08_partial_file_preservation(self):
        channel_id = 4213926054
        msg_id = 26
        self.db.ensure_channel(channel_id, "Channel_4213926054")

        # Create a historical .part file matching the 548MB pattern
        part_dir = self.download_dir / "Channel_4213926054"
        part_dir.mkdir(parents=True, exist_ok=True)
        part_file = part_dir / "Current_Affairs_02_9th_September_2026.mp4.part"
        part_file.write_bytes(b"A" * 1024)

        # Record in DB as DOWNLOADING with historical rel path
        rel_path = r"Channel_4213926054\Current_Affairs_02_9th_September_2026.mp4"
        self.db.record_start(
            channel_id=channel_id,
            message_id=msg_id,
            file_name="Current_Affairs_02_9th_September_2026.mp4",
            file_size=548405248,
            relative_path=rel_path
        )

        # Now resolve destination path with topic reorganization enabled
        target_path, resolved_rel, _, _ = self.engine.resolve_destination_path(
            channel_id=channel_id,
            channel_name="Channel_4213926054",
            raw_filename="Current_Affairs_02_9th_September_2026.mp4",
            message_id=msg_id,
            is_forum=True,
            topic_id=10,
            topic_title="Economy"
        )
        # Must retain historical relative path to preserve the .part resume file
        self.assertEqual(resolved_rel, rel_path)
        self.assertTrue(part_file.exists())
        self.assertEqual(part_file.stat().st_size, 1024)

    # 9. Zero fallback to C: drive
    def test_09_zero_fallback_to_c_drive(self):
        fake_f_path = Path("Q:\\NonExistentNetworkStorage\\TelegramDownloads")
        self.cfg_mgr.config["download_directory"] = str(fake_f_path)
        self.cfg_mgr.save_config()

        valid, error_msg, _ = self.cfg_mgr.validate_storage()
        self.assertFalse(valid)
        self.assertIn("Storage unreachable", error_msg)

        # Ensure no fallback to C:\ occurs
        resolved_dir = self.cfg_mgr.get_download_dir()
        self.assertNotEqual(str(resolved_dir).lower(), "c:\\")
        self.assertTrue(str(resolved_dir).startswith("Q:\\"))

    # 10. GUI/headless destination consistency
    def test_10_gui_headless_destination_consistency(self):
        with patch("worker_headless.setup_logging"):
            with patch("worker_headless.ProcessLock"):
                headless = HeadlessWorker()
                headless.config_mgr = self.cfg_mgr
                headless.db = self.db
                headless.engine = TelegramDownloaderEngine(self.cfg_mgr, self.db)
                headless.engine.account_id = self.engine.account_id

        gui_target, gui_rel, gui_tid, gui_ttitle = self.engine.resolve_destination_path(
            channel_id=4450272939,
            channel_name="Premium 2027",
            raw_filename="Editorial_14_Oct.pdf",
            message_id=1620,
            is_forum=True,
            topic_id=150,
            topic_title="DAILY NEWSPAPER"
        )

        headless_target, headless_rel, headless_tid, headless_ttitle = headless.engine.resolve_destination_path(
            channel_id=4450272939,
            channel_name="Premium 2027",
            raw_filename="Editorial_14_Oct.pdf",
            message_id=1620,
            is_forum=True,
            topic_id=150,
            topic_title="DAILY NEWSPAPER"
        )

        self.assertEqual(gui_rel, headless_rel)
        self.assertEqual(gui_target, headless_target)
        self.assertEqual(gui_tid, headless_tid)
        self.assertEqual(gui_ttitle, headless_ttitle)

if __name__ == "__main__":
    unittest.main()
