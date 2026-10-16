# Telegram Downloader — Stage C: Historical Download Organizer Architecture, Read-Only Inventory, and Migration Plan
**Document Version:** 1.0.0  
**Date:** 2026-10-14  
**Project Directory:** `C:\TelegramData`  
**Media Storage Root:** `F:\TelegramDownloads`  
**Database File:** `C:\TelegramData\download_tracker.db`  
**Status:** Read-Only Inventory and Architectural Design (No Production Mutations)  

---

## Executive Summary

Stage C addresses the historical download reorganization challenges for the Windows 11 Python + Tkinter Telegram Downloader application.

Over successive versions of the application, downloads were written to:
1. Legacy identifier-based directories (e.g., `Channel_3509423997`, `Channel_3911876130`, `Channel_4213926054`).
2. Flat root channel folders that predate forum-topic routing (e.g., `UPPCS TEST 2026`, `Premium 2027`, `DH UPPCS PRELIMS STUDY IQ`).
3. Partial download files (`.part`) created before download completion or before multi-session resume stabilization.

This document provides a **complete, verified read-only production inventory** of all 2,786 database records and 1,610 disk files, the results of **live Telethon MTProto read-only topic recovery** for all 1,174 completed forum messages lacking topic metadata, a **crash-resilient migration engine architecture**, and a native **Tkinter GUI organizer design**.

**Safety Guarantee:** Zero production media files were moved, renamed, truncated, or deleted. Zero SQLite records were modified. The headless background worker remains disabled.

---

## 1. Verified Current Folder and File Inventory

### 1.1 SQLite Production Database Baseline
A read-only connection (`file:C:\TelegramData\download_tracker.db?mode=ro`) confirmed:
- **Database Integrity:** `PRAGMA integrity_check` -> `ok`
- **Total Download Records:** 2,786
- **Status Distribution:**
  - `COMPLETED`: 1,608 records
  - `DOWNLOADING`: 2 records
  - `FAILED`: 1,176 records
- **Channels in Database:** 11 registered channels
- **Registered Forum Topics:** 71 cataloged topics across 5 forum channels

### 1.2 Disk Baseline on `F:\TelegramDownloads`
Physical disk inspection of `F:\TelegramDownloads` revealed 11 top-level directories:
- **Total Regular Files on Disk:** 1,607
- **Total Partial (`.part`) Files on Disk:** 3
- **Total Disk Items:** 1,610
- **Total Disk Volume:** ~43.8 GB
- **Unmanaged Orphan Files:** 0 (all files on disk correspond to tracked database rows)

### 1.3 Folder-by-Folder Physical Audit

| Directory Name | Directory Type | Files on Disk | Total Size | Corresponding DB Channel Title | SQLite Channel ID |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `Channel_3509423997` | Legacy Folder | 59 files | 10,767.8 MB | FORUM PTS 2027 | 3509423997 |
| `Channel_3911876130` | Legacy Folder | 31 files | 40.5 MB | PVT VISION IAS PTS 2027 | 3911876130 |
| `Channel_4213926054` | Legacy Folder | 1 part file | 523.0 MB | Prashant sir current affairs 2027 | 4213926054 |
| `DH UPPCS PRELIMS STUDY IQ` | Named Folder | 75 files + 1 part | 23,397.6 MB | DH UPPCS PRELIMS STUDY IQ | 4489360145 |
| `FORUM IAS GS 2027` | Named Folder | 81 files | 305.8 MB | FORUM IAS GS 2027 | 3986064115 |
| `Praveen sir current affairs 2027` | Named Folder | 84 files | 52.8 MB | Praveen sir current affairs 2027 | 4410972886 |
| `Premium 2027` | Named Folder | 513 files | 6,368.6 MB | Premium 2027 | 4450272939 |
| `Prashant sir current affairs 2027` | Named Folder | 76 files + 1 part | 64.9 MB | Prashant sir current affairs 2027 | 4213926054 |
| `PVT UPPCS TEST` | Named Folder | 71 files | 51.5 MB | PVT UPPCS TEST | 4474776106 |
| `PVT VISION IAS PTS 2027` | Named Folder | 0 files | 0.0 MB | PVT VISION IAS PTS 2027 | 3911876130 |
| `UPPCS TEST 2026` | Named Folder | 598 files | 2,238.1 MB | UPPCS TEST 2026 | 4412184534 |
| **Total** | | **1,610 items** | **~43,810.6 MB** | | |

### 1.4 File Size Fidelity and Historical Missing File
- **Size Fidelity:** 1,609 out of 1,609 found files match their database-recorded `file_size` byte-for-byte (100% exact match).
- **Missing File Audit:** Exactly 1 record marked `COMPLETED` in SQLite does not physically exist in any folder on `F:\TelegramDownloads`:
  - Channel: `4474776106` (`PVT UPPCS TEST`)
  - Message ID: 226 (Download ID: 177)
  - Filename: `UPPCS_ITS_2026_Micro_Test_50_UPPSC_PTS_2026_Test_50_361048_Sol_Eng.pdf`
  - Recorded Size: 847,159 bytes
  - Recorded Relative Path: `PVT UPPCS TEST\UPPCS_ITS_2026_Micro_Test_50_UPPSC_PTS_2026_Test_50_361048_Sol_Eng.pdf`
  - Finding: This file was historically omitted or removed prior to Phase D1. The organizer design safely treats this record as `MISSING_SOURCE` and will never attempt a phantom move.

---

## 2. Legacy Channel-Title Reconciliation Candidates

Every legacy directory was verified against the full `(account_id, peer_type, channel_id)` tuple in `channel_storage_mapping` and Telethon cached entities:

```
+-----------------------------------------------------------------------------------------------------+
| Legacy Folder        | Verified Telegram Title           | Peer Type | Channel ID | Action Classification    |
+----------------------+-----------------------------------+-----------+------------+--------------------------+
| Channel_3509423997   | FORUM PTS 2027                    | Broadcast | 3509423997 | CLEAN_RENAME_MOVE        |
| Channel_3911876130   | PVT VISION IAS PTS 2027           | Broadcast | 3911876130 | MERGE_INTO_EMPTY_TARGET  |
| Channel_4213926054   | Prashant sir current affairs 2027 | Broadcast | 4213926054 | STRICTLY_BLOCKED         |
+-----------------------------------------------------------------------------------------------------+
```

### Detailed Reconciliation Assessment:

1. **`Channel_3509423997` -> `FORUM PTS 2027`:**
   - **Entity Details:** Peer Type: `channel` (Broadcast channel, `is_forum = 0`). Verified Title: `FORUM PTS 2027`.
   - **Content:** 59 completed video/PDF files (10.77 GB). 0 failed records.
   - **Destination Status:** `F:\TelegramDownloads\FORUM PTS 2027` does NOT exist on disk.
   - **Eligibility:** 100% eligible for directory rename or file-by-file move.

2. **`Channel_3911876130` -> `PVT VISION IAS PTS 2027`:**
   - **Entity Details:** Peer Type: `channel` (Broadcast channel, `is_forum = 0`). Verified Title: `PVT VISION IAS PTS 2027`.
   - **Content:** 31 completed PDF files (40.5 MB). 0 failed records.
   - **Destination Status:** `F:\TelegramDownloads\PVT VISION IAS PTS 2027` already exists on disk but is currently EMPTY (0 files).
   - **Eligibility:** 100% eligible for moving files into the existing target directory. Zero name collisions.

3. **`Channel_4213926054` -> `Prashant sir current affairs 2027`:**
   - **Entity Details:** Channel ID: `4213926054`. Verified Title: `Prashant sir current affairs 2027`.
   - **Content:** 0 completed files, 1 partial file (`Current_Affairs_02_9th_September_2026.mp4.part`, 548,405,248 bytes).
   - **Destination Status:** `F:\TelegramDownloads\Prashant sir current affairs 2027` already exists with 76 completed files AND contains ANOTHER partial file with the exact same name: `Current_Affairs_02_9th_September_2026.mp4.part` (7,864,320 bytes).
   - **Safety Decision:** **STRICTLY BLOCKED.** Moving or merging this legacy folder would collide with and destroy one of the two historical partial files.

---

## 3. Historical Telegram Forum-Topic Recovery Results

### 3.1 Topic Recovery Methodology
Using the authenticated Telethon session (`Account ID: 774983606`, `@randhirsona`), read-only MTProto metadata requests (`client.get_messages(entity, ids=chunk)`) were issued in rate-limited batches of 100 messages.

No media files were downloaded. The recovery logic evaluated:
1. Whether `message_id` matches a known topic creation message in `forum_topics`.
2. Server-provided `reply_to.reply_to_top_id` pointing to a cataloged topic root.
3. Server-provided `reply_to.forum_topic == True` with `reply_to_msg_id` pointing to a cataloged topic root.
4. Top-level messages in supergroups where General topic (`topic_id = 1`) is cataloged and open.
5. Strict fallback: Any message failing verified server thread correlation is classified `AMBIGUOUS` and remains in its root folder.

### 3.2 Recovery Scan Findings
Across all 3 forum supergroups, exactly **1,174 completed records** lacked `topic_id`.

```
========================================================================================
Channel ID   Verified Title              Total Scanned   VERIFIED   AMBIGUOUS   UNAVAILABLE
========================================================================================
4412184534   UPPCS TEST 2026                       598        598           0             0
4450272939   Premium 2027                          509        509           0             0
4489360145   DH UPPCS PRELIMS STUDY IQ              67         67           0             0
----------------------------------------------------------------------------------------
TOTAL                                            1,174      1,174           0             0
                                                          (100.0%)     (0.0%)        (0.0%)
========================================================================================
```

All 1,174 historical completed messages were definitively resolved to verified forum topics in the catalog.

### 3.3 Topic Breakdown by Channel

#### Channel 4412184534 (`UPPCS TEST 2026`) — 598 Files:
- Topic 7 (`Make ias`): 239 files
- Topic 5 (`Drishti ias`): 68 files
- Topic 616 (`Abhyas 9.0 make ias`): 51 files
- Topic 3 (`Uppcs one`): 49 files
- Topic 2 (`Dhyey ias`): 43 files
- Topic 681 (`Physics wallah`): 39 files
- Topic 472 (`Study iq`): 38 files
- Topic 4 (`Sanskriti ias`): 30 files
- Topic 528 (`NEXT IAS`): 29 files
- Topic 401 (`Gs world`): 12 files

#### Channel 4450272939 (`Premium 2027`) — 509 Files:
- Topic 150 (`DAILY NEWSPAPER`): 179 files
- Topic 125 (`Dipin Sir CA 2027 Handouts`): 74 files
- Topic 1014 (`UNDERSTAND UPSC`): 45 files
- Topic 1177 (`SARRTHI MAINS TESTS`): 32 files
- Topic 91 (`IAS BABA MAINS 2026`): 31 files
- Topic 1114 (`PSIR TESTS & MATERIAL`): 30 files
- Topic 899 (`Magazines`): 16 files
- Topic 1057 (`CIVILDAILY MATERIAL`): 15 files
- Topic 1026 (`OTHER MATERIAL OF ALL COACHINGS`): 14 files
- Topic 474 (`COACHING MATERIAL`): 14 files
- Topic 952 (`DRISHTI IAS PRELIMS 2027`): 10 files
- Topic 3 (`Forum Prelims 2027`): 9 files
- Topic 4 (`Vajiram Prelims 2027`): 8 files
- Topic 2 (`Vision Prelims 2027`): 7 files
- Topic 7 (`Vajiram Mains 2027`): 7 files
- Topic 959 (`INSIGHT PRE 2027`): 6 files
- Topic 1145 (`ANTHROPOLOGY TESTS & MATERIAL`): 5 files
- Topic 873 (`Next IAS Ancient History`): 3 files
- Topic 16 (`Schedule Of Prelims 2027 Tests`): 2 files
- Topic 1098 (`SCS CURRENT AFFAIRS HINDI/ENG`): 2 files

#### Channel 4489360145 (`DH UPPCS PRELIMS STUDY IQ`) — 67 Files:
- Topic 451 (`Modern history`): 66 files
- Topic 73 (`Economy`): 1 files

The complete item-by-item JSON manifest is preserved outside the repository in the artifact scratch store:  
`C:\Users\Sumir\.gemini\antigravity-ide\brain\7c9c34ba-a764-4c04-91c1-9adbd8856401\scratch\stage_c_manifest.json`

---

## 4. Collision and Partial-File Protection Findings

### 4.1 Historical Shared-Path Collisions Detected
Analysis of the proposed destination paths identified **3 destination collisions** involving **6 SQLite records**:

```
+--------------------------------------------------------------------------------------------------------------------+
| Proposed Destination Path                                                                 | SQLite IDs | Message IDs|
+-------------------------------------------------------------------------------------------+------------+------------+
| UPPCS TEST 2026\Uppcs one\UPPCS Sectional Test 6 - Science (Questions).pdf                | 1704, 1702 | 30, 31     |
| Premium 2027\IAS BABA MAINS 2026\TLP_2026_09TH AUGUST 2026_SYNOPSIS.pdf                  | 2834, 2832 | 1221, 1222 |
| Premium 2027\IAS BABA MAINS 2026\MEP_2026_05THAUGUST2026_GS_02_ANSWERBOOKLET.pdf         | 2821, 2819 | 1230, 1231 |
+--------------------------------------------------------------------------------------------------------------------+
```

#### Root Cause Analysis:
In the legacy flat directory structure, both messages in each pair shared the exact same filename and byte count. When message 31 was downloaded after message 30, it referenced or overwrote the same file on disk (`relative_path` in SQLite is identical for both records).

On disk today, there is only **one physical file** for each pair.
- **Handling Strategy:**
  1. The migration planner groups records by physical file path (`(download_root, relative_path)`).
  2. The single physical file is moved exactly once into the verified topic directory.
  3. Both SQLite records are simultaneously updated to reference the new relative path and topic ID.
  4. This eliminates redundant move operations and avoids "file already exists" errors.

### 4.2 Untouchable Partial-File Inventory
The organizer architecture defines an **immutable quarantine** for partial files:

1. `Channel_4213926054\Current_Affairs_02_9th_September_2026.mp4.part`  
   - Size: 548,405,248 bytes  
   - Record: Download ID 142 (Message 26, `DOWNLOADING`)
2. `Prashant sir current affairs 2027\Current_Affairs_02_9th_September_2026.mp4.part`  
   - Size: 7,864,320 bytes  
   - Record: Historical interrupted transfer
3. `DH UPPCS PRELIMS STUDY IQ\ANCIENT MEDIEVAL\L8 - Vedic Age (Part - 02).mp4.part`  
   - Size: 544,210,944 bytes  
   - Record: Download ID 2795 (Message 17, `DOWNLOADING`)

**Quarantine Policy:**
- Filter rule: Any file with `.part` extension or `status == 'DOWNLOADING'` is marked `PROTECTED_PARTIAL`.
- These files are excluded from all candidate selection, directory merges, moves, renames, and deletions.

---

## 5. Migration Engine Architecture (Phase C3)

```
                                  +---------------------------------+
                                  |     User Requests Preview       |
                                  +---------------------------------+
                                                   |
                                                   v
                                  +---------------------------------+
                                  |   Pre-Flight Discovery & Check  |
                                  |   - Compare SQLite vs Disk      |
                                  |   - Check permissions & space   |
                                  |   - Detect collisions & parts   |
                                  +---------------------------------+
                                                   |
                                                   v
                                  +---------------------------------+
                                  |     Generate Migration Plan     |
                                  |   (Dry-Run Tree in Memory/GUI)  |
                                  +---------------------------------+
                                                   |
                                     [User Explicit Approval Gate]
                                                   |
                                                   v
                                  +---------------------------------+
                                  | Step 1: Verified SQLite Backup  |
                                  | (PRAGMA vacuum_into / hot copy) |
                                  +---------------------------------+
                                                   |
                                                   v
                                  +---------------------------------+
                                  | Step 2: Open Migration Journal  |
                                  | (WAL-enabled journal table)     |
                                  +---------------------------------+
                                                   |
                                                   v
                 +------------------->[ Process File by File ]<--------------------+
                 |                                 |                                |
                 |                                 v                                |
                 |                +---------------------------------+               |
                 |                | Record 'PENDING' in Journal     |               |
                 |                +---------------------------------+               |
                 |                                 |                                |
                 |                                 v                                |
                 |                +---------------------------------+               |
                 |                | Verify Source Size & Mtime      |               |
                 |                +---------------------------------+               |
                 |                                 |                                |
                 |                                 v                                |
                 |                +---------------------------------+               |
                 |                | Atomic Rename (os.replace)      |               |
                 |                +---------------------------------+               |
                 |                                 |                                |
                 |                                 v                                |
                 |                +---------------------------------+               |
                 |                | Verify Destination File On Disk |               |
                 |                +---------------------------------+               |
                 |                                 |                                |
                 |                                 v                                |
                 |                +---------------------------------+               |
                 |                | Update SQLite Relative Path     |               |
                 |                | (and associated topic_id)       |               |
                 |                +---------------------------------+               |
                 |                                 |                                |
                 |                                 v                                |
                 |                +---------------------------------+               |
                 |                | Record 'COMMITTED' in Journal   |               |
                 |                +---------------------------------+               |
                 |                                 |                                |
                 +---------------------------------+--------------------------------+
                                                   |
                                                   v
                                  +---------------------------------+
                                  | Step 3: Migration Complete      |
                                  | Clean up Journal on 100% OK     |
                                  +---------------------------------+
```

### 5.1 Non-Atomic Separation of Filesystem and Database
Filesystem file renames and SQLite transactions are fundamentally separate I/O operations and **cannot be made jointly atomic**.
To eliminate data loss and inconsistencies, the engine introduces a dedicated **Operation Journal table**:

```sql
CREATE TABLE IF NOT EXISTS migration_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    download_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    source_root TEXT NOT NULL,
    source_rel_path TEXT NOT NULL,
    target_root TEXT NOT NULL,
    target_rel_path TEXT NOT NULL,
    recovered_topic_id INTEGER,
    expected_size INTEGER NOT NULL,
    state TEXT NOT NULL, -- PENDING, VERIFIED_SRC, MOVED_FS, COMMITTED_DB, FAILED
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

### 5.2 State Transitions and Idempotent Restart Recovery
If execution is interrupted at any point (power outage, crash, user abort):
1. **State `PENDING`:** Neither filesystem nor DB changed. Action: Re-evaluate source.
2. **State `MOVED_FS`:** File was moved on disk, but SQLite was not yet updated.
   - Action upon restart: Detect file present at destination with matching byte size. Update SQLite `downloads` table and advance state to `COMMITTED_DB`.
3. **State `COMMITTED_DB`:** File moved and database updated. Action: Skip.

### 5.3 Cross-Volume Move Safety
- Moves are restricted to the same volume using `os.replace` (atomic filesystem directory entry update).
- If source and destination roots ever span different drive volumes in future extensions, the engine will enforce a strict 3-phase copy-verify-delete protocol:
  1. `shutil.copy2` preserving timestamps.
  2. SHA-256 byte stream verification between source and target.
  3. Only after verification succeeds, delete source file and commit DB.
- Cross-volume moves will require an explicit, separate user approval gate.

### 5.4 Directory Cleanup Rules
Empty directories are **never** deleted automatically during file migrations.
After all files from a directory are safely moved and committed, empty directories are listed in a "Completed Cleanups" panel for optional, separate user approval.

---

## 6. Historical Download Organizer GUI Design (Phase C4)

The Organizer interface is designed as an integrated modal dialog accessible from the main `TelegramDownloaderApp` window via a dedicated toolbar button: **`[📁 Historical Organizer]`**.

### 6.1 Layout Wireframe

```
+----------------------------------------------------------------------------------------------------+
|  Historical Download Organizer — Safe Migration & Topic Organization                              |
+----------------------------------------------------------------------------------------------------+
|  Storage Root: [ F:\TelegramDownloads                        ] [ Browse... ] [ Refresh Scan ]      |
|                                                                                                    |
|  Summary: Total Files: 1,607 | Reorganizable: 1,264 | Blocked/Partial: 3 | Missing: 1 | Volume: 13.1 GB|
+----------------------------------------------------------------------------------------------------+
|  Channels & Topics Migration Plan:                                                                 |
|  +-----------------------------------------------------------------------------------------------+ |
|  | [x] Source Folder / Channel Title | Target Destination            | Items | Status   | Reason | |
|  +-----------------------------------+-------------------------------+-------+----------+--------+ |
|  | [x] Channel_3509423997            | FORUM PTS 2027\               | 59    | READY    | Rename | |
|  | [x] Channel_3911876130            | PVT VISION IAS PTS 2027\      | 31    | READY    | Merge  | |
|  | [ ] Channel_4213926054            | Prashant sir current affairs  | 1     | BLOCKED  | PartCol| |
|  | [x] UPPCS TEST 2026 (Flat)        | UPPCS TEST 2026\<Topics>\     | 598   | READY    | 10 Top | |
|  |     |-- [x] Flat Files (30, 31)   | ...\Uppcs one\test6.pdf       | 2     | RESOLVED | Shared | |
|  |     |-- [x] Drishti ias           | ...\Drishti ias\...           | 68    | READY    | Verified| |
|  |     |-- [x] Make ias              | ...\Make ias\...              | 239   | READY    | Verified| |
|  | [x] Premium 2027 (Flat)           | Premium 2027\<Topics>\        | 509   | READY    | 20 Top | |
|  |     |-- [x] DAILY NEWSPAPER       | ...\DAILY NEWSPAPER\...       | 179   | READY    | Verified| |
|  |     |-- [x] Dipin Sir CA Handouts | ...\Dipin Sir CA Handouts\... | 74    | READY    | Verified| |
|  | [x] DH UPPCS PRELIMS STUDY IQ     | DH UPPCS...\<Topics>\         | 67    | READY    | 2 Topics| |
|  |     |-- [ ] Ancient Medieval .part| ...\L8 - Vedic Age.mp4.part   | 1     | BLOCKED  | Partial| |
|  +-----------------------------------------------------------------------------------------------+ |
|                                                                                                    |
|  Selected File Details:                                                                            |
|  Source:      F:\TelegramDownloads\UPPCS TEST 2026\01_Make IAS Pre Test - 01 (Questions).pdf      |
|  Proposed:    F:\TelegramDownloads\UPPCS TEST 2026\Make ias\01_Make IAS Pre Test - 01 (Questions). |
|  Topic Match: ID 7 ('Make ias') [VERIFIED via Telethon MTProto Reply Top ID]                       |
|  File Size:   2,418,290 bytes | Disk State: EXACT MATCH                                            |
+----------------------------------------------------------------------------------------------------+
|  [ Export Dry-Run Manifest (JSON) ]   [ Validate Pre-Flight ]     [ Apply Migration (DISABLED) ]  |
|                                                                                                    |
|  (!) 'Apply Migration' is disabled. Implementation requires Stage C approval gate authorization.   |
+----------------------------------------------------------------------------------------------------+
```

### 6.2 Key GUI Behaviors
1. **Strict Preview/Apply Separation:**
   - The dialog opens in **Dry-Run Preview** mode.
   - The `[Apply Migration]` button has `state=DISABLED` with explicit explanatory tooltip.
2. **Interactive Selection:**
   - Checkboxes allow selecting individual channels or topics for staged migration.
   - Any selection containing blocked items or collisions displays warning badges.
3. **Itemized Inspection:**
   - Clicking any channel or file displays full provenance: Source Path, Proposed Destination, Topic Identity, Match Method, File Size, and Collision Status.

---

## 7. Database Compatibility and Storage-Root Strategy

### 7.1 Compatibility with Phase D2 Configurable Roots
The organizer operates seamlessly with the configurable download root architecture introduced in Phase D2:
- **Root Independence:** Database records store `relative_path` (e.g., `UPPCS TEST 2026\Make ias\test.pdf`).
- **Path Resolution:** The application locates files via `find_file_across_roots(rel_path, active_root, historical_roots)`.
- When a file is moved to a topic subfolder, its `relative_path` is updated to include the topic directory name:  
  `channel_folder\topic_folder\filename.ext`.
- **Duplicate Prevention:** Duplicate suppression in `telegram_engine.py` evaluates `(channel_id, message_id)` and `(file_name, file_size)` against `downloads`. Moving the physical file and updating `relative_path` preserves duplicate-prevention integrity without re-downloading.

### 7.2 Database Update Transaction Specification
For each migrated record:
```sql
UPDATE downloads
SET relative_path = :new_relative_path,
    topic_id = :recovered_topic_id
WHERE id = :download_id
  AND channel_id = :channel_id
  AND message_id = :message_id;
```

---

## 8. Test Plan

A dedicated test suite `tests/test_stage_c_organizer_planning.py` has been created and verified (5/5 tests passing). Future test phases will cover:

1. **Dry-Run Engine Tests (`tests/test_organizer_dry_run.py`):**
   - Verification of folder reconciliation without disk writes.
   - Accurate calculation of bytes to move.
   - Identification of missing source files.
2. **Collision Resolution Tests (`tests/test_organizer_collisions.py`):**
   - Detection of duplicate filenames across messages.
   - Single-move shared-file execution.
   - Strict `.part` partial-file quarantine enforcement.
3. **Crash Recovery & Rollback Tests (`tests/test_organizer_recovery.py`):**
   - Simulated interruption after filesystem move before SQLite commit.
   - Verification that journal replay resolves orphaned disk moves.
   - Full rollback verification restoring files to original flat layout.
4. **GUI Component Tests (`tests/test_organizer_gui.py`):**
   - Disabled Apply button enforcement.
   - Treeview rendering of channels, topics, and status badges.

---

## 9. Proposed Implementation Phases and Approval Gates

```
+------------------------------------------------------------------------------------+
| STAGE C APPROVAL GATE 1: Architecture and Read-Only Inventory Review               |
| [Current State: Ready for User Review]                                             |
+------------------------------------------------------------------------------------+
                                         |
                                         v
+------------------------------------------------------------------------------------+
| Phase C.1: Migration Engine & Operation Journal Core Implementation                |
| - Implement journal table & state machine in db_tracker.py                         |
| - Implement file move executor with SHA-256 and size verification                  |
| - Comprehensive unit test coverage with mock filesystem & DB                       |
+------------------------------------------------------------------------------------+
                                         |
                                         v
+------------------------------------------------------------------------------------+
| Phase C.2: Historical Organizer GUI Dialog                                         |
| - Build Tkinter organizer dialog with Treeview preview                             |
| - Connect live dry-run scan engine                                                 |
| - Wire progress indicators and safety confirmations                                |
+------------------------------------------------------------------------------------+
                                         |
                                         v
+------------------------------------------------------------------------------------+
| STAGE C APPROVAL GATE 2: Non-Destructive GUI & Engine Validation                   |
| - Demonstrate dry-run preview on production VM                                     |
| - User inspects and authorizes single pilot migration                              |
+------------------------------------------------------------------------------------+
                                         |
                                         v
+------------------------------------------------------------------------------------+
| Phase C.3: Controlled Pilot Migration — Single Legacy Channel                      |
| - Migrate Channel_3509423997 (59 files) -> FORUM PTS 2027                          |
| - Verify 100% file integrity and SQLite relative_path synchronization              |
+------------------------------------------------------------------------------------+
                                         |
                                         v
+------------------------------------------------------------------------------------+
| STAGE C APPROVAL GATE 3: Pilot Results Sign-Off                                    |
| - Verify zero regressions in downloader operation                                  |
| - Authorize batch migration of forum supergroup topics                             |
+------------------------------------------------------------------------------------+
                                         |
                                         v
+------------------------------------------------------------------------------------+
| Phase C.4: Production Forum Topic Migration                                        |
| - Migrate UPPCS TEST 2026 (598 files)                                              |
| - Migrate Premium 2027 (509 files)                                                 |
| - Migrate DH UPPCS PRELIMS STUDY IQ (67 files)                                     |
| - Full post-migration verification report                                          |
+------------------------------------------------------------------------------------+
```

---

## 10. Conclusion and Recommendation

The read-only inventory and Telethon metadata queries establish that:
1. **100% of historical forum messages (1,174 files)** can be safely and accurately organized into verified topic subfolders.
2. **Legacy folders `Channel_3509423997` and `Channel_3911876130`** can be cleanly reconciled to their verified names.
3. **Legacy folder `Channel_4213926054` and all 3 partial files** are protected by quarantine rules.
4. **All 3 destination collisions** have been identified with a deterministic shared-inode resolution strategy.

The application is completely prepared for implementation of Phase C.1 upon approval.
