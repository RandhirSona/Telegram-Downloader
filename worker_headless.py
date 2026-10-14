import os
import sys
import time
import signal
import asyncio
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Explicitly ensure current working directory is the application directory
os.chdir(Path(__file__).parent.resolve())

from config_manager import ConfigManager, StorageError
from db_tracker import DownloadTrackerDB, normalize_channel_id
from instance_lock import ProcessLock
from telegram_engine import TelegramDownloaderEngine

def setup_logging():
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "worker.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Rotating file handler (10 MB per file, keep 5 backups)
    fh = RotatingFileHandler(str(log_file), maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(formatter)
    root_logger.addHandler(fh)

    # Console stdout
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    root_logger.addHandler(ch)

    return root_logger

logger = setup_logging()

class HeadlessWorker:
    def __init__(self):
        self.config_mgr = ConfigManager()
        self.db = DownloadTrackerDB()
        self.lock = ProcessLock(role="worker")
        self.running = True
        self.engine: TelegramDownloaderEngine = None

    def on_engine_event(self, event_type: str, data: any):
        if event_type == "log":
            logger.info(f"[Engine] {data}")
        elif event_type == "status":
            self.lock.update_status({
                "status_text": str(data),
                "is_downloading": self.engine.is_downloading if self.engine else False
            })
        elif event_type == "progress":
            self.lock.update_heartbeat()
        elif event_type == "storage_error":
            logger.critical(f"[Storage] {data}")

    async def run(self):
        # 1. Acquire single-instance lock
        if not self.lock.acquire_session_lock():
            logger.error("Another process (GUI or worker) is already holding the session lock. Exiting worker.")
            return 1

        logger.info("Acquired session lock. Starting headless downloader worker...")

        # Setup engine
        self.engine = TelegramDownloaderEngine(
            config_mgr=self.config_mgr,
            db_tracker=self.db,
            event_callback=self.on_engine_event
        )

        # 2. Check Telegram Authorization
        try:
            is_authed = await self.engine.check_auth()
            if not is_authed:
                logger.error("Session is not authorized. Please log in using the GUI or initial setup first.")
                self.lock.release_lock()
                return 2
        except Exception as e:
            logger.error(f"Error checking Telegram session authorization: {e}")
            self.lock.release_lock()
            return 3

        logger.info("Telegram session verified.")
        self.db.migrate_legacy_history_if_needed()

        # 3. Main Unattended Loop
        while self.running:
            # Storage pre-flight check
            valid, msg, metrics = self.config_mgr.validate_storage()
            if not valid:
                logger.critical(f"Target storage is not accessible: {msg}. Retrying in 60s. ZERO FALLBACK TO C:.")
                self.lock.update_status({
                    "status_text": f"STORAGE ERROR: {msg}",
                    "is_downloading": False
                })
                for _ in range(12):
                    if not self.running: break
                    await asyncio.sleep(5)
                continue

            # Authoritative query from SQLite download_selections by account_id
            account_id = self.engine.account_id
            if account_id is None:
                logger.warning("Telegram account_id not yet resolved. Skipping cycle.")
                await asyncio.sleep(5)
                continue

            # One-time account-aware legacy migration if not yet run
            legacy_ids = self.config_mgr.config.get("active_channels", [])
            if legacy_ids:
                try:
                    self.db.migrate_legacy_active_channels(account_id, legacy_ids, self.config_mgr)
                except Exception as me:
                    logger.warning(f"Error checking legacy active channels migration: {me}")

            try:
                active_selections = self.db.get_active_selections(account_id)
            except (sqlite3.Error, Exception) as dbe:
                logger.critical(
                    f"DATABASE ERROR reading selections: {dbe}. "
                    f"Halting worker pass to protect data integrity. ZERO fallback to legacy config."
                )
                self.lock.update_status({
                    "status_text": f"HALTED: Database error ({dbe})",
                    "is_downloading": False
                })
                # Sleep and retry next cycle
                for _ in range(12):
                    if not self.running: break
                    await asyncio.sleep(5)
                continue

            if not active_selections:
                logger.info(f"Idle. Account {account_id} has 0 channels selected in Download List.")
                self.lock.update_status({
                    "status_text": "Idle. Download list is empty.",
                    "is_downloading": False
                })
            else:
                logger.info(f"Account {account_id}: Found {len(active_selections)} active selections in SQLite. Resolving entities...")
                await self.engine.fetch_dialogs()  # Refresh dialogs catalog in memory
                selected_dialogs = []
                for sel in active_selections:
                    entity = await self.engine.resolve_selection_entity(sel)
                    if entity:
                        selected_dialogs.append({
                            "id": sel["channel_id"],
                            "name": sel["title"],
                            "entity": entity
                        })
                    else:
                        logger.warning(f"Could not resolve Telegram entity for channel {sel['channel_id']} ({sel['title']}).")

                if selected_dialogs:
                    logger.info(f"Starting download pass for {len(selected_dialogs)} resolved channels...")
                    try:
                        await self.engine.run_batch(selected_dialogs)
                    except StorageError as se:
                        logger.critical(f"Storage error during download pass: {se}")
                    except Exception as e:
                        logger.error(f"Error during batch pass: {e}")
                else:
                    logger.warning("No active channels could be resolved to valid Telegram entities.")
                    self.lock.update_status({
                        "status_text": "Idle. Active channels could not be resolved.",
                        "is_downloading": False
                    })

            # Sleep interval before next periodic scan
            interval_min = int(self.config_mgr.config.get("scan_interval_minutes", 15))
            interval_sec = max(60, interval_min * 60)
            logger.info(f"Download cycle completed. Sleeping for {interval_min} minutes until next scan...")
            
            elapsed = 0
            while elapsed < interval_sec and self.running:
                self.lock.update_status({
                    "status_text": f"Sleeping until next scan in {int((interval_sec - elapsed)/60)} min",
                    "is_downloading": False
                })
                await asyncio.sleep(5)
                elapsed += 5

        logger.info("Headless worker shutdown complete.")
        self.lock.release_lock()
        return 0

def main():
    worker = HeadlessWorker()

    def signal_handler(sig, frame):
        logger.info("Termination signal received. Shutting down worker gracefully...")
        worker.running = False
        if worker.engine:
            worker.engine.cancel_flag = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        code = asyncio.run(worker.run())
        sys.exit(code)
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user.")
        worker.lock.release_lock()
        sys.exit(0)

if __name__ == "__main__":
    main()
