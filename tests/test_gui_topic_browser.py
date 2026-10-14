import unittest
import asyncio
import tempfile
import shutil
import os
import sqlite3
import tkinter as tk
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

from db_tracker import DownloadTrackerDB
from config_manager import ConfigManager
from telegram_engine import TelegramDownloaderEngine, normalize_channel_id, CancelledByUser
from telegram_gui_downloader import TelegramGUIApp


from telethon.tl.types import MessageMediaDocument, Document


def make_mock_media_doc(size=1024, mime_type="application/pdf"):
    doc = Document(id=1, access_hash=2, file_reference=b'1', date=None, mime_type=mime_type, size=size, dc_id=1, attributes=[])
    return MessageMediaDocument(document=doc)


class MockMessage:
    def __init__(self, msg_id, date=None, media=None, reply_to=None):
        self.id = msg_id
        self.date = date
        self.media = media
        self.reply_to = reply_to
        self.message = "test"


class MockReplyHeader:
    def __init__(self, reply_to_top_id=None, reply_to_msg_id=None, forum_topic=False):
        self.reply_to_top_id = reply_to_top_id
        self.reply_to_msg_id = reply_to_msg_id
        self.forum_topic = forum_topic


class TestGUITopicBrowser(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_gui_topic_browser_")
        self.db_path = os.path.join(self.test_dir, "test_tracker.db")
        self.download_dir = os.path.join(self.test_dir, "downloads")
        os.makedirs(self.download_dir, exist_ok=True)

        self.cfg = ConfigManager(config_path=os.path.join(self.test_dir, "config.json"))
        self.cfg.config["download_directory"] = self.download_dir
        self.cfg.config["organize_forum_topics"] = True
        self.cfg.save_config()

        self.db = DownloadTrackerDB(db_path=self.db_path)
        self.db.migrate_to_stage_b()

        # Seed test account and forum topics
        self.account_id = 123456789
        self.forum_cid = 4450272939
        self.broadcast_cid = 3061094461
        self.other_forum_cid = 4412184534

        # Add selections to DB
        self.db.add_selection(self.account_id, self.forum_cid, "Premium 2027", "channel")
        self.db.add_selection(self.account_id, self.broadcast_cid, "FORUM PTS 2026", "channel")
        self.db.add_selection(self.account_id, self.other_forum_cid, "UPPCS TEST 2026", "channel")

        # Set storage mappings
        self.db.set_channel_storage_mapping(self.account_id, self.forum_cid, "Premium 2027", "Premium 2027", is_forum=True)
        self.db.set_channel_storage_mapping(self.account_id, self.broadcast_cid, "FORUM PTS 2026", "FORUM PTS 2026", is_forum=False)
        self.db.set_channel_storage_mapping(self.account_id, self.other_forum_cid, "UPPCS TEST 2026", "UPPCS TEST 2026", is_forum=True)

        # Seed forum topics for Premium 2027
        self.db.save_forum_topic(self.forum_cid, 150, "DAILY NEWSPAPER", "DAILY NEWSPAPER", account_id=self.account_id)
        self.db.save_forum_topic(self.forum_cid, 2, "Vision Prelims 2027", "Vision Prelims 2027", account_id=self.account_id)
        self.db.save_forum_topic(self.forum_cid, 5, "Forum Mains 2026", "Forum Mains 2026", account_id=self.account_id, is_closed=1)
        self.db.save_forum_topic(self.forum_cid, 999, "Restricted Admin Topic", "Restricted Admin Topic", account_id=self.account_id, is_hidden=1)

        # Seed forum topics for UPPCS TEST 2026
        self.db.save_forum_topic(self.other_forum_cid, 10, "Test Series Schedule", "Test Series Schedule", account_id=self.account_id)
        self.db.save_forum_topic(self.other_forum_cid, 20, "GS Paper 1", "GS Paper 1", account_id=self.account_id)

        # Tkinter root
        self.root = tk.Tk()
        self.root.withdraw()

        # Patch GUI instance to use test DB, Config, and mocked Engine
        mock_engine_cls = MagicMock()
        mock_engine_instance = MagicMock()
        async def dummy_auth(): return False
        mock_engine_instance.check_auth = dummy_auth
        mock_engine_instance.client = MagicMock()
        mock_engine_cls.return_value = mock_engine_instance

        with patch("telegram_gui_downloader.ConfigManager", return_value=self.cfg), \
             patch("telegram_gui_downloader.DownloadTrackerDB", return_value=self.db), \
             patch("telegram_gui_downloader.ProcessLock.acquire_session_lock", return_value=True), \
             patch("telegram_gui_downloader.TelegramDownloaderEngine", mock_engine_cls):
            self.app = TelegramGUIApp(self.root)
            self.app.db = self.db
            self.app.config_mgr = self.cfg
            self.app.engine = mock_engine_instance
            self.app.get_account_id = MagicMock(return_value=self.account_id)
            self.app.refresh_download_list_tree()

    def tearDown(self):
        try:
            if hasattr(self.app, "_after_id") and self.app._after_id:
                try:
                    self.root.after_cancel(self.app._after_id)
                except Exception:
                    pass
                self.app._after_id = None
            self.app.engine = None
            self.root.destroy()
        except Exception:
            pass
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # 1. Forum group topic inspector displays cached topics
    def test_01_forum_group_topic_inspector(self):
        iid = f"channel_{self.forum_cid}"
        self.app.download_tree.selection_set(iid)
        self.app.on_download_tree_select()

        # Verify topic inspector is populated with 4 topics
        items = self.app.topic_tree.get_children()
        self.assertEqual(len(items), 4)

        # Verify topic values
        vals_150 = self.app.topic_tree.item("topic_150", "values")
        self.assertEqual(vals_150[0], "☐")
        self.assertEqual(str(vals_150[1]), "150")
        self.assertEqual(vals_150[2], "DAILY NEWSPAPER")
        self.assertEqual(vals_150[3], "Available")
        self.assertIn("DAILY NEWSPAPER", vals_150[4])

    # 2. Normal broadcast channel displays flat information and no topics
    def test_02_normal_channel_without_topics(self):
        iid = f"channel_{self.broadcast_cid}"
        self.app.download_tree.selection_set(iid)
        self.app.on_download_tree_select()

        # Verify topic tree is cleared
        items = self.app.topic_tree.get_children()
        self.assertEqual(len(items), 0)
        self.assertIn("No topics exist", self.app.topic_inspector_status_var.get())
        self.assertEqual(str(self.app.btn_select_all_topics.cget("state")), "disabled")
        self.assertEqual(str(self.app.btn_download_topics.cget("state")), "disabled")

    # 3. Account-scoped checkbox identity
    def test_03_account_scoped_checkbox_identity(self):
        # Select topic 150 in Premium 2027
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()
        self.app.toggle_topic_checkbox("topic_150")

        # Verify checked_topics key has (account_id, peer_type, channel_id, topic_id)
        expected_key = (self.account_id, "channel", self.forum_cid, 150)
        self.assertIn(expected_key, self.app.checked_topics)

        # Cross-account check: a different account does not have this checked
        diff_account_key = (9999999, "channel", self.forum_cid, 150)
        self.assertNotIn(diff_account_key, self.app.checked_topics)

    # 4. Switching between forum groups does not transfer checkboxes
    def test_04_switching_between_forum_groups(self):
        # 1. Select group A (Premium 2027), check topic 150
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()
        self.app.toggle_topic_checkbox("topic_150")
        self.assertEqual(self.app.topic_tree.item("topic_150", "values")[0], "☑")

        # 2. Switch to group B (UPPCS TEST 2026)
        self.app.download_tree.selection_set(f"channel_{self.other_forum_cid}")
        self.app.on_download_tree_select()

        # Topics for group B should be unchecked
        for item in self.app.topic_tree.get_children():
            self.assertEqual(self.app.topic_tree.item(item, "values")[0], "☐")

        # 3. Switch back to group A (Premium 2027)
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()

        # Topic 150 must still be checked, topic 2 must be unchecked
        self.assertEqual(self.app.topic_tree.item("topic_150", "values")[0], "☑")
        self.assertEqual(self.app.topic_tree.item("topic_2", "values")[0], "☐")

    # 5. Select All Topics
    def test_05_select_all_topics(self):
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()

        self.app.on_select_all_topics()
        for item in self.app.topic_tree.get_children():
            self.assertEqual(self.app.topic_tree.item(item, "values")[0], "☑")

        # Button should show count
        self.assertIn("Download Selected Topics (4)", self.app.btn_download_topics.cget("text"))

    # 6. Unselect All Topics
    def test_06_unselect_all_topics(self):
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()
        self.app.on_select_all_topics()
        self.app.on_unselect_all_topics()

        for item in self.app.topic_tree.get_children():
            self.assertEqual(self.app.topic_tree.item(item, "values")[0], "☐")
        self.assertEqual(str(self.app.btn_download_topics.cget("state")), "disabled")

    # 7. Empty-selection rejection
    def test_07_empty_selection_rejection(self):
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()
        # No topics checked
        with patch("tkinter.messagebox.showwarning") as mock_warn:
            self.app.on_download_selected_topics()
            mock_warn.assert_called_once()
            self.assertIn("No topics are currently checked", mock_warn.call_args[0][1])

    # 8. Single-topic engine filtering
    def test_08_single_topic_engine_filtering(self):
        engine = TelegramDownloaderEngine(self.cfg, self.db)
        engine.client = MagicMock()

        # Mock iter_messages returning 3 messages with different topic headers
        msg1 = MockMessage(101, reply_to=MockReplyHeader(reply_to_top_id=150))
        msg2 = MockMessage(102, reply_to=MockReplyHeader(reply_to_top_id=2))
        msg3 = MockMessage(103, reply_to=MockReplyHeader(reply_to_top_id=150))

        async def mock_iter(*args, **kwargs):
            for m in [msg1, msg2, msg3]:
                yield m

        engine.client.iter_messages = mock_iter
        engine.match_media = MagicMock(return_value=(True, "test.pdf", 1024))
        engine.download_single_message = AsyncMock()

        entity = MagicMock(forum=True, megagroup=True)
        # Filter for topic 150 only
        completed = asyncio.run(engine.scan_and_download_channel(
            entity, self.forum_cid, "Premium 2027",
            media_filters={"PDFs": True}, cutoff_date=None,
            target_topic_ids={150}
        ))

        # Only msg1 and msg3 should be downloaded
        self.assertEqual(engine.download_single_message.call_count, 2)
        downloaded_msg_ids = [call[0][0].id for call in engine.download_single_message.call_args_list]
        self.assertEqual(downloaded_msg_ids, [101, 103])

    # 9. Multiple-topic engine filtering
    def test_09_multiple_topic_engine_filtering(self):
        engine = TelegramDownloaderEngine(self.cfg, self.db)
        engine.client = MagicMock()

        msg1 = MockMessage(201, reply_to=MockReplyHeader(reply_to_top_id=150))
        msg2 = MockMessage(202, reply_to=MockReplyHeader(reply_to_top_id=2))
        msg3 = MockMessage(203, reply_to=MockReplyHeader(reply_to_top_id=5))
        msg4 = MockMessage(204, reply_to=MockReplyHeader(reply_to_top_id=999))

        async def mock_iter(*args, **kwargs):
            for m in [msg1, msg2, msg3, msg4]:
                yield m

        engine.client.iter_messages = mock_iter
        engine.match_media = MagicMock(return_value=(True, "test.pdf", 1024))
        engine.download_single_message = AsyncMock()

        entity = MagicMock(forum=True, megagroup=True)
        # Filter for topics {150, 5}
        completed = asyncio.run(engine.scan_and_download_channel(
            entity, self.forum_cid, "Premium 2027",
            media_filters={"PDFs": True}, cutoff_date=None,
            target_topic_ids={150, 5}
        ))

        # Only msg1 (150) and msg3 (5) should be downloaded
        self.assertEqual(engine.download_single_message.call_count, 2)
        downloaded_msg_ids = [call[0][0].id for call in engine.download_single_message.call_args_list]
        self.assertEqual(downloaded_msg_ids, [201, 203])

    # 10. Strict one-group execution scope in engine
    def test_10_strict_one_group_execution_scope(self):
        engine = TelegramDownloaderEngine(self.cfg, self.db)
        dialogs = [
            {"id": self.forum_cid, "name": "Premium 2027", "entity": MagicMock()},
            {"id": self.broadcast_cid, "name": "FORUM PTS 2026", "entity": MagicMock()}
        ]

        # Expect ValueError when expected_channel_id is provided but batch has multiple dialogs
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(engine.run_batch(
                dialogs,
                target_topic_ids={150},
                expected_channel_id=self.forum_cid
            ))
        self.assertIn("Scope violation", str(ctx.exception))

    # 11. Backward compatibility with target_topic_id
    def test_11_backward_compatibility_with_target_topic_id(self):
        engine = TelegramDownloaderEngine(self.cfg, self.db)
        engine.client = MagicMock()

        msg1 = MockMessage(301, reply_to=MockReplyHeader(reply_to_top_id=150))
        msg2 = MockMessage(302, reply_to=MockReplyHeader(reply_to_top_id=2))

        async def mock_iter(*args, **kwargs):
            for m in [msg1, msg2]:
                yield m

        engine.client.iter_messages = mock_iter
        engine.match_media = MagicMock(return_value=(True, "test.pdf", 1024))
        engine.download_single_message = AsyncMock()

        entity = MagicMock(forum=True, megagroup=True)
        # Using legacy target_topic_id parameter
        asyncio.run(engine.scan_and_download_channel(
            entity, self.forum_cid, "Premium 2027",
            media_filters={"PDFs": True}, cutoff_date=None,
            target_topic_id=150
        ))

        self.assertEqual(engine.download_single_message.call_count, 1)
        self.assertEqual(engine.download_single_message.call_args_list[0][0][0].id, 301)

    # 12. Unavailable / inaccessible topic handling
    def test_12_unavailable_topic_handling(self):
        mock_topics = self.db.get_forum_topics(self.forum_cid, account_id=self.account_id)
        for m in mock_topics:
            if m["topic_id"] == 999:
                m["is_inaccessible"] = True

        with patch.object(self.db, "get_forum_topics", return_value=mock_topics):
            self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
            self.app.on_download_tree_select()

            # Check status in topic tree
            vals = self.app.topic_tree.item("topic_999", "values")
            self.assertEqual(vals[3], "Inaccessible")

            # Attempt to toggle it
            with patch("tkinter.messagebox.showwarning") as mock_warn:
                self.app.toggle_topic_checkbox("topic_999")
                mock_warn.assert_called_once()
                self.assertIn("marked Inaccessible", mock_warn.call_args[0][1])

            # Verify it remains unchecked
            vals_after = self.app.topic_tree.item("topic_999", "values")
            self.assertEqual(vals_after[0], "☐")

    # 13. No accidental global Start Downloads fallback
    def test_13_no_accidental_global_start_downloads_fallback(self):
        # Select forum group, check topic 150
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()
        self.app.toggle_topic_checkbox("topic_150")

        # Mock engine.run_batch and user confirmation
        self.app.engine = MagicMock()
        self.app.engine.resolve_selection_entity = AsyncMock(return_value=MagicMock())
        self.app.engine.run_batch = AsyncMock()

        with patch("tkinter.messagebox.askyesno", return_value=True):
            self.app.on_download_selected_topics()
            import time
            for _ in range(20):
                if self.app.engine.run_batch.call_count > 0:
                    break
                time.sleep(0.05)

        # Check that run_batch was called with exactly 1 dialog entity, NOT all 3 channels!
        self.assertEqual(self.app.engine.run_batch.call_count, 1)
        call_args = self.app.engine.run_batch.call_args
        selected_dialogs = call_args[0][0]
        self.assertEqual(len(selected_dialogs), 1)
        self.assertEqual(selected_dialogs[0]["id"], self.forum_cid)
        self.assertEqual(call_args[1]["target_topic_ids"], {150})
        self.assertEqual(call_args[1]["expected_channel_id"], self.forum_cid)
        self.assertEqual(call_args[1]["expected_peer_type"], "channel")
        self.assertEqual(call_args[1]["expected_account_id"], self.account_id)

    # 14. Destination folder preview
    def test_14_destination_folder_preview(self):
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()

        vals_150 = self.app.topic_tree.item("topic_150", "values")
        expected_folder = r"Premium 2027\DAILY NEWSPAPER"
        self.assertEqual(vals_150[4], expected_folder)

    # 15. Legacy completed-file duplicate skipping
    def test_15_legacy_completed_file_duplicate_skipping(self):
        # Record completed file in DB
        rel_path = r"Premium 2027\DAILY NEWSPAPER\existing.pdf"
        target_path = Path(self.download_dir) / rel_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"content")

        self.db.record_completed(
            self.forum_cid, 501, "existing.pdf", len(b"content"),
            rel_path, topic_id=150, topic_title="DAILY NEWSPAPER"
        )

        engine = TelegramDownloaderEngine(self.cfg, self.db)
        engine.account_id = self.account_id

        # resolve_destination_path must recognize existing file
        res_path, res_rel, t_id, t_title = engine.resolve_destination_path(
            self.forum_cid, "Premium 2027", "existing.pdf", 501,
            is_forum=True, topic_id=150, topic_title="DAILY NEWSPAPER",
            expected_size=len(b"content")
        )
        self.assertEqual(res_rel, rel_path)
        self.assertEqual(t_id, 150)
        self.assertTrue(self.db.is_message_downloaded(self.forum_cid, 501))

    # 16. .part path preservation
    def test_16_part_path_preservation(self):
        rel_path = r"Channel_4213926054\partial_video.mp4"
        part_path = Path(self.download_dir) / f"{rel_path}.part"
        part_path.parent.mkdir(parents=True, exist_ok=True)
        part_path.write_bytes(b"partial content" * 100)

        # Ensure channel foreign key exists in channels table
        self.db.ensure_channel(4213926054, "Prashant sir current affairs 2027")
        # Record start in DB pointing to this .part file
        self.db.record_start(4213926054, 888, "partial_video.mp4", 5000, rel_path)

        engine = TelegramDownloaderEngine(self.cfg, self.db)
        engine.account_id = self.account_id

        # Destination resolver must preserve exact .part path for message 888
        res_path, res_rel, _, _ = engine.resolve_destination_path(
            4213926054, "Prashant sir current affairs 2027", "partial_video.mp4", 888,
            expected_size=5000
        )
        self.assertEqual(res_rel, rel_path)
        self.assertTrue(Path(f"{res_path}.part").exists())

    # 17. GUI responsiveness & background topic refresh without blocking
    def test_17_gui_responsiveness(self):
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()

        self.app.engine = MagicMock()
        async def mock_fetch(*args, **kwargs):
            return {150: {"title": "DAILY NEWSPAPER", "local_folder_name": "DAILY NEWSPAPER"}}
        self.app.engine.get_or_fetch_forum_topics = mock_fetch
        self.app.engine.resolve_selection_entity = AsyncMock(return_value=MagicMock())

        # Trigger refresh
        self.app.on_refresh_topics_from_telegram()
        # Verify button immediately enters non-blocking "Refreshing..." state
        self.assertEqual(self.app.btn_refresh_topics.cget("text"), "Refreshing...")
        self.assertEqual(str(self.app.btn_refresh_topics.cget("state")), "disabled")

    # 18. GUI Controlled Acceptance Test Parameter Forwarding
    def test_18_gui_controlled_acceptance_test_parameter_forwarding(self):
        self.app.controlled_acceptance_test_mode = True
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()
        self.app.toggle_topic_checkbox("topic_150")

        self.app.engine = MagicMock()
        self.app.engine.resolve_selection_entity = AsyncMock(return_value=MagicMock())
        self.app.engine.run_batch = AsyncMock()

        captured_dialog = {}
        def mock_askyesno(title, message):
            captured_dialog["title"] = title
            captured_dialog["message"] = message
            return True

        with patch("tkinter.messagebox.askyesno", side_effect=mock_askyesno):
            self.app.on_download_selected_topics()
            import time
            for _ in range(20):
                if self.app.engine.run_batch.call_count > 0:
                    break
                time.sleep(0.05)

        # 1. Verify confirmation dialog text
        self.assertIn("Confirm Controlled Acceptance Test", captured_dialog["title"])
        msg = captured_dialog["message"]
        self.assertIn("INDIAN EXPRESS HD Delhi 13~10~2026.pdf", msg)
        self.assertIn("INDIAN EXPRESS HD Delhi 12~10~2026.pdf", msg)
        self.assertIn("1613", msg)
        self.assertIn("1607", msg)
        self.assertIn("6,215,860 bytes", msg)
        self.assertIn("6,654,297 bytes", msg)
        self.assertIn("DAILY NEWSPAPER", msg)
        self.assertIn("15,000,000 bytes", msg)

        # 2. Verify run_batch arguments
        self.assertEqual(self.app.engine.run_batch.call_count, 1)
        args, kwargs = self.app.engine.run_batch.call_args
        selected_dialogs = args[0]
        self.assertEqual(len(selected_dialogs), 1)
        self.assertEqual(selected_dialogs[0]["id"], self.forum_cid)
        self.assertEqual(kwargs["target_topic_ids"], {150})
        self.assertEqual(kwargs["expected_channel_id"], 4450272939)
        self.assertEqual(kwargs["expected_peer_type"], "channel")
        self.assertEqual(kwargs["expected_account_id"], self.account_id)
        self.assertEqual(kwargs["allowed_message_ids"], [1613, 1607])
        self.assertEqual(kwargs["max_completed_files"], 2)
        self.assertEqual(kwargs["max_total_bytes"], 15000000)

    # 19. Confirmation Dialog Cancel Invokes No Engine Operation
    def test_19_gui_acceptance_test_cancel_invokes_no_engine_operation(self):
        self.app.controlled_acceptance_test_mode = True
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()
        self.app.toggle_topic_checkbox("topic_150")

        self.app.engine = MagicMock()
        self.app.engine.run_batch = AsyncMock()

        with patch("tkinter.messagebox.askyesno", return_value=False):
            self.app.on_download_selected_topics()

        self.assertEqual(self.app.engine.run_batch.call_count, 0)

    # 20. Bounded Test Disabled Outside Acceptance Mode
    def test_20_bounded_test_disabled_outside_acceptance_mode(self):
        self.app.controlled_acceptance_test_mode = False
        self.app.download_tree.selection_set(f"channel_{self.forum_cid}")
        self.app.on_download_tree_select()
        self.app.toggle_topic_checkbox("topic_150")

        self.app.engine = MagicMock()
        self.app.engine.resolve_selection_entity = AsyncMock(return_value=MagicMock())
        self.app.engine.run_batch = AsyncMock()

        captured_dialog = {}
        def mock_askyesno(title, message):
            captured_dialog["title"] = title
            captured_dialog["message"] = message
            return True

        with patch("tkinter.messagebox.askyesno", side_effect=mock_askyesno):
            self.app.on_download_selected_topics()
            import time
            for _ in range(20):
                if self.app.engine.run_batch.call_count > 0:
                    break
                time.sleep(0.05)

        # Standard dialog should be shown, not controlled acceptance test
        self.assertEqual(captured_dialog["title"], "Confirm Scoped Topic Download")
        self.assertNotIn("CONTROLLED ACCEPTANCE TEST MODE", captured_dialog["message"])

        # run_batch called with normal production parameters (no bounded limits)
        self.assertEqual(self.app.engine.run_batch.call_count, 1)
        args, kwargs = self.app.engine.run_batch.call_args
        self.assertNotIn("allowed_message_ids", kwargs)
        self.assertNotIn("max_completed_files", kwargs)
        self.assertNotIn("max_total_bytes", kwargs)
        self.assertEqual(kwargs["target_topic_ids"], {150})
        self.assertEqual(kwargs["expected_channel_id"], self.forum_cid)

    # 21. Fail-Closed Byte Budget Enforcement: Pre-Transfer and Chunk Reception
    def test_21_fail_closed_byte_budget_enforcement(self):
        engine = TelegramDownloaderEngine(self.cfg, self.db)
        engine.client = MagicMock()

        channel_folder = Path(self.download_dir) / "Premium 2027"
        channel_folder.mkdir(parents=True, exist_ok=True)

        # Part A: Pre-transfer check rejection (file size exceeds budget before download starts)
        doc_large = make_mock_media_doc(size=1000000, mime_type="application/pdf")
        msg_large = MockMessage(1613, date=None, media=doc_large)

        with self.assertRaises(ValueError) as ctx_pre:
            asyncio.run(engine.download_single_message(
                msg_large, self.forum_cid, channel_folder, "test_pre.pdf",
                expected_size=1000000,
                is_forum=True, topic_id=150, topic_title="DAILY NEWSPAPER",
                bytes_prior=0,
                max_total_bytes=800 * 1024
            ))
        self.assertIn("Byte budget exceeded before transfer", str(ctx_pre.exception))

        # Part B: In-chunk check rejection (streamed chunks exceed budget mid-transfer)
        # Expected size is 800KB (passes pre-transfer), but actual streamed chunks total 1024KB
        doc_chunk = make_mock_media_doc(size=800 * 1024, mime_type="application/pdf")
        msg_chunk = MockMessage(1613, date=None, media=doc_chunk)

        chunk1 = b"A" * (512 * 1024)
        chunk2 = b"B" * (512 * 1024)
        async def mock_iter_download(*args, **kwargs):
            yield chunk1
            yield chunk2

        engine.client.iter_download = mock_iter_download

        with self.assertRaises(ValueError) as ctx_chunk:
            asyncio.run(engine.download_single_message(
                msg_chunk, self.forum_cid, channel_folder, "test_chunk.pdf",
                expected_size=800 * 1024,
                is_forum=True, topic_id=150, topic_title="DAILY NEWSPAPER",
                bytes_prior=0,
                max_total_bytes=800 * 1024
            ))
        self.assertIn("Byte budget exceeded during chunk transfer", str(ctx_chunk.exception))

    # 22. Fail-Closed Rejections: MIME, Size, Topic, Allowlist, and Third File
    def test_22_fail_closed_rejections(self):
        engine = TelegramDownloaderEngine(self.cfg, self.db)
        engine.client = MagicMock()

        # Message with wrong MIME (video/mp4)
        msg_wrong_mime = MockMessage(1613, reply_to=MockReplyHeader(reply_to_top_id=150),
                                     media=make_mock_media_doc(size=1000, mime_type="video/mp4"))

        entity = MagicMock(forum=True, megagroup=True)

        # Test wrong MIME rejection
        async def iter_wrong_mime(*a, **k): yield msg_wrong_mime
        engine.client.iter_messages = iter_wrong_mime
        count = asyncio.run(engine.scan_and_download_channel(
            entity, self.forum_cid, "Premium 2027",
            media_filters={"All Media": True, "PDFs": True}, cutoff_date=None,
            target_topic_ids={150}, allowed_message_ids=[1613, 1607],
            max_completed_files=2, max_total_bytes=15000000
        ))
        self.assertEqual(count, 0)

        # Test unexpected third file prevention
        valid_msg1 = MockMessage(1613, reply_to=MockReplyHeader(reply_to_top_id=150),
                                 media=make_mock_media_doc(size=1000, mime_type="application/pdf"))
        valid_msg2 = MockMessage(1607, reply_to=MockReplyHeader(reply_to_top_id=150),
                                 media=make_mock_media_doc(size=1000, mime_type="application/pdf"))
        valid_msg3 = MockMessage(1600, reply_to=MockReplyHeader(reply_to_top_id=150),
                                 media=make_mock_media_doc(size=1000, mime_type="application/pdf"))

        async def iter_three_msgs(*a, **k):
            yield valid_msg1
            yield valid_msg2
            yield valid_msg3
        engine.client.iter_messages = iter_three_msgs
        engine.download_single_message = AsyncMock()

        count_three = asyncio.run(engine.scan_and_download_channel(
            entity, self.forum_cid, "Premium 2027",
            media_filters={"PDFs": True}, cutoff_date=None,
            target_topic_ids={150}, allowed_message_ids=[1613, 1607],
            max_completed_files=2, max_total_bytes=15000000
        ))
        # Strictly at most 2 files permitted
        self.assertEqual(count_three, 2)
        self.assertEqual(engine.download_single_message.call_count, 2)

    # 23. Stopping a Transfer Preserves Resumable Partial Data
    def test_23_stopping_transfer_preserves_resumable_partial_data(self):
        engine = TelegramDownloaderEngine(self.cfg, self.db)
        engine.client = MagicMock()

        doc_media = make_mock_media_doc(size=5000000, mime_type="application/pdf")
        msg = MockMessage(1613, date=None, media=doc_media)

        # Mock chunk download that sets cancel_flag on second chunk
        chunk_data = b"X" * (512 * 1024)
        async def mock_iter_download(*args, **kwargs):
            yield chunk_data
            engine.cancel_flag = True
            yield chunk_data

        engine.client.iter_download = mock_iter_download
        channel_folder = Path(self.download_dir) / "Premium 2027"
        channel_folder.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(CancelledByUser):
            asyncio.run(engine.download_single_message(
                msg, self.forum_cid, channel_folder, "cancel_test.pdf",
                expected_size=5000000,
                is_forum=True, topic_id=150, topic_title="DAILY NEWSPAPER",
                max_total_bytes=15000000
            ))

        # Verify .part file exists on disk and preserves exactly the flushed first chunk
        expected_part = channel_folder / "DAILY NEWSPAPER" / "cancel_test.pdf.part"
        self.assertTrue(expected_part.exists())
        self.assertEqual(expected_part.stat().st_size, len(chunk_data))

    # 24. Duplicate Prevention
    def test_24_duplicate_prevention(self):
        rel_path = r"Premium 2027\DAILY NEWSPAPER\dup_test.pdf"
        target_path = Path(self.download_dir) / rel_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"existing content")

        self.db.record_completed(
            self.forum_cid, 1613, "dup_test.pdf", len(b"existing content"),
            rel_path, topic_id=150, topic_title="DAILY NEWSPAPER"
        )

        engine = TelegramDownloaderEngine(self.cfg, self.db)
        engine.client = MagicMock()
        engine.client.iter_download = AsyncMock()

        doc_media = make_mock_media_doc(size=len(b"existing content"), mime_type="application/pdf")
        msg = MockMessage(1613, date=None, media=doc_media)
        channel_folder = Path(self.download_dir) / "Premium 2027"

        # Calling download_single_message for existing completed file
        asyncio.run(engine.download_single_message(
            msg, self.forum_cid, channel_folder, "dup_test.pdf",
            expected_size=len(b"existing content"),
            is_forum=True, topic_id=150, topic_title="DAILY NEWSPAPER"
        ))

        # Iter download was NEVER called because duplicate was skipped
        self.assertEqual(engine.client.iter_download.call_count, 0)


if __name__ == "__main__":
    unittest.main()
