import os
import sys
import json
import time
import logging
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

LOCK_FILE = Path("app_data/downloader.lock")
STATUS_FILE = Path("app_data/worker_status.json")

class ProcessLock:
    def __init__(self, role: str = "worker"):
        self.role = role  # "worker" or "gui"
        self.lock_path = LOCK_FILE
        self.status_path = STATUS_FILE
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.is_owner = False
        self._file_handle = None

    def _is_pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if process != 0:
                kernel32.CloseHandle(process)
                return True
            return False
        else:
            try:
                os.kill(pid, 0)
                return True
            except (OSError, ProcessLookupError):
                return False

    def acquire_session_lock(self) -> bool:
        """
        Attempts to acquire exclusive session ownership for Telethon downloads.
        Returns True if acquired, False if another process holds active ownership.
        """
        now = time.time()
        if self.lock_path.exists():
            try:
                with open(self.lock_path, "r", encoding="utf-8") as f:
                    lock_info = json.load(f)
                existing_pid = lock_info.get("pid", 0)
                last_heartbeat = lock_info.get("last_heartbeat", 0)

                # If process is still alive and heartbeat is recent (<30s)
                if self._is_pid_alive(existing_pid) and (now - last_heartbeat < 30.0):
                    logger.info(f"Lock active by PID {existing_pid} ({lock_info.get('role')})")
                    return False
                else:
                    logger.warning(f"Clearing stale lock from dead/unresponsive PID {existing_pid}")
            except Exception as e:
                logger.warning(f"Corrupt lock file found, will overwrite: {e}")

        # Acquire lock
        lock_data = {
            "pid": os.getpid(),
            "role": self.role,
            "acquired_at": now,
            "last_heartbeat": now
        }
        try:
            with open(self.lock_path, "w", encoding="utf-8") as f:
                json.dump(lock_data, f, indent=2)
            self.is_owner = True
            return True
        except Exception as e:
            logger.error(f"Failed to write lock file: {e}")
            return False

    def update_heartbeat(self):
        if not self.is_owner:
            return
        try:
            now = time.time()
            lock_data = {
                "pid": os.getpid(),
                "role": self.role,
                "last_heartbeat": now
            }
            with open(self.lock_path, "w", encoding="utf-8") as f:
                json.dump(lock_data, f, indent=2)
        except Exception:
            pass

    def release_lock(self):
        if self.is_owner and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except OSError:
                pass
            self.is_owner = False

    def update_status(self, status_dict: Dict[str, Any]):
        """Worker writes live telemetry to worker_status.json for GUI monitoring."""
        if not self.is_owner:
            return
        status_dict["timestamp"] = time.time()
        status_dict["pid"] = os.getpid()
        temp_file = f"{self.status_path}.tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(status_dict, f, indent=2)
            os.replace(temp_file, self.status_path)
            self.update_heartbeat()
        except Exception as e:
            logger.debug(f"Error updating worker status: {e}")

    @classmethod
    def get_active_worker_status(cls) -> Optional[Dict[str, Any]]:
        """Reads worker_status.json if a worker is actively running."""
        if not LOCK_FILE.exists() or not STATUS_FILE.exists():
            return None
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                lock_info = json.load(f)
            pid = lock_info.get("pid", 0)
            role = lock_info.get("role", "")
            last_heartbeat = lock_info.get("last_heartbeat", 0)
            
            # Check if alive and heartbeat fresh within 20s
            if time.time() - last_heartbeat > 20.0:
                return None

            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                status_info = json.load(f)
            status_info["worker_pid"] = pid
            status_info["worker_role"] = role
            return status_info
        except Exception:
            return None
