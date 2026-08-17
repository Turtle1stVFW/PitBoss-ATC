"""
Opus tanker tracks + official boom AAR comms (ATP-56 / USAF KC-135).

F-16s use the KC-135 boom — not MPRS pods and not the KC-130. Blackjack /
Bandsaw give vectors from the theater tanker list plus the live CAOC track.
On tanker freq this app only fills what DCS does not say (rejoin left,
observation). Cleared contact / disconnect stay on the DCS tanker radio
so the boom AI still works.
"""

from __future__ import annotations

import math
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
)

# Receiver position in the boom pattern.
PHASE_NONE = ""
PHASE_JOIN = "join"
PHASE_OBSERVATION = "observation"
PHASE_ASTERN = "astern"
PHASE_CONTACT = "contact"
PHASE_RIGHT = "right"
PHASE_DEPARTED = "departed"


def tanker_is_f16_boom(row: dict[str, Any] | None) -> bool:
    """True for KC-135 boom (not MPRS, not KC-130)."""
    ac = re.sub(r"[\s_\-]+", "", str((row or {}).get("aircraft") or "").upper())
    if not ac:
        return False
    if "MPRS" in ac:
        return False
    if "KC130" in ac:
        return False
    return "KC135" in ac


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
    """'ARLNS' → 'A R L N S'; 'AR-625H/L' → letters and digits."""
    raw = str(track or "").strip().upper()
    if not raw:
        return ""
    bits: list[str] = []
    for ch in raw:
        if ch.isspace() or ch in "-_/":
            continue
        if ch.isdigit():
            bits.append(atc_phrase.speak_digits(ch))
        elif ch.isalpha():
            if ch == "X":
                bits.append("x-ray")
            else:
                bits.append(ch)
    return " ".join(bits)


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
        mhz = row.get("freq_mhz")
        if mhz is None:
            continue
        try:
            val = float(mhz)
        except (TypeError, ValueError):
            continue
        if val > 0 and not any(abs(val - x) < 0.01 for x in out):
            out.append(val)
    return out


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
            out["distance_nm"] = atc_phrase._haversine_nm(
                own_ll[0], own_ll[1], float(out["lat"]), float(out["lon"])
            )
            out["bearing_deg"] = _bearing_deg(
                own_ll[0], own_ll[1], float(out["lat"]), float(out["lon"])
            )
        except (TypeError, ValueError):
            pass
    out["live"] = True
    return out


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(rlat2)
    y = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(
        dlon
    )
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


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
    """
    catalog = fetch_opus_tankers(config, opus=opus)
    if not catalog:
        return None
    units = _caoc_tanker_units(config)
    want = normalize_tanker_name(name)
    if not want and isinstance(state, dict):
        want = normalize_tanker_name(
            str(state.get("tanker_callsign") or state.get("tanker_id") or "")
        )

    chosen: dict[str, Any] | None = None
    if want:
        for row in catalog:
            if normalize_tanker_name(str(row.get("callsign") or "")) == want:
                chosen = row
                break
            if want in normalize_tanker_name(str(row.get("callsign") or "")):
                chosen = row
                break
        if chosen is None:
            # Brand only (TEXACO) — prefer boom of that brand.
            brand = re.sub(r"\d+$", "", want)
            for row in catalog:
                if not normalize_tanker_name(str(row.get("callsign") or "")).startswith(
                    brand
                ):
                    continue
                if boom_only and not row.get("boom"):
                    continue
                chosen = row
                break

    if chosen is None:
        pool = [r for r in catalog if r.get("boom")] if boom_only else list(catalog)
        if not pool:
            pool = list(catalog)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in pool:
            unit = _match_live_unit(row, units)
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

    if chosen is None:
        return None
    unit = _match_live_unit(chosen, units)
    return _enrich_live(chosen, unit, config, opus=opus, own_ll=own_ll)


def remember_tanker(state: dict[str, Any] | None, tanker: dict[str, Any] | None) -> None:
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
    mhz = tanker.get("freq_mhz")
    try:
        state["tanker_freq_mhz"] = float(mhz) if mhz is not None else None
    except (TypeError, ValueError):
        state["tanker_freq_mhz"] = None
    if not state.get("tanker_phase"):
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


def build_c2_tanker_vectors(
    agency: str,
    callsign: str,
    tanker: dict[str, Any] | None,
) -> str:
    """
    Blackjack / Bandsaw vectors to the published Opus tanker.

    Official C2 style: who, type, track, block, TACAN, freq, then live
    bullseye / steer if the CAOC track is up.
    """
    cs = atc_phrase.speak_callsign(callsign)
    ag = atc_phrase.speak_agency_name(agency)
    if not tanker:
        return (
            f"{cs}, {ag}, unable tanker, no Opus tanker published for this theater."
        )
    tcs = speak_tanker_callsign(str(tanker.get("callsign") or "tanker"))
    bits = [f"{cs}, {ag}, tanker {tcs}"]
    ac = str(tanker.get("aircraft") or "").strip()
    if ac:
        ac_say = "KC-135" if tanker_is_f16_boom(tanker) else ac
        if tanker_is_f16_boom(tanker):
            bits.append("KC-135 boom")
        else:
            bits.append(ac_say)
    track = speak_aar_track(str(tanker.get("track") or ""))
    if track:
        bits.append(f"air refueling track {track}")
    alt = atc_phrase.speak_altitude_value(
        tanker.get("live_alt_ft") or tanker.get("altitude"),
        prefer_fl_below=1000,
    )
    if alt:
        bits.append(alt)
    tcn = speak_tacan(str(tanker.get("tcn") or ""))
    if tcn:
        bits.append(f"TACAN {tcn}")
    mhz = tanker.get("freq_mhz")
    try:
        if mhz is not None and float(mhz) > 0:
            bits.append(f"tanker frequency {atc_phrase.speak_freq(float(mhz))}")
    except (TypeError, ValueError):
        pass
    be = tanker.get("bullseye") if isinstance(tanker.get("bullseye"), dict) else None
    if be and be.get("spoken"):
        bits.append(f"tanker is {be['spoken']}")
    elif be and be.get("bearing") is not None and be.get("range_nm") is not None:
        bits.append(
            atc_phrase.speak_picture_bullseye(
                str(be.get("name") or "ELVIS"),
                int(be["bearing"]),
                int(be["range_nm"]),
            )
        )
    hdg = tanker.get("heading_deg")
    try:
        if hdg is not None:
            bits.append(f"track {atc_phrase.speak_digits(f'{int(hdg) % 360:03d}')}")
    except (TypeError, ValueError):
        pass
    steer = tanker.get("bearing_deg")
    try:
        if steer is not None:
            bits.append(
                f"steer heading {atc_phrase.speak_digits(f'{int(steer) % 360:03d}')}"
            )
    except (TypeError, ValueError):
        pass
    bits.append(f"contact {tcs} when able")
    bits.append("intent to refuel and ready pre-contact on tanker radio")
    return ", ".join(bits) + "."


# DCS tanker radio owns these — SRS must not speak "cleared contact" or the
# boom AI never sees the menu call.
DCS_TANKER_ACTIONS = frozenset(
    {
        "tanker_astern",
        "tanker_contact",
        "tanker_disconnect",
        "tanker_depart",
        "tanker_dcs_precontact",
        "tanker_dcs_abort",
    }
)


def dcs_tanker_radio_hint(action: str) -> str:
    """What to use in the DCS tanker comms menu (not an SRS clearance)."""
    key = str(action or "").strip().lower()
    if key in ("tanker_disconnect", "tanker_depart", "tanker_dcs_abort"):
        return "DCS tanker radio — Abort refueling / disconnect"
    return "DCS tanker radio — Ready pre-contact (cleared contact)"


def build_tanker_check_in(callsign: str, tanker: dict[str, Any] | None) -> str:
    """
    Missing official join call. DCS still needs Intent to refuel on its menu.
    """
    cs = atc_phrase.speak_callsign(callsign)
    tcs = speak_tanker_callsign(str((tanker or {}).get("callsign") or "Tanker"))
    return (
        f"{cs}, {tcs}, identified, cleared rejoin left, "
        f"intent to refuel on tanker radio."
    )


def build_tanker_observation(callsign: str, tanker: dict[str, Any] | None) -> str:
    """
    Missing official observation call. Boom clearance stays on DCS radio.
    """
    cs = atc_phrase.speak_callsign(callsign)
    tcs = speak_tanker_callsign(str((tanker or {}).get("callsign") or "Tanker"))
    return (
        f"{cs}, {tcs}, cleared astern, proceed to pre-contact, "
        f"ready pre-contact on tanker radio for cleared contact."
    )


def apply_tanker_phase(state: dict[str, Any] | None, phase: str) -> None:
    if isinstance(state, dict):
        state["tanker_phase"] = phase
