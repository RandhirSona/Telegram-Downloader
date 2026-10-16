# Telegram Downloader — Download History Repair Architecture, Read-Only Audit, and Verification Plan
**Document Version:** 1.0.0  
**Date:** 2026-10-14  
**Project Directory:** `C:\TelegramData`  
**Media Storage Root:** `F:\TelegramDownloads` (User-configurable via `ConfigManager.get_download_dir()`)  
**Production Database:** `C:\TelegramData\download_tracker.db`  
**Status:** Read-Only Audit & Isolated Test Verification Complete (Zero Production Mutations)  

---

## Executive Summary

Following manual deletion by the user of previously downloaded channel and forum-topic folders from `F:\TelegramDownloads`, the SQLite database (`download_tracker.db`) retained historical download records for these items, including records marked `COMPLETED`.

Because the downloader engine skips messages whose database status is `COMPLETED`, messages whose physical media files were deleted could not be downloaded again through normal channel scans.

This document presents a **safe, read-only audit and verified repair plan** that:
1. Confirms the exact physical status of all 1,608 `COMPLETED` records across the storage roots.
2. Identifies all 1,277 genuinely missing records without altering or deleting existing completed records that remain on disk.
3. Implements an isolated, verified `HistoryRepairService` that transitions only confirmed missing records from `COMPLETED` to `PENDING`, making them eligible for redownload into their verified channel and forum-topic folders.
4. Generates an SQLite-consistent backup verified with `PRAGMA integrity_check` and `PRAGMA foreign_key_check`.
5. Guarantees that zero production media files or database rows have been modified during this phase.

---

## 1. Initial Safety Checks

Prior to performing any analysis:
- **Active Worker Status:** `None` (verified via `ProcessLock.get_active_worker_status()`). Headless background worker is completely inactive.
- **Active Download Operations:** `0` active transfers.
- **Configured Storage Roots:**
  - Active Root: `F:\TelegramDownloads` (accessible and writable).
  - Registered Historical Roots: `[]`.
- **Database Integrity:**
  - `PRAGMA integrity_check`: `ok` (100% healthy).
  - `PRAGMA foreign_key_check`: `[]` (0 foreign key violations).
- **Before-Repair Database Baseline:**
  - `COMPLETED`: 1,608 records
  - `DOWNLOADING`: 2 records
  - `FAILED`: 1,176 records
  - **Total Records:** 2,786 records
- **Migration Journal Status:** No uncommitted or in-progress migration operations exist.
- **Protected Partial Downloads:** Exactly 1 `.part` file physically exists on disk:
  - `F:\TelegramDownloads\Prashant sir current affairs 2027\Current_Affairs_02_9th_September_2026.mp4.part` (7,864,320 bytes).
  - This file remains 100% untouched and quarantined.

---

## 2. Physical Disk Audit and Deleted Folders

Physical inspection of `F:\TelegramDownloads` revealed that the user manually removed several directories while preserving others:

### 2.1 Currently Present Directories on Disk

| Directory Name | Directory Status | Regular Files | Partial Files | Total Disk Volume |
| :--- | :--- | :--- | :--- | :--- |
| `FORUM PTS 2026` | Present | 54 files | 0 | 8,248.04 MB |
| `PVT UPPCS TEST` | Present | 194 files | 0 | 3,454.67 MB |
| `PVT VAJIRAM POWER UP 2027` | Present | 7 files | 0 | 5,121.80 MB |
| `PVT VISION IAS PTS 2027` | Present (Empty) | 0 files | 0 | 0.00 MB |
| `Prashant sir current affairs 2027` | Present | 76 files | 1 (`.part`) | 9,000.18 MB |
| **Total Present on Disk** | | **331 files** | **1 `.part`** | **25,824.69 MB** |

### 2.2 Confirmed Deleted Top-Level Channel Folders

The following top-level directories were confirmed deleted by the user:
1. `UPPCS TEST 2026` (Channel ID `4412184534`): 598 completed records missing.
2. `Premium 2027` (Channel ID `4450272939`): 513 completed records missing.
3. `DH UPPCS PRELIMS STUDY IQ` (Channel ID `4489360145`): 75 completed records missing.
4. `Channel_3509423997` (`FORUM PTS 2027`, Channel ID `3509423997`): 59 completed records missing.
5. `Channel_3911876130` (`PVT VISION IAS PTS 2027`, Channel ID `3911876130`): 31 completed records missing.

---

## 3. Classification of Database Completed Records

Every one of the 1,608 `COMPLETED` records in `downloads` was evaluated against physical disk storage using the required seven-category classification taxonomy:

```
========================================================================================
Classification Category       Record Count   Percentage        Total Volume
========================================================================================
PRESENT_VERIFIED                       331        20.6%        25,817.19 MB
MISSING_CONFIRMED                    1,271        79.0%        36,046.17 MB
SHARED_PATH_COLLISION                    6         0.4%             5.85 MB
PARTIAL_PRESENT                          0         0.0%             0.00 MB
STORAGE_UNAVAILABLE                      0         0.0%             0.00 MB
PATH_AMBIGUOUS                           0         0.0%             0.00 MB
METADATA_INSUFFICIENT                    0         0.0%             0.00 MB
----------------------------------------------------------------------------------------
TOTAL EXAMINED                       1,608       100.0%        61,869.21 MB (~60.42 GB)
========================================================================================
```

### Analysis of Findings:
- **`PRESENT_VERIFIED` (331 records):** Every file physically present on disk matches its recorded byte size and relative path. These records will remain `COMPLETED` and will not be re-downloaded.
- **`MISSING_CONFIRMED` (1,271 records):** Genuinely missing because their parent folders or files were deleted from disk. Verified eligible for repair.
- **`SHARED_PATH_COLLISION` (6 records):** 3 pairs of records that historically pointed to 3 shared physical files on disk:
  1. `UPPCS TEST 2026\Uppcs one\UPPCS Sectional Test 6 - Science (Questions).pdf` (Messages 30 & 31)
  2. `Premium 2027\IAS BABA MAINS 2026\TLP_2026_09TH AUGUST 2026_SYNOPSIS.pdf` (Messages 1221 & 1222)
  3. `Premium 2027\IAS BABA MAINS 2026\MEP_2026_05THAUGUST2026_GS_02_ANSWERBOOKLET.pdf` (Messages 1230 & 1231)
  Because their parent folders were deleted, these 3 physical files (6 records) are confirmed missing and are included in the repair candidates.
- **Total Repair Candidates:** Exactly **1,277 records** (1,271 + 6).
- **Total Bytes Requiring Redownload:** **37,803,289,892 bytes (~35.21 GB)**.

---

## 4. Breakdown of Missing Records by Channel and Topic

```
+------------------------------------------------------------------------------------------------------+
| Channel ID | Verified Telegram Title           | Missing Files | Redownload Volume | Primary Topics Affected         |
+------------+-----------------------------------+---------------+-------------------+---------------------------------+
| 3509423997 | FORUM PTS 2027                    |      59 files |      11,023.50 MB | Broadcast channel (Non-forum)   |
| 3683278928 | PVT UPPCS TEST                    |       1 files |          1.40 MB | Historical missing (Msg 226)    |
| 3911876130 | PVT VISION IAS PTS 2027           |      31 files |         40.91 MB | Broadcast channel (Non-forum)   |
| 4412184534 | UPPCS TEST 2026                   |     598 files |       2,224.15 MB | Make ias (239), Drishti ias (68)|
| 4450272939 | Premium 2027                      |     513 files |       9,709.83 MB | DAILY NEWSPAPER (179), Dipin CA |
| 4489360145 | DH UPPCS PRELIMS STUDY IQ         |      75 files |      13,052.24 MB | Modern history (66), Ancient (7)|
+------------+-----------------------------------+---------------+-------------------+---------------------------------+
| TOTAL      |                                   |   1,277 files |      36,052.03 MB | (37,803,289,892 bytes)          |
+------------------------------------------------------------------------------------------------------+
```

### Detailed Topic Breakdown for Forum Supergroups:

1. **`UPPCS TEST 2026` (`4412184534`) — 598 Files:**
   - Topic `Make ias`: 239 files
   - Topic `Drishti ias`: 68 files
   - Topic `Abhyas 9.0 make ias`: 51 files
   - Topic `Uppcs one`: 49 files (includes collision pair Msg 30/31)
   - Topic `Dhyey ias`: 43 files
   - Topic `Physics wallah`: 39 files
   - Topic `Study iq`: 38 files
   - Topic `Sanskriti ias`: 30 files
   - Topic `NEXT IAS`: 29 files
   - Topic `Gs world`: 12 files

2. **`Premium 2027` (`4450272939`) — 513 Files:**
   - Topic `DAILY NEWSPAPER`: 179 files (4 were already subfolder-tagged, 175 flat)
   - Topic `Dipin Sir CA 2027 Handouts`: 74 files
   - Topic `UNDERSTAND UPSC`: 45 files
   - Topic `SARRTHI MAINS TESTS`: 32 files
   - Topic `IAS BABA MAINS 2026`: 31 files (includes collision pairs Msg 1221/1222, 1230/1231)
   - Topic `PSIR TESTS & MATERIAL`: 30 files
   - Topic `Magazines`: 16 files
   - Topic `CIVILDAILY MATERIAL`: 15 files
   - Other Topics (12 topics): 91 files

3. **`DH UPPCS PRELIMS STUDY IQ` (`4489360145`) — 75 Files:**
   - Topic `Modern history`: 66 files
   - Topic `ANCIENT MEDIEVAL`: 8 files (7 completed files missing + 1 historical orientation file)
   - Topic `Economy`: 1 file

---

## 5. Records Intentionally Excluded from Repair

To ensure strict safety and duplicate prevention, the following records are **explicitly excluded** from modification:

1. **All 331 `PRESENT_VERIFIED` Completed Records:**
   - `PVT UPPCS TEST`: 194 files present.
   - `Prashant sir current affairs 2027`: 76 files present.
   - `FORUM PTS 2026`: 54 files present.
   - `PVT VAJIRAM POWER UP 2027`: 7 files present.
   - *Guarantee:* These files are already on disk and will not be re-downloaded.

2. **All 2 `DOWNLOADING` Records:**
   - `Channel_4213926054` (Message 26, `Current_Affairs_02_9th_September_2026.mp4`, 877,752,966 bytes)
   - `DH UPPCS PRELIMS STUDY IQ` (Message 17, `L8 - Vedic Age (Part - 02).mp4`, 1,132,471,303 bytes)
   - *Guarantee:* Not modified; retained for separate partial resume handling.

3. **All 1,176 `FAILED` Records:**
   - These records represent historical network timeouts or rate limits and already have status `FAILED`.
   - *Guarantee:* Left untouched by this repair operation.

4. **Quarantined Physical Partial Files:**
   - `Prashant sir current affairs 2027\Current_Affairs_02_9th_September_2026.mp4.part` (7,864,320 bytes).
   - *Guarantee:* Untouched, not truncated or deleted.

---

## 6. Verified SQLite Database Backup

Prior to preparing the repair specification, a verified hot backup of `download_tracker.db` was created using SQLite's backup API:

- **Backup File Path:** `C:\TelegramData\backups\download_tracker_repair_backup_20261014_161308.db`
- **File Size:** 1,110,016 bytes (1.06 MB)
- **SHA-256 Checksum:** `7d730fc4b013a48e7988fcb61a127c9fcded34598a9f288885a0126fbfa1d03f`
- **Integrity Check:** `PRAGMA integrity_check` -> `ok`
- **Foreign Key Check:** `PRAGMA foreign_key_check` -> `[]` (0 errors)
- **Total Preserved Records:** 2,786 rows in `downloads`, all channel mappings, selections, and topics intact.

---

## 7. Database Repair Architecture and Implementation

### 7.1 Reusable Service Design (`history_repair_service.py`)
A dedicated, standalone service `HistoryRepairService` has been implemented in `c:\TelegramData\history_repair_service.py`:

```python
class HistoryRepairService:
    def __init__(self, db_path: str = "download_tracker.db", config_mgr: Optional[ConfigManager] = None): ...
    def check_safety_guards(self) -> Tuple[bool, str]: ...
    def audit_records(self, active_root: Optional[Path] = None, historical_roots: Optional[List[Path]] = None) -> Dict[str, Any]: ...
    def create_backup(self, backup_dir: Optional[Path] = None) -> Tuple[Path, str]: ...
    def generate_repair_manifest(self, audit_data: Dict[str, Any]) -> List[Dict[str, Any]]: ...
    def apply_repair(self, repair_manifest: List[Dict[str, Any]], confirmed: bool = False) -> Dict[str, Any]: ...
```

### 7.2 Safe State Transition
For each confirmed missing file:
```sql
UPDATE downloads
SET status = 'PENDING',
    error_message = 'FILE_DELETED_FROM_DISK: Missing from storage root; queued for redownload'
WHERE id = :download_id
  AND channel_id = :channel_id
  AND message_id = :message_id
  AND status = 'COMPLETED';
```

### 7.3 Compatibility with Downloader Logic:
1. **Schema Check Constraint Compliant:**
   `CHECK(status IN ('PENDING', 'DOWNLOADING', 'COMPLETED', 'FAILED', 'SKIPPED'))` — `'PENDING'` is a valid native state without requiring schema alterations.
2. **Duplicate Prevention Preserved:**
   `db_tracker.is_message_downloaded(channel_id, message_id)` returns `True` only when `status == 'COMPLETED'`. When transitioned to `'PENDING'`, `is_message_downloaded` returns `False`, allowing normal channel scans to select the message for download.
3. **No Duplicate Rows:**
   When redownloading begins, `db_tracker.record_start()` executes `INSERT ... ON CONFLICT(channel_id, message_id) DO UPDATE SET status = 'DOWNLOADING'`, safely updating the existing row in-place without creating duplicate database entries.
4. **Clean Statistics Separation:**
   Records marked `'PENDING'` are not counted as failed transfers (`status == 'FAILED'`), keeping genuine network failures separate from deleted media redownloads.
5. **Topic Subfolder Restoration:**
   When redownloaded, the engine routes each file using `channel_storage_mapping` and `forum_topics` to place files into verified topic subfolders (e.g., `UPPCS TEST 2026\Make ias\...`), creating topic directories only on demand.

---

## 8. Automated Test Results

A comprehensive isolated test suite `tests/test_history_repair_service.py` was created and verified against temporary databases and mock filesystems:

```
----------------------------------------------------------------------
Ran 16 tests in 1.618s

OK (16/16 tests passing)
```

### Verified Test Scenarios:
1. `test_01_entire_channel_directory_deleted`: Entire channel folder deleted -> all files marked `MISSING_CONFIRMED`, repaired to `PENDING`.
2. `test_02_single_forum_topic_directory_deleted`: Single topic folder deleted -> only deleted topic files repaired; remaining topic files stay `PRESENT_VERIFIED`.
3. `test_03_single_completed_file_deleted`: Single file deleted from existing folder -> only deleted file repaired.
4. `test_04_completed_file_present_under_historical_root`: File present under historical root -> marked `PRESENT_VERIFIED`, excluded from repair.
5. `test_05_configured_drive_disconnected`: Unreachable drive -> marked `STORAGE_UNAVAILABLE`, zero records repaired.
6. `test_06_permission_denied_folder`: Inaccessible folder -> marked `STORAGE_UNAVAILABLE`, zero records repaired.
7. `test_07_two_database_records_sharing_one_physical_file`: Shared physical file missing -> both records marked `SHARED_PATH_COLLISION` and repaired together.
8. `test_08_missing_completed_media_with_verified_topic_id`: Verified topic ID and title preserved across repair.
9. `test_09_missing_media_with_ambiguous_or_missing_metadata`: Empty relative path -> marked `METADATA_INSUFFICIENT`, excluded from automatic repair.
10. `test_10_existing_part_file_protection`: Existing `.part` file on disk -> marked `PARTIAL_PRESENT`, strictly excluded from repair.
11. `test_11_repeated_repair_execution_without_duplicate_changes`: Idempotency confirmed (0 duplicate updates on second run).
12. `test_12_retry_followed_by_successful_database_completion`: Full redownload lifecycle (`PENDING` -> `DOWNLOADING` -> `COMPLETED`).
13. `test_13_previously_completed_files_not_being_downloaded_again`: Physically present files skipped by engine.
14. `test_14_user_changes_download_root`: Moving download root retains `PRESENT_VERIFIED` status for historical root files.
15. `test_15_backup_creation_and_integrity`: Backup API, SHA-256 computation, and integrity verification.
16. `test_16_active_worker_safety_guard`: Live worker process detection halts unsafe execution.

### Full Regression Suite:
In addition, the complete application regression suite was executed:
- **Total Tests Run:** 220 tests
- **Result:** **220 PASSED, 0 FAILED** (Ran in 51.689s). Zero regressions introduced.

---

## 9. Production Repair Manifest Summary

The full 1,277-item repair manifest is saved outside the repository:  
`C:\Users\Sumir\.gemini\antigravity-ide\brain\7c9c34ba-a764-4c04-91c1-9adbd8856401\scratch\production_repair_manifest.json`

### Representative Manifest Samples:

| Download ID | Channel ID | Channel Title | Message ID | Filename | Topic Title | Proposed Change |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `145` | `3509423997` | FORUM PTS 2027 | 1 | `01_PTS_2026_Sim_Test_01_GS_QP.pdf` | Non-forum | `COMPLETED` -> `PENDING` |
| `177` | `3683278928` | PVT UPPCS TEST | 226 | `UPPCS_ITS_2026_Micro_Test_50...pdf` | Non-forum | `COMPLETED` -> `PENDING` |
| `149` | `3911876130` | PVT VISION IAS PTS 2027 | 1 | `1_PTS_2026_GS_FLT_1_Eng.pdf` | Non-forum | `COMPLETED` -> `PENDING` |
| `1704` | `4412184534` | UPPCS TEST 2026 | 30 | `UPPCS Sectional Test 6 - Science...pdf` | Uppcs one | `COMPLETED` -> `PENDING` |
| `1702` | `4412184534` | UPPCS TEST 2026 | 31 | `UPPCS Sectional Test 6 - Science...pdf` | Uppcs one | `COMPLETED` -> `PENDING` |
| `2834` | `4450272939` | Premium 2027 | 1221 | `TLP_2026_09TH AUGUST 2026_SYNOPSIS.pdf` | IAS BABA MAINS | `COMPLETED` -> `PENDING` |
| `2832` | `4450272939` | Premium 2027 | 1222 | `TLP_2026_09TH AUGUST 2026_SYNOPSIS.pdf` | IAS BABA MAINS | `COMPLETED` -> `PENDING` |
| `2295` | `4489360145` | DH UPPCS STUDY IQ | 16 | `L7 - Vedic Age (Part - 01).mp4` | Modern history | `COMPLETED` -> `PENDING` |

---

## 10. Approval Request and Next Steps

```
+-----------------------------------------------------------------------------------------+
| APPROVAL GATE: Apply Database Repair to Production SQLite Database                      |
+-----------------------------------------------------------------------------------------+
| Scope:                                                                                  |
| - Transition exactly 1,277 verified missing records from 'COMPLETED' to 'PENDING'.      |
| - Keep all 331 physically present records as 'COMPLETED' (no redownload).               |
| - Keep all 2 'DOWNLOADING' records intact (preserve partials).                          |
| - Keep all 1,176 'FAILED' records untouched.                                            |
| - Verified backup exists: download_tracker_repair_backup_20261014_161308.db            |
+-----------------------------------------------------------------------------------------+
```

### Execution Boundary:
In strict compliance with instructions:
- **No production database rows were modified during this turn.**
- **No media files were moved, downloaded, or deleted.**
- **No changes pushed to GitHub.**

The implementation is verified and ready. I await your explicit approval to apply the repair transition to the production SQLite database.
