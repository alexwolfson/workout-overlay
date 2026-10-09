#!/bin/bash
set -e

if [ -f "$HOME/dashboard-env/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$HOME/dashboard-env/bin/activate"
else
  echo "Error: virtualenv not found at ~/dashboard-env"
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
  echo "  --preview           Render at 1080p (faster)"
  echo "  -o FILE             Output MP4 name"
  echo "  --factor N          Hyperlapse factor for silent sections (default: 15)"
  echo "  --time auto|local|utc"
  echo "  --utc-offset HOURS  Manual display timezone offset"
  echo "  --no-auto-align     Disable auto FIT alignment"
  echo ""
  echo "Examples:"
  echo "  ./run_overlay.sh ride.mp4 ride.fit --preview"
  echo "  ./run_overlay.sh ride.mp4 ride.fit --factor 10 --preview"
  echo "  ./run_overlay.sh --batch ../testride1x/ --preview -o batch.mp4"
  echo "  ./run_overlay.sh --batch ../testride1x/ --factor 15 -o batch.mp4"
  exit 1
fi

python workout_overlay.py "$@"