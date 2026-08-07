#!/usr/bin/env python3
"""
Stream Deck Ground/Tower phrase builder for SRS ExternalAudio.

Fetches METAR from Opus, picks active runway from wind, fills phrase templates,
and transmits via the patched DCS-SR-ExternalAudio.exe to the squadron SRS server.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
AIRPORTS_PATH = HERE / "airports.json"
STATE_PATH = HERE / "state.json"
CONFIG_PATH = HERE / "config.json"

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
    ctx.radio_callsign = override
    ctx.flight_callsign = override
    print(f"Callsign override: {override}")
    return ctx


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
        if override:
            print(f"Using manual callsign only (no Opus user/backend): {override}")
            return synthetic_flight_context(override)
        return None

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

    candidates: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    for flight in sorted(flights, key=sort_key, reverse=True):
        fid = flight.get("id")
        if fid is None:
            continue
        try:
            signups = http_get_json(f"{backend}/opus/flights/{fid}/signups", ua)
        except urllib.error.URLError:
            continue
        if not isinstance(signups, list):
            continue
        for su in signups:
            if str(su.get("user_name") or "").casefold() == user.casefold():
                candidates.append((flight, su, signups))
                break

    if not candidates:
        print(f"WARNING: No Opus signup found for user '{user}'", file=sys.stderr)
        if override:
            return synthetic_flight_context(override)
        return None

    flight_list, signup, signups = candidates[0]
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
    return apply_callsign_override(config, ctx)


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

    # Drop redundant transitions ("Fitter seven" + "Fitter transition")
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
      Flex west, Fitter transition
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


def random_initial_climb_feet() -> int:
    """Random initial climb between 12,000 and 17,000 ft (1,000 ft steps)."""
    return random.randint(12, 17) * 1000


def speak_departure_freq_or_local(airport: dict[str, Any]) -> str:
    """
    'departure three fife zero point two two fife' or 'departure Local five'
    when the channel has local_preset set (Opus UHF preset number).
    """
    departure = airport.get("departure") or {"freq_mhz": 350.0}
    preset = departure.get("local_preset")
    if preset is not None:
        try:
            n = int(preset)
            return f"departure Local {speak_minutes_natural(n) if n in MINUTE_WORDS else speak_digits(str(n))}"
        except (TypeError, ValueError):
            pass
    mhz = float(departure.get("freq_mhz") or 350.0)
    return _pick(
        f"departure {speak_freq(mhz)}",
        f"departure frequency {speak_freq(mhz)}",
    )


def _looks_like_icao(token: str) -> bool:
    return len(token) == 4 and token.isalpha()


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
) -> str:
    """Prefer runway coded on the filed route; else wind-preferred active runway."""
    from_fp = runway_from_route(opus.fp_route_string if opus else None)
    if from_fp:
        return from_fp
    return active_runway(list(airport.get("runways") or ["21"]), weather.wind_dir)


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
) -> str:
    """
    Clearance order:
      Callsign, Delivery, cleared to DEST, [DP/transition], then as filed,
      climb (12–17k random), expect FL after ten, departure freq|Local N, squawk…
    """
    name = airport["name"]
    cs = speak_callsign(callsign)
    filed_alt = speak_filed_altitude(opus.fp_altitude if opus else None)
    squawk = squawk_clearance_phrase(opus)
    dep_clause = speak_departure_freq_or_local(airport)
    climb = speak_altitude_value(str(random_initial_climb_feet()), prefer_fl_below=1000)

    # Who you are calling, who is calling
    if not opus or not opus.has_filed_plan:
        no_fp = _pick(
            "I show no flight plan on file",
            "negative flight plan on file",
            "I have no flight plan on file",
        )
        parts = [f"{cs}, {name} Delivery, {no_fp}"]
        if climb:
            parts.append(f"climb and maintain {climb}")
        parts.append(dep_clause)
        if squawk:
            parts.append(squawk)
        return f"{', '.join(parts)}."

    dest = speak_icao_or_name(opus.arr_icao, airport)
    parts = [f"{cs}, {name} Delivery, cleared to {dest}"]

    dep_via = speak_departure_clearance(airport, opus.fp_route_string)
    if dep_via:
        parts.append(dep_via)
    parts.append("then as filed")

    if climb:
        parts.append(_pick(f"climb and maintain {climb}", f"climb {climb}"))

    if filed_alt:
        minutes = expect_minutes_value(airport)
        natural = speak_minutes_natural(minutes)
        parts.append(f"expect {filed_alt} after {natural}")

    parts.append(dep_clause)
    if squawk:
        parts.append(squawk)

    return f"{', '.join(parts)}."


def build_clearance_readback(
    airport: dict[str, Any],
    callsign: str,
    weather: Weather,
    runway: str,
) -> str:
    """
    After pilot readback — slight phrasing variety, same content.
    """
    name = airport["name"]
    cs = speak_callsign(callsign)
    rwy = speak_runway(runway)
    alt = speak_altimeter(weather.altimeter_inhg or 29.92)
    ground = airport.get("ground") or {"freq_mhz": 275.8}
    gnd_freq = speak_freq(float(ground["freq_mhz"]))
    confirm = _pick("readback correct", "readback is correct", "that's correct")
    expect_rwy = _pick(f"expect runway {rwy}", f"expect {rwy}")
    taxi = _pick("when ready for taxi", "when ready to taxi")
    return (
        f"{cs}, {name} Delivery, {confirm}, {expect_rwy}, "
        f"{name} altimeter {alt}, "
        f"contact ground {gnd_freq} {taxi}."
    )


def speak_freq(mhz: float) -> str:
    # 275.8 -> two seven fife point eight
    s = f"{mhz:.3f}".rstrip("0").rstrip(".")
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
    req = urllib.request.Request(
        url,
        headers={"User-Agent": config.get("user_agent", "DCS-ATC-Phrase/1.0")},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"WARNING: Opus METAR fetch failed ({exc}); using calm defaults", file=sys.stderr)
        return Weather(None, 0, 29.92, "")

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
        return Weather(None, 0, 29.92, "")
    return parse_metar(raw)


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
    # --- United States (primary) — Neural2 preferred for ATC clarity ---
    # Deeper / more "controller" male voices first
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
    # Google: en-US-Neural2-D → letter D
    m = re.search(r"-(?:neural2|wavenet|standard|chirp3?-?hd|studio)-([a-z])\b", low)
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
) -> str:
    name = airport["name"]
    cs = speak_callsign(callsign)
    rwy = speak_runway(runway)
    alt = speak_altimeter(weather.altimeter_inhg or 29.92)
    wind = speak_wind(weather.wind_dir, weather.wind_speed_kt)
    taxi_via = airport.get("taxi_via", "Alpha")
    tower = airport.get("tower") or {"freq_mhz": 327.0}
    departure = airport.get("departure") or {"freq_mhz": 350.0}
    approach = airport.get("approach") or {"freq_mhz": 291.0}

    if template == "clearance":
        return build_clearance_delivery(airport, callsign, weather, runway, opus)
    if template == "clearance_readback":
        return build_clearance_readback(airport, callsign, weather, runway)

    templates = {
        "taxi": (
            f"{cs}, {name} Ground, taxi runway {rwy} via {taxi_via}, "
            f"hold short runway {rwy}, altimeter {alt}."
        ),
        "hold_short": f"{cs}, {name} Ground, hold short runway {rwy}.",
        "contact_tower": (
            f"{cs}, {name} Ground, contact tower on {speak_freq(float(tower['freq_mhz']))}."
        ),
        "taxi_in": f"{cs}, {name} Ground, taxi to parking via {taxi_via}.",
        "lineup": f"{cs}, {name} Tower, runway {rwy}, line up and wait.",
        "clear_takeoff": (
            f"{cs}, {name} Tower, runway {rwy}, {wind}, cleared for takeoff."
        ),
        "clear_land": f"{cs}, {name} Tower, runway {rwy}, {wind}, cleared to land.",
        "go_around": f"{cs}, {name} Tower, go around.",
        "contact_departure": (
            f"{cs}, {name} Tower, contact Departure on {speak_freq(float(departure['freq_mhz']))}."
        ),
        "radar_contact": f"{cs}, Departure, radar contact.",
        "approach_check_in": (
            f"{cs}, {name} Approach, radar contact, information received, "
            f"expect approach runway {rwy}."
        ),
        "cleared_approach": (
            f"{cs}, {name} Approach, cleared approach runway {rwy}, "
            f"contact tower on {speak_freq(float(tower['freq_mhz']))}."
        ),
        "bj_check_in": f"{cs}, Blackjack, loud and clear, report ready for range.",
        "bj_alpha_check": f"{cs}, Blackjack, alpha check, loud and clear.",
        "bj_range_entry": f"{cs}, Blackjack, cleared onto the range, hot.",
        "bj_range_exit": f"{cs}, Blackjack, range exit approved, report off.",
        "ops_check_in": f"{cs}, Ops, go ahead.",
        "radio_check": f"{cs}, {name}, loud and clear.",
    }
    if template not in templates:
        raise ValueError(f"Unknown template '{template}'. Valid: {', '.join(sorted(templates))}")
    return templates[template]


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
    ("taxi", "Ground — Taxi"),
    ("hold_short", "Ground — Hold short"),
    ("contact_tower", "Ground — Contact tower"),
    ("taxi_in", "Ground — Taxi in"),
    ("lineup", "Tower — Line up and wait"),
    ("clear_takeoff", "Tower — Cleared takeoff"),
    ("clear_land", "Tower — Cleared to land"),
    ("go_around", "Tower — Go around"),
    ("contact_departure", "Departure — Contact departure"),
    ("radar_contact", "Departure — Radar contact"),
    ("approach_check_in", "Approach — Check-in"),
    ("cleared_approach", "Approach — Cleared approach"),
    ("bj_check_in", "Blackjack — Check-in"),
    ("bj_alpha_check", "Blackjack — Alpha check"),
    ("bj_range_entry", "Blackjack — Range entry"),
    ("bj_range_exit", "Blackjack — Range exit"),
    ("ops_check_in", "Ops — Check-in"),
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
        text = build_template_text(
            airport, template or "radio_check", callsign, weather, runway, opus=opus
        )
    freq, mod, tx_name = channel_radio(airport, channel)
    return text, tx_name, freq, mod


def tts_speed(config: dict[str, Any] | None = None, speed: float | int | None = None) -> int:
    """ExternalAudio / System.Speech rate: -10..10 (1 = normal)."""
    if speed is None and config is not None:
        speed = config.get("tts_speed", 3)
    try:
        n = int(round(float(speed if speed is not None else 3)))
    except (TypeError, ValueError):
        n = 3
    return max(-10, min(10, n))


def preview_voice_local(
    voice: str,
    text: str,
    volume: float = 0.8,
    speed: float | int | None = None,
) -> None:
    """Speak TTS on local Windows speakers (does not go to SRS)."""
    if not text.strip():
        return
    # Escape for PowerShell single-quoted string
    safe_voice = voice.replace("'", "''")
    safe_text = text.replace("'", "''").replace("\r", " ").replace("\n", " ")
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

    cmd = [
        str(exe),
        f"--text={text}",
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
    if voice_override and str(voice_override).strip():
        voice = str(voice_override).strip()
        gender = voice_gender(voice)
    else:
        voice, gender = voice_for_channel(config, channel)
    if voice:
        cmd.append(f"--voice={voice}")
    if gender:
        cmd.append(f"--gender={gender}")
    if provider == "google" and google_creds is not None:
        cmd.append(f"--googleCredentials={google_creds}")
        # Language hint when voice id is incomplete (ExternalAudio also parses voice prefix)
        if len(voice) >= 5 and voice[2] == "-":
            cmd.append(f"--culture={voice[:5]}")

    print("TX:", text)
    print(f"TTS: {provider}" + (f" ({google_creds})" if provider == "google" else ""))
    print("CMD:", " ".join(cmd))

    if config.get("dry_run"):
        print("dry_run=true; not launching ExternalAudio")
        return 0

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc = subprocess.run(
        cmd,
        cwd=str(exe.parent),
        creationflags=creationflags,
    )
    return int(proc.returncode)


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

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(cmd, cwd=str(exe.parent), creationflags=creationflags)
    return int(proc.returncode)


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
    runway = active_runway(list(airport.get("runways") or ["21"]), weather.wind_dir)

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
