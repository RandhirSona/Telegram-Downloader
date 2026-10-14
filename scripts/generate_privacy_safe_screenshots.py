"""
Generate privacy-safe screenshots directly from the actual Tkinter application.
Uses real Tkinter components with strictly sanitized/masked mock data.
All sensitive data (phone, API hash, channel names, real IDs, usernames, private paths) are masked.
Screenshots are saved directly to docs/screenshots/.
"""

import os
import sys
import time
import ctypes
from ctypes import wintypes
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from PIL import Image

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_manager import ConfigManager
from db_tracker import DownloadTrackerDB
from telegram_gui_downloader import TelegramGUIApp

def capture_window_to_image(widget: tk.Widget) -> Image.Image:
    """Capture a Tkinter window or widget using Windows PrintWindow API."""
    widget.deiconify()
    widget.lift()
    widget.focus_force()
    widget.update_idletasks()
    widget.update()
    time.sleep(0.1)
    
    hwnd = widget.winfo_id()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    w = max(rect.right - rect.left, 1)
    h = max(rect.bottom - rect.top, 1)

    hdc_screen = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)

    # PW_RENDERFULLCONTENT = 2
    user32.PrintWindow(hwnd, hdc_mem, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ('biSize', wintypes.DWORD),
            ('biWidth', wintypes.LONG),
            ('biHeight', wintypes.LONG),
            ('biPlanes', wintypes.WORD),
            ('biBitCount', wintypes.WORD),
            ('biCompression', wintypes.DWORD),
            ('biSizeImage', wintypes.DWORD),
            ('biXPelsPerMeter', wintypes.LONG),
            ('biYPelsPerMeter', wintypes.LONG),
            ('biClrUsed', wintypes.DWORD),
            ('biClrImportant', wintypes.DWORD)
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wintypes.DWORD * 3)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)

    im = Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1)
    im = im.convert('RGB')

    # Cleanup GDI
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_screen)
    return im

def strip_metadata_and_save(img: Image.Image, output_path: Path):
    """Save clean PNG without any EXIF or host metadata."""
    clean = Image.new(img.mode, img.size)
    clean.paste(img)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    clean.save(str(output_path), "PNG", optimize=True)
    print(f"Saved: {output_path} ({clean.size[0]}x{clean.size[1]})")

def main():
    import tempfile
    import shutil
    from unittest.mock import MagicMock

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "demo.db"
    cfg_path = Path(tmpdir) / "config.json"
    dest_dir = Path(tmpdir) / "TelegramDownloads"
    dest_dir.mkdir(parents=True, exist_ok=True)

    out_dir = PROJECT_ROOT / "docs" / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    db = DownloadTrackerDB(str(db_path))
    cfg_mgr = ConfigManager(str(cfg_path))
    cfg_mgr.save_config({
        "download_directory": "D:\\TelegramDownloads",
        "min_free_space_gb": 5.0,
        "scan_interval_minutes": 15,
        "concurrent_downloads": 2,
        "organize_forum_topics": True,
        "media_filters": {
            "PDFs": True,
            "Videos": True,
            "Photos": False,
            "Voice/Audio": False,
            "Documents (Other)": False,
            "All Media": False
        },
        "cutoff_date": "2026-01-01"
    })

    root = tk.Tk()
    root.title("Telegram Downloader (Privacy-Safe Demo)")
    root.geometry("1024x720")
    root.configure(bg="#f0f0f0")

    mock_lock = MagicMock()
    mock_lock.acquire_session_lock.return_value = True

    mock_engine = MagicMock()
    mock_engine.account_id = 10001
    mock_engine.is_downloading = False
    mock_engine.stop_requested = False

    async def fake_check_auth():
        return True
    mock_engine.check_auth = fake_check_auth

    # Privacy-masked dataset per prompt specifications:
    # 2-4 chars visible, rest masked with asterisks
    sanitized_dialogs = [
        {"id": 1001, "name": "UPP***", "peer_type": "channel", "is_forum": True, "access_hash": 111, "username": "upp_***"},
        {"id": 1002, "name": "Pre***", "peer_type": "channel", "is_forum": True, "access_hash": 222, "username": "pre_***"},
        {"id": 1003, "name": "Vis*** Test Series", "peer_type": "channel", "is_forum": False, "access_hash": 333, "username": "vis_***"},
        {"id": 1004, "name": "Una*** COURSE", "peer_type": "channel", "is_forum": True, "access_hash": 444, "username": "una_***"},
        {"id": 1005, "name": "Dri*** IAS Updates", "peer_type": "channel", "is_forum": False, "access_hash": 555, "username": "dri_***"},
        {"id": 1006, "name": "Mak*** Prelims Group", "peer_type": "chat", "is_forum": False, "access_hash": None, "username": ""},
        {"id": 1007, "name": "Pvt*** Discussion", "peer_type": "channel", "is_forum": False, "access_hash": 777, "username": "pvt_***"},
    ]

    mock_engine.dialogs_cache = {(d["peer_type"], d["id"]): d for d in sanitized_dialogs}

    def fake_filter(term):
        if not term:
            return list(sanitized_dialogs)
        t = term.lower()
        return [d for d in sanitized_dialogs if t in d["name"].lower() or t in d.get("username", "").lower()]

    mock_engine.filter_cached_dialogs.side_effect = fake_filter

    from unittest.mock import patch
    with patch("telegram_gui_downloader.ConfigManager", return_value=cfg_mgr), \
         patch("telegram_gui_downloader.DownloadTrackerDB", return_value=db), \
         patch("telegram_gui_downloader.ProcessLock", return_value=mock_lock), \
         patch("telegram_gui_downloader.TelegramDownloaderEngine", return_value=mock_engine):

        app = TelegramGUIApp(root)
        app.engine = mock_engine
        app.all_dialogs = sanitized_dialogs
        app.dialogs_map = {(d["peer_type"], d["id"]): d for d in sanitized_dialogs}

        # Pre-seed Download List with 3 channels
        db.add_selection(10001, 1001, "UPP***", "channel", access_hash=111)
        db.set_channel_storage_mapping(10001, 1001, "UPP***", "UPP***", is_forum=True)

        db.add_selection(10001, 1002, "Pre***", "channel", access_hash=222)
        db.set_channel_storage_mapping(10001, 1002, "Pre***", "Pre***", is_forum=True)

        db.add_selection(10001, 1003, "Vis*** Test Series", "channel", access_hash=333)
        db.set_channel_storage_mapping(10001, 1003, "Vis*** Test Series", "Vis*** Test Series", is_forum=False)

        # -------------------------------------------------------------
        # 1. Capture Login Tab
        # -------------------------------------------------------------
        app.notebook.select(0)
        app.phone_var.set("+1 (555) 019-2834")
        app.code_var.set("*****")
        app.set_auth_feedback("Ready to authenticate with Telegram API.", color="#0066cc")
        root.update()
        time.sleep(0.3)
        img_login = capture_window_to_image(root)
        strip_metadata_and_save(img_login, out_dir / "login.png")

        # -------------------------------------------------------------
        # 2. Capture Browse & Search Channels Tab
        # -------------------------------------------------------------
        app.notebook.select(1)
        app.search_var.set("")
        app.refresh_browse_tree(sanitized_dialogs)
        root.update()
        time.sleep(0.3)
        img_browse = capture_window_to_image(root)
        strip_metadata_and_save(img_browse, out_dir / "browse-channels.png")

        # -------------------------------------------------------------
        # 3. Capture Search Results with Checkbox Multi-Selection
        # -------------------------------------------------------------
        app.search_var.set("Una")
        filtered = fake_filter("Una")
        # Mark Una*** as checked (Pending Add)
        app.checked_channels.clear()
        app.checked_channels.add(("channel", 1004))
        app.refresh_browse_tree(filtered)
        root.update()
        time.sleep(0.3)
        img_search = capture_window_to_image(root)
        strip_metadata_and_save(img_search, out_dir / "search-multiselect.png")

        # -------------------------------------------------------------
        # 4. Capture Download List Manager Tab
        # -------------------------------------------------------------
        app.notebook.select(2)
        app.refresh_download_list_tree()
        root.update()
        time.sleep(0.3)
        img_list = capture_window_to_image(root)
        strip_metadata_and_save(img_list, out_dir / "download-list.png")

        # -------------------------------------------------------------
        # 5. Capture Downloader & Logs Tab (Active Progress)
        # -------------------------------------------------------------
        app.notebook.select(3)
        app.storage_path_var.set("D:\\TelegramDownloads")
        app.storage_status_var.set("Storage: 1.42 TB Free / 2.00 TB Total (Healthy)")
        app.lbl_storage_status.config(foreground="#008800")
        app.progress_var.set(65.0)
        app.status_var.set("Downloading: Lecture_Module_04.mp4 (143.0 MB / 220.0 MB) [65%] - 3.8 MB/s | ETA: 00:00:20")
        app.btn_download.config(state="disabled")
        app.btn_stop.config(state="normal")
        app.log_text.config(state='normal')
        app.log_text.delete("1.0", tk.END)
        app.log_text.config(state='disabled')
        app.log("INFO: Standalone GUI Mode. Telethon engine connected.")
        app.log("INFO: Channel selected: UPP*** (Forum Supergroup)")
        app.log("INFO: Discovered 12 native forum topics via MTProto.")
        app.log("INFO: Crypto acceleration active: OpenSSL Hardware AES-NI.")
        app.log("INFO: Routing message #402 to topic 'General' -> D:\\TelegramDownloads\\UPP***\\General\\")
        app.log("INFO: Downloading Lecture_Module_04.mp4 (Chunk 286/440)")
        root.update()
        time.sleep(0.3)
        img_status = capture_window_to_image(root)
        strip_metadata_and_save(img_status, out_dir / "download-status.png")

        # -------------------------------------------------------------
        # 6. Capture Settings & Organization View
        # -------------------------------------------------------------
        app.progress_var.set(0)
        app.status_var.set("Ready to download. 3 channels queued.")
        app.btn_download.config(state="normal")
        app.btn_stop.config(state="disabled")
        app.organize_forum_topics_var.set(True)
        app.log_text.config(state='normal')
        app.log_text.delete("1.0", tk.END)
        app.log_text.config(state='disabled')
        app.log("INFO: Storage verified at D:\\TelegramDownloads.")
        app.log("INFO: Organization mode: Subfolders by Native Forum Topic enabled.")
        app.log("INFO: Media filters active: PDFs, Videos.")
        app.log("INFO: 3 channels in persistent download list.")
        root.update()
        time.sleep(0.3)
        img_settings = capture_window_to_image(root)
        strip_metadata_and_save(img_settings, out_dir / "settings.png")

        try:
            root.destroy()
        except Exception:
            pass

    shutil.rmtree(tmpdir, ignore_errors=True)
    print("All 6 screenshots generated successfully!")

if __name__ == "__main__":
    main()
