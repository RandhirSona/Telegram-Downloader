# Telegram Downloader

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Telethon 1.43.2](https://img.shields.io/badge/telethon-1.43.2-blue.svg)](https://github.com/LonamiWebs/Telethon)
[![Platform Windows](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-lightgrey.svg)](https://microsoft.com/windows)
[![Tests Passing](https://img.shields.io/badge/tests-174%20passed-brightgreen.svg)]()
[![License MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

An enterprise-grade, high-reliability Telegram media downloader designed for seamless batch media acquisition, unattended 24/7 background operation, and intelligent forum topic subfolder routing on Windows.

---

## Table of Contents

- [Project Overview](#project-overview)
- [Key Features](#key-features)
- [Screenshot Gallery](#screenshot-gallery)
- [System Requirements](#system-requirements)
- [Installation Guide](#installation-guide)
- [First-Time Authentication](#first-time-authentication)
- [Application Usage](#application-usage)
  - [1. Login Tab](#1-login-tab)
  - [2. Browse & Search Channels](#2-browse--search-channels)
  - [3. Download List Manager & Forum Topic Browser](#3-download-list-manager--forum-topic-browser)
  - [4. Downloader & Logs](#4-downloader--logs)
- [Folder & Storage Architecture](#folder--storage-architecture)
- [Resuming & Duplicate Prevention](#resuming--duplicate-prevention)
- [Headless Worker (24/7 Background Service)](#headless-worker-247-background-service)
- [Troubleshooting & FAQ](#troubleshooting--faq)
- [Security & Privacy](#security--privacy)
- [Automated Testing](#automated-testing)
- [Development Roadmap](#development-roadmap)
- [License](#license)

---

## Project Overview

**Telegram Downloader** provides a robust solution for backing up, organizing, and downloading large volumes of documents, videos, and media from Telegram broadcast channels, private groups, and native **Forum Supergroups**. 

Built with **Python**, **Telethon MTProto API**, and **SQLite WAL**, the application is optimized for high-throughput downloads with hardware-accelerated AES-NI cryptography, atomic chunking, and intelligent crash recovery. It features both a desktop Tkinter GUI and a dedicated headless daemon (`worker_headless.py`) for automated background execution.

---

## Key Features

- **Native Telegram User Authentication:** Direct official Telegram MTProto connection supporting user phone numbers, OTP verification, and optional Two-Factor Authentication (2FA).
- **Interactive Chat Catalog & Instant Filter:** Browse all accessible dialogs with real-time search filtering that preserves active queries when selecting channels.
- **Checkbox Multi-Selection:** Efficient bulk selection using checkboxes (`☐` / `☑`) with standard `Shift` and `Ctrl` range selection.
- **Persistent Download List Manager:** Retains selected target channels across sessions, displaying verified Telegram titles and chat types (`Broadcast Channel` vs `Forum Supergroup`).
- **Interactive Forum Topic Browser (Stage B):** Inspect nested forum topics with verified topic titles, stable topic IDs, closed/hidden status, and estimated local folder paths.
- **Selective Topic Downloading & Composite Identity Guards:** Download only user-selected topics with fail-closed security guards that strictly validate the composite identity `(account_id, peer_type, channel_id)`.
- **Native Forum Topic Subfolder Routing:** Automatically detects Telegram native forum supergroups, enumerates and caches forum topics, and routes messages directly into topic-specific directories (`<GroupName>/<TopicName>/<Filename>`).
- **Preserved Flat Layout for Ordinary Channels:** Non-forum broadcast channels and simple groups retain a clean flat folder structure (`<GroupName>/<Filename>`).
- **Resilient Chunked Resume:** Downloads media in 512 KB chunks to temporary `.part` files. If a connection drops, downloads resume from the exact byte offset without redownloading.
- **SQLite State Tracking & Duplicate Prevention:** Every completed download is tracked by Telegram channel ID and message ID in a local SQLite database in WAL mode. Completed files are never redownloaded.
- **Hardware-Accelerated Cryptography:** Seamlessly leverages `cryptg` / OpenSSL AES-NI for ultra-fast Telegram MTProto transport decryption.
- **Storage Safety Guards:** Real-time free disk space validation against configurable thresholds (`min_free_space_gb`). Downloads halt safely before filling the disk.
- **Decoupled Headless Worker:** Includes a headless background worker (`worker_headless.py`) with process locking (`instance_lock.py`), allowing unattended continuous downloading while the GUI runs in live monitoring mode.

---

## Screenshot Gallery

> [!NOTE]
> **Privacy & Demonstration Notice:** All screenshots illustrate the actual Tkinter desktop application running on Windows using simulated, privacy-masked channel titles (`UPP***`, `Pre***`), synthetic IDs (`1001-1007`), and simulated progress telemetry to protect personal account privacy and credentials.

All screenshots below represent the actual application interface running on Windows:

| Login & Authentication | Channel Catalog & Search |
| :---: | :---: |
| ![Login Tab](docs/screenshots/login.png) | ![Browse Channels](docs/screenshots/browse-channels.png) |
| *Official Telegram OTP and 2FA authentication* | *Live channel catalog with search filtering* |

| Checkbox Multi-Selection | Persistent Download List Manager |
| :---: | :---: |
| ![Search Multi-Select](docs/screenshots/search-multiselect.png) | ![Download List](docs/screenshots/download-list.png) |
| *Bulk channel selection without search resets* | *Verified titles, forum supergroup detection & queue management* |

| Active Download Progress & Logs | Storage & Organization Settings |
| :---: | :---: |
| ![Download Progress](docs/screenshots/download-status.png) | ![Settings View](docs/screenshots/settings.png) |
| *Real-time speed, progress bar & MTProto logs* | *Forum topic subfolder toggle & storage space validation* |

---

## System Requirements

- **Operating System:** Windows 10, Windows 11, or Windows Server 2022+ (64-bit).
- **Python Version:** Python 3.10, 3.11, 3.12, or 3.13 (64-bit).
- **Core Dependencies:**
  - `Telethon==1.43.2`
  - `cryptg==0.6.0`
  - `python-dotenv==1.2.2`
  - `tkcalendar==1.6.1`
  - `babel==2.18.0`
  - `Pillow>=10.0.0`
- **Telegram Credentials:** Valid `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org).

---

## Installation Guide

Follow these step-by-step instructions in PowerShell to install and run the application:

### 1. Clone the Repository
```powershell
git clone https://github.com/RandhirSona/Telegram-Downloader.git
cd Telegram-Downloader
```

### 2. Create a Virtual Environment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install Required Dependencies
```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Create Configuration Files
Copy the provided configuration templates:
```powershell
Copy-Item .env.example .env
Copy-Item config.json.example config.json
```

### 5. Obtain Telegram API Credentials
1. Log in to [https://my.telegram.org](https://my.telegram.org) using your Telegram phone number.
2. Navigate to **API development tools**.
3. Create a new application (e.g. named `MyDownloader`).
4. Copy your `api_id` and `api_hash`.
5. Open `.env` and fill in your credentials:
   ```ini
   API_ID=12345678
   API_HASH=0123456789abcdef0123456789abcdef
   PHONE=1234567890
   SESSION_NAME=anon
   ```

### 6. Configure Download Storage

You can configure the download storage directly from the Tkinter GUI or via `config.json`:

```json
{
  "download_directory": "D:\\TelegramDownloads",
  "historical_download_directories": [],
  "min_free_space_gb": 5.0,
  "scan_interval_minutes": 15,
  "concurrent_downloads": 2,
  "organize_forum_topics": true,
  "media_filters": {
    "PDFs": true,
    "Videos": true,
    "Photos": false,
    "Voice/Audio": false,
    "Documents (Other)": false,
    "All Media": false
  },
  "cutoff_date": ""
}
```

#### Choosing and Changing the Download Location
- **In the GUI ("Downloader & Logs" Tab):**
  1. Click **Browse...** to select any folder on any local drive or mapped network share.
  2. Click **Save / Apply** to validate and persist the directory.
  3. If the selected folder does not exist, the application will prompt for confirmation before safely creating it.
  4. If a folder on the system drive (`C:`) is chosen, the application presents a confirmation prompt explaining the disk usage implications.
  5. The active path is immediately displayed, and changes persist across application restarts.
- **Changing Storage Locations Safely:**
  - Changing the storage location applies exclusively to **future downloads**.
  - **No automated moving or renaming:** Existing downloaded media remains untouched on the original drive/folder.
  - **Zero duplicate downloads:** Previously completed downloads recorded in SQLite are recognized and will never be re-downloaded to the new location.
  - **Partial files protected:** In-progress `.part` files on previous storage locations remain associated with their original directory and are never guessed or corrupted under the new root.
  - **Active transfer guard:** Root changes are prohibited while transfers are active. Stop active transfers before applying a new directory.
- **Handling Disconnected or Missing Drives:**
  - Both the desktop GUI and headless background worker validate storage availability before every transfer and during chunk writing.
  - If a configured drive is detached, unmounted, or full, transfers halt safely with a clear notification.
  - **Zero silent fallback:** The application will **never** silently fall back to `C:\` or any unexpected directory.
- **No Dedicated 2 TB Drive Requirement:**
  - Any storage drive, partition, external disk, or SMB network share with sufficient free space can be used. No specific drive letter, disk size, or volume label is required.

### 7. Launch the Application
To launch the desktop GUI:
```powershell
.\.venv\Scripts\python.exe telegram_gui_downloader.py
```
*(Or double-click `start_downloader.bat`)*

---

## First-Time Authentication

When launching the application for the first time:

1. Navigate to the **Login** tab.
2. Confirm your phone number with your country code (e.g. `1234567890` without leading `+`).
3. Click **Send Code**. Telegram will deliver a login code to your official Telegram app or SMS.
4. Enter the code in the **OTP Code** box and click **Login**.
5. If Two-Factor Authentication is enabled on your account, enter your cloud password when prompted.
6. Once logged in, Telethon stores your credentials in an authorized session file (`anon.session`). Future launches authenticate automatically.

---

## Application Usage

### 1. Login Tab
Displays current authentication status, session validity, and controls for sending verification codes and logging in.

### 2. Browse & Search Channels
- Displays all channels and groups associated with your account.
- **Search Bar:** Real-time search by channel title or username. Search results remain visible when adding channels.
- **Checkbox Multi-Selection:** Check or uncheck individual items or use `Shift`/`Ctrl` range selection.
- **Bulk Action:** Click `Add Selected to Download List` to add items directly to your persistent queue.

### 3. Download List Manager & Forum Topic Browser
- **Queue Overview:** Displays channels currently active in your download queue with verified real titles, chat types (`Forum Supergroup` vs `Broadcast Channel`), and dates added.
- **Forum Topic Browser Split-Pane:** When selecting any forum supergroup, the lower panel dynamically displays its nested forum topics with verified topic titles, stable topic IDs, availability status (open/closed/hidden), and estimated local folder paths.
- **Topic Multi-Selection:** Check or uncheck individual topics using checkboxes (`☐` / `☑`), or use bulk **Select All Topics** / **Unselect All Topics** controls.
- **Dedicated Topic Action:** Click **Download Selected Topics** to download media exclusively from the checked topics of that specific supergroup without affecting other channels.
- **Queue Management:** Remove selected channels or clear the entire queue with a single click.

### 4. Downloader & Logs
- **Storage Destination:** Set and validate target download path with live free-space reporting.
- **Media Filters:** Toggle download of PDFs, Videos, Photos, Audio, and Other Documents.
- **Cutoff Date:** Filter out older messages prior to a specific date (`YYYY-MM-DD`).
- **Organization Setting:** Toggle *Organize Forum Supergroup Topics into Subfolders*.
- **Progress & Speed:** Green progress bar indicating active file completion percentage, live download speed (MB/s), and ETA.
- **Live Logs:** Real-time streaming log output detailing chunk downloads, rate limit handling, and topic routing.

---

## Folder & Storage Architecture

### Standard Broadcast Channels (Flat Layout)
Files from regular broadcast channels are stored in a dedicated channel directory:
```
D:\TelegramDownloads\
└── Tech Announcements\
    ├── Guidelines_2026.pdf
    └── Introduction_Video.mp4
```

### Forum Supergroups (Topic Subfolder Layout)
When *Organize Forum Supergroup Topics into Subfolders* is active:
```
D:\TelegramDownloads\
└── Discussion Supergroup\
    ├── General\
    │   └── welcome_notes.pdf
    ├── Mathematics\
    │   ├── lecture_01.mp4
    │   └── practice_set.pdf
    ├── Physics\
    │   └── lab_manual.pdf
    └── Unclassified\
        └── standalone_document.zip
```

### Sanitization & Safety
- **Windows Illegal Characters:** Automatically replaces invalid characters (`\ / : * ? " < > |`) with underscores (`_`).
- **Reserved Device Names:** Automatically avoids collisions with Windows reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`).
- **Unicode Support:** Fully preserves multilingual titles (e.g. Hindi, Russian, Arabic, Chinese, Emoji).
- **Name Collisions:** Identical topic titles within the same supergroup are automatically disambiguated (e.g. `Notes` and `Notes [12]`).

---

## Resuming & Duplicate Prevention

```mermaid
flowchart TD
    A[Start Message Download] --> B{In SQLite History?}
    B -- Yes: Status = Completed --> C[Skip Message - Already Downloaded]
    B -- No --> D{Does .part Exist?}
    D -- Yes --> E[Inspect .part File Size]
    E --> F[Resume Transfer from Byte Offset]
    D -- No --> G[Begin Transfer from Byte 0]
    F --> H[Download 512 KB Chunks]
    G --> H
    H --> I[Complete All Chunks]
    I --> J[Atomic Rename: .part to Final Name]
    J --> K[Record Completed in SQLite]
```

- **Atomic Staging:** In-progress downloads write to `<filename>.part`. Only fully downloaded and verified files are promoted to their final filename.
- **Offset Resuming:** If network connectivity drops or the application restarts, downloads resume at the exact byte offset where they stopped.
- **Duplicate Prevention:** Completed files are recorded in SQLite `downloads` table with channel ID, message ID, file size, and path. Files are never downloaded twice.

---

## Headless Worker (24/7 Background Service)

For servers and unattended virtual machines, `worker_headless.py` runs as a continuous background daemon:

```powershell
.\.venv\Scripts\python.exe worker_headless.py
```

### Key Worker Capabilities
- **Reads Persistent Selections:** Automatically picks up channels saved in the SQLite `download_selections` table.
- **Periodic Scanning:** Loops through selected channels every `scan_interval_minutes` (default: 15 minutes).
- **Process Mutual Exclusion:** Uses `instance_lock.py` to prevent session conflicts between the GUI and background worker.
- **GUI Monitoring Mode:** If the background worker is running, launching the GUI automatically connects in read-only **Monitoring Mode**, displaying live download progress and logs without interrupting the worker.
- **Windows Task Scheduler:** Register the worker as an unattended startup task using `create_task_scheduler.bat`. See [docs/24X7_TASK_SCHEDULER.md](docs/24X7_TASK_SCHEDULER.md) for full instructions.

---

## Troubleshooting & FAQ

### 1. `Invalid API_ID or API_HASH`
Ensure you copied the exact numerical `API_ID` and 32-character hexadecimal `API_HASH` from [my.telegram.org](https://my.telegram.org) into `.env` without surrounding quotes or spaces.

### 2. `FloodWaitError` (Telegram Rate Limit)
Telegram temporarily restricts accounts that issue too many requests. The downloader automatically detects `FloodWaitError`, logs the wait time, and sleeps until the restriction expires before continuing.

### 3. `Storage Unavailable` or Insufficient Disk Space
If the target drive is detached or free space falls below `min_free_space_gb` (default: 5.0 GB), downloading pauses immediately with a warning. Reconnect your drive or free disk space, then click **Check Space**.

### 4. `Channel Private / Access Denied`
Ensure your Telegram account is currently a member of the target channel or group. For private channels, you must join the channel first before files can be enumerated.

### 5. Database Locked (`sqlite3.OperationalError`)
The database uses SQLite WAL mode (`PRAGMA journal_mode=WAL;`), allowing concurrent reads. Do not open `download_tracker.db` with external database editors in exclusive write mode while downloads are running.

---

## Security & Privacy

> [!WARNING]
> Never share or commit your `.env`, session files (`*.session`), or database (`download_tracker.db`) to public repositories.

- **Session Secrets:** Telegram session files contain active cryptographic keys that allow complete account access. They are strictly excluded via `.gitignore`.
- **Local Data Only:** No telemetry, downloaded content, or user credentials are transmitted to any third-party server. All data remains exclusively on your local machine.

---

## Automated Testing

The project includes an isolated unit test suite covering authentication mocks, checkbox multi-selection, database migrations, topic routing, and storage validation:

```powershell
.\.venv\Scripts\python.exe -m unittest discover tests -v
```

**Test Coverage Summary:**
- 20 / 20 Stage B Topic Routing & Navigation Tests Passed
- 35 / 35 Composite Identity & Safety Guard Tests Passed
- 22 / 22 Forum Topic Browser GUI Tests Passed
- 12 / 12 Phase D1 Release-Readiness, Security & Stability Tests Passed
- 174 / 174 Total Unit Tests Passed (0 Regressions)

---

## Development Roadmap & Current Production Status

- [x] **Stage A (Live & Active in Production):** Verified Telegram channel titles and Browse checkbox multi-selection synchronization.
- [x] **Stage B (Live Acceptance Verified):** Native Telegram forum supergroup detection, MTProto topic pagination, topic-aware download routing, interactive Forum Topic Browser in GUI, composite identity security guards `(account_id, peer_type, channel_id)`, and successful live two-PDF bounded acceptance test.
- [ ] **Stage C (In Preparation):** Safe historical existing-folder reconciliation (mapping legacy flat downloads to verified topic directories without modifying files prematurely).
- [ ] **Stage D (Planned):** Automated background archive extraction (`.zip`, `.rar`, `.7z`) for completed downloads.

---

## License

This project is licensed under the [MIT License](LICENSE).
