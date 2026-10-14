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

from db_tracker import DownloadTrackerDB
from config_manager import ConfigManager
from telegram_gui_downloader import TelegramGUIApp

class TestCheckboxMultiSelect(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_gui_tracker.db"
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
            {"id": 1001, "name": "TEST Series Alpha", "peer_type": "channel", "access_hash": 111, "username": "alpha"},
            {"id": 1002, "name": "TEST Daily Quiz", "peer_type": "channel", "access_hash": 222, "username": "quiz"},
            {"id": 1003, "name": "TEST Mains Papers", "peer_type": "channel", "access_hash": 333, "username": "mains"},
            {"id": 1004, "name": "TEST Discussion", "peer_type": "chat", "access_hash": None, "username": ""},
            {"id": 1005, "name": "TEST General Group", "peer_type": "channel", "access_hash": 555, "username": "gen"},
            {"id": 2001, "name": "IAS Notes Central", "peer_type": "channel", "access_hash": 666, "username": "ias1"},
            {"id": 2002, "name": "IAS Mentorship", "peer_type": "channel", "access_hash": 777, "username": "ias2"},
            {"id": 3001, "name": "Civil Services Weekly", "peer_type": "channel", "access_hash": 888, "username": "civil"}
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

    # 1. Search TEST -> Add one item -> Filter remains TEST
    def test_search_test_add_one_preserves_filter(self):
        self.app.search_var.set("TEST")
        self.app.on_search()
        
        # Verify 5 TEST items visible
        visible_initial = list(self.app.browse_tree.get_children())
        self.assertEqual(len(visible_initial), 5)

        # Check item 1001 and click Add Selected
        self.app.toggle_checkbox("channel_1001")
        self.assertIn(("channel", 1001), self.app.checked_channels)
        self.app.on_add_checked_to_download_list()

        # Search term must still be TEST and matching rows still 5
        self.assertEqual(self.app.search_var.get(), "TEST")
        visible_after = list(self.app.browse_tree.get_children())
        self.assertEqual(len(visible_after), 5)

        # Item 1001 status must be marked [✓ In Download List] with checked ☑
        vals_1001 = self.app.browse_tree.item("channel_1001")["values"]
        self.assertEqual(vals_1001[4], "[✓ In Download List]")
        self.assertEqual(vals_1001[0], "☑")  # Checkbox permanently checked for saved channel


    # 2. Search TEST -> Select five rows -> Add all five
    def test_search_test_add_five_rows(self):
        self.app.search_var.set("TEST")
        self.app.on_search()

        self.app.on_select_all_filtered()
        self.assertEqual(len(self.app.checked_channels), 5)

        self.app.on_add_checked_to_download_list()
        
        # All 5 added to DB
        active = self.db.get_active_selections(99999)
        self.assertEqual(len(active), 5)

        # Filter remains TEST
        self.assertEqual(self.app.search_var.get(), "TEST")
        visible = list(self.app.browse_tree.get_children())
        self.assertEqual(len(visible), 5)

    # 3. Previously added channels remain in the Download List
    def test_previously_added_remain_in_download_list(self):
        self.db.add_selection(99999, 1001, "Alpha")
        self.app.refresh_download_list_tree()
        self.app.refresh_browse_tree()

        self.app.search_var.set("IAS")
        self.app.on_search()
        self.app.toggle_checkbox("channel_2001")
        self.app.on_add_checked_to_download_list()

        active = self.db.get_active_selections(99999)
        active_ids = {s["channel_id"] for s in active}
        self.assertEqual(active_ids, {1001, 2001})

    # 4. Duplicate Add does not create duplicate records
    def test_duplicate_add_no_duplicate_records(self):
        self.db.add_selection(99999, 1001, "Alpha")
        
        # Re-add via GUI
        self.app.checked_channels.add(("channel", 1001))
        self.app.on_add_checked_to_download_list()

        active = self.db.get_active_selections(99999)
        self.assertEqual(len(active), 1)

    # 5. Select All Filtered selects only visible eligible results
    def test_select_all_filtered_only_eligible_visible(self):
        # Pre-add 1001
        self.db.add_selection(99999, 1001, "Alpha")

        self.app.search_var.set("TEST")
        self.app.on_search()

        self.app.on_select_all_filtered()
        # Out of 5 TEST channels, 1001 is already in DB, so only remaining 4 should be checked
        self.assertEqual(len(self.app.checked_channels), 4)
        self.assertNotIn(("channel", 1001), self.app.checked_channels)

    # 6. Unselect All Filtered leaves persistent selections untouched
    def test_unselect_all_filtered_leaves_db_untouched(self):
        self.db.add_selection(99999, 1001, "Alpha")

        self.app.search_var.set("TEST")
        self.app.on_search()
        self.app.on_select_all_filtered()
        self.assertGreater(len(self.app.checked_channels), 0)

        self.app.on_unselect_all_filtered()
        self.assertEqual(len(self.app.checked_channels), 0)

        # DB remains intact
        active = self.db.get_active_selections(99999)
        self.assertEqual(len(active), 1)

    # 7. Cross-search temporary selections
    def test_cross_search_temporary_selections(self):
        # Step A: Search TEST and check 2 channels
        self.app.search_var.set("TEST")
        self.app.on_search()
        self.app.toggle_checkbox("channel_1001")
        self.app.toggle_checkbox("channel_1002")
        self.assertEqual(len(self.app.checked_channels), 2)

        # Step B: Search IAS and check 2 more channels
        self.app.search_var.set("IAS")
        self.app.on_search()
        self.app.toggle_checkbox("channel_2001")
        self.app.toggle_checkbox("channel_2002")
        self.assertEqual(len(self.app.checked_channels), 4)

        # Step C: Add all 4 in one operation
        self.app.on_add_checked_to_download_list()
        
        active = self.db.get_active_selections(99999)
        self.assertEqual(len(active), 4)
        active_ids = {s["channel_id"] for s in active}
        self.assertEqual(active_ids, {1001, 1002, 2001, 2002})

        # Active search results (IAS) remain visible
        self.assertEqual(self.app.search_var.get(), "IAS")
        visible = list(self.app.browse_tree.get_children())
        self.assertEqual(len(visible), 2)

    # 8. Temporary checkbox selections are independent from SQLite state
    def test_checkbox_selection_independent_from_sqlite(self):
        self.app.search_var.set("TEST")
        self.app.on_search()

        self.app.toggle_checkbox("channel_1001")
        self.app.toggle_checkbox("channel_1002")

        # SQLite must still have 0 selections
        active = self.db.get_active_selections(99999)
        self.assertEqual(len(active), 0)

    # 9. TreeView refresh preserves the search query
    def test_refresh_browse_tree_preserves_query(self):
        self.app.search_var.set("Civil")
        self.app.on_search()
        self.assertEqual(len(self.app.browse_tree.get_children()), 1)

        # Calling refresh_browse_tree without arguments preserves "Civil" filter
        self.app.refresh_browse_tree()
        self.assertEqual(self.app.search_var.get(), "Civil")
        self.assertEqual(len(self.app.browse_tree.get_children()), 1)

    # 10. Single-channel Add action does not clear filter
    def test_single_channel_add_does_not_clear_filter(self):
        self.app.search_var.set("TEST")
        self.app.on_search()

        # Highlight row and call on_add_selected_from_browse
        self.app.browse_tree.selection_set("channel_1003")
        self.app.on_add_selected_from_browse()

        self.assertEqual(self.app.search_var.get(), "TEST")
        visible = list(self.app.browse_tree.get_children())
        self.assertEqual(len(visible), 5)
        self.assertTrue(self.db.is_selected(99999, 1003, "channel"))

    # 11. Removing a channel from Download List does not clear search
    def test_remove_channel_does_not_clear_search(self):
        self.db.add_selection(99999, 1001, "TEST Alpha")
        self.app.search_var.set("TEST")
        self.app.on_search()

        self.app.browse_tree.selection_set("channel_1001")
        self.app.on_remove_selected_from_browse()

        self.assertEqual(self.app.search_var.get(), "TEST")
        self.assertFalse(self.db.is_selected(99999, 1001, "channel"))

    # 12. Search with zero matching rows does not affect saved selections
    def test_zero_matching_search_does_not_affect_db(self):
        self.db.add_selection(99999, 1001, "Alpha")
        self.app.search_var.set("NonExistentXYZKeyword")
        self.app.on_search()

        visible = list(self.app.browse_tree.get_children())
        self.assertEqual(len(visible), 0)

        active = self.db.get_active_selections(99999)
        self.assertEqual(len(active), 1)

    # 13. Same numeric ID with different peer types handled correctly
    def test_same_numeric_id_different_peer_types(self):
        # 1004 is a chat in dialogs_dataset
        self.app.checked_channels.add(("channel", 1004))
        self.app.checked_channels.add(("chat", 1004))
        self.app.on_add_checked_to_download_list()

        active = self.db.get_active_selections(99999)
        self.assertEqual(len(active), 2)
        peer_types = {s["peer_type"] for s in active}
        self.assertEqual(peer_types, {"channel", "chat"})

    # 14. Partial database failure reports which additions succeeded and keeps failed checked
    def test_partial_database_failure_retains_failed_checks(self):
        self.app.checked_channels.add(("channel", 1001))
        self.app.checked_channels.add(("channel", 1002))

        # Mock add_selection to fail for 1002
        real_add = self.db.add_selection
        def mock_add(*args, **kwargs):
            if kwargs.get("channel_id") == 1002 or (len(args) > 2 and args[2] == 1002):
                raise sqlite3.OperationalError("Simulated disk error on 1002")
            return real_add(*args, **kwargs)

        with patch.object(self.db, "add_selection", side_effect=mock_add):
            with patch("tkinter.messagebox.showwarning"):
                self.app.on_add_checked_to_download_list()

        # 1001 succeeded and was removed from checked_channels
        self.assertTrue(self.db.is_selected(99999, 1001, "channel"))
        self.assertNotIn(("channel", 1001), self.app.checked_channels)

        # 1002 failed and REMAINS in checked_channels for retry
        self.assertFalse(self.db.is_selected(99999, 1002, "channel"))
        self.assertIn(("channel", 1002), self.app.checked_channels)

    # 15. Spacebar toggles highlighted rows
    def test_spacebar_toggles_highlighted_rows(self):
        self.app.refresh_browse_tree()
        self.app.browse_tree.selection_set("channel_1001")
        self.app.on_space_toggle(None)
        self.assertIn(("channel", 1001), self.app.checked_channels)

        # Press space again to untoggle
        self.app.on_space_toggle(None)
        self.assertNotIn(("channel", 1001), self.app.checked_channels)

if __name__ == "__main__":
    unittest.main()
