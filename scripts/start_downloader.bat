@echo off
cd /d "%~dp0"

:: Launch GUI using pythonw for clean desktop execution without console window
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0telegram_gui_downloader.py"
) else if exist "%~dp0.venv\Scripts\python.exe" (
    start "" "%~dp0.venv\Scripts\python.exe" "%~dp0telegram_gui_downloader.py"
) else (
    echo [ERROR] Virtual environment not found. Please run install_windows.bat first.
    pause
)
exit
