@echo off
cd /d "%~dp0"
title Configure Windows Task Scheduler - Telegram Downloader

echo ========================================================
echo   Configuring 24/7 Unattended Background Worker
echo ========================================================
echo.

set APP_DIR=%~dp0
if "%APP_DIR:~-1%"=="\" set APP_DIR=%APP_DIR:~0,-1%

set PYTHON_BIN=%APP_DIR%\.venv\Scripts\pythonw.exe
set SCRIPT_PATH=%APP_DIR%\worker_headless.py

if not exist "%PYTHON_BIN%" (
    echo [ERROR] Virtual environment pythonw.exe not found at: %PYTHON_BIN%
    echo Please run install_windows.bat first.
    pause
    exit /b 1
)

echo [*] Target Executable: %PYTHON_BIN%
echo [*] Script Path:       %SCRIPT_PATH%
echo [*] Working Directory: %APP_DIR%
echo [*] Task Name:         TelegramDownloader24x7
echo.

:: Attempt PowerShell Register-ScheduledTask with explicit WorkingDirectory & Restart Policies
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$action = New-ScheduledTaskAction -Execute '%PYTHON_BIN%' -Argument '\"%SCRIPT_PATH%\"' -WorkingDirectory '%APP_DIR%';" ^
    "$trigger = New-ScheduledTaskTrigger -AtLogOn;" ^
    "$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest;" ^
    "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew;" ^
    "Register-ScheduledTask -TaskName 'TelegramDownloader24x7' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null;" >nul 2>&1

if %ERRORLEVEL% EQU 0 (
    echo [SUCCESS] Task 'TelegramDownloader24x7' registered successfully with WorkingDirectory!
    echo Features configured:
    echo   - Working Directory: %APP_DIR%
    echo   - Duplicate Instance Policy: IgnoreNew (single-instance enforced)
    echo   - Automatic Restart on Failure: 3 retries every 5 minutes
    echo   - Execution Timeout: Unlimited
) else (
    echo [*] PowerShell registration failed. Falling back to schtasks.exe...
    schtasks /Create /TN "TelegramDownloader24x7" /TR "\"%PYTHON_BIN%\" \"%SCRIPT_PATH%\"" /SC ONLOGON /RL HIGHEST /F
    if %ERRORLEVEL% EQU 0 (
        echo [SUCCESS] Task registered via schtasks.
    ) else (
        echo [ERROR] Failed to register task. Please run this script as Administrator.
    )
)

echo.
echo You can run the task on-demand anytime using:
echo   schtasks /Run /TN "TelegramDownloader24x7"
echo Or monitor live logs in:
echo   %APP_DIR%\logs\worker.log
echo.
pause
