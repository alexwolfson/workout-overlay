# Workout Overlay
**Version 1.0**
v1.0 — DJI + Zepp FIT on Linux

Overlay Amazfit / Zepp FIT telemetry onto action-camera video and stills.  
Built for real outings: one workout FIT, many Osmo (or phone) clips, photos taken between or **during** clips, optional hyperlapse, batch export with transitions.

## Features

- **Single clip** or **batch folder** (one `.fit` + many videos/photos)
- Telemetry panel: time, speed, heart rate, elevation, distance + GPS minimap
- **Auto-align** video to FIT via MP4 `creation_time` (default on)
- **Hyperlapse**: silent sections use `--factor` (default 15×); audio sections stay 1×
- **Photos** in batch: EXIF time → FIT (local EXIF converted with display TZ offset)
- **Mid-clip photos**: stills taken while a video was recording are inserted by splitting that clip
- **Transitions**: `--transition-seconds N` fades out to black then in (extra time; content not shortened)
- **Preview** mode: 1080p for faster test renders
- Time display: `--time auto|local|utc` and optional `--utc-offset`

## Requirements

- Linux (tested on Pop!_OS)
- Python 3.10+
- `ffmpeg` / `ffprobe`
- Virtualenv packages: `fitparse`, `opencv-python`, `numpy`, `pillow`, `librosa`, `soundfile`

## Setup

```bash
chmod +x install_overlay.sh run_overlay.sh
./install_overlay.sh
```

Creates `~/dashboard-env` and installs dependencies.

## Usage

### Single clip

```bash
./run_overlay.sh video.mp4 activity.fit --preview
./run_overlay.sh video.mp4 activity.fit -o final.mp4
./run_overlay.sh video.mp4 activity.fit --factor 10 --preview
./run_overlay.sh video.mp4 activity.fit -3 --preview          # manual align offset (seconds)
```

### Batch (recommended for real workouts)

Put **exactly one** `.fit` and any mix of videos/photos in a folder:

```text
my_walk/
  Zepp20261007....fit
  DJI_....MP4
  DJI_....JPG
  PXL_....jpg
```

```bash
./run_overlay.sh --batch ./my_walk --preview \
  --photo-seconds 3 \
  --transition-seconds 2 \
  -o walk_batch.mp4
```

| Option | Meaning | Default |
|--------|---------|---------|
| `--batch DIR` | Process folder | — |
| `--preview` | Render at 1080p | off (full resolution) |
| `--photo-seconds N` | How long each still is shown | `3` |
| `--transition-seconds N` | Fade through black between items (extra time) | `0` (hard cut) |
| `--factor N` | Hyperlapse speed for **silent** sections | `15` |
| `--time auto\|local\|utc` | Clock display mode | `auto` |
| `--utc-offset H` | Force UTC offset hours (e.g. `-4`) | from auto/DJI |
| `--no-auto-align` | Disable video start ↔ FIT start align | auto on |
| `-o FILE` | Output path | `workout_overlay.mp4` / `workout_batch.mp4` |

Direct Python (with venv active):

```bash
source ~/dashboard-env/bin/activate
python workout_overlay.py --batch ./my_walk --preview --transition-seconds 2 -o out.mp4
```

## How alignment works

| Media | Method |
|-------|--------|
| **Video** | MP4 `creation_time` (UTC) vs FIT start → offset (auto). Hyperlapse map from audio. |
| **Photo** | EXIF capture time treated as **local**; converted to UTC with the same offset as `--time auto` / `--utc-offset`, then placed on the FIT timeline. |
| **Mid-clip photo** | If photo time falls inside a video’s FIT window, that video is split and the still is inserted at that moment. |

Original DJI files keep reliable timestamps. Edited/merged exports often lose `creation_time` — use a manual offset if needed.

## Hyperlapse note

Detection assumes **silent ≈ sped-up** and **audio ≈ 1×**.  
Quiet 1× audio (e.g. mic noise cancellation) can be mistaken for hyperlapse. For pure 1× walks:

```bash
--factor 1
```

## Project layout

```text
workout_overlay.py   # main tool
run_overlay.sh       # activates venv, forwards args
install_overlay.sh   # one-time setup
README.md
.gitignore
```

## Version 1.0 scope

Included:

- Single + batch pipelines  
- Auto-align, hyperlapse factor, photo EXIF align  
- Mid-clip photo insertion  
- Fade-through-black transitions  
- Preview / full quality  

Not in 1.0 (possible later):

- Real map tiles under the minimap  
- FIT GPS → timezone for travel (e.g. Eilat processed at home)  
- Auto-detect hyperlapse factor from FIT duration  
- Multiple FIT files per folder  
- Cadence / temperature rows when present  

## License
MIT
