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
import json
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
def _rec_float(r, *names):
    """First present numeric field among names, else nan."""
    for name in names:
        v = r.get(name)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return np.nan


# FIT sport enum (common values). fitparse may return int or string.
SPORT_ID_MAP = {
    0: "generic", 1: "running", 2: "cycling", 5: "swimming",
    11: "walking", 15: "rowing", 17: "hiking", 21: "e_biking",
    53: "diving",
}

# Session sport / sub_sport strings (and id names) that use the dive panel
DIVE_SPORT_TOKENS = {
    "diving", "dive", "scuba", "scuba_diving", "apnea", "apnea_diving",
    "freedive", "free_dive", "freediving", "spearfishing",
}


def read_session_sport(fit_path):
    """
    Read sport / sub_sport from the first session message.
    Returns dict: sport, sub_sport, sport_id, sport_name (best-effort strings/ids).
    """
    fit = FitFile(fit_path)
    out = {"sport": None, "sub_sport": None, "sport_id": None, "sport_name": None}
    for session_msg in fit.get_messages("session"):
        data = {f.name: f.value for f in session_msg}
        sport = data.get("sport")
        sub = data.get("sub_sport")
        out["sport"] = sport
        out["sub_sport"] = sub
        if isinstance(sport, int):
            out["sport_id"] = sport
            out["sport_name"] = SPORT_ID_MAP.get(sport, f"id_{sport}")
        elif sport is not None:
            out["sport_name"] = str(sport).strip().lower()
            # reverse lookup if string matches map values
            for sid, name in SPORT_ID_MAP.items():
                if name == out["sport_name"]:
                    out["sport_id"] = sid
                    break
        break
    return out


def detect_sport(records, forced=None, fit_path=None, session=None):
    """
    Return panel mode: 'dive' or 'outdoor'.

    Priority:
      1. forced flag (dive|freedive|scuba|outdoor)
      2. session sport / sub_sport from FIT (if fit_path or session given)
      3. depth samples in records (>= 5)
      4. outdoor
    """
    if forced and forced not in ("auto", None, ""):
        if forced in ("dive", "freedive", "scuba"):
            return "dive"
        return "outdoor"

    if session is None and fit_path:
        try:
            session = read_session_sport(fit_path)
        except Exception:
            session = None

    if session:
        tokens = []
        for key in ("sport_name", "sport", "sub_sport"):
            v = session.get(key)
            if v is None:
                continue
            if isinstance(v, int):
                tokens.append(SPORT_ID_MAP.get(v, str(v)).lower())
                if v == 53:
                    return "dive"
            else:
                tokens.append(str(v).strip().lower().replace(" ", "_"))
        for t in tokens:
            if t in DIVE_SPORT_TOKENS or "dive" in t or "scuba" in t or "apnea" in t:
                return "dive"

    n_depth = sum(1 for r in records if r.get("depth") is not None)
    if n_depth >= 5:
        return "dive"
    return "outdoor"


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

    session = None
    try:
        session = read_session_sport(fit_path)
    except Exception:
        session = None

    sport = detect_sport(records, fit_path=fit_path, session=session)
    n_depth = sum(1 for r in records if r.get("depth") is not None)
    n_temp = sum(1 for r in records if r.get("temperature") is not None)

    print(f"FIT start     : {start_time}")
    print(f"FIT points    : {len(records)}")
    print(f"GPS points    : {len(gps_points)}")
    print(f"FIT duration  : {records[-1]['timestamp'] - start_time}")
    if session:
        print(
            f"Session sport : sport={session.get('sport')!r}  "
            f"sub_sport={session.get('sub_sport')!r}  "
            f"name={session.get('sport_name')!r}"
        )
    print(f"Sport detect  : {sport}  (depth samples={n_depth}, temp samples={n_temp})")
    if sport == "dive" or n_depth >= 5:
        immersions = detect_dive_immersions(records, start_time)
        if immersions:
            print(
                f"Dive immersions: {len(immersions)}  "
                f"(start>={DIVE_START_DEPTH_M}m end<={DIVE_END_DEPTH_M}m "
                f"min>={DIVE_MIN_DURATION_S}s start_hold>={DIVE_START_HOLD_S}s "
                f"end_hold>={DIVE_END_HOLD_S}s merge_gap<={DIVE_MERGE_SURFACE_GAP_S}s)"
            )


            for im in immersions:
                print(
                    f"  #{im['num']}  {im['start_s']:.1f}→{im['end_s']:.1f}s  "
                    f"({im['end_s'] - im['start_s']:.0f}s)  max={im['max_depth']:.1f}m"
                )
        else:
            print("Dive immersions: none (try lower depth threshold if Zepp shows dives)")
    return records, start_time, gps_points


# ============================================================
# Time helpers
# ============================================================
def parse_pixel_filename_utc(video_path):
    """Parse exact UTC start time from Google Pixel filename (PXL_YYYYMMDD_HHMMSSmmm*.mp4)."""
    if not video_path:
        return None
    name = os.path.basename(video_path)
    m = re.search(r"PXL_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?:(\d{3}))?", name, re.IGNORECASE)
    if not m:
        return None
    y, mo, d, h, mi, s = map(int, m.groups()[:6])
    ms_str = m.group(7)
    ms = int(ms_str) if ms_str is not None else 0
    try:
        return datetime(y, mo, d, h, mi, s, ms * 1000, tzinfo=timezone.utc)
    except ValueError:
        return None


def is_phone_video(video_path):
    """
    Detect whether video is a phone recording by filename prefix (PXL_, IMG_)
    or ffprobe container manufacturer metadata.
    """
    if not video_path:
        return False
    name = os.path.basename(video_path).upper()
    if name.startswith("PXL_") or name.startswith("IMG_"):
        return True
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format_tags=com.android.manufacturer,com.apple.quicktime.make,make,manufacturer",
        "-of", "default=noprint_wrappers=1",
        video_path,
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            line = line.strip()
            if "=" in line:
                val = line.split("=", 1)[1].strip()
                if val:
                    return True
    except Exception:
        pass
    return False


def get_effective_video_dimensions(video_path):
    """Return (width, height, rotation) taking rotation metadata into account."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:stream_tags=rotate:side_data=rotation",
        "-of", "json",
        video_path,
    ]
    try:
        data = json.loads(subprocess.check_output(cmd, text=True))
        streams = data.get("streams", [])
        if not streams:
            return 1920, 1080, 0
        stream = streams[0]
        w = int(stream.get("width", 1920))
        h = int(stream.get("height", 1080))
        rotate = 0
        tags = stream.get("tags", {})
        if "rotate" in tags:
            try:
                rotate = int(tags["rotate"])
            except ValueError:
                pass
        for sd in stream.get("side_data_list", []):
            if "rotation" in sd:
                try:
                    rotate = int(sd["rotation"])
                except ValueError:
                    pass
        if abs(rotate) in (90, 270):
            return h, w, rotate
        return w, h, rotate
    except Exception:
        try:
            w, h, _ = get_video_size_fps(video_path)
            return w, h, 0
        except Exception:
            return 1920, 1080, 0


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
    pixel = parse_pixel_filename_utc(video_path)
    if pixel is not None:
        return (0, pixel.timestamp(), os.path.basename(video_path))
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
    pixel_utc = parse_pixel_filename_utc(video_path)
    creation_utc = get_mp4_creation_time_utc(video_path)
    filename_local = parse_dji_filename_local(video_path)
    video_start = None
    source = None
    if pixel_utc is not None:
        video_start = to_utc_naive(pixel_utc)
        source = "Pixel filename UTC"
    elif creation_utc is not None:
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


def has_audio_stream(video_path):
    if not video_path:
        return False
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0",
        video_path,
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
        return bool(out)
    except Exception:
        return False


def normalize_phone_video_clip(
    src_video_path, dst_video_path, out_w, out_h, fps,
    phone_vertical="blur", t0=0.0, t1=None,
    cam_audio_source=None, cam_audio_t0=None, cam_audio_t1=None,
):
    """
    Normalize phone video clip (or slice [t0, t1]) to project resolution, CFR,
    yuv420p, and SAR=1 so concat never drifts or fails on VFR phone footage.
    If vertical (9:16), centers over blurred (-0.12 brightness) or black 16:9 background.
    If cam_audio_source provided, routes external action-camera audio.
    Applies 20ms (0.02s) audio fade-in / fade-out at segment boundaries.
    """
    total_dur = get_media_duration(src_video_path)
    t0 = max(0.0, float(t0 or 0.0))
    t1 = float(t1) if t1 is not None else total_dur
    t1 = min(total_dur, t1)
    slice_dur = max(0.01, t1 - t0)

    eff_w, eff_h, _ = get_effective_video_dimensions(src_video_path)
    is_vertical = (eff_h > eff_w)

    if is_vertical:
        if phone_vertical == "blur":
            vf = (
                f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                f"crop={out_w}:{out_h},boxblur=25:5,eq=brightness=-0.12[bg];"
                f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={fps},format=yuv420p,setsar=1[v]"
            )
        else:
            vf = (
                f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,fps={fps},format=yuv420p,setsar=1[v]"
            )
    else:
        vf = (
            f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,fps={fps},format=yuv420p,setsar=1[v]"
        )

    fade_dur = min(0.02, slice_dur / 2.0)
    af_fade = (
        f"aformat=sample_rates=48000:channel_layouts=stereo,"
        f"afade=t=in:st=0:d={fade_dur:.3f},afade=t=out:st={slice_dur - fade_dur:.3f}:d={fade_dur:.3f}[a]"
    )

    cmd = ["ffmpeg", "-y", "-ss", f"{t0:.6f}", "-to", f"{t1:.6f}", "-i", src_video_path]

    use_cam_audio = (
        cam_audio_source is not None
        and cam_audio_t0 is not None
        and cam_audio_t1 is not None
        and os.path.exists(cam_audio_source)
        and has_audio_stream(cam_audio_source)
    )

    if use_cam_audio:
        cam_t0_val = max(0.0, float(cam_audio_t0))
        cam_t1_val = max(cam_t0_val + 0.01, float(cam_audio_t1))
        cmd.extend(["-ss", f"{cam_t0_val:.6f}", "-to", f"{cam_t1_val:.6f}", "-i", cam_audio_source])
        filter_complex = f"{vf};[1:a]{af_fade}"
    else:
        if has_audio_stream(src_video_path):
            filter_complex = f"{vf};[0:a]{af_fade}"
        else:
            filter_complex = f"{vf};anullsrc=r=48000:cl=stereo:d={slice_dur:.6f},{af_fade}"

    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac",
        "-t", f"{slice_dur:.6f}",
        dst_video_path,
    ])
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return dst_video_path


def compress_silent_video(video_path, segments, factor, out_w=None, out_h=None, fps=None):
    """
    Compress silent segments of a 1x action-camera clip by factor using smooth frame blending (tblend/setpts).
    Spoken commentary segments (sp <= 1.0) stay at 1x, keeping FIT telemetry aligned.
    Returns (compressed_path, new_segments, was_compressed).
    """
    has_silent = any(sp > 1.0 for _, _, sp in segments)
    if not has_silent or factor <= 1.0:
        return video_path, segments, False

    src_w, src_h, src_fps = get_video_size_fps(video_path)
    target_fps = fps or src_fps or 30.0
    if abs(target_fps - 29.97) < 0.05 or abs(target_fps - 59.94) < 0.05:
        target_fps = 30.0
    else:
        target_fps = max(1.0, round(target_fps, 3))

    has_audio = has_audio_stream(video_path)
    tmp_out = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name

    vf_parts = []
    af_parts = []
    concat_inputs = []
    new_segments = []
    cur_comp_t = 0.0

    for i, (s, e, sp) in enumerate(segments):
        orig_dur = e - s
        if orig_dur <= 0.001:
            continue
        if sp > 1.0:
            f = float(sp)
            comp_dur = orig_dur / f
            vf_parts.append(
                f"[0:v]trim=start={s:.6f}:end={e:.6f},setpts=PTS-STARTPTS,"
                f"tblend=all_mode=average,setpts=PTS/{f:.6f},fps={target_fps}[v{i}]"
            )
            af_parts.append(f"anullsrc=r=48000:cl=stereo:d={comp_dur:.6f}[a{i}]")
            new_segments.append((cur_comp_t, cur_comp_t + comp_dur, f))
            cur_comp_t += comp_dur
        else:
            comp_dur = orig_dur
            vf_parts.append(
                f"[0:v]trim=start={s:.6f}:end={e:.6f},setpts=PTS-STARTPTS,fps={target_fps}[v{i}]"
            )
            if has_audio:
                af_parts.append(
                    f"[0:a]atrim=start={s:.6f}:end={e:.6f},asetpts=PTS-STARTPTS,"
                    f"aformat=sample_rates=48000:channel_layouts=stereo[a{i}]"
                )
            else:
                af_parts.append(f"anullsrc=r=48000:cl=stereo:d={comp_dur:.6f}[a{i}]")
            new_segments.append((cur_comp_t, cur_comp_t + comp_dur, 1.0))
            cur_comp_t += comp_dur
        concat_inputs.append(f"[v{i}][a{i}]")

    if not concat_inputs:
        return video_path, segments, False

    n_segs = len(concat_inputs)
    concat_str = "".join(concat_inputs) + f"concat=n={n_segs}:v=1:a=1[outv][outa]"
    all_filters = ";".join(vf_parts + af_parts) + ";" + concat_str

    print(f"Compressing silent 1x sections ({n_segs} segments) by {factor:g}x with smooth frame blending (tblend/setpts)...")
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-filter_complex", all_filters,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac",
        tmp_out,
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    print(f"Compressed 1x video: {get_media_duration(video_path):.1f}s -> {cur_comp_t:.1f}s")
    return tmp_out, new_segments, True


# ============================================================
# Hyperlapse audio detection
# ============================================================
def detect_speed_segments(video_path, threshold_db=None, min_segment_s=1.0, factor=15.0):
    if is_phone_video(video_path):
        dur = get_media_duration(video_path)
        print(f"Phone video ({os.path.basename(video_path)}): locked to 1x speed ({dur:.2f}s)")
        return [(0.0, dur, 1.0)]
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
# Dive immersion detection (depth timeline — Amazfit multi-dive)
# ============================================================
# Amazfit auto start ~1.2 m / end ~0.9 m.
# One continuous scuba dive must not be split by brief shallow noise.
DIVE_START_DEPTH_M = 1.2
DIVE_END_DEPTH_M = 0.9
DIVE_MIN_DURATION_S = 30.0          # ignore short dips
DIVE_START_HOLD_S = 5.0             # time deeper than start threshold to begin
DIVE_END_HOLD_S = 60.0              # must stay shallow this long to end a dive
DIVE_MERGE_SURFACE_GAP_S = 600.0    # surface < 10 min → still same dive


def detect_dive_immersions(
    records,
    start_time,
    start_depth_m=DIVE_START_DEPTH_M,
    end_depth_m=DIVE_END_DEPTH_M,
    min_duration_s=DIVE_MIN_DURATION_S,
    start_hold_s=DIVE_START_HOLD_S,
    end_hold_s=DIVE_END_HOLD_S,
    merge_surface_gap_s=DIVE_MERGE_SURFACE_GAP_S,
):
    """
    Split a multi-dive session into immersions from the depth series.
    Returns list of dicts: num (1-based), start_s, end_s, max_depth
    (times are seconds from FIT start_time).

    Not per-video: always the full FIT timeline.
    Robustness for one long scuba dive:
      - missing depth forward-filled (not treated as 0 m)
      - start after start_hold_s deep
      - end only after end_hold_s continuous shallow (default 60 s)
      - merge immersions with surface gap <= merge_surface_gap_s (default 10 min)
    """
    times = []
    depths_raw = []
    for r in records:
        t = (r["timestamp"] - start_time).total_seconds()
        d = r.get("depth")
        times.append(t)
        try:
            depths_raw.append(float(d) if d is not None else None)
        except (TypeError, ValueError):
            depths_raw.append(None)

    if not times:
        return []

    # Forward-fill missing depth so gaps do not look like the surface
    depths = []
    last = 0.0
    for d in depths_raw:
        if d is not None and not (isinstance(d, float) and np.isnan(d)):
            last = d
        depths.append(last)

    immersions = []
    in_dive = False
    seg_start = 0.0
    max_d = 0.0
    pending_start_t = None
    pending_end_t = None

    for t, dep in zip(times, depths):
        if not in_dive:
            if dep >= start_depth_m:
                if pending_start_t is None:
                    pending_start_t = t
                elif t - pending_start_t >= start_hold_s:
                    in_dive = True
                    seg_start = pending_start_t
                    max_d = dep
                    pending_start_t = None
                    pending_end_t = None
            else:
                pending_start_t = None
        else:
            if dep > max_d:
                max_d = dep
            if dep <= end_depth_m:
                if pending_end_t is None:
                    pending_end_t = t
                elif t - pending_end_t >= end_hold_s:
                    end_t = pending_end_t
                    dur = end_t - seg_start
                    if dur >= min_duration_s:
                        immersions.append({
                            "num": len(immersions) + 1,
                            "start_s": seg_start,
                            "end_s": end_t,
                            "max_depth": max_d,
                        })
                    in_dive = False
                    pending_end_t = None
                    pending_start_t = None
            else:
                pending_end_t = None  # went deep again — cancel surface

    if in_dive and times:
        end_t = times[-1]
        if end_t - seg_start >= min_duration_s:
            immersions.append({
                "num": len(immersions) + 1,
                "start_s": seg_start,
                "end_s": end_t,
                "max_depth": max_d,
            })

    # Merge immersions separated by a short surface interval
    if len(immersions) >= 2 and merge_surface_gap_s > 0:
        merged = [dict(immersions[0])]
        for im in immersions[1:]:
            prev = merged[-1]
            gap = im["start_s"] - prev["end_s"]
            if gap <= merge_surface_gap_s:
                prev["end_s"] = im["end_s"]
                prev["max_depth"] = max(prev["max_depth"], im["max_depth"])
            else:
                merged.append(dict(im))
        for i, im in enumerate(merged, 1):
            im["num"] = i
        immersions = merged

    return immersions


def _dive_state_at(immersions, real_t):
    """
    Return (dive_num, dive_time_s, dive_max_depth) at real_t.
    While underwater: live dive time. On surface: hide dive time (nan),
    keep last dive number for context.
    """
    if not immersions:
        return np.nan, np.nan, np.nan
    for im in immersions:
        if im["start_s"] <= real_t <= im["end_s"]:
            return float(im["num"]), real_t - im["start_s"], im["max_depth"]
    last = None
    for im in immersions:
        if im["end_s"] < real_t:
            last = im
        elif im["start_s"] > real_t:
            break
    if last is not None:
        return float(last["num"]), np.nan, last["max_depth"]
    return np.nan, np.nan, np.nan


# ============================================================
# Precompute / lookup helpers
# ============================================================
def _build_record_arrays(records, start_time, immersions=None):
    """Arrays aligned to FIT records, including dive fields and per-dive max depth."""
    if immersions is None:
        immersions = detect_dive_immersions(records, start_time)

    rec_times = np.array([(r["timestamp"] - start_time).total_seconds() for r in records])
    rec_speed = np.array([_rec_float(r, "speed") for r in records])
    rec_hr = np.array([_rec_float(r, "heart_rate") for r in records])
    rec_elev = np.array([
        _rec_float(r, "enhanced_altitude", "altitude") for r in records
    ])
    rec_dist = np.array([_rec_float(r, "distance") for r in records])
    rec_depth = np.array([_rec_float(r, "depth") for r in records])
    rec_temp = np.array([_rec_float(r, "temperature") for r in records])
    rec_ndl = np.array([_rec_float(r, "ndl_time", "ndl") for r in records])
    rec_tts = np.array([_rec_float(r, "time_to_surface") for r in records])
    rec_ascent = np.array([_rec_float(r, "ascent_rate") for r in records])
    rec_cns = np.array([_rec_float(r, "cns_load") for r in records])
    rec_ts = [r["timestamp"] for r in records]

    n = len(records)
    rec_max_depth = np.full(n, np.nan)
    rec_dive_num = np.full(n, np.nan)
    rec_dive_time = np.full(n, np.nan)

    # Session running max (fallback)
    session_running = np.nan
    for i, d in enumerate(rec_depth):
        if not np.isnan(d):
            session_running = d if np.isnan(session_running) else max(session_running, d)
        real_t = rec_times[i]
        dnum, dtime, dmax = _dive_state_at(immersions, real_t)
        rec_dive_num[i] = dnum
        rec_dive_time[i] = dtime
        # Prefer current/last dive max; else session running max
        if dmax is not None and not (isinstance(dmax, float) and np.isnan(dmax)):
            rec_max_depth[i] = dmax
        else:
            rec_max_depth[i] = session_running

    return {
        "times": rec_times,
        "speed": rec_speed,
        "hr": rec_hr,
        "elev": rec_elev,
        "dist": rec_dist,
        "depth": rec_depth,
        "max_depth": rec_max_depth,
        "temp": rec_temp,
        "ndl": rec_ndl,
        "tts": rec_tts,
        "ascent": rec_ascent,
        "cns": rec_cns,
        "dive_num": rec_dive_num,
        "dive_time": rec_dive_time,
        "ts": rec_ts,
        "immersions": immersions,
    }


def precompute_data(records, start_time, gps_points, video_to_real, duration, extra_offset, step=0.20):
    print("Precomputing dashboard data...")
    times = np.arange(0, duration + step, step)
    n = len(times)
    arrays = {k: np.full(n, np.nan) for k in (
        "speed", "hr", "elev", "dist", "lat", "lon",
        "depth", "max_depth", "temp", "ndl", "tts", "ascent", "cns",
        "dive_num", "dive_time",
    )}
    clock = []

    rec = _build_record_arrays(records, start_time)
    immersions = rec.get("immersions") or []
    if immersions:
        print(f"Dive immersions detected: {len(immersions)}")
        for im in immersions:
            print(
                f"  #{im['num']}  {im['start_s']:.1f}→{im['end_s']:.1f}s  "
                f"({im['end_s'] - im['start_s']:.0f}s)  max={im['max_depth']:.1f}m"
            )
    gps_times = np.array([(p[2] - start_time).total_seconds() for p in gps_points]) if gps_points else np.array([])
    gps_lat = np.array([p[0] for p in gps_points]) if gps_points else np.array([])
    gps_lon = np.array([p[1] for p in gps_points]) if gps_points else np.array([])

    for i, t in enumerate(times):
        real_t = video_to_real(t) + extra_offset
        idx = int(np.argmin(np.abs(rec["times"] - real_t)))
        for key in (
            "speed", "hr", "elev", "dist", "depth", "max_depth", "temp",
            "ndl", "tts", "ascent", "cns", "dive_num", "dive_time",
        ):
            arrays[key][i] = rec[key][idx]
        clock.append(rec["ts"][idx])
        if len(gps_times) > 0:
            gidx = int(np.argmin(np.abs(gps_times - real_t)))
            arrays["lat"][i] = gps_lat[gidx]
            arrays["lon"][i] = gps_lon[gidx]

    print(f"Precomputed {n} points\n")
    return times, arrays, clock


def lookup_at_real_t(records, start_time, gps_points, real_t):
    rec = _build_record_arrays(records, start_time)
    idx = int(np.argmin(np.abs(rec["times"] - real_t)))
    lat = lon = np.nan
    if gps_points:
        gps_times = np.array([(p[2] - start_time).total_seconds() for p in gps_points])
        gidx = int(np.argmin(np.abs(gps_times - real_t)))
        lat, lon = gps_points[gidx][0], gps_points[gidx][1]
    return {
        "speed": rec["speed"][idx],
        "hr": rec["hr"][idx],
        "elev": rec["elev"][idx],
        "dist": rec["dist"][idx],
        "depth": rec["depth"][idx],
        "max_depth": rec["max_depth"][idx],
        "temp": rec["temp"][idx],
        "ndl": rec["ndl"][idx],
        "tts": rec["tts"][idx],
        "ascent": rec["ascent"][idx],
        "cns": rec["cns"][idx],
        "dive_num": rec["dive_num"][idx],
        "dive_time": rec["dive_time"][idx],
        "lat": lat,
        "lon": lon,
        "ts": rec["ts"][idx],
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


def _fmt_row_value(value, fmt="auto"):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if fmt == "int":
        return str(int(round(value)))
    if fmt == "time_s":
        # NDL / TTS as mm:ss when >= 60s, else seconds
        s = int(round(value))
        if s >= 60:
            return f"{s // 60}:{s % 60:02d}"
        return str(s)
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def make_overlay_rgba_values(data, gps_points, w, h, fonts, offset_hours, sport="outdoor"):
    """
    data: dict with keys speed, hr, elev, dist, depth, max_depth, temp,
          ndl, tts, ascent, cns, lat, lon, ts
    sport: 'outdoor' | 'dive'
    """
    font_label, font_value, font_time = fonts
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    time_str = format_display_time(data["ts"], offset_hours)
    lat = data.get("lat", np.nan)
    lon = data.get("lon", np.nan)

    # Build visible rows for this sport (skip missing values)
    rows = []
    if sport == "dive":
        depth = data.get("depth", np.nan)
        max_d = data.get("max_depth", np.nan)
        hr = data.get("hr", np.nan)
        temp = data.get("temp", np.nan)
        ndl = data.get("ndl", np.nan)
        tts = data.get("tts", np.nan)
        ascent = data.get("ascent", np.nan)
        cns = data.get("cns", np.nan)
        dive_num = data.get("dive_num", np.nan)
        dive_time = data.get("dive_time", np.nan)
        if not (isinstance(dive_num, float) and np.isnan(dive_num)):
            rows.append(("DIVE", _fmt_row_value(dive_num, "int"), "", (180, 220, 255, 255)))
        if not (isinstance(dive_time, float) and np.isnan(dive_time)):
            rows.append(
                ("DIVE TIME", _fmt_row_value(dive_time, "time_s"),
                 "min" if dive_time >= 60 else "s", (200, 230, 255, 255))
            )
        if not (isinstance(depth, float) and np.isnan(depth)):
            rows.append(("DEPTH", _fmt_row_value(depth), "m", (120, 200, 255, 255)))
        if not (isinstance(max_d, float) and np.isnan(max_d)):
            rows.append(("MAX DEPTH", _fmt_row_value(max_d), "m", (80, 180, 255, 255)))
        if not (isinstance(hr, float) and np.isnan(hr)):
            rows.append(("HEART RATE", _fmt_row_value(hr, "int"), "bpm", (255, 120, 120, 255)))
        if not (isinstance(temp, float) and np.isnan(temp)):
            rows.append(("TEMP", _fmt_row_value(temp), "°C", (140, 255, 180, 255)))
        if not (isinstance(ndl, float) and np.isnan(ndl)):
            rows.append(("NDL", _fmt_row_value(ndl, "time_s"), "min" if ndl >= 60 else "s", (255, 210, 120, 255)))
        if not (isinstance(tts, float) and np.isnan(tts)):
            rows.append(("TTS", _fmt_row_value(tts, "time_s"), "min" if tts >= 60 else "s", (255, 180, 100, 255)))
        if not (isinstance(ascent, float) and np.isnan(ascent)):
            # FIT often m/s; show m/min when small
            if abs(ascent) < 2:
                rows.append(("ASCENT", _fmt_row_value(ascent * 60), "m/min", (200, 160, 255, 255)))
            else:
                rows.append(("ASCENT", _fmt_row_value(ascent), "m/s", (200, 160, 255, 255)))
        if not (isinstance(cns, float) and np.isnan(cns)):
            rows.append(("CNS", _fmt_row_value(cns, "int"), "%", (255, 140, 180, 255)))
    else:
        speed = data.get("speed", np.nan)
        hr = data.get("hr", np.nan)
        elev = data.get("elev", np.nan)
        dist = data.get("dist", np.nan)
        speed_kmh = speed * 3.6 if not (isinstance(speed, float) and np.isnan(speed)) else None
        dist_km = dist / 1000 if not (isinstance(dist, float) and np.isnan(dist)) else None
        rows.append(("SPEED", _fmt_row_value(speed_kmh) if speed_kmh is not None else None, "km/h", (120, 200, 255, 255)))
        rows.append(("HEART RATE", _fmt_row_value(hr, "int") if not (isinstance(hr, float) and np.isnan(hr)) else None, "bpm", (255, 120, 120, 255)))
        rows.append(("ELEVATION", _fmt_row_value(elev) if not (isinstance(elev, float) and np.isnan(elev)) else None, "m", (140, 255, 180, 255)))
        rows.append(("DISTANCE", _fmt_row_value(dist_km) if dist_km is not None else None, "km", (255, 210, 120, 255)))

    scale = max(1.0, w / 1920.0)
    margin = int(24 * scale)
    box_w = int(320 * scale)
    # Dynamic height: TIME block + each row + padding
    box_h = int((12 + 74 + len(rows) * 72 + 20) * scale)
    box_h = max(box_h, int(200 * scale))
    x0 = margin
    y0 = h - box_h - margin

    draw.rectangle([x0, y0, x0 + box_w, y0 + box_h], fill=(0, 0, 0, 185))
    draw.rectangle([x0, y0, x0 + box_w, y0 + box_h], outline=(255, 255, 255, 45), width=1)

    y = y0 + int(12 * scale)

    def row(label, value, unit="", accent=(120, 200, 255, 255)):
        nonlocal y
        val = value if value is not None else "--"
        draw.text((x0 + int(18 * scale), y), label, font=font_label, fill=accent)
        draw.text(
            (x0 + int(18 * scale), y + int(16 * scale)),
            f"{val} {unit}".strip() if value is not None else "--",
            font=font_value,
            fill=(255, 255, 255, 255),
        )
        y += int(72 * scale)

    draw.text((x0 + int(18 * scale), y), "TIME", font=font_label, fill=(220, 220, 220, 255))
    draw.text(
        (x0 + int(18 * scale), y + int(16 * scale)),
        time_str,
        font=font_time,
        fill=(255, 255, 255, 255),
    )
    y += int(74 * scale)

    for label, value, unit, accent in rows:
        row(label, value, unit, accent)

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


def make_overlay_rgba(idx, arrays, clock, gps_points, w, h, fonts, offset_hours, sport="outdoor"):
    data = {
        "speed": arrays["speed"][idx],
        "hr": arrays["hr"][idx],
        "elev": arrays["elev"][idx],
        "dist": arrays["dist"][idx],
        "depth": arrays["depth"][idx],
        "max_depth": arrays["max_depth"][idx],
        "temp": arrays["temp"][idx],
        "ndl": arrays["ndl"][idx],
        "tts": arrays["tts"][idx],
        "ascent": arrays["ascent"][idx],
        "cns": arrays["cns"][idx],
        "dive_num": arrays["dive_num"][idx] if "dive_num" in arrays else np.nan,
        "dive_time": arrays["dive_time"][idx] if "dive_time" in arrays else np.nan,
        "lat": arrays["lat"][idx],
        "lon": arrays["lon"][idx],
        "ts": clock[idx],
    }
    return make_overlay_rgba_values(
        data, gps_points, w, h, fonts, offset_hours, sport=sport,
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
                   segments=None, fixed_offset=None, sport=None,
                   phone_vertical="blur", compress_silent=False,
                   cam_audio_source=None, cam_audio_t0=None, cam_audio_t1=None):
    """Render overlay for a full video or a [file_t_start, file_t_end) slice."""
    if records is None or start_time is None or gps_points is None:
        records, start_time, gps_points = parse_fit(fit_path)
    sport = detect_sport(records, forced=sport, fit_path=fit_path)
    print(f"Panel sport   : {sport}")

    is_phone = is_phone_video(video_path)
    tmp_cleaned = []

    try:
        if is_phone:
            if segments is None:
                segments = [(0.0, get_media_duration(video_path), 1.0)]
        elif compress_silent and segments is None:
            raw_segs = detect_speed_segments(video_path, factor=factor)
            comp_path, new_segs, was_comp = compress_silent_video(video_path, raw_segs, factor)
            if was_comp:
                video_path = comp_path
                segments = new_segs
                tmp_cleaned.append(comp_path)
            else:
                segments = raw_segs

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

        eff_w, eff_h, _ = get_effective_video_dimensions(video_path)
        is_vertical = (eff_h > eff_w)

        # In single-clip mode, if phone video is vertical, normalize it to 16:9 canvas
        if is_phone and is_vertical:
            target_w = 1920
            target_h = 1080
            tmp_norm = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
            tmp_cleaned.append(tmp_norm)
            fps_val = get_video_size_fps(video_path)[2] or 30.0
            print(f"Normalizing vertical phone clip to 16:9 with {phone_vertical} background...")
            normalize_phone_video_clip(
                video_path, tmp_norm, target_w, target_h, fps_val,
                phone_vertical=phone_vertical, t0=file_t_start, t1=file_t_end,
                cam_audio_source=cam_audio_source,
                cam_audio_t0=cam_audio_t0, cam_audio_t1=cam_audio_t1,
            )
            video_path = tmp_norm
            file_t_start = 0.0
            file_t_end = get_media_duration(tmp_norm)
            segments = [(0.0, file_t_end, 1.0)]
            video_to_real, real_to_video, _total_real = build_time_map(segments)

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

        times, arrays, clock = precompute_data(
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
                    idx, arrays, clock,
                    gps_points, out_w, out_h, fonts, offset_hours, sport=sport,
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

        print("Muxing audio with ffmpeg (20ms afade)...")
        dur = t1 - t0
        fade_dur = min(0.02, dur / 2.0)
        has_audio = has_audio_stream(video_path)
        if has_audio:
            af = (
                f"aformat=sample_rates=48000:channel_layouts=stereo,"
                f"afade=t=in:st=0:d={fade_dur:.3f},afade=t=out:st={dur - fade_dur:.3f}:d={fade_dur:.3f}"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", tmp_video,
                "-ss", f"{t0:.6f}",
                "-to", f"{t1:.6f}",
                "-i", video_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-af", af,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-c:a", "aac",
                "-shortest",
                output_path,
            ]
        else:
            af = f"afade=t=in:st=0:d={fade_dur:.3f},afade=t=out:st={dur - fade_dur:.3f}:d={fade_dur:.3f}"
            cmd = [
                "ffmpeg", "-y",
                "-i", tmp_video,
                "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={dur:.6f}",
                "-af", af,
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
    finally:
        for p in tmp_cleaned:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass



def create_photo_clip(photo_path, output_path, records, start_time, gps_points,
                      out_w, out_h, fps, photo_seconds, time_mode, utc_offset,
                      ref_video_for_tz=None, sport=None):
    # Resolve timezone first so EXIF local can be converted to UTC for FIT lookup
    offset_hours, offset_desc = resolve_display_offset_hours(
        ref_video_for_tz, time_mode, utc_offset
    )
    print(f"Display time   : {offset_desc}")
    sport = detect_sport(records, forced=sport, fit_path=None)

    real_t, photo_dt = photo_align_offset(photo_path, start_time, offset_hours=offset_hours)
    data = lookup_at_real_t(records, start_time, gps_points, real_t)

    fonts = load_fonts(out_w)
    base = fit_image_to_canvas(photo_path, out_w, out_h)
    overlay = make_overlay_rgba_values(
        data, gps_points, out_w, out_h, fonts, offset_hours, sport=sport,
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

    # silent audio track with 20ms fade so concat with video clips is happier
    dur_f = float(photo_seconds)
    fade_dur = min(0.02, dur_f / 2.0)
    af = f"afade=t=in:st=0:d={fade_dur:.3f},afade=t=out:st={dur_f - fade_dur:.3f}:d={fade_dur:.3f}"
    cmd = [
        "ffmpeg", "-y",
        "-i", tmp_video,
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={dur_f:.6f}",
        "-af", af,
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
                     time_mode, utc_offset,
                     phone_priority=True, phone_vertical="blur",
                     keep_camera_audio=False, compress_silent=False):
    """
    Build ordered render plan: video slices and photos.
    - Phone Video Priority: carves overlapping action-camera video intervals around phone videos
      on the FIT timeline (dropping camera fragments < 0.5s).
    - Phone videos locked to 1x speed.
    - Action-camera clips with --compress-silent compress silent segments by factor via tblend/setpts.
    - Mid-clip photos: stills falling inside carved slices split those slices.
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

    # Analyze and prepare video metadata
    video_meta = []
    for vpath in videos:
        print("=" * 60)
        print(f"Analyzing: {os.path.basename(vpath)}")
        print("=" * 60)
        is_phone = is_phone_video(vpath)

        cap = cv2.VideoCapture(vpath)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = frame_count / fps if fps > 0 else 0.0
        cap.release()

        effective_path = vpath
        if is_phone:
            print(f"  Phone video ({os.path.basename(vpath)}): locked to 1x speed ({duration:.2f}s)")
            segments = [(0.0, duration, 1.0)]
        else:
            if compress_silent:
                raw_segments = detect_speed_segments(vpath, factor=factor)
                comp_path, new_segments, was_comp = compress_silent_video(vpath, raw_segments, factor)
                effective_path = comp_path
                segments = new_segments
                duration = segments[-1][1] if segments else duration
            else:
                segments = detect_speed_segments(vpath, factor=factor)

        video_to_real, real_to_video, total_real = build_time_map(segments)

        if offset_explicit:
            align = float(extra_offset)
        elif auto_align:
            auto = compute_auto_align_offset(vpath, start_time)
            align = auto if auto is not None else 0.0
        else:
            align = float(extra_offset)

        v_real0 = align + video_to_real(0.0)
        v_real1 = align + video_to_real(duration)

        video_meta.append({
            "orig_path": vpath,
            "path": effective_path,
            "is_phone": is_phone,
            "duration": duration,
            "segments": segments,
            "video_to_real": video_to_real,
            "real_to_video": real_to_video,
            "align": align,
            "real_start": v_real0,
            "real_end": v_real1,
        })

    # Phone Video Priority & Interval Carving
    if phone_priority:
        phone_vids = [v for v in video_meta if v["is_phone"]]
        cam_vids = [v for v in video_meta if not v["is_phone"]]

        # Audio routing from overlapping action camera clip if keep_camera_audio
        if keep_camera_audio:
            for p in phone_vids:
                for c in cam_vids:
                    ovlp_start = max(p["real_start"], c["real_start"])
                    ovlp_end = min(p["real_end"], c["real_end"])
                    if ovlp_end - ovlp_start > 0.0:
                        cam_t0 = c["real_to_video"](p["real_start"] - c["align"])
                        cam_t1 = c["real_to_video"](p["real_end"] - c["align"])
                        p["cam_audio_source"] = c["path"]
                        p["cam_audio_t0"] = cam_t0
                        p["cam_audio_t1"] = cam_t1
                        print(f"Audio routing: {os.path.basename(p['orig_path'])} <- {os.path.basename(c['orig_path'])} ({cam_t0:.2f}s..{cam_t1:.2f}s)")
                        break

        # Carve each action-camera interval around phone intervals on FIT timeline
        carved_cam_slices = []
        for c in cam_vids:
            intervals = [(c["real_start"], c["real_end"])]
            for p in phone_vids:
                p0, p1 = p["real_start"], p["real_end"]
                next_intervals = []
                for a0, a1 in intervals:
                    if p1 <= a0 or p0 >= a1:
                        next_intervals.append((a0, a1))
                    else:
                        if a0 < p0:
                            next_intervals.append((a0, p0))
                        if p1 < a1:
                            next_intervals.append((p1, a1))
                intervals = next_intervals

            for r0, r1 in intervals:
                t0 = c["real_to_video"](r0 - c["align"])
                t1 = c["real_to_video"](r1 - c["align"])
                t0 = max(0.0, min(c["duration"], float(t0)))
                t1 = max(0.0, min(c["duration"], float(t1)))
                frag_dur = t1 - t0
                if frag_dur >= 0.5:
                    carved_cam_slices.append({
                        "orig_path": c["orig_path"],
                        "path": c["path"],
                        "is_phone": False,
                        "t0": t0,
                        "t1": t1,
                        "align": c["align"],
                        "segments": c["segments"],
                        "video_to_real": c["video_to_real"],
                        "real_to_video": c["real_to_video"],
                        "real_start": r0,
                        "real_end": r1,
                    })
                else:
                    print(f"Dropped short camera fragment ({frag_dur:.2f}s < 0.5s) from {os.path.basename(c['orig_path'])}")

        base_slices = []
        for p in phone_vids:
            base_slices.append({
                "orig_path": p["orig_path"],
                "path": p["path"],
                "is_phone": True,
                "t0": 0.0,
                "t1": p["duration"],
                "align": p["align"],
                "segments": p["segments"],
                "video_to_real": p["video_to_real"],
                "real_to_video": p["real_to_video"],
                "real_start": p["real_start"],
                "real_end": p["real_end"],
                "cam_audio_source": p.get("cam_audio_source"),
                "cam_audio_t0": p.get("cam_audio_t0"),
                "cam_audio_t1": p.get("cam_audio_t1"),
            })
        base_slices.extend(carved_cam_slices)
    else:
        base_slices = []
        for v in video_meta:
            base_slices.append({
                "orig_path": v["orig_path"],
                "path": v["path"],
                "is_phone": v["is_phone"],
                "t0": 0.0,
                "t1": v["duration"],
                "align": v["align"],
                "segments": v["segments"],
                "video_to_real": v["video_to_real"],
                "real_to_video": v["real_to_video"],
                "real_start": v["real_start"],
                "real_end": v["real_end"],
            })

    # Mid-clip photo splitting
    used_photos = set()
    plan = []

    for s in base_slices:
        v_real0 = s["real_start"]
        v_real1 = s["real_end"]
        t0 = s["t0"]
        t1 = s["t1"]
        align = s["align"]
        real_to_video = s["real_to_video"]
        video_to_real = s["video_to_real"]

        inside = [
            p for p in photo_meta
            if p["path"] not in used_photos and v_real0 < p["real_t"] < v_real1
        ]
        inside.sort(key=lambda p: p["real_t"])

        cuts = [round(t0, 3)]
        photos_after_cut = {}
        for p in inside:
            vt = real_to_video(p["real_t"] - align)
            vt = max(t0, min(t1, float(vt)))
            key = round(vt, 2)
            cuts.append(key)
            photos_after_cut.setdefault(key, []).append(p)
            used_photos.add(p["path"])
        cuts.append(round(t1, 3))
        cuts = sorted(set(cuts))

        for i in range(len(cuts) - 1):
            a, b = cuts[i], cuts[i + 1]
            if b - a >= 0.08:
                sort_key = align + video_to_real(a)
                item = {
                    "kind": "video_slice",
                    "orig_path": s["orig_path"],
                    "path": s["path"],
                    "is_phone": s["is_phone"],
                    "t0": a,
                    "t1": b,
                    "align": align,
                    "segments": s["segments"],
                    "sort_key": sort_key,
                }
                if s.get("cam_audio_source"):
                    offset_in_phone = a - t0
                    slice_dur = b - a
                    cam_base_t0 = s["cam_audio_t0"]
                    item["cam_audio_source"] = s["cam_audio_source"]
                    item["cam_audio_t0"] = cam_base_t0 + offset_in_phone
                    item["cam_audio_t1"] = cam_base_t0 + offset_in_phone + slice_dur
                plan.append(item)

            for p in photos_after_cut.get(cuts[i + 1], []):
                plan.append({
                    "kind": "photo",
                    "path": p["path"],
                    "sort_key": p["real_t"],
                })

    for p in photo_meta:
        if p["path"] not in used_photos:
            plan.append({
                "kind": "photo",
                "path": p["path"],
                "sort_key": p["real_t"],
            })
            used_photos.add(p["path"])

    plan.sort(key=lambda e: (e["sort_key"], 0 if e["kind"] == "photo" else 1, e["path"]))

    print("\nFinal render plan:")
    for i, e in enumerate(plan, 1):
        if e["kind"] == "video_slice":
            tag = ""
            if e.get("is_phone"):
                cam_note = " (+cam_audio)" if e.get("cam_audio_source") and keep_camera_audio else ""
                tag = f"  PHONE [PRIORITY]{cam_note}"
            print(f"  {i}. [video] {os.path.basename(e['orig_path'])}  {e['t0']:.2f}-{e['t1']:.2f}s{tag}")
        else:
            print(f"  {i}. [photo] {os.path.basename(e['path'])}")
    print()
    return plan, offset_hours


def run_batch(folder, output_path, extra_offset=0.0, step=0.20, preview=False,
              time_mode="auto", utc_offset=None, auto_align=True, offset_explicit=False,
              factor=15.0, photo_seconds=3.0, transition_seconds=0.0, sport=None,
              phone_priority=True, phone_vertical="blur",
              keep_camera_audio=False, compress_silent=False):
    fit_path, videos, photos = discover_batch(folder)
    records, start_time, gps_points = parse_fit(fit_path)
    sport = detect_sport(records, forced=sport, fit_path=fit_path)
    print(f"Panel sport   : {sport}")

    plan, _offset_hours = build_batch_plan(
        videos, photos, fit_path, records, start_time, gps_points,
        extra_offset, auto_align, offset_explicit, factor,
        time_mode, utc_offset,
        phone_priority=phone_priority, phone_vertical=phone_vertical,
        keep_camera_audio=keep_camera_audio, compress_silent=compress_silent,
    )
    if not plan:
        raise RuntimeError("Batch plan is empty")

    # Target resolution: prefer landscape action camera video dimensions
    cam_vids = [e["path"] for e in plan if e["kind"] == "video_slice" and not e.get("is_phone")]
    all_vids = [e["path"] for e in plan if e["kind"] == "video_slice"]
    ref_vid = cam_vids[0] if cam_vids else (all_vids[0] if all_vids else None)

    if ref_vid:
        w, h, _ = get_effective_video_dimensions(ref_vid)
        _, _, fps = get_video_size_fps(ref_vid)
        if w < h:
            out_w, out_h = 1920, 1080
        else:
            out_w, out_h = w, h
    else:
        out_w, out_h, fps = 1920, 1080, 30.0

    if abs(fps - 29.97) < 0.05 or abs(fps - 59.94) < 0.05:
        fps = 30.0
    else:
        fps = max(1.0, round(fps, 3))

    if preview:
        out_h = 1080
        out_w = int(out_w * (1080.0 / out_h))
        if out_w % 2:
            out_w += 1
        out_h = 1080
    else:
        if out_w % 2:
            out_w += 1
        if out_h % 2:
            out_h += 1

    ref_video = videos[0] if videos else None
    tmp_dir = tempfile.mkdtemp(prefix="overlay_batch_")
    clip_outputs = []

    try:
        for i, e in enumerate(plan, 1):
            print("=" * 60)
            if e["kind"] == "video_slice":
                tag = ""
                if e.get("is_phone"):
                    cam_note = " (+cam_audio)" if e.get("cam_audio_source") and keep_camera_audio else ""
                    tag = f" PHONE [PRIORITY]{cam_note}"
                print(f"Batch item {i}/{len(plan)}: [video] {os.path.basename(e['orig_path'])} "
                      f"{e['t0']:.2f}-{e['t1']:.2f}s{tag}")
            else:
                print(f"Batch item {i}/{len(plan)}: [photo] {os.path.basename(e['path'])}")
            print("=" * 60)
            out_clip = os.path.join(tmp_dir, f"clip_{i:03d}.mp4")

            if e["kind"] == "video_slice":
                if e.get("is_phone"):
                    norm_slice = os.path.join(tmp_dir, f"norm_phone_{i:03d}.mp4")
                    normalize_phone_video_clip(
                        src_video_path=e["orig_path"],
                        dst_video_path=norm_slice,
                        out_w=out_w, out_h=out_h, fps=fps,
                        phone_vertical=phone_vertical,
                        t0=e["t0"], t1=e["t1"],
                        cam_audio_source=e.get("cam_audio_source") if keep_camera_audio else None,
                        cam_audio_t0=e.get("cam_audio_t0"),
                        cam_audio_t1=e.get("cam_audio_t1"),
                    )
                    slice_dur = e["t1"] - e["t0"]
                    create_overlay(
                        norm_slice, fit_path, output_path=out_clip,
                        extra_offset=extra_offset, step=step, preview=False,
                        time_mode=time_mode, utc_offset=utc_offset,
                        auto_align=False, offset_explicit=False,
                        records=records, start_time=start_time, gps_points=gps_points,
                        factor=1.0,
                        file_t_start=0.0, file_t_end=slice_dur,
                        segments=[(0.0, slice_dur, 1.0)], fixed_offset=e["align"] + e["t0"],
                        sport=sport,
                    )
                    if os.path.exists(norm_slice):
                        os.remove(norm_slice)
                else:
                    create_overlay(
                        e["path"], fit_path, output_path=out_clip,
                        extra_offset=extra_offset, step=step, preview=preview,
                        time_mode=time_mode, utc_offset=utc_offset,
                        auto_align=False, offset_explicit=False,
                        records=records, start_time=start_time, gps_points=gps_points,
                        factor=factor,
                        file_t_start=e["t0"], file_t_end=e["t1"],
                        segments=e["segments"], fixed_offset=e["align"],
                        sport=sport,
                    )
            else:
                create_photo_clip(
                    e["path"], out_clip, records, start_time, gps_points,
                    out_w, out_h, fps, photo_seconds, time_mode, utc_offset,
                    ref_video_for_tz=ref_video, sport=sport,
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
    p.add_argument("--sport", choices=("auto", "outdoor", "dive", "freedive", "scuba"),
                   default="auto",
                   help="Panel field set: auto detects dive from FIT depth samples")
    p.add_argument("--phone-priority", dest="phone_priority", action="store_true", default=True,
                   help="Prioritize phone videos and carve overlapping camera intervals (default: on)")
    p.add_argument("--no-phone-priority", dest="phone_priority", action="store_false",
                   help="Disable phone video priority and interval carving")
    p.add_argument("--phone-vertical", choices=("blur", "black"), default="blur",
                   help="Background for vertical (9:16) phone videos: blur or black (default: blur)")
    p.add_argument("--keep-camera-audio", action="store_true", default=False,
                   help="Route action-camera audio onto overlapping phone clips (default: off)")
    p.add_argument("--compress-silent", action="store_true", default=False,
                   help="Compress silent segments in 1x action-camera clips by --factor using tblend/setpts (default: off)")
    args = p.parse_args()

    offset_explicit = args.offset is not None
    extra_offset = 0.0 if args.offset is None else args.offset
    sport = None if args.sport == "auto" else args.sport

    if args.batch:
        output = args.output or "workout_batch.mp4"
        run_batch(
            args.batch, output_path=output,
            extra_offset=extra_offset, step=args.step, preview=args.preview,
            time_mode=args.time, utc_offset=args.utc_offset,
            auto_align=args.auto_align, offset_explicit=offset_explicit,
            factor=args.factor, photo_seconds=args.photo_seconds,
            transition_seconds=args.transition_seconds, sport=sport,
            phone_priority=args.phone_priority, phone_vertical=args.phone_vertical,
            keep_camera_audio=args.keep_camera_audio, compress_silent=args.compress_silent,
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
        factor=args.factor, sport=sport,
        phone_vertical=args.phone_vertical, compress_silent=args.compress_silent,
    )


if __name__ == "__main__":
    main()
