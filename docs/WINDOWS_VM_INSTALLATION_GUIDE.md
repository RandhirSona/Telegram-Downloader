# Windows VM Installation & Operational Guide

This document provides complete instructions for deploying, configuring, and maintaining the Telegram Downloader on a Windows Virtual Machine hosted within Proxmox VE.

---

## 1. System Requirements & Prerequisites

- **Host Environment:** Proxmox VE 7.x or 8.x
- **Guest OS:** Windows 10, Windows 11, or Windows Server 2019/2022 (64-bit)
- **CPU / RAM:** 2 vCPUs, 4 GB RAM recommended
- **Operating Storage:** 20 GB free on `C:\` for OS and Python environment
- **Media Storage:** Any dedicated or shared virtual disk (VirtIO SCSI), external drive, or mapped SMB/NFS share (e.g. `D:\`, `E:\`, or `\\192.168.1.4\Media`)
- **Python Version:** Python 3.10 to 3.12 (64-bit) with `Add Python to PATH` enabled

---

## 2. Proxmox Storage Preparation

### Option A: VirtIO SCSI Virtual Disk (Fastest)
1. In Proxmox VE, select your Windows VM.
2. Navigate to **Hardware** -> **Add** -> **Hard Disk**.
3. Bus/Device: `VirtIO SCSI`. Size: Choose any disk size according to your media storage needs.
4. Inside Windows Disk Management (`diskmgmt.msc`):
   - Initialize disk as **GPT**.
   - Create a Simple Volume formatted as **NTFS** with default allocation unit size.
   - Assign drive letter: `D:` (or preferred).

### Option B: Mapped SMB Network Share
1. If using a NAS or Proxmox ZFS pool shared over SMB (e.g. `\\192.168.1.4\Media`):
2. In Windows File Explorer, map the share as a persistent network drive (e.g. `M:`), or use the direct UNC path in the downloader.

---

## 3. Application Installation

1. Clone or extract the repository into `C:\TelegramData`.
2. Open `C:\TelegramData` and run `install_windows.bat`.
3. The script will:
   - Verify Python is present.
   - Create the isolated `.venv` environment.
   - Install `Telethon`, `cryptg` (for accelerated decryption), `python-dotenv`, `tkcalendar`, and `babel`.
   - Prepare initial template files (`.env` and `config.json`).

---

## 4. Credentials & First-Time Login

1. Edit `C:\TelegramData\.env`:
   ```ini
   API_ID=your_api_id
   API_HASH=your_api_hash
   PHONE=your_phone_number
   SESSION_NAME=anon
   ```
2. Double-click `start_downloader.bat`.
3. On the **Login** tab:
   - Click **Send Code**.
   - Enter the OTP code received from Telegram.
   - Enter your 2FA password if prompted.
4. On successful login, the app opens the **Downloader** tab.

---

## 5. Storage Configuration

1. In the **Download Location** bar at the top of the GUI:
   - Click **Browse...** and select your chosen storage folder (e.g. `D:\TelegramDownloads`).
   - Click **Save / Apply** to save and validate the folder.
   - Confirm the badge shows green: `Free: ... GB / ... GB (Ready)`.
2. Check your desired channels from the left list.
3. Choose your media types (PDFs, Videos, etc.) and optional cutoff date.
4. Click **Save Active Channels**.

---

## 6. Verification and Troubleshooting

| Symptom | Root Cause | Solution |
|---|---|---|
| `Storage: NONE SELECTED` | Storage path not chosen | Click Browse and select your storage volume. |
| `STORAGE HALT: Storage unreachable` | Storage disk unmounted or disconnected | Verify drive letter or share in File Explorer. Downloader halts safely without writing to C:. |
| `Rate limited. Pausing for Xs...` | Telegram FloodWait triggered | Normal behavior. Engine automatically waits for countdown to expire before resuming. |
| `Another instance is running` | Worker or previous GUI instance active | Check Task Manager or `app_data\downloader.lock`. GUI opens in Monitoring Mode automatically if worker is active. |
