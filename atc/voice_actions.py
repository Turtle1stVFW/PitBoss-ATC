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

_ORDINALS = ("lead", "second", "third", "fourth")


class RadarUnavailable(RuntimeError):
    """No CAOC feed — distinct from a genuinely clean picture."""


def _feet(alt_meters: Any) -> int | None:
    try:
        return int(round(float(alt_meters) * 3.28084))
    except (TypeError, ValueError):
        return None


def _speak_angels(feet: int | None) -> str:
    if feet is None:
        return ""
    if feet < 1000:
        return "low"
    return f"angels {atc_phrase.speak_natural_number(int(round(feet / 1000.0)))}"


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
    agency = str(airport.get("name") or "").strip()
    wind = atc_phrase.speak_wind(weather.wind_dir, weather.wind_speed_kt)
    bits = [f"{atc_phrase.speak_callsign(callsign)}, {agency}, {wind}"]
    if runway:
        bits.append(f"runway {atc_phrase.speak_runway(runway)}")
    return ", ".join(bits) + "."


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

    Returns (groups, own_track). Each group has bearing/range from bullseye,
    altitude in feet, a contact count, and distance from our own aircraft.
    """
    radar = atc_phrase.fetch_caoc_radar(config)
    if not radar:
        raise RadarUnavailable("CAOC radar feed is unreachable")
    units = list(radar.get("units") or [])
    air = atc_phrase.caoc_air_units(units)
    if not air:
        return [], None

    hostile_side = _hostile_coalition(airport)
    own = atc_phrase.match_caoc_unit_for_flight(units, opus=opus, config=config)
    # A match on the hostile side means the callsign heuristics latched onto the
    # wrong track; better to fall back to bullseye ordering than to sort by it.
    if own and str(own.get("coalition") or "").lower() == hostile_side:
        own = None

    tracks: list[dict[str, Any]] = []
    for unit in air:
        if str(unit.get("coalition") or "").lower() != hostile_side:
            continue
        fix = atc_phrase.bullseye_for_caoc_unit(unit, config, opus=opus)
        if not fix:
            continue
        tracks.append(
            {
                "bearing": int(fix["bearing"]),
                "range_nm": int(fix["range_nm"]),
                "name": fix.get("name") or "BULLSEYE",
                "label": fix.get("label") or fix.get("unit_name") or "unknown",
                "object": fix.get("object_name") or "",
                "lat": fix.get("lat"),
                "lon": fix.get("lon"),
                "feet": _feet(unit.get("altMeters")),
            }
        )
    if not tracks:
        return [], own

    own_ll: tuple[float, float] | None = None
    if own:
        try:
            own_ll = atc_phrase.caoc_xz_to_ll(float(own["xMeters"]), float(own["zMeters"]))
        except (KeyError, TypeError, ValueError):
            own_ll = None

    groups: list[dict[str, Any]] = []
    for cluster in _cluster(tracks):
        count = len(cluster)
        lead = min(cluster, key=lambda t: t["range_nm"])
        feet = [t["feet"] for t in cluster if t["feet"] is not None]
        distance = None
        if own_ll and lead.get("lat") is not None:
            distance = atc_phrase._haversine_nm(
                own_ll[0], own_ll[1], float(lead["lat"]), float(lead["lon"])
            )
        groups.append(
            {
                "bearing": lead["bearing"],
                "range_nm": lead["range_nm"],
                "bullseye_name": lead["name"],
                "feet": max(feet) if feet else None,
                "count": count,
                "label": lead["label"],
                "object": lead["object"],
                "distance_nm": distance,
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
            atc_phrase.speak_alpha_bullseye(
                group["bullseye_name"], group["bearing"], group["range_nm"]
            ),
        ]
        angels = _speak_angels(group["feet"])
        if angels:
            parts.append(angels)
        if group["count"] > 1:
            parts.append(f"{atc_phrase.speak_natural_number(group['count'])} contacts")
        parts.append("hostile")
        sentence = ", ".join(parts)
        sentences.append(sentence[0].upper() + sentence[1:])
    return ". ".join(sentences) + ".", shown
