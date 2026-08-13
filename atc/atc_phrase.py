#!/usr/bin/env python3
"""
Stream Deck Ground/Tower phrase builder for SRS ExternalAudio.

Fetches METAR from Opus, picks active runway from wind, fills phrase templates,
and transmits via the patched DCS-SR-ExternalAudio.exe to the squadron SRS server.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
AIRPORTS_PATH = HERE / "airports.json"
STATE_PATH = HERE / "state.json"
CONFIG_PATH = HERE / "config.json"
TTS_USAGE_PATH = HERE / "tts_usage.json"

# Google Cloud TTS free monthly characters + overage USD per 1M (see cloud pricing)
TTS_FREE_CHARS_PER_MONTH: dict[str, int] = {
    "chirp": 1_000_000,
    "neural2": 1_000_000,
    "wavenet": 4_000_000,
    "studio": 1_000_000,
    "standard": 4_000_000,
    "other": 1_000_000,
}
TTS_USD_PER_MILLION: dict[str, float] = {
    "chirp": 30.0,
    "neural2": 16.0,
    "wavenet": 4.0,
    "studio": 160.0,
    "standard": 4.0,
    "other": 16.0,
}
# Rough chars for one full Nellis default sortie (for estimator copy)
TTS_CHARS_PER_SORTIE_EST = 1400
# Auto-fallback when a family's free tier is nearly exhausted (avoid paid overage)
TTS_FREE_TIER_WARN_PCT = 0.90
TTS_FAMILY_FALLBACK_ORDER = ("chirp", "neural2", "wavenet")

DIGIT_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "tree",
    "4": "four",
    "5": "fife",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "niner",
}
# Tokens Chirp must not breathe between (squawk / freq / heading digit runs).
_RADIO_DIGIT_TOKENS = frozenset(DIGIT_WORDS.values()) | frozenset(
    {"oh", "five", "nine", "three"}  # common spoken variants
)
_RADIO_DIGIT_GLUE_MID = frozenset({"point", "decimal"})
# Words Chirp likes to pause before/after around numbers — keep with the run.
_RADIO_DIGIT_GLUE_PREFIX = frozenset(
    {
        "runway",
        "squawk",
        "heading",
        "altitude",
        "altimeter",
        "flight",
        "level",
        "angels",
        "wind",
        "at",
        "taxiway",
        "gate",
        "pad",
        "channel",
        "frequency",
        "contact",
    }
)
_RADIO_DIGIT_GLUE_SUFFIX = frozenset(
    {
        "left",
        "right",
        "center",
        "degrees",
        "knots",
        "thousand",
        "hundred",
    }
)
# Conversational range/clock tails after a digit run (alpha: "… fife twenty five").
_RADIO_NATURAL_NUMBER_WORDS = frozenset(
    {
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
    }
)
# Narrow no-break space: Chirp treats the run as one prosody unit (less list-pause).
_RADIO_DIGIT_NBSP = "\u00a0"
# Map spoken radio digit words back to numerals (legacy helpers / parsing).
_RADIO_WORD_TO_DIGIT: dict[str, str] = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "tree": "3",
    "three": "3",
    "four": "4",
    "fife": "5",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "niner": "9",
    "nine": "9",
    "point": ".",
    "decimal": ".",
}
# Canonical ICAO radio digit speech (never "oh" / "nine" / "five" / "three").
_RADIO_DIGIT_CANONICAL: dict[str, str] = {
    "oh": "zero",
    "zero": "zero",
    "one": "one",
    "two": "two",
    "three": "tree",
    "tree": "tree",
    "four": "four",
    "five": "fife",
    "fife": "fife",
    "six": "six",
    "seven": "seven",
    "eight": "eight",
    "nine": "niner",
    "niner": "niner",
    "point": "point",
    "decimal": "decimal",
}

# Natural minute words for conversational ATC (vs digit-by-digit)
MINUTE_WORDS = {
    1: "one",
    2: "two",
    3: "tree",
    4: "four",
    5: "fife",
    6: "six",
    7: "seven",
    8: "eight",
    9: "niner",
    10: "ten",
    15: "fifteen",
    20: "twenty",
    30: "thirty",
}


def _pick(*options: str) -> str:
    return random.choice(options)


def takeoff_departure_switch() -> str:
    """Tower takeoff / freq-switch closer — not 'contact departure'."""
    return random.choice(("switch to departure", "change to departure"))


def with_freq_handoff_closer(sentence: str) -> str:
    """
    Often append a friendly closer on frequency-switch / handoff calls.
    About 70% of the time: good day / see ya / see you; otherwise bare.
    """
    s = str(sentence or "").strip().rstrip(".")
    if not s:
        return ""
    closer = random.choices(
        ("", "good day", "see ya", "see you"),
        weights=(30, 40, 20, 10),
        k=1,
    )[0]
    if closer:
        return f"{s}, {closer}."
    return f"{s}."


@dataclass
class Weather:
    wind_dir: int | None
    wind_speed_kt: int | None
    altimeter_inhg: float | None
    raw: str
    ceiling_ft: int | None = None  # lowest BKN/OVC height AGL, if present
    visibility_sm: float | None = None


@dataclass
class OpusFlightContext:
    """Active Opus signup + filed flight-plan fields for clearance delivery."""

    radio_callsign: str  # spoken ATC callsign, e.g. BRUISER 5 (no seat by default)
    flight_id: int
    flight_callsign: str
    seat: int
    event_date: str | None
    theater_id: int | None
    dep_icao: str | None
    arr_icao: str | None
    aircraft: str | None
    fp_altitude: str | None
    fp_speed: str | None
    fp_route_string: str | None
    fp_remarks: str | None
    fp_aircraft_type: str | None
    fp_filed_at: str | None
    mode3: str | None
    tcn: str | None
    comms_vhf: str | None
    signup_count: int = 1
    flight_qty: int | None = None
    mission: str | None = None
    mission_number: str | None = None
    vul_start: str | None = None
    vul_end: str | None = None
    squadron_name: str | None = None

    @property
    def element_callsign(self) -> str:
        """Flight + seat, e.g. BRUISER 5-1 (when seat is needed)."""
        return f"{self.flight_callsign}-{int(self.seat)}"

    @property
    def has_filed_plan(self) -> bool:
        """True when Opus shows a filed IFR/VFR plan (route, altitude, or filed timestamp)."""
        if self.fp_filed_at:
            return True
        if self.fp_route_string:
            return True
        if self.fp_altitude:
            return True
        return False

    @property
    def squawk_in_sequence(self) -> bool:
        """Multi-ship: more than one Opus signup (or planned qty > 1)."""
        if self.signup_count > 1:
            return True
        if self.flight_qty is not None and self.flight_qty > 1:
            return True
        return False


_RUNWAY_TOKEN = re.compile(r"^(\d{1,2})([LCR]?)$", re.IGNORECASE)
_FIX_TRAIL_DIGITS = re.compile(r"^([A-Za-z]+)(\d+)$")
# Opus visual Flex: FLEX, FLEX21R, FLEX21L, FLEX03, FLEX03L, FLEX03R, …
_FLEX_DEP_TOKEN = re.compile(r"^FLEX(\d{1,2}[LCR]?)?$", re.IGNORECASE)

# Map our agency channels -> substrings in Opus theater radio preset names
OPUS_FREQ_NAME_MAP: dict[str, tuple[str, ...]] = {
    "delivery": ("delivery",),
    "ground": ("ground",),
    "tower": ("tower",),
    "departure": ("departure",),
    "approach": ("approach",),
    "blackjack": ("blackjack",),
    "bandsaw": (
        "bandsaw",
        "band saw",
        "ansa",
        "and saw",
        "bansaw",
        "ban saw",
    ),
    "ops": ("squadron ops", "ops"),
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)


def resolve_repo_path(raw: str) -> Path:
    """Expand ~ / env vars; resolve relative paths against the atc/ folder."""
    path = Path(os.path.expandvars(os.path.expanduser(str(raw).strip()))).expanduser()
    if not path.is_absolute():
        path = (HERE / path).resolve()
    return path


def resolve_external_audio_exe(config: dict[str, Any]) -> Path:
    raw = str(config.get("external_audio_exe") or "../DCS-SR-ExternalAudio.exe").strip()
    return resolve_repo_path(raw)


def save_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def speak_digits(text: str) -> str:
    """Expand digit runs for radio speech; leave letters/words alone."""
    out: list[str] = []
    for ch in text:
        if ch.isdigit():
            out.append(DIGIT_WORDS[ch])
            out.append(" ")
        elif ch == ".":
            out.append("point")
            out.append(" ")
        elif ch == "-":
            out.append(" ")
        elif ch == " ":
            out.append(" ")
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def speak_callsign(callsign: str) -> str:
    # "BRUISER 5" -> "Bruiser fife"; "BRUISER 5-1" -> "Bruiser fife one"
    cleaned = callsign.strip().replace("-", " ")
    parts = cleaned.split()
    spoken: list[str] = []
    for part in parts:
        if any(c.isdigit() for c in part):
            spoken.append(speak_digits(part))
        else:
            spoken.append(part.title() if part.isupper() else part)
    return " ".join(spoken)


def http_get_json(url: str, user_agent: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def callsign_override(config: dict[str, Any]) -> str | None:
    """Manual flight callsign from config/UI, if set."""
    return _str_or_none(config.get("callsign_override"))


def normalize_runway(raw: str | None) -> str | None:
    """Normalize '21r' / '21 R' / '3l' → '21R' / '03L'; None if blank."""
    s = re.sub(r"\s+", "", str(raw or "").strip())
    if not s:
        return None
    m = _RUNWAY_TOKEN.match(s)
    if not m:
        return s.upper()
    return f"{int(m.group(1)):02d}{m.group(2).upper()}"


def runway_override(config: dict[str, Any]) -> str | None:
    """Manual departure runway from config/UI, if set."""
    return normalize_runway(config.get("runway_override"))


def synthetic_flight_context(callsign: str) -> OpusFlightContext:
    """Minimal context when using a manual callsign without Opus signup/FP."""
    return OpusFlightContext(
        radio_callsign=callsign,
        flight_id=0,
        flight_callsign=callsign,
        seat=1,
        event_date=None,
        theater_id=None,
        dep_icao=None,
        arr_icao=None,
        aircraft=None,
        fp_altitude=None,
        fp_speed=None,
        fp_route_string=None,
        fp_remarks=None,
        fp_aircraft_type=None,
        fp_filed_at=None,
        mode3=None,
        tcn=None,
        comms_vhf=None,
    )


def apply_callsign_override(config: dict[str, Any], ctx: OpusFlightContext) -> OpusFlightContext:
    override = callsign_override(config)
    if not override:
        return ctx
    print(f"Callsign override: {override}")
    return replace(ctx, radio_callsign=override, flight_callsign=override)


def configured_opus_flight_id(config: dict[str, Any]) -> int | None:
    raw = config.get("opus_flight_id")
    if raw is None or raw == "":
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def configured_opus_seat(config: dict[str, Any]) -> int | None:
    raw = config.get("opus_seat")
    if raw is None or raw == "":
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _opus_cache_key(config: dict[str, Any]) -> str:
    fid = configured_opus_flight_id(config)
    seat = configured_opus_seat(config)
    return "|".join(
        [
            (config.get("opus_backend_url") or "").rstrip("/"),
            (config.get("opus_user_name") or "").strip().casefold(),
            str(fid or ""),
            str(seat or ""),
            callsign_override(config) or "",
            "1" if config.get("tts_include_seat") else "0",
        ]
    )


def invalidate_opus_cache() -> None:
    _OPUS_CACHE["key"] = ""
    _OPUS_CACHE["exp"] = 0.0
    _OPUS_CACHE["ctx"] = None


def invalidate_metar_cache() -> None:
    _METAR_CACHE.clear()


def invalidate_flight_lookups() -> None:
    """Drop cached Opus flight + METAR so the next resolve is fresh."""
    invalidate_opus_cache()
    invalidate_metar_cache()


def _opus_flight_sort_key(flight: dict[str, Any]) -> tuple:
    return (str(flight.get("event_date") or ""), str(flight.get("vul_start") or ""), int(flight.get("id") or 0))


def list_opus_flights(
    config: dict[str, Any],
    *,
    include_detail: bool = True,
) -> list[dict[str, Any]]:
    """
    List flights from Opus backend (newest first).

    Each row includes list fields; when include_detail=True also pulls FP route/alt
    and signup names for the picker UI.
    """
    backend = (config.get("opus_backend_url") or "").rstrip("/")
    if not backend:
        raise RuntimeError("opus_backend_url is not set")
    ua = config.get("user_agent", "DCS-ATC-Phrase/1.0")
    flights = http_get_json(f"{backend}/opus/flights", ua)
    if not isinstance(flights, list):
        raise RuntimeError("Opus /opus/flights did not return a list")

    rows: list[dict[str, Any]] = []
    for flight in sorted(flights, key=_opus_flight_sort_key, reverse=True):
        fid = flight.get("id")
        if fid is None:
            continue
        row: dict[str, Any] = {
            "id": int(fid),
            "callsign": str(flight.get("callsign") or "").strip(),
            "event_date": _str_or_none(flight.get("event_date")),
            "vul_start": _str_or_none(flight.get("vul_start")),
            "vul_end": _str_or_none(flight.get("vul_end")),
            "aircraft": _str_or_none(flight.get("aircraft")),
            "qty": flight.get("qty"),
            "mission": _str_or_none(flight.get("mission")),
            "mission_number": _str_or_none(flight.get("mission_number")),
            "squadron_name": _str_or_none(flight.get("squadron_name")),
            "theater_name": _str_or_none(flight.get("theater_name")),
            "fp_route_string": None,
            "fp_altitude": None,
            "fp_filed_at": None,
            "has_filed_plan": False,
            "signups": [],
            "signup_labels": [],
        }
        if include_detail:
            try:
                detail = http_get_json(f"{backend}/opus/flights/{int(fid)}", ua)
                if isinstance(detail, dict):
                    row["fp_route_string"] = _str_or_none(detail.get("fp_route_string"))
                    row["fp_altitude"] = _str_or_none(detail.get("fp_altitude"))
                    row["fp_filed_at"] = _str_or_none(detail.get("fp_filed_at"))
                    row["has_filed_plan"] = bool(
                        row["fp_filed_at"] or row["fp_route_string"] or row["fp_altitude"]
                    )
                    # Prefer list event/vul; fill from detail if missing
                    row["event_date"] = row["event_date"] or _str_or_none(detail.get("event_date"))
                    row["vul_start"] = row["vul_start"] or _str_or_none(detail.get("vul_start"))
                    row["vul_end"] = row["vul_end"] or _str_or_none(detail.get("vul_end"))
            except urllib.error.URLError as exc:
                print(f"WARNING: Opus flight {fid} detail failed ({exc})", file=sys.stderr)
            try:
                signups = http_get_json(f"{backend}/opus/flights/{int(fid)}/signups", ua)
                if isinstance(signups, dict):
                    signups = [signups]
                if isinstance(signups, list):
                    row["signups"] = signups
                    row["crew_slots"] = opus_crew_slots(signups, qty=row.get("qty"))
                    row["signup_labels"] = [
                        f"{c['seat']}:{c['user_name']}" for c in row["crew_slots"] if c.get("user_name")
                    ]
            except urllib.error.URLError as exc:
                print(f"WARNING: Opus flight {fid} signups failed ({exc})", file=sys.stderr)
        rows.append(row)
    return rows


def opus_crew_slots(
    signups: list[dict[str, Any]] | None,
    *,
    qty: Any = None,
) -> list[dict[str, Any]]:
    """
    Normalize Opus signups into seat-ordered crew slots.

    Includes open seats through planned qty when qty > signed-up count.
    """
    by_seat: dict[int, dict[str, Any]] = {}
    for su in signups or []:
        if not isinstance(su, dict):
            continue
        try:
            seat_n = int(su.get("seat"))
        except (TypeError, ValueError):
            continue
        if seat_n <= 0:
            continue
        by_seat[seat_n] = {
            "seat": seat_n,
            "user_name": str(su.get("user_name") or "").strip() or None,
            "tail_number": _str_or_none(su.get("tail_number")),
            "callsign": _str_or_none(su.get("callsign")),
            "raw": su,
        }
    try:
        planned = int(qty) if qty is not None else 0
    except (TypeError, ValueError):
        planned = 0
    max_seat = max([planned, *by_seat.keys()], default=0)
    slots: list[dict[str, Any]] = []
    for seat_n in range(1, max_seat + 1):
        if seat_n in by_seat:
            slots.append(by_seat[seat_n])
        else:
            slots.append(
                {
                    "seat": seat_n,
                    "user_name": None,
                    "tail_number": None,
                    "callsign": None,
                    "raw": None,
                }
            )
    return slots


def format_crew_slot(slot: dict[str, Any]) -> str:
    """Human label: '1 - Turtle (66-4368)' or '3 - (open)'."""
    seat = slot.get("seat")
    name = slot.get("user_name") or "(open)"
    tail = slot.get("tail_number")
    if tail:
        return f"{seat} - {name} ({tail})"
    return f"{seat} - {name}"

def _pick_seat_from_signups(
    signups: list[dict[str, Any]],
    *,
    preferred_seat: int | None,
    user: str | None,
) -> int:
    if preferred_seat is not None:
        for su in signups:
            try:
                if int(su.get("seat")) == preferred_seat:
                    return preferred_seat
            except (TypeError, ValueError):
                continue
        return preferred_seat
    if user:
        for su in signups:
            if str(su.get("user_name") or "").casefold() == user.casefold():
                try:
                    return int(su.get("seat"))
                except (TypeError, ValueError):
                    break
    if signups:
        try:
            return int(signups[0].get("seat") or 1)
        except (TypeError, ValueError):
            pass
    return 1


def _context_from_opus_flight(
    config: dict[str, Any],
    flight_list: dict[str, Any],
    detail: dict[str, Any],
    signups: list[dict[str, Any]],
    *,
    seat: int,
    user: str,
) -> OpusFlightContext:
    base = str(flight_list.get("callsign") or detail.get("callsign") or "").strip()
    fid = int(flight_list.get("id") or detail.get("id") or 0)
    include_seat = bool(config.get("tts_include_seat", False))
    radio = f"{base}-{int(seat)}" if include_seat and base else (base or "CALLSIGN")
    theater_raw = detail.get("theater_id")
    if theater_raw is None:
        theater_raw = flight_list.get("theater_id")
    theater_id = int(theater_raw) if theater_raw is not None else None
    qty_raw = detail.get("qty", flight_list.get("qty"))
    try:
        flight_qty = int(qty_raw) if qty_raw is not None else None
    except (TypeError, ValueError):
        flight_qty = None
    ctx = OpusFlightContext(
        radio_callsign=radio,
        flight_id=fid,
        flight_callsign=base or radio,
        seat=int(seat),
        event_date=str(detail.get("event_date") or flight_list.get("event_date") or "") or None,
        theater_id=theater_id,
        dep_icao=_str_or_none(detail.get("fp_departure") or detail.get("dep_icao")),
        arr_icao=_str_or_none(detail.get("arr_icao")),
        aircraft=_str_or_none(detail.get("aircraft") or flight_list.get("aircraft")),
        fp_altitude=_str_or_none(detail.get("fp_altitude")),
        fp_speed=_str_or_none(detail.get("fp_speed")),
        fp_route_string=_str_or_none(detail.get("fp_route_string")),
        fp_remarks=_str_or_none(detail.get("fp_remarks")),
        fp_aircraft_type=_str_or_none(detail.get("fp_aircraft_type")),
        fp_filed_at=_str_or_none(detail.get("fp_filed_at")),
        mode3=_str_or_none(detail.get("mode3")),
        tcn=_str_or_none(detail.get("tcn")),
        comms_vhf=_str_or_none(detail.get("comms_vhf")),
        signup_count=len(signups),
        flight_qty=flight_qty,
        mission=_str_or_none(detail.get("mission") or flight_list.get("mission")),
        mission_number=_str_or_none(
            detail.get("mission_number") or flight_list.get("mission_number")
        ),
        vul_start=_str_or_none(detail.get("vul_start") or flight_list.get("vul_start")),
        vul_end=_str_or_none(detail.get("vul_end") or flight_list.get("vul_end")),
        squadron_name=_str_or_none(
            detail.get("squadron_name") or flight_list.get("squadron_name")
        ),
    )
    print(
        f"Opus callsign: {radio} "
        f"(user={user or '-'}, flight_id={fid}, flight={base}, seat={seat}, "
        f"include_seat={include_seat}, signups={len(signups)}, qty={flight_qty}, "
        f"event={ctx.event_date}, filed={ctx.has_filed_plan}, route={ctx.fp_route_string}, "
        f"alt={ctx.fp_altitude}, squawk={ctx.mode3})"
    )
    return ctx


def _opus_flights_list_row(
    config: dict[str, Any], flight_id: int
) -> dict[str, Any] | None:
    """
    One row from GET /opus/flights (has event_date / VUL / package).

    Flight detail omits those fields — Blackjack airspace + VUL need the list row.
    """
    backend = (config.get("opus_backend_url") or "").rstrip("/")
    if not backend or not flight_id:
        return None
    ua = config.get("user_agent", "DCS-ATC-Phrase/1.0")
    try:
        flights = http_get_json(f"{backend}/opus/flights", ua)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError):
        return None
    if not isinstance(flights, list):
        return None
    for flight in flights:
        try:
            if int(flight.get("id") or 0) == int(flight_id):
                return flight if isinstance(flight, dict) else None
        except (TypeError, ValueError):
            continue
    return None


def _merge_opus_list_metadata(
    detail: dict[str, Any], list_row: dict[str, Any] | None
) -> dict[str, Any]:
    """Fill event/VUL/package (and callsign) from the flights list when detail lacks them."""
    out = dict(detail or {})
    if not list_row:
        return out
    for key in (
        "event_date",
        "vul_start",
        "vul_end",
        "package_name",
        "squadron_name",
        "theater_name",
        "callsign",
        "qty",
        "mission",
        "mission_number",
        "aircraft",
        "opus_event_id",
    ):
        if out.get(key) in (None, "") and list_row.get(key) not in (None, ""):
            out[key] = list_row.get(key)
    return out


def resolve_active_opus_flight(config: dict[str, Any]) -> OpusFlightContext | None:
    """
    Load filed FP + callsign from Opus.

    Preference order:
      1. config opus_flight_id (chosen in the flights picker)
      2. Scan flights for opus_user_name signup (legacy auto-match)
      3. Manual callsign / offline placeholder

    Seat: opus_seat if set, else matching signup for opus_user_name, else seat 1.
    callsign_override replaces the spoken callsign (FP still from Opus when available).
    """
    override = callsign_override(config)
    user = (config.get("opus_user_name") or "").strip()
    backend = (config.get("opus_backend_url") or "").rstrip("/")
    selected_id = configured_opus_flight_id(config)
    preferred_seat = configured_opus_seat(config)

    if not backend:
        label = override or "CALLSIGN"
        print(f"Using offline callsign (no Opus backend): {label}")
        return synthetic_flight_context(label)

    if not selected_id and not user:
        # Backend set but nothing to resolve yet — allow manual/offline use
        if override:
            print(f"Using manual callsign (no Opus flight selected): {override}")
            return synthetic_flight_context(override)
        print("Using offline callsign (no Opus flight/user selected): CALLSIGN")
        return synthetic_flight_context("CALLSIGN")

    cache_key = _opus_cache_key(config)
    now = time.time()
    if (
        _OPUS_CACHE.get("key") == cache_key
        and float(_OPUS_CACHE.get("exp") or 0) > now
        and _OPUS_CACHE.get("ctx") is not None
    ):
        ctx = _OPUS_CACHE["ctx"]
        print(f"Opus callsign (cached): {ctx.radio_callsign}")
        return apply_callsign_override(config, replace(ctx))

    ua = config.get("user_agent", "DCS-ATC-Phrase/1.0")
    flight_list: dict[str, Any] | None = None
    signups: list[dict[str, Any]] = []

    if selected_id is not None:
        try:
            fetched = http_get_json(f"{backend}/opus/flights/{selected_id}", ua)
            if isinstance(fetched, dict):
                flight_list = fetched
        except urllib.error.URLError as exc:
            print(f"WARNING: Opus flight {selected_id} fetch failed ({exc})", file=sys.stderr)
        if flight_list is None:
            # Fall back to list endpoint row
            try:
                flights = http_get_json(f"{backend}/opus/flights", ua)
                if isinstance(flights, list):
                    for flight in flights:
                        if int(flight.get("id") or 0) == selected_id:
                            flight_list = flight
                            break
            except urllib.error.URLError as exc:
                print(f"WARNING: Opus flights fetch failed ({exc})", file=sys.stderr)
        if flight_list is not None:
            try:
                fetched_signups = http_get_json(
                    f"{backend}/opus/flights/{selected_id}/signups", ua
                )
                if isinstance(fetched_signups, dict):
                    signups = [fetched_signups]
                elif isinstance(fetched_signups, list):
                    signups = fetched_signups
            except urllib.error.URLError as exc:
                print(f"WARNING: Opus signups fetch failed ({exc})", file=sys.stderr)
    else:
        # Legacy: find newest flight where opus_user_name is signed up
        try:
            flights = http_get_json(f"{backend}/opus/flights", ua)
        except urllib.error.URLError as exc:
            print(f"WARNING: Opus flights fetch failed ({exc})", file=sys.stderr)
            if override:
                return synthetic_flight_context(override)
            return None
        if not isinstance(flights, list) or not flights:
            if override:
                return synthetic_flight_context(override)
            return None
        for flight in sorted(flights, key=_opus_flight_sort_key, reverse=True):
            fid = flight.get("id")
            if fid is None:
                continue
            try:
                fetched_signups = http_get_json(f"{backend}/opus/flights/{fid}/signups", ua)
            except urllib.error.URLError:
                continue
            if not isinstance(fetched_signups, list):
                continue
            for su in fetched_signups:
                if str(su.get("user_name") or "").casefold() == user.casefold():
                    flight_list = flight
                    signups = fetched_signups
                    break
            if flight_list is not None:
                break
        if flight_list is None:
            print(f"WARNING: No Opus signup found for user '{user}'", file=sys.stderr)
            if override:
                return synthetic_flight_context(override)
            return None

    if flight_list is None:
        if override:
            return synthetic_flight_context(override)
        return None

    fid = int(flight_list.get("id") or selected_id or 0)
    # Detail endpoint omits event_date / VUL — merge from /opus/flights list row.
    if flight_list.get("event_date") in (None, "") or flight_list.get("vul_start") in (
        None,
        "",
    ):
        flight_list = _merge_opus_list_metadata(
            flight_list, _opus_flights_list_row(config, fid)
        )
    detail: dict[str, Any] = dict(flight_list)
    # If we only have a list row, fetch detail for FP fields
    if not detail.get("fp_route_string") and not detail.get("fp_altitude"):
        try:
            fetched = http_get_json(f"{backend}/opus/flights/{fid}", ua)
            if isinstance(fetched, dict):
                # Keep list VUL/event — detail never carries them.
                detail = _merge_opus_list_metadata(fetched, flight_list)
        except urllib.error.URLError as exc:
            print(f"WARNING: Opus flight detail fetch failed ({exc}); using list fields", file=sys.stderr)

    base = str(flight_list.get("callsign") or detail.get("callsign") or "").strip()
    if not base or not fid:
        if override:
            return synthetic_flight_context(override)
        return None

    seat = _pick_seat_from_signups(signups, preferred_seat=preferred_seat, user=user or None)
    ctx = _context_from_opus_flight(
        config, flight_list, detail, signups, seat=seat, user=user
    )
    _OPUS_CACHE["key"] = cache_key
    _OPUS_CACHE["exp"] = now + _OPUS_CACHE_TTL_SEC
    _OPUS_CACHE["ctx"] = ctx
    return apply_callsign_override(config, replace(ctx))


def resolve_callsign_from_opus(config: dict[str, Any]) -> str | None:
    """Compatibility wrapper — radio callsign only."""
    ctx = resolve_active_opus_flight(config)
    return None if ctx is None else ctx.radio_callsign


def cached_radio_callsign(config: dict[str, Any]) -> str:
    """
    Callsign already known for this flight, without touching the network.

    Voice recognition needs the callsign on every transmission to tell "Fleece 1"
    (us) from "Fleece 2" (a wingman), and cannot afford an Opus round-trip in
    the middle of a PTT release. Returns "" until something else has resolved it.
    """
    override = callsign_override(config)
    if override:
        return override
    ctx = _OPUS_CACHE.get("ctx")
    return str(getattr(ctx, "radio_callsign", "") or "")


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_route_tokens(route: str | None) -> list[str]:
    """
    Split an Opus/filed route into tokens.
    Handles FLEX21R.DREAM, KLSV FLEX21L DREAM, FLEX03R/MINTT, etc.
    """
    if not route:
        return []
    # Normalize common separators to spaces, keep alphanumerics together
    cleaned = re.sub(r"[./\\|+_,;]+", " ", str(route).strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return [p for p in cleaned.split(" ") if p]


def _flex_runway(token: str) -> str | None:
    """Extract runway from FLEX21R / FLEX03L style tokens."""
    m = _FLEX_DEP_TOKEN.match((token or "").strip())
    if not m or not m.group(1):
        return None
    rm = _RUNWAY_TOKEN.match(m.group(1).upper())
    if not rm:
        return None
    return f"{int(rm.group(1)):02d}{rm.group(2).upper()}"


def _normalize_dep_token(tok: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (tok or "").upper())


def _is_flex_dep_token(tok: str) -> bool:
    return bool(_FLEX_DEP_TOKEN.match(_normalize_dep_token(tok)))


def runway_from_route(route: str | None) -> str | None:
    tokens = parse_route_tokens(route)
    if not tokens:
        return None
    for tok in tokens:
        flex_rwy = _flex_runway(tok)
        if flex_rwy:
            return flex_rwy
    m = _RUNWAY_TOKEN.match(tokens[-1])
    if not m:
        return None
    return f"{int(m.group(1)):02d}{m.group(2).upper()}"


def first_route_fix(route: str | None) -> str | None:
    """First enroute fix after departure ICAO / FLEX*; skip arrival ICAO / runway tokens."""
    tokens = parse_route_tokens(route)
    if len(tokens) < 2:
        return None
    start = 1 if _looks_like_icao(tokens[0]) else 0
    end = len(tokens)
    if end > start and _RUNWAY_TOKEN.match(tokens[end - 1]):
        end -= 1
    if end > start and _looks_like_icao(tokens[end - 1]):
        end -= 1
    if start >= end:
        return None
    first = tokens[start]
    if _FLEX_DEP_TOKEN.match(first) and start + 1 < end:
        return tokens[start + 1]
    return first


def known_sids(airport: dict[str, Any]) -> set[str]:
    """Legacy manual SID allowlist (uppercase). Prefer departures/*.json catalog."""
    raw = airport.get("known_sids") or []
    if not isinstance(raw, list):
        return set()
    return {str(x).strip().upper() for x in raw if str(x).strip()}


def filed_sid(airport: dict[str, Any], route: str | None) -> str | None:
    """Legacy: SID token only if listed in airport.known_sids."""
    sids = known_sids(airport)
    if not sids or not route:
        return None
    tokens = parse_route_tokens(route)
    start = 1 if tokens and _looks_like_icao(tokens[0]) else 0
    for tok in tokens[start:]:
        if _RUNWAY_TOKEN.match(tok) or _looks_like_icao(tok):
            continue
        if tok.upper() in sids:
            return tok.upper()
    return None


def load_departure_catalog(airport: dict[str, Any]) -> dict[str, Any] | None:
    rel = airport.get("departures_file")
    if not rel:
        return None
    path = Path(rel)
    if not path.is_absolute():
        path = HERE / path
    if not path.is_file():
        print(f"WARNING: departures file not found: {path}", file=sys.stderr)
        return None
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: departures file read failed ({exc})", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def load_approach_catalog(airport: dict[str, Any]) -> dict[str, Any] | None:
    """NAFBI 11-250 VFR recoveries + instrument IAF catalog for this airport."""
    rel = airport.get("approaches_file")
    if not rel:
        return None
    path = Path(rel)
    if not path.is_absolute():
        path = HERE / path
    if not path.is_file():
        print(f"WARNING: approaches file not found: {path}", file=sys.stderr)
        return None
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: approaches file read failed ({exc})", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def approach_defaults(catalog: dict[str, Any] | None) -> dict[str, Any]:
    raw = (catalog or {}).get("defaults") if isinstance(catalog, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}


def _runway_side(runway: str | None) -> str:
    digits = re.sub(r"[^0-9]", "", str(runway or ""))
    if not digits:
        return "21"
    n = int(digits) % 100
    if n in (3, 4):
        return "03"
    if n in (21, 22):
        return "21"
    # Nearest of 03 / 21 by heading
    hdg = (n * 10) % 360
    d21 = heading_delta(float(hdg), 210.0)
    d03 = heading_delta(float(hdg), 30.0)
    return "21" if d21 <= d03 else "03"


def _headwind_kt(wind_dir: float | None, wind_spd: float | None, rwy_hdg: float) -> float:
    """Headwind component (kt) on a runway heading; negative = tailwind."""
    if wind_dir is None or wind_spd is None:
        return 0.0
    # Angle from runway heading to wind-from direction
    delta = (float(wind_dir) - float(rwy_hdg) + 180.0) % 360.0 - 180.0
    return float(wind_spd) * math.cos(math.radians(delta))


def pick_recovery_runway(
    airport: dict[str, Any],
    weather: Weather,
    *,
    instrument: bool = False,
    catalog: dict[str, Any] | None = None,
) -> str:
    """
    Prefer the 21s. Use the 03s only when headwind on 03 is >= wind_flip_min_kt
    (default 11) and stronger than the 21 headwind.

    Visual ops → 21R / 03L; instrument → 21L / 03R.
    """
    cat = catalog if catalog is not None else load_approach_catalog(airport)
    defs = approach_defaults(cat)
    prefer = str(defs.get("prefer_runway_side") or "21")
    try:
        min_kt = float(defs.get("wind_flip_min_kt") or 11)
    except (TypeError, ValueError):
        min_kt = 11.0

    side = prefer
    hw21 = _headwind_kt(weather.wind_dir, weather.wind_speed_kt, 210.0)
    hw03 = _headwind_kt(weather.wind_dir, weather.wind_speed_kt, 30.0)
    if prefer == "21":
        if hw03 >= min_kt and hw03 > hw21:
            side = "03"
        else:
            side = "21"
    elif hw21 >= min_kt and hw21 > hw03:
        side = "21"
    else:
        side = prefer

    raw = "21R" if side == "21" else "03L"
    if instrument:
        raw = "21L" if side == "21" else "03R"
    return align_runway_to_airport(airport, raw, instrument=instrument)


def is_vfr_recovery_weather(
    weather: Weather,
    *,
    catalog: dict[str, Any] | None = None,
) -> bool:
    """True when NAFBI-style VFR recoveries are allowed (day/VMC thresholds)."""
    defs = approach_defaults(catalog)
    try:
        ceil_lim = int(defs.get("ifr_ceiling_ft") or 3000)
    except (TypeError, ValueError):
        ceil_lim = 3000
    try:
        vis_lim = float(defs.get("ifr_visibility_sm") or 3.0)
    except (TypeError, ValueError):
        vis_lim = 3.0
    raw = (weather.raw or "").upper()
    if any(tok in raw.split() for tok in ("FG", "FZFG", "TS", "+TSRA", "TSRA")):
        # Fog / thunderstorm — treat as no VFR recovery
        if "FG" in raw.split() or "FZFG" in raw.split():
            return False
    if weather.ceiling_ft is not None and weather.ceiling_ft < ceil_lim:
        return False
    if weather.visibility_sm is not None and weather.visibility_sm < vis_lim:
        return False
    # Broken/overcast without parsed height still present in raw near field elev
    if weather.ceiling_ft is None and re.search(r"\b(BKN|OVC)00[0-2]\d\b", raw):
        return False
    return True


def find_vfr_recovery(
    catalog: dict[str, Any] | None, token: str | None
) -> dict[str, Any] | None:
    if not catalog or not token:
        return None
    want = re.sub(r"[^A-Z0-9]", "", str(token).upper())
    for entry in catalog.get("vfr_recoveries") or []:
        if not isinstance(entry, dict):
            continue
        aliases = [str(entry.get("id") or "")] + [
            str(a) for a in (entry.get("aliases") or [])
        ]
        for a in aliases:
            if re.sub(r"[^A-Z0-9]", "", a.upper()) == want:
                return entry
    return None


def find_instrument_approach(
    catalog: dict[str, Any] | None,
    *,
    runway: str | None = None,
    token: str | None = None,
) -> dict[str, Any] | None:
    if not catalog:
        return None
    entries = [e for e in (catalog.get("instrument") or []) if isinstance(e, dict)]
    if token:
        want = re.sub(r"[^A-Z0-9]", "", str(token).upper())
        for entry in entries:
            aliases = [str(entry.get("id") or "")] + [
                str(a) for a in (entry.get("aliases") or [])
            ]
            for a in aliases:
                if re.sub(r"[^A-Z0-9]", "", a.upper()) == want:
                    return entry
    if runway:
        rwy = normalize_runway(runway) or str(runway)
        side = _runway_side(rwy)
        for entry in entries:
            er = normalize_runway(entry.get("runway")) or str(entry.get("runway") or "")
            if er == rwy or _runway_side(er) == side:
                return entry
    return entries[0] if entries else None


def find_iaf(
    instrument: dict[str, Any] | None,
    token: str | None = None,
    *,
    strict: bool = False,
) -> dict[str, Any] | None:
    """
    IAF entry on this procedure.

    A named token that is not published on this plate returns None (never the
    first IAF). DUDBE cannot be attached to ILS Z — it belongs on HI-TACAN Y.
    With no token, returns the procedure's default IAF.
    """
    if not instrument:
        return None
    iafs = [i for i in (instrument.get("iaf") or []) if isinstance(i, dict)]
    if not iafs:
        return None
    if token:
        want = re.sub(r"[^A-Z0-9]", "", str(token).upper())
        for entry in iafs:
            aliases = [str(entry.get("id") or "")] + [
                str(a) for a in (entry.get("aliases") or [])
            ]
            for a in aliases:
                if re.sub(r"[^A-Z0-9]", "", a.upper()) == want:
                    return entry
        return None
    return iafs[0]


def approach_plan_is_valid(
    plan: dict[str, Any] | None,
    catalog: dict[str, Any] | None,
) -> bool:
    """
    True when the cached plan still matches the published catalog.

    Rejects pre-rebuild leftovers such as DUDBE on HI_ILS_OR_LOC_Z_21L.
    """
    if not isinstance(plan, dict) or not plan:
        return False
    pattern = normalize_recovery_key(plan.get("pattern"))
    if pattern != "instrument":
        # VFR: named recovery must still exist when set.
        vfr_id = str(plan.get("vfr_recovery") or "").strip()
        if vfr_id and catalog and not find_vfr_recovery(catalog, vfr_id):
            return False
        return bool(plan.get("runway") or vfr_id or pattern)
    if not catalog:
        return False
    inst_id = str(plan.get("instrument_id") or "").strip()
    iaf_id = str(plan.get("iaf") or "").strip()
    inst = find_instrument_approach(catalog, token=inst_id) if inst_id else None
    if inst is None:
        return False
    # Catalog id may have been renamed (HI_ILS_OR_LOC_Z → ILS_Z).
    if inst_id and str(inst.get("id") or "") != inst_id:
        return False
    if iaf_id and find_iaf(inst, iaf_id) is None:
        return False
    say = str(plan.get("instrument_say") or "")
    if re.search(r"\bor\s+(localizer|loc)\b", say, flags=re.IGNORECASE):
        return False
    return True


def find_instrument_by_iaf(
    catalog: dict[str, Any] | None,
    token: str | None,
    *,
    runway: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """
    Procedure that actually publishes this IAF, preferring the runway in use.

    Returns (instrument, iaf_entry) or None.
    """
    if not catalog or not token:
        return None
    entries = [e for e in (catalog.get("instrument") or []) if isinstance(e, dict)]
    side = _runway_side(runway) if runway else ""
    hits: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for inst in entries:
        iaf_entry = find_iaf(inst, token, strict=True)
        if iaf_entry:
            hits.append((inst, iaf_entry))
    if not hits:
        return None
    if side:
        for inst, iaf_entry in hits:
            er = normalize_runway(inst.get("runway")) or str(inst.get("runway") or "")
            if _runway_side(er) == side:
                return inst, iaf_entry
    return hits[0]


def _entry_latlon(entry: dict[str, Any] | None) -> tuple[float, float] | None:
    if not isinstance(entry, dict):
        return None
    try:
        return float(entry["lat"]), float(entry["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def coerce_latlon(value: Any) -> tuple[float, float] | None:
    """(lat, lon) from a tuple/list/dict, or None when unusable."""
    if value is None:
        return None
    if isinstance(value, dict):
        return _entry_latlon(value)
    try:
        lat, lon = value  # type: ignore[misc]
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


# A cached position outlives the sortie in flow_state.json, so ignore old fixes
# rather than recovering a fresh flight to where the last one happened to be.
OWNSHIP_FIX_MAX_AGE_S = 300.0


def _ownship_fix_is_fresh(state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict) or not state.get("ownship_ll"):
        return False
    try:
        return (time.time() - float(state.get("ownship_ll_t") or 0)) <= OWNSHIP_FIX_MAX_AGE_S
    except (TypeError, ValueError):
        return False


def nearest_instrument_for_position(
    catalog: dict[str, Any] | None,
    position: Any,
    *,
    runway: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """
    Closest published IAF to the aircraft, restricted to the landing runway.

    Keeps arrivals from the north on the ARCOE plates and arrivals from the
    west on DUDBE instead of always handing out the first plate in the catalog.
    Returns (instrument, iaf) or None when nothing has coordinates.
    """
    pos = coerce_latlon(position)
    if not catalog or pos is None:
        return None
    side = _runway_side(runway) if runway else ""
    best: tuple[float, dict[str, Any], dict[str, Any]] | None = None
    for inst in catalog.get("instrument") or []:
        if not isinstance(inst, dict):
            continue
        er = normalize_runway(inst.get("runway")) or str(inst.get("runway") or "")
        if side and _runway_side(er) != side:
            continue
        for iaf_entry in inst.get("iaf") or []:
            ll = _entry_latlon(iaf_entry)
            if ll is None:
                continue
            d = _haversine_nm(pos[0], pos[1], ll[0], ll[1])
            if best is None or d < best[0]:
                best = (d, inst, iaf_entry)
    if best is None:
        return None
    return best[1], best[2]


def nearest_vfr_recovery_for_position(
    catalog: dict[str, Any] | None,
    position: Any,
    *,
    runway: str | None = None,
) -> dict[str, Any] | None:
    """
    Closest surveyed VFR recovery fix valid for this runway side.

    Unsurveyed entries are skipped. Returns None when no candidate on that
    side has coordinates, so the published priority order still decides.
    """
    pos = coerce_latlon(position)
    if not catalog or pos is None:
        return None
    side = _runway_side(runway) if runway else ""
    best: tuple[float, dict[str, Any]] | None = None
    for entry in catalog.get("vfr_recoveries") or []:
        if not isinstance(entry, dict):
            continue
        sides = [str(s) for s in (entry.get("runway_sides") or [])]
        if side and sides and side not in sides:
            continue
        ll = _entry_latlon(entry)
        if ll is None:
            continue
        d = _haversine_nm(pos[0], pos[1], ll[0], ll[1])
        if best is None or d < best[0]:
            best = (d, entry)
    return best[1] if best else None


def find_hold(catalog: dict[str, Any] | None, token: str | None) -> dict[str, Any] | None:
    if not catalog:
        return None
    holds = [h for h in (catalog.get("holds") or []) if isinstance(h, dict)]
    if not holds:
        return None
    if not token:
        return holds[0]
    want = re.sub(r"[^A-Z0-9]", "", str(token).upper())
    for entry in holds:
        aliases = [str(entry.get("id") or "")] + [
            str(a) for a in (entry.get("aliases") or [])
        ]
        for a in aliases:
            if re.sub(r"[^A-Z0-9]", "", a.upper()) == want:
                return entry
        say = re.sub(r"[^A-Z0-9]", "", str(entry.get("say") or "").upper())
        if say and say == want:
            return entry
    return None


def pick_default_vfr_recovery(
    catalog: dict[str, Any] | None, runway: str
) -> dict[str, Any] | None:
    defs = approach_defaults(catalog)
    side = _runway_side(runway)
    order = list((defs.get("vfr_by_runway_side") or {}).get(side) or [])
    for rid in order:
        found = find_vfr_recovery(catalog, rid)
        if found:
            sides = [str(s) for s in (found.get("runway_sides") or [])]
            if not sides or side in sides:
                return found
    # Fallback: first recovery that matches runway side
    for entry in (catalog or {}).get("vfr_recoveries") or []:
        if not isinstance(entry, dict):
            continue
        sides = [str(s) for s in (entry.get("runway_sides") or [])]
        if not sides or side in sides:
            return entry
    return None


def _route_tail_tokens(route: str | None, *, limit: int = 8) -> list[str]:
    """Last enroute tokens of a filed route (skip arrival ICAO / runway)."""
    tokens = _enroute_tokens(route)
    if not tokens:
        return []
    n = max(1, int(limit))
    return tokens[-n:]


def match_recovery_from_route(
    catalog: dict[str, Any] | None,
    route: str | None,
) -> dict[str, Any] | None:
    """
    Scan the filed route for a VFR recovery fix or an instrument IAF.

    The tail (nearest arrival) wins; if nothing matches there the whole route
    is scanned, so a recovery fix filed mid-route is still honoured.

    A fix can be both — ARCOE is a published VFR recovery *and* the IAF for the
    HI-ILS Z / HI-TACAN Z RWY 21L. Both sides are returned so the caller can use
    whichever the weather calls for.
    """
    if not catalog or not route:
        return None
    defs = approach_defaults(catalog)
    try:
        tail_n = int(defs.get("route_tail_tokens") or 8)
    except (TypeError, ValueError):
        tail_n = 8
    tail = _route_tail_tokens(route, limit=tail_n)
    everything = _enroute_tokens(route)
    if not tail and not everything:
        return None

    seen: set[str] = set()
    # Walk from arrival backward so the fix closest to the field wins.
    for tok in list(reversed(tail)) + list(reversed(everything)):
        key = str(tok).upper()
        if key in seen:
            continue
        seen.add(key)
        vfr = find_vfr_recovery(catalog, tok)
        inst_hit = find_instrument_by_iaf(catalog, tok)
        if not vfr and not inst_hit:
            continue
        hit: dict[str, Any] = {
            "kind": "vfr" if vfr else "iaf",
            "id": str((vfr or inst_hit[1]).get("id") or ""),
            "entry": vfr or inst_hit[1],
        }
        if inst_hit:
            inst, iaf_entry = inst_hit
            hit["iaf_id"] = str(iaf_entry.get("id") or "")
            hit["iaf_entry"] = iaf_entry
            hit["instrument_id"] = str(inst.get("id") or "")
            hit["instrument"] = inst
            if vfr:
                hit["kind"] = "both"
        if vfr:
            hit["vfr_id"] = str(vfr.get("id") or "")
        return hit
    return None


def _int_or(default: int, *candidates: Any) -> int:
    for raw in candidates:
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return default


def _route_fix_conflicts_plan(
    plan: dict[str, Any],
    route_hit: dict[str, Any] | None,
    *,
    vmc: bool,
) -> bool:
    """True when the filed route names a recovery the cached plan ignored."""
    if not route_hit or not plan:
        return False
    source = str(plan.get("source") or "")
    # Pilot/voice overrides stay until the pilot changes them.
    if source in ("override", "request"):
        return False
    kind = str(route_hit.get("kind") or "")
    if vmc and kind in ("vfr", "both"):
        want = str(route_hit.get("vfr_id") or "")
        return bool(want) and want != str(plan.get("vfr_recovery") or "")
    if (not vmc) or kind == "iaf":
        want = str(route_hit.get("iaf_id") or route_hit.get("vfr_id") or "")
        have = str(plan.get("iaf") or plan.get("vfr_recovery") or "")
        return bool(want) and want != have
    return False


def assign_approach_plan(
    airport: dict[str, Any],
    weather: Weather,
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    opus: OpusFlightContext | None = None,
    force: bool = False,
    recovery: str | None = None,
    vfr_recovery: str | None = None,
    instrument_id: str | None = None,
    iaf: str | None = None,
    position: Any = None,
) -> dict[str, Any]:
    """
    Build / refresh the Approach assignment in mission state.

    Priority for which recovery/IAF:
      1. Explicit pilot/voice/Fly fix override (vfr_recovery / iaf / instrument_id)
      2. Named fix in the Opus filed route (always re-checked)
      3. Published fix nearest the aircraft (CAOC position)
      4. METAR VMC → runway-side VFR default; IFR → instrument + IAF

    A recovery *pattern* alone (tactical_overhead / instrument) does not suppress
    the flight-plan scan — only an explicit fix name does.

    Descend / speed come from the chosen recovery or instrument entry
    (not a single global 10k/300), capped by filed altitude when known.
    """
    catalog = load_approach_catalog(airport)
    defs = approach_defaults(catalog)
    st = state if isinstance(state, dict) else {}
    # Pattern-only kwargs still allow the filed route to pick the fix.
    explicit_fix = bool(vfr_recovery or instrument_id or iaf)
    route = (opus.fp_route_string if opus else None) or None
    route_hit = (
        None if explicit_fix else match_recovery_from_route(catalog, route)
    )
    vmc_now = is_vfr_recovery_weather(weather, catalog=catalog)

    if (
        not force
        and st.get("approach_assigned")
        and not explicit_fix
        and recovery is None
    ):
        plan = dict(st.get("approach_plan") or {})
        # Drop catalog leftovers (DUDBE on HI-ILS Z) and rebuild from route/position.
        if not approach_plan_is_valid(plan, catalog):
            return assign_approach_plan(
                airport,
                weather,
                mission=mission,
                state=state,
                opus=opus,
                force=True,
                recovery=str(plan.get("pattern") or st.get("active_recovery") or "")
                or None,
                position=position,
            )
        # Filed route changed, or a position/default plan ignored the FP fix.
        route_changed = bool(route) and str(plan.get("fp_route") or "") != str(route)
        if route_changed or _route_fix_conflicts_plan(plan, route_hit, vmc=vmc_now):
            return assign_approach_plan(
                airport,
                weather,
                mission=mission,
                state=state,
                opus=opus,
                force=True,
                recovery=str(plan.get("pattern") or "") or None,
                position=position,
            )
        # Stale plan after a wind shift (e.g. still on 03 with 081/07) — re-pick
        # the runway side unless the pilot/Setup pinned a runway.
        if plan and not requested_runway(mission=mission, state=state):
            instrument = normalize_recovery_key(plan.get("pattern")) == "instrument"
            wind_rwy = pick_recovery_runway(
                airport, weather, instrument=instrument, catalog=catalog
            )
            if _runway_side(wind_rwy) != _runway_side(str(plan.get("runway") or "")):
                return assign_approach_plan(
                    airport,
                    weather,
                    mission=mission,
                    state=state,
                    opus=opus,
                    force=True,
                    recovery=str(plan.get("pattern") or "") or None,
                    position=position,
                )
        return plan

    if position is None and _ownship_fix_is_fresh(st):
        position = st.get("ownship_ll")
    position = coerce_latlon(position)

    if route_hit:
        kind = str(route_hit.get("kind") or "")
        if route_hit.get("vfr_id"):
            vfr_recovery = str(route_hit.get("vfr_id") or "") or vfr_recovery
        # A filed fix that is also an IAF carries its own plate, so an IMC
        # recovery uses the fix the pilot actually filed (ARCOE stays ARCOE).
        if route_hit.get("iaf_id"):
            iaf = str(route_hit.get("iaf_id") or "") or iaf
            instrument_id = str(route_hit.get("instrument_id") or "") or instrument_id
        if kind == "iaf" and not recovery:
            recovery = "instrument"

    # Explicit pattern request, else keep prior, else weather / route default
    if recovery:
        pattern = normalize_recovery_key(recovery)
    elif st.get("active_recovery") and any((vfr_recovery, instrument_id, iaf)):
        pattern = normalize_recovery_key(st.get("active_recovery"))
    elif not force and st.get("active_recovery") and st.get("approach_assigned"):
        pattern = normalize_recovery_key(st.get("active_recovery"))
    else:
        vmc = is_vfr_recovery_weather(weather, catalog=catalog)
        # Route-named IAF already forced instrument above; VFR route hit stays VMC.
        if route_hit and str(route_hit.get("kind")) in ("vfr", "both") and vmc:
            pattern = normalize_recovery_key(
                defs.get("default_pattern_vmc"), default=DEFAULT_RECOVERY
            )
        else:
            pattern = normalize_recovery_key(
                defs.get("default_pattern_vmc")
                if vmc
                else defs.get("default_pattern_imc"),
                default=DEFAULT_RECOVERY if vmc else "instrument",
            )
            if not vmc:
                pattern = "instrument"

    # An explicitly *requested* VFR recovery implies a VMC pattern. Filing one
    # in the route does not — weather still decides whether you get it.
    if vfr_recovery and pattern == "instrument" and not recovery and not route_hit:
        pattern = normalize_recovery_key(
            defs.get("default_pattern_vmc"), default=DEFAULT_RECOVERY
        )

    instrument = pattern == "instrument"
    rwy = pick_recovery_runway(
        airport, weather, instrument=instrument, catalog=catalog
    )
    # Honor pilot runway request if set
    req = requested_runway(mission=mission, state=state)
    if req:
        rwy = align_runway_to_airport(airport, req, instrument=instrument)

    plan: dict[str, Any] = {
        "pattern": pattern,
        "runway": rwy,
        "vfr_recovery": None,
        "vfr_recovery_say": None,
        "direct_fix": None,
        "direct_say": None,
        "instrument_id": None,
        "instrument_say": None,
        "plate": None,
        "iaf": None,
        "iaf_say": None,
        "iaf_source": None,
        "iaf_altitude_type": None,
        "vmc": is_vfr_recovery_weather(weather, catalog=catalog),
        "source": (
            "override"
            if explicit_fix and not route_hit
            else ("route" if route_hit else "weather")
        ),
        "fp_route": route,
        "route_fix": str(
            (route_hit or {}).get("vfr_id")
            or (route_hit or {}).get("iaf_id")
            or (route_hit or {}).get("id")
            or ""
        )
        or None,
    }

    # Altitude from recovery / plate IAF. Speed only when traffic restricts.
    plan["descend_ft"] = _int_or(10000, defs.get("descend_ft"))
    plan["speed_kt"] = None
    plan["speed_restrict"] = bool(st.get("speed_restrict"))

    if instrument:
        inst = None
        iaf_entry = None
        iaf_source = "default"
        # A named IAF picks its own plate — DUDBE is HI-TACAN Y 21L, ARCOE is
        # ILS/TACAN Z 21L. A mismatched instrument_id+IAF pair (old sticky
        # HI-ILS Z + DUDBE) is resolved by the IAF, not the plate name.
        if iaf:
            hit = find_instrument_by_iaf(catalog, iaf, runway=rwy)
            if hit:
                by_iaf_inst, by_iaf_entry = hit
                if not instrument_id:
                    inst, iaf_entry = by_iaf_inst, by_iaf_entry
                    iaf_source = "route" if route_hit else "request"
                else:
                    named = find_instrument_approach(
                        catalog, runway=rwy, token=instrument_id
                    )
                    if named is None or find_iaf(named, iaf) is None:
                        inst, iaf_entry = by_iaf_inst, by_iaf_entry
                        iaf_source = "route" if route_hit else "request"
                    else:
                        inst, iaf_entry = named, find_iaf(named, iaf)
                        iaf_source = "route" if route_hit else "request"
        if inst is None and iaf:
            iaf_source = "route" if route_hit else "request"
        # Nothing filed or requested — hand out the plate whose IAF the jet is
        # actually closest to instead of the first one in the catalog.
        if (
            inst is None
            and not iaf
            and not instrument_id
            and not route_hit
            and position
        ):
            near = nearest_instrument_for_position(catalog, position, runway=rwy)
            if near:
                inst, iaf_entry = near
                iaf_source = "position"
                plan["source"] = "position"
        if inst is None:
            inst = find_instrument_approach(
                catalog, runway=rwy, token=instrument_id
            ) or find_instrument_approach(catalog, runway=rwy)
        if inst:
            plan["instrument_id"] = str(inst.get("id") or "")
            # One procedure only (ILS *or* LOC) — never "ILS or localizer".
            plan["instrument_say"] = str(
                inst.get("clearance_say") or inst.get("say") or plan["instrument_id"]
            )
            plan["instrument_procedure"] = str(inst.get("procedure") or "ILS")
            plan["plate"] = str(inst.get("plate") or "") or None
            if inst.get("runway"):
                plan["runway"] = align_runway_to_airport(
                    airport, str(inst["runway"]), instrument=True
                )
            if iaf_entry is None:
                iaf_entry = find_iaf(inst, iaf) if iaf else find_iaf(inst)
            if iaf_entry:
                plan["iaf"] = str(iaf_entry.get("id") or "")
                plan["iaf_say"] = str(iaf_entry.get("say") or plan["iaf"])
                plan["iaf_source"] = iaf_source
                plan["iaf_altitude_type"] = str(
                    iaf_entry.get("altitude_type") or "at_or_above"
                )
                try:
                    plan["iaf_lat"] = float(iaf_entry["lat"])
                    plan["iaf_lon"] = float(iaf_entry["lon"])
                except (KeyError, TypeError, ValueError):
                    plan.pop("iaf_lat", None)
                    plan.pop("iaf_lon", None)
                # Plate IAF crossing altitude is the descend clearance.
                plan["descend_ft"] = _int_or(
                    plan["descend_ft"],
                    iaf_entry.get("altitude_ft"),
                    inst.get("descend_ft"),
                    defs.get("descend_ft"),
                )
            else:
                plan["descend_ft"] = _int_or(
                    plan["descend_ft"], inst.get("descend_ft"), defs.get("descend_ft")
                )
    else:
        vfr = find_vfr_recovery(catalog, vfr_recovery) if vfr_recovery else None
        # Position never outranks a filed recovery fix.
        if vfr is None and not route_hit and position:
            vfr = nearest_vfr_recovery_for_position(
                catalog, position, runway=plan["runway"]
            )
            if vfr:
                plan["source"] = "position"
        if vfr is None:
            vfr = pick_default_vfr_recovery(catalog, plan["runway"])
        if vfr:
            plan["vfr_recovery"] = str(vfr.get("id") or "")
            plan["vfr_recovery_say"] = str(vfr.get("say") or plan["vfr_recovery"])
            if vfr.get("default_pattern") and not recovery:
                plan["pattern"] = normalize_recovery_key(
                    vfr.get("default_pattern"), default=plan["pattern"]
                )
            plan["descend_ft"] = _int_or(
                plan["descend_ft"], vfr.get("descend_ft"), defs.get("descend_ft")
            )
            plan["direct_fix"] = str(
                vfr.get("direct_fix") or vfr.get("id") or ""
            ) or None
            plan["direct_say"] = str(
                vfr.get("direct_say") or vfr.get("say") or plan["direct_fix"] or ""
            ) or None
            try:
                plan["fix_lat"] = float(vfr["lat"])
                plan["fix_lon"] = float(vfr["lon"])
            except (KeyError, TypeError, ValueError):
                plan.pop("fix_lat", None)
                plan.pop("fix_lon", None)
            # Route-named recovery on 03-only (MINTT): keep wind gate — may stay 21.
            sides = [str(s) for s in (vfr.get("runway_sides") or [])]
            side = _runway_side(plan["runway"])
            if sides and side not in sides and not req:
                if "21" in sides:
                    plan["runway"] = align_runway_to_airport(
                        airport, "21R", instrument=False
                    )
                # else leave wind-picked runway (do not force 03 in light wind)

    # Traffic speed restriction only — never a routine "maintain 300".
    if plan.get("speed_restrict"):
        plan["speed_kt"] = _int_or(300, st.get("speed_kt"), defs.get("speed_kt"))

    # Never clear above filed altitude when Opus has one.
    filed_ft = filed_altitude_feet(opus.fp_altitude if opus else None)
    if filed_ft is not None and plan["descend_ft"] > filed_ft:
        plan["descend_ft"] = filed_ft

    if state is not None:
        state["approach_assigned"] = True
        state["approach_plan"] = plan
        state["active_recovery"] = plan["pattern"]
        state["active_vfr_recovery"] = plan.get("vfr_recovery")
        state["active_instrument"] = plan.get("instrument_id")
        state["active_iaf"] = plan.get("iaf")
        state["approach_runway"] = plan.get("runway")
        state["hold_active"] = bool(state.get("hold_active"))
        state["vectors_active"] = bool(state.get("vectors_active"))
    return plan


def approach_plan_from_state(
    state: dict[str, Any] | None,
    *,
    airport: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    plan = state.get("approach_plan")
    if not isinstance(plan, dict):
        return {}
    out = dict(plan)
    if airport is not None:
        catalog = load_approach_catalog(airport)
        if not approach_plan_is_valid(out, catalog):
            return {}
    return out


def approach_exit_fix_latlon(
    plan: dict[str, Any] | None,
    *,
    airport: dict[str, Any] | None = None,
) -> tuple[float, float] | None:
    """
    IAF (instrument) or VFR recovery / exit fix coordinates for the active plan.
    """
    p = dict(plan or {})
    for lat_k, lon_k in (("iaf_lat", "iaf_lon"), ("fix_lat", "fix_lon")):
        try:
            lat = float(p[lat_k])
            lon = float(p[lon_k])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(lat) <= 90 and abs(lon) <= 180:
            return lat, lon
    if airport is None:
        return None
    catalog = load_approach_catalog(airport)
    if not catalog:
        return None
    iaf_id = str(p.get("iaf") or "").strip()
    inst_id = str(p.get("instrument_id") or "").strip()
    if iaf_id and inst_id:
        inst = find_instrument_approach(catalog, token=inst_id)
        entry = find_iaf(inst, iaf_id) if inst else None
        if entry:
            try:
                return float(entry["lat"]), float(entry["lon"])
            except (KeyError, TypeError, ValueError):
                pass
    vfr_id = str(p.get("vfr_recovery") or p.get("direct_fix") or "").strip()
    if vfr_id:
        vfr = find_vfr_recovery(catalog, vfr_id)
        if vfr:
            try:
                return float(vfr["lat"]), float(vfr["lon"])
            except (KeyError, TypeError, ValueError):
                pass
    return None


# Auto approach clearance: shortly after check-in, while still inbound to the fix.
APPROACH_CLEARANCE_GAP_S = 6.0
# If closer than this to the IAF / exit fix, still fire (about to arrive) —
# clearance is meant to come before the fix, not wait until you get there.
APPROACH_CLEARANCE_AT_FIX_NM = 3.0


def approach_clearance_auto_ready(
    *,
    airport: dict[str, Any] | None,
    state: dict[str, Any] | None,
    config: dict[str, Any] | None = None,
    callsign: str | None = None,
    opus: OpusFlightContext | None = None,
    gap_s: float | None = None,
) -> tuple[bool, str]:
    """
    True when Approach may auto-issue the procedure clearance.

    After check-in (cursor on approach_procedure): wait a short radio gap, then
    fire while still inbound — do not wait until the jet reaches the IAF / exit.
    Holding suspends auto-clearance until the hold is cancelled.
    """
    st = state if isinstance(state, dict) else {}
    hold = auto_tx_hold_reason(st)
    if hold:
        return False, hold
    if st.get("hold_active"):
        return False, "holding - cancel hold for approach clearance"
    gap = float(APPROACH_CLEARANCE_GAP_S if gap_s is None else gap_s)
    if gap > 0:
        try:
            last = float(st.get("last_tx_at") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        if last > 0.0:
            left = gap - (time.time() - last)
            if left > 0:
                return False, f"approach clearance in {left:.0f}s"

    plan = approach_plan_from_state(st, airport=airport)
    fix = approach_exit_fix_latlon(plan, airport=airport)
    if fix is None:
        return True, "after check-in"
    pos = ownship_latlon(config, callsign=callsign, opus=opus, state=st)
    if pos is None:
        return True, "after check-in (no position)"
    dist = _haversine_nm(pos[0], pos[1], fix[0], fix[1])
    name = str(
        plan.get("iaf_say")
        or plan.get("iaf")
        or plan.get("direct_say")
        or plan.get("vfr_recovery_say")
        or plan.get("vfr_recovery")
        or "fix"
    )
    # Still inbound or already at the fix — either way, clear now (before / at).
    if dist > APPROACH_CLEARANCE_AT_FIX_NM:
        return True, f"{dist:.1f} NM to {name} — clearance"
    return True, f"{dist:.1f} NM to {name} — clearance (near fix)"


def _enroute_tokens(route: str | None, *, dep_icao: str | None = None) -> list[str]:
    tokens = parse_route_tokens(route)
    if not tokens:
        return []
    start = 0
    first = _normalize_dep_token(tokens[0])
    if dep_icao and first == dep_icao.strip().upper():
        start = 1
    elif _looks_like_icao(tokens[0]):
        start = 1
    end = len(tokens)
    if end > start and _RUNWAY_TOKEN.match(tokens[end - 1]):
        end -= 1
    if end > start and _looks_like_icao(tokens[end - 1]):
        end -= 1
    return [_normalize_dep_token(t) for t in tokens[start:end]]


@dataclass
class DepartureMatch:
    """Resolved DP / visual departure + optional transition for clearance speech."""

    instrument_id: str | None = None
    instrument_say: str | None = None
    visual_id: str | None = None
    visual_say: str | None = None
    transition_id: str | None = None
    transition_say: str | None = None
    runway: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.instrument_say or self.visual_say)


def match_departure(airport: dict[str, Any], route: str | None) -> DepartureMatch:
    """
    Match filed route against airport departure catalog.

    Instrument SIDs (DREAM7 / FYTTR7 / MMM8) only when that token is explicitly filed.
    Any FLEX / FLEX21R / FLEX21L / FLEX03R / FLEX03L token is a visual departure;
    north/west inferred from the next fix (DREAM/MINTT/JUNNO vs FYTTR).
    Bare fixes alone do not invent a SID — stay 'as filed'.
    """
    catalog = load_departure_catalog(airport)
    result = DepartureMatch()
    result.runway = runway_from_route(route)
    if not catalog or not route:
        sid = filed_sid(airport, route)
        if sid:
            result.instrument_id = sid
            result.instrument_say = speak_fix(sid)
        return result

    dep_icao = str(airport.get("icao") or "").strip().upper() or None
    norms = _enroute_tokens(route, dep_icao=dep_icao)
    if not norms:
        return result
    global_transitions = {
        _normalize_dep_token(k): str(v)
        for k, v in (catalog.get("transitions") or {}).items()
    }

    # --- Instrument SID: exact alias only (must be filed, e.g. DREAM7) ---
    for entry in catalog.get("instrument") or []:
        aliases = {_normalize_dep_token(a) for a in (entry.get("aliases") or [])}
        if not aliases.intersection(norms):
            continue
        result.instrument_id = str(entry.get("id"))
        result.instrument_say = str(entry.get("say") or entry.get("id"))
        for tok in norms:
            for tk, say in (entry.get("transitions") or {}).items():
                if tok == _normalize_dep_token(tk) and tok not in aliases:
                    result.transition_id = str(tk)
                    result.transition_say = str(say)
                    break
            if result.transition_say:
                break
        break

    # --- Visual Flex: FLEX / FLEX21R / aliases / prefixes ---
    flex_idx = next((i for i, tok in enumerate(norms) if _is_flex_dep_token(tok)), None)
    after_flex = norms[flex_idx + 1 :] if flex_idx is not None else norms

    vis_hit: tuple[int, dict[str, Any]] | None = None
    for entry in catalog.get("visual") or []:
        score = 0
        aliases = {_normalize_dep_token(a) for a in (entry.get("aliases") or [])}
        if aliases.intersection(norms):
            score = 200
        for prefix in entry.get("route_prefixes") or []:
            pref = [_normalize_dep_token(p) for p in prefix]
            if not pref:
                continue
            # Exact: FLEX + MINTT
            if norms[: len(pref)] == pref:
                score = max(score, len(pref) * 40)
            # FLEX21R.DREAM — FLEX* then prefix tail
            if (
                flex_idx is not None
                and len(pref) >= 2
                and pref[0] == "FLEX"
                and after_flex[: len(pref) - 1] == pref[1:]
            ):
                score = max(score, 150 + len(pref))
            # Transition hint after any FLEX* (DREAM → Flex north)
            if flex_idx is not None and len(pref) >= 2 and pref[1] in after_flex:
                score = max(score, 120)
        if flex_idx is not None and score == 0 and entry.get("id") == "FLEX_GENERIC":
            score = 50
        if score and (vis_hit is None or score > vis_hit[0]):
            vis_hit = (score, entry)

    if flex_idx is not None and vis_hit is None:
        for entry in catalog.get("visual") or []:
            if entry.get("id") == "FLEX_GENERIC" or str(entry.get("say") or "").casefold() == "flex":
                vis_hit = (50, entry)
                break
        if vis_hit is None:
            result.visual_id = "FLEX"
            result.visual_say = "Flex"

    if vis_hit:
        entry = vis_hit[1]
        result.visual_id = str(entry.get("id"))
        result.visual_say = str(entry.get("say") or entry.get("id"))
        if not result.transition_say:
            scan = after_flex if flex_idx is not None else norms
            for tok in scan:
                if tok in global_transitions and not _is_flex_dep_token(tok):
                    if result.instrument_id and tok in {
                        _normalize_dep_token(a)
                        for a in (
                            next(
                                (
                                    e.get("aliases") or []
                                    for e in (catalog.get("instrument") or [])
                                    if e.get("id") == result.instrument_id
                                ),
                                [],
                            )
                        )
                    }:
                        continue
                    result.transition_id = tok
                    result.transition_say = global_transitions[tok]
                    break
            if not result.transition_say:
                default_tr = entry.get("default_transition")
                if default_tr:
                    key = _normalize_dep_token(str(default_tr))
                    if key in global_transitions:
                        result.transition_id = str(default_tr)
                        result.transition_say = global_transitions[key]

    # Drop redundant transitions ("Fighter seven" + "Fighter transition")
    if (
        result.instrument_say
        and result.transition_say
        and result.transition_say.casefold() in result.instrument_say.casefold()
    ):
        result.transition_id = None
        result.transition_say = None

    return result


def speak_departure_clearance(airport: dict[str, Any], route: str | None) -> str | None:
    """
    DP / transition clause for clearance (no leading 'via').
    Examples:
      Flex north, Dream transition
      Flex west, Fighter transition
      Dream seven departure, Mintt transition
      Mormon Mesa eight departure
    """
    m = match_departure(airport, route)
    if not m.found:
        return None

    if m.visual_say and m.instrument_say and m.transition_say:
        return f"{m.visual_say}, {m.instrument_say} departure, {m.transition_say} transition"
    if m.visual_say and m.instrument_say:
        return f"{m.visual_say}, {m.instrument_say} departure"
    if m.instrument_say and m.transition_say:
        return f"{m.instrument_say} departure, {m.transition_say} transition"
    if m.instrument_say:
        return f"{m.instrument_say} departure"
    if m.visual_say and m.transition_say:
        return f"{m.visual_say}, {m.transition_say} transition"
    if m.visual_say:
        return f"{m.visual_say} departure"
    return None


def resolve_taxi_route(
    airport: dict[str, Any],
    runway: str,
    *,
    opus: OpusFlightContext | None = None,
) -> dict[str, str]:
    """
    Runway-dependent EOR / taxi via for Nellis-style fields.
    21R → NW EOR via F, E; 03L → Alpha South via Foxtrot.

    Parking uses squadron overrides when known (see resolve_parking).
    """
    rwy = normalize_runway(runway) or str(runway or "").strip().upper()
    routes = airport.get("taxi_routes")
    route: dict[str, Any] = {}
    if isinstance(routes, dict) and rwy:
        raw = routes.get(rwy) or routes.get(rwy.replace("L", "").replace("R", ""))
        if isinstance(raw, dict):
            route = raw
        else:
            # case-insensitive key match
            for key, val in routes.items():
                if normalize_runway(str(key)) == rwy and isinstance(val, dict):
                    route = val
                    break
    eor = str(route.get("eor") or "").strip()
    outbound = str(route.get("outbound_via") or "").strip()
    inbound = str(route.get("inbound_via") or "").strip()
    exit_via = str(route.get("exit") or "").strip()
    intersection = str(route.get("intersection") or "").strip()
    legacy = str(airport.get("taxi_via") or "Foxtrot").strip() or "Foxtrot"
    parking = resolve_parking(airport, opus=opus)
    if not eor:
        eor = "NW EOR" if rwy.startswith("21") else "Alpha South"
    if not outbound:
        outbound = legacy
    if not inbound:
        inbound = legacy
    if not exit_via:
        exit_via = "right at Alpha" if rwy.startswith("21") else "left at Alpha"
    if not intersection:
        intersection = "Delta" if rwy.startswith("21") else "Bravo"
    return {
        "eor": eor,
        "outbound_via": outbound,
        "inbound_via": inbound,
        "exit": exit_via,
        "parking": parking,
        "intersection": intersection,
    }


def resolve_parking(
    airport: dict[str, Any],
    *,
    opus: OpusFlightContext | None = None,
    squadron_name: str | None = None,
) -> str:
    """
    Parking / ramp for taxi-in.

    Uses airport.parking_by_squadron match list when the Opus squadron is known,
    else airport.parking (Nellis default Ramp).
    """
    default = str(airport.get("parking") or "parking").strip() or "parking"
    sq = (
        str(squadron_name or "").strip()
        or (str(opus.squadron_name).strip() if opus and opus.squadron_name else "")
    )
    rules = airport.get("parking_by_squadron")
    if not sq or not isinstance(rules, list):
        return default
    hay = sq.casefold()
    # Prefer longer / more specific match tokens first.
    scored: list[tuple[int, str]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        parking = str(rule.get("parking") or "").strip()
        if not parking:
            continue
        raw_match = rule.get("match")
        tokens: list[str] = []
        if isinstance(raw_match, str):
            tokens = [raw_match]
        elif isinstance(raw_match, list):
            tokens = [str(t) for t in raw_match if str(t).strip()]
        for tok in tokens:
            t = tok.strip().casefold()
            if not t:
                continue
            if t in hay:
                scored.append((len(t), parking))
    if not scored:
        return default
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def speak_local_preset(airport: dict[str, Any], channel: str) -> str | None:
    """
    Speak a UHF Local channel when set, e.g.:
      'Local three'  or  'Local button three'
    """
    block = airport.get(channel) or {}
    preset = block.get("local_preset")
    if preset is None:
        return None
    try:
        n = int(preset)
    except (TypeError, ValueError):
        return None
    # Channel/Local numbers are conversational (three), not ICAO digit speech (tree).
    natural = {
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
        14: "fourteen",
        15: "fifteen",
        20: "twenty",
    }
    spoken = natural.get(n) or speak_digits(str(n))
    return _pick(f"Local {spoken}", f"Local button {spoken}")


def random_initial_climb_feet(fixed: int | None = None) -> int:
    """Initial climb between 12,000 and 17,000 ft (1,000 ft steps). Reuse fixed when set."""
    if fixed is not None:
        try:
            n = int(fixed)
            # Allow sticky/PDF climbs outside the random band (e.g. FL180–FL200)
            if 5000 <= n <= 45000:
                return n
        except (TypeError, ValueError):
            pass
    return random.randint(12, 17) * 1000


def filed_altitude_feet(fp_altitude: str | None) -> int | None:
    """
    Opus filed altitude → feet.

    Values under 1000 are flight levels (220 → 22,000). Larger values are feet.
    """
    if fp_altitude is None or str(fp_altitude).strip() == "":
        return None
    raw = str(fp_altitude).strip().upper().replace("FL", "").replace("FT", "")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    try:
        n = int(digits)
    except ValueError:
        return None
    if n < 1000:
        return n * 100
    if 5000 <= n <= 45000:
        return n
    return None


def cruise_climb_target_ft(
    opus: OpusFlightContext | None = None,
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> int | None:
    """
    Filed cruise when it is higher than the interim Departure climb.

    None → skip the cruise-amendment step (already at cruise, or no FP).
    """
    filed = filed_altitude_feet(opus.fp_altitude if opus else None)
    if filed is None:
        filed = _parse_climb_ft((state or {}).get("filed_altitude_ft"))
    if filed is None:
        return None
    interim = resolve_shared_climb_ft(step=None, mission=mission, state=state)
    if interim is not None and filed <= interim:
        return None
    return filed


def should_skip_cruise_climb_step(
    step: dict[str, Any] | None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    opus: OpusFlightContext | None = None,
) -> bool:
    """True when there is no higher filed altitude to clear."""
    if str((step or {}).get("template") or "") != "climb_cruise":
        return False
    return cruise_climb_target_ft(opus, mission=mission, state=state) is None


def unrestricted_climb_ceiling_ft(opus: OpusFlightContext | None) -> int | None:
    """
    Unrestricted climb ceiling = filed altitude (never higher).

    Returns None when there is no filed altitude to clear to.
    """
    return filed_altitude_feet(opus.fp_altitude if opus else None)


def speak_unrestricted_ceiling(
    climb_ft: int | None,
    fp_altitude: str | None = None,
) -> str | None:
    """Filed altitude / ceiling — FL when at or above 18k."""
    if fp_altitude:
        spoken = speak_filed_altitude(fp_altitude)
        if spoken:
            return spoken
    if climb_ft is None:
        return None
    return speak_altitude_value(str(climb_ft))


# Templates that share one interim climb (Delivery clearance ↔ Departure/Center)
CLIMB_SHARED_TEMPLATES = frozenset({"clearance", "radar_contact", "center_radar"})
# After radar contact, Departure amends to filed cruise before the 18 NM handoff.
CRUISE_CLIMB_BEYOND_NM = 10.0

DEFAULT_UNRESTRICTED_CLIMB_APPROVE_CHANCE = 0.80
_TAKEOFF_CLEARANCE_TEMPLATES = frozenset(
    {"clear_takeoff", "clear_takeoff_rolling", "clear_takeoff_intersection"}
)


def unrestricted_climb_enabled(
    config: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
) -> bool:
    for src in (mission, config):
        if not src:
            continue
        if "unrestricted_climb_enabled" in src:
            return bool(src.get("unrestricted_climb_enabled"))
    return True


def unrestricted_climb_approve_chance(
    config: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
) -> float:
    for src in (mission, config):
        if not src:
            continue
        try:
            n = float(src.get("unrestricted_climb_approve_chance"))
            return max(0.0, min(1.0, n))
        except (TypeError, ValueError, AttributeError):
            continue
    return DEFAULT_UNRESTRICTED_CLIMB_APPROVE_CHANCE


def unrestricted_climb_window_open(state: dict[str, Any] | None = None) -> bool:
    """True until Tower has already issued takeoff clearance."""
    if not state:
        return True
    last = str(state.get("last_tx_template") or "").strip()
    if last in _TAKEOFF_CLEARANCE_TEMPLATES:
        return False
    return True


def build_unrestricted_climb_standby(callsign: str) -> str:
    """Immediate Tower reply while coordinating with approach."""
    cs = speak_callsign(callsign)
    return f"{cs}, on request, standby."


def build_unrestricted_climb_unable(callsign: str) -> str:
    """Follow-up when approach cannot approve unrestricted."""
    cs = speak_callsign(callsign)
    return f"{cs}, unable unrestricted climb."


def unrestricted_climb_prefix(
    state: dict[str, Any] | None,
    opus: OpusFlightContext | None = None,
) -> str:
    """
    Leading clause for takeoff clearance when unrestricted was approved.
    'climb unrestricted up to flight level two two zero, '
    """
    if not state or str(state.get("unrestricted_climb") or "") != "approved":
        return ""
    climb_ft = _parse_climb_ft(state.get("unrestricted_climb_ft"))
    climb = speak_unrestricted_ceiling(
        climb_ft, opus.fp_altitude if opus else None
    )
    if not climb:
        return ""
    return f"climb unrestricted up to {climb}, "


def _parse_climb_ft(raw: Any) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if 5000 <= n <= 45000:
        return n
    return None


def resolve_shared_climb_ft(
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> int | None:
    """
    Shared interim climb for Delivery / Departure radar / Center.

    Prefer Fly/state (e.g. unrestricted approval), then mission, then this step,
    then any sibling climb step.
    """
    if state is not None:
        for key in ("unrestricted_climb_ft", "initial_climb_ft"):
            n = _parse_climb_ft(state.get(key))
            if n is not None:
                return n
    if mission is not None:
        n = _parse_climb_ft(mission.get("initial_climb_ft"))
        if n is not None:
            return n
    if step is not None:
        n = _parse_climb_ft(step.get("initial_climb_ft"))
        if n is not None:
            return n
    if mission is not None and isinstance(mission.get("steps"), list):
        for peer in mission["steps"]:
            if not isinstance(peer, dict):
                continue
            if (peer.get("template") or "") not in CLIMB_SHARED_TEMPLATES:
                continue
            n = _parse_climb_ft(peer.get("initial_climb_ft"))
            if n is not None:
                return n
    return None


def stick_shared_climb_ft(
    climb_ft: int,
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
) -> None:
    """Write climb to mission + all Delivery/Departure/Center climb steps."""
    n = _parse_climb_ft(climb_ft)
    if n is None:
        return
    if mission is not None:
        mission["initial_climb_ft"] = n
        steps = mission.get("steps")
        if isinstance(steps, list):
            for peer in steps:
                if isinstance(peer, dict) and (peer.get("template") or "") in CLIMB_SHARED_TEMPLATES:
                    peer["initial_climb_ft"] = n
    if step is not None:
        step["initial_climb_ft"] = n


# Sticky sortie fields that survive Fly Reset today and make the next flight
# reuse unrestricted climb / approach / runway choices from the last one.
_SORTIE_STATE_CACHE_KEYS = (
    "unrestricted_climb",
    "unrestricted_climb_ft",
    "pending_unrestricted_climb",
    "initial_climb_ft",
    "filed_altitude_ft",
    "approach_assigned",
    "approach_plan",
    "active_recovery",
    "active_vfr_recovery",
    "active_instrument",
    "active_iaf",
    "approach_runway",
    "hold_active",
    "vectors_active",
    "requested_runway",
    "active_takeoff_mode",
    "pending_takeoff_offer",
    "takeoff_offer_rolled",
    "landing_cleared_seats",
    "landing_ships_total",
    "_landing_clear_built_seat",
    "landing_intent",
    "awaiting_on_the_go",
    "go_around_plan",
    "rearm_tower_outside_nm",
    "pattern_land_needs_leave",
    "overhead_recovery",
    "range_exit_approved",
)


def clear_sticky_climb(mission: dict[str, Any] | None = None) -> None:
    """Remove shared / unrestricted climb stickiness from the mission timeline."""
    if not isinstance(mission, dict):
        return
    mission.pop("initial_climb_ft", None)
    steps = mission.get("steps")
    if not isinstance(steps, list):
        return
    for peer in steps:
        if isinstance(peer, dict) and (peer.get("template") or "") in CLIMB_SHARED_TEMPLATES:
            peer.pop("initial_climb_ft", None)


def clear_flight_session_cache(
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    airport: dict[str, Any] | None = None,
    weather: Weather | None = None,
    opus: OpusFlightContext | None = None,
    reset_runway: bool = True,
    invalidate_lookups: bool = True,
) -> dict[str, Any]:
    """
    Drop sticky flight-session cache so the next sortie starts clean.

    Clears unrestricted climb, shared initial climb, approach assignment,
    pilot runway request, and takeoff-offer stickiness. Optionally re-picks
    runway from winds and refreshes Opus/METAR lookups.
    """
    if invalidate_lookups:
        invalidate_flight_lookups()
    if config is not None:
        config["runway_override"] = ""
    clear_sticky_climb(mission)
    set_requested_runway(None, mission=mission, state=state)
    if isinstance(state, dict):
        for key in _SORTIE_STATE_CACHE_KEYS:
            state.pop(key, None)
        state["hold_active"] = False
        state["vectors_active"] = False
        state["takeoff_offer_rolled"] = False
        state["pending_takeoff_offer"] = None
        state["active_takeoff_mode"] = None
        state["pending_unrestricted_climb"] = False
    rwy = ""
    if reset_runway and airport is not None and weather is not None:
        rwy = reset_runway_to_winds(
            airport,
            weather,
            mission=mission,
            state=state,
            opus=opus,
            config=config,
        )
    return {"runway": rwy}


def speak_departure_freq_or_local(airport: dict[str, Any]) -> str:
    """
    When Opus local_preset is set, randomly pick:
      'departure Local five'  OR  spoken MHz
    Falls back to MHz only when local_preset is missing.
    """
    block = airport.get("departure") or {}
    mhz = float(block.get("freq_mhz") or 350.0)
    freq_phrase = _pick(
        f"departure {speak_freq(mhz)}",
        f"departure frequency {speak_freq(mhz)}",
    )
    local = speak_local_preset(airport, "departure")
    if local:
        return _pick(f"departure {local}", freq_phrase)
    return freq_phrase


def speak_ground_contact_target(airport: dict[str, Any]) -> str:
    """
    Delivery→Ground handoff target:
      'ground Local three'  OR  'ground on two seven fife point eight'
    """
    block = airport.get("ground") or {}
    mhz = float(block.get("freq_mhz") or 275.8)
    freq_phrase = f"ground on {speak_freq(mhz)}"
    local = speak_local_preset(airport, "ground")
    if local:
        return _pick(f"ground, {local}", f"ground {local}", freq_phrase)
    return freq_phrase


# Spoken agency names for handoffs (channel key → radio call)
HANDOFF_AGENCY_NAMES: dict[str, str] = {
    "delivery": "Delivery",
    "ground": "Ground",
    "tower": "Tower",
    "departure": "Departure",
    "approach": "Approach",
    "blackjack": "Blackjack",
    "bandsaw": "Bandsaw",
    "ops": "Ops",
    "other": "Center",
    "center": "Center",
}

# Skip these when auto-picking the next handoff agency from the timeline
_HANDOFF_SKIP_CHANNELS = frozenset({"ops", "bandsaw"})


def speak_agency_name(channel: str) -> str:
    ch = (channel or "").strip().lower()
    return HANDOFF_AGENCY_NAMES.get(ch) or (ch.title() if ch else "Blackjack")


def speak_agency_contact_target(airport: dict[str, Any], channel: str) -> str:
    """
    Contact target for a handoff, e.g.:
      'Blackjack, Local fourteen'  OR  'Blackjack on tree seven seven point eight'
    """
    ch = (channel or "blackjack").strip().lower() or "blackjack"
    agency = speak_agency_name(ch)
    block = airport.get(ch) or {}
    try:
        mhz = float(block.get("freq_mhz") or 0.0)
    except (TypeError, ValueError):
        mhz = 0.0
    local = speak_local_preset(airport, ch)
    if local:
        return _pick(f"{agency}, {local}", f"{agency} {local}")
    if mhz > 0:
        return f"{agency} on {speak_freq(mhz)}"
    return agency


def resolve_handoff_channel(
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
    *,
    from_channel: str | None = None,
    default: str = "blackjack",
) -> str:
    """
    Next agency for a Departure/Center handoff.
    Prefer step.handoff_channel, else the next timeline agency (skip Ops), else default.
    """
    if step is not None:
        raw = step.get("handoff_channel") or step.get("handoff_to")
        if raw is not None and str(raw).strip():
            return str(raw).strip().lower()

    from_ch = (from_channel or (step.get("channel") if step else None) or "departure")
    from_ch = str(from_ch).strip().lower()

    if mission is not None and step is not None and isinstance(mission.get("steps"), list):
        steps = mission["steps"]
        sid = step.get("id")
        start = -1
        for i, peer in enumerate(steps):
            if not isinstance(peer, dict):
                continue
            if peer is step or (sid and peer.get("id") == sid):
                start = i
                break
        if start >= 0:
            for peer in steps[start + 1 :]:
                if not isinstance(peer, dict):
                    continue
                if peer.get("enabled") is False:
                    continue
                ch = str(peer.get("channel") or peer.get("phase") or "").strip().lower()
                if not ch or ch == from_ch or ch in _HANDOFF_SKIP_CHANNELS:
                    continue
                # Skip another handoff template — look for the receiving agency
                tmpl = str(peer.get("template") or "")
                if tmpl in ("departure_handoff", "center_handoff", "bj_range_exit"):
                    continue
                return ch
    return default


def build_departure_handoff(
    airport: dict[str, Any],
    callsign: str,
    *,
    handoff_channel: str = "blackjack",
    from_channel: str = "departure",
) -> str:
    """
    Departure (or Center) hands off to the next agency:
      '{cs}, Nellis Departure, contact Blackjack, Local fourteen.'
      (sometimes closes with good day / see ya)
    """
    name = airport["name"]
    cs = speak_callsign(callsign)
    from_ch = (from_channel or "departure").strip().lower()
    agency = (
        f"{name} Departure"
        if from_ch == "departure"
        else (f"{name} Center" if from_ch in ("other", "center") else f"{name} {speak_agency_name(from_ch)}")
    )
    target = speak_agency_contact_target(airport, handoff_channel)
    return with_freq_handoff_closer(f"{cs}, {agency}, contact {target}")


# --- Recovery / situation phrase helper ---------------------------------

# Visual recoveries + instrument. Keys are stable; labels are UI / Fly picker.
RECOVERY_CHOICES: list[tuple[str, str]] = [
    ("visual_overhead", "Visual — Overhead"),
    ("tactical_overhead", "Visual — Tactical overhead"),
    ("straight_in", "Visual — Straight-in"),
    ("instrument", "Instrument"),
]

DEFAULT_RECOVERY = "visual_overhead"

# Legacy / free-text aliases → canonical key
_RECOVERY_ALIASES: dict[str, str] = {
    "stryk": "tactical_overhead",
    "visual": "visual_overhead",
    "overhead": "visual_overhead",
    "visual overhead": "visual_overhead",
    "visual — overhead": "visual_overhead",
    "visual - overhead": "visual_overhead",
    "tactical overhead": "tactical_overhead",
    "tactical": "tactical_overhead",
    "tac overhead": "tactical_overhead",
    "visual — tactical overhead": "tactical_overhead",
    "visual - tactical overhead": "tactical_overhead",
    "straight-in": "straight_in",
    "straight in": "straight_in",
    "visual — straight-in": "straight_in",
    "visual - straight-in": "straight_in",
    "instrument": "instrument",
    "instrument approach": "instrument",
    "insturment": "instrument",  # common typo
}

APPROACH_PATTERN_CHOICES: list[tuple[str, str]] = [
    ("visual_overhead", "Visual — Overhead"),
    ("tactical_overhead", "Visual — Tactical overhead"),
    ("straight_in", "Visual — Straight-in"),
    ("instrument", "Instrument"),
]


def normalize_recovery_key(raw: str | None, *, default: str = DEFAULT_RECOVERY) -> str:
    """Canonical recovery key (visual_overhead / tactical_overhead / straight_in / instrument)."""
    text = str(raw or "").strip()
    if not text:
        return default
    low = text.casefold()
    if low in {k for k, _ in RECOVERY_CHOICES}:
        return low
    if low in _RECOVERY_ALIASES:
        return _RECOVERY_ALIASES[low]
    # Label match
    for key, lab in RECOVERY_CHOICES:
        if lab.casefold() == low:
            return key
    return default


def normalize_recovery(raw: str | None, *, default: str = DEFAULT_RECOVERY) -> str:
    """Spoken recovery phrase fragment (legacy helper name)."""
    return recovery_spoken(normalize_recovery_key(raw, default=default))


def recovery_label(raw: str | None) -> str:
    key = normalize_recovery_key(raw)
    for k, lab in RECOVERY_CHOICES:
        if k == key:
            return lab
    return key


def recovery_spoken(key: str) -> str:
    """Short spoken name used inside clearances."""
    return {
        "visual_overhead": "visual overhead",
        "tactical_overhead": "tactical overhead",
        "straight_in": "straight-in",
        "instrument": "instrument approach",
    }.get(normalize_recovery_key(key), "tactical overhead")


def recovery_expect(raw: str | None) -> str:
    """Expect-clause key derived from the active recovery."""
    return normalize_recovery_key(raw)


def recovery_pattern(raw: str | None) -> str:
    """Approach clearance pattern key (= recovery key)."""
    return normalize_recovery_key(raw)


def resolve_active_recovery(
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    """
    Active recovery for this sortie.
    Prefer Fly/state override → mission.active_recovery → step.recovery → default.
    """
    if state is not None:
        n = normalize_recovery_key(state.get("active_recovery"), default="")
        if n:
            return n
    if mission is not None:
        n = normalize_recovery_key(mission.get("active_recovery"), default="")
        if n:
            return n
    if step is not None:
        n = normalize_recovery_key(
            step.get("recovery") or step.get("recovery_type"), default=""
        )
        if n:
            return n
    return DEFAULT_RECOVERY


def apply_active_recovery_to_step(
    step: dict[str, Any],
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Shallow-copy step with recovery / expect / pattern filled from the active choice.
    Custom locked text is left alone (Fly picker won't rewrite it).
    """
    out = dict(step)
    tmpl = str(out.get("template") or "")
    if tmpl not in (
        "approach_check_in",
        "cleared_approach",
        "approach_procedure",
        "approach_iaf",
    ):
        return out
    if out.get("text"):
        return out
    rec = resolve_active_recovery(out, mission, state=state)
    out["recovery"] = rec
    out["expect"] = recovery_expect(rec)
    out["approach_pattern"] = recovery_pattern(rec)
    plan = approach_plan_from_state(state)
    if plan.get("vfr_recovery"):
        out["vfr_recovery"] = plan["vfr_recovery"]
    if plan.get("iaf"):
        out["iaf"] = plan["iaf"]
    if plan.get("runway"):
        out["runway"] = plan["runway"]
    return out


# --- Takeoff mode (line up and wait vs rolling) + pilot requests --------

TAKEOFF_MODE_CHOICES: list[tuple[str, str]] = [
    ("lineup", "Line up and wait"),
    ("rolling", "Rolling takeoff"),
]
DEFAULT_TAKEOFF_MODE = "lineup"
DEFAULT_TAKEOFF_OFFER_CHANCE = 0.35

# Takeoff Tower requests — only during departure (not approach / landing).
_TAKEOFF_TOWER_REQUESTS: list[tuple[str, str]] = [
    ("accept_rolling", "Accept rolling"),
    ("deny_rolling", "Deny rolling"),
    ("request_rolling", "Request rolling takeoff"),
    ("request_lineup", "Request line up and wait"),
]
_TAKEOFF_REQUEST_KEYS = frozenset(k for k, _ in _TAKEOFF_TOWER_REQUESTS)

# Approach Tower requests — landing phase (LUAW / rolling do not apply).
_APPROACH_TOWER_REQUESTS: list[tuple[str, str]] = [
    ("request_landing", "Gear down full stop"),
    ("request_low_approach", "Request the option"),
    ("request_go_around", "On the go / go around"),
]
_APPROACH_REQUEST_KEYS = frozenset(k for k, _ in _APPROACH_TOWER_REQUESTS)

# Frequency-scoped pilot requests (extend per agency later).
# Alternate-runway requests are appended dynamically from airport.instrument_runways.
# Tower rows are chosen by mission phase in pilot_requests_for_channel().
_HANDOFF_REQUEST_TEMPLATES = frozenset({"departure_handoff", "center_handoff"})
_DEPARTURE_HANDOFF_REQUESTS: list[tuple[str, str]] = [
    ("request_handoff", "Request handoff"),
]

PILOT_REQUESTS_BY_CHANNEL: dict[str, list[tuple[str, str]]] = {
    "tower": list(_TAKEOFF_TOWER_REQUESTS),
    "ground": [],
    "delivery": [],
    "departure": list(_DEPARTURE_HANDOFF_REQUESTS),
    "approach": [],
    "blackjack": [],
    "bandsaw": [],
    "ops": [],
    "other": [],
}
_CHANNELS_WITH_RUNWAY_REQUESTS = frozenset({"tower", "ground", "approach"})


def normalize_takeoff_mode(raw: str | None, *, default: str = DEFAULT_TAKEOFF_MODE) -> str:
    text = str(raw or "").strip().casefold().replace(" ", "_").replace("-", "_")
    if not text:
        return default
    if text in {"lineup", "line_up", "line_up_and_wait", "luaw", "lauw"}:
        return "lineup"
    if text in {"rolling", "roll", "rolling_takeoff"}:
        return "rolling"
    for key, lab in TAKEOFF_MODE_CHOICES:
        if lab.casefold() == str(raw or "").strip().casefold():
            return key
    return default


def takeoff_mode_label(raw: str | None) -> str:
    key = normalize_takeoff_mode(raw)
    for k, lab in TAKEOFF_MODE_CHOICES:
        if k == key:
            return lab
    return key


def resolve_active_takeoff_mode(
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    """Prefer Fly/state → mission → default lineup."""
    if state is not None:
        raw = state.get("active_takeoff_mode")
        if raw is not None and str(raw).strip() != "":
            return normalize_takeoff_mode(raw)
    if mission is not None:
        raw = mission.get("active_takeoff_mode")
        if raw is not None and str(raw).strip() != "":
            return normalize_takeoff_mode(raw)
    return DEFAULT_TAKEOFF_MODE


def pending_takeoff_offer(state: dict[str, Any] | None) -> str | None:
    if not state:
        return None
    raw = str(state.get("pending_takeoff_offer") or "").strip().casefold()
    if raw in ("rolling", "roll"):
        return "rolling"
    return None


def takeoff_offer_chance(
    config: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
) -> float:
    for src in (mission, config):
        if not src:
            continue
        try:
            n = float(src.get("takeoff_offer_chance"))
            return max(0.0, min(1.0, n))
        except (TypeError, ValueError, AttributeError):
            continue
    return DEFAULT_TAKEOFF_OFFER_CHANCE


def is_takeoff_related_template(template: str | None) -> bool:
    t = str(template or "").strip()
    return t in {
        "lineup",
        "rolling_accept",
        "clear_takeoff",
        "clear_takeoff_rolling",
        "clear_takeoff_intersection",
    }


def is_landing_related_template(template: str | None) -> bool:
    """Tower / Ground templates used on the approach / landing side."""
    t = str(template or "").strip()
    return t in {
        "right_break",
        "clear_land",
        "exit_runway",
        "go_around",
        "taxi_in",
        "cleared_approach",
        "approach_check_in",
        "approach_procedure",
        "contact_tower",
    }


def resolve_pilot_request_phase(
    *,
    phase: str | None = None,
    channel: str | None = None,
    template: str | None = None,
) -> str:
    """
    departure | approach | flight | '' for gating pilot requests / offers.

    Prefer the step's mission phase; fall back to template family when Tower
    is ambiguous (same agency on both ends of the sortie).
    """
    p = str(phase or "").strip().lower()
    if p in ("departure", "flight", "approach"):
        return p
    if is_landing_related_template(template):
        return "approach"
    if is_takeoff_related_template(template):
        return "departure"
    ch = str(channel or "").strip().lower()
    if ch in ("approach",):
        return "approach"
    if ch in ("delivery", "ground", "departure"):
        return "departure"
    if ch in ("blackjack", "bandsaw"):
        return "flight"
    return ""


def should_skip_takeoff_step(
    step: dict[str, Any] | None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> bool:
    """Rolling takeoff skips line-up-and-wait."""
    if not step:
        return False
    if str(step.get("template") or "") != "lineup":
        return False
    # While an offer is pending, keep the step so we can TX "accept rolling?"
    if pending_takeoff_offer(state) == "rolling":
        return False
    return resolve_active_takeoff_mode(mission, state) == "rolling"


def should_skip_approach_step(
    step: dict[str, Any] | None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> bool:
    """
    Whether the flow cursor should skip an approach step.

    Straight-in / instrument used to skip Tower check-in (no break to approve).
    They still need that call — continue straight-in / roger continue — inside
    ~12 NM; cleared-to-land stays gated at 6 NM. So nothing is skipped here.
    """
    return False


def effective_takeoff_template(
    template: str,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    """
    Remap tower takeoff templates from active mode / pending offer.
    lineup + pending offer → rolling_accept; clear_takeoff ↔ rolling by mode.
    """
    tmpl = str(template or "").strip() or "radio_check"
    pending = pending_takeoff_offer(state)
    mode = resolve_active_takeoff_mode(mission, state)
    if tmpl == "lineup" and pending == "rolling":
        return "rolling_accept"
    if tmpl in ("clear_takeoff", "clear_takeoff_rolling"):
        return "clear_takeoff_rolling" if mode == "rolling" else "clear_takeoff"
    return tmpl


def readback_template_for_step(
    step: dict[str, Any] | None,
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    """Template the READ BACK card should use — what ATC actually said."""
    tmpl = str((step or {}).get("template") or "").strip()
    return effective_takeoff_template(tmpl, mission, state)


def requested_runway(
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> str | None:
    """Pilot-requested runway override (Fly / mission), if any."""
    for src in (state, mission):
        if not src:
            continue
        n = normalize_runway(src.get("requested_runway"))
        if n:
            return n
    return None


def set_requested_runway(
    runway: str | None,
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> str | None:
    """Stick a pilot-requested runway (or clear when blank)."""
    n = normalize_runway(runway)
    for src in (mission, state):
        if src is None:
            continue
        if n:
            src["requested_runway"] = n
        else:
            src.pop("requested_runway", None)
    return n


def parse_runway_request_key(request_key: str | None) -> str | None:
    """'request_runway_21L' → '21L'."""
    key = str(request_key or "").strip().casefold()
    if not key.startswith("request_runway_"):
        return None
    return normalize_runway(key[len("request_runway_") :])


def pilot_requests_for_channel(
    channel: str | None,
    state: dict[str, Any] | None = None,
    airport: dict[str, Any] | None = None,
    *,
    phase: str | None = None,
    template: str | None = None,
) -> list[tuple[str, str]]:
    """
    Requests available on this agency for the current mission phase.

    Tower: departure → rolling / LUAW; approach → landing / go-around.
    Accept/Deny only while a rolling takeoff offer is pending (departure).
    """
    ch = str(channel or "other").strip().lower() or "other"
    phase_key = resolve_pilot_request_phase(
        phase=phase, channel=ch, template=template
    )
    if ch == "tower":
        if phase_key == "approach":
            rows = list(_APPROACH_TOWER_REQUESTS)
        elif phase_key == "departure":
            rows = list(_TAKEOFF_TOWER_REQUESTS)
        else:
            # Ambiguous Tower (no phase/template) — show neither family.
            rows = []
    else:
        rows = list(PILOT_REQUESTS_BY_CHANNEL.get(ch) or [])

    pending = pending_takeoff_offer(state)
    out: list[tuple[str, str]] = []
    for key, lab in rows:
        if key in _TAKEOFF_REQUEST_KEYS and phase_key != "departure":
            continue
        if key in _APPROACH_REQUEST_KEYS and phase_key != "approach":
            continue
        if key in ("accept_rolling", "deny_rolling") and pending != "rolling":
            continue
        if key == "request_handoff" and str(template or "") not in _HANDOFF_REQUEST_TEMPLATES:
            continue
        out.append((key, lab))
    # Alternate runway (e.g. 21L) — only when pilot requests it, or instrument use.
    if airport is not None and ch in _CHANNELS_WITH_RUNWAY_REQUESTS:
        seen = {k for k, _ in out}
        for rwy in airport_instrument_runways(airport):
            key = f"request_runway_{rwy}"
            if key in seen:
                continue
            out.append((key, f"Request runway {rwy}"))
            seen.add(key)
        # Always available — clears a sticky pilot request / Setup override and
        # re-picks the runway from METAR (including Approach plan runway).
        out.append(("clear_runway_request", "Reset runway to winds"))
    return out


def takeoff_offer_visible(
    state: dict[str, Any] | None,
    *,
    channel: str | None = None,
    phase: str | None = None,
    template: str | None = None,
) -> bool:
    """Rolling takeoff offer only on departure Tower — never on landing."""
    if pending_takeoff_offer(state) != "rolling":
        return False
    ch = str(channel or "").strip().lower()
    if ch != "tower":
        return False
    return (
        resolve_pilot_request_phase(phase=phase, channel=ch, template=template)
        == "departure"
    )


def set_takeoff_mode(
    mode: str,
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    clear_offer: bool = True,
) -> str:
    """Stick takeoff mode on mission + state; optionally clear pending offer."""
    key = normalize_takeoff_mode(mode)
    if mission is not None:
        mission["active_takeoff_mode"] = key
    if state is not None:
        state["active_takeoff_mode"] = key
        if clear_offer:
            state["pending_takeoff_offer"] = None
    return key


def reset_runway_to_winds(
    airport: dict[str, Any],
    weather: Weather,
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    opus: OpusFlightContext | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """
    Drop pilot / Setup runway overrides and re-pick from METAR winds.

    If an Approach plan is already assigned, keep the pattern (tactical /
    instrument) but rebuild runway-side recovery / IAF for the wind end.
    """
    set_requested_runway(None, mission=mission, state=state)
    if config is not None:
        config["runway_override"] = ""

    st = state if isinstance(state, dict) else {}
    prev = approach_plan_from_state(st)
    pattern = str(prev.get("pattern") or st.get("active_recovery") or "") or None
    # Drop cached runway so a force rebuild cannot keep the wrong end.
    if st.get("approach_plan") and isinstance(st["approach_plan"], dict):
        st["approach_plan"] = dict(st["approach_plan"])
        st["approach_plan"].pop("runway", None)
    st.pop("approach_runway", None)

    if st.get("approach_assigned") or prev or pattern:
        plan = assign_approach_plan(
            airport,
            weather,
            mission=mission,
            state=st,
            opus=opus,
            force=True,
            recovery=pattern,
        )
        if weather.wind_dir is not None and weather.wind_speed_kt is not None:
            print(
                f"Runway reset to winds: {int(weather.wind_dir):03d}/"
                f"{int(weather.wind_speed_kt)} -> {plan.get('runway')} "
                f"({plan.get('vfr_recovery') or plan.get('iaf') or plan.get('pattern')})"
            )
        else:
            print(f"Runway reset to winds -> {plan.get('runway')}")
        return str(plan.get("runway") or "")

    instrument = uses_instrument_runway(mission=mission, state=state)
    picked = pick_recovery_runway(airport, weather, instrument=instrument)
    if weather.wind_dir is not None and weather.wind_speed_kt is not None:
        print(
            f"Runway reset to winds: {int(weather.wind_dir):03d}/"
            f"{int(weather.wind_speed_kt)} -> {picked}"
        )
    else:
        print(f"Runway reset to winds -> {picked}")
    return picked


def apply_pilot_request(
    request_key: str,
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    airport: dict[str, Any] | None = None,
    weather: Weather | None = None,
    opus: OpusFlightContext | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Apply a pilot request to mission/state.
    Returns {key, takeoff_mode, pending_offer, ack_kind, runway?}.
    """
    key = str(request_key or "").strip().casefold()
    if key == "accept_rolling":
        set_takeoff_mode("rolling", mission=mission, state=state, clear_offer=True)
        return {"key": key, "takeoff_mode": "rolling", "pending_offer": None, "ack_kind": "accept_rolling"}
    if key == "deny_rolling":
        set_takeoff_mode("lineup", mission=mission, state=state, clear_offer=True)
        return {"key": key, "takeoff_mode": "lineup", "pending_offer": None, "ack_kind": "deny_rolling"}
    if key == "request_rolling":
        set_takeoff_mode("rolling", mission=mission, state=state, clear_offer=True)
        return {"key": key, "takeoff_mode": "rolling", "pending_offer": None, "ack_kind": "request_rolling"}
    if key == "request_lineup":
        set_takeoff_mode("lineup", mission=mission, state=state, clear_offer=True)
        return {"key": key, "takeoff_mode": "lineup", "pending_offer": None, "ack_kind": "request_lineup"}
    if key == "request_landing":
        # Already cleared for the option → take it full stop (no second clear).
        if awaiting_option_on_the_go(state):
            commit_option_full_stop(state=state, mission=mission)
            return {
                "key": key,
                "takeoff_mode": resolve_active_takeoff_mode(mission, state),
                "pending_offer": pending_takeoff_offer(state),
                "ack_kind": "",
                "landing_intent": LANDING_INTENT_FULL_STOP,
                "execute_option_full_stop": True,
            }
        set_landing_intent(LANDING_INTENT_FULL_STOP, state=state, mission=mission)
        return {
            "key": key,
            "takeoff_mode": resolve_active_takeoff_mode(mission, state),
            "pending_offer": pending_takeoff_offer(state),
            "ack_kind": "request_landing",
            "landing_intent": LANDING_INTENT_FULL_STOP,
        }
    if key == "request_low_approach":
        set_landing_intent(LANDING_INTENT_LOW_APPROACH, state=state, mission=mission)
        return {
            "key": key,
            "takeoff_mode": resolve_active_takeoff_mode(mission, state),
            "pending_offer": pending_takeoff_offer(state),
            "ack_kind": "request_low_approach",
            "landing_intent": LANDING_INTENT_LOW_APPROACH,
        }
    if key == "request_go_around":
        # Full go-around / missed is played by FlowEngine.execute_go_around.
        return {
            "key": key,
            "takeoff_mode": resolve_active_takeoff_mode(mission, state),
            "pending_offer": pending_takeoff_offer(state),
            "ack_kind": "",
            "execute_go_around": True,
        }
    if key == "request_handoff":
        return {
            "key": key,
            "takeoff_mode": resolve_active_takeoff_mode(mission, state),
            "pending_offer": pending_takeoff_offer(state),
            "ack_kind": "",
            "execute_departure_handoff": True,
        }
    if key == "clear_runway_request":
        rwy: str | None = None
        if airport is not None and weather is not None:
            rwy = reset_runway_to_winds(
                airport,
                weather,
                mission=mission,
                state=state,
                opus=opus,
                config=config,
            ) or None
        else:
            set_requested_runway(None, mission=mission, state=state)
            if config is not None:
                config["runway_override"] = ""
        return {
            "key": key,
            "takeoff_mode": resolve_active_takeoff_mode(mission, state),
            "pending_offer": pending_takeoff_offer(state),
            "ack_kind": "clear_runway_request",
            "runway": rwy,
        }
    req_rwy = parse_runway_request_key(key)
    if req_rwy:
        set_requested_runway(req_rwy, mission=mission, state=state)
        return {
            "key": key,
            "takeoff_mode": resolve_active_takeoff_mode(mission, state),
            "pending_offer": pending_takeoff_offer(state),
            "ack_kind": "request_runway",
            "runway": req_rwy,
        }
    raise ValueError(f"Unknown pilot request '{request_key}'")


def build_pilot_request_ack(
    ack_kind: str,
    airport: dict[str, Any],
    callsign: str,
    runway: str | None = None,
    channel: str | None = None,
) -> str:
    """Short agency ack after Accept / Deny / Request."""
    name = airport.get("name") or "Tower"
    agency = speak_agency_name(channel or "tower")
    cs = speak_callsign(callsign)
    kind = str(ack_kind or "").strip().casefold()
    if kind in ("accept_rolling", "request_rolling"):
        return f"{cs}, {name} {agency}, rolling approved."
    if kind == "deny_rolling":
        return f"{cs}, {name} {agency}, unable rolling, expect line up and wait."
    if kind == "request_lineup":
        return f"{cs}, {name} {agency}, expect line up and wait."
    if kind == "request_landing":
        return f"{cs}, {name} {agency}, roger, expect landing clearance."
    if kind == "request_low_approach":
        return f"{cs}, {name} {agency}, roger, expect the option."
    if kind == "request_go_around":
        return f"{cs}, {name} Tower, go around."
    if kind == "request_runway":
        rwy = speak_runway(runway) if runway else "requested"
        return f"{cs}, {name} {agency}, runway {rwy} approved."
    if kind == "clear_runway_request":
        if runway:
            return (
                f"{cs}, {name} {agency}, roger, expect runway {speak_runway(runway)}."
            )
        return f"{cs}, {name} {agency}, roger, expect active runway."
    return f"{cs}, {name} {agency}, roger."


def speak_recovery_clearance(recovery: str | None) -> str:
    """'cleared tactical overhead' / 'cleared visual overhead' / …"""
    key = normalize_recovery_key(recovery)
    spoken = recovery_spoken(key)
    if key == "instrument":
        return f"cleared {spoken}"
    if key == "straight_in":
        return "cleared straight-in"
    return f"cleared {spoken}"


def speak_landing_flow(runway: str | None, airport_name: str) -> str:
    """'Nellis landing south' (RWY 21) / 'Nellis landing north' (RWY 03)."""
    name = str(airport_name or "Nellis").strip() or "Nellis"
    side = _runway_side(runway)
    direction = "north" if side == "03" else "south"
    return f"{name} landing {direction}"


def speak_pattern_for_runway(pattern: str | None, runway: str) -> str:
    """'TAC Overhead runway two one right' / 'straight-in runway …'."""
    key = normalize_recovery_key(pattern, default="")
    rwy = speak_runway(runway) if runway else ""
    if key == "visual_overhead":
        return f"overhead runway {rwy}" if rwy else "overhead"
    if key == "tactical_overhead":
        return f"TAC Overhead runway {rwy}" if rwy else "TAC Overhead"
    if key == "straight_in":
        return f"straight-in runway {rwy}" if rwy else "straight-in"
    if key == "instrument":
        return f"instrument approach runway {rwy}" if rwy else "instrument approach"
    p = str(pattern or "").strip()
    if not p:
        return f"TAC Overhead runway {rwy}" if rwy else "TAC Overhead"
    return f"{p} runway {rwy}" if rwy else p


def speak_expect_pattern(pattern: str | None, runway: str) -> str:
    """Legacy 'expect TAC Overhead runway …' (no named VFR recovery)."""
    return f"expect {speak_pattern_for_runway(pattern, runway)}"


def speak_expect_vfr_recovery(
    vfr_say: str | None,
    pattern: str | None,
    runway: str,
) -> str:
    """'expect Arcoe recovery for the TAC Overhead runway two one right'."""
    name = str(vfr_say or "recovery").strip() or "recovery"
    pattern_body = speak_pattern_for_runway(pattern, runway)
    return f"expect {name} recovery for the {pattern_body}"


def _plan_direct_say(
    plan: dict[str, Any],
    airport: dict[str, Any],
    *,
    runway: str,
) -> str:
    """Spoken fix for Blackjack 'proceed direct …' / Approach naming."""
    for key in ("direct_say", "vfr_recovery_say", "iaf_say", "direct_fix", "vfr_recovery", "iaf"):
        val = str(plan.get(key) or "").strip()
        if val:
            return val
    catalog = load_approach_catalog(airport)
    defs = approach_defaults(catalog)
    side = _runway_side(runway)
    by_side = (defs.get("direct_by_runway_side") or {}).get(side) or {}
    if isinstance(by_side, dict):
        return str(by_side.get("say") or by_side.get("id") or "").strip()
    return str(by_side or "").strip()


def build_approach_recovery(
    airport: dict[str, Any],
    callsign: str,
    weather: Weather,
    runway: str,
    *,
    recovery: str | None = None,
    descend_ft: int | None = None,
    speed_kt: int | None = None,
    expect: str | None = None,
    plan: dict[str, Any] | None = None,
) -> str:
    """
    Approach check-in (NATCF style), e.g.:
    Fleece 1, Nellis Approach, Nellis landing south, expect Arcoe recovery
    for the TAC Overhead runway two one right, …
    """
    name = airport["name"]
    cs = speak_callsign(callsign)
    alt = speak_altimeter(weather.altimeter_inhg or 29.92)
    p = dict(plan or {})
    rec_key = normalize_recovery_key(recovery or p.get("pattern"))
    rwy = str(p.get("runway") or runway or "")
    rwy_s = speak_runway(rwy) if rwy else ""

    bits = [
        f"{cs}, {name} Approach",
        speak_landing_flow(rwy, name),
    ]

    instrument = rec_key == "instrument" or bool(p.get("iaf"))
    if instrument:
        # Check-in: expect the assigned procedure only (clearance is the next step).
        inst_say = str(p.get("instrument_say") or "").strip()
        if inst_say:
            bits.append(f"expect {inst_say}")
        elif rwy_s:
            bits.append(f"expect instrument approach runway {rwy_s}")
        else:
            bits.append("expect instrument approach")
    else:
        vfr_say = str(p.get("vfr_recovery_say") or p.get("vfr_recovery") or "").strip()
        expect_key = expect if expect is not None else recovery_expect(rec_key)
        if vfr_say:
            bits.append(speak_expect_vfr_recovery(vfr_say, expect_key, rwy))
        else:
            bits.append(speak_expect_pattern(expect_key, rwy))
        # VFR descend on check-in; instrument crossing altitude is on the clearance.
        dft = p.get("descend_ft") if descend_ft is None else descend_ft
        if dft is None and not p:
            dft = 10000
        if dft:
            try:
                clause = speak_descend_pilot_discretion(int(dft))
                if clause:
                    bits.append(clause)
            except (TypeError, ValueError):
                pass

    # Speed only when traffic (or other factor) sets speed_restrict on the plan.
    if p.get("speed_restrict"):
        sk = p.get("speed_kt") if speed_kt is None else speed_kt
        if sk:
            try:
                bits.append(f"maintain {speak_digits(str(int(sk)))} knots")
            except (TypeError, ValueError):
                pass

    bits.append(f"{name} altimeter {alt}")
    return ", ".join(bits) + "."


def build_vfr_recovery_clearance(
    airport: dict[str, Any],
    callsign: str,
    *,
    plan: dict[str, Any] | None = None,
    recovery_say: str | None = None,
    pattern: str | None = None,
    runway: str | None = None,
) -> str:
    """Step after check-in: clear the named VFR recovery / pattern."""
    name = airport["name"]
    cs = speak_callsign(callsign)
    p = dict(plan or {})
    vfr = str(recovery_say or p.get("vfr_recovery_say") or p.get("vfr_recovery") or "").strip()
    rec_key = normalize_recovery_key(pattern or p.get("pattern"))
    rwy = str(runway or p.get("runway") or "")
    pattern_body = speak_pattern_for_runway(rec_key, rwy)
    if vfr:
        body = f"cleared {vfr} recovery for the {pattern_body}"
    else:
        body = f"cleared {pattern_body}"
    return f"{cs}, {name} Approach, {body}."


def build_iaf_clearance(
    airport: dict[str, Any],
    callsign: str,
    *,
    plan: dict[str, Any] | None = None,
    iaf_say: str | None = None,
    instrument_say: str | None = None,
    runway: str | None = None,
) -> str:
    """
    Instrument approach clearance (after check-in expect), e.g.:
    Fleece 1, Nellis Approach, cross Dudbe at or above one six thousand,
    cleared ILS Zulu runway two one left.
    """
    name = airport["name"]
    cs = speak_callsign(callsign)
    p = dict(plan or {})
    iaf = str(iaf_say or p.get("iaf_say") or p.get("iaf") or "").strip()
    inst = str(instrument_say or p.get("instrument_say") or "").strip()
    rwy = speak_runway(str(runway or p.get("runway") or ""))
    if not inst:
        inst = f"instrument approach runway {rwy}" if rwy else "instrument approach"
    # Never clear "ILS or LOC" — catalog assigns one procedure (usually ILS).
    inst = re.sub(
        r"\b(or|,)\s+(localizer|loc)\b",
        "",
        inst,
        flags=re.IGNORECASE,
    )
    inst = re.sub(r"\s+", " ", inst).strip(" ,")

    bits = [f"{cs}, {name} Approach"]
    alt_ft = p.get("descend_ft")
    if iaf and alt_ft:
        try:
            alt_s = speak_altitude_value(str(int(alt_ft)), prefer_fl_below=1000)
            at_or_above = str(p.get("iaf_altitude_type") or "at_or_above") == "at_or_above"
            qualifier = "at or above" if at_or_above else "at"
            bits.append(f"cross {iaf} {qualifier} {alt_s}")
        except (TypeError, ValueError):
            bits.append(f"cross {iaf}")
    elif iaf:
        bits.append(f"cross {iaf}")
    bits.append(f"cleared {inst}")
    return ", ".join(bits) + "."


def build_approach_change(
    airport: dict[str, Any],
    callsign: str,
    weather: Weather,
    *,
    plan: dict[str, Any] | None = None,
) -> str:
    """Acknowledge a pilot-requested change of recovery / approach."""
    name = airport["name"]
    cs = speak_callsign(callsign)
    p = dict(plan or {})
    rec_key = normalize_recovery_key(p.get("pattern"))
    rwy = speak_runway(str(p.get("runway") or ""))
    if rec_key == "instrument" or p.get("iaf"):
        iaf = str(p.get("iaf_say") or p.get("iaf") or "the IAF")
        inst = str(p.get("instrument_say") or "instrument approach")
        body = f"roger, proceed direct {iaf}, expect {inst}"
        if rwy and "runway" not in inst.lower():
            body += f" runway {rwy}"
    else:
        vfr = str(p.get("vfr_recovery_say") or p.get("vfr_recovery") or "").strip()
        cleared = speak_recovery_clearance(rec_key)
        body = f"roger, {cleared}"
        if vfr:
            body = f"roger, cleared {vfr} recovery, {cleared}"
        if rwy:
            body += f" runway {rwy}"
    alt = speak_altimeter(weather.altimeter_inhg or 29.92)
    return f"{cs}, {name} Approach, {body}, {name} altimeter {alt}."


def build_hold_clearance(
    airport: dict[str, Any],
    callsign: str,
    *,
    hold: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    efc_minutes: int | None = None,
) -> str:
    """Spoken hold clearance (simple state — not a published-leg simulator)."""
    name = airport["name"]
    cs = speak_callsign(callsign)
    p = dict(plan or {})
    h = dict(hold or {})
    fix = str(h.get("say") or h.get("id") or p.get("iaf_say") or p.get("iaf") or "the IAF")
    try:
        alt_ft = int(h.get("altitude_ft") or p.get("descend_ft") or 10000)
    except (TypeError, ValueError):
        alt_ft = 10000
    alt = speak_altitude_value(str(alt_ft), prefer_fl_below=1000)
    turn = str(h.get("turn") or "standard").strip().lower()
    turn_bit = "left turns" if turn in ("left", "west") else (
        "right turns" if turn in ("right", "east") else "standard turns"
    )
    bits = [
        f"{cs}, {name} Approach, hold at {fix}",
        f"maintain {alt}",
        turn_bit,
    ]
    if efc_minutes is not None:
        bits.append(f"expect further clearance in {speak_minutes_natural(int(efc_minutes))} minutes")
    else:
        bits.append("expect further clearance")
    return ", ".join(bits) + "."


def build_vector_clearance(
    airport: dict[str, Any],
    callsign: str,
    *,
    heading: int | None = None,
    altitude_ft: int | None = None,
    plan: dict[str, Any] | None = None,
) -> str:
    """Simple radar vector clearance toward recovery / final."""
    name = airport["name"]
    cs = speak_callsign(callsign)
    p = dict(plan or {})
    hdg = heading
    if hdg is None:
        # Point roughly toward the field from a northern recovery (210) or southern (030)
        side = _runway_side(str(p.get("runway") or "21R"))
        hdg = 180 if side == "21" else 360
    try:
        hdg_i = int(hdg) % 360
    except (TypeError, ValueError):
        hdg_i = 180
    bits = [
        f"{cs}, {name} Approach, fly heading {speak_digits(f'{hdg_i:03d}')}",
        "vectors",
    ]
    if p.get("iaf_say") or p.get("vfr_recovery_say"):
        dest = str(p.get("iaf_say") or p.get("vfr_recovery_say"))
        bits.append(f"for {dest}")
    else:
        bits.append("for the field")
    alt = altitude_ft if altitude_ft is not None else p.get("descend_ft")
    if alt:
        try:
            clause = speak_descend_pilot_discretion(int(alt))
            if clause:
                bits.append(clause)
        except (TypeError, ValueError):
            pass
    return ", ".join(bits) + "."


def build_leave_hold_clearance(
    airport: dict[str, Any],
    callsign: str,
    *,
    plan: dict[str, Any] | None = None,
) -> str:
    name = airport["name"]
    cs = speak_callsign(callsign)
    p = dict(plan or {})
    if p.get("iaf_say") or p.get("pattern") == "instrument":
        return build_iaf_clearance(airport, callsign, plan=p)
    return build_vfr_recovery_clearance(airport, callsign, plan=p)


def build_approach_tower_handoff(
    airport: dict[str, Any],
    callsign: str,
    *,
    plan: dict[str, Any] | None = None,
    runway: str | None = None,
) -> str:
    """Approach → Tower handoff only (approach already cleared)."""
    name = airport["name"]
    cs = speak_callsign(callsign)
    twr_local = speak_local_preset(airport, "tower")
    tower = airport.get("tower") or {"freq_mhz": 327.0}
    if twr_local:
        contact = f"contact tower, {twr_local}"
    else:
        contact = f"contact tower on {speak_freq(float(tower['freq_mhz']))}"
    return with_freq_handoff_closer(f"{cs}, {name} Approach, {contact}")


def build_tower_check_in(
    airport: dict[str, Any],
    callsign: str,
    runway: str,
    *,
    plan: dict[str, Any] | None = None,
    recovery: str | None = None,
) -> str:
    """
    First Tower call after handoff — recovery-aware.

    OHB / TAC OHB → right break approved
    Straight-in → continue straight-in
    Instrument → roger, continue
    """
    name = airport["name"]
    cs = speak_callsign(callsign)
    p = dict(plan or {})
    rec_key = normalize_recovery_key(recovery or p.get("pattern"))
    rwy = speak_runway(str(runway or p.get("runway") or ""))
    if rec_key == "tactical_overhead":
        body = f"right break approved runway {rwy}" if rwy else "right break approved"
    elif rec_key == "visual_overhead":
        body = f"right break approved runway {rwy}" if rwy else "right break approved"
    elif rec_key == "straight_in":
        body = f"continue straight-in runway {rwy}" if rwy else "continue straight-in"
    elif rec_key == "instrument":
        inst = str(p.get("instrument_say") or "the approach").strip()
        body = f"roger, continue {inst}"
        if rwy and "runway" not in body.lower():
            body = f"{body} runway {rwy}"
    else:
        body = f"roger, continue runway {rwy}" if rwy else "roger, continue"
    return f"{cs}, {name} Tower, {body}."


def flight_ship_count(opus: OpusFlightContext | None) -> int:
    """
    How many ships are in the flight for landing sequencing.

    Use actual Opus signups only — planned flight qty is often 2 even when
    flying alone, which used to double the cleared-to-land call.
    """
    if not opus:
        return 1
    try:
        signed = int(opus.signup_count or 0)
    except (TypeError, ValueError):
        return 1
    return signed if signed > 1 else 1


def callsign_base_and_seat(callsign: str) -> tuple[str, int | None]:
    """'Fleece 1' → ('Fleece', 1); 'BRUISER' → ('BRUISER', None)."""
    cleaned = str(callsign or "").strip().replace("-", " ")
    parts = cleaned.split()
    if not parts:
        return "", None
    if len(parts) >= 2 and parts[-1].isdigit():
        return " ".join(parts[:-1]), int(parts[-1])
    return cleaned, None


def parse_element_callsign(callsign: str) -> tuple[str, int | None]:
    """
    Split flight vs element: 'Fleece 1-2' → ('Fleece 1', 2); 'Fleece 1' → ('Fleece 1', None).
    """
    cleaned = str(callsign or "").strip()
    if not cleaned:
        return "", None
    m = re.match(r"^(.+)-(\d+)$", cleaned)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return cleaned, None


def flight_callsign_base(
    callsign: str, opus: OpusFlightContext | None = None
) -> str:
    """Flight radio name without seat (e.g. 'Fleece 1')."""
    if opus is not None:
        base = str(getattr(opus, "flight_callsign", "") or "").strip()
        if base:
            return base
    flight, _seat = parse_element_callsign(callsign)
    return flight


def element_radio_callsign(flight_callsign: str, seat: int) -> str:
    """Full element callsign: 'Fleece 1' + seat 2 → 'Fleece 1-2'."""
    base = str(flight_callsign or "").strip()
    return f"{base}-{int(seat)}" if base else str(int(seat))


def landing_clearance_mode(
    recovery: str | None = None,
    *,
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    """
    How Tower clears to land for this recovery.

    'flight'  — one clearance for the whole flight (overhead / TAC overhead).
    'per_ship' — one ship per transmission (straight-in / instrument).
    """
    rec = normalize_recovery_key(recovery, default="") if recovery else ""
    if not rec:
        rec = resolve_active_recovery(step, mission, state=state)
    if not rec or rec == DEFAULT_RECOVERY:
        plan = approach_plan_from_state(state) if state is not None else {}
        pat = normalize_recovery_key(plan.get("pattern"), default="")
        if pat:
            rec = pat
    if rec in ("straight_in", "instrument"):
        return "per_ship"
    return "flight"


def landing_cleared_seats(state: dict[str, Any] | None) -> list[int]:
    if not isinstance(state, dict):
        return []
    raw = state.get("landing_cleared_seats") or []
    out: list[int] = []
    if isinstance(raw, list):
        for x in raw:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if n > 0 and n not in out:
                out.append(n)
    return out


def peek_next_landing_clear_seat(
    opus: OpusFlightContext | None,
    callsign: str,
    *,
    recovery: str | None = None,
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    ship: int | None = None,
) -> int | None:
    """
    Seat number for the next per-ship landing clearance, or None when the
    recovery clears the flight as a whole (or there is only one ship).
    """
    mode = landing_clearance_mode(
        recovery, step=step, mission=mission, state=state
    )
    ships = flight_ship_count(opus)
    flight_cs = flight_callsign_base(callsign, opus)
    _flight, seat_from_cs = parse_element_callsign(callsign)
    if mode != "per_ship" or ships <= 1 or not flight_cs:
        return None
    cleared = set(landing_cleared_seats(state))
    if ship is not None:
        try:
            target = int(ship)
        except (TypeError, ValueError):
            target = 0
        if 1 <= target <= ships:
            return target
    if seat_from_cs is not None and 1 <= seat_from_cs <= ships and seat_from_cs not in cleared:
        return seat_from_cs
    for n in range(1, ships + 1):
        if n not in cleared:
            return n
    return seat_from_cs if seat_from_cs is not None else 1


def commit_landing_clearance(state: dict[str, Any] | None) -> int | None:
    """Record the seat(s) that were just cleared to land (after TX)."""
    if not isinstance(state, dict):
        return None
    raw = state.pop("_landing_clear_built_seat", None)
    if raw is None:
        return None
    if str(raw).casefold() == "all":
        try:
            total = int(state.get("landing_ships_total") or 0)
        except (TypeError, ValueError):
            total = 0
        if total > 0:
            state["landing_cleared_seats"] = list(range(1, total + 1))
        return None
    try:
        seat = int(raw)
    except (TypeError, ValueError):
        return None
    if seat <= 0:
        return None
    cleared = landing_cleared_seats(state)
    if seat not in cleared:
        cleared.append(seat)
    state["landing_cleared_seats"] = cleared
    return seat


def landing_already_cleared(
    state: dict[str, Any] | None,
    *,
    opus: OpusFlightContext | None = None,
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
) -> bool:
    """True after Tower has already issued the landing clearance for this recovery."""
    if not isinstance(state, dict):
        return False
    seats = landing_cleared_seats(state)
    if not seats:
        return False
    mode = landing_clearance_mode(step=step, mission=mission, state=state)
    if mode == "flight":
        return True
    try:
        total = int(state.get("landing_ships_total") or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        total = flight_ship_count(opus)
    if total <= 1:
        return True
    return len(seats) >= total


def should_hold_for_landing_clearances(
    state: dict[str, Any] | None,
    *,
    opus: OpusFlightContext | None = None,
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
) -> bool:
    """True while straight-in / instrument still have uncleared ships."""
    if landing_clearance_mode(step=step, mission=mission, state=state) != "per_ship":
        return False
    try:
        total = int((state or {}).get("landing_ships_total") or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        total = flight_ship_count(opus)
    if total <= 1:
        return False
    return len(landing_cleared_seats(state)) < total


def build_clear_land(
    airport: dict[str, Any],
    callsign: str,
    weather: Weather,
    runway: str,
    *,
    opus: OpusFlightContext | None = None,
    recovery: str | None = None,
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    ship: int | None = None,
) -> str:
    """
    Military landing clearance: winds, runway, cleared to land, check gear down.

    Overhead / TAC overhead: one clearance for the flight (lead callsign).
    Straight-in / instrument: one ship per transmission; remaining ships are
    tracked in state and cleared on later Play / auto / voice calls.
    """
    name = airport["name"]
    wind = speak_wind(weather.wind_dir, weather.wind_speed_kt)
    rwy = speak_runway(runway)
    mode = landing_clearance_mode(
        recovery, step=step, mission=mission, state=state
    )
    ships = flight_ship_count(opus)
    flight_cs = flight_callsign_base(callsign, opus)
    target = peek_next_landing_clear_seat(
        opus,
        callsign,
        recovery=recovery,
        step=step,
        mission=mission,
        state=state,
        ship=ship,
    )

    if mode == "per_ship" and target is not None and flight_cs:
        # Fleece 1-1 / Fleece 1-2 → spoken "Fleece one one" / "Fleece one two"
        cs = speak_callsign(element_radio_callsign(flight_cs, target))
        if isinstance(state, dict):
            state["landing_ships_total"] = ships
            state["_landing_clear_built_seat"] = target
    else:
        cs = speak_callsign(callsign)
        if isinstance(state, dict):
            if mode == "flight":
                state["landing_ships_total"] = max(1, ships)
                state["_landing_clear_built_seat"] = "all"
            else:
                state.pop("_landing_clear_built_seat", None)

    intent = resolve_landing_intent(state)
    if intent == "low_approach":
        if isinstance(state, dict):
            state["awaiting_on_the_go"] = True
        return (
            f"{cs}, {name} Tower, {wind}, runway {rwy}, "
            f"cleared for the option."
        )
    return (
        f"{cs}, {name} Tower, {wind}, runway {rwy}, "
        f"cleared to land, check gear down."
    )


# --- Go-around / missed approach / VFR pattern work ------------------------

LANDING_INTENT_FULL_STOP = "full_stop"
LANDING_INTENT_LOW_APPROACH = "low_approach"
DEFAULT_REARM_TOWER_OUTSIDE_NM = 15.0
CLOSED_TRAFFIC_LAND_NM = 2.0
REENTRY_LAND_NM = 6.0


def resolve_landing_intent(state: dict[str, Any] | None = None) -> str:
    raw = str((state or {}).get("landing_intent") or "").strip().casefold()
    if raw in ("low_approach", "low", "option", "low approach"):
        return LANDING_INTENT_LOW_APPROACH
    return LANDING_INTENT_FULL_STOP


def set_landing_intent(
    intent: str | None,
    *,
    state: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
) -> str:
    key = str(intent or "").strip().casefold().replace(" ", "_").replace("-", "_")
    if key in ("low_approach", "low", "option"):
        val = LANDING_INTENT_LOW_APPROACH
    else:
        val = LANDING_INTENT_FULL_STOP
    for src in (state, mission):
        if isinstance(src, dict):
            src["landing_intent"] = val
            if val != LANDING_INTENT_LOW_APPROACH:
                src.pop("awaiting_on_the_go", None)
    return val


def awaiting_option_on_the_go(state: dict[str, Any] | None = None) -> bool:
    """True after 'cleared for the option' until on-the-go or full stop."""
    return bool(isinstance(state, dict) and state.get("awaiting_on_the_go"))


def commit_option_full_stop(
    *,
    state: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Pilot takes the option to a full stop — no second landing clearance.

    Clears the on-the-go wait so the flow can continue to exit / taxi-in.
    """
    set_landing_intent(LANDING_INTENT_FULL_STOP, state=state, mission=mission)
    if isinstance(state, dict):
        state.pop("awaiting_on_the_go", None)
        for key in (
            "landing_cleared_seats",
            "landing_ships_total",
            "_landing_clear_built_seat",
        ):
            state.pop(key, None)
    return {
        "landing_intent": LANDING_INTENT_FULL_STOP,
        "awaiting_on_the_go": False,
    }


def pattern_work_config(airport: dict[str, Any] | None) -> dict[str, Any]:
    catalog = load_approach_catalog(airport or {})
    raw = (catalog or {}).get("pattern_work") if isinstance(catalog, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}


def closed_traffic_side(runway: str | None, airport: dict[str, Any] | None) -> str:
    """'left' or 'right' closed traffic for this runway."""
    cfg = pattern_work_config(airport)
    by_rwy = cfg.get("closed_traffic_by_runway") or {}
    rwy = normalize_runway(runway) or ""
    if isinstance(by_rwy, dict):
        side = str(by_rwy.get(rwy) or "").strip().casefold()
        if side in ("left", "right"):
            return side
        # Fall back to number-only key (21 / 03) — uncommon
        digits = re.sub(r"[^0-9]", "", rwy)
        side = str(by_rwy.get(digits) or "").strip().casefold()
        if side in ("left", "right"):
            return side
    # Default: right for 21s, left for 03s
    return "right" if _runway_side(rwy) == "21" else "left"


def vfr_reentry_options(
    runway: str | None, airport: dict[str, Any] | None
) -> list[dict[str, Any]]:
    cfg = pattern_work_config(airport)
    rwy = normalize_runway(runway) or ""
    out: list[dict[str, Any]] = []
    for entry in cfg.get("reentries") or []:
        if not isinstance(entry, dict):
            continue
        allowed = [normalize_runway(x) for x in (entry.get("runways") or []) if x]
        if allowed and rwy not in allowed:
            continue
        out.append(dict(entry))
    return out


def _same_vfr_go_around(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if str(a.get("kind") or "") != str(b.get("kind") or ""):
        return False
    if str(a.get("kind") or "") == "reentry":
        return str(a.get("reentry_id") or "").upper() == str(b.get("reentry_id") or "").upper()
    return True


def pick_vfr_go_around_plan(
    runway: str | None,
    airport: dict[str, Any] | None,
    *,
    prefer: str | None = None,
    randomize: bool | None = None,
    last: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Choose closed traffic (L/R by runway) or Flex(21R) / Duck(21L) reentry.

    Random mix — does not repeat the last pick when another option exists.
    Returns {kind, side?, reentry_id?, reentry_say?, runway, randomize}.
    """
    cfg = pattern_work_config(airport)
    rwy = normalize_runway(runway) or "21R"
    side = closed_traffic_side(rwy, airport)
    reentries = vfr_reentry_options(rwy, airport)
    do_rand = bool(cfg.get("randomize", True)) if randomize is None else bool(randomize)
    pref = str(prefer or "").strip().casefold().replace(" ", "_").replace("-", "_")

    options: list[dict[str, Any]] = [
        {
            "kind": "closed_traffic",
            "side": side,
            "runway": rwy,
            "label": f"{side} closed traffic",
        }
    ]
    for ent in reentries:
        rid = str(ent.get("id") or "").strip().upper()
        say = str(ent.get("say") or f"{rid} reentry").strip()
        options.append(
            {
                "kind": "reentry",
                "reentry_id": rid,
                "reentry_say": say,
                "runway": rwy,
                "label": say,
            }
        )

    chosen: dict[str, Any] | None = None
    if pref in ("closed", "closed_traffic", "traffic"):
        chosen = options[0]
    elif pref:
        for opt in options[1:]:
            if pref in {
                str(opt.get("reentry_id") or "").casefold(),
                str(opt.get("reentry_say") or "").casefold().replace(" ", "_"),
            }:
                chosen = opt
                break
        if chosen is None and "flex" in pref:
            chosen = next(
                (o for o in options if str(o.get("reentry_id") or "").upper() == "FLEX"),
                None,
            )
        if chosen is None and "duck" in pref:
            chosen = next(
                (o for o in options if str(o.get("reentry_id") or "").upper() == "DUCK"),
                None,
            )
    if chosen is None:
        pool = list(options)
        if do_rand and last and len(pool) > 1:
            alt = [o for o in pool if not _same_vfr_go_around(o, last)]
            if alt:
                pool = alt
        chosen = random.choice(pool) if do_rand and pool else options[0]
    chosen = dict(chosen)
    chosen["randomize"] = do_rand
    return chosen


def instrument_missed_plan(
    airport: dict[str, Any] | None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Published missed instructions + return IAF from the active instrument."""
    cfg = pattern_work_config(airport)
    default = dict(cfg.get("default_missed_approach") or {})
    plan = approach_plan_from_state(state) if state else {}
    catalog = load_approach_catalog(airport or {})
    inst_id = str(plan.get("instrument_id") or "").strip()
    inst = find_instrument_approach(catalog, token=inst_id) if catalog and inst_id else None
    missed = dict((inst or {}).get("missed_approach") or {}) if inst else {}
    say = str(missed.get("say") or default.get("say") or "missed approach as published")
    alt = missed.get("altitude_ft", default.get("altitude_ft"))
    iaf_say = str(plan.get("iaf_say") or plan.get("iaf") or "the IAF")
    try:
        rearm = float(
            cfg.get("rearm_tower_outside_nm")
            if cfg.get("rearm_tower_outside_nm") is not None
            else DEFAULT_REARM_TOWER_OUTSIDE_NM
        )
    except (TypeError, ValueError):
        rearm = DEFAULT_REARM_TOWER_OUTSIDE_NM
    return {
        "kind": "instrument_missed",
        "say": say,
        "altitude_ft": alt,
        "iaf": plan.get("iaf"),
        "iaf_say": iaf_say,
        "instrument_say": plan.get("instrument_say") or plan.get("instrument_id"),
        "runway": plan.get("runway"),
        "rearm_tower_outside_nm": rearm,
        "return_channel": "approach",
    }


def assign_go_around_plan(
    airport: dict[str, Any],
    *,
    runway: str | None = None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    prefer: str | None = None,
) -> dict[str, Any]:
    """
    Build and stick the go-around / missed plan for the active recovery.

    VFR closed traffic → stay on Tower, seek land on base / short final.
    Flex / Duck reentry → switch to straight-in, seek land on short final.
    Already with Tower — no second check-in / initial.
    Instrument → published missed + return to Approach (rearm tower gates).
    """
    rec = resolve_active_recovery(None, mission, state=state)
    plan_rwy = ""
    if isinstance(state, dict):
        ap = approach_plan_from_state(state, airport=airport)
        plan_rwy = str(ap.get("runway") or state.get("approach_runway") or "")
    rwy = normalize_runway(runway) or plan_rwy or "21R"
    if rec == "instrument":
        ga = instrument_missed_plan(airport, state)
    else:
        last = dict(state.get("go_around_plan") or {}) if isinstance(state, dict) else {}
        ga = pick_vfr_go_around_plan(rwy, airport, prefer=prefer, last=last or None)
        ga["return_channel"] = "tower"
        if str(ga.get("kind") or "") == "reentry":
            # Flex / Duck is a straight-in come-back, not another overhead.
            _remember_overhead_recovery(rec, state=state)
            ga["seek_template"] = "clear_land"
            rec = "straight_in"
            _apply_reentry_straight_in(mission=mission, state=state)
        else:
            # Closed traffic is a pattern — restore overhead, not straight-in.
            rec = _apply_closed_traffic_overhead(mission=mission, state=state)
            ga["seek_template"] = "clear_land"
    ga["recovery"] = rec
    if isinstance(state, dict):
        state["go_around_plan"] = dict(ga)
        if ga.get("kind") in ("closed_traffic", "reentry"):
            # Already with Tower — next call is land on base / short final.
            # Must leave the current final first so a waveoff does not re-clear.
            state["pattern_land_needs_leave"] = True
        if ga.get("kind") == "instrument_missed":
            try:
                state["rearm_tower_outside_nm"] = float(
                    ga.get("rearm_tower_outside_nm") or DEFAULT_REARM_TOWER_OUTSIDE_NM
                )
            except (TypeError, ValueError):
                state["rearm_tower_outside_nm"] = DEFAULT_REARM_TOWER_OUTSIDE_NM
        state.pop("awaiting_on_the_go", None)
        # Fresh landing sequence after the go
        for key in (
            "landing_cleared_seats",
            "landing_ships_total",
            "_landing_clear_built_seat",
        ):
            state.pop(key, None)
    return ga


_OVERHEAD_RECOVERIES = frozenset({"visual_overhead", "tactical_overhead"})


def _remember_overhead_recovery(
    rec: str,
    *,
    state: dict[str, Any] | None = None,
) -> None:
    key = normalize_recovery_key(rec, default="")
    if key in _OVERHEAD_RECOVERIES and isinstance(state, dict):
        state["overhead_recovery"] = key


def _apply_reentry_straight_in(
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Flex / Duck: live recovery becomes straight-in for the come-back."""
    for src in (state, mission):
        if isinstance(src, dict):
            src["active_recovery"] = "straight_in"
    if not isinstance(state, dict):
        return
    plan = dict(state.get("approach_plan") or {})
    if plan:
        plan["pattern"] = "straight_in"
        state["approach_plan"] = plan


def _apply_closed_traffic_overhead(
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    """
    Closed traffic stays in the pattern — not a 6/12 NM straight-in.

    Restore the overhead that was active before a Flex/Duck, or TAC overhead
    when the live recovery had been switched to straight-in.
    """
    rec = ""
    if isinstance(state, dict):
        rec = normalize_recovery_key(state.get("overhead_recovery") or "", default="")
    if rec not in _OVERHEAD_RECOVERIES:
        current = resolve_active_recovery(None, mission, state=state)
        rec = current if current in _OVERHEAD_RECOVERIES else "tactical_overhead"
    for src in (state, mission):
        if isinstance(src, dict):
            src["active_recovery"] = rec
    if isinstance(state, dict):
        plan = dict(state.get("approach_plan") or {})
        if plan:
            plan["pattern"] = rec
            state["approach_plan"] = plan
        state["overhead_recovery"] = rec
    return rec


def reentry_go_around_pending(state: dict[str, Any] | None) -> bool:
    """
    True after Flex / Duck until Tower issues the come-back landing clearance.

    Used to keep Watch from treating the jet as a 6 NM straight-in while it
    is still climbing out at the field.
    """
    if not isinstance(state, dict):
        return False
    ga = state.get("go_around_plan")
    if not isinstance(ga, dict) or str(ga.get("kind") or "") != "reentry":
        return False
    return not landing_already_cleared(state)


def pattern_land_within_nm(state: dict[str, Any] | None) -> float | None:
    """
    Auto-land distance after a VFR go-around (already with Tower).

    Closed traffic: tight pattern — base / short final (~2 NM).
    Flex / Duck: straight-in come-back (~6 NM).
    """
    if closed_traffic_go_around_pending(state):
        return CLOSED_TRAFFIC_LAND_NM
    if reentry_go_around_pending(state):
        return REENTRY_LAND_NM
    return None


def closed_traffic_go_around_pending(state: dict[str, Any] | None) -> bool:
    """
    True after a closed-traffic go-around until the next landing clearance.

    Watch must not treat the jet as a straight-in inside 12/6 NM — they are
    still at the field in the pattern.
    """
    if not isinstance(state, dict):
        return False
    ga = state.get("go_around_plan")
    if not isinstance(ga, dict) or str(ga.get("kind") or "") != "closed_traffic":
        return False
    return not landing_already_cleared(state)


def tower_land_gates_allowed(
    state: dict[str, Any] | None,
    distance_nm: float | None,
) -> tuple[bool, str]:
    """
    After an instrument missed, suppress contact-tower / land until outside
    rearm_tower_outside_nm so a near-field Approach rewind cannot auto-hand
    off to Tower / clear to land immediately.
    """
    if not isinstance(state, dict):
        return True, ""
    raw = state.get("rearm_tower_outside_nm")
    if raw is None or raw == "":
        return True, ""
    try:
        need = float(raw)
    except (TypeError, ValueError):
        state.pop("rearm_tower_outside_nm", None)
        return True, ""
    if distance_nm is not None and float(distance_nm) >= need:
        state.pop("rearm_tower_outside_nm", None)
        return True, ""
    if distance_nm is None:
        return False, f"missed approach — need >= {need:g} NM before tower/land re-arms"
    return (
        False,
        f"missed approach — {float(distance_nm):.1f} NM, need >= {need:g} NM to re-arm tower/land",
    )


def build_go_around(
    airport: dict[str, Any],
    callsign: str,
    runway: str | None = None,
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    prefer: str | None = None,
) -> str:
    """
    Tower go-around / instrument missed.

    VFR: go around + left/right closed traffic, or Flex/Duck reentry.
    Instrument: missed as published, contact Approach (cycle restarts there).
    """
    name = airport["name"]
    cs = speak_callsign(callsign)
    ga = assign_go_around_plan(
        airport,
        runway=runway,
        mission=mission,
        state=state,
        prefer=prefer,
    )
    rwy = speak_runway(str(ga.get("runway") or runway or ""))
    kind = str(ga.get("kind") or "")
    if kind == "instrument_missed":
        body = str(ga.get("say") or "missed approach as published").rstrip(".")
        iaf = str(ga.get("iaf_say") or "the IAF")
        # Keep this short — full Approach Local/freq is on the Approach check-in.
        return (
            f"{cs}, {name} Tower, {body}, "
            f"contact {name} Approach, expect {iaf}."
        )
    if kind == "reentry":
        reentry = str(ga.get("reentry_say") or "reentry")
        if rwy:
            return (
                f"{cs}, {name} Tower, go around, {reentry} approved runway {rwy}."
            )
        return f"{cs}, {name} Tower, go around, {reentry} approved."
    side = str(ga.get("side") or closed_traffic_side(runway, airport))
    if rwy:
        return (
            f"{cs}, {name} Tower, go around, make {side} closed traffic runway {rwy}."
        )
    return f"{cs}, {name} Tower, go around, make {side} closed traffic."


def build_blackjack_range_exit(
    airport: dict[str, Any],
    callsign: str,
    *,
    handoff_channel: str = "approach",
    plan: dict[str, Any] | None = None,
    include_handoff: bool = True,
) -> str:
    """
    Blackjack range exit: proceed direct to the exit / recovery fix, and
    start the descent to the IAF / recovery altitude at pilot discretion
    so they can make it before Approach picks them up.

    Approach handoff is only when include_handoff is True (inside ~40 NM).
    Farther out they stay this frequency. Expect recovery stays on Approach.
    """
    cs = speak_callsign(callsign)
    p = dict(plan or {})
    dest = _plan_direct_say(p, airport, runway=str(p.get("runway") or ""))
    direct = f", proceed direct {dest}" if dest else ""
    descend = ""
    dft = p.get("descend_ft")
    if dft:
        try:
            clause = speak_descend_pilot_discretion(int(dft))
            if clause:
                descend = f", {clause}"
        except (TypeError, ValueError):
            pass
    if not include_handoff:
        return (
            f"{cs}, Blackjack, range exit approved{direct}{descend}, "
            f"remain this frequency."
        )
    target = speak_agency_contact_target(airport, handoff_channel or "approach")
    return with_freq_handoff_closer(
        f"{cs}, Blackjack, range exit approved{direct}{descend}, contact {target}"
    )


def build_blackjack_approach_handoff(
    airport: dict[str, Any],
    callsign: str,
    *,
    handoff_channel: str = "approach",
) -> str:
    """Blackjack → Approach after they are already released to the exit fix."""
    cs = speak_callsign(callsign)
    target = speak_agency_contact_target(airport, handoff_channel or "approach")
    return with_freq_handoff_closer(f"{cs}, Blackjack, contact {target}")


def build_blackjack_continue(
    callsign: str,
    *,
    alpha_bullseye: str | None = None,
) -> str:
    """
    Back on Blackjack after Bandsaw (or still working the range).

    Do not hand to Approach yet — that waits for range exit near the boundary.
    """
    cs = speak_callsign(callsign)
    if alpha_bullseye:
        return (
            f"{cs}, Blackjack, radar contact {alpha_bullseye}. "
            f"Continue, remain this frequency."
        )
    return f"{cs}, Blackjack, radar contact. Continue, remain this frequency."


def build_bandsaw_check_in(
    callsign: str,
    *,
    alpha_bullseye: str | None = None,
) -> str:
    """Bandsaw C2 check-in: radar contact, then alpha check."""
    cs = speak_callsign(callsign)
    if alpha_bullseye:
        return f"{cs}, Bandsaw, radar contact. Alpha check {alpha_bullseye}."
    return f"{cs}, Bandsaw, radar contact. Alpha check."


def build_bandsaw_check_out(
    airport: dict[str, Any],
    callsign: str,
    *,
    handoff_channel: str = "blackjack",
) -> str:
    """Bandsaw checkout — acknowledge and push back to Blackjack."""
    cs = speak_callsign(callsign)
    target = speak_agency_contact_target(airport, handoff_channel or "blackjack")
    return with_freq_handoff_closer(
        _pick(
            f"{cs}, Bandsaw, copy, contact {target}",
            f"{cs}, Bandsaw, roger, contact {target}",
        )
    )


def build_contact_bandsaw(airport: dict[str, Any], callsign: str) -> str:
    """Stub Blackjack push to Bandsaw."""
    cs = speak_callsign(callsign)
    target = speak_agency_contact_target(airport, "bandsaw")
    return with_freq_handoff_closer(f"{cs}, Blackjack, contact {target}")


# --- CAOC radar / bullseye alpha check ---------------------------------
# Match Opus CAOC aircraft popup math (/opus/caoc): click unit → tooltip
#   "ELVIS 305 25"  (name + mag bearing 000-359 + range NM, zero-padded to 2)
# Unit xz → lat/lon uses CAOC's planar L(x,z); bullseye is WGS84 ELVIS.

_CAOC_XZ_ORIGIN_LAT = 36.2362
_CAOC_XZ_ORIGIN_LON = -115.0343
_CAOC_XZ_METERS_PER_DEG_LAT = 110540.0
_CAOC_XZ_METERS_PER_DEG_LON = 111320.0 * math.cos(math.radians(_CAOC_XZ_ORIGIN_LAT))
# Approx magnetic declination °E for NTTR (true → magnetic: subtract)
_NTTR_MAG_DECLINATION_E_DEG = 12.0
_CAOC_DEFAULT_BULLSEYE = {"name": "ELVIS", "lat": 37.25, "lon": -115.75}

_caoc_radar_cache: dict[str, Any] = {"t": 0.0, "data": None}
_theater_nav_cache: dict[int, list[dict[str, Any]]] = {}


def speak_natural_number(n: int) -> str:
    """Conversational number speech for ranges (twenty five), not ICAO digits."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "zero"
    if n < 0:
        return speak_natural_number(-n)
    ones = (
        "zero one two three four five six seven eight nine ten "
        "eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
    ).split()
    tens = (
        "zero ten twenty thirty forty fifty sixty seventy eighty ninety"
    ).split()
    if n < 20:
        return ones[n]
    if n < 100:
        t, o = divmod(n, 10)
        return tens[t] if o == 0 else f"{tens[t]} {ones[o]}"
    if n < 1000:
        h, rem = divmod(n, 100)
        if rem == 0:
            return f"{ones[h]} hundred"
        return f"{ones[h]} hundred {speak_natural_number(rem)}"
    return speak_digits(str(n))


def speak_bullseye_fix(name: str) -> str:
    """Bullseye code name as spoken (ELVIS → elvis)."""
    text = re.sub(r"[^A-Za-z0-9 ]+", " ", str(name or "")).strip()
    if not text:
        return "bullseye"
    # Drop parenthetical / DEFAULT BLUE suffixes
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\bDEFAULT\b.*", "", text, flags=re.I).strip()
    text = re.sub(r"\s+", " ", text)
    return text if text else "bullseye"


def speak_alpha_bullseye(name: str, bearing_deg: int, range_nm: int) -> str:
    """
    Alpha-check bullseye speech:
      'ELVIS three zero fife twenty five'
    """
    be = speak_bullseye_fix(name)
    brg = max(0, min(360, int(bearing_deg))) % 360
    rng = max(0, int(range_nm))
    return f"{be} {speak_digits(f'{brg:03d}')} {speak_natural_number(rng)}"


def speak_picture_bullseye(name: str, bearing_deg: int, range_nm: int) -> str:
    """
    Picture-call bullseye: ICAO bearing, then range as a whole number.

    Example: 'ELVIS two niner fife, one hundred fourteen'
    The comma keeps Chirp from list-reading bearing digits into the range.
    """
    be = speak_bullseye_fix(name)
    brg = max(0, min(360, int(bearing_deg))) % 360
    rng = max(0, int(range_nm))
    return f"{be} {speak_digits(f'{brg:03d}')}, {speak_natural_number(rng)}"


def format_caoc_bullseye_display(name: str, bearing_deg: int, range_nm: int) -> str:
    """Same string CAOC puts in the unit popup/tooltip: 'ELVIS 305 25'."""
    be = re.sub(r"\s+", " ", str(name or "BULLSEYE")).strip() or "BULLSEYE"
    brg = max(0, int(bearing_deg)) % 360
    rng = max(0, int(range_nm))
    return f"{be} {brg:03d} {rng:02d}"


def caoc_xz_to_ll(x_meters: float, z_meters: float) -> tuple[float, float]:
    """CAOC map L(x,z): radar metres → lat/lon (matches /opus/caoc popup)."""
    lat = _CAOC_XZ_ORIGIN_LAT + float(z_meters) / _CAOC_XZ_METERS_PER_DEG_LAT
    lon = _CAOC_XZ_ORIGIN_LON + float(x_meters) / _CAOC_XZ_METERS_PER_DEG_LON
    return lat, lon


def caoc_ll_to_xz(lat: float, lon: float) -> tuple[float, float]:
    """
    Inverse of caoc_xz_to_ll: lat/lon → CAOC radar metres.

    The x/z origin is the Nellis reference point, so around KLSV this round-trips
    to well under a metre — good enough to place runway edges.
    """
    x = (float(lon) - _CAOC_XZ_ORIGIN_LON) * _CAOC_XZ_METERS_PER_DEG_LON
    z = (float(lat) - _CAOC_XZ_ORIGIN_LAT) * _CAOC_XZ_METERS_PER_DEG_LAT
    return x, z


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)) * 6371000.0 / 1852.0


def _true_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing from point 1 → point 2 (CAOC bearingDeg)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def caoc_bullseye_brg_rng_nm(
    unit_lat: float,
    unit_lon: float,
    be_lat: float,
    be_lon: float,
    *,
    magnetic_declination_e_deg: float = _NTTR_MAG_DECLINATION_E_DEG,
) -> tuple[int, int]:
    """Magnetic bearing + range NM from bullseye → unit (CAOC popup rounding)."""
    true_brg = _true_bearing_deg(be_lat, be_lon, unit_lat, unit_lon)
    mag_brg = (true_brg - float(magnetic_declination_e_deg)) % 360.0
    rng_nm = _haversine_nm(be_lat, be_lon, unit_lat, unit_lon)
    return int(round(mag_brg)) % 360, int(round(rng_nm))


def ll_at_bullseye_brg_rng(
    be_lat: float,
    be_lon: float,
    mag_bearing_deg: float,
    range_nm: float,
    *,
    magnetic_declination_e_deg: float = _NTTR_MAG_DECLINATION_E_DEG,
) -> tuple[float, float]:
    """
    Lat/lon of a point at magnetic bearing/range from bullseye.

    Inverse of caoc_bullseye_brg_rng_nm (WGS84 great-circle).
    """
    true_brg = (float(mag_bearing_deg) + float(magnetic_declination_e_deg)) % 360.0
    # Earth radius in NM
    r_nm = 3440.065
    δ = float(range_nm) / r_nm
    θ = math.radians(true_brg)
    φ1 = math.radians(float(be_lat))
    λ1 = math.radians(float(be_lon))
    φ2 = math.asin(
        math.sin(φ1) * math.cos(δ) + math.cos(φ1) * math.sin(δ) * math.cos(θ)
    )
    λ2 = λ1 + math.atan2(
        math.sin(θ) * math.sin(δ) * math.cos(φ1),
        math.cos(δ) - math.sin(φ1) * math.sin(φ2),
    )
    return math.degrees(φ2), ((math.degrees(λ2) + 540.0) % 360.0) - 180.0


def parse_caoc_bullseye_text(text: str) -> tuple[str, int, int] | None:
    """Parse CAOC tooltip/atcPosition 'ELVIS 305 25' → (name, brg, rng)."""
    m = re.match(
        r"^\s*([A-Za-z][A-Za-z0-9]*)\s+(\d{1,3})\s+(\d{1,3})\s*$",
        str(text or "").strip(),
    )
    if not m:
        return None
    name, brg_s, rng_s = m.group(1), m.group(2), m.group(3)
    brg, rng = int(brg_s) % 360, int(rng_s)
    return name, brg, rng


def fetch_caoc_radar(config: dict[str, Any], *, max_age_s: float = 5.0) -> dict[str, Any] | None:
    """Latest CAOC radar snapshot from Opus (`GET /opus/caoc/radar`)."""
    now = time.time()
    cached = _caoc_radar_cache.get("data")
    if cached is not None and (now - float(_caoc_radar_cache.get("t") or 0)) < max_age_s:
        return cached
    backend = (config.get("opus_backend_url") or "").rstrip("/")
    if not backend:
        return None
    ua = str(config.get("user_agent") or "DCS-ATC-Phrase/1.0")
    try:
        data = http_get_json(f"{backend}/opus/caoc/radar", ua)
    except Exception:
        return cached if isinstance(cached, dict) else None
    if not isinstance(data, dict):
        return None
    _caoc_radar_cache["t"] = now
    _caoc_radar_cache["data"] = data
    return data


def fetch_theater_navpoints(config: dict[str, Any], theater_id: int) -> list[dict[str, Any]]:
    if theater_id in _theater_nav_cache:
        return _theater_nav_cache[theater_id]
    backend = (config.get("opus_backend_url") or "").rstrip("/")
    if not backend:
        return []
    ua = str(config.get("user_agent") or "DCS-ATC-Phrase/1.0")
    try:
        data = http_get_json(f"{backend}/theaters/{int(theater_id)}", ua)
    except Exception:
        return []
    nav = list(data.get("navpoints") or []) if isinstance(data, dict) else []
    _theater_nav_cache[theater_id] = nav
    return nav


def resolve_bullseye_navpoint(
    config: dict[str, Any],
    *,
    theater_id: int | None = None,
    preferred_name: str | None = None,
) -> dict[str, Any] | None:
    """
    Prefer config/bullseye name (default ELVIS), else BULLSEYE (DEFAULT BLUE),
    else CAOC default ELVIS 37.25/-115.75.
    """
    tid = theater_id or int(config.get("opus_theater_id") or 1)
    nav = fetch_theater_navpoints(config, int(tid))
    want = (preferred_name or config.get("bullseye_navpoint") or "ELVIS").strip()
    want_cf = want.casefold()
    for n in nav:
        if str(n.get("name") or "").strip().casefold() == want_cf:
            return n
    for n in nav:
        name = str(n.get("name") or "")
        if "BULLSEYE" in name.upper() and "BLUE" in name.upper():
            return n
    for n in nav:
        if "BULLSEYE" in str(n.get("name") or "").upper():
            return n
    # Same default the CAOC map uses when localStorage has no override
    return dict(_CAOC_DEFAULT_BULLSEYE)


def _norm_match_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").casefold())


def caoc_air_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Air tracks suitable for CAOC map / alpha check (skip FARPs, numeric junk names)."""
    out: list[dict[str, Any]] = []
    for u in units:
        if not isinstance(u, dict):
            continue
        if str(u.get("type") or "").lower() != "air":
            continue
        obj = str(u.get("objectName") or "").upper()
        if any(tok in obj for tok in ("FARP", "CONTAINER", "HELIPAD", "INVISIBLE")):
            continue
        name = str(u.get("name") or "").strip()
        if not name or re.fullmatch(r"\d+", name):
            continue
        if "farp" in name.casefold():
            continue
        out.append(u)
    return out


# Theater fixtures that are always on the CAOC picture (hostile tanker / AWACS).
# Matched on DCS objectName and common group/callsign wording.
_PICTURE_FIXTURE_OBJECT_TOKENS: tuple[str, ...] = (
    "E-3A",
    "E-3",
    "E-2C",
    "E-2D",
    "E-2",
    "A-50",
    "KJ-2000",
    "A-100",
    "KC-135",
    "KC135",
    "KC130",
    "KC-130",
    "KC_10",
    "KC-10",
    "IL-78",
    "S-3B TANKER",
    "S-3BTANKER",
)
_PICTURE_FIXTURE_ROLE_WORDS: tuple[str, ...] = (
    "awacs",
    "aew",
    "tanker",
    "tankers",
)


def caoc_unit_is_picture_fixture(unit: dict[str, Any]) -> bool:
    """
    True for always-present support tracks (AWACS / tankers).

    These stay on the CAOC feed all sortie and should not fill a picture call.
    """
    obj_raw = str(unit.get("objectName") or unit.get("object_name") or "")
    obj_u = obj_raw.upper()
    obj_compact = re.sub(r"[\s_\-]+", "", obj_u)
    for tok in _PICTURE_FIXTURE_OBJECT_TOKENS:
        t = tok.upper()
        if t in obj_u or re.sub(r"[\s_\-]+", "", t) in obj_compact:
            return True
    blob = " ".join(
        str(unit.get(k) or "")
        for k in (
            "name",
            "groupName",
            "unitName",
            "flightLabel",
            "unitCallsign",
            "pilotName",
            "objectName",
        )
    ).casefold()
    for word in _PICTURE_FIXTURE_ROLE_WORDS:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", blob):
            return True
    attrs = unit.get("attributes") or unit.get("Attrs") or unit.get("attr") or []
    if isinstance(attrs, dict):
        attrs = list(attrs.keys()) + list(attrs.values())
    for a in attrs if isinstance(attrs, (list, tuple)) else []:
        al = str(a or "").casefold()
        if al in {"awacs", "tankers", "tanker"} or "awacs" in al or "tanker" in al:
            return True
    return False


def radio_callsign_from_caoc_unit(unit: dict[str, Any]) -> str:
    """Best radio callsign for a CAOC track (no Opus FP required)."""
    for key in ("flightLabel", "unitCallsign"):
        v = str(unit.get(key) or "").strip()
        if "#" in v:
            v = v.split("#", 1)[0].strip()
        if v:
            return v
    name = str(unit.get("name") or "").strip()
    name = re.sub(r"\s*[@#]IFF:.*$", "", name, flags=re.I).strip()
    if "|" in name:
        name = name.split("|")[-1].strip()
    if name:
        return name
    group = str(unit.get("groupName") or unit.get("unitName") or "").strip()
    if "#" in group:
        group = group.split("#", 1)[0].strip()
    return group or "Aircraft"


def caoc_unit_label(unit: dict[str, Any]) -> str:
    """Human label for a CAOC air track (pilot · callsign · name)."""
    name = str(unit.get("name") or "").strip()
    pilot = str(unit.get("pilotName") or "").strip()
    cs = radio_callsign_from_caoc_unit(unit)
    ac = str(unit.get("objectName") or "").strip()
    bits = [b for b in (pilot, cs, ac) if b]
    # De-dupe while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for b in bits:
        k = b.casefold()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(b)
    return " · ".join(uniq) if uniq else name or "Aircraft"


def bullseye_for_caoc_unit(
    unit: dict[str, Any],
    config: dict[str, Any],
    *,
    opus: OpusFlightContext | None = None,
) -> dict[str, Any] | None:
    """
    Bullseye fix for one CAOC unit — same numbers as the /opus/caoc unit popup.
    Returns {name, bearing, range_nm, display, spoken, unit_name, label, …} or None.
    """
    raw_pos = str(unit.get("atcPosition") or "").strip()
    parsed = parse_caoc_bullseye_text(raw_pos) if raw_pos else None
    label = caoc_unit_label(unit)
    radio_cs = radio_callsign_from_caoc_unit(unit)
    unit_name = unit.get("name") or unit.get("groupName")

    unit_lat: float | None = None
    unit_lon: float | None = None
    try:
        ux, uz = float(unit["xMeters"]), float(unit["zMeters"])
        unit_lat, unit_lon = caoc_xz_to_ll(ux, uz)
    except (KeyError, TypeError, ValueError):
        pass

    if parsed:
        name, brg, rng = parsed
        out = {
            "name": name,
            "bearing": brg,
            "range_nm": rng,
            "display": format_caoc_bullseye_display(name, brg, rng),
            "spoken": speak_alpha_bullseye(name, brg, rng),
            "unit_name": unit_name,
            "radio_callsign": radio_cs,
            "label": label,
            "pilot_name": unit.get("pilotName"),
            "object_name": unit.get("objectName"),
            "unit_id": unit.get("id"),
        }
        if unit_lat is not None and unit_lon is not None:
            out["lat"] = unit_lat
            out["lon"] = unit_lon
        return out

    tid = opus.theater_id if opus and opus.theater_id else int(config.get("opus_theater_id") or 1)
    be = resolve_bullseye_navpoint(config, theater_id=tid) or dict(_CAOC_DEFAULT_BULLSEYE)
    try:
        be_lat, be_lon = float(be["lat"]), float(be["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if unit_lat is None or unit_lon is None:
        return None
    try:
        decl = float(config.get("bullseye_magnetic_declination_deg", _NTTR_MAG_DECLINATION_E_DEG))
    except (TypeError, ValueError):
        decl = _NTTR_MAG_DECLINATION_E_DEG
    brg, rng = caoc_bullseye_brg_rng_nm(
        unit_lat, unit_lon, be_lat, be_lon, magnetic_declination_e_deg=decl
    )
    if rng > 400:
        return None
    name = str(be.get("name") or "ELVIS")
    name = re.split(r"[\s(]", name, maxsplit=1)[0] or "ELVIS"
    return {
        "name": name,
        "bearing": brg,
        "range_nm": rng,
        "display": format_caoc_bullseye_display(name, brg, rng),
        "spoken": speak_alpha_bullseye(name, brg, rng),
        "unit_name": unit_name,
        "radio_callsign": radio_cs,
        "label": label,
        "pilot_name": unit.get("pilotName"),
        "object_name": unit.get("objectName"),
        "unit_id": unit.get("id"),
        "lat": unit_lat,
        "lon": unit_lon,
    }


def build_standalone_alpha_check(callsign: str, alpha_bullseye: str | None = None) -> str:
    """On-demand Blackjack alpha check (any time, any matched track)."""
    cs = speak_callsign(callsign)
    if alpha_bullseye:
        return f"{cs}, Blackjack, alpha check {alpha_bullseye}."
    return f"{cs}, Blackjack, alpha check."


def list_caoc_air_bullseyes(
    config: dict[str, Any],
    *,
    query: str | None = None,
    max_age_s: float = 0.0,
) -> list[dict[str, Any]]:
    """
    Live CAOC air tracks with bullseye computed (no Opus flight plan required).
    Optional query filters label/name/pilot (e.g. 'damn').
    """
    radar = fetch_caoc_radar(config, max_age_s=max_age_s)
    if not radar:
        return []
    q = (query or "").strip().casefold()
    rows: list[dict[str, Any]] = []
    for u in caoc_air_units(list(radar.get("units") or [])):
        fix = bullseye_for_caoc_unit(u, config)
        if not fix:
            continue
        if q:
            blob = " ".join(
                str(x or "")
                for x in (
                    fix.get("label"),
                    fix.get("unit_name"),
                    fix.get("pilot_name"),
                    u.get("groupName"),
                    u.get("flightLabel"),
                )
            ).casefold()
            if q not in blob:
                continue
        rows.append(fix)
    # Manned / named first, then by label
    rows.sort(
        key=lambda r: (
            0 if r.get("pilot_name") else 1,
            str(r.get("label") or "").casefold(),
        )
    )
    return rows


def match_caoc_unit_for_flight(
    units: list[dict[str, Any]],
    *,
    callsign: str | None = None,
    opus: OpusFlightContext | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Pick the CAOC radar unit for this flight (air only).
    Match order: opusFlightId → pilot name → flightLabel/unitCallsign → squawk → name tokens.

    DCS often puts the player name in `name` with `pilotName` empty — treat that
    as a pilot hit. A bare "has squawk" bonus must never win by itself (that is
    how tanker / AWACS tracks used to beat the jet on the ramp).
    """
    config = config or {}
    air = caoc_air_units(units)
    if not air:
        return None

    fid = opus.flight_id if opus else configured_opus_flight_id(config)
    mode3 = (opus.mode3 if opus else None) or None
    pilot = (config.get("opus_user_name") or "").strip()
    pilot_n = _norm_match_token(pilot)
    cs = (opus.radio_callsign if opus else None) or callsign or ""
    cs_tokens = [
        _norm_match_token(t)
        for t in re.split(r"[\s\-/|_]+", cs)
        if _norm_match_token(t) and _norm_match_token(t) not in {"the", "flight"}
    ]
    # Drop lone seat digits for matching
    cs_tokens = [t for t in cs_tokens if not t.isdigit()]

    def _unit_pilot_blob(u: dict[str, Any]) -> str:
        for key in ("pilotName", "name"):
            raw = str(u.get(key) or "").strip()
            if not raw:
                continue
            raw = re.sub(r"\s*[@#]IFF:.*$", "", raw, flags=re.I).strip()
            if "|" in raw:
                raw = raw.split("|")[-1].strip()
            if raw:
                return raw
        return ""

    scored: list[tuple[int, dict[str, Any]]] = []
    for u in air:
        score = 0
        if fid is not None and u.get("opusFlightId") is not None:
            try:
                if int(u["opusFlightId"]) == int(fid):
                    score += 100
            except (TypeError, ValueError):
                pass
        label = str(u.get("flightLabel") or u.get("unitCallsign") or "")
        if label and cs and _norm_match_token(label) and _norm_match_token(label) in _norm_match_token(cs):
            score += 80
        if label and cs_tokens and _norm_match_token(label) in cs_tokens:
            score += 70
        unit_pilot = _unit_pilot_blob(u)
        if pilot_n and unit_pilot and _norm_match_token(unit_pilot) == pilot_n:
            score += 90
        if mode3 and u.get("squawk"):
            sq = re.sub(r"\D", "", str(u.get("squawk")))
            m3 = re.sub(r"\D", "", str(mode3))
            if sq and m3 and sq == m3:
                score += 60
        blob = " ".join(
            str(u.get(k) or "")
            for k in ("name", "groupName", "unitName", "flightLabel", "unitCallsign")
        )
        blob_n = _norm_match_token(blob)
        for tok in cs_tokens:
            if len(tok) >= 3 and tok in blob_n:
                score += 40
                break
        # Tie-breaker only — never the sole reason a track wins.
        if score > 0 and (u.get("pilotName") or u.get("flightLabel") or u.get("squawk")):
            score += 5
        if score > 0:
            scored.append((score, u))

    if not scored:
        return None
    scored.sort(key=lambda it: it[0], reverse=True)
    return scored[0][1]


def resolve_alpha_bullseye(
    config: dict[str, Any],
    *,
    callsign: str | None = None,
    opus: OpusFlightContext | None = None,
) -> dict[str, Any] | None:
    """
    Live alpha-check fix from Opus CAOC radar — same numbers as the unit popup
    when you click an aircraft on /opus/caoc.
    Returns {name, bearing, range_nm, display, spoken, unit_name} or None.
    """
    radar = fetch_caoc_radar(config)
    if not radar:
        return None
    units = list(radar.get("units") or [])
    unit = match_caoc_unit_for_flight(units, callsign=callsign, opus=opus, config=config)
    if unit is None:
        return None
    return bullseye_for_caoc_unit(unit, config, opus=opus)


# Templates whose phrasing depends on the assigned recovery / approach plate.
_RECOVERY_TEMPLATES = frozenset(
    {
        "approach_check_in",
        "approach_procedure",
        "approach_iaf",
        "cleared_approach",
        "bj_range_exit",
    }
)


_ownship_miss_until = 0.0
OWNSHIP_MISS_BACKOFF_S = 30.0


def ownship_latlon(
    config: dict[str, Any] | None,
    *,
    callsign: str | None = None,
    opus: OpusFlightContext | None = None,
    state: dict[str, Any] | None = None,
    max_age_s: float = 10.0,
) -> tuple[float, float] | None:
    """
    Own aircraft position from the CAOC feed, cached into state.

    Approach uses this to hand out the recovery/plate nearest the jet. The feed
    is often unavailable (no mission, no backend), so callers must treat None as
    "decide from the flight plan and weather instead". A miss backs off for
    OWNSHIP_MISS_BACKOFF_S so a dead backend never stalls phrase building.
    """
    global _ownship_miss_until
    if not config:
        return None
    def _cached_ll() -> tuple[float, float] | None:
        if not isinstance(state, dict):
            return None
        raw = state.get("ownship_ll")
        try:
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                return (float(raw[0]), float(raw[1]))
        except (TypeError, ValueError):
            return None
        return None

    now = time.time()
    if now < _ownship_miss_until:
        return _cached_ll()

    def _miss() -> None:
        global _ownship_miss_until
        _ownship_miss_until = time.time() + OWNSHIP_MISS_BACKOFF_S

    try:
        radar = fetch_caoc_radar(config, max_age_s=max_age_s)
        if not radar:
            _miss()
            return _cached_ll()
        units = caoc_air_units(list(radar.get("units") or []))
        own = match_caoc_unit_for_flight(
            units, callsign=callsign, opus=opus, config=config
        )
        if not own:
            _miss()
            return _cached_ll()
        ll = caoc_xz_to_ll(float(own["xMeters"]), float(own["zMeters"]))
    except (KeyError, TypeError, ValueError, OSError):
        _miss()
        return _cached_ll()
    if isinstance(state, dict):
        state["ownship_ll"] = [ll[0], ll[1]]
        state["ownship_ll_t"] = time.time()
    return ll


_airspace_schedule_cache: dict[str, Any] = {"t": 0.0, "key": "", "rows": None}


def fetch_opus_airspace_schedule(
    config: dict[str, Any],
    *,
    opus_flight_id: int | None = None,
    include_past: bool = False,
    max_age_s: float = 30.0,
) -> list[dict[str, Any]]:
    """
    Opus `/opus/airspace/schedule` — reserved zones for a flight (or all current).

    Each row: zone_name, vul_start, vul_end, opus_flight_id, …
    """
    backend = (config.get("opus_backend_url") or "").rstrip("/")
    if not backend:
        return []
    cache_key = f"{opus_flight_id or ''}|{int(bool(include_past))}"
    now = time.time()
    if (
        _airspace_schedule_cache.get("key") == cache_key
        and _airspace_schedule_cache.get("rows") is not None
        and (now - float(_airspace_schedule_cache.get("t") or 0)) < max_age_s
    ):
        return list(_airspace_schedule_cache["rows"] or [])

    params: list[str] = []
    if opus_flight_id:
        params.append(f"opus_flight_id={int(opus_flight_id)}")
    if include_past:
        params.append("include_past=true")
    qs = ("?" + "&".join(params)) if params else ""
    ua = config.get("user_agent", "DCS-ATC-Phrase/1.0")
    try:
        data = http_get_json(f"{backend}/opus/airspace/schedule{qs}", ua)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"WARNING: Opus airspace schedule failed ({exc})", file=sys.stderr)
        return []
    rows = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    _airspace_schedule_cache["t"] = now
    _airspace_schedule_cache["key"] = cache_key
    _airspace_schedule_cache["rows"] = rows
    return list(rows)


def airspace_schedule_for_flight(
    config: dict[str, Any] | None,
    opus: OpusFlightContext | None,
) -> list[dict[str, Any]]:
    """
    Reserved airspace rows for this Opus flight.

    Current schedule first; if empty (past event still selected), fall back to
    include_past filtered to the flight's event_date.
    """
    if not config or not opus or not opus.flight_id:
        return []
    fid = int(opus.flight_id)
    rows = fetch_opus_airspace_schedule(config, opus_flight_id=fid)
    if rows:
        return rows
    past = fetch_opus_airspace_schedule(
        config, opus_flight_id=fid, include_past=True
    )
    if not past:
        return []
    event = str(opus.event_date or "").strip()
    if event:
        matched = [
            r for r in past if str(r.get("event_date") or "").strip() == event
        ]
        if matched:
            return matched
    # No event_date on context — keep the newest event group for this flight.
    dates = sorted(
        {
            str(r.get("event_date") or "").strip()
            for r in past
            if str(r.get("event_date") or "").strip()
        },
        reverse=True,
    )
    if not dates:
        return list(past)
    newest = dates[0]
    return [r for r in past if str(r.get("event_date") or "").strip() == newest]


def airspace_areas_for_flight(
    config: dict[str, Any] | None,
    opus: OpusFlightContext | None,
    *,
    limit: int | None = None,
) -> list[str]:
    """
    Reserved zone names for this Opus flight (unique, schedule order).

    When `limit` is set, only that many names are returned — callers that need
    to know if more exist should omit limit and slice themselves.
    """
    rows = airspace_schedule_for_flight(config, opus)
    names: list[str] = []
    for row in rows:
        name = str(row.get("zone_name") or "").strip()
        if name and name not in names:
            names.append(name)
    if limit is not None and int(limit) >= 0:
        return names[: int(limit)]
    return names


# NTTR / Nellis airspace codes → spoken names (NELLISAFBI 11-250 / AFMAN 13-212).
# Keys are normalized: uppercase, no spaces/hyphens/underscores.
_AIRSPACE_NATO_LETTER = {
    "A": "Alpha",
    "B": "Bravo",
    "C": "Charlie",
    "D": "Delta",
    "E": "Echo",
    "F": "Foxtrot",
    "G": "Golf",
}
_AIRSPACE_CARDINAL = {
    "N": "North",
    "S": "South",
    "E": "East",
    "W": "West",
}
_AIRSPACE_ZONE_SPOKEN: dict[str, str] = {
    # Desert MOA / Eastern NTTR
    "CAL": "Caliente",
    "CALA": "Caliente Alpha",
    "CALB": "Caliente Bravo",
    "CALC": "Caliente Charlie",
    "COY": "Coyote",
    "COYA": "Coyote Alpha",
    "COYB": "Coyote Bravo",
    "COYC": "Coyote Charlie",
    "COYD": "Coyote Delta",
    "ELGN": "Elgin",
    "ELGIN": "Elgin",
    "REV": "Reveille",
    "REVN": "Reveille North",
    "REVS": "Reveille South",
    "REVNORTH": "Reveille North",
    "REVSOUTH": "Reveille South",
    "SALLY": "Sally Corridor",
    "SALLYCORRIDOR": "Sally Corridor",
    # Alamo / R-4806
    "ALAMO": "Alamo",
    "ALAMOA": "Alamo Alpha",
    "ALAMOB": "Alamo Bravo",
    "ALAMOC": "Alamo Charlie",
    "ALAMOCORRIDOR": "Alamo Corridor",
    "R61A": "Range 61 Alpha",
    "R61B": "Range 61 Bravo",
    "R62A": "Range 62 Alpha",
    "R62B": "Range 62 Bravo",
    "R63A": "Range 63 Alpha",
    "R63B": "Range 63 Bravo",
    "R63C": "Range 63 Charlie",
    "R64A": "Range 64 Alpha",
    "R64B": "Range 64 Bravo",
    "R64C": "Range 64 Charlie",
    "R64D": "Range 64 Delta",
    "R64E": "Range 64 Echo",
    "R64F": "Range 64 Foxtrot",
    "R64G": "Range 64 Golf",
    "R65A": "Range 65 Alpha",
    "R65B": "Range 65 Bravo",
    "R65C": "Range 65 Charlie",
    "R65D": "Range 65 Delta",
    # R-4807 / Central NTTR
    "R71N": "Range 71 North",
    "R71S": "Range 71 South",
    "R74A": "Range 74 Alpha",
    "R74B": "Range 74 Bravo",
    "R74C": "Range 74 Charlie",
    "R75E": "Range 75 East",
    "R75W": "Range 75 West",
    "R76": "Range 76",
    "ECE": "E C East",
    "R77A": "Range 77 Alpha",
    "R77B": "Range 77 Bravo",
    "TPECR": "Tolicha Peak Electronic Combat Range",
    # R-4807B / Pahute
    "PAHUTE": "Pahute",
    "PAH": "Pahute",
    "PAHA": "Pahute Alpha",
    "PAHB": "Pahute Bravo",
    # R-4808
    "R4808": "R 4808",
    "R4808A": "R 4808 Alpha",
    "R4808B": "R 4808 Bravo",
    "R4808C": "R 4808 Charlie",
    "R4808D": "R 4808 Delta",
    "R4808E": "R 4808 Echo",
    # R-4809 / Western NTTR
    "ECW": "E C West",
    "R4809": "R 4809",
    "R4809A": "R 4809 Alpha",
    "R4809B": "R 4809 Bravo",
    # Special / named
    "XRAY": "X-Ray Extension",
    "XRAYEXTENSION": "X-Ray Extension",
    "GOMER": "Gomer Triangle",
    "GOMERTRIANGLE": "Gomer Triangle",
    "DORITO": "Dorito",
}


def _normalize_airspace_zone_key(name: str) -> str:
    """Uppercase identifier with spaces/hyphens/underscores removed for alias lookup."""
    raw = str(name or "").strip().upper()
    return re.sub(r"[\s\-_]+", "", raw)


def _speak_airspace_zone_pattern(key: str) -> str | None:
    """
    Pattern fallbacks when Opus uses a code not listed explicitly.

    Tactical R-61…R-77 → 'Range …'; FAA R-4808/R-4809 → 'R 4808/4809 …'.
    """
    # FAA restricted areas R-4808 / R-4809 (keep "R", not "Range")
    m = re.fullmatch(r"R480([89])([A-E])?", key)
    if m:
        base = f"R 480{m.group(1)}"
        letter = _AIRSPACE_NATO_LETTER.get(m.group(2) or "")
        return f"{base} {letter}".strip() if letter else base

    # Tactical ranges R61–R77 with letter or cardinal
    m = re.fullmatch(r"R(6[1-5]|7[1-7])([A-G]|[NSEW])?", key)
    if m:
        num = m.group(1)
        suf = m.group(2) or ""
        if not suf:
            return f"Range {num}"
        if suf in _AIRSPACE_NATO_LETTER:
            return f"Range {num} {_AIRSPACE_NATO_LETTER[suf]}"
        if suf in _AIRSPACE_CARDINAL:
            return f"Range {num} {_AIRSPACE_CARDINAL[suf]}"

    # Bare digits with optional cardinal (legacy Opus "75E")
    m = re.fullmatch(r"(\d{2})([NSEW])", key)
    if m and m.group(1) in {f"{n}" for n in range(61, 78)}:
        return f"Range {m.group(1)} {_AIRSPACE_CARDINAL[m.group(2)]}"

    return None


def speak_airspace_zone_name(name: str) -> str:
    """
    Convert Opus airspace/range codes to TTS-friendly NTTR names.

    CALA → Caliente Alpha; R63A → Range 63 Alpha; R4808D → R 4808 Delta.
    Named areas (Owens, Saline, …) keep readable title case.
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    key = _normalize_airspace_zone_key(raw)
    if not key:
        return ""

    spoken = _AIRSPACE_ZONE_SPOKEN.get(key)
    if spoken:
        return spoken

    patterned = _speak_airspace_zone_pattern(key)
    if patterned:
        return patterned

    # Multi-word Opus labels already close to spoken form (e.g. "REV SOUTH")
    spaced = re.sub(r"[\-_]+", " ", raw).strip()
    spaced_key = _normalize_airspace_zone_key(spaced)
    if spaced_key != key:
        spoken = _AIRSPACE_ZONE_SPOKEN.get(spaced_key)
        if spoken:
            return spoken
        patterned = _speak_airspace_zone_pattern(spaced_key)
        if patterned:
            return patterned

    # Readable fallback for named MOAs / corridors already in plain English
    if re.search(r"[a-z]", raw) or " " in spaced:
        return spaced.title() if spaced.isupper() or " " in spaced else spaced

    if re.fullmatch(r"[A-Z0-9]+(?:\s+[A-Z0-9]+)*", spaced.upper()):
        parts: list[str] = []
        for tok in spaced.upper().split():
            if tok.isdigit():
                parts.append(tok)
            elif re.fullmatch(r"\d+[A-Z]", tok):
                letter = _AIRSPACE_NATO_LETTER.get(tok[-1], tok[-1])
                parts.append(f"{tok[:-1]} {letter}")
            elif tok in _AIRSPACE_CARDINAL:
                parts.append(_AIRSPACE_CARDINAL[tok])
            elif len(tok) == 1 and tok in _AIRSPACE_NATO_LETTER:
                parts.append(_AIRSPACE_NATO_LETTER[tok])
            else:
                parts.append(tok.title())
        return " ".join(parts)

    return raw


def _join_spoken_list(items: list[str]) -> str:
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"


def _join_summary_phrases(items: list[str]) -> str:
    """Join major airspace summaries with commas (phrases may already contain 'and')."""
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]}, and {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])}, and {cleaned[-1]}"


def classify_airspace_zone(name: str) -> tuple[str, str, str]:
    """
    Classify an Opus zone for summarization.

    Returns (group_id, parent_spoken, full_spoken).
    group_id: desert_moa | r4806 | r4807 | pahute | r4808 | r4809 | special | other
    """
    raw = str(name or "").strip()
    spoken = speak_airspace_zone_name(raw)
    key = _normalize_airspace_zone_key(raw)
    if not key:
        return "other", spoken, spoken

    if key.startswith("CAL"):
        return "desert_moa", "Caliente", spoken
    if key.startswith("COY"):
        return "desert_moa", "Coyote", spoken
    if key.startswith("REV"):
        return "desert_moa", "Reveille", spoken
    if key in ("ELGN", "ELGIN"):
        return "desert_moa", "Elgin", spoken
    if key.startswith("SALLY"):
        return "desert_moa", "Sally Corridor", spoken

    if key.startswith("ALAMO"):
        return "r4806", "Alamo", spoken
    m = re.fullmatch(r"R(6[1-5])([A-G])?", key) or re.fullmatch(r"(6[1-5])([A-G])?", key)
    if m:
        return "r4806", f"Range {m.group(1)}", spoken

    if key.startswith("PAH"):
        return "pahute", "Pahute", spoken

    if key.startswith("R4808") or re.fullmatch(r"R-?4808[A-E]?", key):
        return "r4808", "R-4808", spoken
    if key.startswith("R4809") or key == "ECW" or re.fullmatch(r"R-?4809[A-B]?", key):
        return "r4809", "R-4809", spoken

    if key in ("ECE", "TPECR") or re.match(r"^R7[1-7]", key) or re.match(r"^7[1-7][A-GNSEW]?$", key):
        if key == "TPECR":
            return "r4807", "Tolicha Peak", spoken
        if key == "ECE":
            return "r4807", "E C East", spoken
        m = re.match(r"^R?(7[1-7])", key)
        parent = f"Range {m.group(1)}" if m else "R-4807"
        return "r4807", parent, spoken

    if key in ("XRAY", "XRAYEXTENSION", "GOMER", "GOMERTRIANGLE", "DORITO"):
        return "special", spoken, spoken

    # Parent word from multi-word spoken name ("Caliente Alpha" → Caliente)
    parts = spoken.split()
    if len(parts) >= 2 and parts[-1] in set(_AIRSPACE_NATO_LETTER.values()) | set(
        _AIRSPACE_CARDINAL.values()
    ):
        return "other", " ".join(parts[:-1]), spoken
    return "other", spoken, spoken


def _summarize_airspace_group(
    group_id: str, rows: list[tuple[str, str, str]]
) -> str | None:
    """
    One spoken summary for a classified group, or None to list members instead.

    rows: (parent, spoken, raw_key) in reservation order.
    """
    if not rows:
        return None
    parents: list[str] = []
    for parent, _spoken, _raw in rows:
        if parent and parent not in parents:
            parents.append(parent)
    n = len(rows)

    if group_id == "desert_moa":
        northern = [p for p in parents if p in ("Caliente", "Coyote", "Reveille")]
        extras = [p for p in parents if p not in ("Caliente", "Coyote", "Reveille")]
        if n >= 3 or len(northern) >= 2:
            if northern and not extras:
                return f"northern ranges {_join_spoken_list(northern)}"
            if northern and extras:
                return (
                    f"northern ranges {_join_spoken_list(northern)}, "
                    f"{_join_spoken_list(extras)}"
                )
            return "Desert MOA"
        if n >= 2 and len(parents) == 1:
            return parents[0]
        if n >= 2 and len(parents) >= 2:
            return _join_spoken_list(parents)
        return None

    if group_id == "r4806":
        has_alamo = any(p == "Alamo" or p.startswith("Alamo") for p, _, _ in rows)
        has_ranges = any(p.startswith("Range ") for p, _, _ in rows)
        if n >= 2 or (has_alamo and has_ranges):
            if has_alamo and has_ranges:
                return "Alamo and R-4806"
            if has_alamo and not has_ranges:
                return "Alamo"
            return "R-4806"
        return None

    if group_id == "r4807":
        if n >= 2:
            return "R-4807"
        return None

    if group_id == "pahute":
        if n >= 2:
            return "Pahute"
        return None

    if group_id == "r4808":
        if n >= 2:
            return "R-4808"
        return None

    if group_id == "r4809":
        if n >= 2:
            return "R-4809"
        return None

    return None


def summarize_scheduled_airspace(areas: list[str] | None) -> str | None:
    """
    Blackjack 'scheduled airspace, …' clause for one or many reserved zones.

    Few zones → full spoken names. Many → NTTR group summaries
    (northern ranges Caliente and Coyote, R-4806, R-4807, …).
    """
    raw_list = [str(a).strip() for a in (areas or []) if str(a).strip()]
    if not raw_list:
        return None

    # De-dupe while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in raw_list:
        key = _normalize_airspace_zone_key(raw)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(raw)

    classified: list[tuple[str, str, str, str]] = []
    for raw in ordered:
        group, parent, spoken = classify_airspace_zone(raw)
        classified.append((group, parent, spoken, _normalize_airspace_zone_key(raw)))

    if len(classified) == 1:
        return f"scheduled airspace, area {classified[0][2]}"

    if len(classified) == 2:
        g0, p0, s0, _ = classified[0]
        g1, p1, s1, _ = classified[1]
        # Same parent (Caliente Alpha+Bravo, Pahute Alpha+Bravo, …)
        if g0 == g1 and p0 and p0 == p1:
            collapsed = _summarize_airspace_group(
                g0, [(p0, s0, ""), (p1, s1, "")]
            )
            return f"scheduled airspace, {collapsed or p0}"
        return f"scheduled airspace, areas {s0} and {s1}"

    group_order = (
        "desert_moa",
        "r4806",
        "r4807",
        "pahute",
        "r4808",
        "r4809",
        "special",
        "other",
    )
    by_group: dict[str, list[tuple[str, str, str]]] = {g: [] for g in group_order}
    for group, parent, spoken, key in classified:
        by_group.setdefault(group, []).append((parent, spoken, key))

    phrases: list[str] = []
    omitted = False
    for gid in group_order:
        rows = by_group.get(gid) or []
        if not rows:
            continue
        summary = _summarize_airspace_group(gid, rows)
        if summary:
            phrases.append(summary)
            continue
        parents: list[str] = []
        for parent, spoken, _key in rows:
            label = parent if parent else spoken
            if label not in parents:
                parents.append(label)
        if gid in ("special", "other"):
            if len(parents) <= 3:
                phrases.append(_join_spoken_list(parents))
            else:
                phrases.append(_join_spoken_list(parents[:2]))
                omitted = True
            continue
        if len(parents) == 1 and len(rows) >= 2:
            phrases.append(parents[0])
        elif len(parents) <= 2:
            phrases.extend(parents)
        else:
            phrases.append(_join_spoken_list(parents[:2]))
            omitted = True

    if len(phrases) > 3:
        phrases = phrases[:3]
        omitted = True

    if not phrases:
        names = [c[2] for c in classified[:2]]
        clause = f"scheduled airspace, areas {_join_spoken_list(names)}"
        if len(classified) > 2:
            clause += ", others as fragged"
        return clause

    clause = f"scheduled airspace, {_join_summary_phrases(phrases)}"
    if omitted:
        clause += ", others as fragged"
    return clause


def speak_zulu_clock(raw: str | None) -> str | None:
    """
    '04:00' / '04:00:00' / '0400' → 'zero four zero zero zulu' (HH:MM only).
    """
    if raw is None or str(raw).strip() == "":
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 3:
        digits = "0" + digits
    if len(digits) < 4:
        return None
    # Always HH:MM — drop seconds if present.
    digits = digits[:4]
    return f"{speak_digits(digits)} zulu"


def speak_zulu_now() -> str:
    """Current UTC clock as spoken HHMM zulu."""
    now = datetime.now(timezone.utc)
    return speak_zulu_clock(f"{now.hour:02d}:{now.minute:02d}") or "time unavailable"


def build_blackjack_check_in(
    callsign: str,
    *,
    ship_note: str = "",
    alpha_bullseye: str | None = None,
    weather: Weather | None = None,
    areas: list[str] | None = None,
    vul_end: str | None = None,
    vul_start: str | None = None,
) -> str:
    """
    Blackjack check-in reply after the pilot reports callsign + mission.

    Example:
      Bruiser 5, Blackjack. Radar contact Elvis … . Scheduled airspace, areas
      seven fife and seven six. Cleared entry until … zulu, time now … zulu.
      Current altimeter two niner niner two. Cleared tactical. Frequency
      change approved, check out this frequency when range work complete.

    With no reserved range / VUL end, skip the time-now line.
    """
    del vul_start, ship_note  # ship count is not spoken on Blackjack check-in
    cs = speak_callsign(callsign)
    bits: list[str] = [f"{cs}, Blackjack"]

    if alpha_bullseye:
        bits.append(f"radar contact {alpha_bullseye}")
    else:
        bits.append("radar contact")

    zone_raw = [str(a).strip() for a in (areas or []) if str(a).strip()]
    airspace_clause = summarize_scheduled_airspace(zone_raw)
    if airspace_clause:
        bits.append(airspace_clause)

    until = speak_zulu_clock(vul_end)
    # Time hack only with a real range entry window — not when nothing is reserved.
    if until:
        bits.append(f"cleared entry until {until}, time now {speak_zulu_now()}")

    if weather and weather.altimeter_inhg:
        bits.append(f"current altimeter {speak_altimeter(weather.altimeter_inhg)}")

    bits.append("cleared tactical")
    bits.append(
        "frequency change approved, check out this frequency when range work complete"
    )
    clauses: list[str] = []
    for bit in bits:
        b = str(bit or "").strip()
        if not b:
            continue
        clauses.append(b[:1].upper() + b[1:] if len(b) > 1 else b.upper())
    return ". ".join(clauses) + "."


# Situation helper: generate custom / uncommon ATC wording from structured fields
SITUATION_CHOICES: list[tuple[str, str]] = [
    ("approach_recovery", "Approach — Recovery check-in"),
    ("approach_clearance", "Approach — Contact tower"),
    ("bj_alpha_check", "Blackjack — Alpha check (live bullseye)"),
    ("bj_range_exit", "Blackjack — Range exit + handoff"),
    ("agency_radio_check", "Any — Radio check"),
    ("agency_contact", "Any — Contact handoff"),
    ("freeform", "Any — Freeform line"),
]


def situation_fields(situation: str) -> list[dict[str, Any]]:
    """Field specs for the Plan → Phrase helper modal."""
    agencies = [(c, HANDOFF_AGENCY_NAMES.get(c, c.title())) for c in (
        "delivery", "ground", "tower", "departure", "approach",
        "blackjack", "bandsaw", "ops", "other",
    )]
    recovery_opts = list(RECOVERY_CHOICES) + [("__custom__", "Custom…")]
    pattern_opts = list(APPROACH_PATTERN_CHOICES) + [("__custom__", "Custom…")]
    if situation == "approach_recovery":
        return [
            {"key": "recovery", "label": "Recovery", "kind": "choice", "choices": recovery_opts, "default": DEFAULT_RECOVERY},
            {"key": "recovery_custom", "label": "Custom recovery", "kind": "text", "default": "", "when": "recovery=__custom__"},
            {"key": "descend_ft", "label": "Descend (ft)", "kind": "text", "default": "10000"},
            {"key": "speed_kt", "label": "Speed (kt)", "kind": "text", "default": "300"},
        ]
    if situation == "approach_clearance":
        return [
            {"key": "pattern", "label": "Clearance", "kind": "choice", "choices": pattern_opts, "default": DEFAULT_RECOVERY},
            {"key": "pattern_custom", "label": "Custom clearance", "kind": "text", "default": "", "when": "pattern=__custom__"},
        ]
    if situation == "bj_range_exit":
        return [
            {"key": "handoff_channel", "label": "Contact", "kind": "choice", "choices": agencies, "default": "approach"},
        ]
    if situation == "bj_alpha_check":
        return [
            {
                "key": "track_query",
                "label": "Find track",
                "kind": "text",
                "default": "",
                "hint": "Filter e.g. damn (blank = all)",
            },
            {
                "key": "track_id",
                "label": "Unit",
                "kind": "caoc_track",
                "default": "",
                "hint": "Live CAOC — no Opus FP needed",
            },
            {
                "key": "callsign",
                "label": "Callsign",
                "kind": "text",
                "default": "",
                "hint": "Blank = from selected unit",
            },
        ]
    if situation == "agency_radio_check":
        return [
            {"key": "agency", "label": "Agency", "kind": "choice", "choices": agencies, "default": "ops"},
        ]
    if situation == "agency_contact":
        return [
            {"key": "from_channel", "label": "From", "kind": "choice", "choices": agencies, "default": "departure"},
            {"key": "handoff_channel", "label": "Contact", "kind": "choice", "choices": agencies, "default": "approach"},
        ]
    if situation == "freeform":
        return [
            {"key": "agency", "label": "Agency", "kind": "choice", "choices": agencies, "default": "other"},
            {"key": "agency_custom", "label": "Spoken agency", "kind": "text", "default": "", "hint": "Blank = airport + channel name"},
            {"key": "body", "label": "Say", "kind": "text", "default": "loud and clear", "hint": "Words after callsign / agency"},
        ]
    return []


def _situation_choice_value(params: dict[str, Any], key: str, custom_key: str | None = None) -> str:
    raw = str(params.get(key) or "").strip()
    if raw == "__custom__" and custom_key:
        return str(params.get(custom_key) or "").strip()
    return raw


def resolve_situation_alpha_fix(
    config: dict[str, Any] | None,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve live CAOC bullseye for Phrase helper alpha-check params."""
    if not config:
        return None
    track_id = str(params.get("track_id") or "").strip()
    query = str(params.get("track_query") or params.get("callsign") or "").strip() or None
    rows = list_caoc_air_bullseyes(config, query=query, max_age_s=0.0)
    if track_id:
        for row in rows:
            if str(row.get("unit_id") or "") == track_id:
                return row
        # ID may have dropped off filter — search unfiltered
        for row in list_caoc_air_bullseyes(config, query=None, max_age_s=0.0):
            if str(row.get("unit_id") or "") == track_id:
                return row
        return None
    if len(rows) == 1:
        return rows[0]
    # Fall back to callsign / mission match
    cs = str(params.get("callsign") or "").strip()
    if cs:
        return resolve_alpha_bullseye(config, callsign=cs)
    return None


def generate_situation_phrase(
    situation: str,
    airport: dict[str, Any],
    callsign: str,
    weather: Weather,
    runway: str,
    params: dict[str, Any] | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> str:
    """
    Build spoken ATC text for a custom / uncommon situation.
    Used by Plan → Phrase helper; also available to scripts.
    """
    params = dict(params or {})
    name = airport.get("name") or "Airport"
    cs = speak_callsign(callsign)
    rwy = speak_runway(runway) if runway else ""
    tower = airport.get("tower") or {"freq_mhz": 327.0}
    twr_local = speak_local_preset(airport, "tower")

    if situation == "approach_recovery":
        recovery = _situation_choice_value(params, "recovery", "recovery_custom") or DEFAULT_RECOVERY
        try:
            descend_ft = int(str(params.get("descend_ft") or "10000").replace(",", ""))
        except ValueError:
            descend_ft = 10000
        try:
            speed_kt = int(str(params.get("speed_kt") or "300").replace(",", ""))
        except ValueError:
            speed_kt = 300
        return build_approach_recovery(
            airport,
            callsign,
            weather,
            runway,
            recovery=recovery,
            descend_ft=descend_ft or None,
            speed_kt=speed_kt or None,
        )

    if situation == "approach_clearance":
        return build_approach_tower_handoff(airport, callsign)

    if situation == "bj_range_exit":
        handoff = str(params.get("handoff_channel") or "approach").strip().lower() or "approach"
        return build_blackjack_range_exit(airport, callsign, handoff_channel=handoff)

    if situation == "bj_alpha_check":
        fix = resolve_situation_alpha_fix(config, params)
        cs_raw = str(params.get("callsign") or "").strip()
        if not cs_raw and fix:
            cs_raw = str(fix.get("radio_callsign") or fix.get("unit_name") or "").strip()
        if not cs_raw:
            cs_raw = callsign
        alpha = str(fix.get("spoken") or "") if fix else None
        if not alpha:
            raise ValueError(
                "No live CAOC track / bullseye — pick a Unit (e.g. Damn) or check Opus backend URL"
            )
        return build_standalone_alpha_check(cs_raw, alpha)

    if situation == "agency_radio_check":
        ch = str(params.get("agency") or "ops").strip().lower() or "ops"
        agency = speak_agency_name(ch)
        spoken = (
            f"{name} {agency}"
            if ch not in ("blackjack", "bandsaw", "ops", "other")
            else agency
        )
        if ch == "other":
            spoken = f"{name} Center"
        return f"{cs}, {spoken}, loud and clear."

    if situation == "agency_contact":
        from_ch = str(params.get("from_channel") or "departure").strip().lower() or "departure"
        to_ch = str(params.get("handoff_channel") or "approach").strip().lower() or "approach"
        return build_departure_handoff(
            airport, callsign, handoff_channel=to_ch, from_channel=from_ch
        )

    if situation == "freeform":
        ch = str(params.get("agency") or "other").strip().lower() or "other"
        custom_agency = str(params.get("agency_custom") or "").strip()
        body = str(params.get("body") or "loud and clear").strip().rstrip(".")
        if custom_agency:
            spoken = custom_agency
        elif ch == "blackjack":
            spoken = "Blackjack"
        elif ch == "bandsaw":
            spoken = "Bandsaw"
        elif ch == "ops":
            spoken = "Ops"
        elif ch in ("other", "center"):
            spoken = f"{name} Center"
        else:
            spoken = f"{name} {speak_agency_name(ch)}"
        return f"{cs}, {spoken}, {body}."

    raise ValueError(f"Unknown situation '{situation}'")


def situation_applies_to_step(situation: str) -> dict[str, Any]:
    """Suggested step fields when applying a helper result (template / recovery / etc.)."""
    if situation == "approach_recovery":
        return {"template": "approach_check_in", "channel": "approach"}
    if situation == "approach_clearance":
        return {"template": "cleared_approach", "channel": "approach"}
    if situation == "bj_alpha_check":
        return {"template": "bj_alpha_check", "channel": "blackjack"}
    if situation == "bj_range_exit":
        return {"template": "bj_range_exit", "channel": "blackjack"}
    if situation == "agency_radio_check":
        return {"template": "radio_check"}
    if situation == "agency_contact":
        return {"template": "departure_handoff"}
    return {"template": "radio_check"}


def sync_opus_local_presets(
    config: dict[str, Any],
    airport: dict[str, Any],
    *,
    icao: str | None = None,
) -> dict[str, int]:
    """
    Pull Opus theater UHF presets into airport[*].local_preset (and freqs).
    Returns {channel: preset_number} written.
    """
    result = fetch_opus_theater_freqs(
        config,
        icao=icao or str(airport.get("icao") or ""),
        band="uhf",
    )
    apply_opus_freqs_to_airport(
        airport,
        result.get("freqs") or {},
        presets=result.get("presets") or {},
    )
    return dict(result.get("presets") or {})


def clearance_spoken_agency(airport: dict[str, Any], channel: str | None = None) -> str:
    """
    Agency name spoken in clearance / readback.

    Prefer the step radio channel: delivery → Delivery, ground → Ground.
    Only when channel is unset does clearance_consolidated_with_ground apply
    (legacy: clearance done on Ground Local 2).
    """
    ch = (channel or "").strip().casefold()
    if ch == "delivery":
        return "Delivery"
    if ch == "ground":
        return "Ground"
    if airport.get("clearance_consolidated_with_ground"):
        return "Ground"
    return "Delivery"


def _looks_like_icao(token: str) -> bool:
    """True for airport ICAOs (KLSV), not 4-letter fixes like FLEX."""
    t = (token or "").strip().upper()
    if len(t) != 4 or not t.isalpha():
        return False
    # US / Pacific / Canada ICAO leading letters — excludes FLEX/WEST/etc.
    return t[0] in "KPC"


def speak_fix(fix: str) -> str:
    m = _FIX_TRAIL_DIGITS.match(fix.strip())
    if m:
        return f"{m.group(1).upper()} {speak_digits(m.group(2))}"
    return fix.strip().upper()


def speak_icao_or_name(icao: str | None, airport: dict[str, Any]) -> str:
    if not icao:
        return "destination"
    code = icao.strip().upper()
    if code == str(airport.get("icao") or "").strip().upper():
        return str(airport.get("name") or code)
    # Spell ICAO for TTS (K L S V -> each letter)
    return " ".join(ch for ch in code)


# Non-Blackjack ATC: feet at/above this are spoken as flight levels.
ATC_FL_AT_OR_ABOVE_FT = 18000


def speak_altitude_value(
    alt: str | None,
    *,
    prefer_fl_below: int = 1000,
    as_flight_level_above_ft: int | None = ATC_FL_AT_OR_ABOVE_FT,
    natural_thousands: bool = False,
    clarify_chance: float = 0.4,
) -> str | None:
    """
    Speak an altitude string for ATC (not Blackjack picture/angels).

    - Values < prefer_fl_below (default 1000) are FL hundreds (220 → FL220).
    - Feet at/above 18,000 → flight levels (22000 → FL220) — no digit/natural clarify.
    - Lower whole thousands: usually 'one two thousand' (or natural 'twelve thousand');
      sometimes clarifies as 'one two, twelve thousand' for all agencies.
    """
    if not alt:
        return None
    raw = str(alt).strip().upper().replace("FL", "").replace("FT", "")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    n = int(digits)
    if n < prefer_fl_below:
        return f"flight level {speak_digits(f'{n:03d}')}"
    # High altitudes → flight levels (no "one two, twelve" style clarify).
    if as_flight_level_above_ft is not None and n >= as_flight_level_above_ft:
        fl = int(round(n / 100.0))
        return f"flight level {speak_digits(f'{fl:03d}')}"
    # Feet below transition
    if n >= 1000 and n % 1000 == 0:
        thousands = n // 1000
        digit_lead = speak_digits(str(thousands))
        natural_lead = speak_natural_number(thousands)
        if (
            float(clarify_chance) > 0
            and digit_lead
            and natural_lead
            and digit_lead != natural_lead
            and random.random() < float(clarify_chance)
        ):
            return f"{digit_lead}, {natural_lead} thousand"
        lead = natural_lead if natural_thousands else digit_lead
        return f"{lead} thousand"
    if n >= 1000:
        thousands = n // 1000
        rest = n % 1000
        lead = (
            speak_natural_number(thousands)
            if natural_thousands
            else speak_digits(str(thousands))
        )
        if rest % 100 == 0:
            hundreds = rest // 100
            hund = (
                speak_natural_number(hundreds)
                if natural_thousands
                else speak_digits(str(hundreds))
            )
            return f"{lead} thousand {hund} hundred"
        return f"{speak_digits(str(n))} feet"
    return f"{speak_digits(str(n))} feet"


def speak_descend_pilot_discretion(alt_ft: int | str | None) -> str | None:
    """
    Approach interim altitude: 'descend pilot discretion to twelve thousand'
    (sometimes '… to one two, twelve thousand' via speak_altitude_value).
    """
    spoken = speak_altitude_value(
        str(alt_ft) if alt_ft is not None else None,
        prefer_fl_below=1000,
        natural_thousands=True,
    )
    if not spoken:
        return None
    return f"descend pilot discretion to {spoken}"


def speak_filed_altitude(alt: str | None) -> str | None:
    """Opus fp_altitude is usually FL hundreds (220)."""
    return speak_altitude_value(alt, prefer_fl_below=1000)


def speak_squawk(mode3: str | None) -> str | None:
    if not mode3:
        return None
    digits = re.sub(r"\D", "", mode3)
    if len(digits) < 4:
        return None
    return speak_digits(digits[:4])


def squawk_clearance_phrase(opus: OpusFlightContext | None) -> str | None:
    """'squawk six fife four one' or '... in sequence' for multi-ship flights."""
    code = speak_squawk(opus.mode3 if opus else None)
    if not code:
        return None
    if opus and opus.squawk_in_sequence:
        return f"squawk {code} in sequence"
    return f"squawk {code}"


# Templates after which the pilot is expected to read items back — address is
# optional for the acknowledge intent because the exchange is already open.
AWAITING_READBACK_TEMPLATES = frozenset(
    {
        "clearance",
        "taxi",
        "clear_takeoff",
        "lineup",
        "line_up_and_wait",
        "rolling_accept",
        "clear_land",
        "go_around",
        "radar_contact",
        "climb_cruise",
        "bj_check_in",
    }
)

# ATC confirm steps that close a readback window.
READBACK_CONFIRM_TEMPLATES = frozenset({"clearance_readback"})


def auto_tx_hold_reason(state: dict[str, Any] | None) -> str:
    """
    Why Watch / auto-clearance must not transmit yet.

    ATC waits for the outstanding readback before the next command.
    """
    if not isinstance(state, dict):
        return ""
    if state.get("awaiting_readback") and state.get("readback_items"):
        return "waiting for readback"
    return ""


def mode3_digits(mode3: str | None) -> str | None:
    digits = re.sub(r"\D", "", str(mode3 or ""))
    return digits[:4] if len(digits) >= 4 else None


def go_around_readback_open(state: dict[str, Any] | None) -> bool:
    """True while Tower is waiting for the go-around / missed readback."""
    if not isinstance(state, dict) or not state.get("awaiting_readback"):
        return False
    return str(state.get("last_tx_template") or "").strip().lower() == "go_around"


def build_readback_checklist(
    template: str,
    airport: dict[str, Any],
    opus: OpusFlightContext | None,
    weather: Weather,
    runway: str,
    *,
    climb_ft: int | None = None,
    state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Items the pilot should read back after this ATC call.

    Each item: key, label, value, spoken, highlight, hinge.
    `hinge` items close the window — any one is enough (agency optional).
    Non-hinge rows are colour only (route, climb, …).
    """
    tmpl = (template or "").strip().lower()
    items: list[dict[str, Any]] = []

    def add(
        key: str,
        label: str,
        value: str,
        spoken: str = "",
        *,
        highlight: bool = False,
        hinge: bool = False,
    ) -> None:
        value = str(value or "").strip()
        if not value:
            return
        items.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "spoken": (spoken or value).strip(),
                "highlight": bool(highlight or hinge),
                "hinge": bool(hinge),
            }
        )

    if tmpl == "clearance":
        if opus and opus.has_filed_plan and opus.arr_icao:
            dest = speak_icao_or_name(opus.arr_icao, airport)
            add("destination", "Cleared to", str(opus.arr_icao).upper(), dest)
        dep_via = speak_departure_clearance(airport, opus.fp_route_string if opus else None)
        if dep_via:
            add("departure", "Via", dep_via, dep_via)
        else:
            add("departure", "Route", "as filed", "as filed")
        if climb_ft:
            spoken_climb = speak_altitude_value(str(climb_ft), prefer_fl_below=1000) or str(climb_ft)
            add("climb", "Climb / maintain", f"{climb_ft:,} ft", spoken_climb)
        filed = speak_filed_altitude(opus.fp_altitude if opus else None)
        if filed:
            add("expect", "Expect", filed, filed)
        # Departure frequency / Local preset — ATC says this in the clearance.
        dep_block = airport.get("departure") or {}
        try:
            dep_mhz = float(dep_block.get("freq_mhz") or 0.0)
        except (TypeError, ValueError):
            dep_mhz = 0.0
        dep_local = speak_local_preset(airport, "departure")
        dep_spoken = speak_departure_freq_or_local(airport)
        if dep_local:
            add("dep_freq", "Departure", dep_local, dep_spoken)
        elif dep_mhz > 0:
            disp = f"{dep_mhz:.3f}".rstrip("0").rstrip(".")
            add("dep_freq", "Departure", f"{disp} MHz", dep_spoken)
        code = mode3_digits(opus.mode3 if opus else None)
        if code:
            spoken = speak_squawk(code) or code
            # Tip shows both forms — matching accepts squawk/squawking + code
            # or the bare digits (spoken or numeric) during this window.
            add(
                "squawk",
                "Squawk",
                code,
                f"squawk {spoken}  ·  or just {spoken}",
                hinge=True,
            )
        return items

    if tmpl == "taxi":
        # Hinge is runway OR taxi-to-EOR — either closes the window.
        rwy = normalize_runway(runway) or str(runway or "").strip()
        if rwy:
            add(
                "runway",
                "Runway",
                rwy,
                f"runway {speak_runway(rwy)}",
                hinge=True,
            )
        taxi = resolve_taxi_route(airport, rwy, opus=opus) if rwy else {}
        eor = str((taxi or {}).get("eor") or "").strip()
        if eor:
            add(
                "eor",
                "Taxi to",
                eor,
                speak_place_label(eor),
                hinge=True,
            )
        return items

    if tmpl == "rolling_accept":
        # Question, not a clearance: answer accept or decline — not runway / LUAW.
        add(
            "accept",
            "Accept",
            "rolling",
            "we'll take the rolling",
            hinge=True,
        )
        add(
            "deny",
            "Decline",
            "unable",
            "unable rolling",
            hinge=True,
        )
        return items

    if tmpl in ("clear_takeoff", "lineup", "line_up_and_wait", "clear_land"):
        # Runway OR the clearance phrase — either closes it.
        rwy = normalize_runway(runway) or str(runway or "").strip()
        if rwy:
            add(
                "runway",
                "Runway",
                rwy,
                f"runway {speak_runway(rwy)}",
                hinge=True,
            )
        if tmpl == "clear_takeoff":
            add(
                "clearance",
                "Clearance",
                "cleared for takeoff",
                "cleared for takeoff",
                hinge=True,
            )
        elif tmpl == "clear_land":
            add(
                "clearance",
                "Clearance",
                "cleared to land",
                "cleared to land",
                hinge=True,
            )
        else:
            add(
                "clearance",
                "Clearance",
                "line up and wait",
                "line up and wait",
                hinge=True,
            )
        return items

    if tmpl == "go_around":
        ga = dict((state or {}).get("go_around_plan") or {}) if isinstance(state, dict) else {}
        kind = str(ga.get("kind") or "closed_traffic")
        rwy = normalize_runway(ga.get("runway") or runway) or str(runway or "").strip()
        if kind == "reentry":
            say = str(ga.get("reentry_say") or ga.get("reentry_id") or "reentry").strip()
            add("instruction", "Reentry", say, say, hinge=True)
        elif kind == "instrument_missed":
            add(
                "instruction",
                "Missed",
                "as published",
                "missed approach as published",
                hinge=True,
            )
        else:
            side = str(ga.get("side") or closed_traffic_side(rwy, airport) or "right")
            phrase = f"{side} closed traffic"
            add("instruction", "Traffic", phrase, phrase, hinge=True)
        if rwy:
            add(
                "runway",
                "Runway",
                rwy,
                f"runway {speak_runway(rwy)}",
                hinge=True,
            )
        return items

    if tmpl in ("radar_contact", "climb_cruise"):
        # Climb is the hinge — altitude alone (flexible forms) closes it.
        if climb_ft:
            spoken_climb = speak_altitude_value(str(climb_ft), prefer_fl_below=1000) or str(
                climb_ft
            )
            add(
                "climb",
                "Climb / maintain",
                f"{climb_ft:,} ft",
                f"climb and maintain {spoken_climb}",
                hinge=True,
            )
        return items

    if tmpl == "bj_check_in":
        # Bare callsign closes it — "Bruiser 5" is enough.
        cs = ""
        if opus and opus.radio_callsign:
            cs = str(opus.radio_callsign).strip()
        if cs:
            add(
                "callsign",
                "Callsign",
                cs,
                speak_callsign(cs),
                hinge=True,
            )
        return items

    return items


def readback_hinge_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Items that alone can close a readback window."""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("hinge"):
            out.append(item)
            continue
        # Legacy checklists (no hinge flag): treat highlighted squawk/runway/eor.
        key = str(item.get("key") or "")
        if item.get("highlight") and key in ("squawk", "runway", "eor"):
            out.append(item)
    return out


def _runway_number(rwy: str | None) -> int | None:
    digits = re.sub(r"[^0-9]", "", str(rwy or ""))
    if not digits:
        return None
    return int(digits[-2:]) if len(digits) >= 2 else int(digits)


def _flip_runway_side(rwy: str) -> str | None:
    """21R → 21L, 03L → 03R; None if no L/R side."""
    n = normalize_runway(rwy)
    if not n or len(n) < 3:
        return None
    side = n[-1]
    if side == "L":
        return f"{n[:-1]}R"
    if side == "R":
        return f"{n[:-1]}L"
    return None


def airport_ops_runways(airport: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for raw in airport.get("runways") or []:
        n = normalize_runway(raw)
        if n:
            out.append(n)
    return out


def airport_instrument_runways(airport: dict[str, Any]) -> list[str]:
    """
    Instrument-approach runways (Nellis: 21L / 03R).
    Falls back to opposite L/R of each ops runway when not configured.
    """
    out: list[str] = []
    for raw in airport.get("instrument_runways") or []:
        n = normalize_runway(raw)
        if n:
            out.append(n)
    if out:
        return out
    for ops in airport_ops_runways(airport):
        flipped = _flip_runway_side(ops)
        if flipped:
            out.append(flipped)
    return out


def align_runway_to_airport(
    airport: dict[str, Any],
    runway: str | None,
    *,
    instrument: bool = False,
) -> str:
    """
    Snap a candidate runway to the airport ops or instrument list by number.

    Ops (default): FLEX21L / 21L → 21R when 21R is the configured ops runway.
    Instrument: 21R → 21L when doing an instrument approach.
    """
    pool = (
        airport_instrument_runways(airport)
        if instrument
        else airport_ops_runways(airport)
    )
    cand = normalize_runway(runway) or str(runway or "").strip().upper()
    if not pool:
        return cand or "21R"
    if cand in pool:
        return cand
    num = _runway_number(cand)
    if num is not None:
        for p in pool:
            if _runway_number(p) == num:
                return p
    # No number match — keep wind-preferred first pool entry
    return pool[0]


def uses_instrument_runway(
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    template: str | None = None,
) -> bool:
    """True only for instrument-approach phraseology (not taxi/tower dep)."""
    tmpl = str(template or (step or {}).get("template") or "").strip()
    # Departure / ground flow always uses ops runways (21R / 03L).
    if tmpl in {
        "clearance",
        "clearance_readback",
        "taxi",
        "hold_short",
        "lineup",
        "clear_takeoff",
        "clear_takeoff_intersection",
        "clear_takeoff_rolling",
        "remain_position",
        "exit_runway",
        "taxi_in",
        "monitor_tower",
        "contact_tower",
        "radar_contact",
        "departure_handoff",
        "center_radar",
        "rolling_accept",
    }:
        return False
    rec = ""
    if step is not None:
        rec = normalize_recovery_key(
            step.get("recovery")
            or step.get("approach_pattern")
            or step.get("pattern")
            or step.get("recovery_type"),
            default="",
        )
    if not rec:
        rec = resolve_active_recovery(step, mission, state=state)
    if rec != "instrument":
        return False
    if tmpl in {
        "approach_check_in",
        "cleared_approach",
        "approach_procedure",
        "approach_iaf",
        "clear_land",
        "go_around",
    }:
        return True
    ch = str((step or {}).get("channel") or "").strip().casefold()
    return ch == "approach"


def pick_departure_runway(
    airport: dict[str, Any],
    weather: Weather,
    opus: OpusFlightContext | None,
    config: dict[str, Any] | None = None,
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    template: str | None = None,
) -> str:
    """
    Runway selection order:
    1. Per-step runway on the mission step (if set) — honored as-is
    2. Pilot-requested runway (Fly) — honored as-is
    3. Manual runway_override from Setup — honored as-is
    4. Runway coded on the filed Opus route (snapped)
    5. Wind-preferred recovery runway (prefer 21; 03 only with >= 11 kt
       headwind on 03) — not nearest-heading alone

    FP/wind results snap to ops runways (21R / 03L), or to instrument
    runways (21L / 03R) only for instrument-approach phrases. Explicit
    step / pilot / Setup choices are never snapped away.
    """
    # Explicit controller / pilot choices — do not remap L/R.
    if step is not None:
        step_rwy = normalize_runway(step.get("runway"))
        if step_rwy:
            print(f"Runway (step): {step_rwy}")
            return step_rwy
    req = requested_runway(mission=mission, state=state)
    if req:
        print(f"Runway (pilot request): {req}")
        return req
    if config is not None:
        override = runway_override(config)
        if override:
            print(f"Runway override: {override}")
            return override

    instrument = uses_instrument_runway(
        step=step, mission=mission, state=state, template=template
    )
    tmpl = str(template or (step or {}).get("template") or "").strip()
    # Recovery / approach phrases prefer the 21s unless wind requires 03.
    if tmpl in {
        "approach_check_in",
        "cleared_approach",
        "approach_procedure",
        "approach_iaf",
        "right_break",
        "clear_land",
        "go_around",
        "bj_range_exit",
    } or str((step or {}).get("phase") or "").lower() == "approach":
        # Refresh stale Approach plans when winds no longer favor that end.
        plan = assign_approach_plan(
            airport,
            weather,
            mission=mission,
            state=state,
            opus=opus,
            force=False,
        )
        plan_rwy = str(plan.get("runway") or "").strip()
        if plan_rwy:
            plan_instrument = normalize_recovery_key(plan.get("pattern")) == "instrument"
            return align_runway_to_airport(
                airport, plan_rwy, instrument=plan_instrument or instrument
            )
        picked = pick_recovery_runway(airport, weather, instrument=instrument)
        print(f"Runway (recovery bias): {picked}")
        return picked

    raw: str | None = runway_from_route(opus.fp_route_string if opus else None)
    if raw is None:
        # Prefer-21 wind gate — do not use nearest-heading (081/07 → 03).
        raw = pick_recovery_runway(airport, weather, instrument=instrument)
    aligned = align_runway_to_airport(airport, raw, instrument=instrument)
    if aligned != raw:
        kind = "instrument" if instrument else "ops"
        print(f"Runway aligned ({kind}): {raw} -> {aligned}")
    return aligned


# Templates whose spoken phrase includes a runway
TEMPLATES_USING_RUNWAY = frozenset(
    {
        "clearance",
        "clearance_readback",
        "taxi",
        "hold_short",
        "lineup",
        "clear_takeoff",
        "clear_takeoff_intersection",
        "clear_takeoff_rolling",
        "remain_position",
        "clear_land",
        "go_around",
        "right_break",
        "exit_runway",
        "approach_check_in",
        "cleared_approach",
        "approach_procedure",
        "approach_iaf",
        "taxi_in",
        "radar_contact",
    }
)


def expect_minutes_value(airport: dict[str, Any]) -> int:
    raw = airport.get("expect_minutes", airport.get("expect_after"))
    if raw is None or raw == "":
        return 10
    digits = re.sub(r"\D", "", str(raw))
    return int(digits) if digits else 10


def speak_minutes_natural(minutes: int) -> str:
    """Human minute word when available ('ten'), else digit speech."""
    return MINUTE_WORDS.get(minutes) or speak_digits(str(minutes))


def expect_timing_phrase(airport: dict[str, Any]) -> str:
    """
    Randomized expect timing:
      'in ten'  OR  'ten minutes after departure'
    """
    minutes = expect_minutes_value(airport)
    natural = speak_minutes_natural(minutes)
    return _pick(
        f"in {natural}",
        f"{natural} minutes after departure",
    )


def build_clearance_delivery(
    airport: dict[str, Any],
    callsign: str,
    weather: Weather,
    runway: str,
    opus: OpusFlightContext | None,
    initial_climb_ft: int | None = None,
    channel: str | None = None,
) -> tuple[str, int]:
    """
    NATCF clearance (455 wiki + Bruiser PDF):
      With SID/Flex: Cleared to DEST via the [procedure], then as filed, …
      No procedure:  Cleared to DEST as filed, …
      Then climb / expect / departure channel / squawk.

    Returns (phrase, climb_feet_used).
    """
    del weather  # clearance does not include altimeter
    del runway
    name = airport["name"]
    agency = clearance_spoken_agency(airport, channel=channel or "delivery")
    cs = speak_callsign(callsign)
    filed_alt = speak_filed_altitude(opus.fp_altitude if opus else None)
    squawk = squawk_clearance_phrase(opus)
    dep_clause = speak_departure_freq_or_local(airport)
    climb_ft = random_initial_climb_feet(initial_climb_ft)
    climb = speak_altitude_value(str(climb_ft), prefer_fl_below=1000)
    minutes = expect_minutes_value(airport)
    natural = speak_minutes_natural(minutes)

    if not opus or not opus.has_filed_plan:
        no_fp = _pick(
            "I show no flight plan on file",
            "negative flight plan on file",
            "I have no flight plan on file",
        )
        parts = [f"{cs}, {name} {agency}, {no_fp}"]
        if climb:
            parts.append(f"climb and maintain {climb}")
        parts.append(dep_clause)
        if squawk:
            parts.append(squawk)
        return f"{', '.join(parts)}.", climb_ft

    dest = speak_icao_or_name(opus.arr_icao, airport)
    dep_match = match_departure(airport, opus.fp_route_string)
    dep_via = speak_departure_clearance(airport, opus.fp_route_string)
    if dep_via:
        # SID/Flex present — "then as filed" only after the procedure
        # Wiki: "Cleared to Nellis via the DREAM SEVEN departure, then as filed"
        parts = [
            f"{cs}, {name} {agency}, cleared to {dest} via the {dep_via}",
            "then as filed",
        ]
    else:
        # No departure procedure — plain "as filed" (no "then")
        parts = [f"{cs}, {name} {agency}, cleared to {dest} as filed"]

    has_instrument_sid = bool(dep_match.instrument_say)
    # Vertical: Climb via the SID / Climb as published, SID with interim cap, or direct + expect
    if has_instrument_sid:
        direct_alt = (
            f"climb and maintain {climb}, expect {filed_alt} {natural} minutes after departure"
            if filed_alt
            else f"climb and maintain {climb}"
        )
        sid_climb = _pick("climb via the SID", "climb as published")
        if initial_climb_ft is not None:
            # Rebuild/Hear: keep published-SID climb (don't re-roll to direct)
            parts.append(sid_climb)
        else:
            choice = _pick("sid", "sid", "sid_except", "direct")  # prefer published SID climb
            if choice == "sid":
                parts.append(sid_climb)
            elif choice == "sid_except":
                # "except maintain" pairs more naturally with via-the-SID
                parts.append(f"climb via the SID except maintain {climb}")
            else:
                parts.append(direct_alt)
    else:
        # Flex / visual — Bruiser-style maintain + expect filed
        if climb:
            parts.append(_pick(f"maintain {climb}", f"climb and maintain {climb}"))
        if filed_alt:
            parts.append(f"expect {filed_alt} {natural} minutes after departure")

    parts.append(dep_clause)
    if squawk:
        parts.append(squawk)

    return f"{', '.join(parts)}.", climb_ft


def build_climb_correction(callsign: str, climb_ft: int | None) -> str:
    """Wrong climb readback — restated altitude, no agency name."""
    cs = speak_callsign(callsign)
    climb = speak_altitude_value(str(climb_ft), prefer_fl_below=1000) if climb_ft else None
    if climb:
        return f"{cs}, negative, climb and maintain {climb}."
    return f"{cs}, negative, say again climb altitude."


def build_clearance_readback(
    airport: dict[str, Any],
    callsign: str,
    weather: Weather,
    runway: str,
    channel: str | None = None,
) -> str:
    """
    After pilot readback — Delivery hands off to Ground:
      readback correct, contact ground [Local N | on freq] when ready for taxi.
    """
    del weather, runway
    name = airport["name"]
    agency = clearance_spoken_agency(airport, channel=channel or "delivery")
    cs = speak_callsign(callsign)
    ground = speak_ground_contact_target(airport)
    return (
        f"{cs}, {name} {agency}, readback correct, "
        f"contact {ground} when ready for taxi."
    )


def speak_freq(mhz: float) -> str:
    # 275.8 -> two seven fife point/decimal eight
    # 327.0 -> three two seven point/decimal zero (keep the trailing zero)
    rounded = round(float(mhz), 3)
    sep = _pick("point", "decimal")
    if abs(rounded - round(rounded)) < 1e-9:
        return f"{speak_digits(str(int(round(rounded))))} {sep} zero"
    s = f"{rounded:.3f}".rstrip("0").rstrip(".")
    if "." in s:
        whole, frac = s.split(".", 1)
        return f"{speak_digits(whole)} {sep} {speak_digits(frac)}"
    return speak_digits(s)


def speak_altimeter(inhg: float) -> str:
    # 29.75 -> two niner seven five
    hundredths = int(round(inhg * 100))
    return speak_digits(f"{hundredths:04d}")


def speak_runway(rwy: str) -> str:
    """Speak runway as two digits + side, e.g. '03L' → 'zero tree left'."""
    m = re.match(r"^\s*(\d{1,2})\s*([LCR]?)\s*$", str(rwy), re.IGNORECASE)
    if not m:
        digits = re.sub(r"[^0-9]", "", str(rwy))
        if not digits:
            return str(rwy or "").strip()
        # Prefer last 1–2 digits as the runway number, always two spoken digits
        n = int(digits[-2:]) if len(digits) >= 2 else int(digits)
        return speak_digits(f"{n:02d}")
    num = speak_digits(f"{int(m.group(1)):02d}")
    side = {"L": "left", "R": "right", "C": "center"}.get(m.group(2).upper(), "")
    return f"{num} {side}".strip()


# Compass / place tokens from airport JSON (e.g. "NW EOR") — expand for every TTS engine.
# Two-letter forms are safe in any phrase; single N/S/E/W only on known place fields
# (so taxiway "E" is not rewritten to "east").
_COMPASS_SPEAK: dict[str, str] = {
    "ne": "northeast",
    "nw": "northwest",
    "se": "southeast",
    "sw": "southwest",
}
_COMPASS_SPEAK_PLACE: dict[str, str] = {
    **_COMPASS_SPEAK,
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
}
_PLACE_ACRONYM_SPEAK: dict[str, str] = {
    # Keep spoken as the acronym (not "end of runway").
    "eor": "EOR",
}

# Words TTS mis-stresses; display/log text stays the real spelling.
_RADIO_WORD_PRONUNCIATION: dict[str, str] = {
    "fragged": "fragd",
    # Flight lead (leed), not the metal "led".
    "lead": "leed",
}

# Multi-word closers — applied before per-token swaps (both Google + Windows).
# "see ya" otherwise becomes yeah / yaw.
_RADIO_PHRASE_PRONUNCIATION: tuple[tuple[str, str], ...] = (
    (r"\bsee\s+ya\b", "seeyuh"),
    (r"\bsee\s+yah\b", "seeyuh"),
)

# Microsoft SAPI often reads weather "wind" as /waɪnd/ (whined). Respell for Speak().
# Do NOT use this on Google/Chirp — they spell "wihnd" letter-by-letter.
_WINDOWS_WORD_PRONUNCIATION: dict[str, str] = {
    "wind": "wihnd",
    "winds": "wihnds",
}

# Google Chirp / Neural2: keep the real spelling and force noun IPA via the
# synthesize customPronunciations field (plain respells get spelled out).
_GOOGLE_WORD_IPA: dict[str, str] = {
    "wind": "wɪnd",
    "winds": "wɪndz",
}


def _expand_place_tokens(text: str, *, compass: dict[str, str]) -> str:
    out: list[str] = []
    for tok in str(text or "").split():
        low = tok.casefold().strip(".,;:")
        if low in compass:
            out.append(compass[low])
        elif low in _PLACE_ACRONYM_SPEAK:
            out.append(_PLACE_ACRONYM_SPEAK[low])
        else:
            out.append(tok)
    return " ".join(out)


def speak_place_label(text: str) -> str:
    """
    Expand taxi/parking place labels for speech on all voice types.

    'NW EOR' → 'northwest EOR'; 'Alpha South' unchanged.
    """
    return _expand_place_tokens(text, compass=_COMPASS_SPEAK_PLACE)


def expand_radio_place_tokens(text: str) -> str:
    """Compass/EOR expansions for TTS prep (all engines; safe two-letter forms)."""
    if not text:
        return text
    return _expand_place_tokens(text, compass=_COMPASS_SPEAK)


def apply_radio_pronunciations(text: str) -> str:
    """
    Swap ATC words TTS misreads across engines (fragged→fragd, lead→leed, …).

    Weather "wind" is NOT respelt here — Google spells phonetic hacks like
    "wihnd" out loud. Windows uses apply_windows_radio_pronunciations; Google
    uses google_custom_pronunciations() IPA overrides on synthesize.
    """
    if not text:
        return text
    s = str(text)
    for pattern, repl in _RADIO_PHRASE_PRONUNCIATION:
        s = re.sub(pattern, repl, s, flags=re.IGNORECASE)
    out: list[str] = []
    for tok in s.split():
        raw = tok
        punct_l = ""
        punct_r = ""
        core = raw
        while core and core[0] in ".,;:!?\"'(":
            punct_l += core[0]
            core = core[1:]
        while core and core[-1] in ".,;:!?\"')":
            punct_r = core[-1] + punct_r
            core = core[:-1]
        low = core.casefold()
        spoken = _RADIO_WORD_PRONUNCIATION.get(low)
        if spoken is None:
            out.append(raw)
        else:
            out.append(f"{punct_l}{spoken}{punct_r}")
    return " ".join(out)


def apply_windows_radio_pronunciations(text: str) -> str:
    """
    Homograph fixes for System.Speech / ExternalAudio plain Speak().

    Google/Chirp must not use these respells — they read "wihnd" as letters.
    """
    if not text:
        return text
    out: list[str] = []
    for tok in str(text).split():
        raw = tok
        punct_l = ""
        punct_r = ""
        core = raw
        while core and core[0] in ".,;:!?\"'(":
            punct_l += core[0]
            core = core[1:]
        while core and core[-1] in ".,;:!?\"')":
            punct_r = core[-1] + punct_r
            core = core[:-1]
        low = core.casefold()
        spoken = _WINDOWS_WORD_PRONUNCIATION.get(low)
        if spoken is None:
            out.append(raw)
        else:
            out.append(f"{punct_l}{spoken}{punct_r}")
    return " ".join(out)


def google_custom_pronunciations(text: str) -> dict[str, Any] | None:
    """
    Chirp / Neural2 IPA overrides for ATC homographs (weather wind = /wɪnd/).

    Phrase must match the synthesize input exactly (Chirp lowercases; Neural2
    may keep capitals), so every casing found in `text` gets its own entry.
    """
    if not text:
        return None
    found: dict[str, str] = {}
    for m in re.finditer(r"\b(winds?)\b", str(text), flags=re.IGNORECASE):
        phrase = m.group(1)
        ipa = _GOOGLE_WORD_IPA.get(phrase.casefold())
        if ipa:
            found[phrase] = ipa
    if not found:
        return None
    return {
        "pronunciations": [
            {
                "phrase": phrase,
                "phoneticEncoding": "PHONETIC_ENCODING_IPA",
                "pronunciation": ipa,
            }
            for phrase, ipa in found.items()
        ]
    }


# ICAO / NATO taxiway letters (spoken on every voice type).
_NATO_TAXIWAY: dict[str, str] = {
    "a": "Alpha",
    "b": "Bravo",
    "c": "Charlie",
    "d": "Delta",
    "e": "Echo",
    "f": "Foxtrot",
    "g": "Golf",
    "h": "Hotel",
    "i": "India",
    "j": "Juliet",
    "k": "Kilo",
    "l": "Lima",
    "m": "Mike",
    "n": "November",
    "o": "Oscar",
    "p": "Papa",
    "q": "Quebec",
    "r": "Romeo",
    "s": "Sierra",
    "t": "Tango",
    "u": "Uniform",
    "v": "Victor",
    "w": "Whiskey",
    "x": "X-ray",
    "y": "Yankee",
    "z": "Zulu",
}


_NATO_TAXIWAY_BY_NAME: dict[str, str] = {
    v.casefold(): v for v in _NATO_TAXIWAY.values()
}


def speak_taxi_via(text: str) -> str:
    """
    Speak a taxi via route for all voice types.

    'F, E' / 'F E' → 'Foxtrot Echo'; 'Foxtrot' kept as Foxtrot.
    """
    raw = str(text or "").replace(",", " ")
    out: list[str] = []
    for tok in raw.split():
        low = tok.casefold().strip(".,;:")
        if len(low) == 1 and low in _NATO_TAXIWAY:
            out.append(_NATO_TAXIWAY[low])
        elif low in _NATO_TAXIWAY_BY_NAME:
            out.append(_NATO_TAXIWAY_BY_NAME[low])
        else:
            out.append(tok)
    return " ".join(out)


def speak_wind(dir_deg: int | None, speed_kt: int | None) -> str:
    if dir_deg is None or speed_kt is None:
        return "wind calm"
    if speed_kt == 0:
        return "wind calm"
    return f"wind {speak_digits(f'{dir_deg:03d}')} at {speak_digits(str(speed_kt))}"


def parse_metar(raw: str) -> Weather:
    wind_dir = None
    wind_speed = None
    altimeter = None
    ceiling_ft = None
    visibility_sm = None

    wind_m = re.search(r"\b(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT\b", raw)
    if wind_m:
        if wind_m.group(1) != "VRB":
            wind_dir = int(wind_m.group(1))
        wind_speed = int(wind_m.group(2))

    alt_m = re.search(r"\bA(\d{4})\b", raw)
    if alt_m:
        altimeter = int(alt_m.group(1)) / 100.0
    else:
        q_m = re.search(r"\bQ(\d{4})\b", raw)
        if q_m:
            # hPa to inHg approx for speech of A-style; keep as inHg-like hundredths from QNH*0.02953
            hpa = int(q_m.group(1))
            altimeter = round(hpa * 0.02953, 2)

    # Visibility: 1SM, 3SM, 10SM, 1/2SM, P6SM, or meters (e.g. 9999)
    vis_m = re.search(r"\b(\d{1,2}(?:/\d{1,2})?)SM\b", raw)
    if vis_m:
        frac = vis_m.group(1)
        if "/" in frac:
            num, den = frac.split("/", 1)
            try:
                visibility_sm = float(num) / float(den)
            except (TypeError, ValueError, ZeroDivisionError):
                visibility_sm = None
        else:
            try:
                visibility_sm = float(frac)
            except (TypeError, ValueError):
                visibility_sm = None
    elif re.search(r"\bP6SM\b", raw):
        visibility_sm = 6.0
    else:
        m_vis = re.search(r"\b(\d{4})\b", raw)
        if m_vis:
            try:
                meters = int(m_vis.group(1))
                if meters >= 9999:
                    visibility_sm = 6.0
                elif meters > 0:
                    visibility_sm = round(meters / 1609.34, 1)
            except (TypeError, ValueError):
                pass

    # Ceiling = lowest BKN/OVC layer (hundreds of feet → feet)
    for cov, hun in re.findall(r"\b(BKN|OVC)(\d{3})\b", raw.upper()):
        del cov
        try:
            ft = int(hun) * 100
        except (TypeError, ValueError):
            continue
        if ceiling_ft is None or ft < ceiling_ft:
            ceiling_ft = ft

    return Weather(
        wind_dir,
        wind_speed,
        altimeter,
        raw,
        ceiling_ft=ceiling_ft,
        visibility_sm=visibility_sm,
    )


def fetch_metar(config: dict[str, Any], icao: str) -> Weather:
    url = config["opus_metar_url"].format(icao=icao)
    cache_key = f"{url}|{icao.strip().upper()}"
    now = time.time()
    cached = _METAR_CACHE.get(cache_key)
    if cached and float(cached.get("exp") or 0) > now:
        return cached["wx"]

    req = urllib.request.Request(
        url,
        headers={"User-Agent": config.get("user_agent", "DCS-ATC-Phrase/1.0")},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"WARNING: Opus METAR fetch failed ({exc}); using calm defaults", file=sys.stderr)
        wx = Weather(None, 0, 29.92, "")
        _METAR_CACHE[cache_key] = {"exp": now + _METAR_CACHE_TTL_SEC, "wx": wx}
        return wx

    raw = ""
    if isinstance(payload, dict):
        metar = payload.get("metar")
        if isinstance(metar, dict):
            raw = metar.get("raw") or ""
        elif isinstance(metar, str):
            raw = metar
        elif "raw" in payload:
            raw = str(payload["raw"])
    if not raw:
        print("WARNING: Opus METAR empty; using calm defaults", file=sys.stderr)
        wx = Weather(None, 0, 29.92, "")
        _METAR_CACHE[cache_key] = {"exp": now + _METAR_CACHE_TTL_SEC, "wx": wx}
        return wx
    wx = parse_metar(raw)
    _METAR_CACHE[cache_key] = {"exp": now + _METAR_CACHE_TTL_SEC, "wx": wx}
    return wx


def resolve_opus_and_metar(
    config: dict[str, Any], icao: str
) -> tuple[OpusFlightContext | None, Weather]:
    """Fetch Opus flight + METAR in parallel (common Hear/TX prep)."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_opus = pool.submit(resolve_active_opus_flight, config)
        fut_wx = pool.submit(fetch_metar, config, icao)
        return fut_opus.result(), fut_wx.result()


def heading_delta(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def active_runway(runways: list[str], wind_dir: int | None) -> str:
    if not runways:
        return "21"
    if wind_dir is None:
        return runways[0]
    best = runways[0]
    best_delta = 999.0
    for rwy in runways:
        digits = re.sub(r"[^0-9]", "", rwy)
        if not digits:
            continue
        hdg = (int(digits) * 10) % 360
        delta = heading_delta(float(wind_dir), float(hdg))
        if delta < best_delta:
            best_delta = delta
            best = rwy
    return best


def channel_radio(airport: dict[str, Any], channel: str) -> tuple[float, str, str]:
    """Return (freq_mhz, mod, tx_name_suffix)."""
    ch = airport.get(channel) or airport.get("other") or {"freq_mhz": 251.0, "mod": "AM"}
    freq = float(ch["freq_mhz"])
    mod = ch.get("mod", "AM")
    name = str(airport.get("name", "ATC")).replace(" ", "")
    suffix = channel.title().replace("_", "")
    return freq, mod, f"{name}{suffix}"


def step_radio(
    airport: dict[str, Any],
    channel: str,
    step: dict[str, Any] | None = None,
) -> tuple[float, str, str]:
    """
    Airport channel freq/mod, with optional per-step overrides.
    Step keys: freq_mhz, mod (commonly used for channel=other).
    """
    freq, mod, tx_name = channel_radio(airport, channel)
    if not step:
        return freq, mod, tx_name
    override = _parse_mhz(step.get("freq_mhz"))
    if override is not None:
        freq = override
    step_mod = str(step.get("mod") or "").strip()
    if step_mod:
        mod = step_mod
    return freq, mod, tx_name


def _parse_mhz(value: Any) -> float | None:
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s or s == "-":
        return None
    try:
        mhz = float(s)
    except ValueError:
        return None
    if mhz <= 0:
        return None
    return mhz


def fetch_opus_theater_freqs(
    config: dict[str, Any],
    *,
    theater_id: int | None = None,
    icao: str | None = None,
    band: str = "uhf",
) -> dict[str, Any]:
    """
    Pull theater radio presets from Opus and map to our agency channels.
    Prefers presets whose name contains the airport ICAO (e.g. KLSV Ground).
    Returns {"freqs": {channel: mhz}, "matched": [...], "theater_id": n}.
    """
    backend = (config.get("opus_backend_url") or "").rstrip("/")
    if not backend:
        raise RuntimeError("opus_backend_url not set")

    tid = theater_id
    if tid is None:
        opus = resolve_active_opus_flight(config)
        tid = opus.theater_id if opus else None
    if tid is None:
        tid = int(config.get("opus_theater_id") or 1)

    ua = config.get("user_agent", "DCS-ATC-Phrase/1.0")
    data = http_get_json(f"{backend}/theaters/{tid}/radios", ua)
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Opus theater radios response")

    presets = list(data.get(band) or [])
    icao_u = (icao or "").strip().upper()
    freqs: dict[str, float] = {}
    matched: list[dict[str, Any]] = []

    presets_by_channel: dict[str, int] = {}
    for channel, needles in OPUS_FREQ_NAME_MAP.items():
        best: tuple[int, float, str, int | None] | None = None  # score, mhz, name, preset#
        for preset in presets:
            name = str(preset.get("name") or "")
            name_l = name.casefold()
            mhz = _parse_mhz(preset.get("frequency"))
            if mhz is None:
                continue
            if not any(n in name_l for n in needles):
                continue
            score = 0
            if icao_u and icao_u.casefold() in name_l:
                score += 10
            if icao_u and name_l.startswith(icao_u.casefold()):
                score += 2
            agency = needles[0]
            if name_l.rstrip().endswith(agency):
                score += 5
            try:
                preset_n = int(preset.get("preset")) if preset.get("preset") is not None else None
            except (TypeError, ValueError):
                preset_n = None
            if best is None or score > best[0]:
                best = (score, mhz, name, preset_n)
        if best is not None:
            freqs[channel] = best[1]
            if best[3] is not None:
                presets_by_channel[channel] = best[3]
            matched.append(
                {
                    "channel": channel,
                    "name": best[2],
                    "freq_mhz": best[1],
                    "preset": best[3],
                }
            )

    return {
        "theater_id": tid,
        "band": band,
        "freqs": freqs,
        "presets": presets_by_channel,
        "matched": matched,
    }


def compare_airport_freqs(
    airport: dict[str, Any],
    opus_freqs: dict[str, float],
) -> list[dict[str, Any]]:
    """Compare local airport freqs vs Opus defaults."""
    rows: list[dict[str, Any]] = []
    for ch, opus_mhz in sorted(opus_freqs.items()):
        local = airport.get(ch) or {}
        local_mhz = _parse_mhz(local.get("freq_mhz"))
        match = local_mhz is not None and abs(local_mhz - opus_mhz) < 0.001
        rows.append(
            {
                "channel": ch,
                "local": local_mhz,
                "opus": opus_mhz,
                "match": match,
            }
        )
    return rows


def apply_opus_freqs_to_airport(
    airport: dict[str, Any],
    opus_freqs: dict[str, float],
    *,
    only_missing: bool = False,
    presets: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Write Opus freqs into airport channel blocks. Returns channels updated."""
    updated: dict[str, Any] = {}
    presets = presets or {}
    for ch, mhz in opus_freqs.items():
        existing = airport.get(ch) or {}
        if only_missing and _parse_mhz(existing.get("freq_mhz")) is not None:
            continue
        block: dict[str, Any] = {
            "freq_mhz": mhz,
            "mod": existing.get("mod") or "AM",
            "source": "opus",
        }
        if ch in presets:
            block["local_preset"] = int(presets[ch])
        elif existing.get("local_preset") is not None:
            block["local_preset"] = existing["local_preset"]
        airport[ch] = block
        updated[ch] = mhz
    return updated


# ATC-focused Google voices: US first (clear / radio-friendly), then a few allies.
# Order in this list is the UI order (do not alphabetize locales).
_GOOGLE_VOICE_CHOICES_RAW: list[str] = [
    # --- United States (primary) ---
    # Chirp 3: HD — firm / direct controller candidates (try these first)
    "en-US-Chirp3-HD-Charon",  # male — deep, direct (best continuous radio cadence)
    "en-US-Chirp3-HD-Orus",  # male — crisp
    "en-US-Chirp3-HD-Schedar",  # male — steady
    "en-US-Chirp3-HD-Algenib",  # male
    "en-US-Chirp3-HD-Algieba",  # male
    "en-US-Chirp3-HD-Alnilam",  # male
    "en-US-Chirp3-HD-Achird",  # male
    "en-US-Chirp3-HD-Umbriel",  # male
    "en-US-Chirp3-HD-Iapetus",  # male
    "en-US-Chirp3-HD-Enceladus",  # male
    "en-US-Chirp3-HD-Sadachbia",  # male
    "en-US-Chirp3-HD-Fenrir",  # male — firm but chunky pauses (last resort)
    "en-US-Chirp3-HD-Kore",  # female — clear / brisk
    "en-US-Chirp3-HD-Aoede",  # female
    "en-US-Chirp3-HD-Leda",  # female
    "en-US-Chirp3-HD-Zephyr",  # female
    # Neural2 — proven ATC clarity
    "en-US-Neural2-D",  # male — solid default ATC
    "en-US-Neural2-J",  # male — firm
    "en-US-Neural2-I",  # male
    # Clear female US (delivery / approach variety)
    "en-US-Neural2-A",
    "en-US-Neural2-C",
    "en-US-Neural2-E",
    "en-US-Neural2-F",
    "en-US-Neural2-G",
    "en-US-Neural2-H",
    # US WaveNet fallbacks (still good on radio)
    "en-US-Wavenet-D",
    "en-US-Wavenet-J",
    "en-US-Wavenet-I",
    "en-US-Wavenet-B",
    "en-US-Wavenet-A",
    "en-US-Wavenet-C",
    "en-US-Wavenet-F",
    # --- Allies (short list) ---
    # UK
    "en-GB-Neural2-B",  # male
    "en-GB-Neural2-D",  # male
    "en-GB-Neural2-A",  # female
    "en-GB-Neural2-C",  # female
    # Australia
    "en-AU-Neural2-B",  # male
    "en-AU-Neural2-D",  # male
    "en-AU-Neural2-A",  # female
    "en-AU-Neural2-C",  # female
    # English (Canada) WaveNet was retired from Google's catalog — only French
    # Canadian (fr-CA) remains. Old en-CA-* ids are remapped below.
]

# Locale display order for picker headers (US → allies)
_LOCALE_DISPLAY_ORDER: list[str] = ["en-US", "en-GB", "en-AU"]

LOCALE_LABELS: dict[str, str] = {
    "en-US": "United States (primary)",
    "en-GB": "United Kingdom (ally)",
    "en-AU": "Australia (ally)",
    "en-IN": "English (India)",
    "en-IE": "English (Ireland)",
    "en-NZ": "English (New Zealand)",
    "en-ZA": "English (South Africa)",
}

# Google's synthesize API now rejects these. Same trailing letter → US WaveNet twin
# so agency gender stays put when an old config is loaded.
_RETIRED_GOOGLE_VOICES: dict[str, str] = {
    "en-CA-Wavenet-A": "en-US-Wavenet-A",
    "en-CA-Wavenet-B": "en-US-Wavenet-B",
    "en-CA-Wavenet-C": "en-US-Wavenet-C",
    "en-CA-Wavenet-D": "en-US-Wavenet-D",
    "en-CA-Standard-A": "en-US-Wavenet-A",
    "en-CA-Standard-B": "en-US-Wavenet-B",
    "en-CA-Standard-C": "en-US-Wavenet-C",
    "en-CA-Standard-D": "en-US-Wavenet-D",
}


def resolve_retired_google_voice(voice: str) -> tuple[str, str | None]:
    """Map a retired Google voice id to a live one. Returns (voice, note|None)."""
    name = (voice or "").strip()
    replacement = _RETIRED_GOOGLE_VOICES.get(name)
    if not replacement:
        return name, None
    return replacement, f"Google retired {name}; using {replacement} instead."


def migrate_retired_google_voices(config: dict[str, Any]) -> list[str]:
    """Rewrite retired Google voice ids in config. Mutates in place; returns notes."""
    if tts_provider(config) != "google":
        return []
    notes: list[str] = []
    voices = config.get("tts_voices")
    if isinstance(voices, dict):
        updated: dict[str, str] = {}
        for channel, raw in voices.items():
            voice, note = resolve_retired_google_voice(str(raw or ""))
            if note and note not in notes:
                notes.append(note)
            if voice:
                updated[str(channel)] = voice
        if updated:
            config["tts_voices"] = updated
    default = str(config.get("tts_voice") or "").strip()
    if default:
        safe, note = resolve_retired_google_voice(default)
        if note and note not in notes:
            notes.append(note)
        if safe != default:
            config["tts_voice"] = safe
            config["tts_gender"] = voice_gender(safe)
    return notes


def voice_locale(voice_name: str) -> str:
    """BCP-47 locale from a Google voice id or Windows voice display name."""
    name = (voice_name or "").strip()
    parts = name.split("-")
    if len(parts) >= 2 and len(parts[0]) == 2 and len(parts[1]) in (2, 3):
        return f"{parts[0]}-{parts[1]}"
    # Windows OneCore: "Microsoft Mark - English (United States)"
    m = re.search(r"\(([^)]+)\)\s*$", name)
    if m:
        region = m.group(1).strip().casefold()
        mapped = {
            "united states": "en-US",
            "united kingdom": "en-GB",
            "canada": "en-CA",
            "australia": "en-AU",
            "ireland": "en-IE",
            "india": "en-IN",
            "new zealand": "en-NZ",
        }.get(region)
        if mapped:
            return mapped
    if "Desktop" in name or name.startswith("Microsoft "):
        return "en-US"
    return name[:5] if len(name) >= 5 else (name or "other")


def locale_label(locale: str) -> str:
    return LOCALE_LABELS.get(locale, locale)


def _locale_sort_key(locale: str) -> tuple[int, str]:
    try:
        return (_LOCALE_DISPLAY_ORDER.index(locale), "")
    except ValueError:
        return (len(_LOCALE_DISPLAY_ORDER), locale.casefold())


def sort_voices(voices: list[str]) -> list[str]:
    """
    Sort for the voice picker:
    - Preferred locale order (US first, then allies)
    - Within a locale, keep curated order when known; else A–Z
    """
    curated_rank = {name: i for i, name in enumerate(_GOOGLE_VOICE_CHOICES_RAW)}
    unique = []
    seen: set[str] = set()
    for v in voices:
        name = str(v).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append(name)

    def key(v: str) -> tuple:
        loc = voice_locale(v)
        if v in curated_rank:
            return (_locale_sort_key(loc)[0], 0, curated_rank[v])
        return (_locale_sort_key(loc)[0], 1, v.casefold())

    return sorted(unique, key=key)


_ONECORE_VOICE_TOKENS = r"HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"
_SAPI_VOICE_TOKENS = r"HKLM:\SOFTWARE\Microsoft\Speech\Voices\Tokens"
_SAPI_VOICE_TOKENS_WOW = r"HKLM:\SOFTWARE\WOW6432Node\Microsoft\Speech\Voices\Tokens"


def list_windows_sapi_voices() -> list[str]:
    """Voices System.Speech can actually SelectVoice() today."""
    if os.name != "nt":
        return ["Microsoft Zira Desktop"]
    ps = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.GetInstalledVoices() | ForEach-Object { if ($_.Enabled) { $_.VoiceInfo.Name } }
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        names = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        return sort_voices(names) or ["Microsoft Zira Desktop"]
    except Exception:
        return ["Microsoft Zira Desktop"]


def list_windows_onecore_voices() -> list[str]:
    """
    OneCore / mobile TTS voices installed on the PC (Linda, Mark, Richard, …).

    These are not visible to System.Speech until unlock_windows_onecore_voices()
    mirrors their registry tokens into the classic Speech path.
    """
    if os.name != "nt":
        return []
    ps = rf"""
$root = '{_ONECORE_VOICE_TOKENS}'
if (-not (Test-Path $root)) {{ return }}
Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {{
  $n = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).'(default)'
  if (-not $n) {{ $n = $_.PSChildName }}
  Write-Output $n
}}
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return sort_voices(
            [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        )
    except Exception:
        return []


def list_windows_voices() -> list[str]:
    """Usable Windows TTS voices for agency assignment / Preview / SRS."""
    return list_windows_sapi_voices()


def windows_onecore_voices_pending() -> list[str]:
    """OneCore voices installed but not yet unlocked for System.Speech."""
    sapi = {n.casefold() for n in list_windows_sapi_voices()}
    return [n for n in list_windows_onecore_voices() if n.casefold() not in sapi]


def unlock_windows_onecore_voices(*, elevate: bool = True) -> tuple[bool, str]:
    """
    Mirror OneCore voice tokens into classic Speech registry so System.Speech
    (and ExternalAudio) can SelectVoice Linda / Mark / Richard / etc.

    Requires Administrator. Returns (ok, message).
    """
    if os.name != "nt":
        return False, "OneCore unlock is Windows-only."
    pending = windows_onecore_voices_pending()
    if not pending and list_windows_onecore_voices():
        return True, "OneCore voices are already unlocked for System.Speech."
    if not list_windows_onecore_voices():
        return False, "No OneCore TTS voices found in the registry."

    ps = rf"""
$ErrorActionPreference = 'Stop'
$srcRoot = '{_ONECORE_VOICE_TOKENS}'
$dstRoots = @('{_SAPI_VOICE_TOKENS}', '{_SAPI_VOICE_TOKENS_WOW}')
if (-not (Test-Path $srcRoot)) {{ throw 'Speech_OneCore voice tokens not found.' }}
$copied = 0
Get-ChildItem $srcRoot | ForEach-Object {{
  $source = $_.PSPath
  $token = $_.PSChildName
  foreach ($dstRoot in $dstRoots) {{
    if (-not (Test-Path $dstRoot)) {{
      New-Item -Path $dstRoot -Force | Out-Null
    }}
    $dest = Join-Path $dstRoot $token
    if (Test-Path $dest) {{ Remove-Item -Path $dest -Recurse -Force }}
    Copy-Item -Path $source -Destination $dstRoot -Recurse -Force
  }}
  $copied++
}}
Write-Output ("UNLOCKED=" + $copied)
exit 0
"""
    script = Path(tempfile.gettempdir()) / "atc_unlock_onecore_voices.ps1"
    script.write_text(ps, encoding="utf-8")
    try:
        if elevate:
            # Relaunch the unlock script elevated; wait for it to finish.
            elev = rf"""
$p = Start-Process -FilePath powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList @(
  '-NoProfile','-ExecutionPolicy','Bypass','-File','{str(script).replace("'", "''")}'
)
if ($null -eq $p) {{ throw 'Elevation cancelled.' }}
exit $p.ExitCode
"""
            elev_path = Path(tempfile.gettempdir()) / "atc_unlock_onecore_elevate.ps1"
            elev_path.write_text(elev, encoding="utf-8")
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(elev_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        else:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except subprocess.TimeoutExpired:
        return False, "Unlock timed out waiting for the admin prompt / registry copy."
    except OSError as exc:
        return False, f"Could not run unlock script: {exc}"

    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        if "canceled" in out.casefold() or "cancelled" in out.casefold():
            return False, "Administrator elevation was cancelled."
        return False, f"Unlock failed (exit {proc.returncode}). {out.strip()[:240]}"

    now = list_windows_sapi_voices()
    gained = [n for n in pending if n in now or any(n.casefold() == x.casefold() for x in now)]
    # After registry copy, names may match OneCore display names.
    if not gained:
        # Re-check pending — empty means unlocked.
        still = windows_onecore_voices_pending()
        if not still:
            return True, f"Unlocked OneCore voices. System.Speech now has {len(now)} voice(s)."
        return (
            True,
            "Registry mirrored. Restart the app (and ExternalAudio if open), then Refresh list. "
            f"Still pending: {', '.join(still[:6])}",
        )
    return True, "Unlocked: " + ", ".join(gained)


def google_voice_choices() -> list[str]:
    """US-first ATC list in curated order (stable; not re-alphabetized by country code)."""
    return list(_GOOGLE_VOICE_CHOICES_RAW)


# Back-compat alias used by older UI code
GOOGLE_VOICE_CHOICES: list[str] = google_voice_choices()

DEFAULT_GOOGLE_VOICES: dict[str, str] = {
    # US male-leaning ATC defaults per agency
    "default": "en-US-Neural2-D",
    "delivery": "en-US-Neural2-J",
    "ground": "en-US-Neural2-D",
    "tower": "en-US-Neural2-I",
    "departure": "en-US-Neural2-J",
    "approach": "en-US-Neural2-C",
    "blackjack": "en-US-Neural2-I",
    "bandsaw": "en-US-Neural2-J",
    "ops": "en-US-Neural2-F",
    "other": "en-US-Neural2-D",
}

# Neural2 / WaveNet trailing letter → typical SsmlGender (Google en-* catalog)
_GOOGLE_VOICE_LETTER_GENDER = {
    "A": "female",
    "B": "male",
    "C": "female",
    "D": "male",
    "E": "female",
    "F": "female",
    "G": "female",
    "H": "female",
    "I": "male",
    "J": "male",
}

# Chirp 3: HD named voices (en-US-Chirp3-HD-Charon)
_CHIRP_VOICE_GENDER = {
    "achernar": "female",
    "achird": "male",
    "algenib": "male",
    "algieba": "male",
    "alnilam": "male",
    "aoede": "female",
    "autonoe": "female",
    "callirrhoe": "female",
    "charon": "male",
    "despina": "female",
    "enceladus": "male",
    "erinome": "female",
    "fenrir": "male",
    "gacrux": "female",
    "iapetus": "male",
    "kore": "female",
    "laomedeia": "female",
    "leda": "female",
    "orus": "male",
    "pulcherrima": "female",
    "puck": "male",
    "rasalgethi": "male",
    "sadachbia": "male",
    "sadaltager": "male",
    "schedar": "male",
    "sulafat": "female",
    "umbriel": "male",
    "vindemiatrix": "female",
    "zephyr": "female",
    "zubenelgenubi": "male",
}


def tts_provider(config: dict[str, Any]) -> str:
    """windows | google"""
    raw = str(config.get("tts_provider") or "windows").strip().casefold()
    if raw in ("google", "gcp", "gcloud"):
        return "google"
    return "windows"


def google_credentials_path(config: dict[str, Any]) -> Path | None:
    """Expand user/env vars; resolve relative paths against atc/; None if unset."""
    raw = str(config.get("google_credentials") or "").strip()
    if not raw:
        return None
    return resolve_repo_path(raw)


def is_google_voice_name(voice_name: str) -> bool:
    low = voice_name.casefold()
    return any(x in low for x in ("neural2", "wavenet", "chirp", "studio", "standard"))


def voice_gender(voice_name: str) -> str:
    low = voice_name.casefold()
    if any(
        x in low
        for x in ("zira", "linda", "female", "hazel", "susan", "eva", "aria", "jenny")
    ):
        return "female"
    if any(
        x in low
        for x in ("david", "guy", "male", "mark", "richard", "james")
    ):
        return "male"
    # Chirp 3: HD — en-US-Chirp3-HD-Charon
    m_chirp = re.search(r"chirp3?-?hd-([a-z]+)\b", low)
    if m_chirp:
        return _CHIRP_VOICE_GENDER.get(m_chirp.group(1), "male")
    # Google: en-US-Neural2-D → letter D
    m = re.search(r"-(?:neural2|wavenet|standard|studio)-([a-z])\b", low)
    if m:
        return _GOOGLE_VOICE_LETTER_GENDER.get(m.group(1).upper(), "male")
    return "male"


def voice_label(voice_name: str) -> str:
    """Short UI label with gender, e.g. 'en-US-Neural2-D  ·  male'."""
    name = (voice_name or "").strip()
    if not name:
        return ""
    return f"{name}  ·  {voice_gender(name)}"


def voice_for_channel(config: dict[str, Any], channel: str | None) -> tuple[str, str]:
    """Return (voice_name, gender) for an agency channel."""
    voices = config.get("tts_voices") or {}
    if tts_provider(config) == "google":
        default = (config.get("tts_voice") or DEFAULT_GOOGLE_VOICES["default"]).strip()
    else:
        default = (config.get("tts_voice") or "Microsoft Zira Desktop").strip()
    if not isinstance(voices, dict):
        voices = {}
    name = ""
    if channel:
        name = str(voices.get(channel) or "").strip()
    if not name:
        name = str(voices.get("default") or default).strip() or default
    if tts_provider(config) == "google":
        name, _note = resolve_retired_google_voice(name)
    gender = voice_gender(name)
    return name, gender


def voice_for_step(
    config: dict[str, Any],
    channel: str | None,
    step: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Per-step voice override (step['voice']) or agency default."""
    if step:
        override = str(step.get("voice") or "").strip()
        if override:
            if tts_provider(config) == "google":
                override, _note = resolve_retired_google_voice(override)
            return override, voice_gender(override)
    return voice_for_channel(config, channel)


def build_template_text(
    airport: dict[str, Any],
    template: str,
    callsign: str,
    weather: Weather,
    runway: str,
    opus: OpusFlightContext | None = None,
    initial_climb_ft: int | None = None,
    climb_ft_out: list[int] | None = None,
    channel: str | None = None,
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> str:
    template = effective_takeoff_template(template, mission=mission, state=state)
    name = airport["name"]
    cs = speak_callsign(callsign)
    rwy = speak_runway(runway)
    alt = speak_altimeter(weather.altimeter_inhg or 29.92)
    wind = speak_wind(weather.wind_dir, weather.wind_speed_kt)
    taxi = resolve_taxi_route(airport, runway, opus=opus)
    tower = airport.get("tower") or {"freq_mhz": 327.0}
    departure = airport.get("departure") or {"freq_mhz": 350.0}
    twr_local = speak_local_preset(airport, "tower")
    climb_ft = random_initial_climb_feet(initial_climb_ft)
    climb = speak_altitude_value(str(climb_ft), prefer_fl_below=1000)
    if climb_ft_out is not None and template in ("radar_contact", "center_radar"):
        climb_ft_out.append(climb_ft)

    if template in _RECOVERY_TEMPLATES and isinstance(state, dict):
        # Refresh where the jet is so the recovery/plate can be picked from it.
        ownship_latlon(config, callsign=callsign, opus=opus, state=state)

    if template == "clearance":
        text, used_climb = build_clearance_delivery(
            airport,
            callsign,
            weather,
            runway,
            opus,
            initial_climb_ft=initial_climb_ft,
            channel=channel or "delivery",
        )
        if climb_ft_out is not None:
            climb_ft_out.append(used_climb)
        return text
    if template == "clearance_readback":
        return build_clearance_readback(
            airport, callsign, weather, runway, channel=channel or "delivery"
        )

    # Takeoff: mention VFR Flex west when route/departure match is Flex west
    flex_west = False
    if opus and opus.fp_route_string:
        m = match_departure(airport, opus.fp_route_string)
        flex_west = bool(m.visual_id == "FLEX_WEST" or (m.visual_say or "").casefold() == "flex west")

    ship_note = ""
    if opus and opus.signup_count > 1:
        ship_note = f", {speak_digits(str(int(opus.signup_count)))} ship"

    if template == "taxi":
        eor = speak_place_label(taxi["eor"])
        via = speak_taxi_via(taxi["outbound_via"])
        return (
            f"{cs}, {name} Ground, runway {rwy}, taxi {eor} via {via}, "
            f"{name} altimeter {alt}."
        )
    if template == "monitor_tower":
        if twr_local:
            return with_freq_handoff_closer(
                f"{cs}, {name} Ground, monitor tower, {twr_local}"
            )
        return with_freq_handoff_closer(
            f"{cs}, {name} Ground, monitor tower on "
            f"{speak_freq(float(tower['freq_mhz']))}"
        )
    if template == "hold_short":
        return f"{cs}, {name} Ground, hold short runway {rwy}."
    if template == "contact_tower":
        if twr_local:
            return with_freq_handoff_closer(
                f"{cs}, {name} Ground, contact tower, {twr_local}"
            )
        return with_freq_handoff_closer(
            f"{cs}, {name} Ground, contact tower on "
            f"{speak_freq(float(tower['freq_mhz']))}"
        )
    if template == "exit_runway":
        # 21R → right at Alpha (Alpha North area); 03L → left at Alpha
        return f"{cs}, {name} Tower, exit {speak_place_label(taxi['exit'])}."
    if template == "taxi_in":
        parking = speak_place_label(taxi["parking"])
        via = speak_taxi_via(taxi["inbound_via"])
        return f"{cs}, {name} Ground, taxi to {parking} via {via}."
    if template == "lineup":
        reply = ""
        if isinstance(state, dict):
            reply = str(state.get("rolling_offer_reply") or "").strip().casefold()
        if reply == "deny":
            return (
                f"No worries, {cs}, {name} Tower, runway {rwy}, line up-and wait."
            )
        return f"{cs}, {name} Tower, runway {rwy}, line up-and wait."
    if template == "remain_position":
        # Traffic: hold at EOR / short of runway until further clearance
        return f"{cs}, {name} Tower, remain in position."
    if template == "rolling_accept":
        # Wiki: Tower solicits rolling takeoff; aircrew may accept or refuse
        return f"{cs}, will you accept rolling?"
    if template == "clear_takeoff":
        # Switch to departure in the takeoff clearance (change freq before roll).
        # Unrestricted (when approved) leads: climb unrestricted up to FL…, winds…
        unres = unrestricted_climb_prefix(state, opus)
        reply = ""
        if isinstance(state, dict):
            reply = str(state.get("rolling_offer_reply") or "").strip().casefold()
        dep = takeoff_departure_switch()
        if reply == "accept":
            return with_freq_handoff_closer(
                f"{cs}, {name} Tower, thanks, {wind}, runway {rwy}, "
                f"cleared for takeoff, {dep}"
            )
        if flex_west:
            return with_freq_handoff_closer(
                f"{cs}, {name} Tower, {unres}VFR Flex west, {wind}, runway {rwy}, "
                f"cleared for takeoff, {dep}"
            )
        return with_freq_handoff_closer(
            f"{cs}, {name} Tower, {unres}{wind}, runway {rwy}, "
            f"cleared for takeoff, {dep}"
        )
    if template == "clear_takeoff_rolling":
        # After a rolling offer accept: thanks + winds + runway + clearance.
        # Otherwise same radio wording as a normal takeoff (no "rolling" said).
        unres = unrestricted_climb_prefix(state, opus)
        reply = ""
        if isinstance(state, dict):
            reply = str(state.get("rolling_offer_reply") or "").strip().casefold()
        dep = takeoff_departure_switch()
        if reply == "accept":
            return with_freq_handoff_closer(
                f"{cs}, {name} Tower, thanks, {wind}, runway {rwy}, "
                f"cleared for takeoff, {dep}"
            )
        return with_freq_handoff_closer(
            f"{cs}, {name} Tower, {unres}{wind}, runway {rwy}, "
            f"cleared for takeoff, {dep}"
        )
    if template == "clear_takeoff_intersection":
        unres = unrestricted_climb_prefix(state, opus)
        ix = taxi.get("intersection") or ("Delta" if str(runway).upper().startswith("21") else "Bravo")
        return with_freq_handoff_closer(
            f"{cs}, {name} Tower, {unres}{wind}, runway {rwy} at {ix}, "
            f"cleared for takeoff, {takeoff_departure_switch()}"
        )
    if template == "right_break":
        plan = approach_plan_from_state(state, airport=airport)
        if not plan:
            plan = assign_approach_plan(
                airport,
                weather,
                mission=mission,
                state=state,
                opus=opus,
                force=False,
            )
        return build_tower_check_in(
            airport,
            callsign,
            str(plan.get("runway") or runway),
            plan=plan,
            recovery=str(plan.get("pattern") or "") or None,
        )
    if template == "clear_land":
        return build_clear_land(
            airport,
            callsign,
            weather,
            runway,
            opus=opus,
            step=step,
            mission=mission,
            state=state,
        )
    if template == "go_around":
        return build_go_around(
            airport,
            callsign,
            runway,
            mission=mission,
            state=state,
        )
    if template == "contact_departure":
        # Standalone handoff if not already in takeoff clearance
        return with_freq_handoff_closer(
            f"{cs}, {name} Tower, {takeoff_departure_switch()}"
        )
    if template == "radar_contact":
        return (
            f"{cs}, {name} Departure, radar contact, climb and maintain {climb}."
        )
    if template == "climb_cruise":
        target = cruise_climb_target_ft(opus, mission=mission, state=state)
        if target is None:
            target = filed_altitude_feet(opus.fp_altitude if opus else None)
        if target is None:
            return f"{cs}, {name} Departure, climb as filed."
        if climb_ft_out is not None:
            climb_ft_out.append(int(target))
        spoken = speak_altitude_value(str(target), prefer_fl_below=1000)
        return f"{cs}, {name} Departure, climb and maintain {spoken}."
    if template == "departure_handoff":
        next_ch = resolve_handoff_channel(
            step=step,
            mission=mission,
            from_channel=channel or "departure",
            default="blackjack",
        )
        if step is not None:
            step["handoff_channel"] = next_ch
        return build_departure_handoff(
            airport,
            callsign,
            handoff_channel=next_ch,
            from_channel=channel or "departure",
        )
    if template == "approach_check_in":
        plan = assign_approach_plan(
            airport,
            weather,
            mission=mission,
            state=state,
            opus=opus,
            force=False,
        )
        recovery = normalize_recovery_key(
            plan.get("pattern")
            or (step or {}).get("recovery")
            or (step or {}).get("recovery_type")
        )
        descend_ft: int | None = plan.get("descend_ft")
        speed_kt: int | None = plan.get("speed_kt")
        if step:
            for key in ("descend_ft", "speed_kt"):
                raw = step.get(key)
                if raw is None or str(raw).strip() == "":
                    continue
                try:
                    if key == "descend_ft":
                        descend_ft = int(raw)
                    else:
                        speed_kt = int(raw)
                except (TypeError, ValueError):
                    pass
        rwy_use = str(plan.get("runway") or runway)
        return build_approach_recovery(
            airport,
            callsign,
            weather,
            rwy_use,
            recovery=recovery,
            descend_ft=descend_ft,
            speed_kt=speed_kt,
            plan=plan,
        )
    if template == "cleared_approach":
        # Always go through the assigner so a filed route can refresh the plan.
        plan = assign_approach_plan(
            airport,
            weather,
            mission=mission,
            state=state,
            opus=opus,
            force=False,
        )
        return build_approach_tower_handoff(
            airport,
            callsign,
            plan=plan,
            runway=str(plan.get("runway") or runway),
        )
    if template in ("approach_procedure", "approach_iaf"):
        plan = assign_approach_plan(
            airport,
            weather,
            mission=mission,
            state=state,
            opus=opus,
            force=False,
        )
        if plan.get("pattern") == "instrument" or plan.get("iaf"):
            return build_iaf_clearance(airport, callsign, plan=plan)
        return build_vfr_recovery_clearance(airport, callsign, plan=plan)
    if template == "bj_check_in":
        # Check-in: radar contact (CAOC), OPUS airspace (≤2), VUL, altimeter, tactical.
        alpha_spoken = None
        if config:
            fix = resolve_alpha_bullseye(config, callsign=callsign, opus=opus)
            if fix and fix.get("spoken"):
                alpha_spoken = str(fix["spoken"])
                if step is not None:
                    step["alpha_bullseye"] = {
                        "name": fix.get("name"),
                        "bearing": fix.get("bearing"),
                        "range_nm": fix.get("range_nm"),
                        "display": fix.get("display"),
                        "unit_name": fix.get("unit_name"),
                        "spoken": alpha_spoken,
                    }
        elif step and isinstance(step.get("alpha_bullseye"), dict):
            alpha_spoken = str(step["alpha_bullseye"].get("spoken") or "") or None
        areas = airspace_areas_for_flight(config, opus)
        vul_end = None
        if opus and opus.vul_end:
            vul_end = opus.vul_end
        elif areas and config and opus and opus.flight_id:
            # Schedule rows carry the VUL window when the flight list omitted it.
            for row in airspace_schedule_for_flight(config, opus):
                if row.get("vul_end"):
                    vul_end = str(row.get("vul_end"))
                    break
        if step is not None:
            step["airspace_areas"] = areas
            if vul_end:
                step["vul_end"] = vul_end
        return build_blackjack_check_in(
            callsign,
            alpha_bullseye=alpha_spoken,
            weather=weather,
            areas=areas,
            vul_end=vul_end,
            vul_start=opus.vul_start if opus else None,
        )
    if template == "bj_alpha_check":
        # Standalone if a plan still uses a separate Alpha step
        alpha_spoken = None
        if config:
            fix = resolve_alpha_bullseye(config, callsign=callsign, opus=opus)
            if fix and fix.get("spoken"):
                alpha_spoken = str(fix["spoken"])
        return build_standalone_alpha_check(callsign, alpha_spoken)
    if template == "bj_range_entry":
        return f"{cs}, Blackjack, Alpha approved, cleared hot."
    if template == "bj_range_exit":
        next_ch = resolve_handoff_channel(
            step=step,
            mission=mission,
            from_channel=channel or "blackjack",
            default="approach",
        )
        if step is not None:
            step["handoff_channel"] = next_ch
        # Pre-assign so Blackjack can clear to the exit / recovery fix;
        # Approach issues expect recovery + clearance on check-in.
        plan = assign_approach_plan(
            airport,
            weather,
            mission=mission,
            state=state,
            opus=opus,
            force=False,
        )
        if isinstance(state, dict) and state.get("range_exit_approved"):
            return build_blackjack_approach_handoff(
                airport, callsign, handoff_channel=next_ch
            )
        return build_blackjack_range_exit(
            airport, callsign, handoff_channel=next_ch, plan=plan
        )
    if template == "contact_bandsaw":
        return build_contact_bandsaw(airport, callsign)
    if template == "bandsaw_check_in":
        alpha_spoken = None
        if config:
            fix = resolve_alpha_bullseye(config, callsign=callsign, opus=opus)
            if fix and fix.get("spoken"):
                alpha_spoken = str(fix["spoken"])
                if step is not None:
                    step["alpha_bullseye"] = {
                        "name": fix.get("name"),
                        "bearing": fix.get("bearing"),
                        "range_nm": fix.get("range_nm"),
                        "display": fix.get("display"),
                        "unit_name": fix.get("unit_name"),
                        "spoken": alpha_spoken,
                    }
        elif step and isinstance(step.get("alpha_bullseye"), dict):
            alpha_spoken = str(step["alpha_bullseye"].get("spoken") or "") or None
        return build_bandsaw_check_in(callsign, alpha_bullseye=alpha_spoken)
    if template == "bandsaw_check_out":
        handoff = str(
            (step or {}).get("handoff_channel")
            or "blackjack"
        ).strip().lower() or "blackjack"
        return build_bandsaw_check_out(
            airport, callsign, handoff_channel=handoff
        )
    if template == "ops_check_in":
        return f"{cs}, Ops, go ahead."
    if template == "center_radar":
        return (
            f"{cs}, radar contact, climb and maintain {climb}."
        )
    if template == "center_handoff":
        # Center/Other → next agency (same auto target as departure_handoff)
        next_ch = resolve_handoff_channel(
            step=step,
            mission=mission,
            from_channel=channel or "other",
            default="blackjack",
        )
        return build_departure_handoff(
            airport,
            callsign,
            handoff_channel=next_ch,
            from_channel=channel or "other",
        )
    if template == "radio_check":
        return f"{cs}, {name}, loud and clear."

    raise ValueError(f"Unknown template '{template}'")


def build_phrase(
    airport: dict[str, Any],
    role: str,
    phrase: str,
    callsign: str,
    weather: Weather,
    runway: str,
) -> tuple[str, str, float, str]:
    """Legacy Ground/Tower helper used by atc_ui Test tab."""
    channel = role
    text = build_template_text(airport, phrase, callsign, weather, runway)
    freq, mod, tx_name = channel_radio(airport, channel)
    return text, tx_name, freq, mod


TEMPLATE_CHOICES = [
    ("clearance", "Delivery — Clearance"),
    ("clearance_readback", "Delivery — Readback correct"),
    ("taxi", "Ground — Taxi to EOR"),
    ("monitor_tower", "Ground — Monitor tower"),
    ("hold_short", "Ground — Hold short"),
    ("contact_tower", "Ground — Contact tower"),
    ("taxi_in", "Ground — Taxi in"),
    ("lineup", "Tower — Line up and wait"),
    ("remain_position", "Tower — Remain in position"),
    ("rolling_accept", "Tower — Accept rolling?"),
    ("clear_takeoff", "Tower — Cleared takeoff"),
    ("clear_takeoff_rolling", "Tower — Cleared takeoff (rolling / no LUAW)"),
    ("clear_takeoff_intersection", "Tower — Cleared takeoff (intersection)"),
    ("right_break", "Tower — Check-in (break / continue)"),
    ("clear_land", "Tower — Cleared to land"),
    ("exit_runway", "Tower — Exit runway"),
    ("go_around", "Tower — Go around"),
    ("contact_departure", "Tower — Contact departure"),
    ("radar_contact", "Departure — Radar contact"),
    ("climb_cruise", "Departure — Climb to cruise"),
    ("departure_handoff", "Departure — Handoff (next agency)"),
    ("approach_check_in", "Approach — Recovery check-in"),
    ("approach_procedure", "Approach — Clearance (VFR / IAF)"),
    ("cleared_approach", "Approach — Contact tower"),
    ("bj_check_in", "Blackjack — Check-in / alpha"),
    ("bj_alpha_check", "Blackjack — Alpha check (standalone)"),
    ("bj_range_entry", "Blackjack — Alpha approved (optional)"),
    ("bj_range_exit", "Blackjack — Range exit → Approach"),
    ("contact_bandsaw", "Blackjack — Contact Bandsaw"),
    ("bandsaw_check_in", "Bandsaw — Check-in + alpha"),
    ("bandsaw_check_out", "Bandsaw — Check-out → Blackjack"),
    ("ops_check_in", "Ops — Check-in"),
    ("center_radar", "Other — Center radar contact"),
    ("center_handoff", "Other — Center / handoff"),
    ("radio_check", "Other — Radio check"),
]

CHANNELS = [
    "delivery",
    "ground",
    "tower",
    "departure",
    "approach",
    "blackjack",
    "bandsaw",
    "ops",
    "other",
]


def build_flow_step_phrase(
    airport: dict[str, Any],
    channel: str,
    template: str,
    callsign: str,
    weather: Weather,
    runway: str,
    custom_text: str | None = None,
    opus: OpusFlightContext | None = None,
    step: dict[str, Any] | None = None,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[str, str, float, str]:
    if custom_text and custom_text.strip():
        # Placeholders for custom TTS; clearance fields filled from Opus when available
        cs = speak_callsign(callsign)
        rwy = speak_runway(runway)
        alt = speak_altimeter(weather.altimeter_inhg or 29.92)
        wind = speak_wind(weather.wind_dir, weather.wind_speed_kt)
        dest = speak_icao_or_name(opus.arr_icao if opus else None, airport)
        filed_alt = speak_filed_altitude(opus.fp_altitude if opus else None) or ""
        squawk = speak_squawk(opus.mode3 if opus else None) or ""
        sid = filed_sid(airport, opus.fp_route_string if opus else None)
        via = speak_fix(sid) if sid else ""
        departure = airport.get("departure") or {"freq_mhz": 350.0}
        alpha = ""
        if config and "{alpha_bullseye}" in custom_text:
            fix = resolve_alpha_bullseye(config, callsign=callsign, opus=opus)
            alpha = str((fix or {}).get("spoken") or "")
        text = (
            custom_text.strip()
            .replace("{callsign}", cs)
            .replace("{runway}", rwy)
            .replace("{altimeter}", alt)
            .replace("{wind}", wind)
            .replace("{destination}", dest)
            .replace("{altitude}", filed_alt)
            .replace("{squawk}", squawk)
            .replace("{via}", via)
            .replace("{departure_freq}", speak_freq(float(departure["freq_mhz"])))
            .replace("{route}", (opus.fp_route_string if opus and opus.fp_route_string else ""))
            .replace("{alpha_bullseye}", alpha)
        )
    else:
        if step is not None:
            step = apply_active_recovery_to_step(step, mission, state=state)
        climb_fixed: int | None = None
        tmpl = template or "radio_check"
        if tmpl in CLIMB_SHARED_TEMPLATES:
            climb_fixed = resolve_shared_climb_ft(
                step=step, mission=mission, state=state
            )
        climb_out: list[int] = []
        text = build_template_text(
            airport,
            tmpl,
            callsign,
            weather,
            runway,
            opus=opus,
            initial_climb_ft=climb_fixed,
            climb_ft_out=climb_out,
            channel=channel,
            step=step,
            mission=mission,
            config=config,
            state=state,
        )
        # Sticky shared climb: Delivery clearance ↔ Departure / Center radar
        if climb_out:
            stick_shared_climb_ft(climb_out[0], step=step, mission=mission)
    freq, mod, tx_name = channel_radio(airport, channel)
    return text, tx_name, freq, mod


# Talk speed (ExternalAudio / System.Speech rate -10..10). Google can run hotter;
# Windows voices sound rushed at the Google default.
DEFAULT_TTS_SPEED_GOOGLE = 7
DEFAULT_TTS_SPEED_WINDOWS = 4


def default_tts_speed(provider: str | None = None) -> int:
    """Default talk speed for a TTS provider."""
    if (provider or "windows").strip().casefold() == "google":
        return DEFAULT_TTS_SPEED_GOOGLE
    return DEFAULT_TTS_SPEED_WINDOWS


def tts_speed(config: dict[str, Any] | None = None, speed: float | int | None = None) -> int:
    """ExternalAudio / System.Speech rate: -10..10 (1 = normal)."""
    if speed is None and config is not None:
        speed = tts_speed_for_provider(config)
    try:
        n = int(round(float(speed if speed is not None else DEFAULT_TTS_SPEED_GOOGLE)))
    except (TypeError, ValueError):
        n = DEFAULT_TTS_SPEED_WINDOWS
    return max(-10, min(10, n))


def tts_speed_for_provider(
    config: dict[str, Any] | None = None,
    provider: str | None = None,
) -> int:
    """
    Effective talk speed for a provider.

    Prefers config['tts_speed_by_provider'][provider], then legacy tts_speed,
    then provider default. Legacy shared default of 7 is treated as Google-only
    so Windows configs fall back to 4 until the user sets a Windows speed.
    """
    cfg = config if isinstance(config, dict) else {}
    prov = (provider or tts_provider(cfg)).strip().casefold()
    if prov not in ("google", "windows"):
        prov = "windows"
    by = cfg.get("tts_speed_by_provider")
    if isinstance(by, dict) and by.get(prov) is not None and str(by.get(prov)).strip() != "":
        return tts_speed(speed=by.get(prov))
    if cfg.get("tts_speed") is not None and str(cfg.get("tts_speed")).strip() != "":
        legacy = tts_speed(speed=cfg.get("tts_speed"))
        if prov == "windows" and legacy == DEFAULT_TTS_SPEED_GOOGLE and (
            not isinstance(by, dict) or "windows" not in by
        ):
            return DEFAULT_TTS_SPEED_WINDOWS
        return legacy
    return default_tts_speed(prov)


def set_tts_speed_for_provider(
    config: dict[str, Any],
    speed: float | int | None,
    provider: str | None = None,
) -> int:
    """Persist talk speed for the active (or given) provider and sync tts_speed."""
    prov = (provider or tts_provider(config)).strip().casefold()
    if prov not in ("google", "windows"):
        prov = "windows"
    n = tts_speed(speed=speed if speed is not None else default_tts_speed(prov))
    by = config.get("tts_speed_by_provider")
    if not isinstance(by, dict):
        by = {}
        config["tts_speed_by_provider"] = by
    by[prov] = n
    config["tts_speed"] = n
    return n


def tts_speed_for_step(
    config: dict[str, Any] | None = None,
    step: dict[str, Any] | None = None,
    speed: float | int | None = None,
) -> int:
    """
    Talk speed for a flow step: explicit override, else step['tts_speed'], else global.
    """
    if speed is not None:
        return tts_speed(speed=speed)
    if step is not None and step.get("tts_speed") is not None and str(step.get("tts_speed")).strip() != "":
        return tts_speed(speed=step.get("tts_speed"))
    return tts_speed(config)


def google_speaking_rate(speed: float | int | None = None) -> float:
    """Map ExternalAudio -10..10 speed to Google speakingRate (0.25..4.0)."""
    return max(0.25, min(4.0, 1.0 + tts_speed(speed=speed) * 0.05))


def voice_billing_family(voice_name: str) -> str:
    """Map a Google voice id to a billing family key."""
    low = (voice_name or "").casefold()
    if "chirp" in low:
        return "chirp"
    if "neural2" in low:
        return "neural2"
    if "wavenet" in low:
        return "wavenet"
    if "studio" in low:
        return "studio"
    if "standard" in low:
        return "standard"
    return "other"


def tts_usage_month_key(when: float | None = None) -> str:
    """Calendar month key in local time (YYYY-MM); counter resets when this changes."""
    return time.strftime("%Y-%m", time.localtime(when if when is not None else time.time()))


def load_tts_usage() -> dict[str, Any]:
    """Load local usage file, auto-resetting when the calendar month rolls over."""
    month = tts_usage_month_key()
    empty: dict[str, Any] = {
        "month": month,
        "total_chars": 0,
        "calls": 0,
        "by_family": {},
    }
    if not TTS_USAGE_PATH.is_file():
        return empty
    try:
        data = json.loads(TTS_USAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict) or str(data.get("month") or "") != month:
        return empty
    by_family = data.get("by_family") if isinstance(data.get("by_family"), dict) else {}
    return {
        "month": month,
        "total_chars": int(data.get("total_chars") or 0),
        "calls": int(data.get("calls") or 0),
        "by_family": {str(k): int(v or 0) for k, v in by_family.items()},
    }


def save_tts_usage(data: dict[str, Any]) -> None:
    TTS_USAGE_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def record_google_tts_usage(voice: str, text: str) -> dict[str, Any]:
    """Increment local monthly character counter after a successful Google synth."""
    chars = len(text or "")
    if chars <= 0:
        return load_tts_usage()
    data = load_tts_usage()
    family = voice_billing_family(voice)
    by_family = data.setdefault("by_family", {})
    by_family[family] = int(by_family.get(family) or 0) + chars
    data["total_chars"] = int(data.get("total_chars") or 0) + chars
    data["calls"] = int(data.get("calls") or 0) + 1
    data["month"] = tts_usage_month_key()
    try:
        save_tts_usage(data)
    except OSError as exc:
        print(f"WARNING: could not save TTS usage: {exc}", file=sys.stderr)
    return data


def estimate_tts_overage_usd(family: str, chars: int) -> float:
    free = TTS_FREE_CHARS_PER_MONTH.get(family, 1_000_000)
    over = max(0, int(chars) - free)
    if over <= 0:
        return 0.0
    rate = TTS_USD_PER_MILLION.get(family, 16.0)
    return over / 1_000_000.0 * rate


def tts_usage_summary() -> dict[str, Any]:
    """
    Local monthly usage + free-tier estimator.
    Resets automatically when the calendar month changes (new month key).
    """
    data = load_tts_usage()
    by_family = dict(data.get("by_family") or {})
    families: list[dict[str, Any]] = []
    est_cost = 0.0
    for family in ("chirp", "neural2", "wavenet", "studio", "standard", "other"):
        used = int(by_family.get(family) or 0)
        if used <= 0 and family not in ("chirp", "neural2", "wavenet"):
            continue
        free = TTS_FREE_CHARS_PER_MONTH.get(family, 1_000_000)
        cost = estimate_tts_overage_usd(family, used)
        est_cost += cost
        families.append(
            {
                "family": family,
                "chars": used,
                "free": free,
                "remaining": max(0, free - used),
                "pct": min(100.0, 100.0 * used / free) if free else 0.0,
                "est_usd": cost,
            }
        )
    total = int(data.get("total_chars") or 0)
    return {
        "month": data.get("month") or tts_usage_month_key(),
        "total_chars": total,
        "calls": int(data.get("calls") or 0),
        "families": families,
        "est_usd": round(est_cost, 4),
        "sorties_equiv": round(total / TTS_CHARS_PER_SORTIE_EST, 1)
        if TTS_CHARS_PER_SORTIE_EST
        else 0.0,
        "chars_per_sortie_est": TTS_CHARS_PER_SORTIE_EST,
    }


def format_tts_usage_lines(summary: dict[str, Any] | None = None) -> list[str]:
    """Short human-readable lines for the Setup UI."""
    s = summary or tts_usage_summary()
    lines = [
        f"Google TTS usage - {s['month']} (resets next calendar month)",
        f"Total: {s['total_chars']:,} chars | {s['calls']} calls | ~{s['sorties_equiv']} full sorties",
    ]
    for row in s.get("families") or []:
        if int(row.get("chars") or 0) <= 0 and row["family"] not in ("chirp", "neural2", "wavenet"):
            continue
        mark = " !" if float(row.get("pct") or 0) >= TTS_FREE_TIER_WARN_PCT * 100 else ""
        lines.append(
            f"  {row['family']}: {row['chars']:,} / {row['free']:,} "
            f"({row['pct']:.1f}% free tier){mark} | est ${row['est_usd']:.2f}"
        )
    lines.append(
        f"Estimated bill this month: ${s['est_usd']:.2f} "
        "(local counter; Cloud Billing is authoritative)"
    )
    return lines


def family_usage_pct(family: str) -> float:
    """0.0–1.0+ fraction of this month's free tier used for a billing family."""
    data = load_tts_usage()
    used = int((data.get("by_family") or {}).get(family) or 0)
    free = TTS_FREE_CHARS_PER_MONTH.get(family) or 1
    return used / float(free)


def family_near_free_limit(family: str, threshold: float | None = None) -> bool:
    thr = TTS_FREE_TIER_WARN_PCT if threshold is None else float(threshold)
    return family_usage_pct(family) >= thr


def pick_voice_for_family(family: str, prefer_gender: str | None = None) -> str | None:
    """First curated Google voice in family, preferring gender when possible."""
    pool = [v for v in google_voice_choices() if voice_billing_family(v) == family]
    if not pool:
        return None
    if prefer_gender:
        same = [v for v in pool if voice_gender(v) == prefer_gender]
        if same:
            return same[0]
    return pool[0]


def resolve_voice_under_free_tier(voice: str) -> tuple[str | None, list[str]]:
    """
    Keep synthesis inside free tiers: Chirp -> Neural2 -> WaveNet.
    Returns (voice_or_None, warning messages). None means block Google TTS.
    """
    voice = (voice or "").strip()
    if not voice:
        return None, ["Voice id is empty."]
    msgs: list[str] = []
    family = voice_billing_family(voice)
    gender = voice_gender(voice)

    if family in TTS_FAMILY_FALLBACK_ORDER:
        start = TTS_FAMILY_FALLBACK_ORDER.index(family)
        chain = list(TTS_FAMILY_FALLBACK_ORDER[start:])
    else:
        # Studio / other premium: try current only if under limit, else Neural2 -> WaveNet
        chain = [family, "neural2", "wavenet"]

    for fam in chain:
        if fam not in TTS_FREE_CHARS_PER_MONTH:
            continue
        if family_near_free_limit(fam):
            if fam == family:
                msgs.append(
                    f"WARNING: {fam} free tier at {family_usage_pct(fam) * 100:.0f}% "
                    f"(limit {TTS_FREE_TIER_WARN_PCT * 100:.0f}%) — falling back"
                )
            continue
        if fam == family:
            return voice, msgs
        replacement = pick_voice_for_family(fam, prefer_gender=gender)
        if not replacement:
            continue
        msgs.append(
            f"WARNING: {family} free tier nearly exhausted — auto-switched to {fam} "
            f"voice {replacement} to avoid charges"
        )
        return replacement, msgs

    msgs.append(
        "WARNING: Chirp, Neural2, and WaveNet are all at/above 90% of free tier — "
        "blocking Google TTS to avoid charges (use Windows voices)"
    )
    return None, msgs


def apply_free_tier_guard_to_config(config: dict[str, Any]) -> list[str]:
    """
    Remap config agency voices (and provider if needed) away from nearly-exhausted
    free tiers. Mutates config in place. Returns warning strings.
    """
    if tts_provider(config) != "google":
        return []
    msgs: list[str] = []
    voices = config.get("tts_voices")
    if not isinstance(voices, dict):
        voices = {}
    new_voices: dict[str, str] = {}
    switched_provider = False
    for ch, raw in voices.items():
        voice = str(raw or "").strip()
        if not voice:
            continue
        safe, notes = resolve_voice_under_free_tier(voice)
        for note in notes:
            if note not in msgs:
                msgs.append(note)
        if safe is None:
            config["tts_provider"] = "windows"
            switched_provider = True
            msgs.append(
                "Switched TTS engine to Windows to avoid Google overage charges. "
                "Save setup to keep this."
            )
            break
        new_voices[str(ch)] = safe
    if switched_provider:
        return msgs
    if new_voices:
        config["tts_voices"] = new_voices
        default_voice = (
            new_voices.get("default")
            or next(iter(new_voices.values()), "")
            or str(config.get("tts_voice") or "")
        )
        if default_voice:
            safe_default, notes = resolve_voice_under_free_tier(default_voice)
            for note in notes:
                if note not in msgs:
                    msgs.append(note)
            if safe_default:
                config["tts_voice"] = safe_default
                config["tts_gender"] = voice_gender(safe_default)
            else:
                config["tts_provider"] = "windows"
                msgs.append(
                    "Switched TTS engine to Windows to avoid Google overage charges. "
                    "Save setup to keep this."
                )
    return msgs


def free_tier_warning_lines(summary: dict[str, Any] | None = None) -> list[str]:
    """Red-banner lines when any primary family is at/above the warn threshold."""
    s = summary or tts_usage_summary()
    lines: list[str] = []
    for row in s.get("families") or []:
        fam = str(row.get("family") or "")
        if fam not in TTS_FAMILY_FALLBACK_ORDER:
            continue
        pct = float(row.get("pct") or 0.0)
        if pct >= TTS_FREE_TIER_WARN_PCT * 100:
            nxt = {
                "chirp": "Neural2",
                "neural2": "WaveNet",
                "wavenet": "Windows (free)",
            }.get(fam, "next tier")
            lines.append(
                f"{fam.upper()} at {pct:.0f}% of free tier — auto-fallback to {nxt} "
                f"to avoid charges"
            )
    return lines


# Controlled Google SSML pauses. Chirp over-pauses when break tags stack with its
# own phrasing, so Chirp uses plain punctuation instead. Neural2/WaveNet need
# real gaps — the old 25/70ms values read as one run-on sentence.
_RADIO_BREAK_COMMA_MS = 180
_RADIO_BREAK_PERIOD_MS = 420
_RADIO_BREAK_END_MS = 220
_RADIO_WAV_PAD_MS = 160
_RADIO_DIGIT_GAP_MS = 40
_RADIO_DIGIT_POINT_GAP_MS = 55


def _is_chirp_voice(voice: str | None) -> bool:
    return bool(voice and "chirp" in voice.casefold())


def _tts_pause_profile(voice: str | None = None) -> dict[str, Any]:
    """Per-engine pause timings (ms). Digits use plain spaces (no break tags)."""
    if _is_chirp_voice(voice):
        # Chirp gets plain-text synthesis (no SSML breaks); profile unused there.
        return {
            "comma_ms": 0,
            "period_ms": 0,
            "end_ms": 0,
            "digit_gap_ms": 0,
            "digit_point_gap_ms": 0,
        }
    return {
        "comma_ms": _RADIO_BREAK_COMMA_MS,
        "period_ms": _RADIO_BREAK_PERIOD_MS,
        "end_ms": _RADIO_BREAK_END_MS,
        "digit_gap_ms": 0,
        "digit_point_gap_ms": 0,
    }

# Short-lived caches — Hear/Preview/TX used to re-hit Opus for every flight signup.
_OPUS_CACHE_TTL_SEC = 45.0
_METAR_CACHE_TTL_SEC = 60.0
_TTS_WAV_CACHE_TTL_SEC = 180.0
_OPUS_CACHE: dict[str, Any] = {"key": "", "exp": 0.0, "ctx": None}
_METAR_CACHE: dict[str, Any] = {}  # key -> {"exp": float, "wx": Weather}
_TTS_WAV_CACHE: dict[str, Any] = {"key": "", "exp": 0.0, "path": None}


def _expand_bare_digit_runs(text: str) -> str:
    """Turn leftover Arabic numerals into spoken digit words (e.g. '21' → 'two one')."""
    if not text:
        return text
    return re.sub(r"\d+", lambda m: speak_digits(m.group(0)), text)


def _canonicalize_radio_digit_words(text: str) -> str:
    """Force ICAO digit speech: zero/niner/fife/tree (not oh/nine/five/three)."""
    if not text:
        return text
    out: list[str] = []
    for w in text.split():
        canon = _RADIO_DIGIT_CANONICAL.get(w.casefold())
        out.append(canon if canon is not None else w)
    return " ".join(out)


def _clean_radio_clause(part: str, voice: str | None = None) -> str:
    p = (part or "").strip()
    if not p:
        return ""
    p = re.sub(r"[\"'`]+", "", p)
    p = re.sub(r"\s+-\s+", " ", p)
    # Compass / EOR expansions for every engine (not Chirp-only).
    p = expand_radio_place_tokens(p)
    p = apply_radio_pronunciations(p)
    p = _expand_bare_digit_runs(p)
    p = _canonicalize_radio_digit_words(p)
    p = re.sub(r"\s+", " ", p).strip()
    if not p:
        return ""
    if _is_chirp_voice(voice):
        # Chirp treats capitals as new prosody units
        p = p.casefold()
    return p


def _radio_tts_segments(text: str, voice: str | None = None) -> list[tuple[str, str | None]]:
    """
    Split a radio phrase into (clause, pause_after) segments.

    pause_after: "comma" (brief), "period" (sentence), or None (end).
    Punctuation is removed from clause text; SSML re-inserts timed breaks.
    """
    s = (text or "").strip()
    if not s:
        return []
    s = s.replace("—", ".").replace("–", ".").replace("…", ".")
    # Keep punctuation tokens so periods are pause points (not swallowed).
    bits = re.split(r"([,;:.?!]+)", s)
    segments: list[tuple[str, str | None]] = []
    buf = ""
    for bit in bits:
        if not bit:
            continue
        punct = bit.strip()
        if re.fullmatch(r"[,;:]+", punct):
            clause = _clean_radio_clause(buf, voice)
            buf = ""
            if clause:
                segments.append((clause, "comma"))
            continue
        if re.fullmatch(r"[.?!]+", punct):
            clause = _clean_radio_clause(buf, voice)
            buf = ""
            if clause:
                segments.append((clause, "period"))
            continue
        buf += bit
    clause = _clean_radio_clause(buf, voice)
    if clause:
        segments.append((clause, None))
    return segments


def _radio_tts_clauses(text: str, voice: str | None = None) -> list[str]:
    return [clause for clause, _pause in _radio_tts_segments(text, voice=voice)]


def prepare_radio_tts_text(text: str, voice: str | None = None) -> str:
    """
    Plain spoken string (Windows TTS / logging).

    Keeps commas/periods so non-Chirp engines get sentence boundaries — joining
    clauses with spaces alone made Blackjack/Bandsaw sound like one run-on line.
    """
    if _is_chirp_voice(voice):
        return prepare_radio_tts_chirp_text(text, voice=voice).replace(
            _RADIO_DIGIT_NBSP, " "
        )
    spoken = spoken_radio_preview(text, voice=voice)
    # ExternalAudio + local preview use System.Speech Speak() (no IPA API).
    if not (voice and is_google_voice_name(voice)):
        spoken = apply_windows_radio_pronunciations(spoken)
    return spoken


def _is_radio_digit_token(word: str) -> bool:
    return word.casefold() in _RADIO_DIGIT_TOKENS


def _is_radio_digit_glue_token(word: str) -> bool:
    low = word.casefold()
    return low in _RADIO_DIGIT_TOKENS or low in _RADIO_DIGIT_GLUE_MID


def _normalize_digit_run_words(run: list[str]) -> list[str]:
    """ICAO forms for a glued digit/point run."""
    out: list[str] = []
    for w in run:
        canon = _RADIO_DIGIT_CANONICAL.get(w.casefold())
        out.append(canon if canon is not None else w.casefold())
    return out


def _digit_run_to_ssml(
    run: list[str],
    *,
    profile: dict[str, Any] | None = None,
    voice: str | None = None,
) -> str:
    """
    ICAO digit words as a normal phrase ("zero four one one").

    No per-digit <break> tags (those made numbers choppy). Avoid <s> wrappers —
    Chirp treats them as hard sentence boundaries.
    """
    words = _normalize_digit_run_words(run)
    if not words:
        return ""
    prof = profile or _tts_pause_profile(voice)
    digit_gap = int(prof.get("digit_gap_ms") or 0)
    point_gap = int(prof.get("digit_point_gap_ms") or digit_gap)
    if digit_gap <= 0:
        return " ".join(html.escape(w) for w in words)
    parts: list[str] = []
    for i, w in enumerate(words):
        if i:
            prev = words[i - 1]
            if w in _RADIO_DIGIT_GLUE_MID or prev in _RADIO_DIGIT_GLUE_MID:
                gap = point_gap
            else:
                gap = digit_gap
            parts.append(f'<break time="{gap}ms"/>')
        parts.append(html.escape(w))
    return " ".join(parts)


def _collect_digit_run(words: list[str], start: int) -> tuple[list[str], int]:
    """
    Collect a digit/point run and optional runway-side / unit suffix so Chirp
    does not breathe between 'two one' and 'right'.
    """
    run = [words[start]]
    j = start + 1
    while j < len(words) and _is_radio_digit_glue_token(words[j]):
        if words[j].casefold() in _RADIO_DIGIT_GLUE_MID:
            if j + 1 >= len(words) or not _is_radio_digit_token(words[j + 1]):
                break
        run.append(words[j])
        j += 1
    # Alpha ranges: "three zero fife twenty five"
    if j < len(words) and words[j].casefold() in _RADIO_NATURAL_NUMBER_WORDS:
        run.append(words[j])
        j += 1
        if j < len(words) and _is_radio_digit_token(words[j]):
            run.append(words[j])
            j += 1
    if j < len(words) and words[j].casefold() in _RADIO_DIGIT_GLUE_SUFFIX:
        run.append(words[j])
        j += 1
    return run, j


def _collect_natural_quantity(words: list[str], start: int) -> tuple[list[str], int]:
    """
    Whole quantities for picture range/altitude: 'one hundred fourteen',
    'twenty five thousand', 'thirty one thousand'.
    """
    run = [words[start]]
    j = start + 1
    low0 = words[start].casefold()
    # twenty/thirty + optional ones digit
    if low0 in _RADIO_NATURAL_NUMBER_WORDS and j < len(words) and _is_radio_digit_token(
        words[j]
    ):
        run.append(words[j])
        j += 1
    # one/two/… hundred [remainder]
    if j < len(words) and words[j].casefold() == "hundred":
        run.append(words[j])
        j += 1
        if j < len(words) and words[j].casefold() in _RADIO_NATURAL_NUMBER_WORDS:
            run.append(words[j])
            j += 1
            if j < len(words) and _is_radio_digit_token(words[j]):
                run.append(words[j])
                j += 1
        elif j < len(words) and _is_radio_digit_token(words[j]):
            run.append(words[j])
            j += 1
    if j < len(words) and words[j].casefold() == "thousand":
        run.append(words[j])
        j += 1
    return run, j


def _normalize_glued_phrase_words(run: list[str]) -> list[str]:
    """ICAO digit forms; leave prefix/suffix words lowercased as-is."""
    out: list[str] = []
    for w in run:
        canon = _RADIO_DIGIT_CANONICAL.get(w.casefold())
        out.append(canon if canon is not None else w.casefold())
    return out


def _iter_glued_runs(clause: str) -> list[tuple[str, list[str]]]:
    """
    Tokenize a clause into ('word', [w]) or ('run', [tokens...]) pieces.
    Prefix words like 'runway' are absorbed into the following digit run.
    Bullseye labels (elvis / desert) stick to the bearing+range that follows.
    """
    words = (clause or "").split()
    if not words:
        return []
    pieces: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(words):
        if (
            words[i].casefold() in _RADIO_DIGIT_GLUE_PREFIX
            and i + 1 < len(words)
            and _is_radio_digit_token(words[i + 1])
        ):
            run = [words[i]]
            rest, j = _collect_digit_run(words, i + 1)
            run.extend(rest)
            pieces.append(("run", run))
            i = j
            continue
        # Picture range / altitude quantities before ICAO digit runs.
        nxt = words[i + 1].casefold() if i + 1 < len(words) else ""
        if words[i].casefold() in _RADIO_NATURAL_NUMBER_WORDS or (
            _is_radio_digit_token(words[i]) and nxt in {"hundred", "thousand"}
        ):
            run, j = _collect_natural_quantity(words, i)
            if len(run) >= 2:
                pieces.append(("run", run))
                i = j
                continue
        if _is_radio_digit_token(words[i]):
            run, j = _collect_digit_run(words, i)
            # Pull a preceding bullseye/fix name into the run so Chirp does not
            # list-pause between "elvis" and "three zero fife…".
            if pieces and pieces[-1][0] == "word":
                prev = pieces[-1][1][0]
                prev_l = prev.casefold()
                if (
                    prev_l not in _RADIO_DIGIT_GLUE_PREFIX
                    and prev_l not in _RADIO_DIGIT_TOKENS
                    and prev_l not in {"a", "an", "the", "and", "or", "to", "of"}
                    and len(prev_l) <= 16
                ):
                    run = [prev] + run
                    pieces.pop()
            pieces.append(("run" if len(run) >= 2 else "word", run))
            i = j
            continue
        pieces.append(("word", [words[i]]))
        i += 1
    return pieces


def _glue_digit_runs_ssml(
    clause: str,
    *,
    profile: dict[str, Any] | None = None,
    voice: str | None = None,
) -> str:
    """Join consecutive radio digits with a readable ICAO cadence."""
    out: list[str] = []
    for kind, run in _iter_glued_runs(clause):
        if kind == "run":
            out.append(_digit_run_to_ssml(run, profile=profile, voice=voice))
        else:
            # Preserve original casing for Neural2/WaveNet non-digit words.
            out.append(html.escape(run[0]))
    return " ".join(out)


def _glue_digit_runs_plain(clause: str) -> str:
    """
    Join digit/ATC number phrases with NBSP so Chirp does not list-pause mid-run.
    Ordinary words stay space-separated (natural phrasing).
    """
    out: list[str] = []
    for kind, run in _iter_glued_runs(clause):
        norm = _normalize_glued_phrase_words(run)
        if kind == "run":
            out.append(_RADIO_DIGIT_NBSP.join(norm))
        else:
            out.append(norm[0])
    return " ".join(out)


def prepare_radio_tts_chirp_text(text: str, voice: str | None = None) -> str:
    """
    Continuous plain text for Chirp 3: HD.

    Chirp stacks its own breaths on SSML <break>/<s>, so we keep real commas /
    periods as characters and glue digit runs with NBSP — closer to Neural flow.
    """
    segments = _radio_tts_segments(text, voice=voice)
    if not segments:
        return ""
    out: list[str] = []
    for clause, pause in segments:
        out.append(_glue_digit_runs_plain(clause))
        if pause == "comma":
            out.append(",")
        elif pause == "period":
            out.append(".")
    s = " ".join(out)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r"\s+\.", ".", s)
    return s


def prepare_radio_tts_ssml(text: str, voice: str | None = None) -> str:
    """
    Google TTS SSML: short breaks at commas/periods; ICAO digit words with spaces.

    Chirp should use prepare_radio_tts_chirp_text + plain-text synthesize instead.
    """
    if _is_chirp_voice(voice):
        # Minimal speak wrapper if a caller still requests SSML for Chirp.
        body = html.escape(prepare_radio_tts_chirp_text(text, voice=voice))
        return f"<speak>{body}</speak>"
    profile = _tts_pause_profile(voice)
    segments = _radio_tts_segments(text, voice=voice)
    if not segments:
        return "<speak></speak>"
    parts: list[str] = []
    last_pause: str | None = None
    for clause, pause in segments:
        parts.append(_glue_digit_runs_ssml(clause, profile=profile, voice=voice))
        last_pause = pause
        if pause == "comma":
            # Keep the comma on the clause so Neural2 gets a phrase boundary,
            # then a timed break (old 25ms breaks read as one run-on sentence).
            parts[-1] = parts[-1] + ","
            ms = int(profile.get("comma_ms") or 0)
            if ms > 0:
                parts.append(f'<break time="{ms}ms"/>')
        elif pause == "period":
            parts[-1] = parts[-1] + "."
            ms = int(profile.get("period_ms") or 0)
            if ms > 0:
                parts.append(f'<break time="{ms}ms"/>')
    # Tail silence for TX clip — skip stacking a full second hang after a period.
    end_ms = int(profile.get("end_ms") or 0)
    if end_ms > 0 and last_pause != "period":
        parts.append(f'<break time="{end_ms}ms"/>')
    elif end_ms > 0 and last_pause == "period":
        period_ms = int(profile.get("period_ms") or 0)
        extra = max(0, end_ms - period_ms)
        if extra >= 20:
            parts.append(f'<break time="{extra}ms"/>')
    return "<speak>" + " ".join(parts) + "</speak>"


def pad_wav_silence(path: Path | str, ms: int = _RADIO_WAV_PAD_MS) -> None:
    """Append silence to a WAV so radio TX does not clip the last syllable."""
    if ms <= 0:
        return
    p = Path(path)
    with wave.open(str(p), "rb") as src:
        params = src.getparams()
        frames = src.readframes(src.getnframes())
    n_pad = max(1, int(params.framerate * (ms / 1000.0)))
    silence = b"\x00" * (n_pad * params.nchannels * params.sampwidth)
    with wave.open(str(p), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames + silence)


def spoken_radio_preview(text: str, voice: str | None = None) -> str:
    """
    Human-readable form of what Google TTS is driven with.

    Chirp: continuous plain text (literal commas/periods, NBSP digit glue).
    Neural2/WaveNet: commas/periods shown for readability; API uses SSML breaks.
    """
    if _is_chirp_voice(voice):
        # Show normal spaces in the UI even though API uses NBSP between digits.
        return prepare_radio_tts_chirp_text(text, voice=voice).replace(
            _RADIO_DIGIT_NBSP, " "
        )
    segments = _radio_tts_segments(text, voice=voice)
    if not segments:
        return ""
    out: list[str] = []
    for clause, pause in segments:
        out.append(clause)
        if pause == "comma":
            out.append(",")
        elif pause == "period":
            out.append(".")
    # "word , word" -> "word, word"
    s = " ".join(out)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r"\s+\.", ".", s)
    return s


def spoken_radio_footer(text: str, voice: str | None = None) -> str:
    """Preview footer explaining Spoken line vs readable phrase."""
    spoken = spoken_radio_preview(text, voice=voice)
    if not spoken:
        return ""
    if _is_chirp_voice(voice):
        note = "Chirp: plain text (no SSML breaks) · commas kept · digit runs glued · lowercased"
    elif voice and is_google_voice_name(voice):
        note = "Google pauses at commas · ICAO digits as normal words"
    else:
        note = "Windows TTS: punctuation stripped (no timed pauses)"
        spoken = prepare_radio_tts_text(text, voice=voice)
    return f"Spoken / Hear / SRS: {spoken}\n({note})"


_GOOGLE_TOKEN_CACHE: dict[str, Any] = {"path": "", "token": "", "exp": 0.0}

VOICE_PREVIEW_SAMPLE = "Nellis Delivery, radio check."


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_rs256_powershell(private_key_pem: str, signing_input: bytes) -> bytes:
    """RSA-SHA256 via Windows CNG (no extra Python packages)."""
    if os.name != "nt":
        raise RuntimeError("Google TTS local preview requires Windows.")
    pem_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".pem", delete=False, encoding="utf-8", newline="\n"
        ) as fh:
            fh.write(private_key_pem.strip() + "\n")
            pem_path = fh.name
        data_b64 = base64.b64encode(signing_input).decode("ascii")
        ps = r"""
$ErrorActionPreference = 'Stop'
$pem = Get-Content -LiteralPath $env:ATC_JWT_PEM -Raw
$pemBody = ($pem -replace '-----BEGIN PRIVATE KEY-----','') -replace '-----END PRIVATE KEY-----',''
$pemBody = $pemBody -replace '\s',''
$keyBytes = [Convert]::FromBase64String($pemBody)
$data = [Convert]::FromBase64String($env:ATC_JWT_DATA)
$cng = [System.Security.Cryptography.CngKey]::Import(
    $keyBytes,
    [System.Security.Cryptography.CngKeyBlobFormat]::Pkcs8PrivateBlob
)
try {
    $rsa = New-Object System.Security.Cryptography.RSACng $cng
    $sig = $rsa.SignData(
        $data,
        [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
    )
    [Convert]::ToBase64String($sig)
} finally {
    $cng.Dispose()
}
"""
        env = os.environ.copy()
        env["ATC_JWT_PEM"] = pem_path
        env["ATC_JWT_DATA"] = data_b64
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            raise RuntimeError(f"JWT signing failed: {err}")
        out = (proc.stdout or "").strip().splitlines()
        if not out:
            raise RuntimeError("JWT signing produced no signature.")
        return base64.b64decode(out[-1].strip())
    finally:
        if pem_path:
            try:
                os.unlink(pem_path)
            except OSError:
                pass


def google_access_token(creds_path: Path) -> str:
    """OAuth2 access token for a Google service-account JSON (cached ~50 min)."""
    path = Path(creds_path)
    now = time.time()
    if (
        _GOOGLE_TOKEN_CACHE.get("path") == str(path)
        and _GOOGLE_TOKEN_CACHE.get("token")
        and float(_GOOGLE_TOKEN_CACHE.get("exp") or 0) > now + 60
    ):
        return str(_GOOGLE_TOKEN_CACHE["token"])

    try:
        sa = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"Cannot read Google credentials: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid Google credentials JSON: {path}") from exc

    email = str(sa.get("client_email") or "").strip()
    private_key = str(sa.get("private_key") or "").strip()
    if not email or not private_key:
        raise RuntimeError("Google credentials JSON missing client_email or private_key.")

    iat = int(now)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claim = _b64url(
        json.dumps(
            {
                "iss": email,
                "scope": "https://www.googleapis.com/auth/cloud-platform",
                "aud": "https://oauth2.googleapis.com/token",
                "iat": iat,
                "exp": iat + 3600,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claim}".encode("ascii")
    sig = _sign_rs256_powershell(private_key, signing_input)
    assertion = f"{header}.{claim}.{_b64url(sig)}"

    body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
    ).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"Google OAuth token failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Google OAuth token network error: {exc.reason}") from exc

    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Google OAuth response missing access_token.")
    expires_in = int(payload.get("expires_in") or 3600)
    _GOOGLE_TOKEN_CACHE["path"] = str(path)
    _GOOGLE_TOKEN_CACHE["token"] = token
    _GOOGLE_TOKEN_CACHE["exp"] = now + expires_in
    return token


def synthesize_google_tts(
    creds_path: Path | str,
    voice: str,
    text: str,
    speed: float | int | None = None,
) -> Path:
    """Synthesize text with Google Cloud TTS to a temp LINEAR16 WAV path."""
    voice = (voice or "").strip()
    if not voice:
        raise ValueError("Google voice id is empty.")
    voice, retired_note = resolve_retired_google_voice(voice)
    if retired_note:
        print(retired_note, file=sys.stderr)
    safe_voice, guard_notes = resolve_voice_under_free_tier(voice)
    for note in guard_notes:
        print(note, file=sys.stderr)
    if safe_voice is None:
        raise RuntimeError(
            "Google TTS blocked: free tiers for Chirp/Neural2/WaveNet are at or above "
            f"{TTS_FREE_TIER_WARN_PCT * 100:.0f}%. Switch to Windows voices to avoid charges."
        )
    voice = safe_voice
    # Chirp: plain text (smoother). Neural2/WaveNet: timed SSML breaks.
    # Both keep the spelling "wind" and force /wɪnd/ via customPronunciations.
    use_chirp_plain = _is_chirp_voice(voice)
    if use_chirp_plain:
        synth_text = prepare_radio_tts_chirp_text(text, voice=voice)
        plain = synth_text.replace(_RADIO_DIGIT_NBSP, " ")
        ssml = ""
        cache_payload = f"text\0{synth_text}"
    else:
        synth_text = ""
        plain = prepare_radio_tts_text(text, voice=voice)
        ssml = prepare_radio_tts_ssml(text, voice=voice)
        cache_payload = f"ssml\0{ssml}"
    if not plain:
        raise ValueError("Preview text is empty.")
    pronunciations = google_custom_pronunciations(plain if use_chirp_plain else ssml)
    if pronunciations:
        cache_payload = f"{cache_payload}\0pron\0{json.dumps(pronunciations, sort_keys=True)}"
    path = Path(creds_path)
    if not path.is_file():
        raise FileNotFoundError(f"Google credentials file not found: {path}")

    rate = google_speaking_rate(speed)
    cache_key = hashlib.sha1(
        f"{voice}\0{rate:.3f}\0{cache_payload}".encode("utf-8")
    ).hexdigest()
    now = time.time()
    cached = _TTS_WAV_CACHE.get("path")
    if (
        _TTS_WAV_CACHE.get("key") == cache_key
        and float(_TTS_WAV_CACHE.get("exp") or 0) > now
        and cached
        and Path(str(cached)).is_file()
    ):
        fd, tmp = tempfile.mkstemp(prefix="atc_gtts_", suffix=".wav")
        os.close(fd)
        out = Path(tmp)
        shutil.copy2(str(cached), out)
        print("Google TTS: cache hit (skipped API synthesize)")
        return out

    token = google_access_token(path)
    locale = voice_locale(voice) or "en-US"
    audio_cfg: dict[str, Any] = {
        "audioEncoding": "LINEAR16",
        "sampleRateHertz": 24000,
        "speakingRate": rate,
        "effectsProfileId": ["telephony-class-application"],
    }
    # Chirp 3: HD rejects pitch; Neural2/WaveNet use a slightly flatter pitch
    if not use_chirp_plain:
        audio_cfg["pitch"] = -1.0
    tts_input: dict[str, Any] = (
        {"text": synth_text} if use_chirp_plain else {"ssml": ssml}
    )
    if pronunciations:
        tts_input["customPronunciations"] = pronunciations
    payload = {
        "input": tts_input,
        "voice": {"languageCode": locale, "name": voice},
        "audioConfig": audio_cfg,
    }
    req = urllib.request.Request(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Google TTS synthesize failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Google TTS network error: {exc.reason}") from exc

    audio_b64 = str(result.get("audioContent") or "").strip()
    if not audio_b64:
        raise RuntimeError("Google TTS response missing audioContent.")
    audio = base64.b64decode(audio_b64)
    fd, tmp = tempfile.mkstemp(prefix="atc_gtts_", suffix=".wav")
    os.close(fd)
    out = Path(tmp)
    out.write_bytes(audio)
    try:
        pad_wav_silence(out, _RADIO_WAV_PAD_MS)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not pad TTS WAV tail silence: {exc}", file=sys.stderr)

    try:
        cache_dir = Path(tempfile.gettempdir()) / "atc_gtts_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        durable = cache_dir / f"{cache_key}.wav"
        shutil.copy2(out, durable)
        old = _TTS_WAV_CACHE.get("path")
        _TTS_WAV_CACHE["key"] = cache_key
        _TTS_WAV_CACHE["exp"] = now + _TTS_WAV_CACHE_TTL_SEC
        _TTS_WAV_CACHE["path"] = str(durable)
        if old and old != str(durable):
            try:
                Path(str(old)).unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as exc:
        print(f"WARNING: could not cache TTS WAV: {exc}", file=sys.stderr)

    record_google_tts_usage(voice, plain)
    return out


def preview_google_voice_local(
    creds_path: Path | str,
    voice: str,
    text: str,
    volume: float = 0.8,
    speed: float | int | None = None,
) -> None:
    """Synthesize with Google Cloud TTS and play on local Windows speakers."""
    if os.name != "nt":
        raise RuntimeError("Google TTS local preview requires Windows.")
    import winsound  # Windows stdlib

    wav_path = synthesize_google_tts(creds_path, voice, text, speed=speed)
    try:
        # winsound has no volume API; System.Speech path honors volume separately.
        _ = volume
        winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass


def preview_voice_local(
    voice: str,
    text: str,
    volume: float = 0.8,
    speed: float | int | None = None,
    google_credentials: str | Path | None = None,
) -> None:
    """Speak TTS on local Windows speakers (does not go to SRS)."""
    voice = (voice or "").strip()
    if not (text or "").strip():
        return
    creds: Path | None = None
    if google_credentials:
        creds = Path(google_credentials)
    if creds is not None or is_google_voice_name(voice):
        if creds is None or not creds.is_file():
            raise RuntimeError(
                "Google voice preview needs a valid google_credentials JSON path in Setup."
            )
        # Pass original phrase so synthesize can place SSML breaks on commas
        preview_google_voice_local(creds, voice, text, volume=volume, speed=speed)
        return

    spoken = prepare_radio_tts_text(text, voice=voice)
    if not spoken:
        return
    # Escape for PowerShell single-quoted string
    safe_voice = voice.replace("'", "''")
    safe_text = spoken.replace("'", "''").replace("\r", " ").replace("\n", " ")
    vol = max(0, min(100, int(float(volume) * 100)))
    rate = tts_speed(speed=speed)
    ps = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('{safe_voice}')
$s.Volume = {vol}
$s.Rate = {rate}
$s.Speak('{safe_text}')
"""
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", ps],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )


def preview_file_local(file_path: str) -> None:
    """Open/play an audio file with the default Windows player."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def terminate_stale_external_audio(exe: Path) -> int:
    """Kill hung ExternalAudio processes for this exe (prevents ghost SRS clients)."""
    if os.name != "nt":
        return 0
    exe_path = str(exe.resolve())
    ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
$n = 0
Get-CimInstance Win32_Process -Filter "Name='{exe.name}'" | ForEach-Object {{
  if ($_.ExecutablePath -and ($_.ExecutablePath -ieq '{exe_path.replace("'", "''")}')) {{
    Stop-Process -Id $_.ProcessId -Force
    $n++
  }}
}}
Write-Output $n
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
            timeout=15,
        )
        out = (proc.stdout or "").strip().splitlines()
        killed = int(out[-1]) if out else 0
        if killed:
            print(f"Cleared {killed} stale ExternalAudio process(es)", file=sys.stderr)
        return killed
    except (ValueError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"Stale ExternalAudio cleanup skipped: {exc}", file=sys.stderr)
        return 0


def _run_external_audio(exe: Path, cmd: list[str], *, timeout_sec: float = 120.0) -> int:
    """Launch ExternalAudio with timeout; kill process group on hang."""
    terminate_stale_external_audio(exe)
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(exe.parent),
            creationflags=creationflags,
            timeout=timeout_sec,
        )
        return int(proc.returncode)
    except subprocess.TimeoutExpired:
        print(
            f"ERROR: ExternalAudio timed out after {timeout_sec:.0f}s — killing hung process.",
            file=sys.stderr,
        )
        terminate_stale_external_audio(exe)
        return 124


def transmit(
    config: dict[str, Any],
    airport: dict[str, Any],
    text: str,
    tx_name: str,
    freq: float,
    mod: str,
    channel: str | None = None,
    voice_override: str | None = None,
    step: dict[str, Any] | None = None,
    speed_override: float | int | None = None,
) -> int:
    import srs_radio  # local — EAM common-PTT overrides TX freq

    exe = resolve_external_audio_exe(config)
    if not exe.is_file():
        print(f"ExternalAudio not found: {exe}", file=sys.stderr)
        return 2

    # EAM strip: common PTT → only the selected radio's MHz goes on the wire.
    freq, mod = srs_radio.maybe_force_eam_tx_freq(config, freq, mod)

    provider = tts_provider(config)
    google_creds = google_credentials_path(config)
    if voice_override and str(voice_override).strip():
        voice = str(voice_override).strip()
        gender = voice_gender(voice)
    else:
        voice, gender = voice_for_channel(config, channel)
    speed = tts_speed_for_step(config, step=step, speed=speed_override)

    # Google: synthesize locally (same path as Hear locally), then TX as WAV.
    # Avoids ExternalAudio Google/gRPC hangs that left ghost SRS clients.
    if provider == "google":
        if google_creds is None:
            print(
                "ERROR: tts_provider=google but google_credentials is empty. "
                "Set the path to your Google service-account JSON in Setup.",
                file=sys.stderr,
            )
            return 2
        if not google_creds.is_file():
            print(
                f"ERROR: Google credentials file not found: {google_creds}",
                file=sys.stderr,
            )
            return 2
        print("TX:", text)
        print("Spoken:", spoken_radio_preview(text, voice=voice))
        print(f"TTS: google local-synth -> WAV ({voice}, speed {speed}) -> ExternalAudio --file")
        if config.get("dry_run"):
            print("dry_run=true; not launching ExternalAudio")
            return 0
        wav_path: Path | None = None
        try:
            wav_path = synthesize_google_tts(
                google_creds, voice, text, speed=speed
            )
            return transmit_file(config, airport, str(wav_path), tx_name, freq, mod)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: Google local synth/TX failed: {exc}", file=sys.stderr)
            return 2
        finally:
            if wav_path is not None:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    pass

    spoken = prepare_radio_tts_text(text, voice=voice)
    cmd = [
        str(exe),
        f"--text={spoken}",
        f"--freqs={freq}",
        f"--modulations={mod}",
        f"--coalition={int(airport['coalition'])}",
        f"--ip={airport['srs_host']}",
        f"--port={int(airport['srs_port'])}",
        f"--name={tx_name}",
        f"--volume={float(config.get('tts_volume', 0.8))}",
        f"--speed={speed}",
        "--minimise",
    ]
    if voice:
        cmd.append(f"--voice={voice}")
    if gender:
        cmd.append(f"--gender={gender}")

    print("TX:", text)
    if spoken != text.strip():
        print("TTS text (flattened):", spoken)
    print(f"TTS: {provider} · speed {speed}")
    print("CMD:", " ".join(cmd))

    if config.get("dry_run"):
        print("dry_run=true; not launching ExternalAudio")
        return 0

    return _run_external_audio(exe, cmd)


def transmit_file(
    config: dict[str, Any],
    airport: dict[str, Any],
    file_path: str,
    tx_name: str,
    freq: float,
    mod: str,
) -> int:
    import srs_radio  # local — EAM common-PTT overrides TX freq

    exe = resolve_external_audio_exe(config)
    path = Path(file_path)
    if not exe.is_file():
        print(f"ExternalAudio not found: {exe}", file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"Audio file not found: {path}", file=sys.stderr)
        return 2

    freq, mod = srs_radio.maybe_force_eam_tx_freq(config, freq, mod)

    cmd = [
        str(exe),
        f"--file={path}",
        f"--freqs={freq}",
        f"--modulations={mod}",
        f"--coalition={int(airport['coalition'])}",
        f"--ip={airport['srs_host']}",
        f"--port={int(airport['srs_port'])}",
        f"--name={tx_name}",
        f"--volume={float(config.get('tts_volume', 0.8))}",
        "--minimise",
    ]
    print("TX FILE:", path)
    print("CMD:", " ".join(cmd))
    if config.get("dry_run"):
        print("dry_run=true; not launching ExternalAudio")
        return 0

    # Estimate timeout from file size (WAV ~32KB/s at 16k mono) + connect slack
    try:
        approx_sec = max(30.0, min(180.0, path.stat().st_size / 16000.0 + 45.0))
    except OSError:
        approx_sec = 120.0
    return _run_external_audio(exe, cmd, timeout_sec=approx_sec)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ground/Tower ATC phrase → SRS ExternalAudio")
    parser.add_argument("--airport", default="nellis", help="Key in airports.json")
    parser.add_argument("--role", required=True, choices=["ground", "tower"])
    parser.add_argument(
        "--phrase",
        required=True,
        help="taxi|hold_short|contact_tower|lineup|clear_takeoff|clear_land|go_around",
    )
    parser.add_argument(
        "--callsign",
        default=None,
        help="Override Opus flight callsign (normally resolved from opus_user_name)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print phrase only")
    parser.add_argument("--print-only", action="store_true", help="Alias for --dry-run")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH)
    airports = load_json(AIRPORTS_PATH)

    key = args.airport.lower()
    if key not in airports:
        print(f"Unknown airport '{key}'. Known: {', '.join(airports)}", file=sys.stderr)
        return 2
    airport = airports[key]

    callsign = args.callsign or resolve_callsign_from_opus(config)
    if not callsign:
        print(
            "ERROR: Could not resolve callsign from Opus. "
            "Set opus_user_name in config.json or pass --callsign.",
            file=sys.stderr,
        )
        return 2

    weather = fetch_metar(config, airport["icao"])
    opus = resolve_active_opus_flight(config)
    runway = pick_departure_runway(airport, weather, opus, config)

    text, tx_name, freq, mod = build_phrase(
        airport, args.role, args.phrase, callsign, weather, runway
    )

    if weather.raw:
        print(f"METAR: {weather.raw}")
    print(f"Callsign: {callsign}")
    print(f"Active runway: {runway}")

    if args.dry_run or args.print_only:
        config = dict(config)
        config["dry_run"] = True

    return transmit(config, airport, text, tx_name, freq, mod)


if __name__ == "__main__":
    raise SystemExit(main())
