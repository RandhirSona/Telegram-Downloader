@echo off
cd /d "%~dp0"
title Telegram Downloader - Setup

echo ========================================================
echo   Telegram Downloader - Windows VM Installation
echo ========================================================
echo.

:: 1. Check Python installation
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python is not found in PATH!
    echo Please install Python 3.10+ from python.org and check "Add Python to PATH".
    pause
    exit /b 1
)

:: 2. Create virtual environment if missing
if not exist ".venv" (
    echo [*] Creating virtual environment in .venv...
    python -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

:: 3. Upgrade pip and install dependencies
echo [*] Installing required packages from requirements.txt...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Package installation failed. Check internet connection and wheel build tools.
    pause
    exit /b 1
)

:: 4. Setup directories and template files
if not exist "logs" mkdir logs
if not exist "app_data" mkdir app_data

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [!] Created .env template. Please edit .env with your API_ID and API_HASH.
    )
)

if not exist "config.json" (
    if exist "config.json.example" (
        copy "config.json.example" "config.json" >nul
        echo [*] Created default config.json.
    )
)

echo.
echo ========================================================
echo   Setup Complete!
echo   1. Edit .env with your Telegram credentials.
echo   2. Run start_downloader.bat to launch GUI and select your 2 TB drive.
echo ========================================================
echo.
pause
