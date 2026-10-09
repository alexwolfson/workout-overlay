#!/bin/bash
# Copyright (c) 2026 Alex Wolfson
# SPDX-License-Identifier: MIT
set -e

if [ -f "$HOME/dashboard-env/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$HOME/dashboard-env/bin/activate"
else
  echo "Error: virtualenv not found at ~/dashboard-env"
  echo "Run ./install_overlay.sh first."
  exit 1
fi

if [ ! -f "workout_overlay.py" ]; then
  echo "Error: workout_overlay.py not found in current directory"
  exit 1
fi

if [ $# -lt 1 ]; then
  echo "Usage:"
  echo "  ./run_overlay.sh video.mp4 activity.fit [offset] [options]"
  echo "  ./run_overlay.sh --batch ./folder [options]"
  echo ""
  echo "Options:"
  echo "  --preview              Render at 1080p (faster)"
  echo "  -o FILE                Output MP4 name"
  echo "  --factor N             Hyperlapse factor for silent sections (default: 15)"
  echo "  --photo-seconds N      Seconds per photo in batch (default: 3)"
  echo "  --transition-seconds N Fade through black (extra time, default: 0)"
  echo "  --time auto|local|utc"
  echo "  --utc-offset HOURS"
  echo "  --no-auto-align"
  exit 1
fi

python workout_overlay.py "$@"
