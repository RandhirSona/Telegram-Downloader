# 24/7 Automation & Unattended Operation Recommendations

This report evaluates strategies for running the Telegram Downloader reliably in unattended 24/7 background mode on a Windows Virtual Machine under Proxmox.

---

## 1. GUI vs. Headless Architecture Evaluation

### The Problem with Unattended GUI Execution
Tkinter applications require an interactive Windows desktop session (Session 1). In a headless VM environment:
- If no user is logged in, or upon an unauthenticated VM reboot, Tkinter cannot initialize its graphical display context and will crash with `_tkinter.TclError: no display name`.
- If an RDP session disconnects, Windows may throttle or suspend interactive GUI event loops.

### The Decoupled Headless Solution
The application separates the downloader into:
1. **Headless Background Worker (`worker_headless.py`)**: Runs purely as an async Python process without GUI dependencies. Compatible with Windows Task Scheduler, system services, and boot triggers.
2. **Interactive Desktop GUI (`telegram_gui_downloader.py`)**: Connects to the worker in **Monitoring Mode** when opened, reading worker telemetry from `worker_status.json` and `download_tracker.db` without locking the session database.

---

## 2. Windows Task Scheduler Architecture

### Recommended Task Settings
```xml
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveTokenOrPassword</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\TelegramData\.venv\Scripts\pythonw.exe</Command>
      <Arguments>C:\TelegramData\worker_headless.py</Arguments>
      <WorkingDirectory>C:\TelegramData</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

### Automated Setup
Running `create_task_scheduler.bat` on the VM registers this task with highest privileges, launching `worker_headless.py` on logon and keeping it running continuously.

---

## 3. Crash Recovery & Resilience Measures

1. **State Persistence Across Reboots:**
   - Active channel IDs, media filters, and cutoff dates are saved in `config.json`.
   - On boot, `worker_headless.py` reads `config.json` and resumes scanning without requiring any user interaction.
2. **Failed Message Retry Queue:**
   - Messages that fail due to transient network drops or socket timeouts are tagged `status = 'FAILED'` in `download_tracker.db`.
   - On each subsequent channel pass, failed messages are automatically re-queued (up to 5 retries).
3. **FloodWait Rate-Limit Backoff:**
   - Telegram rate limits (`FloodWaitError`) are intercepted. The engine displays the exact sleep countdown and pauses execution until the flood window clears.
4. **Log Rotation:**
   - Headless worker output is logged to `logs\worker.log` using `RotatingFileHandler` (10 MB per file, 5 backups preserved), preventing unconstrained disk usage on `C:\`.
5. **Session Lock Guard:**
   - A single-instance lock (`downloader.lock`) ensures only one process interacts with the Telethon SQLite session file (`anon.session`), preventing database locks or credential corruption.
