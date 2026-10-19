@echo off
REM ============================================================
REM  Run all tests for OBS Meeting Guard
REM ============================================================
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+ and add it to PATH.
    pause
    exit /b 1
)

echo === 1/6 Screen static detection ===
python tests\test_screen.py
echo.
echo === 2/6 Desktop audio capture ===
python tests\test_audio.py
echo.
echo === 3/6 OBS WebSocket protocol ===
python tests\test_obs_ws.py
echo.
echo === 4/6 Guard end-to-end ===
python tests\test_guard_e2e.py
echo.
echo === 5/6 GUI unit test ===
python tests\test_gui.py
echo.
echo === 6/6 GUI exe end-to-end ===
python tests\e2e_gui_exe.py
echo.
echo All tests finished.
pause
