@echo off
cd /d "%~dp0"

:: Launch headless worker in background
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0worker_headless.py"
) else if exist "%~dp0.venv\Scripts\python.exe" (
    start "" "%~dp0.venv\Scripts\python.exe" "%~dp0worker_headless.py"
) else (
    echo [ERROR] Virtual environment not found. Please run install_windows.bat first.
    pause
)
exit
