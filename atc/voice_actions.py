"""
ATC replies that exist only as voice responses — they are not timeline steps.

Winds and altimeter come from the same METAR the scripted phrases use. Picture,
bogey dope, and declare calls follow AFTTP 3-2.8 using the live Opus CAOC feed.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

import atc_phrase
import picture_labels as pl

# Re-export for config / UI / tests
GROUP_RADIUS_NM = pl.GROUP_RADIUS_NM
MAX_GROUPS = pl.MAX_DETAIL_GROUPS
PICTURE_MAX_RANGE_NM = 150.0
_PICTURE_GROUPS_KEY = "picture_groups"
_PICTURE_GROUPS_TTL_S = 45 * 60


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


def _feet(alt_meters: Any, *, altimeter_inhg: float | None = None) -> int | None:
    try:
        geometric = float(alt_meters) * 3.28084
    except (TypeError, ValueError):
        return None
    return atc_phrase.pressure_altitude_ft(geometric, altimeter_inhg)


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
    *,
    altimeter_inhg: float | None = None,
) -> dict[str, Any]:
    uid = unit.get("id")
    if uid is None:
        uid = unit.get("unitId") or unit.get("unit_id")
    return {
        "bearing": int(fix["bearing"]),
        "range_nm": int(fix["range_nm"]),
        "name": fix.get("name") or "BULLSEYE",
        "label": fix.get("label") or fix.get("unit_name") or "unknown",
        "lat": fix.get("lat"),
        "lon": fix.get("lon"),
        "feet": _feet(unit.get("altMeters"), altimeter_inhg=altimeter_inhg),
        "heading_deg": _heading(unit),
        "speed_mps": _speed_mps(unit),
        "distance_nm": float(distance),
        "declaration": declaration,
        "coalition": str(unit.get("coalition") or "").lower(),
        "unit_id": str(uid).strip() if uid is not None else "",
        "object": fix.get("object_name") or unit.get("objectName") or unit.get("object_name") or "",
        "affiliation": unit.get("affiliation"),
        "ti_training": pl.caoc_unit_is_ti_training(unit),
        "display_callsign": str(
            unit.get("displayCallsign") or unit.get("display_callsign") or ""
        ).strip(),
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
        coalitions = [
            pl.normalize_coalition(t.get("coalition"))
            for t in cluster
            if t.get("coalition") not in (None, "")
        ]
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
        obj = str(lead.get("object") or "")
        affs = [
            pl.normalize_opus_affiliation(t.get("affiliation"))
            for t in cluster
        ]
        affs = [a for a in affs if a]
        cluster_aff = ""
        if affs:
            cluster_aff = affs[0]
            for extra in affs[1:]:
                cluster_aff = pl.higher_declaration(cluster_aff, extra)
        ti = any(bool(t.get("ti_training")) for t in cluster)
        decl = book.assign(
            ids,
            brg=int(lead["bearing"]),
            rng=int(lead["range_nm"]),
            feet=lead_ft,
            coalition=coal,
            hostile_side=hostile_side,
            upgrade_hostile=upgrade_hostile,
            affiliation=cluster_aff or None,
            ti_training=ti,
        )
        label = str(
            lead.get("display_callsign") or lead.get("label") or ""
        ).strip()
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
                label=label,
                object=obj,
                declaration=decl,
                unit_ids=ids,
                coalition=coal,
                affiliation=cluster_aff or (str(lead.get("affiliation") or "")),
                ti_training=ti,
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
    (groups, own_unit, own_ll). OPUS affiliation drives the spoken label;
    UNKNOWN / TI get a VID cue instead of a random bandit/hostile roll.
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
    qnh = atc_phrase.metar_altimeter_inhg(config)
    tracks: list[dict[str, Any]] = []
    for unit in air:
        if not pl.picture_include_unit(unit, hostile_side):
            continue
        if atc_phrase.caoc_unit_is_picture_fixture(unit):
            continue
        if not atc_phrase.caoc_unit_is_picture_eligible(unit):
            continue
        fix = atc_phrase.bullseye_for_caoc_unit(
            unit, config, opus=opus, altimeter_inhg=qnh
        )
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
        tracks.append(
            _track_dict(unit, fix, float(distance), "", altimeter_inhg=qnh)
        )
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
# 55 NM used to snap a misheard fix onto a friendly across the area.
DECLARE_CUE_MATCH_NM = 18.0
# When the pilot said an altitude (28k / angels 28), reject tracks farther than this.
DECLARE_CUE_ALT_FT = 5000
# Ranking among tracks that already passed the hard gates.
DECLARE_ALT_WEIGHT_NM_PER_1K = 2.0
# If a friendly wins on score, still take a hostile within this extra NM.
DECLARE_HOSTILE_PREFER_NM = 8.0
# Friendly-only matches farther than this are "unable", not a guess.
DECLARE_CUE_CONFIDENT_NM = 10.0

_DECLARE_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
}
# Whisper often writes angel / angles for angels.
_DECLARE_ANGELS = r"(?:angels?|angles?)"
_DECLARE_ONES = {
    "zero": 0,
    "oh": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}

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


_DECLARE_CUE_START_WORDS = frozenset(
    {
        "declare",
        "vid",
        "upgrade",
        "id",
        "group",
        "groups",
        "elvis",
        "ellis",
        "alvis",
        "bullseye",
        "bull",
    }
)


def _parse_packed_bullseye(first: str) -> tuple[int, int, int | None] | None:
    """
    Split a glued bullseye digit run into bearing / range / optional altitude.

    Whisper + normalize() turn '09017-19000' into '0901719000' and
    '357.005.9000' into '3570059000'. For a 7-digit body prefer RR+AAAAA
    when that range is nonzero; otherwise RRR+AAAA.
    """
    if not first.isdigit() or len(first) < 4:
        return None
    bearing = int(first[:3]) % 360
    body = first[3:]

    def _ok(rng: int, alt: int | None) -> bool:
        if rng < 0 or rng > 400:
            return False
        if alt is None:
            return True
        return 1000 <= alt <= 80000

    # Book form for picture/declare: BBB + RR + AAAAA (090 + 17 + 19000).
    if len(body) >= 7:
        rng2 = int(body[:2])
        alt5 = int(body[2:7])
        rest = body[7:]
        if _ok(rng2, alt5) and rng2 >= 1 and not rest:
            return bearing, rng2, alt5
        # BBB + RRR + AAAA (357 + 005 + 9000).
        rng3 = int(body[:3])
        alt4 = int(body[3:7])
        rest = body[7:]
        if _ok(rng3, alt4) and not rest:
            return bearing, rng3, alt4
        # Longer packs: try scoring both with trailing ignored (should not happen).
    if len(body) == 6:
        # BBB + RR + AAAA glued short alt, or BBB + R + AAAAA — uncommon.
        rng2 = int(body[:2])
        alt4 = int(body[2:])
        if _ok(rng2, alt4) and rng2 >= 1:
            return bearing, rng2, alt4
    if len(body) >= 5:
        for alt_len in (5, 4):
            if len(body) <= alt_len:
                continue
            mid = body[:-alt_len]
            if not mid.isdigit():
                continue
            rng = int(mid)
            alt = int(body[-alt_len:])
            if _ok(rng, alt) and (rng >= 1 or alt_len == 4):
                return bearing, rng, alt
    if len(body) <= 3 and body.isdigit():
        rng = int(body)
        if 0 <= rng <= 400:
            return bearing, rng, None
    try:
        rng = int(body)
    except ValueError:
        return None
    if 0 <= rng <= 500:
        return bearing, rng, None
    return None


def parse_declare_cue(
    text: str, *, config: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """
    Pull bearing / range / altitude from a DECLARE / VID / upgrade call.

    Bullseye name is always the theater bullseye (ELVIS). Pilots may say
    elvis, bullseye, or omit the name — Whisper mishears of the name are ignored.

    Altitude accepts the book form ('twenty eight thousand') and the
    informal one ('angels 28' / 'angels twenty eight').
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

    # Prefer digits after the cue word / bullseye name so callsign seat
    # numbers ('RAZOR 1') are not mistaken for bearing.
    start = 0
    for i, t in enumerate(toks):
        if t in _DECLARE_CUE_START_WORDS or t in _DECLARE_BE_WORDS:
            start = i + 1
    # If we never saw a cue word, still scan — but skip lone seat digits.
    digit_i = -1
    for i in range(start, len(toks)):
        tok = toks[i]
        if not tok.isdigit():
            continue
        # Seat / flight number alone is never a bullseye packing.
        if len(tok) <= 2 and i + 1 < len(toks) and not toks[i + 1].isdigit():
            # Allow '056 67' pairs: short digit followed by another digit later.
            if not any(t.isdigit() and len(t) <= 3 for t in toks[i + 1 : i + 3]):
                continue
        if len(tok) == 1:
            # Never start on a single seat digit when a longer pack exists later.
            if any(t.isdigit() and len(t) >= 4 for t in toks[i + 1 :]):
                continue
        digit_i = i
        break
    if digit_i < 0 and start > 0:
        # Cue word present but no digits after it — fall back to whole utterance,
        # still skipping lone seat digits.
        for i, tok in enumerate(toks):
            if not tok.isdigit():
                continue
            if len(tok) == 1 and any(t.isdigit() and len(t) >= 4 for t in toks[i + 1 :]):
                continue
            if len(tok) >= 3 or (
                i + 1 < len(toks) and toks[i + 1].isdigit()
            ):
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

    packed = _parse_packed_bullseye(first) if len(first) >= 4 else None
    if packed is not None:
        bearing, range_nm, altitude_ft = packed
        rest = nums[1:]
    elif len(first) == 7:
        # normalize() glues '056 67 28' → '0566728' (then 'k' / thousand).
        bearing = int(first[:3]) % 360
        range_nm = int(first[3:5])
        rest = [first[5:]] + nums[1:]
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

    if altitude_ft is None:
        altitude_ft = _declare_altitude_ft(toks, digit_i, rest)

    return {
        "name": theater_bullseye_name(config),
        "bearing": int(bearing) % 360,
        "range_nm": int(range_nm),
        "altitude_ft": altitude_ft,
    }


def _declare_ones_value(tok: str) -> int | None:
    if tok.isdigit() and len(tok) == 1:
        return int(tok)
    return _DECLARE_ONES.get(tok)


def _declare_tens_thousands(tens: str, ones_tok: str) -> int | None:
    ones = _declare_ones_value(ones_tok)
    if ones is None or ones > 9:
        return None
    return (_DECLARE_TENS[tens] + ones) * 1000


def _declare_altitude_ft(
    toks: list[str], digit_i: int, rest: list[str]
) -> int | None:
    """
    Altitude from the tokens after the bullseye digits.

    Book: 'twenty eight thousand'. Informal: 'angels 28' / 'angels twenty eight'.
    Also 28000, 28 thousand, 28k, and Whisper 'twenty 8 thousand'.
    """
    blob = " ".join(toks[digit_i:])
    if not blob:
        return None

    # angels twenty eight / angels twenty 8 (thousand optional)
    m = re.search(
        rf"\b{_DECLARE_ANGELS}\s+(twenty|thirty|forty|fifty)\s+(\d{{1,2}}|[a-z]+)"
        r"(?:\s+thousands?)?\b",
        blob,
    )
    if m:
        ft = _declare_tens_thousands(m.group(1), m.group(2))
        if ft is not None:
            return ft

    # angels 28 / angel 28 / angles 28
    m = re.search(rf"\b{_DECLARE_ANGELS}\s+(\d{{1,2}})\b", blob)
    if m:
        return int(m.group(1)) * 1000

    # twenty eight thousand / twenty 8 / twenty eight
    m = re.search(
        r"\b(twenty|thirty|forty|fifty)\s+(\d{1,2}|[a-z]+)(?:\s+thousands?)?\b",
        blob,
    )
    if m:
        ft = _declare_tens_thousands(m.group(1), m.group(2))
        if ft is not None:
            return ft

    m = re.search(r"\b(twenty|thirty|forty|fifty)\s+thousands?\b", blob)
    if m:
        return _DECLARE_TENS[m.group(1)] * 1000

    m = re.search(r"(?<!\w)(\d{1,2})\s*k\b", blob)
    if m:
        return int(m.group(1)) * 1000

    m = re.search(r"(?<!\w)(\d{1,2})\s+thousands?\b", blob)
    if m:
        return int(m.group(1)) * 1000

    if rest:
        alt_n = int(rest[0])
        thousand_near = any(t.startswith("thousand") for t in toks[digit_i:])
        if alt_n >= 1000:
            return alt_n
        if thousand_near or alt_n <= 60:
            return alt_n * 1000
        return alt_n
    return None


def _cue_bullseye_error_nm(fix_brg: int, fix_rng: int, cue: dict[str, Any]) -> float:
    """Approximate NM between a unit bullseye and the pilot's DECLARE cue."""
    brg_err = abs(pl.heading_delta(float(fix_brg), float(cue["bearing"])))
    # Lateral arc ≈ range * radians(bearing error)
    lateral = float(fix_rng) * math.radians(brg_err)
    range_err = abs(float(fix_rng) - float(cue["range_nm"]))
    return math.hypot(lateral, range_err)


def declare_cue_score(
    fix_brg: int,
    fix_rng: int,
    feet: int | None,
    cue: dict[str, Any],
    *,
    is_own: bool = False,
) -> float | None:
    """
    Match score in NM, or None when the track fails the hard gates.

    Bullseye position is required. Altitude is a hard reject when the
    pilot said one and the track height is known.
    """
    spatial = _cue_bullseye_error_nm(fix_brg, fix_rng, cue)
    if spatial > DECLARE_CUE_MATCH_NM:
        return None
    score = spatial
    if is_own:
        score += 15.0
    cue_alt = cue.get("altitude_ft")
    if cue_alt is not None and feet is not None:
        alt_err = abs(int(feet) - int(cue_alt))
        if alt_err > DECLARE_CUE_ALT_FT:
            return None
        score += (alt_err / 1000.0) * DECLARE_ALT_WEIGHT_NM_PER_1K
    elif cue_alt is not None:
        score += 4.0
    return score


def prefer_declare_group(
    groups: list[pl.FightGroup],
    *,
    cue: dict[str, Any] | None,
) -> pl.FightGroup | None:
    """Best DECLARE match. Do not guess friendly from a loose / misheard cue."""
    if not groups:
        return None
    best = groups[0]
    if cue is None:
        return best
    if pl.normalize_declaration(best.declaration) != "friendly":
        return best
    for g in groups[1:]:
        if pl.normalize_declaration(g.declaration) == "friendly":
            continue
        if g.distance_nm <= best.distance_nm + DECLARE_HOSTILE_PREFER_NM:
            return g
    if best.distance_nm > DECLARE_CUE_CONFIDENT_NM:
        return None
    return best


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
    ELVIS) and altitude when the pilot said one. Without a cue: nearest air
    in the bubble.
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

    qnh = atc_phrase.metar_altimeter_inhg(config)
    tracks: list[dict[str, Any]] = []
    for unit in air:
        if atc_phrase.caoc_unit_is_picture_fixture(unit):
            continue
        if not atc_phrase.caoc_unit_is_picture_eligible(unit):
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
        fix = atc_phrase.bullseye_for_caoc_unit(
            unit, config, opus=opus, altimeter_inhg=qnh
        )
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
            score = declare_cue_score(
                fix_brg,
                fix_rng,
                _feet(unit.get("altMeters"), altimeter_inhg=qnh),
                cue,
                is_own=is_own,
            )
            if score is None:
                continue
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

        tracks.append(
            _track_dict(unit, fix, float(distance), "", altimeter_inhg=qnh)
        )

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
        if isinstance(state, dict):
            state[_PICTURE_GROUPS_KEY] = []
        return f"{cs}, {agency}, picture clean.", []

    classified = pl.classify_picture(groups)
    if classified.kind == "clean" or not classified.groups:
        if isinstance(state, dict):
            state[_PICTURE_GROUPS_KEY] = []
        return f"{cs}, {agency}, picture clean.", []

    # Head: agency may omit "picture" word when label is traditional — AFTTP
    # examples often start with the label directly after the controller callsign.
    # We keep agency + label for radio clarity.
    if classified.kind == "single":
        g = classified.groups[0]
        clause = pl.core_group_clause(g, include_bullseye=True, include_track=True)
        # SINGLE GROUP folds into one sentence
        body = clause.replace("Single group", "single group", 1)
        remember_picture_groups(state, classified.groups)
        return _with_vid_cue(f"{cs}, {agency}, {body}.", classified.groups), classified.groups

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
    remember_picture_groups(state, classified.groups)
    return _with_vid_cue(". ".join(sentences) + ".", classified.groups), classified.groups


def build_bogey_dope_reply(
    config: dict[str, Any],
    airport: dict[str, Any],
    callsign: str,
    *,
    agency: str = "Blackjack",
    opus: Any = None,
    state: dict[str, Any] | None = None,
) -> tuple[str, list[pl.FightGroup]]:
    """Ch V §11 — magnetic BRAA relative to ownship on closest hostile group."""
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

    brg, rng = pl.braa_from_own(
        own_ll[0], own_ll[1], float(g.lat), float(g.lon), config=config
    )
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
    # Bogey dope names the closest as a usable "group" / "single group" handle.
    g.name = g.name or "group"
    remember_picture_groups(state, [g])
    return _with_vid_cue(f"{cs}, {agency}, {', '.join(bits)}.", [g]), [g]


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
    Short DECLARE query: callsign, agency, current affiliation only.

    Matching uses the pilot's bullseye and altitude when given; the response
    does not read the fix back (e.g. 'Fleece 1, Bandsaw, bandit.').
    Read-only — never PATCH, never auto-upgrade because the agency is Bandsaw
    or the word hostile appeared on a DECLARE [Elvis] cue.
    """
    del channel  # query path ignores agency upgrade rules
    cs = atc_phrase.speak_callsign(callsign)
    cue = parse_declare_cue(transcript or "", config=config)
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
    g = prefer_declare_group(groups, cue=cue)
    if g is None:
        if cue is not None:
            return f"{cs}, {agency}, unable, say again.", []
        return f"{cs}, {agency}, clean.", []
    decl = str(g.declaration or "bogey").strip() or "bogey"
    text = f"{cs}, {agency}, {decl}."
    return _with_vid_cue(text, [g]), [g]


_VID_AFFILIATION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bhostiles?\b|\bhostels?\b|\bhostals?\b", "hostile"),
    (r"\bbandits?\b", "bandit"),
    (r"\bfriend(?:ly)?\b", "friendly"),
    (r"\bbogey\s+spades\b|\bspades\b", "bogey spades"),
    (r"\bbogeys?\b|\bbogies?\b|\bunknown\b", "bogey"),
)

_VID_SET_MARKERS = (
    "vid",
    "visual id",
    "visual identification",
    "id group",
    "eye dee",
    "upgrade",
    "upgrade group",
    "declare as",
    "group is",
    "that's a",
    "thats a",
    "that is a",
)

# Spoken AFTTP labels from the last picture — used for "ID north group, MiG".
_PICTURE_GROUP_LABELS = (
    "north lead group",
    "south lead group",
    "east lead group",
    "west lead group",
    "north trail group",
    "south trail group",
    "east trail group",
    "west trail group",
    "north middle group",
    "south middle group",
    "east middle group",
    "west middle group",
    "northeast group",
    "northwest group",
    "southeast group",
    "southwest group",
    "north group",
    "south group",
    "east group",
    "west group",
    "lead group",
    "trail group",
    "middle group",
    "single group",
    "second group",
    "third group",
    "fourth group",
)


def remember_picture_groups(
    state: dict[str, Any] | None,
    groups: list[pl.FightGroup],
) -> None:
    """Keep the last picture's spoken labels so the crew can ID by name."""
    if not isinstance(state, dict):
        return
    now = time.time()
    rows: list[dict[str, Any]] = []
    for g in groups:
        name = re.sub(r"\s+", " ", str(g.name or "group").strip().casefold())
        if not name:
            name = "group"
        rows.append(
            {
                "name": name,
                "ids": [str(i) for i in (g.unit_ids or []) if str(i).strip()],
                "brg": int(g.bearing),
                "rng": int(g.range_nm),
                "feet": g.feet,
                "declaration": pl.normalize_declaration(g.declaration),
                "seen": now,
            }
        )
    state[_PICTURE_GROUPS_KEY] = rows


def _picture_group_rows(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    raw = state.get(_PICTURE_GROUPS_KEY)
    if not isinstance(raw, list):
        return []
    now = time.time()
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        seen = float(row.get("seen") or 0)
        if seen and now - seen > _PICTURE_GROUPS_TTL_S:
            continue
        out.append(row)
    return out


def _group_name_aliases(name: str) -> list[str]:
    """Longest-first aliases for a pictured label (north group → north, …)."""
    text = re.sub(r"\s+", " ", str(name or "").strip().casefold())
    if not text:
        return []
    aliases = [text]
    if text.endswith(" group"):
        stem = text[: -len(" group")].strip()
        if stem:
            aliases.append(stem)
    # "north lead" from "north lead group"
    parts = text.split()
    if len(parts) >= 2 and parts[-1] == "group":
        aliases.append(" ".join(parts[:-1]))
    # de-dupe, longest first
    return sorted(dict.fromkeys(aliases), key=len, reverse=True)


def match_picture_group_ref(
    transcript: str | None,
    state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """
    Resolve 'north group' / 'lead group' / 'the group' against the last picture.
    """
    rows = _picture_group_rows(state)
    if not rows:
        return None
    text = re.sub(r"[^a-z0-9\s]", " ", str(transcript or "").casefold())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        for alias in _group_name_aliases(str(row.get("name") or "")):
            if not alias:
                continue
            if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text):
                scored.append((len(alias), row))
                break
    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    # One pictured group: "ID group" / "the group" / "that group" is enough.
    if len(rows) == 1 and re.search(
        r"\b(?:the |that |this )?groups?\b", text
    ):
        return rows[0]
    return None


def parse_vid_affiliation(transcript: str | None) -> str | None:
    """
    Affiliation from a VID / ID / upgrade call.

    Explicit bandit / hostile / friendly wins. If the crew only VIDs the
    type (MiG-23, Flanker, …) with no ROE word, default to **bandit** —
    known enemy, not cleared to engage. Say hostile when ROE allows.
    """
    text = re.sub(r"[^a-z0-9\s]", " ", str(transcript or "").casefold())
    text = re.sub(r"\s+", " ", text).strip()
    last: str | None = None
    last_pos = -1
    for pat, decl in _VID_AFFILIATION_PATTERNS:
        for match in re.finditer(pat, text):
            if match.start() >= last_pos:
                last_pos = match.start()
                last = decl
    if last is not None:
        return last
    # Type-only / bare ID with bullseye or named group → bandit (not hostile).
    if any(marker in text for marker in _VID_SET_MARKERS):
        return "bandit"
    if any(
        re.search(rf"(?<!\w){re.escape(label)}(?!\w)", text)
        for label in _PICTURE_GROUP_LABELS
    ):
        return "bandit"
    if re.search(r"(?<!\w)id(?!\w)", text) and re.search(
        r"(?<!\w)groups?(?!\w)", text
    ):
        return "bandit"
    return None


def _fight_group_from_picture_row(row: dict[str, Any]) -> pl.FightGroup:
    feet = row.get("feet")
    try:
        feet_i = int(feet) if feet is not None else None
    except (TypeError, ValueError):
        feet_i = None
    return pl.FightGroup(
        bearing=int(row.get("brg") or 0),
        range_nm=int(row.get("rng") or 0),
        bullseye_name="ELVIS",
        distance_nm=0.0,
        feet=feet_i,
        feet_list=[feet_i] if feet_i is not None else [],
        declaration=pl.normalize_declaration(row.get("declaration")),
        unit_ids=[str(i) for i in (row.get("ids") or []) if str(i).strip()],
        name=str(row.get("name") or "group"),
    )


def build_vid_affiliation_reply(
    config: dict[str, Any],
    airport: dict[str, Any],
    callsign: str,
    *,
    agency: str = "Bandsaw",
    channel: str = "",
    opus: Any = None,
    transcript: str | None = None,
    state: dict[str, Any] | None = None,
) -> tuple[str, list[pl.FightGroup]]:
    """
    Crew VID / affiliation set: confirm on radio and PATCH OPUS.

    Prefer the last picture's label ('north group', 'lead group'). Else
    bullseye digits. Aircraft type is fluff. No ROE word → bandit.
    """
    del channel
    cs = atc_phrase.speak_callsign(callsign)
    target = parse_vid_affiliation(transcript)
    if not target:
        return f"{cs}, {agency}, unable, say affiliation.", []

    named = match_picture_group_ref(transcript, state)
    g: pl.FightGroup | None = None
    if named is not None:
        g = _fight_group_from_picture_row(named)
    else:
        cue = parse_declare_cue(transcript or "", config=config)
        if cue is None:
            return f"{cs}, {agency}, unable, say group or bullseye.", []
        groups, _own, _own_ll = collect_declare_groups(
            config,
            airport,
            opus=opus,
            cue=cue,
            state=state,
            upgrade_hostile=False,
        )
        g = prefer_declare_group(groups, cue=cue)
        if g is None:
            return f"{cs}, {agency}, unable, say again.", []
        try:
            cue_err = float(g.distance_nm)
        except (TypeError, ValueError):
            cue_err = DECLARE_CUE_MATCH_NM + 1.0
        if cue_err > DECLARE_CUE_MATCH_NM:
            return f"{cs}, {agency}, unable, say again.", []

    if g is None or not g.unit_ids:
        return f"{cs}, {agency}, unable, say again.", []

    g.declaration = target
    book = pl.DeclarationMemory.from_state(state)
    book.force(
        g.unit_ids,
        target,
        brg=g.bearing,
        rng=g.range_nm,
        feet=g.feet,
    )
    book.to_state(state)
    # Keep the pictured label in sync for the next ID call.
    if named is not None and isinstance(state, dict):
        for row in list(state.get(_PICTURE_GROUPS_KEY) or []):
            if not isinstance(row, dict):
                continue
            if str(row.get("name") or "") == str(named.get("name") or ""):
                row["declaration"] = target
                row["seen"] = time.time()
    opus_aff = pl.spoken_to_opus_affiliation(target)
    for uid in g.unit_ids:
        if uid:
            atc_phrase.patch_caoc_unit_affiliation(config, uid, opus_aff)
    label = str(g.name or "").strip()
    if label and label not in ("group", "single group"):
        return f"{cs}, {agency}, {label}, {target}.", [g]
    return f"{cs}, {agency}, {target}.", [g]


def _with_vid_cue(text: str, groups: list[pl.FightGroup]) -> str:
    if not any(pl.needs_vid_cue(g) for g in groups):
        return text
    body = text[:-1] if text.endswith(".") else text
    return f"{body}, {pl.VID_INTERCEPT_CUE}."
