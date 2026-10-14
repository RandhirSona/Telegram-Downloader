import os
import re
import json
import shutil
import logging
import tempfile
import time
from pathlib import Path
from typing import Dict, Any, Tuple, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "download_directory": "",  # Must be set by user or config; empty requires explicit configuration
    "historical_download_directories": [],  # Prior storage roots retained for lookup
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

def validate_windows_path(path_str: str) -> Tuple[bool, str]:
    """
    Validates Windows path syntax, drive specification, illegal characters, and device names.
    Supports local drive paths (e.g. D:\\Media) and UNC network shares (e.g. \\\\server\\share).
    """
    if not path_str or not str(path_str).strip():
        return False, "Download path cannot be empty."
    s = str(path_str).strip()

    # Identify drive or UNC root
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        rest = s[2:]
    elif s.startswith(("\\\\", "//")):
        rest = s[2:]
    else:
        return False, f"Invalid path '{s}'. Must be an absolute path with drive letter (e.g. D:\\) or UNC share (\\\\server\\share)."

    # Forbidden characters in Windows path components: < > : " | ? *
    forbidden = set('<>:"|?*')
    for ch in forbidden:
        if ch in rest:
            return False, f"Invalid character '{ch}' in download directory path."

    # Check component names for Windows reserved device names
    parts = re.split(r'[\\/]+', rest)
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
    }
    for part in parts:
        clean_part = part.strip()
        if not clean_part:
            continue
        stem = clean_part.split('.')[0].upper()
        if stem in reserved:
            return False, f"Path component '{part}' is a Windows reserved device name."

    return True, "Valid Windows path."

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
        """
        Authoritative download storage root resolver.
        Returns Path instance for active configured directory or raises StorageError.
        """
        raw = self.config.get("download_directory", "").strip()
        if not raw:
            raise StorageError("Download directory is not configured. Please select a destination drive.")
        return Path(os.path.normpath(raw))

    def set_download_dir(self, new_path: str, save: bool = True) -> Path:
        """
        Sets active download directory, archiving any previous root into historical roots list.
        """
        target_str = os.path.normpath(str(new_path).strip())
        old_dir = str(self.config.get("download_directory", "")).strip()
        if old_dir:
            old_norm = os.path.normpath(old_dir)
            if old_norm.lower() != target_str.lower():
                hist = list(self.config.get("historical_download_directories", []))
                if not any(os.path.normpath(str(h)).lower() == old_norm.lower() for h in hist):
                    hist.append(old_norm)
                self.config["historical_download_directories"] = hist

        self.config["download_directory"] = target_str
        if save:
            self.save_config()
        return Path(target_str)

    def get_historical_roots(self) -> List[Path]:
        """
        Returns list of historical download roots for backward-compatible lookup.
        """
        roots: List[Path] = []
        for raw in self.config.get("historical_download_directories", []):
            s = str(raw).strip()
            if s:
                p = Path(os.path.normpath(s))
                if p not in roots:
                    roots.append(p)
        return roots

    def validate_storage(self, custom_path: str = None, create_if_missing: bool = False) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates target storage accessibility, write permissions, and disk space.
        Returns: (is_valid, status_message, disk_metrics)
        """
        target_str = custom_path if custom_path is not None else self.config.get("download_directory", "")
        target_str = str(target_str).strip()
        if not target_str:
            return False, "No storage directory configured.", {}

        # 1. Syntax check for Windows path validity
        is_valid_syntax, syntax_msg = validate_windows_path(target_str)
        if not is_valid_syntax:
            return False, syntax_msg, {}

        target_path = Path(os.path.normpath(target_str))

        # 2. Check path existence or safe creation
        if not target_path.exists():
            if create_if_missing:
                try:
                    target_path.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    return False, f"Storage unreachable or path invalid: {e}", {}
            else:
                return False, f"Storage unreachable or path invalid: Storage directory does not exist: {target_path}", {}

        # 3. Verify write capability via secure unique temporary probe file
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

        # 4. Query disk space using shutil.disk_usage
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

    def check_capacity_for_file(self, file_size_bytes: int, custom_path: str = None) -> bool:
        """
        Ensures destination has enough capacity for file_size + safety margin.
        NEVER silently falls back to C:.
        """
        valid, msg, metrics = self.validate_storage(custom_path=custom_path)
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
