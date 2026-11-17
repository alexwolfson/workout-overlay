#!/bin/bash
# Copyright (c) 2026 Alex Wolfson
# SPDX-License-Identifier: MIT
set -e

# 1. Check for and install missing system packages via apt-get if available
if command -v apt-get >/dev/null 2>&1; then
  MISSING_PKGS=()
  for pkg in ffmpeg python3-tk python3-venv libsndfile1; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "ok installed"; then
      MISSING_PKGS+=("$pkg")
    fi
  done

  if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "Installing missing system packages: ${MISSING_PKGS[*]} (may ask for sudo)..."
    sudo apt-get update
    sudo apt-get install -y "${MISSING_PKGS[@]}"
  else
    echo "All required system packages (${MISSING_PKGS[*]:-ffmpeg, python3-tk, python3-venv, libsndfile1}) are already installed."
  fi
else
  echo "apt-get not found; skipping system package check. Ensure ffmpeg and python3-tk are installed."
fi

# 2. Create ~/dashboard-env using python3 -m venv --system-site-packages ~/dashboard-env
#    (or update pyvenv.cfg if ~/dashboard-env already exists) so tkinter is accessible
VENV="$HOME/dashboard-env"
if [ ! -d "$VENV" ]; then
  echo "Creating virtual environment at $VENV with --system-site-packages..."
  python3 -m venv --system-site-packages "$VENV"
else
  echo "Virtual environment already exists at $VENV. Ensuring system site packages are enabled..."
  if [ -f "$VENV/pyvenv.cfg" ]; then
    if grep -q "^include-system-site-packages" "$VENV/pyvenv.cfg"; then
      sed -i 's/^include-system-site-packages\s*=.*/include-system-site-packages = true/' "$VENV/pyvenv.cfg"
    else
      echo "include-system-site-packages = true" >> "$VENV/pyvenv.cfg"
    fi
  fi
fi

# 3. Install requirements.txt plus customtkinter
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
  pip install -r "$SCRIPT_DIR/requirements.txt"
elif [ -f requirements.txt ]; then
  pip install -r requirements.txt
else
  pip install fitparse opencv-python numpy pillow librosa soundfile
fi
pip install customtkinter

echo "Done. Activate with: source ~/dashboard-env/bin/activate"
echo "CLI:  ./run_overlay.sh --batch ./folder --preview"
echo "GUI:  ./run_debug_gui.sh"
