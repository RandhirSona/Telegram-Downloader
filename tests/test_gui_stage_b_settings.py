import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

from config_manager import ConfigManager
from db_tracker import DownloadTrackerDB
from telegram_engine import TelegramDownloaderEngine
from telethon.tl.types import Channel

class TestGUIStageBSettingsIntegration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.download_dir = Path(self.tmpdir) / "Downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = Path(self.tmpdir) / "config.json"
        self.db_path = Path(self.tmpdir) / "test.db"

        self.cfg = ConfigManager(str(self.config_path))
        self.cfg.config["download_directory"] = str(self.download_dir)
        self.cfg.config["min_free_space_gb"] = 0.001
        self.cfg.config["organize_forum_topics"] = True
        self.cfg.save_config()

        self.db = DownloadTrackerDB(str(self.db_path))
        self.db.migrate_to_stage_b()

        self.engine = TelegramDownloaderEngine(self.cfg, self.db)
        self.engine.account_id = 123456789

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_01_setting_loads_from_config(self):
        # Default loads True
        self.assertTrue(self.cfg.config.get("organize_forum_topics", False))

        # Explicitly set False and verify reload
        self.cfg.save_config({"organize_forum_topics": False})
        reloaded_cfg = ConfigManager(str(self.config_path))
        self.assertFalse(reloaded_cfg.config.get("organize_forum_topics", True))

    def test_02_setting_toggle_persists_across_restarts(self):
        # Simulate GUI toggle to True
        self.cfg.save_config({"organize_forum_topics": True})
        cfg2 = ConfigManager(str(self.config_path))
        self.assertTrue(cfg2.config.get("organize_forum_topics", False))

        # Simulate GUI toggle to False
        self.cfg.save_config({"organize_forum_topics": False})
        cfg3 = ConfigManager(str(self.config_path))
        self.assertFalse(cfg3.config.get("organize_forum_topics", True))

    def test_03_ordinary_broadcast_channel_retains_flat_directory(self):
        # Even with organize_forum_topics = True, a non-forum channel routes flat
        self.cfg.save_config({"organize_forum_topics": True})
        
        target_path, rel_path, eff_tid, eff_ttitle = self.engine.resolve_destination_path(
            channel_id=3683278928,
            channel_name="PVT UPPCS TEST",
            raw_filename="Test_01.pdf",
            message_id=50,
            is_forum=False,
            topic_id=None,
            topic_title=None
        )
        self.assertIsNone(eff_tid)
        self.assertIsNone(eff_ttitle)
        self.assertEqual(target_path, self.download_dir / "PVT UPPCS TEST" / "Test_01.pdf")
        self.assertEqual(rel_path, str(Path("PVT UPPCS TEST") / "Test_01.pdf"))

    def test_04_forum_supergroup_routes_to_subfolder_when_enabled(self):
        self.cfg.save_config({"organize_forum_topics": True})

        target_path, rel_path, eff_tid, eff_ttitle = self.engine.resolve_destination_path(
            channel_id=4450272939,
            channel_name="Premium 2027",
            raw_filename="Newspaper.pdf",
            message_id=100,
            is_forum=True,
            topic_id=150,
            topic_title="DAILY NEWSPAPER"
        )
        self.assertEqual(eff_tid, 150)
        self.assertEqual(eff_ttitle, "DAILY NEWSPAPER")
        self.assertEqual(target_path, self.download_dir / "Premium 2027" / "DAILY NEWSPAPER" / "Newspaper.pdf")
        self.assertEqual(rel_path, str(Path("Premium 2027") / "DAILY NEWSPAPER" / "Newspaper.pdf"))

    def test_05_forum_supergroup_falls_back_to_flat_when_disabled(self):
        # When organize_forum_topics = False, forum supergroups route flat
        self.cfg.save_config({"organize_forum_topics": False})

        target_path, rel_path, eff_tid, eff_ttitle = self.engine.resolve_destination_path(
            channel_id=4450272939,
            channel_name="Premium 2027",
            raw_filename="Newspaper.pdf",
            message_id=100,
            is_forum=True,
            topic_id=150,
            topic_title="DAILY NEWSPAPER"
        )
        self.assertIsNone(eff_tid)
        self.assertIsNone(eff_ttitle)
        self.assertEqual(target_path, self.download_dir / "Premium 2027" / "Newspaper.pdf")
        self.assertEqual(rel_path, str(Path("Premium 2027") / "Newspaper.pdf"))

if __name__ == "__main__":
    unittest.main()
