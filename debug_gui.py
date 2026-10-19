#!/usr/bin/env python3
"""
Debug GUI for Workout Overlay (CustomTkinter + OpenCV).

  python debug_gui.py
  python debug_gui.py /path/to/batch_folder

Shows folder media, hyperlapse segments, align offset, and a small preview
with file time, detected factor (1x/15x), mapped FIT time, and sample values.
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import cv2
import numpy as np
from PIL import Image, ImageTk

try:
    import customtkinter as ctk
except ImportError:
    print("Install CustomTkinter:  pip install customtkinter")
    sys.exit(1)

import workout_overlay as wo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def factor_at(segments, t: float) -> float:
    if not segments:
        return 1.0
    for start, end, speed in segments:
        if start <= t < end or (t == end and end == segments[-1][1]):
            return float(speed)
    if t <= segments[0][0]:
        return float(segments[0][2])
    return float(segments[-1][2])


def fmt_hms(seconds: float | None) -> str:
    if seconds is None or (isinstance(seconds, float) and np.isnan(seconds)):
        return "--"
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def fmt_ts(ts) -> str:
    if ts is None:
        return "--"
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    return str(ts)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class DebugApp(ctk.CTk):
    PREVIEW_W = 640
    PREVIEW_H = 360

    def __init__(self, initial_folder: str | None = None):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Workout Overlay — Debug")
        self.geometry("1100x780")
        self.minsize(900, 640)

        # State
        self.folder: str | None = None
        self.fit_path: str | None = None
        self.videos: list[str] = []
        self.photos: list[str] = []
        self.records = None
        self.start_time = None
        self.gps_points = None
        self.sport = "outdoor"
        self.factor = 15.0
        self.manual_offset = 0.0

        self.current_path: str | None = None
        self.kind = "video"  # video | photo
        self.segments: list = []
        self.video_to_real = None
        self.align_offset = 0.0
        self.align_note = ""
        self.threshold_db: float | None = None

        self.cap: cv2.VideoCapture | None = None
        self.fps = 30.0
        self.duration = 0.0
        self.playing = False
        self.play_job = None
        self._photo_image = None  # keep ref

        self._build_ui()
        if initial_folder and os.path.isdir(initial_folder):
            self.after(100, lambda: self.load_folder(initial_folder))

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=10, pady=8)

        ctk.CTkButton(top, text="Pick folder…", width=120, command=self.pick_folder).pack(
            side="left", padx=(0, 8)
        )
        self.folder_label = ctk.CTkLabel(top, text="No folder", anchor="w")
        self.folder_label.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(top, text="Factor").pack(side="left", padx=(12, 4))
        self.factor_var = ctk.StringVar(value="15")
        self.factor_menu = ctk.CTkOptionMenu(
            top, variable=self.factor_var, values=["1", "5", "10", "15", "30"],
            width=70, command=self.on_factor_change,
        )
        self.factor_menu.pack(side="left")

        ctk.CTkLabel(top, text="Sport").pack(side="left", padx=(12, 4))
        self.sport_var = ctk.StringVar(value="auto")
        self.sport_menu = ctk.CTkOptionMenu(
            top, variable=self.sport_var,
            values=["auto", "outdoor", "dive", "freedive", "scuba"],
            width=100, command=self.on_sport_change,
        )
        self.sport_menu.pack(side="left", padx=(0, 8))

        body = ctk.CTkFrame(self)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        # Left: media list
        left = ctk.CTkFrame(body, width=260)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)
        ctk.CTkLabel(left, text="Media", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.media_list = ctk.CTkScrollableFrame(left, width=240)
        self.media_list.pack(fill="both", expand=True, padx=4, pady=4)
        self._media_buttons: list = []

        # Center: preview + transport
        center = ctk.CTkFrame(body)
        center.pack(side="left", fill="both", expand=True)

        self.preview_label = ctk.CTkLabel(
            center, text="No clip", width=self.PREVIEW_W, height=self.PREVIEW_H
        )
        self.preview_label.pack(padx=8, pady=8)

        transport = ctk.CTkFrame(center)
        transport.pack(fill="x", padx=8, pady=(0, 4))
        ctk.CTkButton(transport, text="▶", width=40, command=self.toggle_play).pack(
            side="left", padx=2
        )
        ctk.CTkButton(transport, text="⏹", width=40, command=self.stop_play).pack(
            side="left", padx=2
        )
        self.scrub = ctk.CTkSlider(
            transport, from_=0, to=1, number_of_steps=1000, command=self.on_scrub
        )
        self.scrub.pack(side="left", fill="x", expand=True, padx=8)
        self.time_label = ctk.CTkLabel(transport, text="00:00 / 00:00", width=110)
        self.time_label.pack(side="left")

        # Debug readout
        info = ctk.CTkFrame(center)
        info.pack(fill="x", padx=8, pady=4)
        self.info_vars = {
            "file_t": ctk.StringVar(value="File time: --"),
            "factor": ctk.StringVar(value="Factor: --"),
            "real_t": ctk.StringVar(value="Real (FIT) t: --"),
            "fit_clock": ctk.StringVar(value="FIT clock: --"),
            "align": ctk.StringVar(value="Align: --"),
            "sample": ctk.StringVar(value="Sample: --"),
            "sport": ctk.StringVar(value="Sport: --"),
        }
        for key in ("file_t", "factor", "real_t", "fit_clock", "align", "sample", "sport"):
            ctk.CTkLabel(info, textvariable=self.info_vars[key], anchor="w").pack(
                fill="x", padx=6, pady=1
            )

        btns = ctk.CTkFrame(center)
        btns.pack(fill="x", padx=8, pady=6)
        ctk.CTkButton(btns, text="Re-analyze segments", command=self.reanalyze).pack(
            side="left", padx=4
        )
        ctk.CTkButton(btns, text="Copy debug snapshot", command=self.copy_snapshot).pack(
            side="left", padx=4
        )
        ctk.CTkButton(btns, text="Build plan…", command=self.show_plan).pack(
            side="left", padx=4
        )

        # Right: segments + log
        right = ctk.CTkFrame(body, width=280)
        right.pack(side="left", fill="both", padx=(8, 0))
        right.pack_propagate(False)
        ctk.CTkLabel(right, text="Segments", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.seg_box = ctk.CTkTextbox(right, height=200, font=ctk.CTkFont(family="monospace", size=12))
        self.seg_box.pack(fill="x", padx=6, pady=4)
        ctk.CTkLabel(right, text="Log", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.log_box = ctk.CTkTextbox(right, font=ctk.CTkFont(family="monospace", size=11))
        self.log_box.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def log(self, msg: str):
        self.log_box.insert("end", msg.rstrip() + "\n")
        self.log_box.see("end")

    # ---------------------------------------------------------- folder / load
    def pick_folder(self):
        path = filedialog.askdirectory(title="Workout folder (one .fit + videos/photos)")
        if path:
            self.load_folder(path)

    def load_folder(self, path: str):
        self.stop_play()
        try:
            fit_path, videos, photos = wo.discover_batch(path)
            records, start_time, gps_points = wo.parse_fit(fit_path)
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            self.log(f"ERROR: {e}")
            return

        self.folder = path
        self.fit_path = fit_path
        self.videos = videos
        self.photos = photos
        self.records = records
        self.start_time = start_time
        self.gps_points = gps_points
        forced = None if self.sport_var.get() == "auto" else self.sport_var.get()
        self.sport = wo.detect_sport(records, forced=forced)

        self.folder_label.configure(text=f"{path}   |  {Path(fit_path).name}")
        self.info_vars["sport"].set(f"Sport: {self.sport}")
        self.log(f"Loaded folder: {path}")
        self.log(f"FIT: {Path(fit_path).name}  start={start_time}  points={len(records)}")
        self.log(f"Videos={len(videos)}  Photos={len(photos)}  sport={self.sport}")

        for w in self._media_buttons:
            w.destroy()
        self._media_buttons.clear()

        for v in videos:
            name = Path(v).name
            b = ctk.CTkButton(
                self.media_list, text=f"▶ {name}", anchor="w",
                command=lambda p=v: self.select_clip(p, "video"),
            )
            b.pack(fill="x", pady=2, padx=2)
            self._media_buttons.append(b)
        for ph in photos:
            name = Path(ph).name
            b = ctk.CTkButton(
                self.media_list, text=f"🖼 {name}", anchor="w",
                command=lambda p=ph: self.select_clip(p, "photo"),
            )
            b.pack(fill="x", pady=2, padx=2)
            self._media_buttons.append(b)

        if videos:
            self.select_clip(videos[0], "video")
        elif photos:
            self.select_clip(photos[0], "photo")

    def on_factor_change(self, _value=None):
        try:
            self.factor = float(self.factor_var.get())
        except ValueError:
            self.factor = 15.0
        if self.current_path and self.kind == "video":
            self.reanalyze()

    def on_sport_change(self, _value=None):
        if self.records is None:
            return
        forced = None if self.sport_var.get() == "auto" else self.sport_var.get()
        self.sport = wo.detect_sport(self.records, forced=forced)
        self.info_vars["sport"].set(f"Sport: {self.sport}")
        self.update_readout(self._current_file_t())

    # ---------------------------------------------------------- clip select
    def select_clip(self, path: str, kind: str):
        self.stop_play()
        self.current_path = path
        self.kind = kind
        self.log(f"Select [{kind}] {Path(path).name}")

        if self.cap is not None:
            self.cap.release()
            self.cap = None

        if kind == "photo":
            self.segments = [(0.0, 1.0, 1.0)]
            self.video_to_real, _, _ = wo.build_time_map(self.segments)
            self.duration = 1.0
            self.fps = 1.0
            self.align_offset = 0.0
            self.align_note = "photo (EXIF → FIT)"
            self._show_photo(path)
            self.scrub.configure(from_=0, to=1)
            self.scrub.set(0)
            self._update_segments_box()
            self.update_readout(0.0)
            return

        # Video
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            messagebox.showerror("Video", f"Cannot open {path}")
            return
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        n = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.duration = n / self.fps if self.fps > 0 else 0.0
        self.scrub.configure(from_=0, to=max(self.duration, 0.01))
        self.scrub.set(0)

        auto = wo.compute_auto_align_offset(path, self.start_time)
        if auto is not None:
            self.align_offset = float(auto)
            self.align_note = f"auto {self.align_offset:.1f}s"
        else:
            self.align_offset = self.manual_offset
            self.align_note = "no creation_time (offset 0 or manual)"

        self.log(f"  duration={self.duration:.2f}s fps={self.fps:.3f} align={self.align_note}")
        self.reanalyze()
        self.show_frame_at(0.0)

    def reanalyze(self):
        if not self.current_path or self.kind != "video":
            return
        self.log(f"Analyzing segments factor={self.factor:g} …")
        self.update_idletasks()

        def work():
            try:
                segs = wo.detect_speed_segments(self.current_path, factor=self.factor)
                v2r, _, total = wo.build_time_map(segs)
                self.after(0, lambda: self._apply_segments(segs, v2r, total))
            except Exception as e:
                self.after(0, lambda: self.log(f"Segment error: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _apply_segments(self, segs, v2r, total):
        self.segments = segs
        self.video_to_real = v2r
        self._update_segments_box()
        self.log(f"  segments={len(segs)}  real covered≈{total:.1f}s")
        self.update_readout(self._current_file_t())

    def _update_segments_box(self):
        self.seg_box.delete("1.0", "end")
        if not self.segments:
            self.seg_box.insert("end", "(none)\n")
            return
        for s, e, sp in self.segments:
            self.seg_box.insert("end", f"{s:7.1f} → {e:7.1f}s   {sp:g}x\n")

    # ---------------------------------------------------------- preview
    def _show_photo(self, path: str):
        img = Image.open(path).convert("RGB")
        img.thumbnail((self.PREVIEW_W, self.PREVIEW_H))
        canvas = Image.new("RGB", (self.PREVIEW_W, self.PREVIEW_H), (20, 20, 20))
        canvas.paste(img, ((self.PREVIEW_W - img.width) // 2, (self.PREVIEW_H - img.height) // 2))
        self._photo_image = ImageTk.PhotoImage(canvas)
        self.preview_label.configure(image=self._photo_image, text="")

    def show_frame_at(self, t: float):
        if self.cap is None or self.kind != "video":
            return
        t = max(0.0, min(t, max(self.duration - 1e-3, 0.0)))
        self.cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = self.cap.read()
        if not ok:
            return
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame.shape[:2]
        scale = min(self.PREVIEW_W / w, self.PREVIEW_H / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        img = Image.fromarray(frame)
        canvas = Image.new("RGB", (self.PREVIEW_W, self.PREVIEW_H), (20, 20, 20))
        canvas.paste(img, ((self.PREVIEW_W - nw) // 2, (self.PREVIEW_H - nh) // 2))
        self._photo_image = ImageTk.PhotoImage(canvas)
        self.preview_label.configure(image=self._photo_image, text="")
        self.time_label.configure(text=f"{fmt_hms(t)} / {fmt_hms(self.duration)}")
        self.update_readout(t)

    def _current_file_t(self) -> float:
        try:
            return float(self.scrub.get())
        except Exception:
            return 0.0

    def on_scrub(self, value):
        if self.playing:
            return
        t = float(value)
        if self.kind == "video":
            self.show_frame_at(t)
        else:
            self.update_readout(0.0)

    def toggle_play(self):
        if self.kind != "video" or self.cap is None:
            return
        if self.playing:
            self.playing = False
            if self.play_job:
                self.after_cancel(self.play_job)
                self.play_job = None
        else:
            self.playing = True
            self._play_tick()

    def stop_play(self):
        self.playing = False
        if self.play_job:
            self.after_cancel(self.play_job)
            self.play_job = None

    def _play_tick(self):
        if not self.playing or self.cap is None:
            return
        t = self._current_file_t() + (1.0 / max(self.fps, 1.0))
        if t >= self.duration:
            self.playing = False
            return
        self.scrub.set(t)
        self.show_frame_at(t)
        delay = max(1, int(1000 / max(self.fps, 1.0)))
        self.play_job = self.after(delay, self._play_tick)

    # ---------------------------------------------------------- readout
    def update_readout(self, file_t: float):
        self.info_vars["file_t"].set(f"File time: {file_t:.2f}s  ({fmt_hms(file_t)})")

        if self.kind == "photo" and self.current_path and self.start_time is not None:
            try:
                offset_hours, _ = wo.resolve_display_offset_hours(
                    self.videos[0] if self.videos else None, "auto", None
                )
                real_t, photo_dt = wo.photo_align_offset(
                    self.current_path, self.start_time, offset_hours=offset_hours
                )
                data = wo.lookup_at_real_t(
                    self.records, self.start_time, self.gps_points, real_t
                )
                self.info_vars["factor"].set("Factor: 1× (photo)")
                self.info_vars["real_t"].set(f"Real (FIT) t: {real_t:.1f}s")
                self.info_vars["fit_clock"].set(f"FIT clock: {fmt_ts(data['ts'])}")
                self.info_vars["align"].set(f"Photo EXIF≈ {fmt_ts(photo_dt)}  → real_t {real_t:.1f}s")
                self.info_vars["sample"].set(self._sample_line(data))
            except Exception as e:
                self.info_vars["align"].set(f"Photo align error: {e}")
            return

        fac = factor_at(self.segments, file_t) if self.segments else 1.0
        self.info_vars["factor"].set(f"Factor: {fac:g}×")

        if self.video_to_real is None or self.start_time is None:
            self.info_vars["real_t"].set("Real (FIT) t: --")
            self.info_vars["fit_clock"].set("FIT clock: --")
            self.info_vars["sample"].set("Sample: --")
            self.info_vars["align"].set(f"Align: {self.align_note}")
            return

        real_t = self.video_to_real(file_t) + self.align_offset
        data = wo.lookup_at_real_t(
            self.records, self.start_time, self.gps_points, real_t
        )
        self.info_vars["real_t"].set(
            f"Real (FIT) t: {real_t:.1f}s  (map {self.video_to_real(file_t):.1f} + align {self.align_offset:.1f})"
        )
        self.info_vars["fit_clock"].set(f"FIT clock: {fmt_ts(data['ts'])}")
        self.info_vars["align"].set(f"Align: {self.align_note}")
        self.info_vars["sample"].set(self._sample_line(data))

    def _sample_line(self, data: dict) -> str:
        parts = []
        if self.sport == "dive":
            for key, label, unit in (
                ("depth", "depth", "m"),
                ("max_depth", "max", "m"),
                ("hr", "HR", "bpm"),
                ("temp", "temp", "°C"),
                ("ndl", "NDL", "s"),
                ("tts", "TTS", "s"),
            ):
                v = data.get(key, np.nan)
                if v is not None and not (isinstance(v, float) and np.isnan(v)):
                    if key == "hr":
                        parts.append(f"{label} {int(v)}{unit}")
                    else:
                        parts.append(f"{label} {v:.1f}{unit}")
        else:
            sp = data.get("speed", np.nan)
            if sp is not None and not (isinstance(sp, float) and np.isnan(sp)):
                parts.append(f"speed {sp * 3.6:.1f}km/h")
            hr = data.get("hr", np.nan)
            if hr is not None and not (isinstance(hr, float) and np.isnan(hr)):
                parts.append(f"HR {int(hr)}")
            elev = data.get("elev", np.nan)
            if elev is not None and not (isinstance(elev, float) and np.isnan(elev)):
                parts.append(f"elev {elev:.0f}m")
        return "Sample: " + (", ".join(parts) if parts else "--")

    # ---------------------------------------------------------- plan / copy
    def show_plan(self):
        if not self.folder or self.records is None:
            messagebox.showinfo("Plan", "Load a folder first")
            return
        try:
            plan, _ = wo.build_batch_plan(
                self.videos, self.photos, self.fit_path,
                self.records, self.start_time, self.gps_points,
                extra_offset=0.0, auto_align=True, offset_explicit=False,
                factor=self.factor, time_mode="auto", utc_offset=None,
            )
        except Exception as e:
            messagebox.showerror("Plan", str(e))
            return
        lines = ["Final render plan:"]
        for i, e in enumerate(plan, 1):
            if e["kind"] == "video_slice":
                lines.append(
                    f"  {i}. [video] {Path(e['path']).name}  {e['t0']:.2f}-{e['t1']:.2f}s"
                )
            else:
                lines.append(f"  {i}. [photo] {Path(e['path']).name}")
        text = "\n".join(lines)
        self.log(text)
        win = ctk.CTkToplevel(self)
        win.title("Render plan")
        win.geometry("560x400")
        box = ctk.CTkTextbox(win, font=ctk.CTkFont(family="monospace", size=12))
        box.pack(fill="both", expand=True, padx=8, pady=8)
        box.insert("end", text)

    def copy_snapshot(self):
        t = self._current_file_t()
        lines = [
            f"folder={self.folder}",
            f"fit={self.fit_path}",
            f"clip={self.current_path}",
            f"kind={self.kind}",
            f"sport={self.sport}",
            f"factor_setting={self.factor}",
            f"file_t={t:.3f}",
            f"align={self.align_note} offset={self.align_offset:.3f}",
        ]
        if self.segments and self.video_to_real:
            fac = factor_at(self.segments, t)
            real_t = self.video_to_real(t) + self.align_offset
            lines.append(f"factor_at_t={fac:g}")
            lines.append(f"real_t={real_t:.3f}")
            data = wo.lookup_at_real_t(
                self.records, self.start_time, self.gps_points, real_t
            )
            lines.append(f"fit_clock={fmt_ts(data['ts'])}")
            lines.append(self._sample_line(data))
            lines.append("segments:")
            for s, e, sp in self.segments:
                lines.append(f"  {s:.2f}-{e:.2f} {sp:g}x")
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log("Debug snapshot copied to clipboard")

    def on_close(self):
        self.stop_play()
        if self.cap is not None:
            self.cap.release()
        self.destroy()


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else None
    app = DebugApp(initial_folder=folder)
    app.mainloop()


if __name__ == "__main__":
    main()
