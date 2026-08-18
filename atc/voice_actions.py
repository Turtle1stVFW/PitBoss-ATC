"""
ATC replies that exist only as voice responses — they are not timeline steps.

Winds and altimeter come from the same METAR the scripted phrases use. Picture,
bogey dope, and declare calls follow AFTTP 3-2.8 using the live Opus CAOC feed.
"""

from __future__ import annotations

import math
import re
from typing import Any

import atc_phrase
import picture_labels as pl

# Re-export for config / UI / tests
GROUP_RADIUS_NM = pl.GROUP_RADIUS_NM
MAX_GROUPS = pl.MAX_DETAIL_GROUPS
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


class RadarUnavailable(RuntimeError):
    """No CAOC feed — distinct from a genuinely clean picture."""


def _feet(alt_meters: Any) -> int | None:
    try:
        return int(round(float(alt_meters) * 3.28084))
    except (TypeError, ValueError):
        return None


def _heading(unit: dict[str, Any]) -> float | None:
    for key in ("headingDeg", "heading", "hdg"):
        raw = unit.get(key)
        if raw is None:
            continue
        try:
            return float(raw) % 360.0
        except (TypeError, ValueError):
            continue
    return None


def _speed_mps(unit: dict[str, Any]) -> float | None:
    for key in ("groundSpeedMps", "speedMps", "velocity"):
        raw = unit.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


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
    """Greedy single-link clustering — 3 nm horizontal (AFTTP GROUP)."""
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
    # Prefer lat/lon haversine when both have positions
    if None not in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        try:
            d = atc_phrase._haversine_nm(
                float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"])
            )
            return d <= GROUP_RADIUS_NM
        except (TypeError, ValueError):
            pass
    ax = a["range_nm"] * math.sin(math.radians(a["bearing"]))
    ay = a["range_nm"] * math.cos(math.radians(a["bearing"]))
    bx = b["range_nm"] * math.sin(math.radians(b["bearing"]))
    by = b["range_nm"] * math.cos(math.radians(b["bearing"]))
    return math.hypot(ax - bx, ay - by) <= GROUP_RADIUS_NM


def _ownship(
    units: list[dict[str, Any]],
    airport: dict[str, Any],
    *,
    opus: Any,
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, tuple[float, float] | None]:
    hostile_side = _hostile_coalition(airport)
    own = atc_phrase.match_caoc_unit_for_flight(units, opus=opus, config=config)
    if own and str(own.get("coalition") or "").lower() == hostile_side:
        own = None
    own_ll: tuple[float, float] | None = None
    if own:
        try:
            own_ll = atc_phrase.caoc_xz_to_ll(float(own["xMeters"]), float(own["zMeters"]))
        except (KeyError, TypeError, ValueError):
            own_ll = None
    return own, own_ll


def _track_dict(
    unit: dict[str, Any],
    fix: dict[str, Any],
    distance: float,
    declaration: str,
) -> dict[str, Any]:
    uid = unit.get("id")
    if uid is None:
        uid = unit.get("unitId") or unit.get("unit_id")
    return {
        "bearing": int(fix["bearing"]),
        "range_nm": int(fix["range_nm"]),
        "name": fix.get("name") or "BULLSEYE",
        "label": fix.get("label") or fix.get("unit_name") or "unknown",
        "object": fix.get("object_name") or "",
        "lat": fix.get("lat"),
        "lon": fix.get("lon"),
        "feet": _feet(unit.get("altMeters")),
        "heading_deg": _heading(unit),
        "speed_mps": _speed_mps(unit),
        "distance_nm": float(distance),
        "declaration": declaration,
        "coalition": str(unit.get("coalition") or "").lower(),
        "unit_id": str(uid).strip() if uid is not None else "",
    }


def _groups_from_tracks(
    tracks: list[dict[str, Any]],
    *,
    memory: pl.DeclarationMemory | None = None,
    hostile_side: str = "red",
    upgrade_hostile: bool = False,
) -> list[pl.FightGroup]:
    book = memory or pl.DeclarationMemory()
    groups: list[pl.FightGroup] = []
    for cluster in _cluster(tracks):
        lead = min(
            cluster,
            key=lambda t: (
                t["distance_nm"] is None,
                t["distance_nm"] if t["distance_nm"] is not None else t["range_nm"],
            ),
        )
        feet = [t["feet"] for t in cluster if t["feet"] is not None]
        headings = [t["heading_deg"] for t in cluster if t.get("heading_deg") is not None]
        mean_hdg = None
        if headings:
            x = sum(math.cos(math.radians(h)) for h in headings)
            y = sum(math.sin(math.radians(h)) for h in headings)
            mean_hdg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
        ids = [str(t.get("unit_id") or "") for t in cluster if t.get("unit_id")]
        coalitions = [str(t.get("coalition") or "") for t in cluster if t.get("coalition")]
        coal = ""
        if coalitions:
            # Majority coalition; enemy-side wins a tie so we don't friendly-wash.
            red_or_blue = [c for c in coalitions if c in ("red", "blue")]
            if hostile_side in red_or_blue:
                coal = hostile_side
            else:
                coal = red_or_blue[0] if red_or_blue else coalitions[0]
        lead_ft = int(lead["feet"]) if lead.get("feet") is not None else (
            max(int(f) for f in feet) if feet else None
        )
        decl = book.assign(
            ids,
            brg=int(lead["bearing"]),
            rng=int(lead["range_nm"]),
            feet=lead_ft,
            coalition=coal,
            hostile_side=hostile_side,
            upgrade_hostile=upgrade_hostile,
        )
        groups.append(
            pl.FightGroup(
                bearing=int(lead["bearing"]),
                range_nm=int(lead["range_nm"]),
                bullseye_name=str(lead["name"]),
                distance_nm=float(lead["distance_nm"]),
                count=len(cluster),
                feet=max(feet) if feet else None,
                feet_list=[int(f) for f in feet],
                heading_deg=mean_hdg,
                lat=lead.get("lat"),
                lon=lead.get("lon"),
                label=str(lead.get("label") or ""),
                object=str(lead.get("object") or ""),
                declaration=decl,
                unit_ids=ids,
            )
        )
    groups.sort(key=lambda g: g.distance_nm)
    return groups


def collect_hostile_groups(
    config: dict[str, Any],
    airport: dict[str, Any],
    *,
    opus: Any = None,
    state: dict[str, Any] | None = None,
    upgrade_hostile: bool = False,
) -> tuple[list[pl.FightGroup], dict[str, Any] | None, tuple[float, float] | None]:
    """
    Hostile air groups from the live CAOC feed, nearest first.

    Skips fixtures and groups outside picture_max_range_nm. Returns
    (groups, own_unit, own_ll). Declarations stick across picture / declare /
    bogey dope until Bandsaw or the flight lead upgrades to hostile.
    """
    radar = atc_phrase.fetch_caoc_radar(config)
    if not radar:
        raise RadarUnavailable("CAOC radar feed is unreachable")
    units = list(radar.get("units") or [])
    air = atc_phrase.caoc_air_units(units)
    if not air:
        return [], None, None

    max_nm = picture_max_range_nm(config)
    hostile_side = _hostile_coalition(airport)
    own, own_ll = _ownship(units, airport, opus=opus, config=config)
    if own_ll is None:
        return [], own, None

    book = pl.DeclarationMemory.from_state(state)
    tracks: list[dict[str, Any]] = []
    for unit in air:
        if str(unit.get("coalition") or "").lower() != hostile_side:
            continue
        if atc_phrase.caoc_unit_is_picture_fixture(unit):
            continue
        fix = atc_phrase.bullseye_for_caoc_unit(unit, config, opus=opus)
        if not fix:
            continue
        lat, lon = fix.get("lat"), fix.get("lon")
        if lat is None or lon is None:
            continue
        try:
            distance = atc_phrase._haversine_nm(
                own_ll[0], own_ll[1], float(lat), float(lon)
            )
        except (TypeError, ValueError):
            continue
        if float(distance) > max_nm:
            continue
        tracks.append(_track_dict(unit, fix, float(distance), ""))
    if not tracks:
        return [], own, own_ll

    groups = _groups_from_tracks(
        tracks,
        memory=book,
        hostile_side=hostile_side,
        upgrade_hostile=upgrade_hostile,
    )
    book.to_state(state)
    pl.enrich_fight_bearings(groups, own_ll)
    return groups, own, own_ll


# How close a track's bullseye must be to the pilot's DECLARE cue (NM).
DECLARE_CUE_MATCH_NM = 55.0
DECLARE_ALT_WEIGHT_NM_PER_1K = 0.25

# Spoken bullseye labels (and Whisper near-misses). Always resolve as theater ELVIS.
_DECLARE_BE_WORDS = frozenset(
    {
        "elvis",
        "bullseye",
        "bull",
        "ellis",
        "alvis",
        "alvies",
        "helvis",
        "elviz",
        "elbees",
        "lvis",
    }
)
_DECLARE_SKIP_WORDS = frozenset(
    {
        "declare",
        "group",
        "groups",
        "request",
        "requesting",
        "bandsaw",
        "blackjack",
        "ansa",
        "fleece",
        "hostile",
        "friendly",
        "bogey",
        "bandit",
        "track",
        "thousand",
        "angels",
        "contact",
        "contacts",
    }
) | _DECLARE_BE_WORDS


def theater_bullseye_name(config: dict[str, Any] | None = None) -> str:
    """Configured theater bullseye code name (NTTR default ELVIS)."""
    raw = str((config or {}).get("bullseye_navpoint") or "ELVIS").strip()
    return raw or "ELVIS"


def parse_declare_cue(
    text: str, *, config: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """
    Pull bearing / range / altitude from a DECLARE call.

    Bullseye name is always the theater bullseye (ELVIS). Pilots may say
    elvis, bullseye, or omit the name — Whisper mishears of the name are ignored.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        import voice_intent

        norm = voice_intent.normalize(raw)
    except Exception:
        norm = re.sub(r"[^a-z0-9\s]", " ", raw.lower())
        norm = re.sub(r"\s+", " ", norm).strip()
    toks = norm.split()
    if not toks:
        return None

    # Search after 'declare' when present; otherwise whole utterance.
    start = 0
    for i, t in enumerate(toks):
        if t == "declare":
            start = i + 1
            break

    # First digit token after declare (skip group / elvis / bullseye / fluff).
    digit_i = -1
    for i in range(start, len(toks)):
        if toks[i].isdigit():
            digit_i = i
            break
    if digit_i < 0:
        return None

    nums = [t for t in toks[digit_i:] if t.isdigit()]
    if not nums:
        return None

    bearing: int | None = None
    range_nm: int | None = None
    altitude_ft: int | None = None
    first = nums[0]
    rest: list[str] = []

    # normalize() merges digit tokens: '05667-21000' → '0566721000'
    if len(first) >= 8:
        bearing = int(first[:3]) % 360
        parsed = False
        for alt_len in (5, 4):
            if len(first) <= 3 + alt_len:
                continue
            alt_n = int(first[-alt_len:])
            mid = first[3:-alt_len]
            if not mid or not mid.isdigit():
                continue
            rng = int(mid)
            if 1000 <= alt_n <= 80000 and 0 <= rng <= 400:
                range_nm = rng
                altitude_ft = alt_n
                parsed = True
                break
        if not parsed:
            range_nm = int(first[3:])
    elif len(nums) >= 2 and len(first) <= 3:
        bearing = int(first) % 360
        range_nm = int(nums[1])
        rest = nums[2:]
    elif len(first) >= 4:
        bearing = int(first[:3]) % 360
        range_nm = int(first[3:6] if len(first) >= 6 else first[3:])
        rest = nums[1:]
    else:
        return None

    if bearing is None or range_nm is None or range_nm < 0 or range_nm > 500:
        return None

    if altitude_ft is None and rest:
        alt_n = int(rest[0])
        thousand_near = any(t == "thousand" for t in toks[digit_i:])
        if alt_n >= 1000:
            altitude_ft = alt_n
        elif thousand_near or alt_n <= 60:
            altitude_ft = alt_n * 1000
        else:
            altitude_ft = alt_n

    return {
        "name": theater_bullseye_name(config),
        "bearing": int(bearing) % 360,
        "range_nm": int(range_nm),
        "altitude_ft": altitude_ft,
    }


def _cue_bullseye_error_nm(fix_brg: int, fix_rng: int, cue: dict[str, Any]) -> float:
    """Approximate NM between a unit bullseye and the pilot's DECLARE cue."""
    brg_err = abs(pl.heading_delta(float(fix_brg), float(cue["bearing"])))
    # Lateral arc ≈ range * radians(bearing error)
    lateral = float(fix_rng) * math.radians(brg_err)
    range_err = abs(float(fix_rng) - float(cue["range_nm"]))
    return math.hypot(lateral, range_err)


def collect_declare_groups(
    config: dict[str, Any],
    airport: dict[str, Any],
    *,
    opus: Any = None,
    cue: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    upgrade_hostile: bool = False,
) -> tuple[list[pl.FightGroup], dict[str, Any] | None, tuple[float, float] | None]:
    """
    Air groups for DECLARE.

    With a bullseye cue: match CAOC tracks by bullseye bearing/range (theater
    ELVIS), not by nearest-to-you. Without a cue: nearest air in the bubble.
    """
    radar = atc_phrase.fetch_caoc_radar(config)
    if not radar:
        raise RadarUnavailable("CAOC radar feed is unreachable")
    units = list(radar.get("units") or [])
    air = atc_phrase.caoc_air_units(units)
    if not air:
        return [], None, None

    max_nm = picture_max_range_nm(config)
    hostile_side = _hostile_coalition(airport)
    own, own_ll = _ownship(units, airport, opus=opus, config=config)
    if cue is None and own_ll is None:
        return [], own, None

    tracks: list[dict[str, Any]] = []
    for unit in air:
        if atc_phrase.caoc_unit_is_picture_fixture(unit):
            continue
        # With a bullseye cue, keep friendlies even if ownship match is wrong.
        # Without a cue, skip true ownship for "nearest contact".
        is_own = bool(
            own
            and (
                unit is own
                or (
                    str(unit.get("id") or "")
                    and str(unit.get("id")) == str(own.get("id") or "")
                )
            )
        )
        if cue is None and is_own:
            continue
        fix = atc_phrase.bullseye_for_caoc_unit(unit, config, opus=opus)
        if not fix:
            continue
        try:
            fix_brg = int(fix["bearing"])
            fix_rng = int(fix["range_nm"])
        except (KeyError, TypeError, ValueError):
            continue
        lat, lon = fix.get("lat"), fix.get("lon")
        lat_f: float | None
        lon_f: float | None
        try:
            lat_f = float(lat) if lat is not None else None
            lon_f = float(lon) if lon is not None else None
        except (TypeError, ValueError):
            lat_f, lon_f = None, None

        if cue is not None:
            # Cue match uses bullseye brg/rng only — lat/lon optional.
            score = _cue_bullseye_error_nm(fix_brg, fix_rng, cue)
            if score > DECLARE_CUE_MATCH_NM:
                continue
            if is_own:
                score += 15.0  # prefer another contact over declaring yourself
            cue_alt = cue.get("altitude_ft")
            feet = _feet(unit.get("altMeters"))
            if cue_alt is not None and feet is not None:
                score += (abs(feet - int(cue_alt)) / 1000.0) * DECLARE_ALT_WEIGHT_NM_PER_1K
            distance = float(score)
            to_own = distance
            if own_ll is not None and lat_f is not None and lon_f is not None:
                try:
                    to_own = atc_phrase._haversine_nm(
                        own_ll[0], own_ll[1], lat_f, lon_f
                    )
                except (TypeError, ValueError):
                    pass
        else:
            if lat_f is None or lon_f is None or own_ll is None:
                continue
            try:
                to_own = atc_phrase._haversine_nm(
                    own_ll[0], own_ll[1], lat_f, lon_f
                )
            except (TypeError, ValueError):
                continue
            if float(to_own) > max_nm:
                continue
            distance = float(to_own)

        tracks.append(_track_dict(unit, fix, float(distance), ""))

    if not tracks:
        return [], own, own_ll
    book = pl.DeclarationMemory.from_state(state)
    groups = _groups_from_tracks(
        tracks,
        memory=book,
        hostile_side=hostile_side,
        upgrade_hostile=upgrade_hostile,
    )
    book.to_state(state)
    pl.enrich_fight_bearings(groups, own_ll)
    return groups, own, own_ll


def build_picture_reply(
    config: dict[str, Any],
    airport: dict[str, Any],
    callsign: str,
    *,
    agency: str = "Blackjack",
    opus: Any = None,
    state: dict[str, Any] | None = None,
) -> tuple[str, list[pl.FightGroup]]:
    """
    AFTTP traditional-label (or core) picture call.

    Raises RadarUnavailable when the feed is down.
    """
    cs = atc_phrase.speak_callsign(callsign)
    groups, _own, _own_ll = collect_hostile_groups(
        config, airport, opus=opus, state=state
    )
    if not groups:
        return f"{cs}, {agency}, picture clean.", []

    classified = pl.classify_picture(groups)
    if classified.kind == "clean" or not classified.groups:
        return f"{cs}, {agency}, picture clean.", []

    # Head: agency may omit "picture" word when label is traditional — AFTTP
    # examples often start with the label directly after the controller callsign.
    # We keep agency + label for radio clarity.
    if classified.kind == "single":
        g = classified.groups[0]
        clause = pl.core_group_clause(g, include_bullseye=True, include_track=True)
        # SINGLE GROUP folds into one sentence
        body = clause.replace("Single group", "single group", 1)
        return f"{cs}, {agency}, {body}.", classified.groups

    sentences = [f"{cs}, {agency}, {classified.head}"]
    shared = classified.shared_track
    for i, g in enumerate(classified.groups):
        # Anchor bullseye on first / when azimuth span warrants (always for clarity)
        include_be = True
        if i > 0 and classified.kind in ("range", "ladder", "vic", "champagne"):
            # Trail groups often omit repeated bullseye when on same axis — keep alt
            # AFTTP examples omit bullseye on trail when amplifying picture.
            include_be = False
        include_track = shared is None
        clause = pl.core_group_clause(
            g, include_bullseye=include_be, include_track=include_track
        )
        sentences.append(clause)
    return ". ".join(sentences) + ".", classified.groups


def build_bogey_dope_reply(
    config: dict[str, Any],
    airport: dict[str, Any],
    callsign: str,
    *,
    agency: str = "Blackjack",
    opus: Any = None,
    state: dict[str, Any] | None = None,
) -> tuple[str, list[pl.FightGroup]]:
    """Ch V §11 — BRAA relative to ownship on closest hostile group."""
    cs = atc_phrase.speak_callsign(callsign)
    groups, _own, own_ll = collect_hostile_groups(
        config, airport, opus=opus, state=state
    )
    if own_ll is None:
        return f"{cs}, {agency}, unable bogey dope, no ownship track.", []
    if not groups:
        return f"{cs}, {agency}, clean.", []

    g = groups[0]
    if g.lat is None or g.lon is None:
        return f"{cs}, {agency}, unable bogey dope.", []

    brg, rng = pl.braa_from_own(own_ll[0], own_ll[1], float(g.lat), float(g.lon))
    aspect = pl.aspect_to_fighter(
        own_lat=own_ll[0],
        own_lon=own_ll[1],
        tgt_lat=float(g.lat),
        tgt_lon=float(g.lon),
        tgt_heading=g.heading_deg,
    )
    bits = [f"group {pl.speak_braa(brg, rng)}"]
    alt = pl.group_altitude_speech(g)
    if alt:
        bits.append(alt)
    if aspect:
        bits.append(aspect)
    bits.append(g.declaration)
    if g.count > 1:
        bits.append(f"{atc_phrase.speak_natural_number(g.count)} contacts")
    return f"{cs}, {agency}, {', '.join(bits)}.", [g]


def build_declare_reply(
    config: dict[str, Any],
    airport: dict[str, Any],
    callsign: str,
    *,
    agency: str = "Blackjack",
    channel: str = "",
    opus: Any = None,
    transcript: str | None = None,
    state: dict[str, Any] | None = None,
) -> tuple[str, list[pl.FightGroup]]:
    """
    Short DECLARE reply: callsign, agency, declaration only.

    Matching still uses the pilot's bullseye cue when given; the response does
    not read the fix back (e.g. 'Fleece 1, Bandsaw, hostile.').
    Bandsaw declare, or the flight lead saying hostile, upgrades that group.
    """
    cs = atc_phrase.speak_callsign(callsign)
    cue = parse_declare_cue(transcript or "", config=config)
    upgrade = pl.agency_can_upgrade_hostile(agency, channel) or (
        pl.transcript_upgrades_hostile(transcript)
    )
    groups, _own, own_ll = collect_declare_groups(
        config,
        airport,
        opus=opus,
        cue=cue,
        state=state,
        upgrade_hostile=False,
    )
    if cue is None and own_ll is None:
        return f"{cs}, {agency}, unable.", []
    if not groups:
        return f"{cs}, {agency}, clean.", []

    g = groups[0]
    if upgrade and pl.normalize_declaration(g.declaration) != "friendly":
        book = pl.DeclarationMemory.from_state(state)
        g.declaration = book.upgrade_to_hostile(
            g.unit_ids,
            brg=g.bearing,
            rng=g.range_nm,
            feet=g.feet,
        )
        book.to_state(state)
    decl = str(g.declaration or "bogey").strip() or "bogey"
    return f"{cs}, {agency}, {decl}.", [g]
