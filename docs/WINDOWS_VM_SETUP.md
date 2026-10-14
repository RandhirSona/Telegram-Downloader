# Windows VM Setup Guide - Telegram Downloader

## Overview
This guide describes how to install and run the Telegram Downloader on a Windows VM hosted under Proxmox, storing downloads on any user-configurable storage volume.

---

## Architecture & Paths
- **VM Application Directory:** `C:\TelegramData`
- **VM Target Storage:** User-Configurable (e.g. `D:\TelegramDownloads`, `E:\TelegramMedia`, external disk, or network share `\\Storage\Media`)

---

## Step 1: Clone or Extract Repository to Windows VM
1. On your Windows VM, create the folder: `C:\TelegramData`
2. Clone or extract the repository into `C:\TelegramData`.
3. Verify that the following files are present:
   - `telegram_gui_downloader.py`
   - `telegram_engine.py`
   - `worker_headless.py`
   - `db_tracker.py`
   - `config_manager.py`
   - `instance_lock.py`
   - `requirements.txt`
   - `.env.example`
   - `config.json.example`
   - `install_windows.bat`
   - `start_downloader.bat`
   - `start_worker.bat`

---

## Step 2: Install Python & Run Installer
1. Install **Python 3.10+** (64-bit) from [python.org](https://www.python.org/downloads/).
   - **Crucial:** Check the box: `[x] Add Python to PATH` during installation.
2. In File Explorer, navigate to `C:\TelegramData`.
3. Double-click `install_windows.bat`.
   - This script automatically creates `.venv`, upgrades pip, and installs all required dependencies (`Telethon`, `cryptg`, `python-dotenv`, `tkcalendar`, `babel`).
   - It also generates `.env` and `config.json` from their templates.

---

## Step 3: Configure Telegram API Credentials
1. Open `C:\TelegramData\.env` in Notepad.
2. Fill in your `API_ID`, `API_HASH`, and `PHONE`:
   ```ini
   API_ID=12345678
   API_HASH=abcdef0123456789abcdef0123456789
   PHONE=919876543210
   SESSION_NAME=anon
   ```
3. Save and close `.env`.

---

## Step 4: First Launch & Storage Selection
1. Double-click `start_downloader.bat`.
2. The Tkinter GUI will open.
3. If not already authenticated, navigate to the **Login** tab:
   - Enter your phone number and click **Send Code**.
   - Enter the OTP code sent to your official Telegram app.
   - Enter your 2FA password if prompted.
4. Once authenticated, switch to the **Downloader & Logs** tab:
   - Look at the **Download Location** bar at the top.
   - Click **Browse...** and select your chosen storage drive or folder (e.g. `D:\TelegramDownloads` or your mapped share).
   - Click **Save / Apply** to validate and persist the directory.
   - Click **Check Space** to verify write access and live disk usage.
5. Select your target channels/groups and preferred media filters (PDFs, Videos, etc.).
6. Click **Save Active Channels** to save this selection for automated 24/7 background downloading.

---

## Step 5: Verification & Safety Guarantees
- **No-C:-Fallback Guard:** If your configured storage drive is ever detached or full, the downloader halts immediately. It will **never** silently write large files to your `C:\` system drive.
- **Atomic Downloads:** In-progress transfers write to `.part` files. Only 100% verified complete files are promoted to final filenames.
- **Duplicate Protection:** Media is tracked by Telegram channel ID and message ID in `download_tracker.db`.
