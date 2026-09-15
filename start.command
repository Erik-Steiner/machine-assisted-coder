#!/bin/bash
# macOS one-click launch, mirroring start.bat -- see install.command for setup.
cd "$(dirname "$0")"

# Must match install.command's VENV_DIR -- kept outside this (possibly cloud-synced)
# project folder on purpose. See install.command for why.
VENV_DIR="$HOME/Library/Application Support/InterviewViewer/venv"

if [ ! -x "$VENV_DIR/bin/python3" ]; then
    echo ""
    echo "Setup hasn't run yet on this machine."
    echo "Double-click install.command first, then come back and run this file."
    echo ""
    read -p "Press Enter to close this window..."
    exit 1
fi

echo "Starting Machine Assisted Coder..."
echo "Your browser will open automatically once it's ready."
echo "Closing this window stops the app."
echo ""

"$VENV_DIR/bin/python3" viewer_server.py

echo ""
echo "The app has stopped."
read -p "Press Enter to close this window..."
