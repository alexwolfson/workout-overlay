#!/bin/bash
# Copyright (c) 2026 Alex Wolfson
# SPDX-License-Identifier: MIT
set -e
if [ -f "$HOME/dashboard-env/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$HOME/dashboard-env/bin/activate"
else
  echo "Error: virtualenv not found at ~/dashboard-env"
  echo "Run ./install_overlay.sh first, then: pip install customtkinter"
  exit 1
fi
if [ ! -f "debug_gui.py" ]; then
  echo "Error: debug_gui.py not found in current directory"
  exit 1
fi
python debug_gui.py "$@"
