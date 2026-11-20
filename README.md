# Workout Overlay

**Version 1.2** — DJI Osmo + Phone Videos + Zepp/Amazfit FIT on Linux

Overlay Amazfit / Zepp FIT telemetry onto action-camera video and stills.  
Built for real outings: one workout FIT, many Osmo and phone clips (with phone video priority), photos taken between or **during** clips, optional hyperlapse, batch export with transitions, outdoor and **dive** panels.

<img width="1931" height="1194" alt="Screenshot_2026-10-09_16-58-32" src="https://github.com/user-attachments/assets/2f17610c-0349-44d3-9c2a-2d8c9ad54cb3" />


## Features

- **Single clip** or **batch folder** (one `.fit` + many videos/photos)
- **Phone Video Priority & Interval Carving** (`--phone-priority`, default on; `--no-phone-priority`): Auto-detects phone clips (by `PXL_`/`IMG_` prefix or container metadata), parses exact UTC start times for Google Pixel files (`PXL_YYYYMMDD_HHMMSSmmm.mp4`), locks phone videos to 1× speed, carves overlapping action-camera footage around phone cutaways on the FIT timeline, and drops short fragments (< 0.5s) for clean transitions while keeping telemetry continuous (1:1).
- **Vertical (9:16) Phone Normalization & Audio Routing**: Normalizes phone clips to project resolution, CFR, yuv420p, and SAR=1. Center vertical 9:16 clips over blurred/dimmed 16:9 background (`--phone-vertical blur`, default) or letterboxed black (`--phone-vertical black`). Route action-camera mic narration onto phone clips with `--keep-camera-audio`.
- **20ms Audio Boundary Fades**: Applies 20ms audio fade-in/fade-out (`afade`) at clip boundaries to eliminate clicks and pops on hard cuts without changing segment durations.
- **Silent 1× Hyperlapse Compression** (`--compress-silent`, default off): Speds up silent sections in 1× action-camera recordings by `--factor` (default 15×) using smooth frame blending (`tblend`/`setpts`) while keeping spoken commentary at 1× and the FIT telemetry clock perfectly synchronized.
- **Outdoor panel**: time, speed, heart rate, elevation, distance + GPS minimap
- **Dive panel** (auto when FIT has depth samples): time, depth, max depth, heart rate, temperature; plus NDL, TTS, ascent rate, CNS when present in the FIT
- **Auto-align** video to FIT via Pixel UTC filename or MP4 `creation_time` (default on)
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
| `--phone-priority` / `--no-phone-priority` | Prioritize phone clips & carve overlapping camera intervals | on |
| `--phone-vertical blur\|black` | Background for vertical (9:16) phone clips | `blur` |
| `--keep-camera-audio` | Route action-camera audio onto overlapping phone cutaways | off |
| `--compress-silent` | Compress silent segments in 1× action-cam footage via `tblend`/`setpts` | off |
| `-o FILE` | Output path | `workout_overlay.mp4` / `workout_batch.mp4` |

`freedive` and `scuba` both use the dive panel; extra scuba metrics (NDL, TTS, …) appear only if the FIT contains them.

## Phone Video Priority & Interval Carving

When filming with both an action camera (e.g. DJI Osmo on a chest mount) and a smartphone (e.g. handheld Google Pixel or iPhone cutaways):
- **Detection**: Phone videos are detected automatically by filename prefix (`PXL_`, `IMG_`) or container metadata (`com.android.manufacturer`, `com.apple.quicktime.make`).
- **Pixel UTC Filename Timestamps**: Google Pixel files (`PXL_YYYYMMDD_HHMMSSmmm*.mp4`) have their exact millisecond-accurate UTC start time parsed directly from the filename before falling back to container creation metadata.
- **1× Speed Lock**: Phone videos are always locked to 1× speed, preserving natural real-time movement and speech regardless of background sound levels.
- **Interval Carving**: On the FIT timeline, any action-camera footage overlapping a higher-priority phone clip is carved out. Leftover camera fragments shorter than 0.5 seconds are cleanly discarded.
- **Continuous Real-Time Telemetry**: FIT telemetry advances 1:1 across phone cutaways with continuous metric tracking and GPS positioning.

## Vertical (9:16) Phone Normalization & Audio Routing

- **Normalization**: Phone videos (which are frequently Variable Frame Rate / VFR or 9:16 vertical) are normalized to the project's target resolution (16:9), CFR, SAR=1, and `yuv420p` pixel format to ensure glitch-free concatenation.
- **`--phone-vertical blur` (default)**: Centers the sharp 9:16 phone video over a scaled, cropped, blurred, and slightly dimmed (-0.12 brightness) 16:9 background copy so the overlay HUD and minimap remain clear and legible.
- **`--phone-vertical black`**: Centers the 9:16 phone video with black letterbox bars.
- **`--keep-camera-audio`**: When enabled and a phone clip overlaps an action camera recording, the action camera's audio track (such as an external wireless microphone) is routed onto the phone video slice.
- **Audio Fades**: A 20ms (0.02s) `afade` is applied at all segment boundaries, preventing audible clicks on cuts without shifting segment durations.

## Silent 1× Hyperlapse Compression (`--compress-silent`)

If your action camera was recorded in standard 1× Video mode rather than Hyperlapse mode, `--compress-silent` automatically detects audio silence and accelerates those segments by `--factor` (default 15×) using smooth frame blending (`tblend=all_mode=average,setpts=PTS/factor`). Spoken commentary segments remain at 1×, and the FIT telemetry clock stays fully synchronized.

## Alignment model

| Media | How it is placed on the FIT timeline |
|-------|--------------------------------------|
| **Phone Video** | Exact UTC from Pixel filename or creation metadata; locked to 1×; carves action camera intervals. |
| **Action Cam Video** | MP4 `creation_time` (UTC) vs FIT start → offset (auto). Hyperlapse map from audio (or `--compress-silent`). |
| **Photo** | EXIF capture time treated as **local**; converted to UTC with the same offset as `--time auto` / `--utc-offset`, then placed on the FIT timeline. |
| **Mid-clip photo** | If photo time falls inside a carved video slice's FIT window, that slice is split and the still is inserted at that moment. |

Original DJI and Pixel files keep reliable timestamps. Edited/merged exports often lose `creation_time` — use a manual offset if needed.

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

Optional CustomTkinter tool for alignment, phone priority, and hyperlapse debugging:

```bash
pip install customtkinter   # once, in ~/dashboard-env
./run_debug_gui.sh
./run_debug_gui.sh /path/to/batch_folder
```

- Pick a batch folder (one `.fit` + videos/photos)
- Media list clearly distinguishes action camera clips (`▶ [CAM]`) from phone recordings (`📱 [PHONE]`)
- Toggle controls for **Phone Priority**, **Vertical (blur/black)**, **Keep Cam Audio**, and **Compress Silent (1×)**
- Preview clip with scrub; see **file time**, **detected factor (1×/15×)**, **mapped FIT time**, sample telemetry
- View render plan highlighting `PHONE [PRIORITY]` cuts (and `+cam_audio` when active)
- Segment list, auto-align note, render plan modal, and copy debug snapshot to clipboard

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

## Interactive Highlight Trimming (`debug_gui.py` + `segments.json`)

Use `debug_gui.py` to trim long rides or dives down to a tight **2–5 minute** highlight reel before rendering:

1. **Launch the visual previewer on your batch folder:**
   ```bash
   ./run_debug_gui.sh /path/to/workout_folder
   ```
2. **Mark `15×` speedup regions (`I` / `O`) or complete cuts (`X` / `C`):**
   - **`15× In [I]` → `15× Out [O]`**: Smoothly accelerates silent `1×` stretches by `15×` using frame blending (`tblend`) while leaving native DJI Hyperlapse stretches untouched.
   - **`Cut In [X]` → `Cut Out [C]`**: Removes boring sections completely (`0s` output) while advancing the FIT telemetry clock cleanly across the jump.
   - **Live `Est. Output` counter**: Tracks predicted final runtime in the top bar (turning green inside the **2–5 minute** target window).
3. **Render automatically with `./run_overlay.sh`:**
   - Markers are saved automatically to `<workout_folder>/segments.json` and applied whenever you run `./run_overlay.sh --batch /path/to/workout_folder`.
