@echo off
REM ============================================================
REM  Build obs_meeting_guard.exe (GUI) and obs_meeting_guard_console.exe
REM  Requirements: Python 3.8+ and PyInstaller
REM    python -m pip install pyinstaller
REM ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+ and add it to PATH.
    pause
    exit /b 1
)

echo Building GUI version (obs_meeting_guard.exe) ...
python -m PyInstaller --onefile --windowed --name obs_meeting_guard --distpath . --workpath build --specpath build --clean obs_meeting_guard.py
if errorlevel 1 (
    echo [ERROR] GUI build failed. Make sure PyInstaller is installed:
    echo         python -m pip install pyinstaller
    pause
    exit /b 1
)

echo Building console version (obs_meeting_guard_console.exe) ...
python -m PyInstaller --onefile --console --name obs_meeting_guard_console --distpath . --workpath build --specpath build --clean obs_meeting_guard.py
if errorlevel 1 (
    echo [ERROR] Console build failed.
    pause
    exit /b 1
)

echo.
echo [OK] Build finished:
echo      obs_meeting_guard.exe          GUI version, double-click to run
echo      obs_meeting_guard_console.exe  console version, same as --console
echo      config.ini and guard.log stay next to the exe.
pause
