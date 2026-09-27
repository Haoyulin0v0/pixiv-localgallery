@echo off
cd /d "%~dp0"
if not defined GALLERY_OPEN_BROWSER set GALLERY_OPEN_BROWSER=1
py -3 --version >nul 2>nul
if not errorlevel 1 (
    py -3 server.py
) else (
    python --version >nul 2>nul
    if errorlevel 1 (
        echo Python 3.10 or newer is required. Install Python and add it to PATH.
        pause
        exit /b 1
    )
    python server.py
)
if errorlevel 1 (
    echo.
    echo Server failed to start. Check the error above. Port 8765 may already be in use.
    pause
)
