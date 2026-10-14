import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog, filedialog
import threading
import asyncio
import os
import sys
import json
import time
import queue
from pathlib import Path

# Explicitly ensure current working directory is the application directory
os.chdir(Path(__file__).parent.resolve())

from datetime import datetime, timezone
from dotenv import load_dotenv

from config_manager import ConfigManager, StorageError
from db_tracker import DownloadTrackerDB, normalize_channel_id
from instance_lock import ProcessLock
from telegram_engine import TelegramDownloaderEngine, CancelledByUser, format_size
import logging
from logging.handlers import RotatingFileHandler

# Set up logging for GUI
log_dir = Path("logs")
log_dir.mkdir(parents=True, exist_ok=True)
gui_log_file = log_dir / "gui.log"

logger = logging.getLogger("telegram_gui")
logger.setLevel(logging.INFO)
if not logger.handlers:
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = RotatingFileHandler(str(gui_log_file), maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

try:
    from tkcalendar import DateEntry
    HAS_TKCALENDAR = True
except ImportError:
    HAS_TKCALENDAR = False

class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.widget.bind("<Enter>", self.enter)
        self.widget.bind("<Leave>", self.leave)
        self.tw = None

    def enter(self, event=None):
        x = y = 0
        try:
            x, y, cx, cy = self.widget.bbox("insert")
        except tk.TclError:
            pass
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 20
        self.tw = tk.Toplevel(self.widget)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(self.tw, text=self.text, justify='left',
                         background='#ffffe0', relief='solid', borderwidth=1,
                         font=("tahoma", "8", "normal"))
        label.pack(ipadx=1)

    def leave(self, event=None):
        if self.tw:
            self.tw.destroy()
            self.tw = None

class TelegramGUIApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Telegram Downloader - Proxmox Storage Edition")
        self.root.geometry("1080x780")

        self.config_mgr = ConfigManager()
        self.db = DownloadTrackerDB()
        self.db.migrate_legacy_history_if_needed()

        self.ui_queue = queue.Queue()
        self.loop = asyncio.new_event_loop()
        
        # State separation
        self.all_dialogs = []
        self.dialogs_map = {}  # (peer_type, norm_id) -> dialog dict
        self.checked_channels = set()  # set of (peer_type, norm_cid) temporary UI selections
        self.current_filtered_dialogs = []  # currently visible dialog list

        # Mandatory Scoped Topic Selection State: (account_id, peer_type, norm_cid, topic_id)
        self.checked_topics = set()
        self.current_topic_inspector_channel = None  # (peer_type, norm_cid, title)
        self.controlled_acceptance_test_mode = bool(self.config_mgr.config.get("controlled_topic_test_mode", False))

        # Check process lock
        self.lock = ProcessLock(role="gui")
        self.is_monitoring_mode = False
        if not self.lock.acquire_session_lock():
            worker_status = ProcessLock.get_active_worker_status()
            if worker_status:
                self.is_monitoring_mode = True
            else:
                messagebox.showwarning(
                    "Instance Lock",
                    "Another instance of Telegram Downloader is already running. Opening in Monitoring Mode."
                )
                self.is_monitoring_mode = True

        # Initialize engine if standalone mode
        self.engine = None
        if not self.is_monitoring_mode:
            self.engine = TelegramDownloaderEngine(
                config_mgr=self.config_mgr,
                db_tracker=self.db,
                event_callback=self.on_engine_event
            )
            self.async_thread = threading.Thread(target=self.run_async_loop, daemon=True)
            self.async_thread.start()

        self._is_closing = False
        self._after_id = None
        self.build_ui()
        self._after_id = self.root.after(100, self.poll_queue)
        self.update_storage_display()

        if self.is_monitoring_mode:
            self.log("INFO: Background Worker is currently active. GUI connected in MONITORING MODE.")
            self.status_var.set("Monitoring Background Worker...")
            self.btn_download.config(state="disabled")
            self.btn_stop.config(state="disabled")
            self.btn_send_code.config(state="disabled")
            self.btn_login.config(state="disabled")
            self.set_auth_feedback("Active background worker holds session (Monitoring Mode).", color="#555555")
        else:
            self.log("INFO: Standalone GUI Mode. Ready.")
            future = asyncio.run_coroutine_threadsafe(self.engine.check_auth(), self.loop)
            future.add_done_callback(lambda fut: fut.exception() and logger.debug(f"check_auth error: {fut.exception()}"))

    def set_auth_feedback(self, text: str, color: str = "black"):
        if hasattr(self, "auth_status_var"):
            self.auth_status_var.set(text)
        if hasattr(self, "lbl_auth_status"):
            self.lbl_auth_status.config(fg=color)

    def run_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def on_engine_event(self, event_type: str, data: any):
        self.ui_queue.put((event_type, data))

    def build_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Green.Horizontal.TProgressbar", background='#00cc66', troughcolor='#e0e0e0', thickness=20)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # ==========================================
        # Tab 1: Login
        # ==========================================
        self.auth_frame = ttk.Frame(self.notebook, padding=20)
        self.notebook.add(self.auth_frame, text="Login")

        ttk.Label(self.auth_frame, text="Phone Number (e.g. 1234567890):").pack(anchor=tk.W, pady=(0, 5))
        self.phone_var = tk.StringVar(value=os.getenv("PHONE", ""))
        self.phone_entry = ttk.Entry(self.auth_frame, textvariable=self.phone_var, width=30)
        self.phone_entry.pack(anchor=tk.W, pady=(0, 15))
        ToolTip(self.phone_entry, "Enter your Telegram phone number with country code without +")

        self.btn_send_code = ttk.Button(self.auth_frame, text="Send Code", command=self.on_send_code)
        self.btn_send_code.pack(anchor=tk.W, pady=(0, 15))

        ttk.Label(self.auth_frame, text="OTP Code:").pack(anchor=tk.W, pady=(0, 5))
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(self.auth_frame, textvariable=self.code_var, width=20)
        self.code_entry.pack(anchor=tk.W, pady=(0, 15))

        self.btn_login = ttk.Button(self.auth_frame, text="Login", command=self.on_login)
        self.btn_login.pack(anchor=tk.W, pady=(0, 15))

        self.auth_status_var = tk.StringVar(value="")
        self.lbl_auth_status = tk.Label(
            self.auth_frame, textvariable=self.auth_status_var,
            font=("Segoe UI", 9, "bold"), wraplength=480, justify=tk.LEFT, anchor="w"
        )
        self.lbl_auth_status.pack(anchor=tk.W, fill=tk.X, pady=(5, 0))

        # ==========================================
        # Tab 2: Browse & Search Channels
        # ==========================================
        self.browse_frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.browse_frame, text="Browse & Search Channels")

        # Search Controls Bar
        search_top_frame = ttk.Frame(self.browse_frame)
        search_top_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(search_top_frame, text="Search Channels/Groups:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 5))
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(search_top_frame, textvariable=self.search_var, width=35)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        search_entry.bind("<Return>", lambda e: self.on_search())

        btn_search = ttk.Button(search_top_frame, text="Search", command=self.on_search)
        btn_search.pack(side=tk.LEFT, padx=(0, 5))

        btn_clear_filter = ttk.Button(search_top_frame, text="Clear Search", command=self.on_clear_search)
        btn_clear_filter.pack(side=tk.LEFT, padx=(0, 10))

        btn_refresh_telegram = ttk.Button(search_top_frame, text="Refresh from Telegram", command=self.on_refresh_list)
        btn_refresh_telegram.pack(side=tk.LEFT)

        # Status & Navigation Banner
        browse_banner = ttk.Frame(self.browse_frame)
        browse_banner.pack(fill=tk.X, pady=(0, 8))

        self.browse_status_var = tk.StringVar(value="Showing 0 channels | 0 checked | 0 in Download List")
        lbl_browse_stat = ttk.Label(browse_banner, textvariable=self.browse_status_var, font=("Segoe UI", 9, "italic"))
        lbl_browse_stat.pack(side=tk.LEFT)

        self.btn_view_list = ttk.Button(browse_banner, text="View Download List (0) →", command=self.on_go_to_download_list)
        self.btn_view_list.pack(side=tk.RIGHT)

        # Browse Results TreeView
        browse_tree_frame = ttk.Frame(self.browse_frame)
        browse_tree_frame.pack(fill=tk.BOTH, expand=True)

        browse_scroll = ttk.Scrollbar(browse_tree_frame, orient="vertical")
        self.browse_tree = ttk.Treeview(
            browse_tree_frame,
            columns=("select", "name", "type", "id", "status"),
            show="headings",
            selectmode="extended",
            yscrollcommand=browse_scroll.set
        )
        browse_scroll.config(command=self.browse_tree.yview)
        browse_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.browse_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.browse_tree.heading("select", text="[ ]", anchor=tk.CENTER)
        self.browse_tree.heading("name", text="Channel / Group Name", anchor=tk.W)
        self.browse_tree.heading("type", text="Type", anchor=tk.W)
        self.browse_tree.heading("id", text="Telegram ID", anchor=tk.W)
        self.browse_tree.heading("status", text="Download List Status", anchor=tk.W)

        self.browse_tree.column("select", width=50, minwidth=40, anchor=tk.CENTER)
        self.browse_tree.column("name", width=420, anchor=tk.W)
        self.browse_tree.column("type", width=110, anchor=tk.W)
        self.browse_tree.column("id", width=130, anchor=tk.W)
        self.browse_tree.column("status", width=180, anchor=tk.W)

        # Checkbox interactions: cell click, spacebar, double-click
        self.browse_tree.bind("<Button-1>", self.on_browse_tree_click)
        self.browse_tree.bind("<Double-1>", self.on_browse_tree_double_click)
        self.browse_tree.bind("<space>", self.on_space_toggle)

        # Browse Action Bottom Frame
        browse_action_frame = ttk.Frame(self.browse_frame)
        browse_action_frame.pack(fill=tk.X, pady=(8, 0))

        self.btn_add_checked = ttk.Button(
            browse_action_frame,
            text="+ Add Selected (0) to Download List",
            command=self.on_add_checked_to_download_list
        )
        self.btn_add_checked.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_select_all = ttk.Button(
            browse_action_frame,
            text="Select All Filtered",
            command=self.on_select_all_filtered
        )
        self.btn_select_all.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_unselect_all = ttk.Button(
            browse_action_frame,
            text="Unselect All Filtered",
            command=self.on_unselect_all_filtered
        )
        self.btn_unselect_all.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_remove_sel_browse = ttk.Button(
            browse_action_frame,
            text="- Remove from List",
            command=self.on_remove_selected_from_browse
        )
        self.btn_remove_sel_browse.pack(side=tk.LEFT)

        # ==========================================
        # Tab 3: Download List Manager
        # ==========================================
        self.list_frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.list_frame, text="Download List Manager (0)")

        # List Manager Summary Header
        manager_header = ttk.Frame(self.list_frame)
        manager_header.pack(fill=tk.X, pady=(0, 6))

        self.manager_summary_var = tk.StringVar(value="Active Selections: 0 channels ready for download")
        lbl_mgr_summary = ttk.Label(manager_header, textvariable=self.manager_summary_var, font=("Segoe UI", 10, "bold"))
        lbl_mgr_summary.pack(side=tk.LEFT)

        btn_add_more = ttk.Button(manager_header, text="+ Add More Channels/Groups", command=lambda: self.notebook.select(self.browse_frame))
        btn_add_more.pack(side=tk.RIGHT)

        # PanedWindow for Channels List and Forum Topics Browser
        self.paned_list = ttk.PanedWindow(self.list_frame, orient=tk.VERTICAL)
        self.paned_list.pack(fill=tk.BOTH, expand=True)

        # Top Pane: Channels / Groups TreeView
        channels_frame = ttk.Frame(self.paned_list)
        self.paned_list.add(channels_frame, weight=3)

        list_scroll = ttk.Scrollbar(channels_frame, orient="vertical")
        self.download_tree = ttk.Treeview(
            channels_frame,
            columns=("name", "type", "id", "date_added", "status"),
            show="headings",
            selectmode="browse",
            height=6,
            yscrollcommand=list_scroll.set
        )
        list_scroll.config(command=self.download_tree.yview)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.download_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.download_tree.heading("name", text="Channel / Group Name", anchor=tk.W)
        self.download_tree.heading("type", text="Type", anchor=tk.W)
        self.download_tree.heading("id", text="Telegram ID", anchor=tk.W)
        self.download_tree.heading("date_added", text="Date Added", anchor=tk.W)
        self.download_tree.heading("status", text="Access Status", anchor=tk.W)

        self.download_tree.column("name", width=380, anchor=tk.W)
        self.download_tree.column("type", width=140, anchor=tk.W)
        self.download_tree.column("id", width=120, anchor=tk.W)
        self.download_tree.column("date_added", width=150, anchor=tk.W)
        self.download_tree.column("status", width=110, anchor=tk.W)

        self.download_tree.bind("<<TreeviewSelect>>", self.on_download_tree_select)

        # Bottom Pane: Forum Topic Browser Frame
        self.topic_inspector_frame = ttk.LabelFrame(self.paned_list, text="Forum Topic Browser (Select a Group Above)", padding=8)
        self.paned_list.add(self.topic_inspector_frame, weight=4)

        # Topic Inspector Header / Actions Bar
        topic_header = ttk.Frame(self.topic_inspector_frame)
        topic_header.pack(fill=tk.X, pady=(0, 6))

        self.topic_inspector_status_var = tk.StringVar(value="Select a Forum Supergroup above to view topics.")
        lbl_topic_status = ttk.Label(topic_header, textvariable=self.topic_inspector_status_var, font=("Segoe UI", 9, "bold"))
        lbl_topic_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_select_all_topics = ttk.Button(topic_header, text="Select All", command=self.on_select_all_topics, state="disabled")
        self.btn_select_all_topics.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_unselect_all_topics = ttk.Button(topic_header, text="Unselect All", command=self.on_unselect_all_topics, state="disabled")
        self.btn_unselect_all_topics.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_refresh_topics = ttk.Button(topic_header, text="↻ Refresh Topics", command=self.on_refresh_topics_from_telegram, state="disabled")
        self.btn_refresh_topics.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_download_topics = ttk.Button(
            topic_header,
            text="⬇ Download Selected Topics (0)",
            command=self.on_download_selected_topics,
            state="disabled"
        )
        self.btn_download_topics.pack(side=tk.LEFT)

        # Topic TreeView
        topic_tree_frame = ttk.Frame(self.topic_inspector_frame)
        topic_tree_frame.pack(fill=tk.BOTH, expand=True)

        topic_scroll = ttk.Scrollbar(topic_tree_frame, orient="vertical")
        self.topic_tree = ttk.Treeview(
            topic_tree_frame,
            columns=("select", "id", "title", "status", "folder"),
            show="headings",
            selectmode="browse",
            height=6,
            yscrollcommand=topic_scroll.set
        )
        topic_scroll.config(command=self.topic_tree.yview)
        topic_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.topic_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.topic_tree.heading("select", text="Sel", anchor=tk.CENTER)
        self.topic_tree.heading("id", text="Topic ID", anchor=tk.CENTER)
        self.topic_tree.heading("title", text="Verified Topic Title", anchor=tk.W)
        self.topic_tree.heading("status", text="Status", anchor=tk.CENTER)
        self.topic_tree.heading("folder", text="Estimated Destination Folder", anchor=tk.W)

        self.topic_tree.column("select", width=45, anchor=tk.CENTER)
        self.topic_tree.column("id", width=80, anchor=tk.CENTER)
        self.topic_tree.column("title", width=300, anchor=tk.W)
        self.topic_tree.column("status", width=110, anchor=tk.CENTER)
        self.topic_tree.column("folder", width=360, anchor=tk.W)

        self.topic_tree.bind("<Button-1>", self.on_topic_tree_click)
        self.topic_tree.bind("<Double-1>", self.on_topic_tree_double_click)
        self.topic_tree.bind("<space>", self.on_topic_tree_space)
        self.topic_tree.bind("<Button-3>", self.on_topic_context_menu)

        # Context Menu for Topics
        self.topic_context_menu = tk.Menu(self.root, tearoff=0)
        self.topic_context_menu.add_command(label="Toggle Selection Checkbox", command=self.on_topic_context_toggle)
        self.topic_context_menu.add_command(label="Download Only This Topic", command=self.on_topic_context_download_single)
        self.topic_context_menu.add_separator()
        self.topic_context_menu.add_command(label="Copy Topic Title", command=self.on_copy_topic_title)
        self.topic_context_menu.add_command(label="Copy Topic ID", command=self.on_copy_topic_id)

        # Action Toolbar in Download List Manager
        manager_toolbar = ttk.Frame(self.list_frame)
        manager_toolbar.pack(fill=tk.X, pady=(8, 0))

        self.btn_download_mgr = ttk.Button(manager_toolbar, text="Start Downloads (All Saved Channels)", command=self.on_download)
        self.btn_download_mgr.pack(side=tk.LEFT, padx=(0, 6))

        btn_remove_mgr = ttk.Button(manager_toolbar, text="Remove Selected Channels", command=self.on_remove_selected_from_manager)
        btn_remove_mgr.pack(side=tk.LEFT, padx=(0, 6))

        btn_clear_mgr = ttk.Button(manager_toolbar, text="Clear All Selections", command=self.on_clear_list)
        btn_clear_mgr.pack(side=tk.LEFT, padx=(0, 15))

        # ==========================================
        # Tab 4: Downloader & Logs
        # ==========================================
        self.main_frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.main_frame, text="Downloader & Logs")

        # Storage Bar
        storage_frame = ttk.LabelFrame(self.main_frame, text="Storage Destination (2 TB Drive)", padding=10)
        storage_frame.pack(fill=tk.X, pady=(0, 10))

        self.storage_path_var = tk.StringVar(value=self.config_mgr.config.get("download_directory", ""))
        self.storage_entry = ttk.Entry(storage_frame, textvariable=self.storage_path_var, width=60)
        self.storage_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        btn_browse = ttk.Button(storage_frame, text="Browse...", command=self.on_browse_storage)
        btn_browse.pack(side=tk.LEFT, padx=(0, 5))

        btn_validate = ttk.Button(storage_frame, text="Check Space", command=self.update_storage_display)
        btn_validate.pack(side=tk.LEFT, padx=(0, 10))

        self.storage_status_var = tk.StringVar(value="Storage: Unknown")
        self.lbl_storage_status = ttk.Label(storage_frame, textvariable=self.storage_status_var, font=("Helvetica", 9, "bold"))
        self.lbl_storage_status.pack(side=tk.LEFT)

        # Settings
        settings_frame = ttk.LabelFrame(self.main_frame, text="Download Settings", padding=10)
        settings_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(settings_frame, text="Media Type:").grid(row=0, column=0, sticky=tk.NW, pady=5)
        media_frame = ttk.Frame(settings_frame)
        media_frame.grid(row=0, column=1, sticky=tk.W, padx=10, pady=5)

        saved_filters = self.config_mgr.config.get("media_filters", {})
        self.media_vars = {
            "PDFs": tk.BooleanVar(value=saved_filters.get("PDFs", True)),
            "Videos": tk.BooleanVar(value=saved_filters.get("Videos", False)),
            "Photos": tk.BooleanVar(value=saved_filters.get("Photos", False)),
            "Voice/Audio": tk.BooleanVar(value=saved_filters.get("Voice/Audio", False)),
            "Documents (Other)": tk.BooleanVar(value=saved_filters.get("Documents (Other)", False)),
            "All Media": tk.BooleanVar(value=saved_filters.get("All Media", False))
        }

        for idx, (m_type, m_var) in enumerate(self.media_vars.items()):
            cb = ttk.Checkbutton(media_frame, text=m_type, variable=m_var, command=self.on_save_settings)
            cb.grid(row=idx // 3, column=idx % 3, sticky=tk.W, padx=5, pady=2)

        ttk.Label(settings_frame, text="Cutoff Date:").grid(row=1, column=0, sticky=tk.W, pady=5)
        date_frame = ttk.Frame(settings_frame)
        date_frame.grid(row=1, column=1, sticky=tk.W, padx=10, pady=5)

        self.date_var = tk.StringVar(value=self.config_mgr.config.get("cutoff_date", ""))
        if HAS_TKCALENDAR:
            self.date_entry = DateEntry(date_frame, width=12, background='darkblue',
                                        foreground='white', borderwidth=2, date_pattern='yyyy-mm-dd')
            self.date_entry.pack(side=tk.LEFT)
            if self.date_var.get():
                self.date_entry.set_date(self.date_var.get())
            else:
                self.date_entry.delete(0, 'end')
        else:
            date_e = ttk.Entry(date_frame, textvariable=self.date_var, width=15)
            date_e.pack(side=tk.LEFT)
            ttk.Label(date_frame, text="(YYYY-MM-DD)").pack(side=tk.LEFT, padx=5)

        ttk.Label(settings_frame, text="Organization:").grid(row=2, column=0, sticky=tk.W, pady=5)
        org_frame = ttk.Frame(settings_frame)
        org_frame.grid(row=2, column=1, sticky=tk.W, padx=10, pady=5)

        self.organize_forum_topics_var = tk.BooleanVar(value=self.config_mgr.config.get("organize_forum_topics", True))
        cb_topics = ttk.Checkbutton(
            org_frame,
            text="Organize Forum Supergroup Topics into Subfolders",
            variable=self.organize_forum_topics_var,
            command=self.on_save_settings
        )
        cb_topics.pack(side=tk.LEFT)

        btn_action_frame = ttk.Frame(settings_frame)
        btn_action_frame.grid(row=3, column=0, columnspan=2, pady=10)

        self.btn_download = ttk.Button(btn_action_frame, text="Start Download", command=self.on_download)
        self.btn_download.pack(side=tk.LEFT, padx=5)

        self.btn_stop = ttk.Button(btn_action_frame, text="Stop", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side=tk.LEFT, padx=5)

        # Logs & Progress
        logs_frame = ttk.Frame(self.main_frame)
        logs_frame.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(logs_frame, textvariable=self.status_var, font=("Helvetica", 10, "bold")).pack(anchor=tk.W, pady=(0, 5))

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(logs_frame, variable=self.progress_var, maximum=100, style="Green.Horizontal.TProgressbar")
        self.progress_bar.pack(fill=tk.X, pady=(0, 10))

        self.log_text = scrolledtext.ScrolledText(logs_frame, state='disabled', wrap=tk.WORD, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def get_account_id(self) -> int:
        if self.engine and self.engine.account_id:
            return self.engine.account_id
        return 0

    def on_go_to_download_list(self):
        self.notebook.select(self.list_frame)

    def reconcile_saved_channel_titles(self):
        """
        Reconciles saved channel selections against Telegram dialog cache and SQLite.
        Priority:
        1. Current authenticated master dialog cache (self.dialogs_map).
        2. Successfully resolved Telegram entity from existing session.
        3. Last-known real title stored in SQLite (channels table).
        4. Clearly marked fallback [Unresolved] Channel <ID>.
        Preserves IDs, peer types, account IDs, selection states, and dates unchanged.
        """
        account_id = self.get_account_id()
        if not account_id:
            return

        try:
            active_selections = self.db.get_active_selections(account_id)
        except Exception as e:
            logger.error(f"Error reading selections for title reconciliation: {e}")
            return

        updated_count = 0
        for sel in active_selections:
            cid = sel["channel_id"]
            peer_type = sel.get("peer_type", "channel")
            curr_title = sel.get("title", "")
            key = (peer_type, cid)

            resolved_title = None
            access_hash = sel.get("access_hash")
            username = sel.get("username", "")
            access_status = sel.get("access_status", "OK")

            # Priority 1: Current authenticated dialog cache
            if key in self.dialogs_map:
                entry = self.dialogs_map[key]
                t = entry.get("name")
                if t:
                    resolved_title = t
                    access_hash = entry.get("access_hash") or access_hash
                    username = entry.get("username") or username
                    access_status = "OK"

            # Priority 2: Session entity if already resolved or cached in engine
            if not resolved_title and self.engine and hasattr(self.engine, "dialogs_cache"):
                if key in self.engine.dialogs_cache:
                    entry = self.engine.dialogs_cache[key]
                    t = entry.get("name")
                    if t:
                        resolved_title = t
                        access_hash = entry.get("access_hash") or access_hash
                        username = entry.get("username") or username
                        access_status = "OK"

            # Priority 3: Last-known real title stored in SQLite (channels table)
            if not resolved_title:
                chan_row = self.db.get_channel(cid)
                if chan_row and chan_row["name"] and not chan_row["name"].startswith("Channel_"):
                    resolved_title = chan_row["name"]

            # Priority 4: Clearly identified fallback if genuinely unresolved
            if not resolved_title:
                if curr_title and not curr_title.startswith("Channel_"):
                    resolved_title = curr_title
                else:
                    resolved_title = f"[Unresolved] Channel {cid}"
                    access_status = "Unresolved"

            # Update DB only if title or status changed
            if resolved_title and (resolved_title != curr_title or access_status != sel.get("access_status")):
                try:
                    self.db.update_selection_title(
                        account_id=account_id,
                        channel_id=cid,
                        peer_type=peer_type,
                        title=resolved_title,
                        access_hash=access_hash,
                        username=username,
                        access_status=access_status
                    )
                    updated_count += 1
                except Exception as e:
                    logger.error(f"Failed to update title for {peer_type}_{cid}: {e}")

        if updated_count > 0:
            logger.info(f"Reconciled {updated_count} channel title(s) in Download List.")

    def refresh_download_list_tree(self):

        """Refreshes Download List Manager TreeView from SQLite database."""
        for item in self.download_tree.get_children():
            self.download_tree.delete(item)

        account_id = self.get_account_id()
        if not account_id:
            self.manager_summary_var.set("Not logged in. Please log in on Login tab.")
            self.notebook.tab(self.list_frame, text="Download List Manager (0)")
            self.btn_view_list.config(text="View Download List (0) →")
            return

        try:
            active_selections = self.db.get_active_selections(account_id)
        except Exception as e:
            logger.error(f"Error loading download selections: {e}")
            active_selections = []

        count = len(active_selections)
        self.manager_summary_var.set(f"Active Selections: {count} channel(s) ready for download")
        self.notebook.tab(self.list_frame, text=f"Download List Manager ({count})")
        self.btn_view_list.config(text=f"View Download List ({count}) →")

        for sel in active_selections:
            date_short = (sel.get("date_added", "") or "")[:19].replace("T", " ")
            d_info = self.dialogs_map.get((sel['peer_type'], sel['channel_id']), {})
            entity = d_info.get("entity")
            is_forum = bool(getattr(entity, "forum", False) and getattr(entity, "megagroup", False))
            if not is_forum:
                mapping = self.db.get_channel_storage_mapping(account_id, sel["channel_id"], sel["peer_type"])
                if mapping and mapping.get("is_forum"):
                    is_forum = True
            type_label = "Forum Supergroup" if is_forum else sel.get("peer_type", "channel").capitalize()

            self.download_tree.insert(
                "",
                "end",
                iid=f"{sel['peer_type']}_{sel['channel_id']}",
                values=(
                    sel.get("title", f"Channel_{sel['channel_id']}"),
                    type_label,
                    sel["channel_id"],
                    date_short,
                    sel.get("access_status", "OK")
                )
            )

        # Re-evaluate topic inspector selection
        if self.current_topic_inspector_channel:
            curr_pt, curr_cid, _ = self.current_topic_inspector_channel
            expected_iid = f"{curr_pt}_{curr_cid}"
            if self.download_tree.exists(expected_iid):
                self.download_tree.selection_set(expected_iid)
                self.refresh_topic_inspector()
            else:
                self.current_topic_inspector_channel = None
                self._clear_topic_inspector("Selected channel removed from Download List.")

    def _clear_topic_inspector(self, status_msg: str):
        """Clears topic inspector tree and disables topic-specific actions."""
        if hasattr(self, "topic_tree"):
            for item in self.topic_tree.get_children():
                self.topic_tree.delete(item)
        if hasattr(self, "topic_inspector_status_var"):
            self.topic_inspector_status_var.set(status_msg)
        if hasattr(self, "btn_select_all_topics"):
            self.btn_select_all_topics.config(state="disabled")
        if hasattr(self, "btn_unselect_all_topics"):
            self.btn_unselect_all_topics.config(state="disabled")
        if hasattr(self, "btn_refresh_topics"):
            self.btn_refresh_topics.config(state="disabled")
        if hasattr(self, "btn_download_topics"):
            self.btn_download_topics.config(state="disabled", text="⬇ Download Selected Topics (0)")

    def on_download_tree_select(self, event=None):
        """Triggered when the user highlights a channel in Download List Manager."""
        sel_items = self.download_tree.selection()
        if not sel_items:
            self.current_topic_inspector_channel = None
            self._clear_topic_inspector("Select a Forum Supergroup above to view topics.")
            return

        iid = sel_items[0]
        vals = self.download_tree.item(iid, "values")
        if not vals or len(vals) < 3:
            return

        title = vals[0]
        type_label = vals[1]
        cid = int(vals[2])
        parts = iid.split("_", 1)
        peer_type = parts[0] if len(parts) == 2 else "channel"

        if type_label != "Forum Supergroup":
            self.current_topic_inspector_channel = None
            self._clear_topic_inspector(f"'{title}' is a {type_label} (Broadcast Channel — No topics exist).")
            return

        # Scoped Forum Supergroup selected
        self.current_topic_inspector_channel = (peer_type, cid, title)
        self.refresh_topic_inspector()

    def refresh_topic_inspector(self):
        """Renders forum topics for the currently selected supergroup from cache."""
        if not self.current_topic_inspector_channel:
            return

        account_id = self.get_account_id()
        if not account_id:
            self._clear_topic_inspector("Please log in to view forum topics.")
            return

        peer_type, cid, title = self.current_topic_inspector_channel

        # Clear existing rows
        for item in self.topic_tree.get_children():
            self.topic_tree.delete(item)

        topics = self.db.get_forum_topics(cid, account_id=account_id, peer_type=peer_type)
        if not topics:
            self.topic_inspector_status_var.set(
                f"Forum Supergroup: '{title}' (0 topics in cache. Click 'Refresh Topics' to query Telegram)."
            )
            self.btn_refresh_topics.config(state="normal")
            self.btn_select_all_topics.config(state="disabled")
            self.btn_unselect_all_topics.config(state="disabled")
            self.btn_download_topics.config(state="disabled", text="⬇ Download Selected Topics (0)")
            return

        # Resolve destination folder prefix
        mapping = self.db.get_channel_storage_mapping(account_id, cid, peer_type)
        if mapping and mapping.get("local_folder_name"):
            channel_folder_name = mapping["local_folder_name"]
        else:
            channel_folder_name = sanitize_folder_name(title)

        checked_for_channel = []
        for t in topics:
            tid = t["topic_id"]
            t_title = t.get("title") or f"Topic_{tid}"
            key = (account_id, peer_type, cid, tid)
            is_checked = (key in self.checked_topics)
            if is_checked:
                checked_for_channel.append(tid)
            check_sym = "☑" if is_checked else "☐"

            is_closed = bool(t.get("is_closed"))
            is_hidden = bool(t.get("is_hidden"))
            is_inaccessible = bool(t.get("is_inaccessible", False))

            if is_inaccessible:
                status_str = "Inaccessible"
            elif is_hidden:
                status_str = "Hidden"
            elif is_closed:
                status_str = "Closed"
            else:
                status_str = "Available"

            topic_folder = t.get("local_folder_name") or sanitize_folder_name(t_title)
            est_dest = f"{channel_folder_name}\\{topic_folder}"

            self.topic_tree.insert(
                "",
                "end",
                iid=f"topic_{tid}",
                values=(check_sym, tid, t_title, status_str, est_dest)
            )

        count = len(checked_for_channel)
        self.topic_inspector_status_var.set(
            f"Forum Supergroup: '{title}' — {len(topics)} topic(s) cached | {count} selected"
        )
        self.btn_refresh_topics.config(state="normal")
        self.btn_select_all_topics.config(state="normal")
        self.btn_unselect_all_topics.config(state="normal")
        self.btn_download_topics.config(
            state="normal" if count > 0 else "disabled",
            text=f"⬇ Download Selected Topics ({count})"
        )

    def on_topic_tree_click(self, event):
        """Handles single click on topic treeview to toggle checkbox when clicking select column."""
        region = self.topic_tree.identify_region(event.x, event.y)
        if region == "cell":
            col = self.topic_tree.identify_column(event.x)
            if col == "#1":  # 'select' column
                item_iid = self.topic_tree.identify_row(event.y)
                if item_iid:
                    self.toggle_topic_checkbox(item_iid)

    def on_topic_tree_double_click(self, event):
        """Double clicking any part of a topic row toggles its checkbox."""
        item_iid = self.topic_tree.identify_row(event.y)
        if item_iid:
            self.toggle_topic_checkbox(item_iid)

    def on_topic_tree_space(self, event):
        """Spacebar toggles checkbox for highlighted topic."""
        sel = self.topic_tree.selection()
        if sel:
            self.toggle_topic_checkbox(sel[0])

    def toggle_topic_checkbox(self, item_iid: str):
        """Toggles temporary selection state for an individual topic under stable composite identity."""
        if not self.current_topic_inspector_channel or not self.topic_tree.exists(item_iid):
            return

        account_id = self.get_account_id()
        if not account_id:
            return

        peer_type, cid, title = self.current_topic_inspector_channel
        vals = list(self.topic_tree.item(item_iid, "values"))
        if len(vals) < 5:
            return

        tid = int(vals[1])
        t_title = vals[2]
        status_str = vals[3]

        if status_str == "Inaccessible":
            messagebox.showwarning(
                "Unavailable Topic",
                f"Topic '{t_title}' (ID: {tid}) is marked Inaccessible and cannot be selected for download."
            )
            return

        key = (account_id, peer_type, cid, tid)
        if key in self.checked_topics:
            self.checked_topics.remove(key)
            is_checked = False
        else:
            self.checked_topics.add(key)
            is_checked = True

        vals[0] = "☑" if is_checked else "☐"
        self.topic_tree.item(item_iid, values=vals)

        # Re-evaluate count
        checked_for_channel = [
            t_id for (acc, pt, c, t_id) in self.checked_topics
            if acc == account_id and pt == peer_type and c == cid
        ]
        count = len(checked_for_channel)
        total_topics = len(self.topic_tree.get_children())
        self.topic_inspector_status_var.set(
            f"Forum Supergroup: '{title}' — {total_topics} topic(s) cached | {count} selected"
        )
        self.btn_download_topics.config(
            state="normal" if count > 0 else "disabled",
            text=f"⬇ Download Selected Topics ({count})"
        )

    def on_select_all_topics(self):
        """Selects all accessible topics for the currently viewed forum supergroup only."""
        if not self.current_topic_inspector_channel:
            return

        account_id = self.get_account_id()
        if not account_id:
            return

        peer_type, cid, title = self.current_topic_inspector_channel
        updated = 0
        for item in self.topic_tree.get_children():
            vals = list(self.topic_tree.item(item, "values"))
            tid = int(vals[1])
            status_str = vals[3]
            if status_str != "Inaccessible":
                self.checked_topics.add((account_id, peer_type, cid, tid))
                vals[0] = "☑"
                self.topic_tree.item(item, values=vals)
                updated += 1

        total_topics = len(self.topic_tree.get_children())
        self.topic_inspector_status_var.set(
            f"Forum Supergroup: '{title}' — {total_topics} topic(s) cached | {updated} selected"
        )
        self.btn_download_topics.config(
            state="normal" if updated > 0 else "disabled",
            text=f"⬇ Download Selected Topics ({updated})"
        )

    def on_unselect_all_topics(self):
        """Unselects all topics for the currently viewed forum supergroup only."""
        if not self.current_topic_inspector_channel:
            return

        account_id = self.get_account_id()
        if not account_id:
            return

        peer_type, cid, title = self.current_topic_inspector_channel
        for item in self.topic_tree.get_children():
            vals = list(self.topic_tree.item(item, "values"))
            tid = int(vals[1])
            self.checked_topics.discard((account_id, peer_type, cid, tid))
            vals[0] = "☐"
            self.topic_tree.item(item, values=vals)

        total_topics = len(self.topic_tree.get_children())
        self.topic_inspector_status_var.set(
            f"Forum Supergroup: '{title}' — {total_topics} topic(s) cached | 0 selected"
        )
        self.btn_download_topics.config(state="disabled", text="⬇ Download Selected Topics (0)")

    def on_refresh_topics_from_telegram(self):
        """Non-blocking background refresh of topics for currently viewed forum supergroup."""
        if not self.current_topic_inspector_channel or not self.engine:
            return

        account_id = self.get_account_id()
        if not account_id:
            messagebox.showwarning("Authentication Required", "Please log in first.")
            return

        peer_type, cid, title = self.current_topic_inspector_channel
        self.btn_refresh_topics.config(state="disabled", text="Refreshing...")

        async def fetch_topics_task():
            try:
                sel = self.db.get_selection(account_id, cid, peer_type)
                if not sel:
                    sel = {"account_id": account_id, "peer_type": peer_type, "channel_id": cid, "title": title}
                entity = await self.engine.resolve_selection_entity(sel)
                if not entity:
                    self.ui_queue.put(("log", f"Failed to resolve Telegram entity for '{title}' (ID {cid})."))
                    self.ui_queue.put(("topics_refreshed", (cid, 0)))
                    return
                topics_dict = await self.engine.get_or_fetch_forum_topics(entity, cid, force_refresh=True)
                self.ui_queue.put(("log", f"Discovered {len(topics_dict)} forum topic(s) for '{title}'."))
                self.ui_queue.put(("topics_refreshed", (cid, len(topics_dict))))
            except Exception as e:
                logger.error(f"Error fetching topics from Telegram for {cid}: {e}")
                self.ui_queue.put(("log", f"Error refreshing topics for '{title}': {e}"))
                self.ui_queue.put(("topics_refreshed", (cid, 0)))

        asyncio.run_coroutine_threadsafe(fetch_topics_task(), self.loop)

    def on_download_selected_topics(self):
        """
        Executes download strictly scoped to checked topics of the currently viewed forum supergroup.
        Mandatory: Fails closed if 0 topics selected. Under no circumstances falls back to all channels.
        """
        valid, msg, _ = self.config_mgr.validate_storage()
        if not valid:
            messagebox.showerror("Invalid Storage", f"Cannot start downloads:\n{msg}\n\nNO fallback to C: drive allowed.")
            return

        account_id = self.get_account_id()
        if not account_id:
            messagebox.showwarning("Authentication Required", "Please log in first.")
            return

        if not self.current_topic_inspector_channel:
            messagebox.showwarning("Select Forum Supergroup", "Please select a Forum Supergroup from the list above.")
            return

        peer_type, cid, title = self.current_topic_inspector_channel

        # Retrieve selected topic IDs under scoped composite identity
        checked_tids = [
            tid for (acc, pt, c, tid) in self.checked_topics
            if acc == account_id and pt == peer_type and c == cid
        ]

        if not checked_tids:
            messagebox.showwarning(
                "No Topics Selected",
                f"No topics are currently checked for '{title}'.\n\n"
                "Please check at least one topic checkbox before clicking Download Selected Topics."
            )
            return

        is_acceptance_mode = bool(
            getattr(self, "controlled_acceptance_test_mode", False) or
            self.config_mgr.config.get("controlled_topic_test_mode", False)
        )
        norm_cid = normalize_channel_id(cid)
        is_test_target = (
            is_acceptance_mode and
            norm_cid == 4450272939 and
            peer_type == "channel" and
            150 in checked_tids
        )

        if is_test_target:
            dest_dir = self.config_mgr.get_download_dir() / "Premium 2027" / "DAILY NEWSPAPER"
            confirm_msg = (
                "CONTROLLED ACCEPTANCE TEST MODE (Strictly Bounded)\n\n"
                f"Target Supergroup: '{title}' (ID: {cid}, Peer: {peer_type})\n"
                f"Authenticated Account ID: {account_id}\n"
                f"Topic: DAILY NEWSPAPER (ID: 150)\n"
                f"Destination Directory: {dest_dir}\n\n"
                "Approved Media Files (Strict Message Allowlist):\n"
                "  1. INDIAN EXPRESS HD Delhi 13~10~2026.pdf\n"
                "     - Message ID: 1613\n"
                "     - Size: 6,215,860 bytes (~5.93 MB)\n"
                "     - MIME: application/pdf\n"
                "  2. INDIAN EXPRESS HD Delhi 12~10~2026.pdf\n"
                "     - Message ID: 1607\n"
                "     - Size: 6,654,297 bytes (~6.35 MB)\n"
                "     - MIME: application/pdf\n\n"
                "Enforced Safety Limits:\n"
                "  • Target Topic IDs: {150}\n"
                "  • Allowed Message IDs: [1613, 1607]\n"
                "  • Max Completed Files: 2\n"
                "  • Max Total Bytes: 15,000,000 bytes (~14.31 MB)\n"
                "  • Total Approved Payload: 12,870,157 bytes (~12.27 MB)\n\n"
                "Proceed with bounded acceptance test download?"
            )
            confirm = messagebox.askyesno("Confirm Controlled Acceptance Test", confirm_msg)
            if not confirm:
                return
        else:
            # Build human-readable topic list for explicit confirmation
            cached_topics = self.db.get_forum_topics(cid, account_id=account_id, peer_type=peer_type)
            topic_title_map = {t["topic_id"]: t.get("title", f"Topic_{t['topic_id']}") for t in cached_topics}

            topic_lines = [f"  • {topic_title_map.get(tid, f'Topic {tid}')} (ID: {tid})" for tid in checked_tids[:8]]
            if len(checked_tids) > 8:
                topic_lines.append(f"  ... and {len(checked_tids) - 8} more topic(s)")
            topic_summary_str = "\n".join(topic_lines)

            confirm = messagebox.askyesno(
                "Confirm Scoped Topic Download",
                f"Start downloading media from {len(checked_tids)} selected topic(s) in:\n"
                f"'{title}' (ID: {cid})?\n\n"
                f"Selected Topics:\n{topic_summary_str}\n\n"
                f"IMPORTANT: Only '{title}' will be processed. Other channels in your Download List will NOT be touched.\n\n"
                f"Proceed with download?"
            )
            if not confirm:
                return

        self.on_save_settings()

        async def prepare_and_run_scoped():
            self.ui_queue.put(("status", f"Resolving Telegram entity for forum '{title}'..."))
            sel = self.db.get_selection(account_id, cid, peer_type)
            if not sel:
                sel = {"account_id": account_id, "peer_type": peer_type, "channel_id": cid, "title": title}
            entity = await self.engine.resolve_selection_entity(sel)
            if not entity:
                self.ui_queue.put(("log", f"Error: Could not resolve Telegram entity for channel {cid} ({title})."))
                self.ui_queue.put(("download_ended", None))
                return

            selected_dialogs = [{
                "id": cid,
                "name": title,
                "peer_type": peer_type,
                "entity": entity
            }]

            if is_test_target:
                # Immediate pre-transfer validation of topic & message identities
                cached_check = self.db.get_forum_topics(cid, account_id=account_id, peer_type=peer_type)
                valid_tids = {t["topic_id"] for t in cached_check}
                if 150 not in valid_tids:
                    self.ui_queue.put(("log", "Error: Topic 150 ('DAILY NEWSPAPER') not found in verified forum catalog."))
                    self.ui_queue.put(("download_ended", None))
                    return

                self.ui_queue.put((
                    "log",
                    f"Starting controlled acceptance test download for '{title}' (Topic 150, Messages [1613, 1607], Max 2 files, Max 15MB)..."
                ))
                await self.engine.run_batch(
                    selected_dialogs,
                    target_topic_ids={150},
                    max_completed_files=2,
                    expected_channel_id=4450272939,
                    expected_peer_type="channel",
                    expected_account_id=account_id,
                    allowed_message_ids=[1613, 1607],
                    max_total_bytes=15000000
                )
            else:
                self.ui_queue.put((
                    "log",
                    f"Starting scoped topic download for '{title}' (Topics: {checked_tids})..."
                ))
                await self.engine.run_batch(
                    selected_dialogs,
                    target_topic_ids=set(checked_tids),
                    expected_channel_id=cid,
                    expected_peer_type=peer_type,
                    expected_account_id=account_id
                )

        self.btn_download.config(state="disabled")
        if hasattr(self, "btn_download_mgr"):
            self.btn_download_mgr.config(state="disabled")
        if hasattr(self, "btn_download_topics"):
            self.btn_download_topics.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.notebook.select(self.main_frame)

        asyncio.run_coroutine_threadsafe(prepare_and_run_scoped(), self.loop)

    def on_download_single_topic(self, topic_id: int):
        """Downloads only the single specified topic in the currently viewed forum supergroup."""
        valid, msg, _ = self.config_mgr.validate_storage()
        if not valid:
            messagebox.showerror("Invalid Storage", f"Cannot start downloads:\n{msg}\n\nNO fallback to C: drive allowed.")
            return

        account_id = self.get_account_id()
        if not account_id or not self.current_topic_inspector_channel:
            return

        peer_type, cid, title = self.current_topic_inspector_channel
        topic_rec = self.db.get_forum_topic(cid, topic_id, account_id=account_id, peer_type=peer_type)
        t_title = topic_rec.get("title") if topic_rec else f"Topic_{topic_id}"

        confirm = messagebox.askyesno(
            "Confirm Single Topic Download",
            f"Download media ONLY from topic:\n"
            f"'{t_title}' (ID: {topic_id})\n\n"
            f"in Forum Supergroup '{title}'?\n\n"
            f"Note: Only this single topic will be processed. Proceed?"
        )
        if not confirm:
            return

        self.on_save_settings()

        async def prepare_and_run_single():
            self.ui_queue.put(("status", f"Resolving Telegram entity for forum '{title}'..."))
            sel = self.db.get_selection(account_id, cid, peer_type)
            if not sel:
                sel = {"account_id": account_id, "peer_type": peer_type, "channel_id": cid, "title": title}
            entity = await self.engine.resolve_selection_entity(sel)
            if not entity:
                self.ui_queue.put(("log", f"Error: Could not resolve Telegram entity for channel {cid} ({title})."))
                self.ui_queue.put(("download_ended", None))
                return

            selected_dialogs = [{
                "id": cid,
                "name": title,
                "peer_type": peer_type,
                "entity": entity
            }]
            self.ui_queue.put((
                "log",
                f"Starting single-topic download for '{title}' -> Topic '{t_title}' (ID {topic_id})..."
            ))
            await self.engine.run_batch(
                selected_dialogs,
                target_topic_ids={topic_id},
                expected_channel_id=cid,
                expected_peer_type=peer_type,
                expected_account_id=account_id
            )

        self.btn_download.config(state="disabled")
        if hasattr(self, "btn_download_mgr"):
            self.btn_download_mgr.config(state="disabled")
        if hasattr(self, "btn_download_topics"):
            self.btn_download_topics.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.notebook.select(self.main_frame)

        asyncio.run_coroutine_threadsafe(prepare_and_run_single(), self.loop)

    def on_topic_context_menu(self, event):
        """Displays right-click context menu on highlighted topic row."""
        item = self.topic_tree.identify_row(event.y)
        if item:
            self.topic_tree.selection_set(item)
            try:
                self.topic_context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.topic_context_menu.grab_release()

    def on_topic_context_toggle(self):
        sel = self.topic_tree.selection()
        if sel:
            self.toggle_topic_checkbox(sel[0])

    def on_topic_context_download_single(self):
        sel = self.topic_tree.selection()
        if sel:
            vals = self.topic_tree.item(sel[0], "values")
            if len(vals) >= 2:
                tid = int(vals[1])
                self.on_download_single_topic(tid)

    def on_copy_topic_title(self):
        sel = self.topic_tree.selection()
        if sel:
            vals = self.topic_tree.item(sel[0], "values")
            if len(vals) >= 3:
                self.root.clipboard_clear()
                self.root.clipboard_append(vals[2])

    def on_copy_topic_id(self):
        sel = self.topic_tree.selection()
        if sel:
            vals = self.topic_tree.item(sel[0], "values")
            if len(vals) >= 2:
                self.root.clipboard_clear()
                self.root.clipboard_append(str(vals[1]))

    def toggle_checkbox(self, iid: str):
        parts = iid.split("_", 1)
        if len(parts) != 2:
            return
        peer_type, cid_str = parts
        try:
            cid = int(cid_str)
        except ValueError:
            return
        key = (peer_type, cid)

        account_id = self.get_account_id()
        is_in_db = False
        if account_id:
            try:
                is_in_db = self.db.is_selected(account_id, cid, peer_type)
            except Exception:
                pass

        # If already added to SQLite Download List, keep checked and guard against silent removal
        if is_in_db:
            self.browse_status_var.set(
                f"Channel is already saved in Download List. Use Download List Manager tab to remove it."
            )
            if self.browse_tree.exists(iid):
                current_vals = list(self.browse_tree.item(iid, "values"))
                if len(current_vals) >= 5:
                    current_vals[0] = "☑"
                    current_vals[4] = "[✓ In Download List]"
                    self.browse_tree.item(iid, values=current_vals)
            return

        # For unadded items, toggle temporary pending selection
        if key in self.checked_channels:
            self.checked_channels.remove(key)
            is_checked = False
        else:
            self.checked_channels.add(key)
            is_checked = True

        check_sym = "☑" if is_checked else "☐"
        status_text = "[+ Pending Add]" if is_checked else "[+ Not Added]"

        if self.browse_tree.exists(iid):
            current_vals = list(self.browse_tree.item(iid, "values"))
            if len(current_vals) >= 5:
                current_vals[0] = check_sym
                current_vals[4] = status_text
                self.browse_tree.item(iid, values=current_vals)

        self._update_browse_counts()

    def on_browse_tree_click(self, event):
        region = self.browse_tree.identify_region(event.x, event.y)
        column = self.browse_tree.identify_column(event.x)
        if column == "#1" and region == "cell":
            iid = self.browse_tree.identify_row(event.y)
            if iid:
                self.toggle_checkbox(iid)
                return "break"

    def on_browse_tree_double_click(self, event):
        region = self.browse_tree.identify_region(event.x, event.y)
        if region in ("cell", "tree"):
            iid = self.browse_tree.identify_row(event.y)
            if iid:
                self.toggle_checkbox(iid)
        return "break"

    def on_space_toggle(self, event):
        selected_iids = self.browse_tree.selection()
        if selected_iids:
            for iid in selected_iids:
                self.toggle_checkbox(iid)
            return "break"

    def _update_browse_counts(self):
        total_matching = len(self.current_filtered_dialogs)
        total_pending = len(self.checked_channels)
        account_id = self.get_account_id()
        total_in_db = 0
        if account_id:
            try:
                active = self.db.get_active_selections(account_id)
                total_in_db = len(active)
            except Exception:
                pass
        self.browse_status_var.set(
            f"Showing {total_matching} matching channels | {total_pending} pending add | {total_in_db} in Download List"
        )
        if hasattr(self, "btn_add_checked"):
            self.btn_add_checked.config(text=f"+ Add Selected ({total_pending}) to Download List")
        if hasattr(self, "btn_view_list"):
            self.btn_view_list.config(text=f"View Download List ({total_in_db}) →")
        if hasattr(self, "list_frame"):
            self.notebook.tab(self.list_frame, text=f"Download List Manager ({total_in_db})")

    def refresh_browse_tree(self, dialogs: list = None):
        """Renders dialogs in the Browse tab with checkboxes, preserving active search query."""
        for item in self.browse_tree.get_children():
            self.browse_tree.delete(item)

        if dialogs is not None:
            items_to_render = dialogs
        else:
            term = self.search_var.get().strip() if hasattr(self, "search_var") else ""
            if term and self.engine:
                items_to_render = self.engine.filter_cached_dialogs(term)
            else:
                items_to_render = self.all_dialogs

        self.current_filtered_dialogs = items_to_render
        account_id = self.get_account_id()

        active_keys = set()
        if account_id:
            try:
                active = self.db.get_active_selections(account_id)
                active_keys = {(a["peer_type"], a["channel_id"]) for a in active}
            except Exception:
                pass

        # Discard stale checked state for items that are already in active_keys
        for key in list(self.checked_channels):
            if key in active_keys:
                self.checked_channels.discard(key)

        for d in items_to_render:
            norm_id = normalize_channel_id(d["id"])
            peer_type = d.get("peer_type", "channel")
            key = (peer_type, norm_id)
            is_in_list = key in active_keys
            is_pending = key in self.checked_channels

            if is_in_list:
                check_sym = "☑"
                status_text = "[✓ In Download List]"
            elif is_pending:
                check_sym = "☑"
                status_text = "[+ Pending Add]"
            else:
                check_sym = "☐"
                status_text = "[+ Not Added]"

            entity = d.get("entity")
            is_forum = bool(getattr(entity, "forum", False) and getattr(entity, "megagroup", False))
            type_label = "Forum Supergroup" if is_forum else peer_type.capitalize()

            self.browse_tree.insert(
                "",
                "end",
                iid=f"{peer_type}_{norm_id}",
                values=(
                    check_sym,
                    d["name"],
                    type_label,
                    norm_id,
                    status_text
                )
            )

        self._update_browse_counts()


    def on_search(self):
        if self.is_monitoring_mode: return
        term = self.search_var.get().strip()
        if self.engine:
            filtered = self.engine.filter_cached_dialogs(term)
            self.refresh_browse_tree(filtered)
        else:
            self.refresh_browse_tree()

    def on_clear_search(self):
        self.search_var.set("")
        if self.engine:
            all_cached = self.engine.filter_cached_dialogs("")
            self.refresh_browse_tree(all_cached)
        else:
            self.refresh_browse_tree()

    def on_select_all_filtered(self):
        """Selects (checks) all eligible channels matching the active search query."""
        account_id = self.get_account_id()
        active_keys = set()
        if account_id:
            try:
                active = self.db.get_active_selections(account_id)
                active_keys = {(a["peer_type"], a["channel_id"]) for a in active}
            except Exception:
                pass

        for d in self.current_filtered_dialogs:
            norm_id = normalize_channel_id(d["id"])
            peer_type = d.get("peer_type", "channel")
            key = (peer_type, norm_id)
            if key not in active_keys:
                self.checked_channels.add(key)

        self.refresh_browse_tree()

    def on_unselect_all_filtered(self):
        """Clears temporary checkbox selections for channels matching active search query."""
        for d in self.current_filtered_dialogs:
            norm_id = normalize_channel_id(d["id"])
            peer_type = d.get("peer_type", "channel")
            self.checked_channels.discard((peer_type, norm_id))

        self.refresh_browse_tree()

    def on_add_checked_to_download_list(self):
        """Commits all temporarily checked channels into SQLite download_selections."""
        account_id = self.get_account_id()
        if not account_id:
            messagebox.showwarning("Authentication Required", "Please log in first before adding channels.")
            return

        targets_to_add = set(self.checked_channels)
        if not targets_to_add:
            selected_iids = self.browse_tree.selection()
            if selected_iids:
                for iid in selected_iids:
                    parts = iid.split("_", 1)
                    if len(parts) == 2:
                        peer_type, cid_str = parts
                        try:
                            targets_to_add.add((peer_type, int(cid_str)))
                        except ValueError:
                            pass

        if not targets_to_add:
            messagebox.showinfo("No Channels Selected", "Please tick at least one channel checkbox or highlight a row to add.")
            return

        active_keys = set()
        try:
            active = self.db.get_active_selections(account_id)
            active_keys = {(a["peer_type"], a["channel_id"]) for a in active}
        except Exception:
            pass

        added_count = 0
        already_present_count = 0
        failed_count = 0
        failed_errors = []

        for peer_type, cid in list(targets_to_add):
            key = (peer_type, cid)
            if key in active_keys:
                already_present_count += 1
                self.checked_channels.discard(key)
                continue

            dialog_info = self.dialogs_map.get(key)
            title = dialog_info["name"] if dialog_info else f"Channel_{cid}"
            access_hash = dialog_info.get("access_hash") if dialog_info else None
            username = dialog_info.get("username", "") if dialog_info else ""

            try:
                self.db.add_selection(
                    account_id=account_id,
                    channel_id=cid,
                    title=title,
                    peer_type=peer_type,
                    access_hash=access_hash,
                    username=username,
                    config_mgr=self.config_mgr
                )
                added_count += 1
                self.checked_channels.discard(key)
            except Exception as e:
                failed_count += 1
                failed_errors.append(f"{title} ({cid}): {e}")
                logger.error(f"Failed to add selection {key}: {e}")

        # Update UI views: preserves active search query!
        self.refresh_download_list_tree()
        self.refresh_browse_tree()

        msg = f"Added {added_count} channel(s) to Download List."
        if already_present_count > 0:
            msg += f" ({already_present_count} already in list)."
        if failed_count > 0:
            msg += f" ({failed_count} failed)."
            messagebox.showwarning("Partial Addition Warning", f"{msg}\n\nFailures:\n" + "\n".join(failed_errors[:5]))
        self.log(msg)

    def on_add_selected_from_browse(self):
        """Backward-compatible alias pointing to on_add_checked_to_download_list."""
        self.on_add_checked_to_download_list()

    def on_remove_selected_from_browse(self):
        selected_iids = self.browse_tree.selection()
        if not selected_iids:
            messagebox.showinfo("Select Channel", "Please highlight at least one channel in the list to remove.")
            return

        account_id = self.get_account_id()
        if not account_id: return

        removed_count = 0
        for iid in selected_iids:
            parts = iid.split("_", 1)
            if len(parts) == 2:
                peer_type, cid_str = parts
                cid = int(cid_str)
                self.db.remove_selection(
                    account_id=account_id,
                    channel_id=cid,
                    peer_type=peer_type,
                    config_mgr=self.config_mgr
                )
                removed_count += 1

        self.refresh_download_list_tree()
        self.refresh_browse_tree()
        self.log(f"Removed {removed_count} channel(s) from Download List.")

    def on_remove_selected_from_manager(self):
        selected_iids = self.download_tree.selection()
        if not selected_iids:
            messagebox.showinfo("Notice", "Please highlight at least one channel to remove.")
            return

        account_id = self.get_account_id()
        if not account_id: return

        for iid in selected_iids:
            parts = iid.split("_", 1)
            if len(parts) == 2:
                peer_type, cid_str = parts
                cid = int(cid_str)
                self.db.remove_selection(
                    account_id=account_id,
                    channel_id=cid,
                    peer_type=peer_type,
                    config_mgr=self.config_mgr
                )

        self.refresh_download_list_tree()
        self.refresh_browse_tree()

    def on_clear_list(self):
        account_id = self.get_account_id()
        if not account_id: return

        try:
            active = self.db.get_active_selections(account_id)
        except Exception:
            active = []

        if not active:
            messagebox.showinfo("Empty List", "Your Download List is already empty.")
            return

        confirm = messagebox.askyesno(
            "Confirm Clear Selections",
            f"Are you sure you want to clear all {len(active)} channel(s) from your Download List?\n\n"
            "Previously downloaded files and history will remain 100% intact.",
            icon='warning'
        )
        if confirm:
            self.db.clear_selections(account_id, self.config_mgr)
            self.refresh_download_list_tree()
            self.refresh_browse_tree()

    def update_storage_display(self):
        target = self.storage_path_var.get().strip()
        if not target:
            self.storage_status_var.set("Storage: NONE SELECTED (Required)")
            self.lbl_storage_status.config(foreground="red")
            return

        valid, msg, metrics = self.config_mgr.validate_storage(target)
        if valid:
            self.storage_status_var.set(f"Free: {metrics['free_gb']:.1f} GB / {metrics['total_gb']:.1f} GB (Ready)")
            self.lbl_storage_status.config(foreground="green")
            self.config_mgr.save_config({"download_directory": target})
        else:
            self.storage_status_var.set(f"ERROR: {msg}")
            self.lbl_storage_status.config(foreground="red")

    def on_browse_storage(self):
        chosen = filedialog.askdirectory(title="Select 2 TB Storage Destination Folder")
        if chosen:
            self.storage_path_var.set(chosen)
            self.update_storage_display()

    def on_save_settings(self):
        filters = {k: v.get() for k, v in self.media_vars.items()}
        date_str = self.date_entry.get().strip() if HAS_TKCALENDAR else self.date_var.get().strip()
        org_topics = self.organize_forum_topics_var.get() if hasattr(self, "organize_forum_topics_var") else True
        self.config_mgr.save_config({
            "media_filters": filters,
            "cutoff_date": date_str,
            "organize_forum_topics": org_topics
        })

    def on_save_active_channels(self):
        account_id = self.get_account_id()
        if account_id:
            count = self.db.sync_to_legacy_config(account_id, self.config_mgr)
            messagebox.showinfo("Saved", f"Synchronized {count} active channels to configuration.")

    def log(self, msg):
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')

    def poll_queue(self):
        # 1. Check worker status if in monitoring mode
        if self.is_monitoring_mode:
            status_data = ProcessLock.get_active_worker_status()
            if status_data:
                text = status_data.get("status_text", "Worker running...")
                self.status_var.set(f"[Worker PID {status_data.get('worker_pid')}]: {text}")
            else:
                self.status_var.set("Worker idle / disconnected.")

        # 2. Process local UI queue
        while not self.ui_queue.empty():
            msg_type, data = self.ui_queue.get()
            if msg_type == "log":
                self.log(data)
            elif msg_type == "status":
                self.status_var.set(data)
            elif msg_type == "progress":
                self.progress_var.set(data)
            elif msg_type == "dialogs":
                self.all_dialogs = data
                for d in data:
                    norm_id = normalize_channel_id(d["id"])
                    peer_type = d.get("peer_type", "channel")
                    self.dialogs_map[(peer_type, norm_id)] = d
                self.reconcile_saved_channel_titles()
                self.refresh_browse_tree()
                self.refresh_download_list_tree()

            elif msg_type == "auth_status":
                self.set_auth_feedback(data, color="#0055aa")
            elif msg_type == "auth_code_sent":
                self.set_auth_feedback(data, color="#008800")
                self.btn_send_code.config(state="normal")
                self.code_entry.focus()
                messagebox.showinfo("Login Code Sent", data)
            elif msg_type == "auth_error":
                self.set_auth_feedback(f"Error: {data}", color="#cc0000")
                self.btn_send_code.config(state="normal")
                self.btn_login.config(state="normal")
                messagebox.showerror("Authentication Error", data)
            elif msg_type == "auth_needed":
                self.notebook.select(self.auth_frame)
                self.btn_send_code.config(state="normal")
                self.code_entry.focus()
                messagebox.showinfo("OTP Required", "Please check your Telegram app for the OTP code.")
            elif msg_type == "password_needed":
                self.btn_login.config(state="normal")
                self.set_auth_feedback("Two-factor authentication (2FA) password required.", color="#aa5500")
                pwd = simpledialog.askstring("2FA Password", "Enter your 2FA Password:", show='*')
                if pwd and self.engine:
                    phone = self.phone_var.get().strip().replace('+', '')
                    code = self.code_var.get().strip()
                    self.btn_login.config(state="disabled")
                    self.set_auth_feedback("Verifying 2FA password...", color="#0055aa")
                    future = asyncio.run_coroutine_threadsafe(self.engine.sign_in_code(phone, code, pwd), self.loop)
                    future.add_done_callback(lambda fut: fut.exception() and self.ui_queue.put(("auth_error", str(fut.exception()))))
            elif msg_type == "auth_success":
                self.set_auth_feedback("Authenticated successfully!", color="#008800")
                self.btn_login.config(state="normal")
                self.btn_send_code.config(state="normal")
                self.log("Authenticated with Telegram successfully.")

                # Account-aware one-time legacy migration
                account_id = self.get_account_id()
                if account_id:
                    legacy_ids = self.config_mgr.config.get("active_channels", [])
                    migrated = self.db.migrate_legacy_active_channels(account_id, legacy_ids, self.config_mgr)
                    if migrated > 0:
                        self.log(f"Migrated {migrated} legacy channels into persistent Download List.")

                self.refresh_download_list_tree()
                self.notebook.select(self.browse_frame)
                self.on_refresh_list()
            elif msg_type == "storage_error":
                messagebox.showerror("Storage Halt", f"Download halted: {data}\n\nNO fallback to C: drive allowed.")
            elif msg_type == "topics_refreshed":
                cid, count = data
                if hasattr(self, "btn_refresh_topics"):
                    self.btn_refresh_topics.config(state="normal", text="↻ Refresh Topics")
                self.refresh_topic_inspector()
            elif msg_type == "download_ended":
                self.btn_download.config(state="normal")
                if hasattr(self, "btn_download_mgr"):
                    self.btn_download_mgr.config(state="normal")
                if hasattr(self, "btn_download_topics"):
                    # Re-evaluate topic download button
                    if self.current_topic_inspector_channel:
                        acc_id = self.get_account_id()
                        pt, cid, _ = self.current_topic_inspector_channel
                        checked = [tid for (a, p, c, tid) in self.checked_topics if a == acc_id and p == pt and c == cid]
                        self.btn_download_topics.config(
                            state="normal" if checked else "disabled",
                            text=f"⬇ Download Selected Topics ({len(checked)})"
                        )
                self.btn_stop.config(state="disabled")

        if not self._is_closing:
            self._after_id = self.root.after(100, self.poll_queue)

    def on_send_code(self):
        if self.is_monitoring_mode:
            messagebox.showinfo("Notice", "Cannot authenticate while background worker is running.")
            return
        phone = self.phone_var.get().strip().replace('+', '')
        if not phone:
            messagebox.showwarning("Input Error", "Please enter a phone number.")
            self.phone_entry.focus()
            return

        self.set_auth_feedback("Requesting login code from Telegram...", color="#0055aa")
        self.btn_send_code.config(state="disabled")

        def handle_send_done(fut):
            try:
                fut.result()
            except Exception as ex:
                self.ui_queue.put(("auth_error", str(ex)))

        future = asyncio.run_coroutine_threadsafe(self.engine.send_login_code(phone), self.loop)
        future.add_done_callback(handle_send_done)

    def on_login(self):
        if self.is_monitoring_mode: return
        phone = self.phone_var.get().strip().replace('+', '')
        code = self.code_var.get().strip()
        if not code:
            messagebox.showwarning("Input Error", "Please enter the OTP code.")
            self.code_entry.focus()
            return

        self.set_auth_feedback("Verifying code with Telegram...", color="#0055aa")
        self.btn_login.config(state="disabled")

        def handle_login_done(fut):
            try:
                fut.result()
            except Exception as ex:
                self.ui_queue.put(("auth_error", str(ex)))

        future = asyncio.run_coroutine_threadsafe(self.engine.sign_in_code(phone, code), self.loop)
        future.add_done_callback(handle_login_done)

    def on_refresh_list(self):
        if self.is_monitoring_mode: return
        self.search_var.set("")
        asyncio.run_coroutine_threadsafe(self.engine.fetch_dialogs(), self.loop)

    def on_download(self):
        if self.is_monitoring_mode: return

        # Check storage before starting
        target = self.storage_path_var.get().strip()
        valid, msg, _ = self.config_mgr.validate_storage(target)
        if not valid:
            messagebox.showerror("Invalid Storage", f"Cannot start downloads:\n{msg}\n\nNO fallback to C: is permitted.")
            return

        account_id = self.get_account_id()
        if not account_id:
            messagebox.showwarning("Authentication Required", "Please log in on the Login tab first.")
            return

        try:
            active_selections = self.db.get_active_selections(account_id)
        except Exception as e:
            messagebox.showerror("Database Error", f"Failed to retrieve active selections from database: {e}")
            return

        if not active_selections:
            messagebox.showwarning(
                "No Selections",
                "Your Download List is empty (0 channels selected).\n\n"
                "Please go to the 'Browse & Search Channels' tab and click '+ Add' on the channels you want to download."
            )
            self.notebook.select(self.browse_frame)
            return

        self.on_save_settings()

        async def prepare_and_run():
            self.ui_queue.put(("status", "Resolving channel entities from Telegram..."))
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
                    self.ui_queue.put(("log", f"Warning: Could not resolve channel {sel['channel_id']} ({sel['title']})."))

            if not selected_dialogs:
                self.ui_queue.put(("log", "Error: None of the selected channels could be resolved."))
                self.ui_queue.put(("download_ended", None))
                return

            self.ui_queue.put(("log", f"Starting download pass for {len(selected_dialogs)} channels from persistent Download List..."))
            await self.engine.run_batch(selected_dialogs)

        self.btn_download.config(state="disabled")
        if hasattr(self, "btn_download_mgr"):
            self.btn_download_mgr.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.notebook.select(self.main_frame)

        asyncio.run_coroutine_threadsafe(prepare_and_run(), self.loop)

    def on_stop(self):
        if self.engine:
            self.log("Stopping downloads gracefully...")
            self.engine.cancel_flag = True
            self.btn_stop.config(state="disabled")

    def on_close(self):
        self._is_closing = True
        if hasattr(self, "_after_id") and self._after_id:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self.engine and self.engine.client:
            try:
                asyncio.run_coroutine_threadsafe(self.engine.client.disconnect(), self.loop)
            except Exception:
                pass
        self.lock.release_lock()
        self.root.destroy()

def main():
    root = tk.Tk()
    app = TelegramGUIApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()

if __name__ == "__main__":
    main()
