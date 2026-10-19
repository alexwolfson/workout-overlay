#!/usr/bin/env python3
"""
FIT baseline debugger — session sport, record fields, message types.

  python fit_debugger.py activity.fit
"""

import os
import sys
from collections import Counter

from fitparse import FitFile

# Common FIT sport enum values (fitparse may return int or string)
SPORT_MAP = {
    0: "generic",
    1: "running",
    2: "cycling",
    3: "transition",
    4: "fitness_equipment",
    5: "swimming",
    6: "basketball",
    7: "soccer",
    8: "tennis",
    9: "american_football",
    10: "training",
    11: "walking",
    12: "cross_country_skiing",
    13: "alpine_skiing",
    14: "snowboarding",
    15: "rowing",
    16: "mountaineering",
    17: "hiking",
    18: "multisport",
    19: "paddling",
    20: "flying",
    21: "e_biking",
    22: "motorcycling",
    23: "boating",
    24: "driving",
    25: "golf",
    26: "football",
    27: "hockey",
    28: "rugby",
    29: "cricket",
    30: "extreme_sports",
    31: "mountaineering",
    32: "martial_arts",
    33: "boxing",
    34: "floorball",
    35: "fencing",
    36: "wrestling",
    37: "gymnastics",
    38: "artistic_gymnastics",
    39: "inline_skating",
    40: "mountaineering",
    41: "rafting",
    42: "windsurfing",
    43: "kitesurfing",
    44: "tactical",
    45: "bmx",
    46: "spin",
    47: "flying_disc",
    48: "climbing",
    49: "recreational",
    53: "diving",  # Amazfit scuba / free diving often uses this
}

DIVE_TOKENS = {
    "diving", "dive", "scuba", "scuba_diving", "apnea", "apnea_diving",
    "freedive", "free_dive", "freediving", "spearfishing",
}


def _sport_label(sport):
    """Human-readable label for sport field (int or string)."""
    if sport is None:
        return None
    if isinstance(sport, int):
        return f"{sport} -> '{SPORT_MAP.get(sport, f'unknown_{sport}')}'"
    return f"'{sport}'"


def _suggest_panel(sport, sub_sport, n_depth):
    """Same idea as workout_overlay.detect_sport (without forced override)."""
    tokens = []
    for v in (sport, sub_sport):
        if v is None:
            continue
        if isinstance(v, int):
            if v == 53:
                return "dive"
            tokens.append(SPORT_MAP.get(v, str(v)).lower())
        else:
            tokens.append(str(v).strip().lower().replace(" ", "_"))
    for t in tokens:
        if t in DIVE_TOKENS or "dive" in t or "scuba" in t or "apnea" in t:
            return "dive"
    if n_depth >= 5:
        return "dive"
    return "outdoor"


def debug_fit_fields(fit_path):
    if not os.path.exists(fit_path):
        print(f"Error: file not found: {fit_path}")
        sys.exit(1)

    print("=" * 70)
    print(f"FIT DEBUG: {os.path.basename(fit_path)}")
    print("=" * 70)

    ff = FitFile(fit_path)

    # --- 1. Session sport / sub_sport ---
    detected_sport = None
    detected_sub_sport = None
    for session_msg in ff.get_messages("session"):
        session_data = {f.name: f.value for f in session_msg}
        if "sport" in session_data:
            detected_sport = session_data["sport"]
        if "sub_sport" in session_data:
            detected_sub_sport = session_data["sub_sport"]
        break

    print("[1] Session sport metadata")
    if detected_sport is not None:
        print(f"  sport      : {_sport_label(detected_sport)}")
    else:
        print("  sport      : NOT FOUND")
    if detected_sub_sport is not None:
        print(f"  sub_sport  : {_sport_label(detected_sub_sport)}")
    else:
        print("  sub_sport  : None")

    # --- 2. Record field counts ---
    keys = Counter()
    for m in ff.get_messages("record"):
        for f in m:
            if f.value is not None:
                keys[f.name] += 1

    n_depth = keys.get("depth", 0)
    print("\n[2] Record fields (non-null counts)")
    if keys:
        for k, n in keys.most_common():
            print(f"  {k:30s} {n}")
    else:
        print("  (no record metrics)")

    # --- 3. Message type summary ---
    print("\n[3] Message types")
    types = Counter(m.name for m in ff.get_messages())
    for t, n in types.most_common():
        print(f"  {t:28s} {n}")

    # --- 4. Panel suggestion (matches overlay logic) ---
    panel = _suggest_panel(detected_sport, detected_sub_sport, n_depth)
    print("\n[4] Overlay panel suggestion")
    print(f"  depth samples : {n_depth}")
    print(f"  panel mode    : {panel}")
    print("=" * 70)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:  python3 fit_debugger.py <file.fit>")
        sys.exit(1)
    debug_fit_fields(sys.argv[1])
