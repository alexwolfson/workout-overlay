# Workout Overlay

**Version 1.1** — DJI Osmo + Zepp/Amazfit FIT on Linux

Overlay Amazfit / Zepp FIT telemetry onto action-camera video and stills.  
Built for real outings: one workout FIT, many Osmo (or phone) clips, photos taken between or **during** clips, optional hyperlapse, batch export with transitions, outdoor and **dive** panels.

<img width="1931" height="1194" alt="Screenshot_2026-10-09_16-58-32" src="https://github.com/user-attachments/assets/2f17610c-0349-44d3-9c2a-2d8c9ad54cb3" />


## Features

- **Single clip** or **batch folder** (one `.fit` + many videos/photos)
- **Outdoor panel**: time, speed, heart rate, elevation, distance + GPS minimap
- **Dive panel** (auto when FIT has depth samples): time, depth, max depth, heart rate, temperature; plus NDL, TTS, ascent rate, CNS when present in the FIT
- **Auto-align** video to FIT via MP4 `creation_time` (default on)
- **Hyperlapse**: silent sections use `--factor` (default 15×); audio sections stay 1×
- **Photos** in batch: EXIF time → FIT (local EXIF converted with display TZ offset)
- **Mid-clip photos**: stills taken while a video was recording are inserted by splitting that clip
- **Transitions**: `--transition-seconds N` fades out to black then in (extra time; content not shortened)
- **Preview** mode: 1080p for faster test renders
- Time display: `--time auto|local|utc` and optional `--utc-offset`
- **`--sport`**: `auto` | `outdoor` | `dive` | `freedive` | `scuba`

## Requirements

- Linux (tested on Pop!_OS)
- Python 3.10+
- `ffmpeg` / `ffprobe` on `PATH`
- Python packages listed in `requirements.txt`

## Setup

```bash
chmod +x install_overlay.sh run_overlay.sh
./install_overlay.sh
```

Creates `~/dashboard-env` and installs dependencies.

Manual alternative:

```bash
python3 -m venv ~/dashboard-env
source ~/dashboard-env/bin/activate
pip install -r requirements.txt
```

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

### Dive / underwater

FIT files with depth samples switch to the dive panel automatically. Underwater is usually pure 1× video:

```bash
./run_overlay.sh --batch ./my_dive --factor 1 --preview -o dive_out.mp4
./run_overlay.sh dive.mp4 dive.fit --sport dive --factor 1 -o dive_out.mp4
```

| Option | Meaning | Default |
|--------|---------|---------|
| `--batch DIR` | Process folder | — |
| `--preview` | Render at 1080p | off (full resolution) |
| `--photo-seconds N` | How long each still is shown | `3` |
| `--transition-seconds N` | Fade through black between clips (extra time) | `0` (hard cut) |
| `--factor N` | Speed assumed for **silent** sections | `15` |
| `--sport MODE` | Panel fields: `auto`, `outdoor`, `dive`, `freedive`, `scuba` | `auto` |
| `--time auto\|local\|utc` | How TIME is displayed | `auto` |
| `--utc-offset HOURS` | Force display timezone offset | from video when possible |
| `--no-auto-align` | Do not use MP4 creation_time vs FIT start | auto-align on |
| `-o FILE` | Output path | `workout_overlay.mp4` / `workout_batch.mp4` |

`freedive` and `scuba` both use the dive panel; extra scuba metrics (NDL, TTS, …) appear only if the FIT contains them.

## Alignment model

| Media | How it is placed on the FIT timeline |
|-------|--------------------------------------|
| **Video** | MP4 `creation_time` (UTC) vs FIT start → offset (auto). Hyperlapse map from audio. |
| **Photo** | EXIF capture time treated as **local**; converted to UTC with the same offset as `--time auto` / `--utc-offset`, then placed on the FIT timeline. |
| **Mid-clip photo** | If photo time falls inside a video’s FIT window, that video is split and the still is inserted at that moment. |

Original DJI files keep reliable timestamps. Edited/merged exports often lose `creation_time` — use a manual offset if needed.

## Hyperlapse note

Detection assumes **silent ≈ sped-up** and **audio ≈ 1×**.  
Quiet 1× audio (e.g. mic noise cancellation) can be mistaken for hyperlapse. For pure 1× walks or underwater:

```bash
--factor 1
```

For intentional hyperlapse, leave the default (`15`) and prefer **noise cancellation off** on the camera so silence tracking stays honest.

## Dive fields

When the panel is in dive mode, rows are drawn only if the value exists in the FIT:

| Label | FIT source |
|-------|------------|
| DEPTH | `depth` |
| MAX DEPTH | running max of `depth` |
| HEART RATE | `heart_rate` |
| TEMP | `temperature` |
| NDL | `ndl_time` / `ndl` (when present) |
| TTS | `time_to_surface` (when present) |
| ASCENT | `ascent_rate` (when present) |
| CNS | `cns_load` (when present) |

Amazfit freedive exports typically include depth, temperature, and heart rate. Scuba-specific fields depend on the watch model and session.

## Debug GUI

Optional CustomTkinter tool for alignment / hyperlapse debugging:

```bash
pip install customtkinter   # once, in ~/dashboard-env
./run_debug_gui.sh
./run_debug_gui.sh /path/to/batch_folder
```

- Pick a batch folder (one `.fit` + videos/photos)
- Preview clip with scrub; see **file time**, **detected factor (1×/15×)**, **mapped FIT time**, sample telemetry
- Segment list, auto-align note, render plan, copy debug snapshot

## Project layout

```text
workout_overlay.py   # main tool
run_overlay.sh       # activates venv, forwards args
install_overlay.sh   # one-time setup
requirements.txt
LICENSE              # MIT
README.md
.gitignore
```

## Scope

**Supported today**

- Linux CLI
- DJI Osmo-style MP4/JPG + phone stills
- Zepp / Amazfit (and typical FIT) activity files
- Single + batch pipelines, auto-align, mid-clip photos, fade transitions
- Outdoor and dive telemetry panels (`--sport` / auto-detect from depth)

**Not yet**

- Official Windows/macOS support (may work; not tested)
- Full camera-specific profiles beyond current DJI/GoPro heuristics
- GUI
- Map tiles under the minimap
- Perimeter / Mimo-style dive layout (depth graph)

## Roadmap

Ordered for stability first (CLI before GUI):

1. **Device profiles** — thin adapters for video start time and activity load.
2. **Sport defaults** — walk, run, bike, freedive, scuba field lists and sensible `--factor` defaults.
3. **Optional segment map** — side file or tiny mark script for mixed hyperlapse clips.
4. **Cross-platform GUI** — simple wrapper once CLI flags are stable.
5. **Extras** — map tiles, perimeter dive layout, FIT GPS → timezone for travel.

## Contributing

- Keep media and personal FIT/video files out of git (see `.gitignore`).
- Prefer small PRs: one profile, one bugfix, or docs.
- Test on a short clip before a full batch.
- For new cameras, implement start-time detection first; full telemetry-from-camera is optional.


## Acknowledgments

This project was developed with assistance from [Grok](https://grok.x.ai) (xAI).  
Design decisions, testing, and maintenance are by the project author.

## License

MIT — see [LICENSE](LICENSE).
