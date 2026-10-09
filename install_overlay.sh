#!/bin/bash
set -e

echo "=== Installing system dependencies ==="
sudo apt update
sudo apt install -y \
  python3-pip \
  python3-venv \
  python3-dev \
  portaudio19-dev \
  libsndfile1 \
  ffmpeg

echo "=== Creating virtual environment ==="
python3 -m venv "$HOME/dashboard-env"

echo "=== Activating virtual environment ==="
# shellcheck disable=SC1091
source "$HOME/dashboard-env/bin/activate"

echo "=== Upgrading pip ==="
pip install --upgrade pip

echo "=== Installing Python packages ==="
pip install fitparse opencv-python numpy pillow librosa soundfile

echo ""
echo "=============================================="
echo " Installation complete"
echo "=============================================="
echo ""
echo "Activate later with:"
echo "  source ~/dashboard-env/bin/activate"
echo ""
echo "Then run:"
echo "  ./run_overlay.sh video.mp4 activity.fit --preview"
echo "  ./run_overlay.sh video.mp4 activity.fit"
echo ""