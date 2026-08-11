"""
ATC replies that exist only as voice responses — they are not timeline steps.

Winds and altimeter come from the same METAR the scripted phrases use. The
picture call is built from the live Opus CAOC radar feed, grouped the way a
controller would call it rather than reading out every individual track.
"""

from __future__ import annotations

import math
from typing import Any

import atc_phrase

# Tracks inside both thresholds are called as one group.
GROUP_RADIUS_NM = 7.0
GROUP_ALT_BAND_FT = 6000.0
MAX_GROUPS = 3
# Only call hostiles inside this range of the requesting flight (overridable).
PICTURE_MAX_RANGE_NM = 150.0


def picture_max_range_nm(config: dict[str, Any] | None = None) -> float:
    """
    Max NM from the calling flight for picture groups.

    Config key `picture_max_range_nm` (default 150). Raise it for testing so
    distant hostiles still show up on the call.
    """
    raw = (config or {}).get("picture_max_range_nm", PICTURE_MAX_RANGE_NM)
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return PICTURE_MAX_RANGE_NM
    return n if n > 0 else PICTURE_MAX_RANGE_NM

_ORDINALS = ("lead", "second", "third", "fourth")


class RadarUnavailable(RuntimeError):
    """No CAOC feed — distinct from a genuinely clean picture."""


def _feet(alt_meters: Any) -> int | None:
    try:
        return int(round(float(alt_meters) * 3.28084))
    except (TypeError, ValueError):
        return None


def _speak_picture_altitude(feet: int | None) -> str:
    """Picture altitudes in thousands of feet — not angels."""
    if feet is None:
        return ""
    if feet < 1000:
        return "low"
    thousands = int(round(feet / 1000.0))
    return f"{atc_phrase.speak_natural_number(thousands)} thousand"


def _hostile_coalition(airport: dict[str, Any]) -> str:
    """CAOC labels sides red/blue; our coalition comes from airports.json (2 = blue)."""
    try:
        return "red" if int(airport.get("coalition", 2)) == 2 else "blue"
    except (TypeError, ValueError):
        return "red"


def build_winds_reply(
    airport: dict[str, Any],
    callsign: str,
    weather: atc_phrase.Weather,
    runway: str | None = None,
) -> str:
    """Wind only — runway is not part of a winds check."""
    del runway  # kept for call-site compatibility
    agency = str(airport.get("name") or "").strip()
    wind = atc_phrase.speak_wind(weather.wind_dir, weather.wind_speed_kt)
    return f"{atc_phrase.speak_callsign(callsign)}, {agency}, {wind}."


def build_altimeter_reply(
    airport: dict[str, Any],
    callsign: str,
    weather: atc_phrase.Weather,
) -> str:
    agency = str(airport.get("name") or "").strip()
    altimeter = atc_phrase.speak_altimeter(weather.altimeter_inhg)
    return f"{atc_phrase.speak_callsign(callsign)}, {agency}, altimeter {altimeter}."


def _cluster(tracks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Greedy single-link clustering on bullseye polar position plus altitude."""
    groups: list[list[dict[str, Any]]] = []
    for track in tracks:
        for group in groups:
            if any(_close(track, other) for other in group):
                group.append(track)
                break
        else:
            groups.append([track])
    return groups


def _close(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ax = a["range_nm"] * math.sin(math.radians(a["bearing"]))
    ay = a["range_nm"] * math.cos(math.radians(a["bearing"]))
    bx = b["range_nm"] * math.sin(math.radians(b["bearing"]))
    by = b["range_nm"] * math.cos(math.radians(b["bearing"]))
    if math.hypot(ax - bx, ay - by) > GROUP_RADIUS_NM:
        return False
    if a["feet"] is None or b["feet"] is None:
        return True
    return abs(a["feet"] - b["feet"]) <= GROUP_ALT_BAND_FT


def collect_hostile_groups(
    config: dict[str, Any],
    airport: dict[str, Any],
    *,
    opus: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """
    Hostile air groups from the live CAOC feed, nearest first.

    Skips always-present fixtures (hostile AWACS / tankers) and groups outside
    picture_max_range_nm of the calling flight. Returns (groups, own_track).
    """
    radar = atc_phrase.fetch_caoc_radar(config)
    if not radar:
        raise RadarUnavailable("CAOC radar feed is unreachable")
    units = list(radar.get("units") or [])
    air = atc_phrase.caoc_air_units(units)
    if not air:
        return [], None

    max_nm = picture_max_range_nm(config)
    hostile_side = _hostile_coalition(airport)
    own = atc_phrase.match_caoc_unit_for_flight(units, opus=opus, config=config)
    # A match on the hostile side means the callsign heuristics latched onto the
    # wrong track; better to fall back to bullseye ordering than to sort by it.
    if own and str(own.get("coalition") or "").lower() == hostile_side:
        own = None

    own_ll: tuple[float, float] | None = None
    if own:
        try:
            own_ll = atc_phrase.caoc_xz_to_ll(float(own["xMeters"]), float(own["zMeters"]))
        except (KeyError, TypeError, ValueError):
            own_ll = None

    tracks: list[dict[str, Any]] = []
    for unit in air:
        if str(unit.get("coalition") or "").lower() != hostile_side:
            continue
        # Red tanker / AWACS fixtures are always on the feed — not picture traffic.
        if atc_phrase.caoc_unit_is_picture_fixture(unit):
            continue
        fix = atc_phrase.bullseye_for_caoc_unit(unit, config, opus=opus)
        if not fix:
            continue
        lat = fix.get("lat")
        lon = fix.get("lon")
        distance = None
        if own_ll is not None and lat is not None and lon is not None:
            try:
                distance = atc_phrase._haversine_nm(
                    own_ll[0], own_ll[1], float(lat), float(lon)
                )
            except (TypeError, ValueError):
                distance = None
        # Need the caller on radar to gate by range; otherwise skip the track.
        if own_ll is None or distance is None or float(distance) > max_nm:
            continue
        tracks.append(
            {
                "bearing": int(fix["bearing"]),
                "range_nm": int(fix["range_nm"]),
                "name": fix.get("name") or "BULLSEYE",
                "label": fix.get("label") or fix.get("unit_name") or "unknown",
                "object": fix.get("object_name") or "",
                "lat": lat,
                "lon": lon,
                "feet": _feet(unit.get("altMeters")),
                "distance_nm": float(distance),
            }
        )
    if not tracks:
        return [], own

    groups: list[dict[str, Any]] = []
    for cluster in _cluster(tracks):
        count = len(cluster)
        # Prefer the contact nearest the caller when ranking the group lead.
        lead = min(
            cluster,
            key=lambda t: (
                t["distance_nm"] is None,
                t["distance_nm"] if t["distance_nm"] is not None else t["range_nm"],
            ),
        )
        feet = [t["feet"] for t in cluster if t["feet"] is not None]
        groups.append(
            {
                "bearing": lead["bearing"],
                "range_nm": lead["range_nm"],
                "bullseye_name": lead["name"],
                "feet": max(feet) if feet else None,
                "count": count,
                "label": lead["label"],
                "object": lead["object"],
                "distance_nm": lead.get("distance_nm"),
            }
        )

    groups.sort(key=lambda g: (g["distance_nm"] is None, g["distance_nm"] or g["range_nm"]))
    return groups, own


def build_picture_reply(
    config: dict[str, Any],
    airport: dict[str, Any],
    callsign: str,
    *,
    agency: str = "Blackjack",
    opus: Any = None,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Controller-style picture call. Returns (spoken text, groups used).

    Raises RadarUnavailable when the feed is down so the caller can say
    "unable" instead of falsely reporting a clean picture.
    """
    cs = atc_phrase.speak_callsign(callsign)
    groups, _own = collect_hostile_groups(config, airport, opus=opus)
    if not groups:
        return f"{cs}, {agency}, picture clean.", []

    shown = groups[:MAX_GROUPS]
    if len(shown) == 1:
        head = f"{cs}, {agency}, picture, single group"
    else:
        head = (
            f"{cs}, {agency}, picture, "
            f"{atc_phrase.speak_natural_number(len(shown))} groups"
        )

    sentences = [head]
    for i, group in enumerate(shown):
        label = "group" if len(shown) == 1 else f"{_ORDINALS[min(i, len(_ORDINALS) - 1)]} group"
        parts = [
            label,
            atc_phrase.speak_picture_bullseye(
                group["bullseye_name"], group["bearing"], group["range_nm"]
            ),
        ]
        alt = _speak_picture_altitude(group["feet"])
        if alt:
            parts.append(alt)
        if group["count"] > 1:
            parts.append(f"{atc_phrase.speak_natural_number(group['count'])} contacts")
        parts.append("hostile")
        sentence = ", ".join(parts)
        sentences.append(sentence[0].upper() + sentence[1:])
    return ". ".join(sentences) + ".", shown
