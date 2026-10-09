#!/bin/bash
# Copyright (c) 2026 Alex Wolfson
# SPDX-License-Identifier: MIT
set -e

echo "Installing system packages (may ask for sudo)..."
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip ffmpeg libsndfile1

VENV="$HOME/dashboard-env"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip
if [ -f requirements.txt ]; then
  pip install -r requirements.txt
else
  pip install fitparse opencv-python numpy pillow librosa soundfile
fi
echo "Done. Activate with: source ~/dashboard-env/bin/activate"
