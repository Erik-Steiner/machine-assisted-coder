#!/bin/bash
# macOS one-click setup, mirroring install.bat's steps and reasoning exactly --
# see install.bat's comments for the full rationale behind each step.
cd "$(dirname "$0")"

echo ""
echo "Machine Assisted Coder -- setup"
echo "==============================="
echo ""

# The Python environment lives outside this folder on purpose, in a per-machine
# location under ~/Library/Application Support -- NOT inside the project folder.
# A venv is thousands of small files that change on every install, and if this
# project folder is synced (Dropbox, OneDrive, Google Drive...), the sync client's
# own file activity can fight pip's install/uninstall steps -- see install.bat for
# the confirmed Windows failure this same reasoning is based on. Keeping the venv
# outside any synced folder avoids the whole problem here too.
VENV_DIR="$HOME/Library/Application Support/InterviewViewer/venv"

echo "[1/4] Looking for Python 3..."
PY_CMD=""

if command -v python3 >/dev/null 2>&1; then
    PY_CMD="python3"
elif command -v python >/dev/null 2>&1 && python --version 2>&1 | grep -q "Python 3"; then
    PY_CMD="python"
fi

if [ -z "$PY_CMD" ]; then
    echo ""
    echo "Could not find Python 3 on this machine."
    echo "Install it from https://www.python.org/downloads/ (any 3.9 or newer),"
    echo "then run this file again."
    echo ""
    read -p "Press Enter to close this window..."
    exit 1
fi

echo "      found: $PY_CMD ($("$PY_CMD" --version 2>&1))"

echo "[2/4] Creating a private Python environment..."
echo "      ($VENV_DIR)"
if [ -x "$VENV_DIR/bin/python3" ]; then
    echo "      already exists, skipping."
else
    mkdir -p "$HOME/Library/Application Support/InterviewViewer"
    if ! "$PY_CMD" -m venv "$VENV_DIR"; then
        echo ""
        echo "Couldn't create the Python environment. See the error above."
        echo ""
        read -p "Press Enter to close this window..."
        exit 1
    fi
fi

echo "[3/4] Installing required packages..."
# Deliberately NOT running "pip install --upgrade pip" here -- matches install.bat;
# no need to replace pip's own files on a venv this fresh.
if ! "$VENV_DIR/bin/python3" -m pip install --quiet -r requirements.txt; then
    echo ""
    echo "Package install failed. See the error above."
    echo ""
    read -p "Press Enter to close this window..."
    exit 1
fi

echo "[4/4] Setting up your .env file..."
if [ -f ".env" ]; then
    echo "      .env already exists, leaving it alone."
else
    cp ".env.example" ".env"
    echo "      created .env from .env.example -- edit it later if you get"
    echo "      ceointerviews.ai API credentials. Not required to start browsing"
    echo "      or to bring in your own data."
fi

echo ""
echo "Setup complete."
echo "Run start.command any time you want to open the app."
echo ""
read -p "Press Enter to close this window..."
