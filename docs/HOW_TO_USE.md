# Telegram Downloader — User Guide

This guide walks you through using the Telegram Downloader application.

---

## 1. Authentication (Login Tab)

1. **Enter Phone Number:** In the **Login** tab, enter your Telegram phone number with country code but without the `+` sign (e.g. `1234567890`).
2. **Request Code:** Click **Send Code**. You will receive an official One-Time Password (OTP) code in your Telegram app.
3. **Submit OTP:** Enter the OTP into the **OTP Code** field and click **Login**.
4. **Two-Factor Authentication (2FA):** If 2FA is enabled on your Telegram account, a prompt will appear requesting your 2FA password.
5. **Session Persistence:** Once authenticated, Telethon maintains an authorized session file (`anon.session`). You will remain logged in across application restarts.

---

## 2. Browsing & Selecting Channels (Browse & Search Channels Tab)

1. **Channel Catalog:** The catalog lists all your accessible Telegram dialogs (broadcast channels, supergroups, and groups) with their title, type, Telegram ID, and username.
2. **Instant Search:** Type any keyword into the search bar to filter channels dynamically. The search results update in real-time.
3. **Checkbox Multi-Selection:**
   - Click the checkbox column (`☐` / `☑`) or press the Spacebar to toggle selection.
   - Hold `Shift` or `Ctrl` to select multiple rows, then click any checkbox in the selection to toggle all selected rows simultaneously.
4. **State Indicators:**
   - `☑ In Download List`: Channel is already saved in your persistent download queue.
   - `☑ Pending Add`: Channel is temporarily checked and ready to be added.
   - `☐ Not Added`: Channel is unselected.
5. **Add to Download List:** Click **Add Selected to Download List**. The selected channels will be saved to SQLite and added to your persistent queue without clearing your search query.

---

## 3. Managing Your Download Queue (Download List Manager Tab)

1. **Persistent Queue:** Displays all channels currently queued for active downloading across application sessions and headless background workers.
2. **Verified Channel Titles:** Channel titles reflect verified Telegram entity names rather than placeholder IDs.
3. **Forum Detection:** Identifies whether a chat is an ordinary channel or a **Forum Supergroup**.
4. **Actions:**
   - **Start Downloads:** Switch to active downloading.
   - **Remove Selected:** Remove one or more selected channels from the active queue.
   - **Clear All Selections:** Clear the download queue.

---

## 4. Downloader & Logs Tab

1. **Storage Destination:**
   - Specify your download folder (e.g. `D:\TelegramDownloads`).
   - Click **Check Space** to verify free disk space against the configured threshold (`min_free_space_gb`).
2. **Download Settings:**
   - **Media Filters:** Toggle file types to download: *PDFs*, *Videos*, *Photos*, *Voice/Audio*, *Documents (Other)*, or *All Media*.
   - **Cutoff Date:** Specify a date (`YYYY-MM-DD`) to download only messages posted on or after this date.
   - **Forum Organization:** Enable **Organize Forum Supergroup Topics into Subfolders** to route files into dedicated subfolders for each native Telegram topic.
3. **Execution Controls:**
   - **Start Download:** Begins downloading files according to your queue and filters.
   - **Stop:** Gracefully stops the active download loop after the current chunk completes.
4. **Live Progress:**
   - Green progress bar showing download completion percentage.
   - Real-time transfer speed and estimated time remaining (ETA).
   - Live log console detailing chunk downloads, crypto acceleration, and topic routing.

---

## 5. Directory Organization

### Ordinary Broadcast Channels (Flat Layout)
```
D:\TelegramDownloads\
└── Channel Name\
    ├── document_01.pdf
    └── video_01.mp4
```

### Forum Supergroups (Topic-Aware Layout)
```
D:\TelegramDownloads\
└── Supergroup Name\
    ├── General\
    │   └── announcement.pdf
    ├── Module 1 - Overview\
    │   └── lecture_01.mp4
    └── Unclassified\
        └── standalone_file.zip
```

---

## 6. Resuming & Duplicate Prevention

- **Chunked Transfers:** Files are downloaded in 512 KB chunks with fast hardware AES-NI crypto acceleration.
- **Partial Downloads (`.part`):** In-progress files are written to `.part` files. If interrupted, the download resumes from the exact byte offset where it stopped.
- **SQLite History:** Download status is tracked in SQLite. Once completed, a file will never be redownloaded.
