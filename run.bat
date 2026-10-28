@echo off
rem Double-click to open Anchor Review in your browser and upload demos there.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during install, then run this again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up Python environment...
    python -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)

rem Opens the viewer in your browser; keep this window open while you use it.
".venv\Scripts\python.exe" anchor-stats.py %*
pause
exit /b 0

:fail
echo Setup failed - see the error above.
pause
exit /b 1
