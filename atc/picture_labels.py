"""
AFTTP 3-2.8 (09 OCT 2024) traditional PICTURE labels and fight-axis geometry.

Pure helpers — no CAOC I/O. Used by voice_actions to speak doctrine-accurate
RANGE / AZIMUTH / VIC / CHAMPAGNE / WALL / LADDER / BOX calls.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import atc_phrase

# AFTTP Ch IV constants
GROUP_RADIUS_NM = 3.0
DEPTH_WALL_NM = 5.0
ECHELON_MIN_DEPTH_NM = 5.0
AT_BULLSEYE_NM = 5.0
STACK_SEP_FT = 10_000.0
ANCHOR_AZIMUTH_NM = 10.0
MAX_DETAIL_GROUPS = 3

# Aspect-angle buckets (target aspect to fighter), degrees
_HOT_MAX = 30.0
_FLANK_MAX = 60.0
_BEAM_MAX = 120.0


def heading_delta(a: float, b: float) -> float:
    """Signed smallest turn from heading a to b, −180..+180."""
    return (float(b) - float(a) + 180.0) % 360.0 - 180.0


def cardinal8(deg: float) -> str:
    dirs = (
        "north",
        "northeast",
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
    )
    idx = int((float(deg) % 360.0) / 45.0 + 0.5) % 8
    return dirs[idx]


def cardinal4_pair(threat_axis_deg: float) -> tuple[str, str]:
    """
    Outer names for an AZIMUTH/WALL relative to the threat axis.

    Lateral axis = threat + 90°. Prefer NORTH/SOUTH when that axis is closer
    to N–S; else EAST/WEST.
    """
    lat = (float(threat_axis_deg) + 90.0) % 360.0
    # Distance of lat axis to pure north (0) vs pure east (90)
    to_ns = min(abs(heading_delta(lat, 0)), abs(heading_delta(lat, 180)))
    to_ew = min(abs(heading_delta(lat, 90)), abs(heading_delta(lat, 270)))
    if to_ns <= to_ew:
        return "north", "south"
    return "east", "west"


def speak_thousands(feet: int | None) -> str:
    if feet is None:
        return ""
    if feet < 1000:
        return "altitude unknown" if feet < 0 else "low"
    thousands = int(round(feet / 1000.0))
    return f"{atc_phrase.speak_natural_number(thousands)} thousand"


def speak_stack(feet_list: list[int]) -> str:
    """STACK higher first when altitude spread ≥ 10,000 ft."""
    if not feet_list:
        return ""
    uniq = sorted({int(round(f / 1000.0) * 1000) for f in feet_list}, reverse=True)
    if len(uniq) == 1:
        return speak_thousands(uniq[0])
    if uniq[0] - uniq[-1] < STACK_SEP_FT:
        return speak_thousands(max(feet_list))
    parts = [speak_thousands(f) for f in uniq if speak_thousands(f)]
    if len(parts) == 2:
        return f"stack {parts[0]} and {parts[1]}"
    return "stack " + ", ".join(parts[:-1]) + f", {parts[-1]}"


def speak_bullseye_location(name: str, bearing: int, range_nm: int) -> str:
    """BULLSEYE or AT BULLSEYE per AFTTP (< 5 nm)."""
    be = atc_phrase.speak_bullseye_fix(name)
    if int(range_nm) < AT_BULLSEYE_NM:
        return f"at {be}" if be != "bullseye" else "at bullseye"
    return atc_phrase.speak_picture_bullseye(name, bearing, range_nm)


def speak_braa(bearing: int, range_nm: int) -> str:
    """Spoken magnetic BRAA (HUD / compass heading)."""
    brg = max(0, min(360, int(bearing))) % 360
    rng = max(0, int(range_nm))
    return (
        f"BRAA {atc_phrase.speak_digits(f'{brg:03d}')}, "
        f"{atc_phrase.speak_natural_number(rng)}"
    )


def track_from_heading(heading_deg: float | None) -> str | None:
    if heading_deg is None:
        return None
    return f"track {cardinal8(heading_deg)}"


def aspect_to_fighter(
    *,
    own_lat: float,
    own_lon: float,
    tgt_lat: float,
    tgt_lon: float,
    tgt_heading: float | None,
) -> str | None:
    """
    HOT / FLANK / BEAM / DRAG (+ cardinal for non-hot) from target aspect angle.
    Aspect ≈ angle between target nose and line-of-sight to the fighter.
    """
    if tgt_heading is None:
        return None
    # Bearing from target to ownship
    brg_to_own = _bearing_deg(tgt_lat, tgt_lon, own_lat, own_lon)
    aa = abs(heading_delta(tgt_heading, brg_to_own))
    if aa <= _HOT_MAX:
        return "hot"
    # Cardinal of target track for flank/beam/drag fill-in
    card = cardinal8(tgt_heading)
    if aa <= _FLANK_MAX:
        return f"flank {card}"
    if aa <= _BEAM_MAX:
        return f"beam {card}"
    return f"drag {card}"


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dλ = math.radians(lon2 - lon1)
    y = math.sin(dλ) * math.cos(φ2)
    x = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(dλ)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def braa_from_own(
    own_lat: float,
    own_lon: float,
    tgt_lat: float,
    tgt_lon: float,
    *,
    config: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Magnetic BRAA bearing (HUD / compass) from ownship to the group."""
    brg = int(
        round(
            atc_phrase.magnetic_bearing_deg(
                own_lat, own_lon, tgt_lat, tgt_lon, config=config
            )
        )
    ) % 360
    rng = int(round(atc_phrase._haversine_nm(own_lat, own_lon, tgt_lat, tgt_lon)))
    return brg, max(0, rng)


@dataclass
class FightGroup:
    """One GROUP in fighter-relative coordinates."""

    bearing: int
    range_nm: int
    bullseye_name: str
    distance_nm: float  # to fighters
    count: int = 1
    feet: int | None = None
    feet_list: list[int] = field(default_factory=list)
    heading_deg: float | None = None
    lat: float | None = None
    lon: float | None = None
    label: str = ""
    object: str = ""
    declaration: str = "hostile"
    unit_ids: list[str] = field(default_factory=list)
    coalition: str = ""
    affiliation: str = ""
    ti_training: bool = False
    # Filled by classify
    name: str = "group"
    cross_nm: float = 0.0  # + left / − right of threat axis from fighters
    along_nm: float = 0.0  # range along threat axis ≈ distance_nm


@dataclass
class PictureClass:
    kind: str  # single|range|azimuth|wall|champagne|vic|ladder|box|core|leading_edge
    head: str  # spoken label + dimensions + amps (no callsign/agency)
    groups: list[FightGroup]
    shared_track: str | None = None
    total_count: int = 0


def _project(groups: list[FightGroup], threat_axis_deg: float) -> None:
    for g in groups:
        # Cross-track from fighters: positive toward threat_axis + 90°
        delta = heading_delta(threat_axis_deg, _bearing_from_dist(g))
        # Prefer actual bearing from own if we stored fight bearing via distance
        brg_from_own = getattr(g, "_brg_from_own", None)
        if brg_from_own is not None:
            delta = heading_delta(threat_axis_deg, brg_from_own)
        g.along_nm = float(g.distance_nm) * math.cos(math.radians(delta))
        g.cross_nm = float(g.distance_nm) * math.sin(math.radians(delta))


def _bearing_from_dist(g: FightGroup) -> float:
    brg = getattr(g, "_brg_from_own", None)
    if brg is not None:
        return float(brg)
    return float(g.bearing)


def enrich_fight_bearings(
    groups: list[FightGroup],
    own_ll: tuple[float, float] | None,
) -> None:
    if not own_ll:
        return
    olat, olon = own_ll
    for g in groups:
        if g.lat is None or g.lon is None:
            continue
        try:
            g._brg_from_own = _bearing_deg(olat, olon, float(g.lat), float(g.lon))  # type: ignore[attr-defined]
        except (TypeError, ValueError):
            pass


def threat_axis_deg(groups: list[FightGroup]) -> float:
    """Mean bearing from fighters to groups (or bullseye bearing fallback)."""
    bearings: list[float] = []
    for g in groups:
        b = getattr(g, "_brg_from_own", None)
        bearings.append(float(b if b is not None else g.bearing))
    if not bearings:
        return 0.0
    # Circular mean
    x = sum(math.cos(math.radians(b)) for b in bearings)
    y = sum(math.sin(math.radians(b)) for b in bearings)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _width_depth(groups: list[FightGroup]) -> tuple[float, float]:
    if not groups:
        return 0.0, 0.0
    crosses = [g.cross_nm for g in groups]
    dists = [g.distance_nm for g in groups]
    width = max(crosses) - min(crosses) if len(crosses) > 1 else 0.0
    depth = max(dists) - min(dists) if len(dists) > 1 else 0.0
    return abs(width), abs(depth)


def _shared_track(groups: list[FightGroup]) -> tuple[str | None, bool]:
    """
    Returns (spoken track amp or None, all_hot).
    all_hot → TRACK assumed, omit amplification.
    """
    tracks: list[str] = []
    headings: list[float] = []
    for g in groups:
        if g.heading_deg is None:
            return None, False
        headings.append(float(g.heading_deg))
        t = track_from_heading(g.heading_deg)
        if t:
            tracks.append(t)
    if not tracks:
        return None, False
    # Hot to fight axis ≈ heading toward fighters ≈ reciprocal of threat axis
    # Approximate: headings within 30° of each other
    mean_h = headings[0]
    if any(abs(heading_delta(mean_h, h)) > 25 for h in headings[1:]):
        return None, False
    card = cardinal8(mean_h)
    return f"track {card}", False


def _nm_words(n: float) -> str:
    return atc_phrase.speak_natural_number(max(0, int(round(n))))


def _fill_ins(g: FightGroup) -> list[str]:
    parts: list[str] = []
    if g.count >= 3:
        parts.append("heavy")
        parts.append(f"{atc_phrase.speak_natural_number(g.count)} contacts")
    elif g.count > 1:
        parts.append(f"{atc_phrase.speak_natural_number(g.count)} contacts")
    return parts


def group_altitude_speech(g: FightGroup) -> str:
    feet = list(g.feet_list) if g.feet_list else ([g.feet] if g.feet is not None else [])
    return speak_stack([int(f) for f in feet if f is not None])


def core_group_clause(
    g: FightGroup,
    *,
    include_bullseye: bool = True,
    include_track: bool = True,
    aspect: str | None = None,
) -> str:
    parts: list[str] = [g.name]
    if include_bullseye:
        parts.append(speak_bullseye_location(g.bullseye_name, g.bearing, g.range_nm))
    alt = group_altitude_speech(g)
    if alt:
        parts.append(alt)
    if aspect:
        parts.append(aspect)
    elif include_track:
        tr = track_from_heading(g.heading_deg)
        if tr:
            parts.append(tr)
    parts.append(g.declaration)
    parts.extend(_fill_ins(g))
    text = ", ".join(p for p in parts if p)
    return text[:1].upper() + text[1:] if text else ""


def classify_picture(groups: list[FightGroup]) -> PictureClass:
    """
    Assign an AFTTP traditional label (or core / leading_edge fallback).
    Mutates group.name on the returned groups.
    """
    if not groups:
        return PictureClass(kind="clean", head="picture clean", groups=[], total_count=0)

    total = len(groups)
    # Priority: closest first (already expected sorted)
    ordered = sorted(groups, key=lambda g: g.distance_nm)
    axis = threat_axis_deg(ordered)
    _project(ordered, axis)
    shown = ordered[:MAX_DETAIL_GROUPS]
    width, depth = _width_depth(shown if len(ordered) <= MAX_DETAIL_GROUPS else ordered[:4])
    shared, _ = _shared_track(shown)

    if total == 1:
        g = shown[0]
        g.name = "single group"
        return PictureClass(
            kind="single",
            head="single group",
            groups=shown,
            shared_track=None,  # core includes track on the group
            total_count=1,
        )

    hi, lo = cardinal4_pair(axis)

    if total == 2:
        a, b = shown[0], shown[1]  # a closer
        # ECHELON / RANGE / AZIMUTH
        if depth > width:
            kind = "range"
            a.name, b.name = "lead group", "trail group"
            dim = _nm_words(depth)
            head = f"two groups range {dim}"
            if width > 3 and depth > ECHELON_MIN_DEPTH_NM:
                # Offset → echelon toward the trail's cross side
                ech = hi if b.cross_nm >= a.cross_nm else lo
                # Prefer subcardinal of offset direction
                ech_brg = (axis + (90 if b.cross_nm >= 0 else -90)) % 360
                head += f", echelon {cardinal8(ech_brg)}"
        else:
            kind = "azimuth"
            if a.cross_nm >= b.cross_nm:
                a.name, b.name = f"{hi} group", f"{lo} group"
            else:
                a.name, b.name = f"{lo} group", f"{hi} group"
            dim = _nm_words(max(width, 1))
            head = f"two groups azimuth {dim}"
            # ECHELON when offset aft/forward enough (depth > 5) but still wider
            if depth > ECHELON_MIN_DEPTH_NM:
                aft = b if b.distance_nm >= a.distance_nm else a
                ech = cardinal8((axis + (90 if aft.cross_nm >= 0 else -90)) % 360)
                head += f", echelon {ech}"
        amps = _amps(head, shared, opening=None)
        return PictureClass(
            kind=kind, head=amps, groups=shown, shared_track=shared, total_count=2
        )

    # 3+ groups
    if total >= 4 and _is_box(ordered[:4]):
        box = ordered[:4]
        _name_box(box, hi, lo)
        w, d = _width_depth(box)
        head = f"four group box, {_nm_words(w)} wide, {_nm_words(d)} deep"
        return PictureClass(
            kind="box",
            head=_amps(head, shared, None),
            groups=box[:MAX_DETAIL_GROUPS],
            shared_track=shared,
            total_count=total,
        )

    if depth <= DEPTH_WALL_NM or (total >= 3 and width >= depth * 1.2 and depth <= DEPTH_WALL_NM + 2):
        # WALL
        wall = shown if total <= 5 else shown
        _name_wall(wall, hi, lo)
        head = f"{_count_words(total)} group wall, {_nm_words(width)} wide"
        if _is_weighted(wall):
            side = hi if sum(g.cross_nm for g in wall) >= 0 else lo
            head += f", weighted {side}"
        return PictureClass(
            kind="wall",
            head=_amps(head, shared, None),
            groups=wall[:MAX_DETAIL_GROUPS],
            shared_track=shared,
            total_count=total,
        )

    # Depth > 5 → champagne / vic / ladder
    near = [g for g in ordered if g.distance_nm <= ordered[0].distance_nm + DEPTH_WALL_NM]
    far = [g for g in ordered if g not in near]

    if total == 3:
        g0, g1, g2 = ordered[0], ordered[1], ordered[2]
        # Ladder: small width, large depth
        if width <= DEPTH_WALL_NM and depth > DEPTH_WALL_NM:
            g0.name, g1.name, g2.name = "lead group", "middle group", "trail group"
            head = f"three group ladder, {_nm_words(depth)} deep"
            return PictureClass(
                kind="ladder",
                head=_amps(head, shared, None),
                groups=ordered,
                shared_track=shared,
                total_count=3,
            )
        # Champagne: two near, one far
        if len(near) >= 2 and len(far) == 1:
            leads = sorted(near[:2], key=lambda g: g.cross_nm, reverse=True)
            if leads[0].cross_nm >= leads[1].cross_nm:
                leads[0].name, leads[1].name = f"{hi} lead group", f"{lo} lead group"
            else:
                leads[0].name, leads[1].name = f"{lo} lead group", f"{hi} lead group"
            far[0].name = "trail group"
            w = abs(leads[0].cross_nm - leads[1].cross_nm)
            head = (
                f"three group champagne, {_nm_words(w)} wide, {_nm_words(depth)} deep"
            )
            named = [leads[0], leads[1], far[0]]
            return PictureClass(
                kind="champagne",
                head=_amps(head, shared, None),
                groups=named,
                shared_track=shared,
                total_count=3,
            )
        # Vic: one near, two far
        if len(near) == 1 and len(far) >= 2:
            near[0].name = "lead group"
            trails = sorted(far[:2], key=lambda g: g.cross_nm, reverse=True)
            if trails[0].cross_nm >= trails[1].cross_nm:
                trails[0].name = f"{hi} trail group"
                trails[1].name = f"{lo} trail group"
            else:
                trails[0].name = f"{lo} trail group"
                trails[1].name = f"{hi} trail group"
            w = abs(trails[0].cross_nm - trails[1].cross_nm)
            head = f"three group vic, {_nm_words(depth)} deep, {_nm_words(w)} wide"
            return PictureClass(
                kind="vic",
                head=_amps(head, shared, None),
                groups=[near[0], trails[0], trails[1]],
                shared_track=shared,
                total_count=3,
            )

    # Ladder for 3+ with small width
    if width <= DEPTH_WALL_NM and depth > DEPTH_WALL_NM and total >= 3:
        for i, g in enumerate(shown):
            if i == 0:
                g.name = "lead group"
            elif i == len(shown) - 1:
                g.name = "trail group"
            elif len(shown) == 3:
                g.name = "middle group"
            else:
                g.name = f"{('second', 'third', 'fourth')[min(i - 1, 2)]} group"
        head = f"{_count_words(total)} group ladder, {_nm_words(depth)} deep"
        return PictureClass(
            kind="ladder",
            head=_amps(head, shared, None),
            groups=shown,
            shared_track=shared,
            total_count=total,
        )

    # Fallback: core / leading edge
    for i, g in enumerate(shown):
        g.name = "group" if total > MAX_DETAIL_GROUPS else (
            ("lead group", "second group", "third group")[min(i, 2)]
        )
    if total > MAX_DETAIL_GROUPS:
        head = f"{atc_phrase.speak_natural_number(total)} groups"
    else:
        head = f"{atc_phrase.speak_natural_number(total)} groups"
    return PictureClass(
        kind="core",
        head=_amps(head, shared, None),
        groups=shown,
        shared_track=shared,
        total_count=total,
    )


def _count_words(n: int) -> str:
    if n == 3:
        return "three"
    if n == 4:
        return "four"
    if n == 5:
        return "five"
    return atc_phrase.speak_natural_number(n)


def _amps(head: str, shared_track: str | None, opening: str | None) -> str:
    bits = [head]
    if opening:
        bits[0] = f"{head} {opening}"
    if shared_track:
        # If all hot we still may get a track string — AFTTP: omit when HOT assumed.
        # shared_track helper returns track always when headings agree; strip if "hot"
        # Not computing hot here; include shared track when present.
        bits.append(shared_track)
    return ", ".join(bits)


def _is_weighted(groups: list[FightGroup]) -> bool:
    if len(groups) < 3:
        return False
    crosses = sorted(g.cross_nm for g in groups)
    span = crosses[-1] - crosses[0]
    if span < 1:
        return False
    # thirds
    left = crosses[0] + span / 3
    right = crosses[0] + 2 * span / 3
    # middle third occupancy
    mid = [c for c in crosses if left <= c <= right]
    return len(mid) < len(groups) - 1 and (crosses[0] < left - span * 0.05 or crosses[-1] > right + span * 0.05)


def _is_box(groups: list[FightGroup]) -> bool:
    if len(groups) < 4:
        return False
    g = sorted(groups[:4], key=lambda x: x.distance_nm)
    near, far = g[:2], g[2:4]
    near_depth = abs(near[0].distance_nm - near[1].distance_nm)
    far_depth = abs(far[0].distance_nm - far[1].distance_nm)
    gap = min(x.distance_nm for x in far) - max(x.distance_nm for x in near)
    return near_depth <= DEPTH_WALL_NM and far_depth <= DEPTH_WALL_NM and gap > DEPTH_WALL_NM


def _name_box(groups: list[FightGroup], hi: str, lo: str) -> None:
    g = sorted(groups[:4], key=lambda x: x.distance_nm)
    near = sorted(g[:2], key=lambda x: x.cross_nm, reverse=True)
    far = sorted(g[2:4], key=lambda x: x.cross_nm, reverse=True)
    near[0].name = f"{hi} lead group"
    near[1].name = f"{lo} lead group"
    far[0].name = f"{hi} trail group"
    far[1].name = f"{lo} trail group"


def _name_wall(groups: list[FightGroup], hi: str, lo: str) -> None:
    ordered = sorted(groups, key=lambda g: g.cross_nm, reverse=True)
    if len(ordered) == 1:
        ordered[0].name = "group"
        return
    if len(ordered) == 2:
        ordered[0].name = f"{hi} group"
        ordered[1].name = f"{lo} group"
        return
    ordered[0].name = f"{hi} group"
    ordered[-1].name = f"{lo} group"
    mids = ordered[1:-1]
    if len(mids) == 1:
        mids[0].name = "middle group"
    else:
        for i, g in enumerate(mids):
            if i == len(mids) // 2 and len(mids) % 2 == 1:
                g.name = "middle group"
            elif g.cross_nm >= 0:
                g.name = f"{hi} middle group"
            else:
                g.name = f"{lo} middle group"


def roll_enemy_declaration() -> str:
    """
    Fallback for enemy-side tracks with no OPUS affiliation.

    Known red air is bandit (not the UNKNOWN VID path). Random rolls
    used to mix bogey/hostile onto every new group — that is gone.
    """
    return "bandit"


def normalize_coalition(raw: Any) -> str:
    """CAOC / DCS side → red | blue | neutral | ''."""
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, (int, float)):
        try:
            return {0: "neutral", 1: "red", 2: "blue", 3: "neutral"}.get(int(raw), "")
        except (TypeError, ValueError):
            return ""
    text = str(raw or "").strip().casefold()
    if text in ("1", "red", "r", "enm", "enemy"):
        return "red"
    if text in ("2", "blue", "b", "friend"):
        return "blue"
    if text in (
        "0",
        "3",
        "neutral",
        "neutrals",
        "n",
        "civilian",
        "civ",
        "civil",
    ):
        return "neutral"
    if "neutral" in text or "civil" in text:
        return "neutral"
    return ""


def is_enemy_side(
    coalition: Any = None,
    hostile_side: str = "red",
) -> bool:
    """True when CAOC tagged this track as the enemy coalition."""
    side = normalize_coalition(coalition)
    host = normalize_coalition(hostile_side) or "red"
    return bool(side) and side == host and side in ("red", "blue")


def is_friendly_side(
    coalition: Any = None,
    hostile_side: str = "red",
) -> bool:
    """True when CAOC tagged this track as the friendly coalition."""
    side = normalize_coalition(coalition)
    host = normalize_coalition(hostile_side) or "red"
    return side in ("red", "blue") and side != host


def normalize_declaration(raw: str | None) -> str:
    text = str(raw or "").strip().casefold()
    text = re.sub(r"\s+", " ", text)
    if text in ("spades", "bogey spades", "spade"):
        return "bogey spades"
    if text in ("hostile", "hostiles"):
        return "hostile"
    if text in ("bandit", "bandits"):
        return "bandit"
    if text in ("friendly", "friend"):
        return "friendly"
    if text in ("bogey", "bogie", "unknown", "unk"):
        return "bogey"
    return text or "bogey"


_OPUS_AFFILIATION_UNKNOWN = frozenset(
    {"unknown", "unk", "undeclared", "pending", "u"}
)
_TRUTHY = frozenset({True, 1, 1.0, "1", "true", "yes", "on"})
VID_INTERCEPT_CUE = "recommend intercept for visual ID"
_TI_TRAINING_PREFIX = "TI_TRAINING_"


def normalize_opus_affiliation(raw: Any) -> str | None:
    """
    OPUS CAOC affiliation → spoken declaration, or None if unset.

    UNKNOWN / undeclared → bogey spades (VID training path).
    """
    if raw is None or isinstance(raw, bool):
        return None
    text = re.sub(r"\s+", " ", str(raw).strip().casefold())
    if not text:
        return None
    if text in _OPUS_AFFILIATION_UNKNOWN:
        return "bogey spades"
    if text in ("hostile", "hostiles"):
        return "hostile"
    if text in ("bandit", "bandits"):
        return "bandit"
    if text in ("friendly", "friend"):
        return "friendly"
    if text in ("spades", "bogey spades", "spade"):
        return "bogey spades"
    if text in ("bogey", "bogie"):
        return "bogey"
    return None


def spoken_to_opus_affiliation(raw: str | None) -> str:
    """Spoken declaration → OPUS affiliation enum."""
    decl = normalize_declaration(raw)
    if decl == "hostile":
        return "HOSTILE"
    if decl == "bandit":
        return "BANDIT"
    if decl == "friendly":
        return "FRIENDLY"
    return "UNKNOWN"


def caoc_unit_is_ti_training(unit: dict[str, Any] | None = None, **flags: Any) -> bool:
    """True when OPUS `tiTraining` or a TI_TRAINING_ group/name is present."""
    if flags.get("ti_training") in _TRUTHY:
        return True
    if not isinstance(unit, dict):
        blob = " ".join(
            str(flags.get(k) or "")
            for k in ("name", "group_name", "groupName")
        )
        return _TI_TRAINING_PREFIX in blob.upper()
    for key in ("tiTraining", "ti_training"):
        if unit.get(key) in _TRUTHY:
            return True
    blob = " ".join(
        str(unit.get(k) or "")
        for k in (
            "name",
            "groupName",
            "unitName",
            "flightLabel",
            "unitCallsign",
            "displayCallsign",
        )
    )
    extra = " ".join(str(flags.get(k) or "") for k in ("name", "group_name"))
    return _TI_TRAINING_PREFIX in f"{blob} {extra}".upper()


def picture_include_unit(unit: dict[str, Any] | None, hostile_side: str = "red") -> bool:
    """
    Whether a CAOC air track belongs on picture / bogey dope.

    Affiliation UNKNOWN/BANDIT/HOSTILE, undeclared TI, or legacy enemy coalition.
    FRIENDLY is never pictured.
    """
    if not isinstance(unit, dict):
        return False
    aff = normalize_opus_affiliation(unit.get("affiliation"))
    if aff == "friendly":
        return False
    if aff in ("bogey", "bogey spades", "bandit", "hostile"):
        return True
    if caoc_unit_is_ti_training(unit):
        return True
    side = normalize_coalition(unit.get("coalition"))
    host = normalize_coalition(hostile_side) or "red"
    return bool(side) and side == host


def needs_vid_cue(group: FightGroup | None = None, **kwargs: Any) -> bool:
    """UNKNOWN / undeclared TI only — not known bandit or hostile."""
    aff_raw = kwargs.get("affiliation")
    ti = bool(kwargs.get("ti_training"))
    decl = kwargs.get("declaration")
    if group is not None:
        aff_raw = aff_raw if aff_raw is not None else group.affiliation
        ti = ti or bool(group.ti_training)
        decl = decl if decl is not None else group.declaration
    aff = normalize_opus_affiliation(aff_raw)
    if aff in ("hostile", "bandit", "friendly"):
        return False
    if aff in ("bogey", "bogey spades"):
        return True
    spoken = normalize_declaration(decl) if decl is not None else ""
    return ti and spoken in ("bogey", "bogey spades", "")


_DECL_RANK = {
    "friendly": -1,
    "bogey": 0,
    "bogey spades": 1,
    "bandit": 2,
    "hostile": 3,
}

# How close a stored group must be (bullseye NM) when CAOC ids are missing.
_DECL_MATCH_NM = 8.0
_DECL_MATCH_ALT_FT = 8000
_DECL_TTL_S = 45 * 60
_DECL_MAX_ROWS = 64
_STATE_KEY = "picture_declarations"


def declaration_rank(raw: str | None) -> int:
    return _DECL_RANK.get(normalize_declaration(raw), 0)


def higher_declaration(a: str | None, b: str | None) -> str:
    """Keep the higher threat label. Hostile never loses to bandit/bogey."""
    na, nb = normalize_declaration(a), normalize_declaration(b)
    return na if declaration_rank(na) >= declaration_rank(nb) else nb


def agency_can_upgrade_hostile(agency: str | None = None, channel: str | None = None) -> bool:
    """Bandsaw (AWACS) may upgrade a group to HOSTILE."""
    blob = f"{agency or ''} {channel or ''}".casefold()
    return any(tok in blob for tok in ("bandsaw", "ansa", "bansaw"))


def transcript_upgrades_hostile(transcript: str | None) -> bool:
    """Flight lead calling the contact hostile (declare hostile / that's a hostile)."""
    text = re.sub(r"[^a-z0-9\s]", " ", str(transcript or "").casefold())
    text = re.sub(r"\s+", " ", text).strip()
    return bool(re.search(r"\bhostiles?\b", text))


def bullseye_error_nm(brg: int, rng: int, other_brg: int, other_rng: int) -> float:
    brg_err = abs(heading_delta(float(brg), float(other_brg)))
    lateral = float(rng) * math.radians(brg_err)
    range_err = abs(float(rng) - float(other_rng))
    return math.hypot(lateral, range_err)


def _id_set(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(x).strip() for x in raw if str(x).strip()}
    text = str(raw).strip()
    return {text} if text else set()


class DeclarationMemory:
    """
    Sticky C2 declarations for the sortie.

    Prefer OPUS `affiliation` when present. UNKNOWN / TI stay bogey spades
    until a VID/upgrade intent writes back. Enemy-side tracks with no
    affiliation default to bandit (not the VID path). Local memory fills
    the gap until the next CAOC poll.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = []
        for row in rows or []:
            if isinstance(row, dict) and row.get("declaration"):
                self.rows.append(dict(row))

    @classmethod
    def from_state(cls, state: dict[str, Any] | None) -> "DeclarationMemory":
        rows = []
        if isinstance(state, dict):
            raw = state.get(_STATE_KEY)
            if isinstance(raw, list):
                rows = [r for r in raw if isinstance(r, dict)]
        return cls(rows)

    def to_state(self, state: dict[str, Any] | None) -> None:
        if isinstance(state, dict):
            state[_STATE_KEY] = self.to_rows()

    def to_rows(self) -> list[dict[str, Any]]:
        self._prune(time.time())
        out: list[dict[str, Any]] = []
        for row in self.rows[-_DECL_MAX_ROWS:]:
            out.append(
                {
                    "ids": sorted(_id_set(row.get("ids"))),
                    "brg": int(row.get("brg") or 0),
                    "rng": int(row.get("rng") or 0),
                    "feet": row.get("feet"),
                    "declaration": normalize_declaration(row.get("declaration")),
                    "seen": float(row.get("seen") or 0),
                }
            )
        return out

    def _prune(self, now: float) -> None:
        keep: list[dict[str, Any]] = []
        for row in self.rows:
            seen = float(row.get("seen") or 0)
            if seen and now - seen > _DECL_TTL_S:
                continue
            keep.append(row)
        self.rows = keep[-_DECL_MAX_ROWS:]

    def _hits(
        self,
        ids: set[str],
        brg: int | None,
        rng: int | None,
        feet: int | None,
    ) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for row in self.rows:
            row_ids = _id_set(row.get("ids"))
            if ids and row_ids:
                if ids & row_ids:
                    hits.append(row)
                continue
            if brg is None or rng is None:
                continue
            try:
                err = bullseye_error_nm(
                    int(brg),
                    int(rng),
                    int(row.get("brg") or 0),
                    int(row.get("rng") or 0),
                )
            except (TypeError, ValueError):
                continue
            if err > _DECL_MATCH_NM:
                continue
            row_ft = row.get("feet")
            if feet is not None and row_ft is not None:
                try:
                    if abs(int(feet) - int(row_ft)) > _DECL_MATCH_ALT_FT:
                        continue
                except (TypeError, ValueError):
                    pass
            hits.append(row)
        return hits

    def lookup(
        self,
        ids: Any,
        *,
        brg: int | None = None,
        rng: int | None = None,
        feet: int | None = None,
    ) -> str | None:
        hits = self._hits(_id_set(ids), brg, rng, feet)
        if not hits:
            return None
        best = hits[0]["declaration"]
        for row in hits[1:]:
            best = higher_declaration(best, row.get("declaration"))
        return normalize_declaration(best)

    def assign(
        self,
        ids: Any,
        *,
        brg: int | None = None,
        rng: int | None = None,
        feet: int | None = None,
        coalition: str | None = None,
        hostile_side: str = "red",
        upgrade_hostile: bool = False,
        affiliation: Any = None,
        ti_training: bool = False,
    ) -> str:
        """
        Return the declaration for this group.

        OPUS affiliation wins when present. TI / UNKNOWN never invent
        bandit or hostile from the airframe or group name.
        """
        now = time.time()
        self._prune(now)
        idset = _id_set(ids)
        stored = self.lookup(idset, brg=brg, rng=rng, feet=feet)
        friend = is_friendly_side(coalition, hostile_side)
        enemy = is_enemy_side(coalition, hostile_side)
        opus = normalize_opus_affiliation(affiliation)
        ti = bool(ti_training)

        if opus is not None:
            decl = opus
        elif ti:
            decl = stored if stored in ("bogey", "bogey spades") else "bogey spades"
        elif friend:
            decl = "friendly"
        elif enemy:
            if stored and stored != "friendly":
                decl = stored
            else:
                decl = roll_enemy_declaration()
            if upgrade_hostile:
                decl = "hostile"
            if stored == "hostile":
                decl = "hostile"
        elif stored in ("friendly", "bogey", "bogey spades"):
            decl = stored
        else:
            # Unknown / neutral. Never keep a leftover hostile or bandit.
            decl = "bogey spades"
        decl = normalize_declaration(decl)
        self._remember(idset, brg, rng, feet, decl, now)
        return decl

    def _remember(
        self,
        ids: set[str],
        brg: int | None,
        rng: int | None,
        feet: int | None,
        declaration: str,
        now: float,
    ) -> None:
        hits = self._hits(ids, brg, rng, feet)
        merged_ids = set(ids)
        keep: list[dict[str, Any]] = []
        used = {id(r) for r in hits}
        for row in self.rows:
            if id(row) in used:
                merged_ids |= _id_set(row.get("ids"))
                continue
            keep.append(row)
        keep.append(
            {
                "ids": sorted(merged_ids),
                "brg": int(brg or 0),
                "rng": int(rng or 0),
                "feet": feet,
                "declaration": declaration,
                "seen": now,
            }
        )
        self.rows = keep

    def upgrade_to_hostile(
        self,
        ids: Any,
        *,
        brg: int | None = None,
        rng: int | None = None,
        feet: int | None = None,
        coalition: str | None = None,
        hostile_side: str = "red",
    ) -> str:
        """
        Bandsaw / flight-lead upgrade.

        Never downgrade. Never flip a friendly or a CAOC-neutral track.
        """
        stored = self.lookup(ids, brg=brg, rng=rng, feet=feet)
        if stored == "friendly":
            return "friendly"
        if not is_enemy_side(coalition, hostile_side):
            if stored in ("friendly", "bogey", "bogey spades"):
                return stored
            return "bogey spades"
        self._remember(
            _id_set(ids), brg, rng, feet, "hostile", time.time()
        )
        return "hostile"

    def force(
        self,
        ids: Any,
        declaration: str,
        *,
        brg: int = 0,
        rng: int = 0,
        feet: int | None = None,
    ) -> str:
        """Test helper: pin a declaration."""
        decl = normalize_declaration(declaration)
        self._remember(_id_set(ids), brg, rng, feet, decl, time.time())
        return decl


def declaration_for_coalition(
    coalition: str | None,
    hostile_side: str,
) -> str:
    """
    Coalition → spoken declaration (no memory).

    Prefer DeclarationMemory.assign so labels stick across picture / declare /
    bogey dope. This remains for one-shot tests.
    """
    if is_friendly_side(coalition, hostile_side):
        return "friendly"
    if is_enemy_side(coalition, hostile_side):
        return "bandit"
    if normalize_coalition(coalition) or not str(coalition or "").strip():
        return "bogey spades"
    return "bogey"


def declare_may_upgrade_hostile(
    group: FightGroup,
    *,
    agency: str | None = None,
    channel: str | None = None,
    transcript: str | None = None,
    hostile_side: str = "red",
) -> bool:
    """Bandsaw / lead may call HOSTILE only on the enemy coalition."""
    if normalize_declaration(group.declaration) == "friendly":
        return False
    if not (
        agency_can_upgrade_hostile(agency, channel)
        or transcript_upgrades_hostile(transcript)
    ):
        return False
    return is_enemy_side(group.coalition, hostile_side)
