@echo off
cd /d "%~dp0"

REM Must match install.bat's VENV_DIR -- kept outside this (possibly cloud-synced)
REM project folder on purpose. See install.bat for why.
set "VENV_DIR=%LOCALAPPDATA%\InterviewViewer\venv"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo.
    echo Setup hasn't run yet on this machine.
    echo Double-click install.bat first, then come back and run this file.
    echo.
    pause
    exit /b 1
)

echo Starting Machine Assisted Coder...
echo Your browser will open automatically once it's ready.
echo Closing this window stops the app.
echo.

call "%VENV_DIR%\Scripts\activate.bat"
"%VENV_DIR%\Scripts\python.exe" "%~dp0viewer_server.py"

echo.
echo The app has stopped.
pause
