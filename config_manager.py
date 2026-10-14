import os
import json
import shutil
import logging
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "download_directory": "",  # Must be set by user or config; empty requires explicit configuration
    "min_free_space_gb": 5.0,
    "scan_interval_minutes": 15,
    "active_channels": [],     # List of int channel IDs
    "media_filters": {
        "PDFs": True,
        "Videos": False,
        "Photos": False,
        "Voice/Audio": False,
        "Documents (Other)": False,
        "All Media": False
    },
    "cutoff_date": "",
    "concurrent_downloads": 2,
    "organize_forum_topics": True
}

class StorageError(Exception):
    """Raised when destination storage is missing, read-only, or has insufficient space."""
    pass

class ConfigManager:
    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config_path = Path(config_path)
        self.config = self.load_config()

    def load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return dict(DEFAULT_CONFIG)
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge with defaults
            merged = dict(DEFAULT_CONFIG)
            for k, v in data.items():
                if isinstance(v, dict) and k in merged and isinstance(merged[k], dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
            return merged
        except Exception as e:
            logger.error(f"Error loading {self.config_path}: {e}")
            return dict(DEFAULT_CONFIG)

    def save_config(self, updates: Dict[str, Any] = None):
        if updates:
            self.config.update(updates)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = f"{self.config_path}.tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
            os.replace(temp_file, self.config_path)
        except Exception as e:
            logger.error(f"Error saving config to {self.config_path}: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

    def get_download_dir(self) -> Path:
        raw = self.config.get("download_directory", "").strip()
        if not raw:
            raise StorageError("Download directory is not configured. Please select a destination drive.")
        return Path(raw)

    def validate_storage(self, custom_path: str = None) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates target storage accessibility, write permissions, and disk space.
        Returns: (is_valid, status_message, disk_metrics)
        """
        target_str = custom_path if custom_path is not None else self.config.get("download_directory", "")
        target_str = str(target_str).strip()
        if not target_str:
            return False, "No storage directory configured.", {}

        target_path = Path(target_str)

        # 1. Check path accessibility
        try:
            target_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, f"Storage unreachable or path invalid: {e}", {}

        # 2. Verify write capability via secure unique temporary probe file
        probe_file_path = None
        try:
            fd, probe_file_path = tempfile.mkstemp(
                prefix=f".probe_{os.getpid()}_{int(time.time() * 1000)}_",
                suffix=".tmp",
                dir=str(target_path)
            )
            try:
                with os.fdopen(fd, "w") as f:
                    f.write("probe")
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        except Exception as e:
            return False, f"Storage is READ-ONLY or permission denied: {e}", {}
        finally:
            if probe_file_path and os.path.exists(probe_file_path):
                try:
                    os.unlink(probe_file_path)
                except OSError as e:
                    logger.warning(f"Could not remove probe file {probe_file_path}: {e}")

        # 3. Query disk space using shutil.disk_usage
        try:
            usage = shutil.disk_usage(str(target_path))
            total_gb = usage.total / (1024 ** 3)
            used_gb = usage.used / (1024 ** 3)
            free_gb = usage.free / (1024 ** 3)
            metrics = {
                "total_gb": round(total_gb, 2),
                "used_gb": round(used_gb, 2),
                "free_gb": round(free_gb, 2),
                "total_bytes": usage.total,
                "free_bytes": usage.free
            }
        except Exception as e:
            return False, f"Could not determine disk usage: {e}", {}

        min_free = float(self.config.get("min_free_space_gb", 5.0))
        if free_gb < min_free:
            return False, f"Low disk space: {free_gb:.2f} GB free (Minimum required: {min_free:.1f} GB)", metrics

        msg = f"Ready. Free: {free_gb:.2f} GB / {total_gb:.2f} GB"
        return True, msg, metrics

    def check_capacity_for_file(self, file_size_bytes: int) -> bool:
        """
        Ensures destination has enough capacity for file_size + safety margin.
        NEVER silently falls back to C:.
        """
        valid, msg, metrics = self.validate_storage()
        if not valid:
            raise StorageError(f"Storage validation failed: {msg}")

        free_bytes = metrics.get("free_bytes", 0)
        min_free_bytes = int(float(self.config.get("min_free_space_gb", 5.0)) * (1024 ** 3))
        if free_bytes - file_size_bytes < min_free_bytes:
            raise StorageError(
                f"Insufficient space on destination. Free: {free_bytes / (1024**3):.2f} GB, "
                f"Required: {file_size_bytes / (1024**3):.2f} GB with safety buffer."
            )
        return True
