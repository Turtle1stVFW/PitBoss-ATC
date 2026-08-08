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

# Natural minute words for conversational ATC (vs digit-by-digit)
MINUTE_WORDS = {
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
    15: "fifteen",
    20: "twenty",
    30: "thirty",
}


def _pick(*options: str) -> str:
    return random.choice(options)


@dataclass
class Weather:
    wind_dir: int | None
    wind_speed_kt: int | None
    altimeter_inhg: float | None
    raw: str


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

# Map our agency channels -> substrings in Opus theater radio preset names
OPUS_FREQ_NAME_MAP: dict[str, tuple[str, ...]] = {
    "delivery": ("delivery",),
    "ground": ("ground",),
    "tower": ("tower",),
    "departure": ("departure",),
    "approach": ("approach",),
    "blackjack": ("blackjack",),
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


def _opus_cache_key(config: dict[str, Any]) -> str:
    return "|".join(
        [
            (config.get("opus_backend_url") or "").rstrip("/"),
            (config.get("opus_user_name") or "").strip().casefold(),
            callsign_override(config) or "",
            "1" if config.get("tts_include_seat") else "0",
        ]
    )


def resolve_active_opus_flight(config: dict[str, Any]) -> OpusFlightContext | None:
    """
    Look up Opus username's seat on the active flight and load filed FP fields.
    Turtle on BRUISER 5 seat 1 -> radio_callsign "BRUISER 5" (flight number only by default).
    Set config tts_include_seat=true to speak BRUISER 5-1.
    config callsign_override replaces the spoken callsign (FP still from Opus when available).
    """
    override = callsign_override(config)
    user = (config.get("opus_user_name") or "").strip()
    backend = (config.get("opus_backend_url") or "").rstrip("/")
    if not user or not backend:
        # Offline / no Opus: still allow phrase build + TTS/TX.
        # Manual callsign preferred; otherwise a clear placeholder.
        label = override or "CALLSIGN"
        print(f"Using offline callsign (no Opus user/backend): {label}")
        return synthetic_flight_context(label)

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

    def sort_key(f: dict[str, Any]) -> tuple:
        return (str(f.get("event_date") or ""), str(f.get("vul_start") or ""))

    # Newest first; stop at first signup match (was scanning every flight).
    flight_list: dict[str, Any] | None = None
    signup: dict[str, Any] | None = None
    signups: list[dict[str, Any]] = []
    for flight in sorted(flights, key=sort_key, reverse=True):
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
                signup = su
                signups = fetched_signups
                break
        if flight_list is not None:
            break

    if flight_list is None or signup is None:
        print(f"WARNING: No Opus signup found for user '{user}'", file=sys.stderr)
        if override:
            return synthetic_flight_context(override)
        return None

    base = str(flight_list.get("callsign") or "").strip()
    seat = signup.get("seat")
    fid = flight_list.get("id")
    if not base or seat is None or fid is None:
        if override:
            return synthetic_flight_context(override)
        return None

    detail: dict[str, Any] = dict(flight_list)
    try:
        fetched = http_get_json(f"{backend}/opus/flights/{fid}", ua)
        if isinstance(fetched, dict):
            detail = fetched
    except urllib.error.URLError as exc:
        print(f"WARNING: Opus flight detail fetch failed ({exc}); using list fields", file=sys.stderr)

    include_seat = bool(config.get("tts_include_seat", False))
    radio = f"{base}-{int(seat)}" if include_seat else base
    theater_raw = detail.get("theater_id")
    if theater_raw is None:
        theater_raw = flight_list.get("theater_id")
    theater_id = int(theater_raw) if theater_raw is not None else None
    qty_raw = detail.get("qty")
    try:
        flight_qty = int(qty_raw) if qty_raw is not None else None
    except (TypeError, ValueError):
        flight_qty = None
    signup_count = len(signups)

    ctx = OpusFlightContext(
        radio_callsign=radio,
        flight_id=int(fid),
        flight_callsign=base,
        seat=int(seat),
        event_date=str(detail.get("event_date") or flight_list.get("event_date") or "") or None,
        theater_id=theater_id,
        dep_icao=_str_or_none(detail.get("fp_departure") or detail.get("dep_icao")),
        arr_icao=_str_or_none(detail.get("arr_icao")),
        aircraft=_str_or_none(detail.get("aircraft")),
        fp_altitude=_str_or_none(detail.get("fp_altitude")),
        fp_speed=_str_or_none(detail.get("fp_speed")),
        fp_route_string=_str_or_none(detail.get("fp_route_string")),
        fp_remarks=_str_or_none(detail.get("fp_remarks")),
        fp_aircraft_type=_str_or_none(detail.get("fp_aircraft_type")),
        fp_filed_at=_str_or_none(detail.get("fp_filed_at")),
        mode3=_str_or_none(detail.get("mode3")),
        tcn=_str_or_none(detail.get("tcn")),
        comms_vhf=_str_or_none(detail.get("comms_vhf")),
        signup_count=signup_count,
        flight_qty=flight_qty,
    )
    print(
        f"Opus callsign: {radio} "
        f"(user={user}, flight={base}, seat={seat}, include_seat={include_seat}, "
        f"signups={signup_count}, qty={flight_qty}, "
        f"event={ctx.event_date}, filed={ctx.has_filed_plan}, route={ctx.fp_route_string}, "
        f"alt={ctx.fp_altitude}, squawk={ctx.mode3})"
    )
    _OPUS_CACHE["key"] = cache_key
    _OPUS_CACHE["exp"] = now + _OPUS_CACHE_TTL_SEC
    _OPUS_CACHE["ctx"] = ctx
    return apply_callsign_override(config, replace(ctx))


def resolve_callsign_from_opus(config: dict[str, Any]) -> str | None:
    """Compatibility wrapper — radio callsign only."""
    ctx = resolve_active_opus_flight(config)
    return None if ctx is None else ctx.radio_callsign


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_route_tokens(route: str | None) -> list[str]:
    if not route:
        return []
    return [p for p in re.split(r"[.\s]+", route.strip()) if p]


def runway_from_route(route: str | None) -> str | None:
    tokens = parse_route_tokens(route)
    if not tokens:
        return None
    m = _RUNWAY_TOKEN.match(tokens[-1])
    if not m:
        return None
    return f"{int(m.group(1))}{m.group(2).upper()}"


def first_route_fix(route: str | None) -> str | None:
    """First enroute fix after departure ICAO; skip arrival ICAO / runway tokens."""
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
    return tokens[start]


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


def _enroute_tokens(route: str | None) -> list[str]:
    tokens = parse_route_tokens(route)
    if not tokens:
        return []
    start = 1 if _looks_like_icao(tokens[0]) else 0
    end = len(tokens)
    if end > start and _RUNWAY_TOKEN.match(tokens[end - 1]):
        end -= 1
    if end > start and _looks_like_icao(tokens[end - 1]):
        end -= 1
    return [t.upper().replace(" ", "") for t in tokens[start:end]]


def _normalize_dep_token(tok: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", tok.upper())


@dataclass
class DepartureMatch:
    """Resolved DP / visual departure + optional transition for clearance speech."""

    instrument_id: str | None = None
    instrument_say: str | None = None
    visual_id: str | None = None
    visual_say: str | None = None
    transition_id: str | None = None
    transition_say: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.instrument_say or self.visual_say)


def match_departure(airport: dict[str, Any], route: str | None) -> DepartureMatch:
    """
    Match filed route against airport departure catalog.

    Instrument SIDs (DREAM7 / FYTTR7 / MMM8) only when that token is explicitly filed.
    Visual Flex north/west may be inferred from route prefixes (FLEX.MINTT, FLEX.FYTTR).
    Bare fixes like DREAM / FYTTR / MINTT alone do not invent a SID — stay 'as filed'.
    """
    catalog = load_departure_catalog(airport)
    result = DepartureMatch()
    if not catalog or not route:
        sid = filed_sid(airport, route)
        if sid:
            result.instrument_id = sid
            result.instrument_say = speak_fix(sid)
        return result

    tokens = _enroute_tokens(route)
    if not tokens:
        return result
    norms = [_normalize_dep_token(t) for t in tokens]
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
        # Transitions allowed for this SID
        for tok in norms:
            for tk, say in (entry.get("transitions") or {}).items():
                if tok == _normalize_dep_token(tk) and tok not in aliases:
                    result.transition_id = str(tk)
                    result.transition_say = str(say)
                    break
            if result.transition_say:
                break
        break

    # --- Visual: explicit alias OR route prefix (FLEX.MINTT → Flex north) ---
    vis_hit: tuple[int, dict[str, Any]] | None = None
    for entry in catalog.get("visual") or []:
        score = 0
        aliases = {_normalize_dep_token(a) for a in (entry.get("aliases") or [])}
        if aliases.intersection(norms):
            score = 200
        for prefix in entry.get("route_prefixes") or []:
            pref = [_normalize_dep_token(p) for p in prefix]
            if pref and norms[: len(pref)] == pref:
                score = max(score, len(pref) * 40)
        if score and (vis_hit is None or score > vis_hit[0]):
            vis_hit = (score, entry)

    if vis_hit:
        entry = vis_hit[1]
        result.visual_id = str(entry.get("id"))
        result.visual_say = str(entry.get("say") or entry.get("id"))
        # Transition from route / default when no instrument SID transition already set
        if not result.transition_say:
            for tok in norms:
                if tok in global_transitions and tok != "FLEX":
                    # Don't use FYTTR/MMM as transition if it only restates an explicit SID alias
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
        return f"{m.visual_say}"
    return None


def resolve_taxi_route(airport: dict[str, Any], runway: str) -> dict[str, str]:
    """
    Runway-dependent EOR / taxi via for Nellis-style fields.
    21R → NW EOR via Foxtrot; 03L → Alpha South via Foxtrot.
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
    parking = str(airport.get("parking") or "parking").strip() or "parking"
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


def speak_local_preset(airport: dict[str, Any], channel: str) -> str | None:
    """Speak 'Local three' when channel has local_preset; else None."""
    block = airport.get(channel) or {}
    preset = block.get("local_preset")
    if preset is None:
        return None
    try:
        n = int(preset)
    except (TypeError, ValueError):
        return None
    spoken = speak_minutes_natural(n) if n in MINUTE_WORDS else speak_digits(str(n))
    return f"Local {spoken}"


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


def speak_departure_freq_or_local(airport: dict[str, Any]) -> str:
    """
    Prefers UHF channel preset when set:
      'departure channel four' (455 wiki) / 'departure Local five' (Bruiser PDF)
    else spoken MHz.
    """
    block = airport.get("departure") or {}
    preset = block.get("local_preset")
    if preset is not None:
        try:
            n = int(preset)
            spoken_n = speak_minutes_natural(n) if n in MINUTE_WORDS else speak_digits(str(n))
            # Wiki: "Departure channel 4"; some kneeboards say Local N
            return _pick(f"departure channel {spoken_n}", f"departure Local {spoken_n}")
        except (TypeError, ValueError):
            pass
    mhz = float(block.get("freq_mhz") or 350.0)
    return _pick(
        f"departure {speak_freq(mhz)}",
        f"departure frequency {speak_freq(mhz)}",
    )


def clearance_spoken_agency(airport: dict[str, Any]) -> str:
    """
    When Clearance is consolidated with Ground (455 wiki: Local 2 / 275.8),
    speak 'Ground'; otherwise 'Delivery'.
    """
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


def speak_altitude_value(alt: str | None, *, prefer_fl_below: int = 1000) -> str | None:
    """
    Speak an altitude string.
    Values < prefer_fl_below (default 1000) are treated as flight levels (220 -> FL220).
    Larger values are feet (7000 -> seven thousand).
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
    # Feet: 7000 -> seven thousand; 7500 -> seven thousand fife hundred
    if n >= 1000 and n % 1000 == 0:
        thousands = n // 1000
        return f"{speak_digits(str(thousands))} thousand"
    if n >= 1000:
        thousands = n // 1000
        rest = n % 1000
        if rest % 100 == 0:
            return f"{speak_digits(str(thousands))} thousand {speak_digits(str(rest // 100))} hundred"
        return f"{speak_digits(str(n))} feet"
    return f"{speak_digits(str(n))} feet"


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


def pick_departure_runway(
    airport: dict[str, Any],
    weather: Weather,
    opus: OpusFlightContext | None,
    config: dict[str, Any] | None = None,
    step: dict[str, Any] | None = None,
) -> str:
    """
    Runway selection order:
    1. Per-step runway on the mission step (if set)
    2. Manual runway_override from Setup (if set)
    3. Runway coded on the filed Opus route
    4. Wind-preferred active runway from airport.runways
    """
    if step is not None:
        step_rwy = normalize_runway(step.get("runway"))
        if step_rwy:
            print(f"Runway (step): {step_rwy}")
            return step_rwy
    if config is not None:
        override = runway_override(config)
        if override:
            print(f"Runway override: {override}")
            return override
    from_fp = runway_from_route(opus.fp_route_string if opus else None)
    if from_fp:
        return from_fp
    return active_runway(list(airport.get("runways") or ["21"]), weather.wind_dir)


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
) -> tuple[str, int]:
    """
    NATCF clearance (455 wiki + Bruiser PDF):
      Cleared to DEST via the [SID/Flex], then as filed,
      Climb via SID | Climb via SID except maintain X | climb and maintain X expect FL,
      departure channel/Local N, squawk…

    Returns (phrase, climb_feet_used).
    """
    del weather  # clearance does not include altimeter
    del runway
    name = airport["name"]
    agency = clearance_spoken_agency(airport)
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
        # Wiki: "Cleared to Nellis via the DREAM SEVEN departure, then as filed"
        # Ensure instrument SID includes "departure" (already in speak_departure_clearance)
        parts = [f"{cs}, {name} {agency}, cleared to {dest} via the {dep_via}"]
    else:
        parts = [f"{cs}, {name} {agency}, cleared to {dest}"]
    parts.append("then as filed")

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


def build_clearance_readback(
    airport: dict[str, Any],
    callsign: str,
    weather: Weather,
    runway: str,
) -> str:
    """
    After pilot readback (455 wiki):
      readback correct, advise ready to taxi, expect runway…
    (Clearance is often on Ground — do not send them to Ground again.)
    """
    del weather
    name = airport["name"]
    agency = clearance_spoken_agency(airport)
    cs = speak_callsign(callsign)
    rwy = speak_runway(runway)
    return (
        f"{cs}, {name} {agency}, readback correct, "
        f"advise ready to taxi, expect runway {rwy}."
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
    m = re.match(r"^\s*(\d{1,2})\s*([LCR]?)\s*$", str(rwy), re.IGNORECASE)
    if not m:
        return speak_digits(re.sub(r"[^0-9]", "", str(rwy)))
    num = speak_digits(str(int(m.group(1))))
    side = {"L": "left", "R": "right", "C": "center"}.get(m.group(2).upper(), "")
    return f"{num} {side}".strip()


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

    return Weather(wind_dir, wind_speed, altimeter, raw)


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
    # Canada (WaveNet — no Neural2 catalog for en-CA in many projects)
    "en-CA-Wavenet-A",
    "en-CA-Wavenet-B",
    "en-CA-Wavenet-C",
    "en-CA-Wavenet-D",
]

# Locale display order for picker headers (US → allies)
_LOCALE_DISPLAY_ORDER: list[str] = ["en-US", "en-GB", "en-AU", "en-CA"]

LOCALE_LABELS: dict[str, str] = {
    "en-US": "United States (primary)",
    "en-GB": "United Kingdom (ally)",
    "en-AU": "Australia (ally)",
    "en-CA": "Canada (ally)",
    "en-IN": "English (India)",
    "en-IE": "English (Ireland)",
    "en-NZ": "English (New Zealand)",
    "en-ZA": "English (South Africa)",
}


def voice_locale(voice_name: str) -> str:
    """BCP-47 locale prefix from a Google voice id (en-US-Neural2-D → en-US)."""
    name = (voice_name or "").strip()
    parts = name.split("-")
    if len(parts) >= 2 and len(parts[0]) == 2 and len(parts[1]) in (2, 3):
        return f"{parts[0]}-{parts[1]}"
    return name[:5] if len(name) >= 5 else name


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
    if any(x in low for x in ("zira", "female", "hazel", "susan", "eva", "aria", "jenny")):
        return "female"
    if any(x in low for x in ("david", "guy", "male", "mark", "james")):
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
) -> str:
    name = airport["name"]
    cs = speak_callsign(callsign)
    rwy = speak_runway(runway)
    alt = speak_altimeter(weather.altimeter_inhg or 29.92)
    wind = speak_wind(weather.wind_dir, weather.wind_speed_kt)
    taxi = resolve_taxi_route(airport, runway)
    tower = airport.get("tower") or {"freq_mhz": 327.0}
    departure = airport.get("departure") or {"freq_mhz": 350.0}
    twr_local = speak_local_preset(airport, "tower")
    climb_ft = random_initial_climb_feet(initial_climb_ft)
    climb = speak_altitude_value(str(climb_ft), prefer_fl_below=1000)
    if climb_ft_out is not None and template in ("radar_contact", "center_radar"):
        climb_ft_out.append(climb_ft)

    if template == "clearance":
        text, used_climb = build_clearance_delivery(
            airport,
            callsign,
            weather,
            runway,
            opus,
            initial_climb_ft=initial_climb_ft,
        )
        if climb_ft_out is not None:
            climb_ft_out.append(used_climb)
        return text
    if template == "clearance_readback":
        return build_clearance_readback(airport, callsign, weather, runway)

    # Takeoff: mention VFR Flex west when route/departure match is Flex west
    flex_west = False
    if opus and opus.fp_route_string:
        m = match_departure(airport, opus.fp_route_string)
        flex_west = bool(m.visual_id == "FLEX_WEST" or (m.visual_say or "").casefold() == "flex west")

    ship_note = ""
    if opus and opus.squawk_in_sequence:
        n = opus.signup_count if opus.signup_count > 1 else (opus.flight_qty or 0)
        if n and int(n) > 1:
            ship_note = f", {speak_digits(str(int(n)))} ship"

    if template == "taxi":
        return (
            f"{cs}, {name} Ground, runway {rwy}, taxi {taxi['eor']} via {taxi['outbound_via']}, "
            f"{name} altimeter {alt}."
        )
    if template == "monitor_tower":
        if twr_local:
            return f"{cs}, {name} Ground, monitor tower, {twr_local}, good day."
        return (
            f"{cs}, {name} Ground, monitor tower on "
            f"{speak_freq(float(tower['freq_mhz']))}, good day."
        )
    if template == "hold_short":
        return f"{cs}, {name} Ground, hold short runway {rwy}."
    if template == "contact_tower":
        if twr_local:
            return f"{cs}, {name} Ground, contact tower, {twr_local}."
        return (
            f"{cs}, {name} Ground, contact tower on {speak_freq(float(tower['freq_mhz']))}."
        )
    if template == "exit_runway":
        # 21R → right at Alpha (Alpha North area); 03L → left at Alpha
        return f"{cs}, {name} Tower, exit {taxi['exit']}."
    if template == "taxi_in":
        return (
            f"{cs}, {name} Ground, taxi to {taxi['parking']} via {taxi['inbound_via']}."
        )
    if template == "lineup":
        return f"{cs}, {name} Tower, runway {rwy}, line up-and wait."
    if template == "remain_position":
        # Traffic: hold at EOR / short of runway until further clearance
        return f"{cs}, {name} Tower, remain in position."
    if template == "rolling_accept":
        # Wiki: Tower solicits rolling takeoff; aircrew may accept or refuse
        return f"{cs}, will you accept rolling?"
    if template == "clear_takeoff":
        # 455 wiki: contact departure in takeoff clearance (switch before roll)
        if flex_west:
            return (
                f"{cs}, {name} Tower, VFR Flex west, {wind}, runway {rwy}, "
                f"cleared for takeoff, contact departure."
            )
        return (
            f"{cs}, {name} Tower, {wind}, runway {rwy}, "
            f"cleared for takeoff, contact departure."
        )
    if template == "clear_takeoff_rolling":
        return (
            f"{cs}, {name} Tower, {wind}, runway {rwy}, "
            f"cleared for takeoff rolling, contact departure."
        )
    if template == "clear_takeoff_intersection":
        ix = taxi.get("intersection") or ("Delta" if str(runway).upper().startswith("21") else "Bravo")
        return (
            f"{cs}, {name} Tower, {wind}, runway {rwy} at {ix}, "
            f"cleared for takeoff, contact departure."
        )
    if template == "right_break":
        return f"{cs}, {name} Tower, right break approved runway {rwy}."
    if template == "clear_land":
        return f"{cs}, {name} Tower, {wind}, runway {rwy}, cleared to land."
    if template == "go_around":
        return f"{cs}, {name} Tower, go around."
    if template == "contact_departure":
        # Standalone handoff if not already in takeoff clearance
        return f"{cs}, {name} Tower, contact departure."
    if template == "radar_contact":
        return (
            f"{cs}, {name} Departure, radar contact, climb and maintain {climb}."
        )
    if template == "approach_check_in":
        return (
            f"{cs}, {name} Approach, radar contact, cleared STRYK recovery, "
            f"descend and maintain one zero thousand, maintain three zero zero knots, "
            f"expect TAC overhead runway {rwy}, {name} altimeter {alt}."
        )
    if template == "cleared_approach":
        if twr_local:
            contact = f"contact tower, {twr_local}"
        else:
            contact = f"contact tower on {speak_freq(float(tower['freq_mhz']))}"
        return (
            f"{cs}, {name} Approach, cleared tactical overhead runway {rwy}, {contact}."
        )
    if template == "bj_check_in":
        return (
            f"{cs}, Blackjack, loud and clear{ship_note}, "
            f"cleared onto the range, hot, report Alpha."
        )
    if template == "bj_alpha_check":
        return f"{cs}, Blackjack, alpha check, loud and clear."
    if template == "bj_range_entry":
        return f"{cs}, Blackjack, Alpha approved, cleared hot."
    if template == "bj_range_exit":
        return f"{cs}, Blackjack, range exit approved, report off."
    if template == "ops_check_in":
        return f"{cs}, Ops, go ahead."
    if template == "center_radar":
        return (
            f"{cs}, radar contact, climb and maintain {climb}."
        )
    if template == "center_handoff":
        # Generic handoff; step freq/name filled when used on Other
        return f"{cs}, contact center on {speak_freq(377.1)}, good day."
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
    ("clear_takeoff_rolling", "Tower — Cleared takeoff rolling"),
    ("clear_takeoff_intersection", "Tower — Cleared takeoff (intersection)"),
    ("right_break", "Tower — Right break"),
    ("clear_land", "Tower — Cleared to land"),
    ("exit_runway", "Tower — Exit runway"),
    ("go_around", "Tower — Go around"),
    ("contact_departure", "Tower — Contact departure"),
    ("radar_contact", "Departure — Radar contact"),
    ("approach_check_in", "Approach — STRYK recovery"),
    ("cleared_approach", "Approach — Cleared tactical overhead"),
    ("bj_check_in", "Blackjack — Check-in / range"),
    ("bj_alpha_check", "Blackjack — Alpha check"),
    ("bj_range_entry", "Blackjack — Alpha approved"),
    ("bj_range_exit", "Blackjack — Range exit"),
    ("ops_check_in", "Ops — Check-in"),
    ("center_radar", "Other — Center radar contact"),
    ("center_handoff", "Other — Center handoff"),
    ("radio_check", "Other — Radio check"),
]

CHANNELS = [
    "delivery",
    "ground",
    "tower",
    "departure",
    "approach",
    "blackjack",
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
        )
    else:
        climb_fixed: int | None = None
        tmpl = template or "radio_check"
        if step is not None and tmpl in ("clearance", "radar_contact", "center_radar"):
            try:
                raw_climb = step.get("initial_climb_ft")
                climb_fixed = int(raw_climb) if raw_climb is not None else None
            except (TypeError, ValueError):
                climb_fixed = None
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
        )
        # Sticky climb so Preview / Hear / Fly don't re-roll 12–17k each time
        if step is not None and climb_out:
            step["initial_climb_ft"] = climb_out[0]
    freq, mod, tx_name = channel_radio(airport, channel)
    return text, tx_name, freq, mod


def tts_speed(config: dict[str, Any] | None = None, speed: float | int | None = None) -> int:
    """ExternalAudio / System.Speech rate: -10..10 (1 = normal)."""
    if speed is None and config is not None:
        speed = config.get("tts_speed", 7)
    try:
        n = int(round(float(speed if speed is not None else 7)))
    except (TypeError, ValueError):
        n = 3
    return max(-10, min(10, n))


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


# Controlled Google SSML pauses (Chirp tends to over-breathe on raw commas).
_RADIO_BREAK_COMMA_MS = 70
_RADIO_BREAK_PERIOD_MS = 160
# Trailing hang time so SRS/ExternalAudio does not clip the last syllable.
_RADIO_BREAK_END_MS = 120
_RADIO_WAV_PAD_MS = 180

# Short-lived caches — Hear/Preview/TX used to re-hit Opus for every flight signup.
_OPUS_CACHE_TTL_SEC = 45.0
_METAR_CACHE_TTL_SEC = 60.0
_TTS_WAV_CACHE_TTL_SEC = 180.0
_OPUS_CACHE: dict[str, Any] = {"key": "", "exp": 0.0, "ctx": None}
_METAR_CACHE: dict[str, Any] = {}  # key -> {"exp": float, "wx": Weather}
_TTS_WAV_CACHE: dict[str, Any] = {"key": "", "exp": 0.0, "path": None}


def _clean_radio_clause(part: str, voice: str | None = None) -> str:
    p = (part or "").strip()
    if not p:
        return ""
    p = re.sub(r"[\"'`]+", "", p)
    p = re.sub(r"\s+-\s+", " ", p)
    p = re.sub(r"\s+", " ", p).strip()
    if not p:
        return ""
    if voice and "chirp" in voice.casefold():
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
    """Plain spoken string (Windows TTS / logging). Clauses joined with spaces."""
    return " ".join(_radio_tts_clauses(text, voice=voice))


def _is_radio_digit_token(word: str) -> bool:
    return word.casefold() in _RADIO_DIGIT_TOKENS


def _is_radio_digit_glue_token(word: str) -> bool:
    low = word.casefold()
    return low in _RADIO_DIGIT_TOKENS or low in _RADIO_DIGIT_GLUE_MID


def _glue_digit_runs_ssml(clause: str) -> str:
    """
    Join consecutive radio digit words without prosodic breaks.

    Chirp often inserts a long breath inside squawks ("six fife four……one").
    <break strength="none"/> between digit words suppresses that.
    """
    words = (clause or "").split()
    if not words:
        return ""
    out: list[str] = []
    i = 0
    while i < len(words):
        if not _is_radio_digit_token(words[i]):
            out.append(html.escape(words[i]))
            i += 1
            continue
        run = [words[i]]
        j = i + 1
        while j < len(words) and _is_radio_digit_glue_token(words[j]):
            # Keep trailing point/decimal only when another digit follows
            if words[j].casefold() in _RADIO_DIGIT_GLUE_MID:
                if j + 1 >= len(words) or not _is_radio_digit_token(words[j + 1]):
                    break
            run.append(words[j])
            j += 1
        if len(run) >= 2:
            out.append(
                '<break strength="none"/>'.join(html.escape(w) for w in run)
            )
        else:
            out.append(html.escape(run[0]))
        i = j
    return " ".join(out)


def prepare_radio_tts_ssml(text: str, voice: str | None = None) -> str:
    """
    Google TTS SSML: brief break at commas, slightly longer at periods.
    Always ends with a short hang so the last word is not clipped on TX.
    Digit runs (squawk, freqs) are glued with break strength=none.
    (Chirp supports <break> on sync synthesize.)
    """
    segments = _radio_tts_segments(text, voice=voice)
    if not segments:
        return "<speak></speak>"
    parts: list[str] = []
    for clause, pause in segments:
        parts.append(_glue_digit_runs_ssml(clause))
        if pause == "comma":
            parts.append(f'<break time="{_RADIO_BREAK_COMMA_MS}ms"/>')
        elif pause == "period":
            parts.append(f'<break time="{_RADIO_BREAK_PERIOD_MS}ms"/>')
    parts.append(f'<break time="{_RADIO_BREAK_END_MS}ms"/>')
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

    Commas/periods are kept as characters here for readability; in the API they
    become timed SSML breaks (not literal spoken \"comma\"). Chirp also gets
    lowercased; Neural2/WaveNet keep case.
    """
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
    if voice and "chirp" in voice.casefold():
        note = "Google pauses at , / . · Chirp lowercased for steadier cadence"
    elif voice and is_google_voice_name(voice):
        note = "Google pauses at , / . (same for Neural2 / WaveNet / Chirp)"
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
    # Keep original commas for SSML pause points; plain form is for billing/logging
    plain = prepare_radio_tts_text(text, voice=voice)
    ssml = prepare_radio_tts_ssml(text, voice=voice)
    if not voice:
        raise ValueError("Google voice id is empty.")
    if not plain:
        raise ValueError("Preview text is empty.")
    safe_voice, guard_notes = resolve_voice_under_free_tier(voice)
    for note in guard_notes:
        print(note, file=sys.stderr)
    if safe_voice is None:
        raise RuntimeError(
            "Google TTS blocked: free tiers for Chirp/Neural2/WaveNet are at or above "
            f"{TTS_FREE_TIER_WARN_PCT * 100:.0f}%. Switch to Windows voices to avoid charges."
        )
    voice = safe_voice
    path = Path(creds_path)
    if not path.is_file():
        raise FileNotFoundError(f"Google credentials file not found: {path}")

    rate = google_speaking_rate(speed)
    cache_key = hashlib.sha1(f"{voice}\0{rate:.3f}\0{ssml}".encode("utf-8")).hexdigest()
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
    if "chirp" not in voice.casefold():
        audio_cfg["pitch"] = -1.0
    payload = {
        "input": {"ssml": ssml},
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
) -> int:
    exe = resolve_external_audio_exe(config)
    if not exe.is_file():
        print(f"ExternalAudio not found: {exe}", file=sys.stderr)
        return 2

    provider = tts_provider(config)
    google_creds = google_credentials_path(config)
    if voice_override and str(voice_override).strip():
        voice = str(voice_override).strip()
        gender = voice_gender(voice)
    else:
        voice, gender = voice_for_channel(config, channel)

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
        print(f"TTS: google local-synth -> WAV ({voice}) -> ExternalAudio --file")
        if config.get("dry_run"):
            print("dry_run=true; not launching ExternalAudio")
            return 0
        wav_path: Path | None = None
        try:
            wav_path = synthesize_google_tts(
                google_creds, voice, text, speed=tts_speed(config)
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
        f"--speed={tts_speed(config)}",
        "--minimise",
    ]
    if voice:
        cmd.append(f"--voice={voice}")
    if gender:
        cmd.append(f"--gender={gender}")

    print("TX:", text)
    if spoken != text.strip():
        print("TTS text (flattened):", spoken)
    print(f"TTS: {provider}")
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
    exe = resolve_external_audio_exe(config)
    path = Path(file_path)
    if not exe.is_file():
        print(f"ExternalAudio not found: {exe}", file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"Audio file not found: {path}", file=sys.stderr)
        return 2

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
