"""
Opus tanker tracks + official boom AAR comms (ATP-56 / USAF KC-135).

F-16s use the KC-135 boom — not MPRS pods and not the KC-130. Blackjack /
Bandsaw give vectors from the theater tanker list plus the live CAOC track.
On tanker freq this app only fills the missing join (cleared rejoin left,
sometimes left observation). The tanker does not "identify" the receiver.
Observation / pre-contact / cleared contact stay on the DCS tanker radio
so the boom AI still works. C2 gives track and BRAA; TACAN, frequency,
and bullseye wait until asked.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

import atc_phrase

_TANKER_CACHE: dict[str, Any] = {"key": "", "exp": 0.0, "rows": []}
_TANKER_CACHE_S = 45.0

# Spoken tanker brands (opening address + named request).
TANKER_BRANDS: tuple[str, ...] = (
    "texaco",
    "shell",
    "arco",
    "esso",
    "tanker",
)

_TANKER_STATE_KEYS = (
    "tanker_id",
    "tanker_callsign",
    "tanker_freq_mhz",
    "tanker_phase",
    "tanker_aircraft",
    "tanker_track",
    "tanker_tcn",
    "tanker_rejoined",
    "tanker_chat_in_range_since",
    "tanker_chat_dwell_s",
    "tanker_chat_auto_done",
    "tanker_overlay",
    "tanker_resume_index",
    "tanker_resume_step_id",
    "tanker_resume_channel",
    "tanker_seen_tune",
    "tanker_needs_c2_checkin",
)

# Boom chat lives with the jet on AAR, not on the shared C2 cursor.
_CHAT_STATE_KEYS = (
    "tanker_chat",
    "tanker_chat_last_id",
    "tanker_chat_recent",
    "tanker_chat_history",
    "tanker_chat_greeted",
    "tanker_chat_llm_note",
    "tanker_chat_guard_until",
    "tanker_chat_last_spoke",
    "tanker_chat_last_canned",
    "tanker_chat_said",
)

CHAT_STATE_KEYS = _CHAT_STATE_KEYS
SEAT_STATE_KEYS = _TANKER_STATE_KEYS + _CHAT_STATE_KEYS

# Copied to the other ship in the element (1-2 or 3-4). Not boom chat.
_OVERLAY_COPY_KEYS = (
    "tanker_overlay",
    "tanker_resume_index",
    "tanker_resume_step_id",
    "tanker_resume_channel",
    "tanker_needs_c2_checkin",
    "tanker_seen_tune",
    "tanker_id",
    "tanker_callsign",
    "tanker_freq_mhz",
    "tanker_phase",
    "tanker_aircraft",
    "tanker_track",
    "tanker_tcn",
)

# Receiver position in the boom pattern.
PHASE_NONE = ""
PHASE_JOIN = "join"
PHASE_OBSERVATION = "observation"
PHASE_ASTERN = "astern"
PHASE_CONTACT = "contact"
PHASE_RIGHT = "right"
PHASE_DEPARTED = "departed"

# Boom / observation envelope where Texaco may start small talk.
CHAT_MIN_NM = 0.1
CHAT_MAX_NM = 0.5
CHAT_REARM_NM = 1.0
CHAT_DWELL_MIN_S = 30.0
CHAT_DWELL_MAX_S = 60.0
CHAT_DWELL_S = CHAT_DWELL_MIN_S


def tanker_is_f16_boom(row: dict[str, Any] | None) -> bool:
    """True for KC-135 boom (not MPRS, not KC-130)."""
    blobs = [
        str((row or {}).get("aircraft") or ""),
        str((row or {}).get("objectName") or ""),
        str((row or {}).get("object_name") or ""),
    ]
    ac = re.sub(r"[\s_\-]+", "", " ".join(blobs).upper())
    if not ac:
        return False
    if "MPRS" in ac:
        return False
    if "KC130" in ac or re.search(r"(?<![A-Z])C130", ac):
        return False
    return "KC135" in ac


def _unit_is_f16_boom(unit: dict[str, Any] | None) -> bool | None:
    """Live CAOC type: True/False boom, or None if the unit is not typed."""
    if not unit:
        return None
    obj = re.sub(
        r"[\s_\-]+",
        "",
        str(unit.get("objectName") or unit.get("object_name") or "").upper(),
    )
    if not obj:
        return None
    if "MPRS" in obj:
        return False
    if "KC130" in obj or re.search(r"(?<![A-Z])C130", obj):
        return False
    if "KC135" in obj:
        return True
    return None


def _row_is_boom(
    row: dict[str, Any] | None,
    unit: dict[str, Any] | None = None,
) -> bool:
    live = _unit_is_f16_boom(unit)
    if live is False:
        return False
    if live is True:
        return True
    return tanker_is_f16_boom(row)


def normalize_tanker_name(raw: str | None) -> str:
    """'TEXACO 1' / 'Texaco1' / 'texaco one' → 'TEXACO1'."""
    text = str(raw or "").strip().upper()
    text = text.replace("#", " ")
    text = re.sub(r"\s+", " ", text)
    words = {
        "ONE": "1",
        "TWO": "2",
        "THREE": "3",
        "FOUR": "4",
        "FIVE": "5",
        "SIX": "6",
        "SEVEN": "7",
        "EIGHT": "8",
        "NINE": "9",
    }
    parts = [words.get(p, p) for p in text.split()]
    return re.sub(r"[^A-Z0-9]+", "", "".join(parts))


def speak_tanker_callsign(name: str | None) -> str:
    """'TEXACO 1' → 'Texaco one'."""
    raw = str(name or "").strip()
    if not raw:
        return "Tanker"
    m = re.match(r"^([A-Za-z]+)\s*(\d+)\s*$", raw)
    if m:
        return f"{m.group(1).title()} {atc_phrase.speak_digits(m.group(2))}"
    compact = normalize_tanker_name(raw)
    m = re.match(r"^([A-Z]+)(\d+)$", compact)
    if m:
        return f"{m.group(1).title()} {atc_phrase.speak_digits(m.group(2))}"
    return atc_phrase.speak_callsign(raw)


def speak_aar_track(track: str | None) -> str:
    """'AR231V' → 'A R two tree one Victor'; 'ARLNS' → 'Alpha Romeo Lima …'."""
    raw = str(track or "").strip().upper()
    if not raw:
        return ""
    m = re.match(r"^AR[\s\-/]*(\d{1,4})[\s\-/]*([A-Z])?$", raw)
    if m:
        bits = ["A R", atc_phrase.speak_digits(m.group(1))]
        letter = m.group(2)
        if letter:
            bits.append(_nato_letter(letter))
        return " ".join(p for p in bits if p)
    bits: list[str] = []
    for ch in raw:
        if ch.isspace() or ch in "-_/":
            continue
        if ch.isdigit():
            bits.append(atc_phrase.speak_digits(ch))
        elif ch.isalpha():
            bits.append(_nato_letter(ch))
    return " ".join(bits)


def _nato_letter(ch: str) -> str:
    table = getattr(atc_phrase, "_NATO_TAXIWAY", {}) or {}
    return str(table.get(ch.lower()) or ch)


def speak_tacan(raw: str | None) -> str:
    """'39X' → 'three niner x-ray'."""
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    m = re.match(r"^(\d{1,3})([XY])?$", text.replace(" ", ""))
    if not m:
        return atc_phrase.speak_digits(re.sub(r"\D", "", text) or text)
    chan = atc_phrase.speak_digits(m.group(1))
    band = {"X": "x-ray", "Y": "yankee"}.get(m.group(2) or "", "")
    return f"{chan} {band}".strip()


def _theater_id(config: dict[str, Any] | None, opus: Any = None) -> int:
    if opus is not None and getattr(opus, "theater_id", None) is not None:
        try:
            return int(opus.theater_id)
        except (TypeError, ValueError):
            pass
    try:
        return int((config or {}).get("opus_theater_id") or 1)
    except (TypeError, ValueError):
        return 1


def fetch_opus_tankers(
    config: dict[str, Any] | None,
    *,
    opus: Any = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Theater tanker list from Opus `GET /theaters/{id}/tankers`."""
    cfg = config or {}
    backend = str(cfg.get("opus_backend_url") or "").rstrip("/")
    if not backend:
        return []
    tid = _theater_id(cfg, opus)
    key = f"{backend}|{tid}"
    now = time.time()
    if (
        not force
        and _TANKER_CACHE.get("key") == key
        and float(_TANKER_CACHE.get("exp") or 0) > now
    ):
        return list(_TANKER_CACHE.get("rows") or [])
    ua = str(cfg.get("user_agent") or "DCS-ATC-Phrase/1.0")
    try:
        data = atc_phrase.http_get_json(f"{backend}/theaters/{tid}/tankers", ua)
    except Exception:
        return list(_TANKER_CACHE.get("rows") or [])
    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        for raw in data:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("tanker") or raw.get("callsign") or "").strip()
            if not name:
                continue
            freq = atc_phrase._parse_mhz(raw.get("freq") or raw.get("frequency"))
            rows.append(
                {
                    "id": raw.get("id"),
                    "callsign": name,
                    "track": str(raw.get("track") or "").strip(),
                    "aircraft": str(raw.get("aircraft") or "").strip(),
                    "altitude": str(raw.get("altitude") or "").strip(),
                    "freq_mhz": freq,
                    "tcn": str(raw.get("tcn_iff") or raw.get("tcn") or "").strip(),
                    "boom": tanker_is_f16_boom(raw),
                }
            )
    _TANKER_CACHE["key"] = key
    _TANKER_CACHE["exp"] = now + _TANKER_CACHE_S
    _TANKER_CACHE["rows"] = rows
    return list(rows)


def tanker_freqs_mhz(config: dict[str, Any] | None, *, opus: Any = None) -> list[float]:
    """Published tanker UHF freqs (for tune matching)."""
    out: list[float] = []
    for row in fetch_opus_tankers(config, opus=opus):
        val = _positive_mhz(row.get("freq_mhz"))
        if val is not None and not any(abs(val - x) < 0.01 for x in out):
            out.append(val)
    return out


def _positive_mhz(value: Any) -> float | None:
    mhz = atc_phrase._parse_mhz(value)
    if mhz is None or mhz <= 0:
        return None
    return float(mhz)


def catalog_freq_for_tanker(
    tanker: dict[str, Any] | None,
    config: dict[str, Any] | None = None,
    *,
    opus: Any = None,
) -> float | None:
    """UHF for this tanker identity — never another track's published freq."""
    if not isinstance(tanker, dict):
        return None
    direct = _positive_mhz(tanker.get("freq_mhz"))
    if direct is not None:
        return direct
    try:
        catalog = fetch_opus_tankers(config, opus=opus)
    except Exception:
        catalog = []
    want_id = tanker.get("id")
    if want_id is None:
        want_id = tanker.get("tanker_id")
    want_cs = normalize_tanker_name(
        str(tanker.get("callsign") or tanker.get("tanker_callsign") or "")
    )
    want_track = str(
        tanker.get("track") or tanker.get("tanker_track") or ""
    ).strip().upper()
    if want_id is None and not want_cs:
        return None
    exact_cs: float | None = None
    want_id_s = "" if want_id is None else str(want_id).strip()
    for row in catalog:
        row_mhz = _positive_mhz(row.get("freq_mhz"))
        if row_mhz is None:
            continue
        row_id = row.get("id")
        if want_id_s and row_id is not None and str(row_id).strip() == want_id_s:
            return row_mhz
        row_cs = normalize_tanker_name(str(row.get("callsign") or ""))
        if want_cs and row_cs == want_cs:
            row_track = str(row.get("track") or "").strip().upper()
            if want_track and row_track == want_track:
                return row_mhz
            if exact_cs is None:
                exact_cs = row_mhz
    return exact_cs


def _caoc_tanker_units(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    radar = atc_phrase.fetch_caoc_radar(config or {})
    if not radar:
        return []
    out: list[dict[str, Any]] = []
    for unit in atc_phrase.caoc_air_units(list(radar.get("units") or [])):
        obj = re.sub(
            r"[\s_\-]+",
            "",
            str(unit.get("objectName") or unit.get("object_name") or "").upper(),
        )
        if "KC135" not in obj and "KC130" not in obj and "TANKER" not in obj:
            name = str(unit.get("name") or unit.get("groupName") or "").upper()
            if not any(b.upper() in name for b in TANKER_BRANDS if b != "tanker"):
                continue
        out.append(unit)
    return out


def _match_live_unit(row: dict[str, Any], units: list[dict[str, Any]]) -> dict[str, Any] | None:
    want = normalize_tanker_name(str(row.get("callsign") or ""))
    if not want:
        return None
    for unit in units:
        blob = normalize_tanker_name(
            " ".join(
                str(unit.get(k) or "")
                for k in ("name", "groupName", "flightLabel", "unitCallsign", "unitName")
            )
        )
        if want and want in blob:
            return unit
    return None


def _enrich_live(
    row: dict[str, Any],
    unit: dict[str, Any] | None,
    config: dict[str, Any] | None,
    *,
    opus: Any = None,
    own_ll: tuple[float, float] | None = None,
) -> dict[str, Any]:
    out = dict(row)
    if unit is None:
        return out
    fix = atc_phrase.bullseye_for_caoc_unit(unit, config or {}, opus=opus)
    if fix:
        out["bullseye"] = fix
        out["lat"] = fix.get("lat")
        out["lon"] = fix.get("lon")
    try:
        out["heading_deg"] = float(unit.get("headingDeg") or unit.get("heading") or 0) % 360.0
    except (TypeError, ValueError):
        pass
    try:
        alt_m = float(unit.get("altMeters") or 0)
        if alt_m > 0:
            out["live_alt_ft"] = int(round(alt_m * 3.28084))
    except (TypeError, ValueError):
        pass
    if own_ll and out.get("lat") is not None and out.get("lon") is not None:
        try:
            import picture_labels as pl

            out["distance_nm"] = atc_phrase._haversine_nm(
                own_ll[0], own_ll[1], float(out["lat"]), float(out["lon"])
            )
            out["bearing_deg"] = atc_phrase.magnetic_bearing_deg(
                own_ll[0],
                own_ll[1],
                float(out["lat"]),
                float(out["lon"]),
                config=config,
            )
            aspect = pl.aspect_to_fighter(
                own_lat=own_ll[0],
                own_lon=own_ll[1],
                tgt_lat=float(out["lat"]),
                tgt_lon=float(out["lon"]),
                tgt_heading=out.get("heading_deg"),
            )
            if aspect:
                out["aspect"] = aspect
        except (TypeError, ValueError):
            pass
    out["live"] = True
    return out


def _find_named_row(
    catalog: list[dict[str, Any]],
    want: str,
    *,
    boom_only: bool,
    units: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    units = units or []
    exact: dict[str, Any] | None = None
    brand_hit: dict[str, Any] | None = None
    brand = re.sub(r"\d+$", "", want)
    for row in catalog:
        key = normalize_tanker_name(str(row.get("callsign") or ""))
        unit = _match_live_unit(row, units)
        if boom_only and not _row_is_boom(row, unit):
            continue
        if key == want:
            exact = row
            break
        if want and (want in key or key.startswith(brand)):
            if brand_hit is None:
                brand_hit = row
    return exact or brand_hit


def choose_catalog_tanker(
    catalog: list[dict[str, Any]],
    *,
    name: str | None = None,
    boom_only: bool = True,
    units: list[dict[str, Any]] | None = None,
    own_ll: tuple[float, float] | None = None,
    config: dict[str, Any] | None = None,
    opus: Any = None,
) -> dict[str, Any] | None:
    """Pick a catalog row. boom_only never returns KC-130 / MPRS."""
    units = units or []
    want = normalize_tanker_name(name)
    chosen: dict[str, Any] | None = None
    if want:
        chosen = _find_named_row(catalog, want, boom_only=boom_only, units=units)

    if chosen is None:
        pool = [
            r
            for r in catalog
            if (not boom_only) or _row_is_boom(r, _match_live_unit(r, units))
        ]
        if not pool:
            return None
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in pool:
            unit = _match_live_unit(row, units)
            if boom_only and not _row_is_boom(row, unit):
                continue
            if unit is None or own_ll is None:
                continue
            live = _enrich_live(row, unit, config, opus=opus, own_ll=own_ll)
            dist = live.get("distance_nm")
            if dist is None:
                continue
            scored.append((float(dist), row))
        if scored:
            scored.sort(key=lambda x: x[0])
            chosen = scored[0][1]
        else:
            chosen = pool[0]
    return chosen


def pick_tanker(
    config: dict[str, Any] | None,
    *,
    opus: Any = None,
    state: dict[str, Any] | None = None,
    name: str | None = None,
    own_ll: tuple[float, float] | None = None,
    boom_only: bool = True,
) -> dict[str, Any] | None:
    """
    Choose a tanker: named request, then nearest live boom KC-135, then catalog.
    F-16 C2 requests never fall back to KC-130 or MPRS.
    """
    catalog = fetch_opus_tankers(config, opus=opus)
    if not catalog:
        return None
    units = _caoc_tanker_units(config)
    want = str(name or "").strip()
    if not want and isinstance(state, dict):
        remembered = str(state.get("tanker_callsign") or state.get("tanker_id") or "")
        if remembered:
            row = _find_named_row(
                catalog,
                normalize_tanker_name(remembered),
                boom_only=boom_only,
                units=units,
            )
            if row is not None:
                want = remembered
    chosen = choose_catalog_tanker(
        catalog,
        name=want or None,
        boom_only=boom_only,
        units=units,
        own_ll=own_ll,
        config=config,
        opus=opus,
    )
    if chosen is None:
        return None
    unit = _match_live_unit(chosen, units)
    if boom_only and not _row_is_boom(chosen, unit):
        return None
    return _enrich_live(chosen, unit, config, opus=opus, own_ll=own_ll)


def remember_tanker(
    state: dict[str, Any] | None,
    tanker: dict[str, Any] | None,
    *,
    config: dict[str, Any] | None = None,
    opus: Any = None,
) -> None:
    if not isinstance(state, dict):
        return
    if not tanker:
        for key in _TANKER_STATE_KEYS:
            state.pop(key, None)
        return
    state["tanker_id"] = tanker.get("id")
    state["tanker_callsign"] = str(tanker.get("callsign") or "")
    state["tanker_aircraft"] = str(tanker.get("aircraft") or "")
    state["tanker_track"] = str(tanker.get("track") or "")
    state["tanker_tcn"] = str(tanker.get("tcn") or "")
    mhz = _positive_mhz(tanker.get("freq_mhz"))
    if mhz is None:
        mhz = catalog_freq_for_tanker(tanker, config, opus=opus)
    state["tanker_freq_mhz"] = mhz
    if not state.get("tanker_phase") or state.get("tanker_phase") == PHASE_DEPARTED:
        state["tanker_phase"] = PHASE_JOIN


def tanker_target_mhz(state: dict[str, Any] | None) -> float | None:
    if not isinstance(state, dict):
        return None
    raw = state.get("tanker_freq_mhz")
    try:
        mhz = float(raw)
    except (TypeError, ValueError):
        return None
    return mhz if mhz > 0 else None


def effective_tanker_mhz(
    state: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    *,
    opus: Any = None,
) -> float | None:
    """
    Frequency the pilot should be on for the active / nearest tanker.

    Prefers the remembered Opus tanker from the last request / rejoin. If that
    row has no UHF stored, looks up the same callsign / id / track. Never
    substitutes another published tanker (Fly used to show TEXACO 1's 322.3
    after Blackjack sent you to TEXACO 2). Anonymous fallback is only when no
    tanker is assigned. None keeps the airports.json placeholder.
    """
    live = tanker_target_mhz(state)
    if live is not None:
        return live
    assigned = isinstance(state, dict) and bool(
        state.get("tanker_callsign")
        or state.get("tanker_id")
        or state.get("tanker_track")
    )
    if assigned:
        return catalog_freq_for_tanker(
            {
                "id": state.get("tanker_id"),
                "callsign": state.get("tanker_callsign"),
                "track": state.get("tanker_track"),
            },
            config,
            opus=opus,
        )
    try:
        freqs = tanker_freqs_mhz(config, opus=opus)
    except Exception:
        freqs = []
    return float(freqs[0]) if freqs else None


def extract_tanker_name(text: str) -> str | None:
    """Pull 'texaco 1' / 'shell' from a transcript."""
    blob = re.sub(r"[^a-z0-9\s]", " ", (text or "").casefold())
    blob = re.sub(r"\s+", " ", blob).strip()
    words = {
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
    }
    m = re.search(
        r"\b(texaco|shell|arco|esso)\s*(one|two|three|four|five|six|seven|eight|nine|\d+)?\b",
        blob,
    )
    if not m:
        return None
    brand = m.group(1)
    num = m.group(2)
    if num:
        num = words.get(num, num)
        return f"{brand} {num}"
    return brand


def _tanker_altitude_speech(tanker: dict[str, Any] | None) -> str:
    return (
        atc_phrase.speak_altitude_value(
            (tanker or {}).get("live_alt_ft") or (tanker or {}).get("altitude"),
            prefer_fl_below=1000,
        )
        or ""
    )


def speak_tanker_braa(bearing: int, range_nm: int) -> str:
    """Spoken magnetic BRAA — 'braw', not letter-by-letter B-R-A-A."""
    brg = max(0, min(360, int(bearing))) % 360
    rng = max(0, int(range_nm))
    return (
        f"braw {atc_phrase.speak_digits(f'{brg:03d}')}, "
        f"{atc_phrase.speak_natural_number(rng)}"
    )


def _tanker_braa_bits(tanker: dict[str, Any] | None) -> list[str]:
    """Bearing, range, altitude. No hot/cold aspect."""
    row = tanker or {}
    bits: list[str] = []
    brg = row.get("bearing_deg")
    rng = row.get("distance_nm")
    try:
        if brg is not None and rng is not None:
            bits.append(speak_tanker_braa(int(round(float(brg))), int(round(float(rng)))))
    except (TypeError, ValueError):
        pass
    alt = _tanker_altitude_speech(row)
    if alt:
        bits.append(alt)
    return bits


def _c2_tanker_open(agency: str, callsign: str, tanker: dict[str, Any] | None) -> tuple[str, str, str]:
    cs = atc_phrase.speak_callsign(callsign)
    ag = atc_phrase.speak_agency_name(agency)
    tcs = speak_tanker_callsign(str((tanker or {}).get("callsign") or "tanker"))
    return cs, ag, tcs


def build_c2_tanker_vectors(
    agency: str,
    callsign: str,
    tanker: dict[str, Any] | None,
) -> str:
    """
    Blackjack / Bandsaw: tanker, type, AR track, BRAA with altitude.
    TACAN / frequency / bullseye are separate on-request calls.
    """
    cs, ag, tcs = _c2_tanker_open(agency, callsign, tanker)
    if not tanker:
        return (
            f"{cs}, {ag}, unable tanker, no KC-135 boom published for this theater."
        )
    bits = [f"{cs}, {ag}, tanker {tcs}"]
    ac = str(tanker.get("aircraft") or "").strip()
    if ac:
        if tanker_is_f16_boom(tanker):
            bits.append("KC-135 boom")
        else:
            bits.append(ac)
    track = speak_aar_track(str(tanker.get("track") or ""))
    if track:
        bits.append(f"track {track}")
    bits.extend(_tanker_braa_bits(tanker))
    body = ", ".join(bits) + "."
    return f"{body} Frequency change approved."


def build_tanker_tacan_reply(
    agency: str,
    callsign: str,
    tanker: dict[str, Any] | None,
) -> str:
    cs, ag, tcs = _c2_tanker_open(agency, callsign, tanker)
    if not tanker:
        return f"{cs}, {ag}, unable TACAN, no tanker."
    tcn = speak_tacan(str(tanker.get("tcn") or ""))
    if not tcn:
        return f"{cs}, {ag}, {tcs}, unable TACAN, none published."
    return f"{cs}, {ag}, {tcs}, TACAN {tcn}."


def build_tanker_freq_reply(
    agency: str,
    callsign: str,
    tanker: dict[str, Any] | None,
) -> str:
    cs, ag, tcs = _c2_tanker_open(agency, callsign, tanker)
    if not tanker:
        return f"{cs}, {ag}, unable tanker frequency, no tanker."
    try:
        mhz = float(tanker.get("freq_mhz") or 0)
    except (TypeError, ValueError):
        mhz = 0.0
    if mhz <= 0:
        return f"{cs}, {ag}, {tcs}, unable tanker frequency, none published."
    return f"{cs}, {ag}, {tcs}, tanker frequency {atc_phrase.speak_freq(mhz)}."


def build_tanker_bullseye_reply(
    agency: str,
    callsign: str,
    tanker: dict[str, Any] | None,
) -> str:
    cs, ag, tcs = _c2_tanker_open(agency, callsign, tanker)
    if not tanker:
        return f"{cs}, {ag}, unable tanker bullseye, no tanker."
    be = tanker.get("bullseye") if isinstance(tanker.get("bullseye"), dict) else None
    if be and be.get("spoken"):
        return f"{cs}, {ag}, {tcs} is {be['spoken']}."
    if be and be.get("bearing") is not None and be.get("range_nm") is not None:
        spoken = atc_phrase.speak_picture_bullseye(
            str(be.get("name") or "ELVIS"),
            int(be["bearing"]),
            int(be["range_nm"]),
        )
        return f"{cs}, {ag}, {tcs} is {spoken}."
    return f"{cs}, {ag}, {tcs}, unable bullseye, no live track."


# DCS tanker radio owns these — SRS must not speak "cleared contact" or the
# boom AI never sees the menu call.
DCS_TANKER_ACTIONS = frozenset(
    {
        "tanker_astern",
        "tanker_observation",
        "tanker_contact",
        "tanker_disconnect",
        "tanker_depart",
        "tanker_dcs_precontact",
        "tanker_dcs_abort",
    }
)

C2_TANKER_INFO_ACTIONS = frozenset(
    {"tanker_tacan", "tanker_freq", "tanker_bullseye"}
)


def tanker_awaiting_return(state: dict[str, Any] | None) -> bool:
    """True after C2 sent them to the tanker, until they check back in."""
    if not isinstance(state, dict):
        return False
    if tanker_needs_c2_checkin(state):
        return True
    phase = str(state.get("tanker_phase") or "").strip()
    return phase in {
        PHASE_JOIN,
        PHASE_OBSERVATION,
        PHASE_ASTERN,
        PHASE_CONTACT,
        PHASE_RIGHT,
    }


def tanker_needs_c2_checkin(state: dict[str, Any] | None) -> bool:
    """C2 sent them to AAR; they still owe Blackjack / Bandsaw a check-in."""
    return bool(isinstance(state, dict) and state.get("tanker_needs_c2_checkin"))


def tanker_overlay_active(state: dict[str, Any] | None) -> bool:
    """True while the tanker is a side trip parked off the C2 timeline."""
    return bool(isinstance(state, dict) and state.get("tanker_overlay"))


def element_seats(seat: Any) -> tuple[int, int]:
    """Lead element is 1-2; second element is 3-4; then 5-6, …"""
    try:
        n = int(seat)
    except (TypeError, ValueError):
        n = 1
    if n <= 0:
        n = 1
    start = n - 1 if n % 2 == 0 else n
    return (start, start + 1)


def snapshot_seat_state(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    out: dict[str, Any] = {}
    for key in SEAT_STATE_KEYS:
        if key in state:
            out[key] = state[key]
    return out


def apply_seat_state(
    state: dict[str, Any] | None, local: dict[str, Any] | None
) -> None:
    if not isinstance(state, dict):
        return
    local = local if isinstance(local, dict) else {}
    for key in SEAT_STATE_KEYS:
        if key in local:
            state[key] = local[key]
        else:
            state.pop(key, None)


def strip_seat_state(state: dict[str, Any] | None) -> None:
    if not isinstance(state, dict):
        return
    for key in SEAT_STATE_KEYS:
        state.pop(key, None)


def copy_overlay(dst: dict[str, Any], src: dict[str, Any] | None) -> None:
    """Share AAR parking with the other jet in the element; keep boom chat local."""
    src = src if isinstance(src, dict) else {}
    for key in _OVERLAY_COPY_KEYS:
        if key in src:
            dst[key] = src[key]
        else:
            dst.pop(key, None)


def park_tanker_index(engine: Any) -> None:
    """Point the cursor at the tanker step without rewriting resume."""
    if engine is None:
        return
    state = engine.state if isinstance(getattr(engine, "state", None), dict) else None
    if state is None:
        return
    for i, step in enumerate(list(getattr(engine, "steps", None) or [])):
        if is_tanker_step(step):
            state["index"] = i
            return


def is_tanker_step(step: dict[str, Any] | None) -> bool:
    """Timeline parking spot for AAR — not part of the C2 sequence."""
    if not isinstance(step, dict):
        return False
    if str(step.get("channel") or "").strip().lower() == "tanker":
        return True
    tmpl = str(step.get("template") or "").strip().lower()
    return tmpl == "tanker" or tmpl.startswith("tanker_")


_AAR_CHANNELS = frozenset(
    {
        "tanker",
        "blackjack",
        "bandsaw",
        "joshua",
        "ops",
        "control_east",
        "control_west",
        "center",
    }
)


def step_allows_aar(step: dict[str, Any] | None) -> bool:
    """True when this cursor can honestly be on an AAR side trip."""
    if is_tanker_step(step):
        return True
    ch = str((step or {}).get("channel") or "").strip().lower()
    return ch in _AAR_CHANNELS


def clear_aar_state(state: dict[str, Any] | None) -> None:
    """Drop overlay, boom chat, and rejoin — the ramp is not Texaco."""
    strip_seat_state(state)


def reconcile_aar_overlay(engine: Any) -> bool:
    """
    flow_state.json / a leftover request can keep tanker_overlay and
    tanker_rejoined while the timeline is back on Delivery. Strip that.
    """
    if engine is None:
        return False
    state = engine.state if isinstance(getattr(engine, "state", None), dict) else None
    if state is None:
        return False
    dirty = tanker_overlay_active(state) or has_rejoined(state) or bool(
        state.get("tanker_chat") or state.get("tanker_chat_last_spoke")
    )
    if not dirty:
        return False
    steps = list(getattr(engine, "steps", None) or [])
    idx = int(state.get("index") or 0)
    cur = steps[idx] if 0 <= idx < len(steps) else None
    if step_allows_aar(cur):
        return False
    clear_aar_state(state)
    if hasattr(engine, "save_state"):
        engine.save_state()
    return True


def should_skip_tanker_step(
    step: dict[str, Any] | None,
    state: dict[str, Any] | None = None,
) -> bool:
    """
    Skip tanker timeline steps unless this sortie is actually on AAR.

    Request tanker jumps onto the parking spot; Next after Blackjack must not
    land there and demand Texaco's UHF before Bandsaw / range exit.
    """
    if not is_tanker_step(step):
        return False
    return not tanker_overlay_active(state)


def enter_tanker_overlay(engine: Any) -> None:
    """
    Leave the C2 cursor parked and sit on the tanker step (if the flow has one).

    The tanker is a side trip: Blackjack / Bandsaw stay where they were until
    the jet retunes or checks back in.
    """
    if engine is None:
        return
    state = engine.state if isinstance(getattr(engine, "state", None), dict) else None
    if state is None:
        return
    steps = list(getattr(engine, "steps", None) or [])
    idx = int(state.get("index") or 0)
    cur = steps[idx] if 0 <= idx < len(steps) else None
    already = tanker_overlay_active(state)
    state["tanker_overlay"] = True
    state["tanker_needs_c2_checkin"] = True
    if not already and not is_tanker_step(cur):
        state["tanker_resume_index"] = idx
        state["tanker_resume_step_id"] = str((cur or {}).get("id") or "")
        state["tanker_resume_channel"] = str((cur or {}).get("channel") or "")
    for i, step in enumerate(steps):
        if is_tanker_step(step):
            state["index"] = i
            break
    if hasattr(engine, "save_state"):
        engine.save_state()


def leave_tanker_overlay(
    engine: Any,
    agency: str | None = None,
    *,
    checkin: bool = False,
) -> str:
    """
    End the tanker side trip and land on Blackjack, Bandsaw, or Joshua.

    `agency` is the live radio (tune or addressed). Checking in clears the
    AAR flag; a tune-only return keeps it so C2 still gets a continue call.
    """
    if engine is None:
        return str(agency or "blackjack").strip().lower() or "blackjack"
    state = engine.state if isinstance(getattr(engine, "state", None), dict) else {}
    steps = list(getattr(engine, "steps", None) or [])
    resume = state.pop("tanker_resume_index", None)
    state.pop("tanker_resume_step_id", None)
    resume_ch = str(state.pop("tanker_resume_channel", "") or "").strip().lower()
    state["tanker_overlay"] = False
    state.pop("tanker_seen_tune", None)
    ch = str(agency or "").strip().lower()
    if ch not in ("blackjack", "bandsaw", "joshua", "control_east", "control_west"):
        ch = resume_ch if resume_ch in (
            "blackjack", "bandsaw", "joshua", "control_east", "control_west"
        ) else "blackjack"
    if checkin:
        state["tanker_needs_c2_checkin"] = False
        apply_tanker_phase(state, PHASE_DEPARTED)
        mark_rejoined(state, False)

    def _seek_template(name: str) -> bool:
        want = str(name or "").strip()
        if not want:
            return False
        for i, step in enumerate(steps):
            if str(step.get("template") or "") == want:
                state["index"] = i
                return True
        return False

    def _seek_channel(
        want: str, *, avoid_templates: frozenset[str] = frozenset()
    ) -> bool:
        for i, step in enumerate(steps):
            if is_tanker_step(step):
                continue
            if str(step.get("channel") or "").strip().lower() != want:
                continue
            tmpl = str(step.get("template") or "")
            if tmpl in avoid_templates:
                continue
            state["index"] = i
            return True
        return False

    if ch == "bandsaw":
        if not _seek_template("bandsaw_check_in"):
            _seek_channel("bandsaw")
    elif ch == "joshua":
        if not _seek_template("joshua_check_in"):
            _seek_channel("joshua")
    elif ch in ("control_east", "control_west"):
        if not _seek_template("control_check_in"):
            _seek_channel(ch)
    else:
        restored = False
        try:
            ri = int(resume) if resume is not None else None
        except (TypeError, ValueError):
            ri = None
        if ri is not None and 0 <= ri < len(steps):
            rstep = steps[ri]
            rch = str(rstep.get("channel") or "").strip().lower()
            if rch == "blackjack" and not is_tanker_step(rstep):
                state["index"] = ri
                restored = True
        if not restored:
            if not _seek_template("bj_check_in"):
                _seek_channel(
                    "blackjack",
                    avoid_templates=frozenset({"bj_range_exit"}),
                )
    if hasattr(engine, "_advance_past_skippable"):
        engine._advance_past_skippable()
    if hasattr(engine, "save_state"):
        engine.save_state()
    return ch


def note_tanker_tune(state: dict[str, Any] | None, tuned: str | None) -> bool:
    """
    Follow the radio during an AAR side trip.

    Returns True when the cursor should leave tanker for Blackjack / Bandsaw / Joshua
    (they have been on tanker UHF, then retuned to C2).
    """
    if not tanker_overlay_active(state):
        return False
    ch = str(tuned or "").strip().lower()
    if ch == "tanker":
        state["tanker_seen_tune"] = True
        return False
    if ch not in ("blackjack", "bandsaw", "joshua", "control_east", "control_west"):
        return False
    return bool(state.get("tanker_seen_tune"))


def build_tanker_return_checkin(
    agency: str,
    callsign: str,
    *,
    alpha_bullseye: str | None = None,
) -> str:
    """Back on Blackjack / Bandsaw after AAR."""
    ch = str(agency or "blackjack").strip().lower()
    if ch == "blackjack":
        return atc_phrase.build_blackjack_continue(
            callsign, alpha_bullseye=alpha_bullseye
        )
    cs = atc_phrase.speak_callsign(callsign)
    ag = atc_phrase.speak_agency_name(ch)
    if alpha_bullseye:
        return f"{cs}, {ag}, radar contact {alpha_bullseye}. Continue."
    return f"{cs}, {ag}, radar contact. Continue."


def dcs_tanker_radio_hint(action: str) -> str:
    """What to use in the DCS tanker comms menu (not an SRS clearance)."""
    key = str(action or "").strip().lower()
    if key in ("tanker_disconnect", "tanker_depart", "tanker_dcs_abort"):
        return "DCS tanker radio — Abort refueling / disconnect"
    return "DCS tanker radio — Ready pre-contact (cleared contact)"


def build_tanker_depart_reply(
    callsign: str, tanker: dict[str, Any] | None
) -> str:
    """Boom goodbye after they call exit high/low or thank you for the gas."""
    cs = atc_phrase.speak_callsign(callsign)
    tcs = speak_tanker_callsign(str((tanker or {}).get("callsign") or "Tanker"))
    return atc_phrase._pick(
        f"{cs}, {tcs}, copy, you're cleared off. Thanks for flying with us.",
        f"{cs}, {tcs}, copy exit, looking good. See you next time.",
        f"{cs}, {tcs}, roger, thanks for the trade. You're cleared off.",
    )


def build_tanker_check_in(callsign: str, tanker: dict[str, Any] | None) -> str:
    """Missing official join: cleared rejoin left, sometimes left observation."""
    cs = atc_phrase.speak_callsign(callsign)
    tcs = speak_tanker_callsign(str((tanker or {}).get("callsign") or "Tanker"))
    join = atc_phrase._pick(
        "cleared rejoin left",
        "cleared rejoin left observation",
    )
    return f"{cs}, {tcs}, {join}."


def apply_tanker_phase(state: dict[str, Any] | None, phase: str) -> None:
    if not isinstance(state, dict):
        return
    state["tanker_phase"] = phase
    if phase in {PHASE_DEPARTED, PHASE_NONE, ""}:
        mark_rejoined(state, False)


def mark_rejoined(state: dict[str, Any] | None, value: bool = True) -> None:
    """Set after 'cleared rejoin left'. Cleared when they leave the tanker."""
    if not isinstance(state, dict):
        return
    if value:
        state["tanker_rejoined"] = True
        return
    state.pop("tanker_rejoined", None)
    state.pop("tanker_chat_in_range_since", None)
    state.pop("tanker_chat_dwell_s", None)
    state.pop("tanker_chat_auto_done", None)


def has_rejoined(state: dict[str, Any] | None) -> bool:
    return bool(isinstance(state, dict) and state.get("tanker_rejoined"))


def boom_chat_gate(
    *,
    rejoined: bool,
    chat_open: bool,
    dist_nm: float | None,
    receivers: int,
    auto_done: bool,
    now: float,
    in_range_since: float | None,
    min_nm: float = CHAT_MIN_NM,
    max_nm: float = CHAT_MAX_NM,
    dwell_s: float = CHAT_DWELL_S,
    rearm_nm: float = CHAT_REARM_NM,
) -> dict[str, Any]:
    """
    Decide whether Texaco should start boom chat.

    Ready only after rejoin, with ownship 0.1–0.5 NM from the tanker and at
    least one fighter in that envelope (you count). Hold 30–60 s in the
    envelope before Texaco talks. Leaving past rearm_nm clears auto_done so
    a later plug can chat again.
    """
    reason = ""
    in_range = False
    clear_auto = False
    set_since: float | None = in_range_since
    ready = False
    if chat_open:
        return {
            "ready": False,
            "in_range": False,
            "reason": "chat already open",
            "clear_auto_done": False,
            "in_range_since": in_range_since,
        }
    if not rejoined:
        return {
            "ready": False,
            "in_range": False,
            "reason": "waiting — request rejoin first",
            "clear_auto_done": False,
            "in_range_since": None,
        }
    if dist_nm is None:
        return {
            "ready": False,
            "in_range": False,
            "reason": "waiting — no tanker track",
            "clear_auto_done": False,
            "in_range_since": None,
        }
    if dist_nm > rearm_nm:
        clear_auto = True
        return {
            "ready": False,
            "in_range": False,
            "reason": f"waiting — {dist_nm:.2f} NM from tanker",
            "clear_auto_done": True,
            "in_range_since": None,
        }
    in_range = min_nm <= dist_nm <= max_nm
    if not in_range:
        if dist_nm < min_nm:
            reason = f"inside {min_nm:.1f} NM — holding"
        else:
            reason = f"{dist_nm:.2f} NM — close to {max_nm:.1f} NM"
        return {
            "ready": False,
            "in_range": False,
            "reason": reason,
            "clear_auto_done": False,
            "in_range_since": None,
        }
    if receivers < 1:
        return {
            "ready": False,
            "in_range": True,
            "reason": "in range, no receiver on the boom",
            "clear_auto_done": False,
            "in_range_since": None,
        }
    if auto_done:
        return {
            "ready": False,
            "in_range": True,
            "reason": f"in range {dist_nm:.2f} NM — already chatted",
            "clear_auto_done": False,
            "in_range_since": in_range_since,
        }
    if in_range_since is None:
        set_since = now
        return {
            "ready": False,
            "in_range": True,
            "reason": f"in range {dist_nm:.2f} NM — holding {dwell_s:.0f}s",
            "clear_auto_done": False,
            "in_range_since": set_since,
        }
    held = now - float(in_range_since)
    if held < dwell_s:
        left = max(0.0, dwell_s - held)
        return {
            "ready": False,
            "in_range": True,
            "reason": f"in range {dist_nm:.2f} NM — {left:.0f}s",
            "clear_auto_done": False,
            "in_range_since": in_range_since,
        }
    n = int(receivers)
    who = "receiver" if n == 1 else "receivers"
    return {
        "ready": True,
        "in_range": True,
        "reason": f"on the boom · {dist_nm:.2f} NM · {n} {who}",
        "clear_auto_done": False,
        "in_range_since": in_range_since,
    }


def _unit_latlon(
    unit: dict[str, Any] | None,
    config: dict[str, Any] | None,
    *,
    opus: Any = None,
) -> tuple[float, float] | None:
    if not unit:
        return None
    try:
        return atc_phrase.caoc_xz_to_ll(float(unit["xMeters"]), float(unit["zMeters"]))
    except (KeyError, TypeError, ValueError):
        pass
    try:
        fix = atc_phrase.bullseye_for_caoc_unit(unit, config or {}, opus=opus)
        if fix and fix.get("lat") is not None and fix.get("lon") is not None:
            return (float(fix["lat"]), float(fix["lon"]))
    except (TypeError, ValueError):
        return None
    return None


def count_boom_receivers(
    config: dict[str, Any] | None,
    tanker: dict[str, Any] | None,
    *,
    own_ll: tuple[float, float] | None = None,
    opus: Any = None,
    max_nm: float = CHAT_MAX_NM,
) -> int:
    """Fighters (including you) within max_nm of the tanker."""
    tlat = tanker.get("lat") if isinstance(tanker, dict) else None
    tlon = tanker.get("lon") if isinstance(tanker, dict) else None
    try:
        tlat_f = float(tlat)
        tlon_f = float(tlon)
    except (TypeError, ValueError):
        return 0
    seen: set[tuple[float, float]] = set()
    n = 0
    if own_ll:
        try:
            d = atc_phrase._haversine_nm(own_ll[0], own_ll[1], tlat_f, tlon_f)
            if d <= max_nm:
                n += 1
                seen.add((round(own_ll[0], 5), round(own_ll[1], 5)))
        except (TypeError, ValueError):
            pass
    radar = atc_phrase.fetch_caoc_radar(config or {})
    units = atc_phrase.caoc_air_units(list((radar or {}).get("units") or []))
    for unit in units:
        if atc_phrase.caoc_unit_is_picture_fixture(unit):
            continue
        ll = _unit_latlon(unit, config, opus=opus)
        if not ll:
            continue
        key = (round(ll[0], 5), round(ll[1], 5))
        if key in seen:
            continue
        try:
            d = atc_phrase._haversine_nm(ll[0], ll[1], tlat_f, tlon_f)
        except (TypeError, ValueError):
            continue
        if d <= max_nm:
            n += 1
            seen.add(key)
    return n


def tick_boom_chat(
    state: dict[str, Any] | None,
    *,
    dist_nm: float | None,
    receivers: int,
    chat_open: bool,
    now: float | None = None,
) -> dict[str, Any]:
    """Update dwell / re-arm flags and return boom_chat_gate result."""
    if not isinstance(state, dict):
        state = {}
    now_f = time.time() if now is None else float(now)
    since = state.get("tanker_chat_in_range_since")
    try:
        since_f = float(since) if since is not None else None
    except (TypeError, ValueError):
        since_f = None
    try:
        dwell = float(state.get("tanker_chat_dwell_s") or 0)
    except (TypeError, ValueError):
        dwell = 0.0
    if dwell < CHAT_DWELL_MIN_S or dwell > CHAT_DWELL_MAX_S:
        dwell = random.uniform(CHAT_DWELL_MIN_S, CHAT_DWELL_MAX_S)
        state["tanker_chat_dwell_s"] = dwell
    gate = boom_chat_gate(
        rejoined=has_rejoined(state),
        chat_open=chat_open,
        dist_nm=dist_nm,
        receivers=int(receivers or 0),
        auto_done=bool(state.get("tanker_chat_auto_done")),
        now=now_f,
        in_range_since=since_f,
        dwell_s=dwell,
    )
    if gate.get("clear_auto_done"):
        state.pop("tanker_chat_auto_done", None)
    if gate.get("in_range_since") is None:
        state.pop("tanker_chat_in_range_since", None)
        state.pop("tanker_chat_dwell_s", None)
    else:
        state["tanker_chat_in_range_since"] = gate["in_range_since"]
        state["tanker_chat_dwell_s"] = dwell
    return gate
