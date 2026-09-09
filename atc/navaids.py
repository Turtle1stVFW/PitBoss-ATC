"""
Ground stations and divert fields, for the calls that need a real-world fix.

ATC does not talk in bullseye. Departure, Approach, Nellis Control, Joshua and
Center fix a radar contact off the nearest VOR/TACAN ("two five miles northeast
of Mormon Mesa"); only Blackjack and Bandsaw use ELVIS. This module owns the
station catalog behind that, plus the bearing/range math for vectors to a named
point or to the nearest suitable field.

Deliberately free of project imports at module scope: atc_phrase imports this,
so anything from the rest of the app is pulled in lazily inside a function.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import math
import re

NAVAIDS_PATH = Path(__file__).with_name("navaids.json")

# Beyond this a station is too far away to be a useful position reference.
DEFAULT_MAX_STATION_NM = 150.0
# Approx magnetic declination °E for NTTR (true → magnetic: subtract).
DEFAULT_DECLINATION_E_DEG = 12.0

# 8-point compass — what a controller actually says. 16 points would be
# false precision for a call the pilot only needs for gross orientation.
_CARDINALS: tuple[str, ...] = (
    "north",
    "northeast",
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
)


@dataclass(frozen=True)
class Station:
    """A VOR / VORTAC / TACAN that ATC can fix a contact off."""

    id: str
    kind: str  # vor | vortac | tacan
    say: str
    lat: float
    lon: float
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Divert:
    """A field ATC can send a jet to when it needs to get on the ground."""

    id: str
    say: str
    category: str  # military | civil
    lat: float
    lon: float
    aliases: tuple[str, ...] = ()


def _norm_token(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


def _num(val: Any) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


_CACHE: tuple[float, tuple[tuple[Station, ...], tuple[Divert, ...]]] | None = None


def _load() -> tuple[tuple[Station, ...], tuple[Divert, ...]]:
    """Parse navaids.json, re-reading only when the file changes on disk."""
    global _CACHE
    if not NAVAIDS_PATH.is_file():
        return ((), ())
    stamp = NAVAIDS_PATH.stat().st_mtime
    if _CACHE is not None and _CACHE[0] == stamp:
        return _CACHE[1]
    try:
        data = json.loads(NAVAIDS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ((), ())

    def aliases_of(item: dict[str, Any]) -> tuple[str, ...]:
        return tuple(
            str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()
        )

    stations: list[Station] = []
    for item in (data.get("stations") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        ident = _norm_token(item.get("id") or "")
        lat, lon = _num(item.get("lat")), _num(item.get("lon"))
        if not ident or lat is None or lon is None:
            continue
        stations.append(
            Station(
                id=ident,
                kind=str(item.get("kind") or "vor").strip().lower(),
                say=str(item.get("say") or ident).strip() or ident,
                lat=lat,
                lon=lon,
                aliases=aliases_of(item),
            )
        )

    diverts: list[Divert] = []
    for item in (data.get("diverts") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        ident = _norm_token(item.get("id") or "")
        lat, lon = _num(item.get("lat")), _num(item.get("lon"))
        if not ident or lat is None or lon is None:
            continue
        diverts.append(
            Divert(
                id=ident,
                say=str(item.get("say") or ident).strip() or ident,
                category=str(item.get("category") or "civil").strip().lower(),
                lat=lat,
                lon=lon,
                aliases=aliases_of(item),
            )
        )

    out = (tuple(stations), tuple(diverts))
    _CACHE = (stamp, out)
    return out


def stations() -> tuple[Station, ...]:
    return _load()[0]


def diverts() -> tuple[Divert, ...]:
    return _load()[1]


# --- geometry ---------------------------------------------------------


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    try:
        import atc_phrase

        return atc_phrase._haversine_nm(lat1, lon1, lat2, lon2)
    except Exception:
        rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
        )
        return 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)) * 6371000.0 / 1852.0


def _magnetic_bearing_deg(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    *,
    config: dict[str, Any] | None = None,
) -> float:
    try:
        import atc_phrase

        return atc_phrase.magnetic_bearing_deg(lat1, lon1, lat2, lon2, config=config)
    except Exception:
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dlon = math.radians(lon2 - lon1)
        y = math.sin(dlon) * math.cos(phi2)
        x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
            dlon
        )
        true_brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
        decl = _num((config or {}).get("bullseye_magnetic_declination_deg"))
        if decl is None:
            decl = DEFAULT_DECLINATION_E_DEG
        return (true_brg - decl) % 360.0


def bearing_range_nm(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    *,
    config: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Magnetic bearing and range NM from one point to another, radio-rounded."""
    brg = _magnetic_bearing_deg(from_lat, from_lon, to_lat, to_lon, config=config)
    rng = _haversine_nm(from_lat, from_lon, to_lat, to_lon)
    return int(round(brg)) % 360, int(round(rng))


def cardinal(bearing_deg: float) -> str:
    """Magnetic bearing → 8-point compass word ('northeast')."""
    try:
        brg = float(bearing_deg) % 360.0
    except (TypeError, ValueError):
        return ""
    return _CARDINALS[int((brg + 22.5) % 360.0 // 45.0)]


# --- position reference for ATC ---------------------------------------


def max_station_nm(config: dict[str, Any] | None = None) -> float:
    raw = _num((config or {}).get("navaid_position_max_nm"))
    return raw if raw and raw > 0 else DEFAULT_MAX_STATION_NM


def nearest_station(
    lat: float,
    lon: float,
    *,
    config: dict[str, Any] | None = None,
    max_nm: float | None = None,
    kinds: tuple[str, ...] | None = None,
) -> tuple[Station, float] | None:
    """Closest station to a position, with its range in NM."""
    limit = max_station_nm(config) if max_nm is None else float(max_nm)
    best: tuple[Station, float] | None = None
    for st in stations():
        if kinds and st.kind not in kinds:
            continue
        rng = _haversine_nm(lat, lon, st.lat, st.lon)
        if rng > limit:
            continue
        if best is None or rng < best[1]:
            best = (st, rng)
    return best


def station_position_fix(
    lat: float,
    lon: float,
    *,
    config: dict[str, Any] | None = None,
    max_nm: float | None = None,
) -> dict[str, Any] | None:
    """
    Where a jet is, said the way ATC says it.

    Returns {id, say, kind, bearing, range_nm, cardinal, spoken, display} —
    `spoken` is the whole clause, e.g. 'two five miles northeast of Mormon Mesa'.
    """
    hit = nearest_station(lat, lon, config=config, max_nm=max_nm)
    if hit is None:
        return None
    st, rng = hit
    # Bearing runs station → jet: the jet is what is northeast of the station.
    brg = int(round(_magnetic_bearing_deg(st.lat, st.lon, lat, lon, config=config))) % 360
    rng_i = int(round(rng))
    return {
        "id": st.id,
        "say": st.say,
        "kind": st.kind,
        "bearing": brg,
        "range_nm": rng_i,
        "cardinal": cardinal(brg),
        "spoken": speak_station_position(st.say, brg, rng_i),
        "display": f"{st.id} {brg:03d}/{rng_i}",
    }


def speak_station_position(say: str, bearing_deg: int, range_nm: int) -> str:
    """'two five miles northeast of Mormon Mesa' — no radial, ATC style."""
    try:
        import atc_phrase

        miles = atc_phrase.speak_field_miles(range_nm)
    except Exception:
        miles = f"{int(range_nm)} miles"
    where = cardinal(bearing_deg)
    if int(range_nm) < 1:
        return f"over {say}"
    if not where:
        return f"{miles} from {say}"
    return f"{miles} {where} of {say}"


# --- named point / divert lookup --------------------------------------


def _alias_keys(row: Any) -> list[str]:
    keys = [_norm_token(getattr(row, "id", ""))]
    keys.append(_norm_token(getattr(row, "say", "")))
    for alias in getattr(row, "aliases", ()):
        keys.append(_norm_token(alias))
    return [k for k in keys if len(k) >= 2]


def _lev_within(a: str, b: str, budget: int) -> bool:
    """Cheap bounded edit distance — Whisper mangles fix names constantly."""
    if abs(len(a) - len(b)) > budget:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(
                min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            )
        if min(cur) > budget:
            return False
        prev = cur
    return prev[-1] <= budget


def _fuzzy_budget(token: str) -> int:
    return 0 if len(token) < 5 else (1 if len(token) < 8 else 2)


def resolve_point(
    text: str,
    *,
    airport: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    A spoken point name → {id, say, lat, lon, source}.

    Search order is narrowest-first so the pilot gets the thing he most likely
    meant: this catalog's stations and fields, then the recovery/approach fixes
    and the wider NTTR named-point catalog.
    """
    del config
    # Longest phrase first, then shorter ones: "mormon mesa vortac" should
    # still find Mormon Mesa once the trailing word is dropped.
    words = [w for w in re.split(r"\s+", str(text or "").strip()) if w]
    phrases = [" ".join(words[:n]) for n in range(len(words), 0, -1)] or [str(text or "")]
    tokens = [_norm_token(p) for p in phrases]
    tokens = [t for t in dict.fromkeys(tokens) if len(t) >= 2]
    if not tokens:
        return None

    candidates: list[tuple[list[str], dict[str, Any]]] = []
    for st in stations():
        candidates.append(
            (
                _alias_keys(st),
                {
                    "id": st.id,
                    "say": st.say,
                    "lat": st.lat,
                    "lon": st.lon,
                    "source": f"navaid:{st.kind}",
                },
            )
        )
    for fld in diverts():
        candidates.append(
            (
                _alias_keys(fld),
                {
                    "id": fld.id,
                    "say": fld.say,
                    "lat": fld.lat,
                    "lon": fld.lon,
                    "source": "divert",
                },
            )
        )

    def from_catalog(key: str, hit: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(hit.get("id") or key),
            "say": str(hit.get("say") or hit.get("id") or key),
            "lat": float(hit["lat"]),
            "lon": float(hit["lon"]),
            "source": str(hit.get("source") or "fix"),
        }

    catalog = _fix_catalog(airport)
    for token in tokens:
        for keys, row in candidates:
            if token in keys:
                return row
        hit = catalog.get(token)
        if hit is not None:
            return from_catalog(token, hit)

    for token in tokens:
        budget = _fuzzy_budget(token)
        if not budget:
            continue
        for keys, row in candidates:
            if any(_lev_within(token, key, budget) for key in keys):
                return row
        for key, hit in catalog.items():
            if len(key) >= 4 and _lev_within(token, key, budget):
                return from_catalog(key, hit)
    return None


def _fix_catalog(airport: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The route tester's merged fix/navpoint catalog; empty if unavailable."""
    try:
        import route_tester

        return route_tester.load_fix_catalog(airport)
    except Exception:
        return {}


def divert_categories(config: dict[str, Any] | None = None) -> tuple[str, ...]:
    raw = (config or {}).get("divert_categories")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple)):
        picked = tuple(str(c).strip().lower() for c in raw if str(c).strip())
        if picked:
            return picked
    return ("military", "civil")


def nearest_divert(
    lat: float,
    lon: float,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Closest usable field to a position, with a magnetic bearing and range."""
    allowed = divert_categories(config)
    best: tuple[Divert, float] | None = None
    for fld in diverts():
        if fld.category not in allowed:
            continue
        rng = _haversine_nm(lat, lon, fld.lat, fld.lon)
        if best is None or rng < best[1]:
            best = (fld, rng)
    if best is None:
        return None
    fld, rng = best
    brg, rng_i = bearing_range_nm(lat, lon, fld.lat, fld.lon, config=config)
    return {
        "id": fld.id,
        "say": fld.say,
        "category": fld.category,
        "lat": fld.lat,
        "lon": fld.lon,
        "bearing": brg,
        "range_nm": rng_i,
        "source": "divert",
    }
