@echo off
REM ============================================================
REM  Double-click to start OBS Meeting Guard
REM  Press Ctrl+C in the console window to stop the guard.
REM ============================================================
cd /d "%~dp0"
echo Starting OBS Meeting Guard ... (press Ctrl+C to stop)
obs_meeting_guard.exe
pause
