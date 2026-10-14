# 24/7 Unattended Automation & Windows Task Scheduler Guide

## Overview
This document describes how to configure the Telegram Downloader to run 24/7 in unattended background mode on your Windows VM hosted under Proxmox.

---

## Architecture: Headless Worker vs GUI
- **`worker_headless.py`**: A lightweight CLI process that owns the Telethon session, reads target channels and storage paths from `config.json`, downloads media quietly, handles Telegram rate limits, and sleeps between scans.
- **`telegram_gui_downloader.py`**: An interactive desktop GUI. If the headless worker is running, the GUI automatically opens in **Monitoring Mode**, displaying live worker stats, speed, and active files without interfering with the session or database.
- **Mutual Exclusion (`instance_lock.py`)**: Only one process at a time owns active downloading. SQLite database WAL mode allows simultaneous read monitoring.

---

## Automatic Task Registration
1. Open PowerShell or Command Prompt **as Administrator**.
2. Navigate to `C:\TelegramData`.
3. Run:
   ```cmd
   create_task_scheduler.bat
   ```
4. The script registers a task named `TelegramDownloader24x7` configured to:
   - Run: `C:\TelegramData\.venv\Scripts\pythonw.exe C:\TelegramData\worker_headless.py`
   - Working Directory: `C:\TelegramData`
   - Trigger: At user logon / system boot
   - Run with highest privileges.

---

## Manual Task Scheduler Configuration (Alternative)
If you prefer to configure Windows Task Scheduler manually via GUI (`taskschd.msc`):

1. Press `Win + R`, type `taskschd.msc`, and press **Enter**.
2. In the right pane, click **Create Task...**
3. **General Tab:**
   - Name: `TelegramDownloader24x7`
   - Security Options: Select `Run whether user is logged on or not` (or `Run only when user is logged on`).
   - Check `Run with highest privileges`.
4. **Triggers Tab:**
   - Click **New...**
   - Begin the task: `At startup` (or `At log on`).
   - Click **OK**.
5. **Actions Tab:**
   - Click **New...**
   - Action: `Start a program`
   - Program/script: `C:\TelegramData\.venv\Scripts\pythonw.exe`
   - Add arguments: `C:\TelegramData\worker_headless.py`
   - Start in: `C:\TelegramData`
   - Click **OK**.
6. **Conditions Tab:**
   - Check: `Start only if the following network connection is available: Any connection`.
7. **Settings Tab:**
   - Check: `Allow task to be run on demand`.
   - Check: `If the task fails, restart every: 5 minutes` (Up to 3 times).
   - Uncheck: `Stop the task if it runs longer than...` (must run indefinitely).
   - If the task is already running: `Do not start a new instance`.

---

## Monitoring and Logs
- **Log Files:** Live logs are continuously written to:
  `C:\TelegramData\logs\worker.log`
  *(Automatically rotated at 10 MB per file, keeping 5 backups).*
- **Desktop Monitoring:** Launch `start_downloader.bat` at any time. The GUI displays:
  `[Worker PID 1234]: Scanning messages in Tech Discussions...`
- **Stopping the Worker:**
  To stop the background task:
  ```cmd
  schtasks /End /TN "TelegramDownloader24x7"
  ```
