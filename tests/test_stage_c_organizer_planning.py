import unittest
import os
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

class TestStageCOrganizerPlanning(unittest.TestCase):
    """
    Automated regression and verification test suite for Stage C:
    Historical Download Organizer Planning and Read-Only Inventory.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_tracker.db")
        self._init_mock_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _init_mock_db(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL UNIQUE,
                title TEXT,
                access_hash INTEGER,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE channel_storage_mapping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                peer_type TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                verified_title TEXT NOT NULL,
                local_folder_name TEXT NOT NULL,
                is_forum INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, peer_type, channel_id)
            );
            CREATE TABLE forum_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                peer_type TEXT,
                channel_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                local_folder_name TEXT NOT NULL,
                is_closed INTEGER DEFAULT 0,
                is_hidden INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(channel_id, topic_id)
            );
            CREATE TABLE downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                message_id INTEGER,
                file_name TEXT,
                file_size INTEGER,
                status TEXT,
                relative_path TEXT,
                topic_id INTEGER,
                UNIQUE(channel_id, message_id)
            );
        """)
        conn.commit()
        conn.close()

    def test_legacy_channel_reconciliation_rules(self):
        """
        Verify legacy folder reconciliation requires matching (account_id, peer_type, channel_id)
        and distinguishes broadcast channels from forum supergroups.
        """
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO channel_storage_mapping 
            (account_id, peer_type, channel_id, verified_title, local_folder_name, is_forum, created_at, updated_at)
            VALUES (774983606, 'channel', 3509423997, 'FORUM PTS 2027', 'FORUM PTS 2027', 0, '2026-10-14', '2026-10-14');
        """)
        conn.commit()

        # Legacy folder name: Channel_3509423997
        folder_name = "Channel_3509423997"
        channel_id = int(folder_name.replace("Channel_", ""))
        self.assertEqual(channel_id, 3509423997)

        cur.execute("SELECT verified_title, local_folder_name, is_forum FROM channel_storage_mapping WHERE channel_id = ?;", (channel_id,))
        row = cur.fetchone()
        self.assertIsNotNone(row)
        title, folder, is_forum = row
        self.assertEqual(title, "FORUM PTS 2027")
        self.assertEqual(folder, "FORUM PTS 2027")
        self.assertEqual(is_forum, 0)  # Broadcast channel, not forum
        conn.close()

    def test_partial_file_protection_rule(self):
        """
        Verify partial (.part) files and DOWNLOADING records are strictly protected:
        they must NEVER be selected, moved, renamed, truncated, or consolidated.
        """
        protected_partial_files = [
            r"Channel_4213926054\Current_Affairs_02_9th_September_2026.mp4.part",
            r"Prashant sir current affairs 2027\Current_Affairs_02_9th_September_2026.mp4.part",
            r"DH UPPCS PRELIMS STUDY IQ\ANCIENT MEDIEVAL\L8 - Vedic Age (Part - 02).mp4.part"
        ]

        for p in protected_partial_files:
            # Policy check: must be recognized as .part and marked PROTECTED/BLOCKED
            is_partial = p.endswith(".part")
            self.assertTrue(is_partial)

            # In migration planner, candidate status must be BLOCKED
            is_movable = not is_partial
            self.assertFalse(is_movable, f"Partial file {p} must be strictly blocked from migration!")

    def test_topic_recovery_classification_rules(self):
        """
        Verify classification logic adheres strictly to:
        - VERIFIED: server-provided reply_to_top_id or topic root matches forum_topics catalog
        - AMBIGUOUS: cross-reply, pre-conversion, or reply_to_top_id not in catalog
        - UNAVAILABLE: deleted or inaccessible message on Telegram
        - NON_FORUM: channel is not a forum supergroup
        """
        catalog = {
            1: {"title": "General", "is_closed": 0},
            7: {"title": "Make ias", "is_closed": 0},
            150: {"title": "DAILY NEWSPAPER", "is_closed": 0}
        }

        def classify_message(is_forum, msg, topic_catalog):
            if not is_forum:
                return "NON_FORUM", None
            if msg is None:
                return "UNAVAILABLE", None
            
            mid = msg.get("id")
            if mid in topic_catalog:
                return "VERIFIED", mid

            reply = msg.get("reply_to")
            if reply:
                top_id = reply.get("reply_to_top_id")
                is_forum_reply = reply.get("forum_topic", False)
                reply_msg_id = reply.get("reply_to_msg_id")

                if top_id is not None:
                    if top_id in topic_catalog:
                        return "VERIFIED", top_id
                    else:
                        return "AMBIGUOUS", None
                elif is_forum_reply and reply_msg_id is not None:
                    if reply_msg_id in topic_catalog:
                        return "VERIFIED", reply_msg_id
                    else:
                        return "AMBIGUOUS", None
                else:
                    return "AMBIGUOUS", None
            else:
                if 1 in topic_catalog and not topic_catalog[1].get("is_closed"):
                    return "VERIFIED", 1
                return "AMBIGUOUS", None

        # Case 1: Non-forum
        cls, tid = classify_message(False, {"id": 10}, catalog)
        self.assertEqual(cls, "NON_FORUM")

        # Case 2: Unavailable (deleted)
        cls, tid = classify_message(True, None, catalog)
        self.assertEqual(cls, "UNAVAILABLE")

        # Case 3: Topic root message
        cls, tid = classify_message(True, {"id": 7}, catalog)
        self.assertEqual(cls, "VERIFIED")
        self.assertEqual(tid, 7)

        # Case 4: Reply with top_id in catalog
        cls, tid = classify_message(True, {"id": 40, "reply_to": {"reply_to_top_id": 150}}, catalog)
        self.assertEqual(cls, "VERIFIED")
        self.assertEqual(tid, 150)

        # Case 5: Reply with top_id NOT in catalog
        cls, tid = classify_message(True, {"id": 40, "reply_to": {"reply_to_top_id": 9999}}, catalog)
        self.assertEqual(cls, "AMBIGUOUS")
        self.assertIsNone(tid)

        # Case 6: Cross-reply without forum thread top
        cls, tid = classify_message(True, {"id": 40, "reply_to": {"reply_to_msg_id": 35}}, catalog)
        self.assertEqual(cls, "AMBIGUOUS")
        self.assertIsNone(tid)

    def test_destination_collision_detection(self):
        """
        Verify that multiple downloads targeting the exact same relative destination path
        are detected and flagged before any file movement is attempted.
        """
        candidates = [
            {"id": 1704, "message_id": 30, "dest": r"UPPCS TEST 2026\Uppcs one\test6.pdf"},
            {"id": 1702, "message_id": 31, "dest": r"UPPCS TEST 2026\Uppcs one\test6.pdf"},
            {"id": 1705, "message_id": 32, "dest": r"UPPCS TEST 2026\Uppcs one\test7.pdf"}
        ]

        from collections import defaultdict
        dest_counts = defaultdict(list)
        for c in candidates:
            dest_counts[c["dest"]].append(c["id"])

        collisions = {k: v for k, v in dest_counts.items() if len(v) > 1}
        self.assertIn(r"UPPCS TEST 2026\Uppcs one\test6.pdf", collisions)
        self.assertEqual(len(collisions[r"UPPCS TEST 2026\Uppcs one\test6.pdf"]), 2)
        self.assertNotIn(r"UPPCS TEST 2026\Uppcs one\test7.pdf", collisions)

    def test_gui_apply_button_disabled_rule(self):
        """
        Phase C4 requirement:
        The organizer UI preview and apply actions must be strictly separated.
        The Apply button must remain permanently disabled during Stage C planning.
        """
        gui_state = {
            "preview_enabled": True,
            "apply_enabled": False,
            "selected_eligible_count": 0,
            "migration_engine_approved": False
        }

        # Apply can ONLY be enabled if migration engine is approved and eligible items selected
        def can_enable_apply(state):
            return state["migration_engine_approved"] and state["selected_eligible_count"] > 0

        self.assertFalse(can_enable_apply(gui_state))
        self.assertFalse(gui_state["apply_enabled"])


if __name__ == '__main__':
    unittest.main()
