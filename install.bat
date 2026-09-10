@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo Machine Assisted Coder -- setup
echo ===============================
echo.

REM The Python environment lives outside this folder on purpose, in a per-machine
REM location under %LOCALAPPDATA% -- NOT inside the project folder. A venv is
REM thousands of small files that change on every install, and if this project
REM folder is synced (Dropbox, OneDrive, Google Drive...), the sync client's own
REM file locks fight pip's install/uninstall steps -- confirmed on this machine:
REM a "pip install" here failed outright, and the broken venv afterward took
REM several minutes and multiple attempts to even delete. Keeping the venv outside
REM any synced folder avoids the whole problem.
set "VENV_DIR=%LOCALAPPDATA%\InterviewViewer\venv"

echo [1/4] Looking for Python 3...
set "PY_CMD="

REM Prefer the "py" launcher -- unlike a bare "python", it is not shadowed by the
REM Windows Store's python.exe alias stub, which can silently "succeed" on a
REM machine with no real Python installed at all.
py -3 --version >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3"

if not defined PY_CMD (
    python --version >nul 2>&1
    if not errorlevel 1 (
        for /f "tokens=2" %%V in ('python --version 2^>^&1') do set "PYVER=%%V"
        echo !PYVER! | findstr /b "3." >nul
        if not errorlevel 1 set "PY_CMD=python"
    )
)

if not defined PY_CMD (
    echo.
    echo Could not find Python 3 on this machine.
    echo Install it from https://www.python.org/downloads/ ^(any 3.9 or newer^),
    echo making sure "Add python.exe to PATH" is checked during install,
    echo then run this file again.
    echo.
    pause
    exit /b 1
)

echo       found: !PY_CMD!

echo [2/4] Creating a private Python environment...
echo       (%VENV_DIR%)
if exist "%VENV_DIR%\Scripts\python.exe" (
    echo       already exists, skipping.
) else (
    if not exist "%LOCALAPPDATA%\InterviewViewer" mkdir "%LOCALAPPDATA%\InterviewViewer"
    %PY_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo.
        echo Couldn't create the Python environment. See the error above.
        echo.
        pause
        exit /b 1
    )
)

echo [3/4] Installing required packages...
REM Deliberately NOT running "pip install --upgrade pip" here -- pip replacing its
REM own files while it's the process doing the replacing is unreliable on Windows.
REM The venv's bundled pip is new enough for this file.
"%VENV_DIR%\Scripts\python.exe" -m pip install --quiet -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo Package install failed. See the error above.
    echo.
    pause
    exit /b 1
)

echo [4/4] Setting up your .env file...
if exist "%~dp0.env" (
    echo       .env already exists, leaving it alone.
) else (
    copy /y "%~dp0.env.example" "%~dp0.env" >nul
    echo       created .env from .env.example -- edit it later if you get
    echo       ceointerviews.ai API credentials. Not required to start browsing
    echo       or to bring in your own data.
)

echo.
echo Setup complete.
echo Run start.bat any time you want to open the app.
echo.
pause
