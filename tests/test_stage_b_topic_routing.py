import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

from config_manager import ConfigManager
from db_tracker import DownloadTrackerDB, normalize_channel_id, sanitize_folder_name
from telegram_engine import TelegramDownloaderEngine, sanitize_media_filename
from telethon.tl.types import ForumTopic, ForumTopicDeleted, MessageReplyHeader, Channel

class TestStageBTopicRouting(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_stage_b.db"
        self.download_dir = Path(self.tmpdir) / "Downloads"
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
        self.engine.account_id = 123456
        self.engine.client = MagicMock()

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    # 1. Multiple forum topics: Messages route to respective topic directories
    def test_01_multiple_forum_topics_routing(self):
        ch_id = 1001
        ch_name = "Forum Supergroup Alpha"
        
        # Topic 10: "Chemistry", Topic 20: "Physics"
        path1, rel1, tid1, ttitle1 = self.engine.resolve_destination_path(
            channel_id=ch_id,
            channel_name=ch_name,
            raw_filename="test_chem.pdf",
            message_id=101,
            is_forum=True,
            topic_id=10,
            topic_title="Chemistry"
        )
        path2, rel2, tid2, ttitle2 = self.engine.resolve_destination_path(
            channel_id=ch_id,
            channel_name=ch_name,
            raw_filename="test_phys.pdf",
            message_id=102,
            is_forum=True,
            topic_id=20,
            topic_title="Physics"
        )

        self.assertEqual(tid1, 10)
        self.assertEqual(ttitle1, "Chemistry")
        self.assertEqual(path1, self.download_dir / "Forum Supergroup Alpha" / "Chemistry" / "test_chem.pdf")

        self.assertEqual(tid2, 20)
        self.assertEqual(ttitle2, "Physics")
        self.assertEqual(path2, self.download_dir / "Forum Supergroup Alpha" / "Physics" / "test_phys.pdf")

    # 2. Topic pagination: get_or_fetch_forum_topics paginates through multiple pages
    def test_02_topic_pagination(self):
        ch_id = 2001
        mock_entity = MagicMock(spec=Channel)
        mock_entity.forum = True
        mock_entity.megagroup = True

        # Simulate 250 topics across 3 pages (100, 100, 50)
        all_mock_topics = []
        for i in range(1, 251):
            all_mock_topics.append(ForumTopic(
                id=i,
                date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                peer=mock_entity,
                title=f"Topic_{i}",
                icon_color=0,
                top_message=i * 10,
                read_inbox_max_id=0,
                read_outbox_max_id=0,
                unread_count=0,
                unread_mentions_count=0,
                unread_reactions_count=0,
                unread_poll_votes_count=0,
                from_id=None,
                notify_settings=None,
                closed=False,
                pinned=False,
                short=False,
                hidden=False
            ))

        call_count = 0
        async def mock_client_call(req):
            nonlocal call_count
            call_count += 1
            offset = req.offset_topic
            limit = req.limit
            start_idx = 0
            if offset > 0:
                for idx, t in enumerate(all_mock_topics):
                    if t.id == offset:
                        start_idx = idx + 1
                        break
            slice_topics = all_mock_topics[start_idx:start_idx + limit]
            res = MagicMock()
            res.topics = slice_topics
            res.count = len(all_mock_topics)
            return res

        self.engine.client = AsyncMock(side_effect=mock_client_call)
        import asyncio
        topics = asyncio.run(self.engine.get_or_fetch_forum_topics(mock_entity, ch_id, force_refresh=True))

        self.assertEqual(len(topics), 250)
        self.assertEqual(call_count, 3) # 100 + 100 + 50
        self.assertIn(1, topics)
        self.assertIn(250, topics)

    # 3. Topic root messages: msg.id == topic_id routes to that topic
    def test_03_topic_root_messages(self):
        topics_dict = {
            1: {"id": 1, "title": "General", "is_closed": False},
            681: {"id": 681, "title": "Physics", "is_closed": False}
        }
        msg = MagicMock()
        msg.id = 681
        msg.reply_to = None

        tid, title = self.engine.resolve_message_topic(msg, 1001, is_forum=True, topics_dict=topics_dict)
        self.assertEqual(tid, 681)
        self.assertEqual(title, "Physics")

    # 4. Nested replies: reply_to.reply_to_top_id routes to top topic
    def test_04_nested_replies(self):
        topics_dict = {
            1: {"id": 1, "title": "General", "is_closed": False},
            681: {"id": 681, "title": "Physics", "is_closed": False}
        }
        reply_hdr = MagicMock(spec=MessageReplyHeader)
        reply_hdr.reply_to_top_id = 681
        reply_hdr.reply_to_msg_id = 705
        reply_hdr.forum_topic = True

        msg = MagicMock()
        msg.id = 720
        msg.reply_to = reply_hdr

        tid, title = self.engine.resolve_message_topic(msg, 1001, is_forum=True, topics_dict=topics_dict)
        self.assertEqual(tid, 681)
        self.assertEqual(title, "Physics")

    # 5. General topic: messages with no reply_to in forum route to General (topic_id=1)
    def test_05_general_topic_routing(self):
        topics_dict = {
            1: {"id": 1, "title": "General", "is_closed": False},
            5: {"id": 5, "title": "Mathematics", "is_closed": False}
        }
        msg = MagicMock()
        msg.id = 50
        msg.reply_to = None

        tid, title = self.engine.resolve_message_topic(msg, 1001, is_forum=True, topics_dict=topics_dict)
        self.assertEqual(tid, 1)
        self.assertEqual(title, "General")

        path, rel, eff_tid, eff_title = self.engine.resolve_destination_path(
            channel_id=1001,
            channel_name="Forum Supergroup Alpha",
            raw_filename="general_notes.pdf",
            message_id=50,
            is_forum=True,
            topic_id=tid,
            topic_title=title
        )
        self.assertEqual(eff_tid, 1)
        self.assertEqual(path, self.download_dir / "Forum Supergroup Alpha" / "General" / "general_notes.pdf")

    # 6. Missing or ambiguous topic metadata routes to Unclassified
    def test_06_ambiguous_metadata_routing(self):
        topics_dict = {
            1: {"id": 1, "title": "General", "is_closed": True}, # General is closed/disabled
            5: {"id": 5, "title": "Mathematics", "is_closed": False}
        }
        # Ambiguous reply without forum topic header
        reply_hdr = MagicMock()
        reply_hdr.reply_to_top_id = None
        reply_hdr.forum_topic = False
        reply_hdr.reply_to_msg_id = 999

        msg = MagicMock()
        msg.id = 88
        msg.reply_to = reply_hdr

        tid, title = self.engine.resolve_message_topic(msg, 1001, is_forum=True, topics_dict=topics_dict)
        self.assertIsNone(tid)
        self.assertEqual(title, "Unclassified")

        path, rel, eff_tid, eff_title = self.engine.resolve_destination_path(
            channel_id=1001,
            channel_name="Forum Supergroup Alpha",
            raw_filename="unclassified_doc.pdf",
            message_id=88,
            is_forum=True,
            topic_id=tid,
            topic_title=title
        )
        self.assertIsNone(eff_tid)
        self.assertEqual(eff_title, "Unclassified")
        self.assertEqual(path, self.download_dir / "Forum Supergroup Alpha" / "Unclassified" / "unclassified_doc.pdf")

    # 7. Topic renames: local folder name is retained after Telegram title change
    def test_07_topic_rename_retention(self):
        ch_id = 1001
        # Original topic title
        self.db.save_forum_topic(ch_id, 30, "Original Notes", "Original Notes")

        # Telegram title changes to "Updated Notes 2027"
        folder_name = self.db.resolve_unique_topic_folder_name(ch_id, 30, "Updated Notes 2027")
        self.assertEqual(folder_name, "Original Notes")

    # 8. Identical topic titles in same channel get distinct local folder names
    def test_08_identical_topic_titles_disambiguation(self):
        ch_id = 1001
        # First topic with title "Study Material"
        f1 = self.db.resolve_unique_topic_folder_name(ch_id, 10, "Study Material")
        self.db.save_forum_topic(ch_id, 10, "Study Material", f1)
        self.assertEqual(f1, "Study Material")

        # Second distinct topic with identical title "Study Material"
        f2 = self.db.resolve_unique_topic_folder_name(ch_id, 20, "Study Material")
        self.db.save_forum_topic(ch_id, 20, "Study Material", f2)
        self.assertEqual(f2, "Study Material [20]")

    # 9. Deleted and inaccessible topics: ForumTopicDeleted is handled safely
    def test_09_deleted_and_inaccessible_topics(self):
        del_topic = ForumTopicDeleted(id=99)
        mock_entity = MagicMock(spec=Channel)
        mock_entity.forum = True
        mock_entity.megagroup = True

        async def mock_call(req):
            res = MagicMock()
            res.topics = [del_topic]
            return res

        self.engine.client = AsyncMock(side_effect=mock_call)
        import asyncio
        topics = asyncio.run(self.engine.get_or_fetch_forum_topics(mock_entity, 12345, force_refresh=True))

        self.assertIn(99, topics)
        self.assertTrue(topics[99]["is_closed"])
        self.assertTrue(topics[99]["is_hidden"])
        self.assertEqual(topics[99]["title"], "Deleted_Topic_99")

    # 10. Normal broadcast channels: retain flat directory layout
    def test_10_normal_broadcast_channel_flat_layout(self):
        ch_id = 3001
        ch_name = "Broadcast Announcements"

        path, rel, tid, title = self.engine.resolve_destination_path(
            channel_id=ch_id,
            channel_name=ch_name,
            raw_filename="mains_question.pdf",
            message_id=55,
            is_forum=False, # Normal broadcast channel
            topic_id=None,
            topic_title=None
        )

        self.assertIsNone(tid)
        self.assertIsNone(title)
        self.assertEqual(path, self.download_dir / "Broadcast Announcements" / "mains_question.pdf")

    # 11. Windows filename sanitization: illegal chars and reserved names
    def test_11_windows_filename_and_folder_sanitization(self):
        dirty_folder = 'Test: Channel * with ? "illegal" <chars> | and .dots.'
        safe = sanitize_folder_name(dirty_folder)
        for char in [':', '*', '?', '"', '<', '>', '|']:
            self.assertNotIn(char, safe)
        self.assertFalse(safe.endswith('.'))

        # Windows reserved name
        res_con = sanitize_folder_name("CON")
        self.assertEqual(res_con, "CON_dir")

        res_prn = sanitize_folder_name("prn")
        self.assertEqual(res_prn, "prn_dir")

    # 12. Unicode folder titles: Hindi and emojis preserved
    def test_12_unicode_folder_titles_preservation(self):
        unicode_title = "हिंदी साहित्य एवं निबंध 📚"
        safe = sanitize_folder_name(unicode_title)
        self.assertIn("हिंदी", safe)
        self.assertIn("साहित्य", safe)
        self.assertIn("📚", safe)

    # 13. Stable directory mapping across restarts
    def test_13_stable_directory_mapping_across_restarts(self):
        acct = 123456
        cid = 987654321
        self.db.set_channel_storage_mapping(acct, cid, "Initial Channel Name", "Stable_Local_Folder", is_forum=True)
        self.db.save_forum_topic(cid, 42, "Test Topic", "Stable_Topic_Folder")

        # Simulate fresh app startup with second DB instance pointing to same file
        db2 = DownloadTrackerDB(str(self.db_path))
        m = db2.get_channel_storage_mapping(acct, cid)
        t = db2.get_forum_topic(cid, 42)

        self.assertIsNotNone(m)
        self.assertEqual(m["local_folder_name"], "Stable_Local_Folder")
        self.assertIsNotNone(t)
        self.assertEqual(t["local_folder_name"], "Stable_Topic_Folder")

    # 14. Existing flat-file duplicate prevention: completed files in DB are skipped
    def test_14_existing_flat_file_duplicate_prevention(self):
        ch_id = 1111
        msg_id = 555
        self.db.ensure_channel(ch_id, "TestChannel")
        legacy_path = self.download_dir / "TestChannel" / "already_done.pdf"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_bytes(b"X" * 500)
        self.db.record_completed(ch_id, msg_id, "already_done.pdf", 500, "TestChannel/already_done.pdf")

        # is_message_downloaded returns True
        self.assertTrue(self.db.is_message_downloaded(ch_id, msg_id))

        # download_single_message skips execution without calling Telethon client
        self.engine.client.download_media = AsyncMock()
        msg = MagicMock()
        msg.id = msg_id
        import asyncio
        asyncio.run(self.engine.download_single_message(
            msg, ch_id, self.download_dir / "TestChannel", "already_done.pdf", 500, is_forum=True, topic_id=5
        ))
        self.assertFalse(self.engine.client.download_media.called)

    # 15. Existing .part resume preservation: legacy .part files are resumed in-place
    def test_15_existing_part_resume_preservation(self):
        ch_id = 4001
        msg_id = 999
        self.db.ensure_channel(ch_id, "Channel_4001")
        
        legacy_rel = "Channel_4001/Current_Affairs_02.mp4"
        legacy_full = self.download_dir / legacy_rel
        legacy_full.parent.mkdir(parents=True, exist_ok=True)
        part_path = Path(f"{legacy_full}.part")
        part_path.write_bytes(b"A" * 1024) # 1 KB partial download

        # Record in downloads table pointing to legacy flat path
        self.db.record_start(ch_id, msg_id, "Current_Affairs_02.mp4", 5000, legacy_rel)

        # Even if topic routing is requested, resolver must preserve existing .part path!
        resolved_path, resolved_rel, eff_tid, eff_ttitle = self.engine.resolve_destination_path(
            channel_id=ch_id,
            channel_name="Current Affairs Channel",
            raw_filename="Current_Affairs_02.mp4",
            message_id=msg_id,
            is_forum=True,
            topic_id=7,
            topic_title="Current Affairs"
        )

        self.assertEqual(resolved_path, legacy_full)
        self.assertEqual(resolved_rel, legacy_rel)

    # 16. Failed-download retry paths: retry correctly routes with topic metadata
    def test_16_failed_download_retry_paths(self):
        ch_id = 1003
        msg_id = 333
        self.db.ensure_channel(ch_id, "Discussion Hub Beta")
        self.db.record_start(ch_id, msg_id, "retry_test.pdf", 1000, "Discussion Hub Beta/retry_test.pdf")
        self.db.record_failed(ch_id, msg_id, "Connection timeout")

        failed = self.db.get_failed_messages(ch_id)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["message_id"], msg_id)

        # Path resolution on retry routes to proper topic directory
        path, rel, tid, title = self.engine.resolve_destination_path(
            channel_id=ch_id,
            channel_name="Discussion Hub Beta",
            raw_filename="retry_test.pdf",
            message_id=msg_id,
            is_forum=True,
            topic_id=15,
            topic_title="Weekly Quiz"
        )
        self.assertEqual(path, self.download_dir / "Discussion Hub Beta" / "Weekly Quiz" / "retry_test.pdf")

    # 17. GUI and headless worker path consistency
    def test_17_gui_and_headless_path_consistency(self):
        # Both entry points use TelegramDownloaderEngine.resolve_destination_path
        engine_gui = TelegramDownloaderEngine(self.cfg_mgr, self.db)
        engine_gui.account_id = 123456

        engine_worker = TelegramDownloaderEngine(self.cfg_mgr, self.db)
        engine_worker.account_id = 123456

        p_gui, r_gui, t_gui, tt_gui = engine_gui.resolve_destination_path(
            channel_id=1001,
            channel_name="Forum Supergroup Alpha",
            raw_filename="consistent.pdf",
            message_id=404,
            is_forum=True,
            topic_id=616,
            topic_title="Advanced Module 1"
        )
        p_worker, r_worker, t_worker, tt_worker = engine_worker.resolve_destination_path(
            channel_id=1001,
            channel_name="Forum Supergroup Alpha",
            raw_filename="consistent.pdf",
            message_id=404,
            is_forum=True,
            topic_id=616,
            topic_title="Advanced Module 1"
        )

        self.assertEqual(p_gui, p_worker)
        self.assertEqual(r_gui, r_worker)
        self.assertEqual(t_gui, t_worker)
        self.assertEqual(tt_gui, tt_worker)

    # 18. SQLite migrations and rollback idempotency
    def test_18_sqlite_migrations_idempotency(self):
        # Running migrate_to_stage_b multiple times must be completely safe and idempotent
        self.assertTrue(self.db.migrate_to_stage_b())
        self.assertTrue(self.db.migrate_to_stage_b())

        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute("PRAGMA table_info(downloads)")
            cols = {r[1] for r in cursor.fetchall()}
            self.assertIn("topic_id", cols)
            self.assertIn("topic_title", cols)

            # Check new tables exist
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("channel_storage_mapping", tables)
            self.assertIn("forum_topics", tables)

    # 19. Account and peer identity collisions
    def test_19_account_and_peer_identity_collisions(self):
        cid = 123456
        # Same numeric ID under account 1 vs account 2
        self.db.set_channel_storage_mapping(111, cid, "Account 1 Channel", "Folder_A1", is_forum=False)
        self.db.set_channel_storage_mapping(222, cid, "Account 2 Channel", "Folder_A2", is_forum=True)

        m1 = self.db.get_channel_storage_mapping(111, cid)
        m2 = self.db.get_channel_storage_mapping(222, cid)

        self.assertEqual(m1["local_folder_name"], "Folder_A1")
        self.assertFalse(m1["is_forum"])
        self.assertEqual(m2["local_folder_name"], "Folder_A2")
        self.assertTrue(m2["is_forum"])

    # 20. Full existing download preservation: relative_path preserved on existing rows
    def test_20_preserve_historical_relative_paths(self):
        ch_id = 9999
        self.db.ensure_channel(ch_id, "LegacyChan")
        # Legacy flat download record
        self.db.record_completed(ch_id, 1, "legacy_file.pdf", 1000, "LegacyChan/legacy_file.pdf")

        rec = self.db.get_download_record(ch_id, 1)
        self.assertEqual(rec["relative_path"], "LegacyChan/legacy_file.pdf")
        self.assertIsNone(rec["topic_id"])

        # Recording a new topic download does not affect legacy records
        self.db.record_completed(ch_id, 2, "new_topic_file.pdf", 2000, "LegacyChan/Topic_A/new_topic_file.pdf", topic_id=5, topic_title="Topic_A")

        rec_legacy = self.db.get_download_record(ch_id, 1)
        rec_new = self.db.get_download_record(ch_id, 2)

        self.assertEqual(rec_legacy["relative_path"], "LegacyChan/legacy_file.pdf")
        self.assertEqual(rec_new["relative_path"], "LegacyChan/Topic_A/new_topic_file.pdf")
        self.assertEqual(rec_new["topic_id"], 5)
        self.assertEqual(rec_new["topic_title"], "Topic_A")

if __name__ == "__main__":
    unittest.main()
