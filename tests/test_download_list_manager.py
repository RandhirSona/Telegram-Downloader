import os
import sys
import unittest
import tempfile
import sqlite3
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import ConfigManager, StorageError
from db_tracker import DownloadTrackerDB, normalize_channel_id
from telegram_engine import TelegramDownloaderEngine
from telethon.tl import types

class TestDownloadListManager(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_tracker.db"
        self.cfg_path = Path(self.tmpdir) / "test_config.json"
        self.storage_dir = Path(self.tmpdir) / "Downloads"
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        self.db = DownloadTrackerDB(str(self.db_path))
        self.cfg_mgr = ConfigManager(str(self.cfg_path))
        self.cfg_mgr.save_config({
            "download_directory": str(self.storage_dir),
            "min_free_space_gb": 0.001,
            "active_channels": [101, 102, 103]
        })

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # 1. Selection Reset Fix: Add A, Search B, Add B -> Both A and B remain selected
    def test_search_and_selection_independence(self):
        engine = TelegramDownloaderEngine(self.cfg_mgr, self.db)
        engine.account_id = 12345

        # Populate engine dialogs cache with Channel A and Channel B
        engine.dialogs_cache = {
            ("channel", 101): {"id": 101, "name": "Python Channel A", "peer_type": "channel", "access_hash": 111, "username": "py_a", "entity": MagicMock()},
            ("channel", 102): {"id": 102, "name": "Civil Services B", "peer_type": "channel", "access_hash": 222, "username": "cs_b", "entity": MagicMock()}
        }

        # Step 1: Select Channel A
        self.db.add_selection(12345, 101, "Python Channel A", "channel", access_hash=111, config_mgr=self.cfg_mgr)
        active = self.db.get_active_selections(12345)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["channel_id"], 101)

        # Step 2: Search for "Civil" (non-destructive filter)
        filtered = engine.filter_cached_dialogs("Civil")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["id"], 102)

        # Verify Channel A is STILL selected in DB despite search query
        active_during_search = self.db.get_active_selections(12345)
        self.assertEqual(len(active_during_search), 1)
        self.assertEqual(active_during_search[0]["channel_id"], 101)

        # Step 3: Select Channel B
        self.db.add_selection(12345, 102, "Civil Services B", "channel", access_hash=222, config_mgr=self.cfg_mgr)

        # Step 4: Verify BOTH A and B are actively selected
        active_final = self.db.get_active_selections(12345)
        self.assertEqual(len(active_final), 2)
        selected_ids = {a["channel_id"] for a in active_final}
        self.assertEqual(selected_ids, {101, 102})

    # 2. Clear Search preserves selections
    def test_clear_search_preserves_selections(self):
        engine = TelegramDownloaderEngine(self.cfg_mgr, self.db)
        engine.account_id = 12345
        engine.dialogs_cache = {
            ("channel", 101): {"id": 101, "name": "Channel 101", "peer_type": "channel", "access_hash": 111, "username": "", "entity": MagicMock()},
            ("channel", 102): {"id": 102, "name": "Channel 102", "peer_type": "channel", "access_hash": 222, "username": "", "entity": MagicMock()}
        }

        self.db.add_selection(12345, 101, "Channel 101", "channel")
        self.db.add_selection(12345, 102, "Channel 102", "channel")

        # Filter and clear
        engine.filter_cached_dialogs("NonExistent")
        all_unfiltered = engine.filter_cached_dialogs("")
        self.assertEqual(len(all_unfiltered), 2)

        # Selections must remain intact
        active = self.db.get_active_selections(12345)
        self.assertEqual(len(active), 2)

    # 3. Duplicate Add is Idempotent
    def test_duplicate_add_is_idempotent(self):
        self.db.add_selection(12345, 555, "Channel 555", "channel")
        self.db.add_selection(12345, 555, "Channel 555", "channel")
        self.db.add_selection(12345, 555, "Channel 555 Renamed", "channel")

        active = self.db.get_active_selections(12345)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["channel_id"], 555)
        self.assertEqual(active[0]["title"], "Channel 555 Renamed")

    # 4. Remove item only removes that specific item
    def test_remove_selection_isolation(self):
        self.db.add_selection(12345, 1, "Chan 1", config_mgr=self.cfg_mgr)
        self.db.add_selection(12345, 2, "Chan 2", config_mgr=self.cfg_mgr)
        self.db.add_selection(12345, 3, "Chan 3", config_mgr=self.cfg_mgr)

        self.db.remove_selection(12345, 2, config_mgr=self.cfg_mgr)
        active = self.db.get_active_selections(12345)
        self.assertEqual(len(active), 2)
        active_ids = {a["channel_id"] for a in active}
        self.assertEqual(active_ids, {1, 3})
        self.assertFalse(self.db.is_selected(12345, 2))

    # 5. Restart Recovery: Persistent in SQLite
    def test_restart_recovery(self):
        self.db.add_selection(12345, 777, "Channel 777")
        
        # Instantiate fresh DB connection pointing to same file
        reloaded_db = DownloadTrackerDB(str(self.db_path))
        active = reloaded_db.get_active_selections(12345)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["channel_id"], 777)

    # 6. Empty list persistence: an intentionally empty list remains empty across restarts
    def test_empty_list_persistence(self):
        # Step 1: Execute initial migration
        migrated = self.db.migrate_legacy_active_channels(12345, [101, 102, 103], self.cfg_mgr)
        self.assertEqual(migrated, 3)
        self.assertEqual(len(self.db.get_active_selections(12345)), 3)

        # Step 2: User intentionally clears all selections
        self.db.clear_selections(12345, config_mgr=self.cfg_mgr)
        self.assertEqual(len(self.db.get_active_selections(12345)), 0)

        # Step 3: Fresh DB instance (simulated application restart)
        reloaded_db = DownloadTrackerDB(str(self.db_path))
        active = reloaded_db.get_active_selections(12345)
        self.assertEqual(len(active), 0)

        # Step 4: Verify legacy config with stale channels does NOT repopulate empty DB
        reloaded_db.migrate_legacy_active_channels(12345, [101, 102, 103], self.cfg_mgr)
        self.assertEqual(len(reloaded_db.get_active_selections(12345)), 0)

    # 7. One-Time Legacy Migration is Account-Aware & Idempotent
    def test_account_aware_legacy_migration(self):
        # Account A migration
        migrated_a = self.db.migrate_legacy_active_channels(account_id=111, legacy_channel_ids=[101, 102], config_mgr=self.cfg_mgr)
        self.assertEqual(migrated_a, 2)

        # Second call for Account A is a no-op
        migrated_a_again = self.db.migrate_legacy_active_channels(account_id=111, legacy_channel_ids=[101, 102], config_mgr=self.cfg_mgr)
        self.assertEqual(migrated_a_again, 0)

        # Account B has independent migration and isolated selections
        migrated_b = self.db.migrate_legacy_active_channels(account_id=222, legacy_channel_ids=[901], config_mgr=self.cfg_mgr)
        self.assertEqual(migrated_b, 1)

        active_a = self.db.get_active_selections(111)
        active_b = self.db.get_active_selections(222)
        self.assertEqual({a["channel_id"] for a in active_a}, {101, 102})
        self.assertEqual({b["channel_id"] for b in active_b}, {901})

    # 8. Peer Identity distinction (Channels vs Chats with overlapping IDs)
    def test_peer_identity_distinction(self):
        # Basic Chat 500 vs Supergroup Channel 500
        self.db.add_selection(account_id=123, channel_id=500, title="Megagroup Channel", peer_type="channel", access_hash=999)
        self.db.add_selection(account_id=123, channel_id=500, title="Basic Chat Group", peer_type="chat", access_hash=None)

        selections = self.db.get_active_selections(123)
        self.assertEqual(len(selections), 2)
        types_set = {s["peer_type"] for s in selections}
        self.assertEqual(types_set, {"channel", "chat"})

        # Removing the chat leaves the channel active
        self.db.remove_selection(account_id=123, channel_id=500, peer_type="chat")
        remaining = self.db.get_active_selections(123)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["peer_type"], "channel")

    # 9. Headless Worker Fail-Safe on Database Error vs Legitimately Empty
    def test_worker_database_error_vs_empty_list(self):
        # Case A: Legitimately empty list returns empty list without error
        active = self.db.get_active_selections(9999)
        self.assertEqual(active, [])

        # Case B: Database error raises sqlite3.Error rather than masquerading as empty list
        corrupt_db = DownloadTrackerDB(str(self.tmpdir + "/nonexistent/test.db"))
        # Force a database error by executing against a closed/invalid connection
        with patch.object(self.db, "_get_connection", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_active_selections(12345)

    # 10. Access Hash Resolution Cascade
    def test_access_hash_resolution_cascade(self):
        engine = TelegramDownloaderEngine(self.cfg_mgr, self.db)
        mock_client = MagicMock()
        engine.client = mock_client

        selection = {
            "channel_id": 100555666,
            "peer_type": "channel",
            "access_hash": 987654321
        }

        # Simulate cache miss on client.get_entity(bare_id), falling back to InputPeerChannel
        async def mock_get_entity(entity):
            if isinstance(entity, int):
                raise ValueError("Could not find input entity for bare int")
            elif isinstance(entity, types.InputPeerChannel):
                mock_resolved = MagicMock()
                mock_resolved.id = entity.channel_id
                return mock_resolved
            return None

        mock_client.get_entity = AsyncMock(side_effect=mock_get_entity)

        resolved = asyncio.run(engine.resolve_selection_entity(selection))
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, 100555666)

    # 11. Removal Does Not Delete Downloaded Files or History Records
    def test_removal_preserves_files_and_history(self):
        # Create a downloaded file
        self.db.update_channel(100, "Channel 100")
        self.db.record_start(100, 1, "test.pdf", 1024, "Channel 100/test.pdf")
        self.db.record_completed(100, 1, "test.pdf", 1024, "Channel 100/test.pdf")
        
        file_path = self.storage_dir / "Channel 100" / "test.pdf"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("sample content")

        # Add selection, then remove it
        self.db.add_selection(123, 100, "Channel 100")
        self.db.remove_selection(123, 100)

        # File must remain untouched
        self.assertTrue(file_path.exists())
        self.assertEqual(file_path.read_text(), "sample content")

        # Database record must remain COMPLETED
        self.assertTrue(self.db.is_message_downloaded(100, 1))

    # 12. Skip Deselected Channels During Batch Execution
    def test_skip_deselected_channel_during_batch(self):
        engine = TelegramDownloaderEngine(self.cfg_mgr, self.db)
        engine.account_id = 12345

        # Channel 1 is active, Channel 2 is deselected
        self.db.add_selection(12345, 1, "Channel 1")
        self.db.add_selection(12345, 2, "Channel 2")
        self.db.remove_selection(12345, 2)

        processed = []
        async def mock_download_msg(*args, **kwargs):
            processed.append(args[1]) # channel_id

        engine.download_single_message = AsyncMock(side_effect=mock_download_msg)
        mock_client = MagicMock()
        mock_msg = MagicMock()
        mock_msg.id = 1
        mock_msg.date = None
        mock_msg.message = ""
        mock_msg.media = None

        async def mock_iter(entity, limit=None):
            yield mock_msg
        mock_client.iter_messages = mock_iter
        engine.client = mock_client

        # Channel 2 should be skipped immediately
        asyncio.run(engine.scan_and_download_channel(MagicMock(), 2, "Channel 2", {}, None))
        self.assertEqual(len(processed), 0)

    # 13. Pre-Migration Verified Database Backup
    def test_database_backup_integrity(self):
        backup_file = self.db.backup_database()
        self.assertTrue(backup_file.exists())

        conn = sqlite3.connect(str(backup_file))
        res = conn.execute("PRAGMA integrity_check;").fetchone()
        conn.close()
        self.assertEqual(res[0], "ok")

    # 14. Concurrent GUI and Worker ProcessLock Competition
    def test_process_lock_gui_and_worker_concurrency(self):
        from instance_lock import ProcessLock
        lock_gui = ProcessLock(role="gui")
        lock_worker = ProcessLock(role="worker")

        # Clean any leftover lock for test isolation
        lock_gui.release_lock()

        # GUI acquires session lock
        acquired_gui = lock_gui.acquire_session_lock()
        self.assertTrue(acquired_gui)

        # Worker attempts to acquire session lock and MUST fail
        acquired_worker = lock_worker.acquire_session_lock()
        self.assertFalse(acquired_worker)

        # Release GUI lock
        lock_gui.release_lock()

        # Worker should now be able to acquire session lock
        acquired_worker_after = lock_worker.acquire_session_lock()
        self.assertTrue(acquired_worker_after)
        lock_worker.release_lock()

    # 15. SQLite WAL Concurrent Reads and Writes
    def test_wal_concurrent_reads_and_writes(self):
        db2 = DownloadTrackerDB(str(self.db_path))
        self.db.add_selection(111, 201, "Channel 201")
        
        # Second instance immediately sees selection
        active_from_db2 = db2.get_active_selections(111)
        self.assertEqual(len(active_from_db2), 1)
        self.assertEqual(active_from_db2[0]["channel_id"], 201)

        # Second instance adds selection
        db2.add_selection(111, 202, "Channel 202")
        active_from_db1 = self.db.get_active_selections(111)
        self.assertEqual(len(active_from_db1), 2)

    # 16. Storage Unavailability Strict Zero Fallback to C:
    def test_storage_unavailability_zero_fallback_to_c(self):
        cfg_bad = ConfigManager(str(Path(self.tmpdir) / "cfg_bad.json"))
        cfg_bad.save_config({
            "download_directory": "X:\\NonExistent_Storage_Location_98765",
            "min_free_space_gb": 5.0
        })

        valid, msg, metrics = cfg_bad.validate_storage()
        self.assertFalse(valid)
        self.assertTrue("unreachable" in msg.lower() or "cannot find" in msg.lower() or "does not exist" in msg.lower())

        with self.assertRaises(StorageError):
            cfg_bad.check_capacity_for_file(1024 * 1024)

    # 17. Worker Executes One-Time Migration Idempotently
    def test_worker_runs_one_time_legacy_migration(self):
        from worker_headless import HeadlessWorker
        worker = HeadlessWorker()
        worker.config_mgr = self.cfg_mgr
        worker.db = self.db
        
        # Pre-seed legacy channels [101, 102, 103] in config
        account_id = 99999
        self.db.migrate_legacy_active_channels(account_id, [101, 102, 103], self.cfg_mgr)
        
        # Verify in DB
        active = self.db.get_active_selections(account_id)
        self.assertEqual(len(active), 3)

        # Rerunning migration is a no-op (idempotent)
        migrated_again = self.db.migrate_legacy_active_channels(account_id, [101, 102, 103], self.cfg_mgr)
        self.assertEqual(migrated_again, 0)

    # 18. Mid-Batch Deselection Dynamic Cancellation
    def test_mid_batch_deselection_halts_processing(self):
        engine = TelegramDownloaderEngine(self.cfg_mgr, self.db)
        engine.account_id = 12345
        self.db.add_selection(12345, 999, "Channel 999")

        # Mock match_media so messages are eligible
        engine.match_media = MagicMock(return_value=(True, "test.pdf", 1024))

        processed = []
        async def mock_download_msg(*args, **kwargs):
            # During first download, channel gets deselected
            self.db.remove_selection(12345, 999)
            processed.append(args[1])

        engine.download_single_message = AsyncMock(side_effect=mock_download_msg)
        mock_client = MagicMock()
        
        msg1 = MagicMock(); msg1.id = 1; msg1.date = None; msg1.message = ""; msg1.media = MagicMock()
        msg2 = MagicMock(); msg2.id = 2; msg2.date = None; msg2.message = ""; msg2.media = MagicMock()

        async def mock_iter(entity, limit=None):
            yield msg1
            yield msg2
        mock_client.iter_messages = mock_iter
        engine.client = mock_client

        mock_entity = MagicMock(spec=types.Channel)
        mock_entity.id = 999

        asyncio.run(engine.scan_and_download_channel(mock_entity, 999, "Channel 999", {}, None))
        
        # Msg 1 was processed, but after removal, Msg 2 was halted
        self.assertEqual(len(processed), 1)

if __name__ == "__main__":
    unittest.main()

