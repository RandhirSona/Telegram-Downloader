import os
import sys
import unittest
import tempfile
import sqlite3
import tkinter as tk
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db_tracker import DownloadTrackerDB, normalize_channel_id
from config_manager import ConfigManager
from telegram_gui_downloader import TelegramGUIApp

class TestStageAReconciliationAndCheckbox(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_stage_a.db"
        self.cfg_path = Path(self.tmpdir) / "config.json"

        self.cfg_mgr = ConfigManager(str(self.cfg_path))
        self.cfg_mgr.save_config({
            "download_directory": self.tmpdir,
            "api_id": 12345,
            "api_hash": "testhash",
            "active_channels": []
        })

        self.db = DownloadTrackerDB(str(self.db_path))

        self.root = tk.Tk()
        self.root.withdraw()

        self.mock_lock = MagicMock()
        self.mock_lock.acquire_session_lock.return_value = True

        self.dialogs_dataset = [
            {"id": 1001, "name": "Real Alpha Channel", "peer_type": "channel", "access_hash": 111, "username": "alpha"},
            {"id": 1002, "name": "Real Beta Supergroup", "peer_type": "channel", "access_hash": 222, "username": "beta"},
            {"id": 1003, "name": "Real Gamma Discussion", "peer_type": "chat", "access_hash": None, "username": ""},
            {"id": 1004, "name": "Real Delta Channel", "peer_type": "channel", "access_hash": 444, "username": "delta"},
        ]

        def fake_filter(term):
            if not term:
                return list(self.dialogs_dataset)
            t = term.lower()
            return [d for d in self.dialogs_dataset if t in d["name"].lower() or t in d.get("username", "").lower()]

        async def fake_check_auth():
            return True

        self.mock_engine = MagicMock()
        self.mock_engine.account_id = 99999
        self.mock_engine.check_auth = fake_check_auth
        self.mock_engine.filter_cached_dialogs.side_effect = fake_filter
        self.mock_engine.dialogs_cache = {(d["peer_type"], d["id"]): d for d in self.dialogs_dataset}

        with patch("telegram_gui_downloader.ConfigManager", return_value=self.cfg_mgr), \
             patch("telegram_gui_downloader.DownloadTrackerDB", return_value=self.db), \
             patch("telegram_gui_downloader.ProcessLock", return_value=self.mock_lock), \
             patch("telegram_gui_downloader.TelegramDownloaderEngine", return_value=self.mock_engine):

            self.app = TelegramGUIApp(self.root)
            self.app.engine = self.mock_engine
            self.app.all_dialogs = self.dialogs_dataset
            self.app.dialogs_map = {(d["peer_type"], d["id"]): d for d in self.dialogs_dataset}

    def tearDown(self):
        try:
            self.app.on_close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # 1. Parameterized selection title update leaves account, peer type, dates, and selection intact
    def test_update_selection_title_parameterized(self):
        # Insert legacy placeholder
        self.db.add_selection(99999, 1001, "Channel_1001", "channel")
        orig_sel = self.db.get_active_selections(99999)[0]
        orig_date = orig_sel["date_added"]

        # Update title
        success = self.db.update_selection_title(99999, 1001, "channel", "Real Alpha Channel", access_hash=111, username="alpha")
        self.assertTrue(success)

        updated_sel = self.db.get_active_selections(99999)[0]
        self.assertEqual(updated_sel["title"], "Real Alpha Channel")
        self.assertEqual(updated_sel["account_id"], 99999)
        self.assertEqual(updated_sel["peer_type"], "channel")
        self.assertEqual(updated_sel["channel_id"], 1001)
        self.assertEqual(updated_sel["access_hash"], 111)
        self.assertEqual(updated_sel["username"], "alpha")
        self.assertEqual(updated_sel["is_selected"], 1)
        self.assertEqual(updated_sel["date_added"], orig_date)  # Date added preserved

        # Also verify channels table was updated
        chan = self.db.get_channel(1001)
        self.assertEqual(chan["name"], "Real Alpha Channel")

    # 2. Reconcile saved selections from dialog cache (Priority 1)
    def test_reconcile_from_dialog_cache(self):
        # Insert 2 legacy placeholder channels
        self.db.add_selection(99999, 1001, "Channel_1001", "channel")
        self.db.add_selection(99999, 1002, "Channel_1002", "channel")

        # Run reconciliation
        self.app.reconcile_saved_channel_titles()
        self.app.refresh_download_list_tree()

        # Verify Download List displays real titles
        active = self.db.get_active_selections(99999)
        title_map = {a["channel_id"]: a["title"] for a in active}
        self.assertEqual(title_map[1001], "Real Alpha Channel")
        self.assertEqual(title_map[1002], "Real Beta Supergroup")

        # Verify Treeview displays the real titles
        items = self.app.download_tree.get_children()
        self.assertEqual(len(items), 2)
        vals_1001 = self.app.download_tree.item("channel_1001")["values"]
        self.assertEqual(vals_1001[0], "Real Alpha Channel")
        vals_1002 = self.app.download_tree.item("channel_1002")["values"]
        self.assertEqual(vals_1002[0], "Real Beta Supergroup")

    # 3. Reconcile fallback to SQLite channels table (Priority 3)
    def test_reconcile_fallback_to_sqlite_channels(self):
        # Channel 9999 not in dialogs cache, but exists in channels table with real name
        self.db.ensure_channel(9999, "Last Known Real Title")
        self.db.add_selection(99999, 9999, "Channel_9999", "channel")

        self.app.reconcile_saved_channel_titles()

        active = self.db.get_active_selections(99999)
        self.assertEqual(active[0]["title"], "Last Known Real Title")

    # 4. Genuinely unresolved entity gets clear fallback label (Priority 4)
    def test_reconcile_unresolved_fallback(self):
        # Channel 8888 not in cache and only has placeholder in channels table
        self.db.add_selection(99999, 8888, "Channel_8888", "channel")

        self.app.reconcile_saved_channel_titles()

        active = self.db.get_active_selections(99999)
        self.assertEqual(active[0]["title"], "[Unresolved] Channel 8888")
        self.assertEqual(active[0]["access_status"], "Unresolved")

    # 5. Inaccessible channel preserves selection and metadata
    def test_inaccessible_channel_preserves_selection(self):
        self.db.add_selection(99999, 1001, "Channel_1001", "channel")
        # Simulate inaccessible: update status without deleting
        self.db.update_selection_title(99999, 1001, "channel", "Alpha Channel", access_status="Inaccessible")

        active = self.db.get_active_selections(99999)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["access_status"], "Inaccessible")
        self.assertTrue(self.db.is_selected(99999, 1001, "channel"))

    # 6. Checkbox 3-state display: Already Saved displays ☑ [✓ In Download List]
    def test_saved_channel_displays_checked_and_in_download_list(self):
        self.db.add_selection(99999, 1001, "Real Alpha Channel", "channel")
        self.app.refresh_browse_tree()

        vals = self.app.browse_tree.item("channel_1001")["values"]
        self.assertEqual(vals[0], "☑")
        self.assertEqual(vals[4], "[✓ In Download List]")

    # 7. Checkbox 3-state display: Not added displays ☐ [+ Not Added]
    def test_not_added_channel_displays_unchecked(self):
        self.app.refresh_browse_tree()

        vals = self.app.browse_tree.item("channel_1001")["values"]
        self.assertEqual(vals[0], "☐")
        self.assertEqual(vals[4], "[+ Not Added]")

    # 8. Checkbox 3-state display: Temporarily checked displays ☑ [+ Pending Add]
    def test_temporarily_checked_displays_pending_add(self):
        self.app.refresh_browse_tree()
        self.app.toggle_checkbox("channel_1001")

        vals = self.app.browse_tree.item("channel_1001")["values"]
        self.assertEqual(vals[0], "☑")
        self.assertEqual(vals[4], "[+ Pending Add]")
        self.assertIn(("channel", 1001), self.app.checked_channels)

    # 9. Clicking checkbox of already-added channel guards against silent removal
    def test_clicking_saved_channel_guards_against_silent_removal(self):
        self.db.add_selection(99999, 1001, "Real Alpha Channel", "channel")
        self.app.refresh_browse_tree()

        # Click the saved channel
        self.app.toggle_checkbox("channel_1001")

        # Must still be selected in DB
        self.assertTrue(self.db.is_selected(99999, 1001, "channel"))
        # Must not be added to pending additions
        self.assertNotIn(("channel", 1001), self.app.checked_channels)
        # Visual state must remain checked
        vals = self.app.browse_tree.item("channel_1001")["values"]
        self.assertEqual(vals[0], "☑")
        self.assertEqual(vals[4], "[✓ In Download List]")
        # Status bar informs user
        self.assertIn("already saved", self.app.browse_status_var.get())

    # 10. Select All Filtered checks only unadded channels
    def test_select_all_filtered_excludes_saved(self):
        self.db.add_selection(99999, 1001, "Real Alpha Channel", "channel")
        self.app.refresh_browse_tree()

        self.app.on_select_all_filtered()

        # 1001 was saved, so only remaining channels (1002, 1003, 1004) are in checked_channels
        self.assertNotIn(("channel", 1001), self.app.checked_channels)
        self.assertIn(("channel", 1002), self.app.checked_channels)
        self.assertIn(("chat", 1003), self.app.checked_channels)
        self.assertIn(("channel", 1004), self.app.checked_channels)
        self.assertEqual(len(self.app.checked_channels), 3)

    # 11. Unselect All Filtered clears only pending additions, leaves saved checked
    def test_unselect_all_filtered_preserves_saved(self):
        self.db.add_selection(99999, 1001, "Real Alpha Channel", "channel")
        self.app.refresh_browse_tree()

        self.app.toggle_checkbox("channel_1002")  # Pending
        self.assertEqual(len(self.app.checked_channels), 1)

        self.app.on_unselect_all_filtered()
        self.assertEqual(len(self.app.checked_channels), 0)

        # 1001 remains saved and visually checked
        vals_1001 = self.app.browse_tree.item("channel_1001")["values"]
        self.assertEqual(vals_1001[0], "☑")
        self.assertEqual(vals_1001[4], "[✓ In Download List]")

        # 1002 is reverted to unchecked
        vals_1002 = self.app.browse_tree.item("channel_1002")["values"]
        self.assertEqual(vals_1002[0], "☐")
        self.assertEqual(vals_1002[4], "[+ Not Added]")

    # 12. Add Selected (N) button text reflects only pending additions
    def test_add_selected_counter_only_pending(self):
        self.db.add_selection(99999, 1001, "Real Alpha Channel", "channel")
        self.app.refresh_browse_tree()

        # Button should show 0 pending even though 1 is in DB
        self.assertEqual(self.app.btn_add_checked.cget("text"), "+ Add Selected (0) to Download List")

        self.app.toggle_checkbox("channel_1002")
        self.assertEqual(self.app.btn_add_checked.cget("text"), "+ Add Selected (1) to Download List")

    # 13. Same numeric ID across different peer types handled independently
    def test_same_numeric_id_different_peer_types(self):
        # Channel 1003 and Chat 1003
        self.db.add_selection(99999, 1003, "Channel Version", "channel")
        self.db.add_selection(99999, 1003, "Chat Version", "chat")

        # Update channel version title
        self.db.update_selection_title(99999, 1003, "channel", "Updated Channel Title")

        active = self.db.get_active_selections(99999)
        title_by_peer = {a["peer_type"]: a["title"] for a in active}
        self.assertEqual(title_by_peer["channel"], "Updated Channel Title")
        self.assertEqual(title_by_peer["chat"], "Chat Version")  # Chat version unchanged!

    # 14. Restart GUI restores verified titles and checked status from SQLite
    def test_restart_restores_from_sqlite(self):
        self.db.add_selection(99999, 1001, "Persisted Alpha Title", "channel")
        
        # Simulate GUI restart by initializing a second app instance
        root2 = tk.Tk()
        root2.withdraw()
        with patch("telegram_gui_downloader.ConfigManager", return_value=self.cfg_mgr), \
             patch("telegram_gui_downloader.DownloadTrackerDB", return_value=self.db), \
             patch("telegram_gui_downloader.ProcessLock", return_value=self.mock_lock), \
             patch("telegram_gui_downloader.TelegramDownloaderEngine", return_value=self.mock_engine):

            app2 = TelegramGUIApp(root2)
            app2.engine = self.mock_engine
            app2.all_dialogs = self.dialogs_dataset
            app2.refresh_download_list_tree()
            app2.refresh_browse_tree()

            # Saved selection appears in Download List with persisted title
            vals_mgr = app2.download_tree.item("channel_1001")["values"]
            self.assertEqual(vals_mgr[0], "Persisted Alpha Title")

            # Appears checked in Browse tab
            vals_browse = app2.browse_tree.item("channel_1001")["values"]
            self.assertEqual(vals_browse[0], "☑")
            self.assertEqual(vals_browse[4], "[✓ In Download List]")

            try:
                app2.on_close()
            except Exception:
                pass
            try:
                root2.destroy()
            except Exception:
                pass


    # 15. Search query does not alter saved memberships
    def test_search_preserves_saved_memberships(self):
        self.db.add_selection(99999, 1001, "Real Alpha Channel", "channel")
        self.app.search_var.set("Beta")
        self.app.on_search()

        # Only Beta is visible in Browse tree
        visible = list(self.app.browse_tree.get_children())
        self.assertEqual(visible, ["channel_1002"])

        # DB selection remains 1001
        active = self.db.get_active_selections(99999)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["channel_id"], 1001)

    # 16. Existing download records and files remain 100% untouched
    def test_download_records_untouched(self):
        # Insert a dummy completed record
        self.db.ensure_channel(1001, "Real Alpha Channel")
        self.db.record_start(1001, 10, "doc.pdf", 1024, "Real Alpha Channel/doc.pdf")
        self.db.record_completed(1001, 10, "doc.pdf", 1024, "Real Alpha Channel/doc.pdf")

        # Perform title reconciliation
        self.db.update_selection_title(99999, 1001, "channel", "New Real Title")

        # Verify download record is unchanged
        rec = self.db.get_download_record(1001, 10)
        self.assertEqual(rec["status"], "COMPLETED")
        self.assertEqual(rec["file_name"], "doc.pdf")
        self.assertEqual(rec["relative_path"], "Real Alpha Channel/doc.pdf")

if __name__ == "__main__":
    unittest.main()
