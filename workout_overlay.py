#!/usr/bin/env python3
"""
Workout Overlay
Overlay Amazfit/Zepp FIT telemetry onto video (bike, run, dive clips).

Single clip:
  python workout_overlay.py video.mp4 activity.fit [--preview] [-o out.mp4]

Batch (one FIT + MP4s and/or photos):
  python workout_overlay.py --batch ./folder [--photo-seconds 3] [--preview] [-o out.mp4]
"""

import argparse
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import librosa
import numpy as np
from fitparse import FitFile
from PIL import Image, ImageDraw, ImageFont, ExifTags


VIDEO_EXTS = {".mp4", ".mov", ".MP4", ".MOV"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG", ".webp", ".WEBP"}


# ============================================================
# FIT parsing
# ============================================================
def parse_fit(fit_path):
    fit = FitFile(fit_path)
    records = []
    for record in fit.get_messages("record"):
        data = {f.name: f.value for f in record}
        if "timestamp" in data:
            records.append(data)

    if not records:
        raise ValueError("No records found in FIT file")

    records.sort(key=lambda r: r["timestamp"])
    start_time = records[0]["timestamp"]

    gps_points = []
    for r in records:
        lat = r.get("position_lat")
        lon = r.get("position_long")
        if lat is not None and lon is not None:
            lat = lat * (180.0 / 2**31)
            lon = lon * (180.0 / 2**31)
            gps_points.append((lat, lon, r["timestamp"]))

    print(f"FIT start     : {start_time}")
    print(f"FIT points    : {len(records)}")
    print(f"GPS points    : {len(gps_points)}")
    print(f"FIT duration  : {records[-1]['timestamp'] - start_time}")
    return records, start_time, gps_points


# ============================================================
# Time helpers
# ============================================================
def parse_dji_filename_local(video_path):
    name = os.path.basename(video_path)
    m = re.search(r"(?:DJI_)?(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", name)
    if not m:
        return None
    y, mo, d, h, mi, s = map(int, m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def get_mp4_creation_time_utc(video_path):
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format_tags=creation_time",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        out = subprocess.check_output(cmd, text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if not out:
        return None
    out = out.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(out)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def to_utc_naive(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def video_sort_key(video_path):
    creation = get_mp4_creation_time_utc(video_path)
    if creation is not None:
        return (0, creation.timestamp(), os.path.basename(video_path))
    local = parse_dji_filename_local(video_path)
    if local is not None:
        return (1, local.timestamp(), os.path.basename(video_path))
    return (2, os.path.getmtime(video_path), os.path.basename(video_path))


def get_photo_datetime(photo_path):
    """Best-effort photo capture time as naive datetime (camera local or UTC-ish)."""
    try:
        img = Image.open(photo_path)
        exif = img._getexif() if hasattr(img, "_getexif") else None
        if exif:
            tag_map = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
            for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                raw = tag_map.get(key)
                if raw and isinstance(raw, str):
                    # "2026:10:07 15:01:02"
                    try:
                        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        pass
    except Exception:
        pass
    # fallback: filesystem mtime local
    return datetime.fromtimestamp(os.path.getmtime(photo_path))


def photo_sort_key(photo_path):
    dt = get_photo_datetime(photo_path)
    return (dt.timestamp(), os.path.basename(photo_path))


def resolve_display_offset_hours(video_path, time_mode, utc_offset):
    if utc_offset is not None:
        return float(utc_offset), f"manual --utc-offset {utc_offset}"
    if time_mode == "utc":
        return 0.0, "UTC"
    if time_mode == "local":
        now = datetime.now().astimezone()
        off = now.utcoffset().total_seconds() / 3600.0
        return off, f"system local ({now.tzname()}, UTC{off:+g})"

    creation_utc = get_mp4_creation_time_utc(video_path) if video_path else None
    filename_local = parse_dji_filename_local(video_path) if video_path else None
    if creation_utc is not None and filename_local is not None:
        delta_sec = (filename_local - creation_utc.replace(tzinfo=None)).total_seconds()
        while delta_sec > 14 * 3600:
            delta_sec -= 24 * 3600
        while delta_sec < -12 * 3600:
            delta_sec += 24 * 3600
        offset_h = round(delta_sec / 900.0) * 0.25
        return offset_h, f"DJI auto (filename vs creation_time, UTC{offset_h:+g})"

    now = datetime.now().astimezone()
    off = now.utcoffset().total_seconds() / 3600.0
    return off, f"system local fallback ({now.tzname()}, UTC{off:+g})"


def format_display_time(ts, offset_hours):
    if ts is None:
        return "--:--:--"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    local = ts + timedelta(hours=offset_hours)
    return local.strftime("%H:%M:%S")


def compute_auto_align_offset(video_path, fit_start):
    creation_utc = get_mp4_creation_time_utc(video_path)
    filename_local = parse_dji_filename_local(video_path)
    video_start = None
    source = None
    if creation_utc is not None:
        video_start = to_utc_naive(creation_utc)
        source = "mp4 creation_time"
    elif filename_local is not None:
        video_start = filename_local
        source = "DJI filename"
    if video_start is None:
        print("Auto-align: could not determine video start time")
        return None
    fit_utc = to_utc_naive(fit_start)
    if fit_utc is None:
        return None
    offset = (video_start - fit_utc).total_seconds()
    print("Auto-align:")
    print(f"  video_start = {video_start}  ({source})")
    print(f"  fit_start   = {fit_utc}")
    print(f"  offset      = {offset:.1f} s")
    return offset


def photo_align_offset(photo_path, fit_start, offset_hours=0.0):
    """Map photo capture moment into FIT timeline (seconds from FIT start).

    EXIF times are treated as local wall time at the shoot location.
    offset_hours is the same as display: local = UTC + offset_hours,
    so UTC = local - offset_hours.
    """
    photo_dt = get_photo_datetime(photo_path)
    fit_utc = to_utc_naive(fit_start)
    # Convert EXIF local -> UTC using shoot/display offset
    photo_utc = photo_dt - timedelta(hours=float(offset_hours))
    real_t = (photo_utc - fit_utc).total_seconds()
    print(f"Photo align: {os.path.basename(photo_path)}")
    print(f"  photo_exif_local = {photo_dt}")
    print(f"  offset_hours     = {offset_hours:+g} (local = UTC + offset)")
    print(f"  photo_as_utc     = {photo_utc}")
    print(f"  fit_start_utc    = {fit_utc}")
    print(f"  real_t           = {real_t:.1f} s into FIT")
    return real_t, photo_dt


# ============================================================
# Hyperlapse audio detection
# ============================================================
def detect_speed_segments(video_path, threshold_db=None, min_segment_s=1.0, factor=15.0):
    print("Analyzing audio for hyperlapse / 1x sections...")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_wav = tmp.name
    try:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1", tmp_wav,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        y, sr = librosa.load(tmp_wav, sr=None, mono=True)
        duration = float(librosa.get_duration(y=y, sr=sr))
        hop = 512
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
        rms = np.maximum(rms, 1e-10)
        rms_db = 20.0 * np.log10(rms)
        p10 = float(np.percentile(rms_db, 10))
        p50 = float(np.percentile(rms_db, 50))
        if threshold_db is None:
            threshold_db = p10 + 0.35 * (p50 - p10)
        enter_silence = threshold_db - 1.5
        leave_silence = threshold_db + 1.5
        print(f"Audio threshold: {threshold_db:.1f} dB  | silent factor={factor:g}x")

        silent_state = bool(rms_db[0] < threshold_db)
        raw = np.zeros(len(rms_db), dtype=bool)
        for i, val in enumerate(rms_db):
            if silent_state:
                if val > leave_silence:
                    silent_state = False
            else:
                if val < enter_silence:
                    silent_state = True
            raw[i] = silent_state

        win = max(3, int(0.40 * sr / hop))
        if win % 2 == 0:
            win += 1
        half = win // 2
        smoothed = raw.copy()
        for i in range(len(raw)):
            a = max(0, i - half)
            b = min(len(raw), i + half + 1)
            smoothed[i] = np.mean(raw[a:b]) >= 0.5

        segments = []
        current = bool(smoothed[0])
        seg_start = 0.0
        for i in range(1, len(smoothed)):
            if bool(smoothed[i]) != current:
                speed = factor if current else 1
                segments.append((seg_start, float(times[i]), speed))
                seg_start = float(times[i])
                current = bool(smoothed[i])
        speed = factor if current else 1
        segments.append((seg_start, duration, speed))

        cleaned = []
        for start, end, speed in segments:
            if cleaned and (end - start) < min_segment_s:
                ps, pe, psp = cleaned[-1]
                cleaned[-1] = (ps, end, psp)
            else:
                cleaned.append((start, end, speed))
        if len(cleaned) >= 2 and (cleaned[0][1] - cleaned[0][0]) < min_segment_s:
            s0, e0, sp0 = cleaned[0]
            s1, e1, sp1 = cleaned[1]
            cleaned[0:2] = [(s0, e1, sp1)]

        print("\nDetected segments:")
        for s, e, sp in cleaned:
            print(f"  {s:7.1f} → {e:7.1f}s   {sp:g}x")
        return cleaned
    finally:
        if os.path.exists(tmp_wav):
            os.remove(tmp_wav)


def build_time_map(segments):
    boundaries = []
    real_t = 0.0
    for start, end, speed in segments:
        boundaries.append((start, real_t, speed))
        real_t += (end - start) * speed
    boundaries.append((segments[-1][1], real_t, 1))

    def video_to_real(t):
        if t <= boundaries[0][0]:
            return boundaries[0][1]
        for i in range(len(boundaries) - 1):
            v0, r0, speed = boundaries[i]
            v1 = boundaries[i + 1][0]
            if v0 <= t <= v1:
                return r0 + (t - v0) * speed
        return real_t

    def real_to_video(r):
        """Inverse map: real seconds (within clip, before extra_offset) -> file time."""
        if r <= 0:
            return boundaries[0][0]
        if r >= real_t:
            return boundaries[-1][0]
        for i in range(len(boundaries) - 1):
            v0, r0, speed = boundaries[i]
            r1 = boundaries[i + 1][1]
            if r0 <= r <= r1:
                if speed == 0:
                    return v0
                return v0 + (r - r0) / speed
        return boundaries[-1][0]

    print(f"Total real-world time covered: {real_t:.1f}s\n")
    return video_to_real, real_to_video, real_t


# ============================================================
# Precompute / lookup helpers
# ============================================================
def precompute_data(records, start_time, gps_points, video_to_real, duration, extra_offset, step=0.20):
    print("Precomputing dashboard data...")
    times = np.arange(0, duration + step, step)
    n = len(times)
    speeds = np.full(n, np.nan)
    hrs = np.full(n, np.nan)
    elevs = np.full(n, np.nan)
    dists = np.full(n, np.nan)
    lats = np.full(n, np.nan)
    lons = np.full(n, np.nan)
    clock = []

    rec_times = np.array([(r["timestamp"] - start_time).total_seconds() for r in records])
    rec_speed = np.array([r.get("speed") for r in records], dtype=float)
    rec_hr = np.array([r.get("heart_rate") for r in records], dtype=float)
    rec_elev = np.array([
        r.get("enhanced_altitude") if r.get("enhanced_altitude") is not None else r.get("altitude")
        for r in records
    ], dtype=float)
    rec_dist = np.array([r.get("distance") for r in records], dtype=float)
    rec_ts = [r["timestamp"] for r in records]
    gps_times = np.array([(p[2] - start_time).total_seconds() for p in gps_points]) if gps_points else np.array([])
    gps_lat = np.array([p[0] for p in gps_points]) if gps_points else np.array([])
    gps_lon = np.array([p[1] for p in gps_points]) if gps_points else np.array([])

    for i, t in enumerate(times):
        real_t = video_to_real(t) + extra_offset
        idx = int(np.argmin(np.abs(rec_times - real_t)))
        speeds[i] = rec_speed[idx]
        hrs[i] = rec_hr[idx]
        elevs[i] = rec_elev[idx]
        dists[i] = rec_dist[idx]
        clock.append(rec_ts[idx])
        if len(gps_times) > 0:
            gidx = int(np.argmin(np.abs(gps_times - real_t)))
            lats[i] = gps_lat[gidx]
            lons[i] = gps_lon[gidx]

    print(f"Precomputed {n} points\n")
    return times, speeds, hrs, elevs, dists, lats, lons, clock


def lookup_at_real_t(records, start_time, gps_points, real_t):
    rec_times = np.array([(r["timestamp"] - start_time).total_seconds() for r in records])
    idx = int(np.argmin(np.abs(rec_times - real_t)))
    r = records[idx]
    speed = r.get("speed")
    hr = r.get("heart_rate")
    elev = r.get("enhanced_altitude") if r.get("enhanced_altitude") is not None else r.get("altitude")
    dist = r.get("distance")
    lat = lon = np.nan
    if gps_points:
        gps_times = np.array([(p[2] - start_time).total_seconds() for p in gps_points])
        gidx = int(np.argmin(np.abs(gps_times - real_t)))
        lat, lon = gps_points[gidx][0], gps_points[gidx][1]
    return {
        "speed": float(speed) if speed is not None else np.nan,
        "hr": float(hr) if hr is not None else np.nan,
        "elev": float(elev) if elev is not None else np.nan,
        "dist": float(dist) if dist is not None else np.nan,
        "lat": lat,
        "lon": lon,
        "ts": r["timestamp"],
    }


# ============================================================
# Drawing
# ============================================================
def latlon_to_pixel(lat, lon, min_lat, max_lat, min_lon, max_lon, map_w, map_h, padding=6):
    if max_lat == min_lat or max_lon == min_lon:
        return map_w // 2, map_h // 2
    x = padding + (lon - min_lon) / (max_lon - min_lon) * (map_w - 2 * padding)
    y = padding + (1 - (lat - min_lat) / (max_lat - min_lat)) * (map_h - 2 * padding)
    return int(x), int(y)


def make_overlay_rgba_values(speed, hr, elev, dist, lat, lon, ts,
                             gps_points, w, h, fonts, offset_hours):
    font_label, font_value, font_time = fonts
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    speed_kmh = speed * 3.6 if not np.isnan(speed) else None
    dist_km = dist / 1000 if not np.isnan(dist) else None
    hr_val = None if np.isnan(hr) else int(hr)
    elev_val = None if np.isnan(elev) else elev
    time_str = format_display_time(ts, offset_hours)

    scale = max(1.0, w / 1920.0)
    margin = int(24 * scale)
    box_w = int(420 * scale)
    box_h = int(340 * scale)
    x0 = margin
    y0 = h - box_h - margin

    draw.rectangle([x0, y0, x0 + box_w, y0 + box_h], fill=(0, 0, 0, 185))
    draw.rectangle([x0, y0, x0 + box_w, y0 + box_h], outline=(255, 255, 255, 45), width=1)

    y = y0 + int(12 * scale)

    def row(label, value, unit="", accent=(120, 200, 255, 255)):
        nonlocal y
        val = (
            f"{value:.1f}" if isinstance(value, float)
            else (str(value) if value is not None else "--")
        )
        draw.text((x0 + int(18 * scale), y), label, font=font_label, fill=accent)
        draw.text(
            (x0 + int(18 * scale), y + int(24 * scale)),
            f"{val} {unit}".strip(),
            font=font_value,
            fill=(255, 255, 255, 255),
        )
        y += int(58 * scale)

    draw.text((x0 + int(18 * scale), y), "TIME", font=font_label, fill=(220, 220, 220, 255))
    draw.text(
        (x0 + int(18 * scale), y + int(24 * scale)),
        time_str,
        font=font_time,
        fill=(255, 255, 255, 255),
    )
    y += int(60 * scale)

    row("SPEED", speed_kmh, "km/h", (120, 200, 255, 255))
    row("HEART RATE", hr_val, "bpm", (255, 120, 120, 255))
    row("ELEVATION", elev_val, "m", (140, 255, 180, 255))
    row("DISTANCE", dist_km, "km", (255, 210, 120, 255))

    if len(gps_points) >= 2:
        map_w = int(280 * scale)
        map_h = int(200 * scale)
        mx = w - map_w - margin
        my = h - map_h - margin
        draw.rectangle([mx, my, mx + map_w, my + map_h], fill=(0, 0, 0, 170))
        draw.rectangle([mx, my, mx + map_w, my + map_h], outline=(255, 255, 255, 45), width=1)
        lats_all = [p[0] for p in gps_points]
        lons_all = [p[1] for p in gps_points]
        min_lat, max_lat = min(lats_all), max(lats_all)
        min_lon, max_lon = min(lons_all), max(lons_all)
        points = []
        for glat, glon, _ in gps_points:
            px, py = latlon_to_pixel(glat, glon, min_lat, max_lat, min_lon, max_lon, map_w, map_h)
            points.append((mx + px, my + py))
        if len(points) >= 2:
            draw.line(points, fill=(0, 200, 255, 220), width=max(2, int(3 * scale)))
        if not np.isnan(lat) and not np.isnan(lon):
            cx, cy = latlon_to_pixel(lat, lon, min_lat, max_lat, min_lon, max_lon, map_w, map_h)
            r = max(5, int(6 * scale))
            draw.ellipse(
                [mx + cx - r, my + cy - r, mx + cx + r, my + cy + r],
                fill=(255, 50, 50, 255),
            )

    rgba = np.array(img)
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)


def make_overlay_rgba(idx, speeds, hrs, elevs, dists, lats, lons, clock,
                      gps_points, w, h, fonts, offset_hours):
    return make_overlay_rgba_values(
        speeds[idx], hrs[idx], elevs[idx], dists[idx], lats[idx], lons[idx], clock[idx],
        gps_points, w, h, fonts, offset_hours,
    )


def alpha_blend(base_bgr, overlay_bgra):
    alpha = overlay_bgra[:, :, 3:4].astype(np.float32) / 255.0
    overlay_rgb = overlay_bgra[:, :, :3].astype(np.float32)
    base = base_bgr.astype(np.float32)
    return (base * (1.0 - alpha) + overlay_rgb * alpha).astype(np.uint8)


def load_fonts(out_w):
    try:
        font_label = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            max(16, int(18 * out_w / 1920)),
        )
        font_value = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            max(28, int(34 * out_w / 1920)),
        )
        font_time = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            max(30, int(36 * out_w / 1920)),
        )
    except Exception:
        font_label = ImageFont.load_default()
        font_value = font_label
        font_time = font_label
    return font_label, font_value, font_time


def fit_image_to_canvas(photo_path, out_w, out_h):
    """Letterbox photo onto BGR canvas."""
    img = Image.open(photo_path).convert("RGB")
    # honor EXIF orientation
    try:
        img = ImageOps_exif_transpose(img)
    except Exception:
        pass
    iw, ih = img.size
    scale = min(out_w / iw, out_h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (out_w, out_h), (0, 0, 0))
    canvas.paste(img, ((out_w - nw) // 2, (out_h - nh) // 2))
    rgb = np.array(canvas)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def ImageOps_exif_transpose(img):
    from PIL import ImageOps
    return ImageOps.exif_transpose(img)


# ============================================================
# Single video overlay
# ============================================================
def create_overlay(video_path, fit_path, output_path, extra_offset=0.0,
                   step=0.20, preview=False, time_mode="auto", utc_offset=None,
                   auto_align=True, offset_explicit=False,
                   records=None, start_time=None, gps_points=None,
                   factor=15.0, file_t_start=0.0, file_t_end=None,
                   segments=None, fixed_offset=None):
    """Render overlay for a full video or a [file_t_start, file_t_end) slice."""
    if records is None or start_time is None or gps_points is None:
        records, start_time, gps_points = parse_fit(fit_path)

    if segments is None:
        segments = detect_speed_segments(video_path, factor=factor)
    video_to_real, real_to_video, _total_real = build_time_map(segments)

    if fixed_offset is not None:
        extra_offset = float(fixed_offset)
        print(f"Alignment     : fixed offset {extra_offset:.1f} s")
    elif offset_explicit:
        print(f"Alignment     : manual offset {extra_offset:.1f} s")
    elif auto_align:
        auto = compute_auto_align_offset(video_path, start_time)
        extra_offset = auto if auto is not None else 0.0
        if auto is None:
            print("Alignment     : auto-align failed, using 0")
    else:
        print(f"Alignment     : auto-align disabled, offset {extra_offset:.1f} s")

    offset_hours, offset_desc = resolve_display_offset_hours(video_path, time_mode, utc_offset)
    print(f"Display time   : {offset_desc}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frame_count / fps if fps > 0 else 0

    t0 = max(0.0, float(file_t_start or 0.0))
    t1 = float(file_t_end) if file_t_end is not None else duration
    t1 = min(duration, t1)
    if t1 <= t0 + 0.05:
        raise RuntimeError(f"Empty video slice {t0:.2f}-{t1:.2f}s in {video_path}")

    if preview:
        out_h = 1080
        out_w = int(src_w * (out_h / src_h))
        if out_w % 2:
            out_w += 1
        print(f"Preview mode: scaling {src_w}x{src_h} → {out_w}x{out_h}")
    else:
        out_w, out_h = src_w, src_h

    slice_note = ""
    if t0 > 0.01 or t1 < duration - 0.01:
        slice_note = f" | slice {t0:.2f}-{t1:.2f}s"
    print(f"Video: {duration:.1f}s | src {src_w}x{src_h} | out {out_w}x{out_h} | {fps:.3f} fps{slice_note}")

    times, speeds, hrs, elevs, dists, lats, lons, clock = precompute_data(
        records, start_time, gps_points, video_to_real,
        duration, extra_offset, step=step,
    )
    fonts = load_fonts(out_w)

    tmp_video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    writer = cv2.VideoWriter(tmp_video, cv2.VideoWriter_fourcc(*"mp4v"), fps, (out_w, out_h))
    if not writer.isOpened():
        raise RuntimeError("Could not open VideoWriter")

    cache = {}
    print("Rendering frames...")
    # Seek near start of slice
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(t0 * fps) - 1))
    # Drain until we reach t0
    while True:
        pos = cap.get(cv2.CAP_PROP_POS_FRAMES)
        if pos >= t0 * fps - 0.5:
            break
        ok, _ = cap.read()
        if not ok:
            break

    frames_written = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
        t = frame_idx / fps if fps > 0 else 0.0
        if t < t0 - 1.0 / max(fps, 1):
            continue
        if t >= t1:
            break

        if preview and (frame.shape[1] != out_w or frame.shape[0] != out_h):
            frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)

        idx = int(round(t / step))
        idx = max(0, min(idx, len(times) - 1))
        if idx not in cache:
            cache[idx] = make_overlay_rgba(
                idx, speeds, hrs, elevs, dists, lats, lons, clock,
                gps_points, out_w, out_h, fonts, offset_hours,
            )
        writer.write(alpha_blend(frame, cache[idx]))
        frames_written += 1
        if frames_written % 300 == 0:
            print(f"  slice frames {frames_written} (t≈{t:.1f}s)")

    cap.release()
    writer.release()
    if frames_written == 0:
        os.remove(tmp_video)
        raise RuntimeError(f"No frames written for slice {t0}-{t1} of {video_path}")

    print("Muxing audio with ffmpeg...")
    cmd = [
        "ffmpeg", "-y",
        "-i", tmp_video,
        "-ss", f"{t0:.6f}",
        "-to", f"{t1:.6f}",
        "-i", video_path,
        "-map", "0:v:0",
        "-map", "1:a:0?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-c:a", "aac",
        "-shortest",
        output_path,
    ]
    subprocess.run(cmd, check=True)
    os.remove(tmp_video)
    print(f"\nDone clip → {output_path}")
    return output_path



def create_photo_clip(photo_path, output_path, records, start_time, gps_points,
                      out_w, out_h, fps, photo_seconds, time_mode, utc_offset,
                      ref_video_for_tz=None):
    # Resolve timezone first so EXIF local can be converted to UTC for FIT lookup
    offset_hours, offset_desc = resolve_display_offset_hours(
        ref_video_for_tz, time_mode, utc_offset
    )
    print(f"Display time   : {offset_desc}")

    real_t, photo_dt = photo_align_offset(photo_path, start_time, offset_hours=offset_hours)
    data = lookup_at_real_t(records, start_time, gps_points, real_t)

    fonts = load_fonts(out_w)
    base = fit_image_to_canvas(photo_path, out_w, out_h)
    overlay = make_overlay_rgba_values(
        data["speed"], data["hr"], data["elev"], data["dist"],
        data["lat"], data["lon"], data["ts"],
        gps_points, out_w, out_h, fonts, offset_hours,
    )
    frame = alpha_blend(base, overlay)

    n_frames = max(1, int(round(photo_seconds * fps)))
    tmp_video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
    writer = cv2.VideoWriter(tmp_video, cv2.VideoWriter_fourcc(*"mp4v"), fps, (out_w, out_h))
    if not writer.isOpened():
        raise RuntimeError("Could not open VideoWriter for photo")
    for _ in range(n_frames):
        writer.write(frame)
    writer.release()

    # silent audio track so concat with video clips is happier
    cmd = [
        "ffmpeg", "-y",
        "-i", tmp_video,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-c:a", "aac",
        "-shortest",
        output_path,
    ]
    subprocess.run(cmd, check=True)
    os.remove(tmp_video)
    print(f"Done photo → {output_path} ({photo_seconds}s @ {photo_dt})")
    return output_path


# ============================================================
# Batch
# ============================================================
def discover_batch(folder):
    folder = Path(folder)
    if not folder.is_dir():
        raise RuntimeError(f"Not a directory: {folder}")

    fits = list({p.resolve(): p for p in list(folder.glob("*.fit")) + list(folder.glob("*.FIT"))}.values())
    videos = [p for p in folder.iterdir() if p.is_file() and p.suffix in VIDEO_EXTS]
    photos = [p for p in folder.iterdir() if p.is_file() and p.suffix in PHOTO_EXTS]

    if len(fits) == 0:
        raise RuntimeError(f"No .fit file found in {folder}")
    if len(fits) > 1:
        names = ", ".join(f.name for f in fits)
        raise RuntimeError(f"Multiple .fit files found ({names}). Keep exactly one.")
    if not videos and not photos:
        raise RuntimeError(f"No videos or photos found in {folder}")

    videos_sorted = sorted((str(v) for v in videos), key=video_sort_key)
    photos_sorted = sorted((str(p) for p in photos), key=photo_sort_key)
    fit_path = str(fits[0])

    print(f"Batch folder  : {folder}")
    print(f"FIT           : {fits[0].name}")
    print(f"Videos ({len(videos_sorted)}):")
    for i, v in enumerate(videos_sorted, 1):
        creation = get_mp4_creation_time_utc(v)
        cstr = creation.isoformat() if creation else "no creation_time"
        print(f"  {i}. {os.path.basename(v)}  [{cstr}]")
    print(f"Photos ({len(photos_sorted)}):")
    for i, ph in enumerate(photos_sorted, 1):
        print(f"  {i}. {os.path.basename(ph)}  [{get_photo_datetime(ph)}]")
    print()
    return fit_path, videos_sorted, photos_sorted


def media_timeline(videos, photos):
    """Build sorted list of (sort_ts, kind, path)."""
    items = []
    for v in videos:
        creation = get_mp4_creation_time_utc(v)
        if creation is not None:
            ts = creation.timestamp()
        else:
            local = parse_dji_filename_local(v)
            ts = local.timestamp() if local else os.path.getmtime(v)
        items.append((ts, "video", v))
    for ph in photos:
        dt = get_photo_datetime(ph)
        items.append((dt.timestamp(), "photo", ph))
    items.sort(key=lambda x: (x[0], x[1], x[2]))
    return items


def get_media_duration(path):
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def get_video_size_fps(path):
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "csv=p=0",
        path,
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    parts = out.split(",")
    w, h = int(parts[0]), int(parts[1])
    num, den = parts[2].split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return w, h, fps


def _prepare_clip_fade_black(src, dst, width, height, fps, fade_in, fade_out):
    """
    Normalize clip; add EXTRA freeze pads and fade through black.
    fade_in/fade_out are seconds of extra time (content length unchanged).
    """
    dur = get_media_duration(src)
    vf = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        f"fps={fps}",
        "format=yuv420p",
    ]
    # Extra freeze at ends, then fade those freezes to/from black
    if fade_in > 0:
        vf.append(f"tpad=start_mode=clone:start_duration={fade_in:.6f}")
    if fade_out > 0:
        vf.append(f"tpad=stop_mode=clone:stop_duration={fade_out:.6f}")
    if fade_in > 0:
        vf.append(f"fade=t=in:st=0:d={fade_in:.6f}")
    if fade_out > 0:
        # original content ends at fade_in+dur; fade out covers the end pad
        st = fade_in + dur
        vf.append(f"fade=t=out:st={st:.6f}:d={fade_out:.6f}")
    vf_str = ",".join(vf)

    af_parts = []
    if fade_in > 0:
        ms = int(round(fade_in * 1000))
        af_parts.append(f"adelay={ms}|{ms}:all=1")
        af_parts.append(f"afade=t=in:st=0:d={fade_in:.6f}")
    if fade_out > 0:
        af_parts.append(f"apad=pad_dur={fade_out:.6f}")
        st = fade_in + dur
        af_parts.append(f"afade=t=out:st={st:.6f}:d={fade_out:.6f}")
    af_str = ",".join(af_parts) if af_parts else "anull"

    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vf", vf_str,
        "-af", af_str,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-c:a", "aac",
        "-ar", "48000",
        "-ac", "2",
        dst,
    ]
    subprocess.run(cmd, check=True)


def concat_videos(video_paths, output_path, transition_seconds=0.0):
    """Concatenate clips.
    transition_seconds > 0: fade to black then from black as EXTRA time
    (no hard cuts, content length preserved).
    """
    if len(video_paths) == 1:
        cmd = ["ffmpeg", "-y", "-i", video_paths[0], "-c", "copy", output_path]
        subprocess.run(cmd, check=True)
        print(f"\nDone batch → {output_path}")
        return

    transition_seconds = float(transition_seconds or 0.0)
    if transition_seconds <= 0:
        list_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        try:
            for vp in video_paths:
                safe = os.path.abspath(vp).replace("'", "'\\''")
                list_file.write("file '" + safe + "'\n")
            list_file.close()
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_file.name,
                "-c", "copy",
                output_path,
            ]
            print("Concatenating clips (hard cuts, no transition)...")
            subprocess.run(cmd, check=True)
            print(f"\nDone batch → {output_path}")
        finally:
            if os.path.exists(list_file.name):
                os.remove(list_file.name)
        return

    # Half fade out, half fade in; total extra time per gap = transition_seconds
    half = transition_seconds / 2.0
    if half < 0.05:
        half = transition_seconds  # very short: all on fade out/in minimum

    width, height, fps = get_video_size_fps(video_paths[0])
    if abs(fps - 29.97) < 0.05 or abs(fps - 59.94) < 0.05:
        fps = 30.0
    else:
        fps = max(1.0, round(fps, 3))

    print(f"Transition normalize: {width}x{height} @ {fps:g} fps")
    print(f"Fade through black: {transition_seconds:g}s extra per gap "
          f"({half:g}s out + {half:g}s in)")

    tmp_dir = tempfile.mkdtemp(prefix="fade_black_")
    prepared = []
    try:
        n = len(video_paths)
        for i, vp in enumerate(video_paths):
            fade_in = half if i > 0 else 0.0
            fade_out = half if i < n - 1 else 0.0
            dst = os.path.join(tmp_dir, f"fade_{i:03d}.mp4")
            _prepare_clip_fade_black(vp, dst, width, height, fps, fade_in, fade_out)
            prepared.append(dst)

        list_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        try:
            for vp in prepared:
                safe = os.path.abspath(vp).replace("'", "'\\''")
                list_file.write("file '" + safe + "'\n")
            list_file.close()
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_file.name,
                "-c", "copy",
                output_path,
            ]
            print("Concatenating faded clips...")
            subprocess.run(cmd, check=True)
            print(f"\nDone batch → {output_path}")
        finally:
            if os.path.exists(list_file.name):
                os.remove(list_file.name)
    finally:
        for pth in prepared:
            if os.path.exists(pth):
                os.remove(pth)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass



def build_batch_plan(videos, photos, fit_path, records, start_time, gps_points,
                     extra_offset, auto_align, offset_explicit, factor,
                     time_mode, utc_offset):
    """
    Build ordered render plan: video slices and photos.
    Photos whose time falls inside a video are inserted mid-clip (split video).
    """
    ref_video = videos[0] if videos else None
    offset_hours, offset_desc = resolve_display_offset_hours(ref_video, time_mode, utc_offset)
    print(f"Batch display TZ: {offset_desc}")

    # Photo real_t into FIT
    photo_meta = []
    for ph in photos:
        real_t, pdt = photo_align_offset(ph, start_time, offset_hours=offset_hours)
        photo_meta.append({"path": ph, "real_t": real_t, "dt": pdt})
    photo_meta.sort(key=lambda p: p["real_t"])

    used = set()
    plan = []  # list of {"kind", "path", ... "sort_key"}

    for vpath in videos:
        print("=" * 60)
        print(f"Planning splits for: {os.path.basename(vpath)}")
        print("=" * 60)
        segments = detect_speed_segments(vpath, factor=factor)
        video_to_real, real_to_video, total_real = build_time_map(segments)

        if offset_explicit:
            align = float(extra_offset)
        elif auto_align:
            auto = compute_auto_align_offset(vpath, start_time)
            align = auto if auto is not None else 0.0
        else:
            align = float(extra_offset)

        cap = cv2.VideoCapture(vpath)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = frame_count / fps if fps > 0 else 0.0
        cap.release()

        v_real0 = align + video_to_real(0.0)
        v_real1 = align + video_to_real(duration)

        inside = [
            p for p in photo_meta
            if p["path"] not in used and v_real0 < p["real_t"] < v_real1
        ]
        inside.sort(key=lambda p: p["real_t"])

        # Build cut list: file times with optional photos at each cut (except 0)
        cuts = [0.0]
        photos_after_cut = {}  # file_t -> [photo, ...]
        for p in inside:
            vt = real_to_video(p["real_t"] - align)
            vt = max(0.0, min(duration, float(vt)))
            # quantize slightly to merge near-duplicates
            key = round(vt, 2)
            cuts.append(key)
            photos_after_cut.setdefault(key, []).append(p)
            used.add(p["path"])
        cuts.append(round(duration, 3))
        cuts = sorted(set(cuts))

        print(f"  file duration {duration:.2f}s | real window {v_real0:.1f}..{v_real1:.1f}s into FIT")
        print(f"  mid-clip photos: {len(inside)} | split points: {cuts}")

        for i in range(len(cuts) - 1):
            a, b = cuts[i], cuts[i + 1]
            if b - a >= 0.08:
                sort_key = align + video_to_real(a)
                plan.append({
                    "kind": "video_slice",
                    "path": vpath,
                    "t0": a,
                    "t1": b,
                    "align": align,
                    "segments": segments,
                    "sort_key": sort_key,
                })
            # photos at cut b (start of next piece / end of this)
            for p in photos_after_cut.get(cuts[i + 1], []):
                plan.append({
                    "kind": "photo",
                    "path": p["path"],
                    "sort_key": p["real_t"],
                })

    # Photos not inside any video: place by real_t
    for p in photo_meta:
        if p["path"] not in used:
            plan.append({
                "kind": "photo",
                "path": p["path"],
                "sort_key": p["real_t"],
            })
            used.add(p["path"])

    plan.sort(key=lambda e: (e["sort_key"], 0 if e["kind"] == "video_slice" else 1, e["path"]))

    print("\nFinal render plan:")
    for i, e in enumerate(plan, 1):
        if e["kind"] == "video_slice":
            print(f"  {i}. [video] {os.path.basename(e['path'])}  {e['t0']:.2f}-{e['t1']:.2f}s")
        else:
            print(f"  {i}. [photo] {os.path.basename(e['path'])}")
    print()
    return plan, offset_hours


def run_batch(folder, output_path, extra_offset=0.0, step=0.20, preview=False,
              time_mode="auto", utc_offset=None, auto_align=True, offset_explicit=False,
              factor=15.0, photo_seconds=3.0, transition_seconds=0.0):
    fit_path, videos, photos = discover_batch(folder)
    records, start_time, gps_points = parse_fit(fit_path)

    plan, _offset_hours = build_batch_plan(
        videos, photos, fit_path, records, start_time, gps_points,
        extra_offset, auto_align, offset_explicit, factor,
        time_mode, utc_offset,
    )
    if not plan:
        raise RuntimeError("Batch plan is empty")

    # Output size from first video slice or default
    first_video = next((e["path"] for e in plan if e["kind"] == "video_slice"), None)
    if first_video:
        cap = cv2.VideoCapture(first_video)
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.release()
        if preview:
            out_h = 1080
            out_w = int(src_w * (out_h / src_h))
            if out_w % 2:
                out_w += 1
        else:
            out_w, out_h = src_w, src_h
    else:
        out_w, out_h, fps = 1920, 1080, 30.0

    ref_video = videos[0] if videos else None
    tmp_dir = tempfile.mkdtemp(prefix="overlay_batch_")
    clip_outputs = []

    try:
        for i, e in enumerate(plan, 1):
            print("=" * 60)
            if e["kind"] == "video_slice":
                print(f"Batch item {i}/{len(plan)}: [video] {os.path.basename(e['path'])} "
                      f"{e['t0']:.2f}-{e['t1']:.2f}s")
            else:
                print(f"Batch item {i}/{len(plan)}: [photo] {os.path.basename(e['path'])}")
            print("=" * 60)
            out_clip = os.path.join(tmp_dir, f"clip_{i:03d}.mp4")

            if e["kind"] == "video_slice":
                create_overlay(
                    e["path"], fit_path, output_path=out_clip,
                    extra_offset=extra_offset, step=step, preview=preview,
                    time_mode=time_mode, utc_offset=utc_offset,
                    auto_align=False, offset_explicit=False,
                    records=records, start_time=start_time, gps_points=gps_points,
                    factor=factor,
                    file_t_start=e["t0"], file_t_end=e["t1"],
                    segments=e["segments"], fixed_offset=e["align"],
                )
            else:
                create_photo_clip(
                    e["path"], out_clip, records, start_time, gps_points,
                    out_w, out_h, fps, photo_seconds, time_mode, utc_offset,
                    ref_video_for_tz=ref_video,
                )
            clip_outputs.append(out_clip)

        if len(clip_outputs) == 1:
            cmd = ["ffmpeg", "-y", "-i", clip_outputs[0], "-c", "copy", output_path]
            subprocess.run(cmd, check=True)
            print(f"\nDone batch → {output_path}")
        else:
            concat_videos(clip_outputs, output_path, transition_seconds=transition_seconds)
    finally:
        for p in clip_outputs:
            if os.path.exists(p):
                os.remove(p)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass



def main():
    p = argparse.ArgumentParser(description="Overlay FIT workout data onto video")
    p.add_argument("video", nargs="?", help="Input MP4 (single-clip mode)")
    p.add_argument("fit", nargs="?", help="Input FIT file (single-clip mode)")
    p.add_argument("offset", nargs="?", type=float, default=None,
                   help="Manual alignment offset seconds (overrides auto-align)")
    p.add_argument("--batch", metavar="DIR", help="Folder with one .fit and MP4s/photos")
    p.add_argument("-o", "--output", default=None, help="Output MP4")
    p.add_argument("--preview", action="store_true", help="Render at 1080p for speed")
    p.add_argument("--step", type=float, default=0.20, help="Overlay update step seconds")
    p.add_argument("--time", choices=("auto", "local", "utc"), default="auto",
                   help="Time display mode")
    p.add_argument("--utc-offset", type=float, default=None,
                   help="Manual UTC offset hours for display")
    p.add_argument("--auto-align", dest="auto_align", action="store_true", default=True)
    p.add_argument("--no-auto-align", dest="auto_align", action="store_false")
    p.add_argument("--factor", type=float, default=15.0,
                   help="Hyperlapse factor for silent sections (default: 15)")
    p.add_argument("--photo-seconds", type=float, default=3.0,
                   help="Seconds to show each photo in batch mode (default: 3)")
    p.add_argument("--transition-seconds", type=float, default=0.0,
                   help="Fade through black between clips (extra seconds, 0=hard cut, e.g. 2)")
    args = p.parse_args()

    offset_explicit = args.offset is not None
    extra_offset = 0.0 if args.offset is None else args.offset

    if args.batch:
        output = args.output or "workout_batch.mp4"
        run_batch(
            args.batch, output_path=output,
            extra_offset=extra_offset, step=args.step, preview=args.preview,
            time_mode=args.time, utc_offset=args.utc_offset,
            auto_align=args.auto_align, offset_explicit=offset_explicit,
            factor=args.factor, photo_seconds=args.photo_seconds,
            transition_seconds=args.transition_seconds,
        )
        return

    if not args.video or not args.fit:
        p.error("single-clip mode requires video and fit, or use --batch DIR")

    output = args.output or "workout_overlay.mp4"
    create_overlay(
        args.video, args.fit, output_path=output,
        extra_offset=extra_offset, step=args.step, preview=args.preview,
        time_mode=args.time, utc_offset=args.utc_offset,
        auto_align=args.auto_align, offset_explicit=offset_explicit,
        factor=args.factor,
    )


if __name__ == "__main__":
    main()
