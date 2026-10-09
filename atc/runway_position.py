"""
Where the flight physically is, from the live Opus CAOC radar feed.

Answers the questions ATC would answer by looking out the window:
  * is the whole flight lined up on the runway (→ cleared for takeoff)
  * is the whole flight at the EOR (→ monitor tower)
  * is the whole flight inside any other area someone drew, which is how a step
    in a flow arms itself off a zone rather than off a spoken call

All geometry is done in the feed's own x/z metres rather than lat/lon. The CAOC
x/z origin is the Nellis reference point, so around KLSV those metres are already
a local, distortion-free frame — no projection error to reason about.

Convention (matches atc_phrase.caoc_xz_to_ll, which is what the CAOC map uses):
    x → east, z → north.

Caveats worth knowing when reading a verdict:
  * `groundSpeedMps` in the feed reads 0.0 even for AI at altitude, so speed here
    is derived from position deltas between polls instead.
  * `headingDeg` is assumed true. If it turns out to carry magnetic, the whole
    field is off by one declination — `position_heading_offset_deg` corrects it
    without touching the geometry.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import atc_phrase

FT_PER_M = 3.28084

# Defaults chosen so a jet stopped on the centreline passes comfortably while a
# jet on the parallel taxiway does not. All overridable from config.
DEFAULTS: dict[str, float] = {
    "position_lateral_margin_m": 20.0,
    "position_box_m": 900.0,
    "position_behind_threshold_m": 60.0,
    "position_heading_tolerance_deg": 30.0,
    "position_heading_offset_deg": 0.0,
    "position_alt_tolerance_m": 60.0,
    "position_max_speed_mps": 12.0,
    # position_max_speed_mps is loose enough (23 kt) that a jet still taxiing into
    # position reads as in position. A step that asks for "settled" wants the
    # difference between stopped and rolling, which is much tighter.
    "position_settled_speed_mps": 2.0,
    "eor_radius_m": 250.0,
    "eor_max_speed_mps": 12.0,
    "auto_clearance_dwell_s": 3.0,
    # Last ~2,000 ft of the landing roll — Tower's exit / contact Ground call.
    "runway_end_remaining_m": 600.0,
    # Rollout vs a low pass / missed: wheels on the deck and slowing, not
    # 150 kt at 100 ft over the far end.
    "runway_end_max_agl_m": 12.0,
    "runway_end_max_speed_mps": 50.0,
    # match_caoc_unit_for_flight returns its best guess even on thin evidence, so
    # with nobody flying it will happily hand back some AI flight. Anything this
    # far from the field is not the jet about to depart, whatever it matched.
    # Applied only to EOR / in-position watches — approach and range-exit areas
    # are supposed to score tens of NM out.
    "own_max_distance_m": 20000.0,
}


def rule(config: dict[str, Any] | None, key: str) -> float:
    """Tunable from config, falling back to the default above."""
    try:
        return float((config or {}).get(key))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


# --- geometry model -------------------------------------------------------


def airport_geometry(airport: dict[str, Any] | None) -> dict[str, Any]:
    geo = (airport or {}).get("geometry")
    return geo if isinstance(geo, dict) else {}


def field_elev_m(airport: dict[str, Any] | None) -> float | None:
    geo = airport_geometry(airport)
    for key, scale in (("field_elev_m", 1.0), ("field_elev_ft", 1.0 / FT_PER_M)):
        try:
            return float(geo[key]) * scale
        except (KeyError, TypeError, ValueError):
            continue
    return None


def is_calibrated(airport: dict[str, Any] | None) -> bool:
    return bool(airport_geometry(airport).get("calibrated"))


def runway_geometry(
    airport: dict[str, Any] | None, runway: str | None
) -> dict[str, Any] | None:
    """Geometry for one runway direction, tolerant of 21R / 21r / rwy 21R.

    When only one of a parallel pair is traced (Nellis 21R / 03L), the
    instrument side (21L / 03R) is offset from that centreline.
    """
    runways = airport_geometry(airport).get("runways")
    if not isinstance(runways, dict):
        return None
    want = atc_phrase.normalize_runway(runway) or str(runway or "").strip().upper()
    if not want:
        return None
    found = _runway_geometry_entry(runways, want)
    if found is not None:
        return found
    return _synthetic_parallel_geometry(airport, want, runways)


def _runway_geometry_entry(
    runways: dict[str, Any], want: str
) -> dict[str, Any] | None:
    for key, val in runways.items():
        if not isinstance(val, dict):
            continue
        if (atc_phrase.normalize_runway(key) or str(key).strip().upper()) == want:
            return val
    return None


def _synthetic_parallel_geometry(
    airport: dict[str, Any] | None,
    want: str,
    runways: dict[str, Any],
) -> dict[str, Any] | None:
    flipped = atc_phrase._flip_runway_side(want)
    if not flipped:
        return None
    src = _runway_geometry_entry(runways, flipped)
    if src is None:
        return None
    frame = RunwayFrame.build(flipped, src)
    if frame is None:
        return None
    try:
        offset = float(airport_geometry(airport).get("parallel_offset_m") or 305.0)
    except (TypeError, ValueError):
        offset = 305.0
    if offset <= 0:
        return None
    # Lateral +ve is right of the source heading. 21R→21L is left; 03L→03R is right.
    sign = -1.0 if want.endswith("L") else 1.0
    dx, dz = frame.fx - frame.tx, frame.fz - frame.tz
    ux, uz = dx / frame.length_m, dz / frame.length_m
    rx, rz = uz, -ux
    ox, oz = sign * offset * rx, sign * offset * rz
    tlat, tlon = atc_phrase.caoc_xz_to_ll(frame.tx + ox, frame.tz + oz)
    flat, flon = atc_phrase.caoc_xz_to_ll(frame.fx + ox, frame.fz + oz)
    return {
        "threshold": {"lat": tlat, "lon": tlon},
        "far_end": {"lat": flat, "lon": flon},
        "width_m": frame.width_m,
        "length_m": frame.length_m,
        "synthetic": True,
        "parallel_of": flipped,
    }


def point_xz(obj: Any) -> tuple[float, float] | None:
    """
    A geometry point as (x, z) metres.

    Accepts either native feed metres ({"x", "z"}) or lat/lon, so points captured
    off a map are as usable as points captured from a jet.
    """
    if not isinstance(obj, dict):
        return None
    try:
        return float(obj["x"]), float(obj["z"])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return atc_phrase.caoc_ll_to_xz(float(obj["lat"]), float(obj["lon"]))
    except (KeyError, TypeError, ValueError):
        return None


def unit_xz(unit: dict[str, Any] | None) -> tuple[float, float] | None:
    """Planar metres. Lat/lon on the radar unit wins over xMeters/zMeters."""
    return atc_phrase.caoc_unit_xz(unit)


# --- drawn zones ----------------------------------------------------------
#
# A zone is an area someone traced on a map: a polygon or a circle, tagged with
# what it means. Runway geometry stays separate because heading needs a
# centreline, but everything area-shaped goes through here — which is what lets
# new triggers (hold short, parking, range entry) be drawn rather than coded.


def zones(airport: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = airport_geometry(airport).get("zones")
    return [z for z in raw if isinstance(z, dict)] if isinstance(raw, list) else []


def zone_points_xz(zone: dict[str, Any] | None) -> list[tuple[float, float]]:
    pts = (zone or {}).get("points")
    if not isinstance(pts, list):
        return []
    out = []
    for p in pts:
        xz = point_xz(p)
        if xz is not None:
            out.append(xz)
    return out


def zone_centre_xz(zone: dict[str, Any] | None) -> tuple[float, float] | None:
    centre = point_xz((zone or {}).get("centre") or (zone or {}).get("center"))
    if centre is not None:
        return centre
    pts = zone_points_xz(zone)
    if not pts:
        return None
    return (
        sum(p[0] for p in pts) / len(pts),
        sum(p[1] for p in pts) / len(pts),
    )


def zones_for(
    airport: dict[str, Any] | None,
    trigger: str,
    runway: str | None = None,
) -> list[dict[str, Any]]:
    """
    Zones tagged with this trigger.

    A zone with no runway of its own applies to every runway, so shared areas
    (ramp, hold short) only need drawing once.
    """
    want = str(trigger or "").strip().casefold()
    rwy = atc_phrase.normalize_runway(runway) or str(runway or "").strip().upper()
    out = []
    for zone in zones(airport):
        if str(zone.get("trigger") or "").strip().casefold() != want:
            continue
        own = zone.get("runway")
        if own:
            norm = atc_phrase.normalize_runway(own) or str(own).strip().upper()
            if rwy and norm != rwy:
                continue
        out.append(zone)
    return out


def zone_label(zone: dict[str, Any] | None) -> str:
    """What to call a zone out loud, best available."""
    zone = zone or {}
    for key in ("name", "id", "trigger"):
        val = str(zone.get(key) or "").strip()
        if val:
            return val
    return "zone"


# Taxi "Alpha South" / "NW EOR" ↔ drawn names "AS EOR" / "NW EOR".
_PLACE_EXPAND = {
    "nw": "northwest",
    "ne": "northeast",
    "sw": "southwest",
    "se": "southeast",
    "as": "alpha south",
    "an": "alpha north",
}


def _place_keys(text: str) -> set[str]:
    """Comparable tokens for an EOR place label (compass collapsed, EOR dropped)."""
    raw = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())
    toks = [t for t in raw.split() if t]
    atomic: list[str] = []
    i = 0
    while i < len(toks):
        if i + 1 < len(toks) and toks[i] in ("north", "south") and toks[i + 1] in (
            "west",
            "east",
        ):
            atomic.append(toks[i] + toks[i + 1])
            i += 2
            continue
        atomic.append(_PLACE_EXPAND.get(toks[i], toks[i]))
        i += 1
    atomic = [t for t in atomic if t not in ("eor", "end")]
    keys = set(atomic)
    if atomic:
        keys.add(" ".join(atomic))
    return keys


def zone_matches_place(zone: dict[str, Any] | None, place: str) -> bool:
    """True when a drawn EOR is the taxi-assigned place."""
    place_keys = _place_keys(place)
    if not place_keys or not isinstance(zone, dict):
        return False
    zone_keys: set[str] = set()
    for field in ("name", "id"):
        zone_keys |= _place_keys(str(zone.get(field) or ""))
    return bool(place_keys & zone_keys)


def filter_zones_by_place(
    zone_list: list[dict[str, Any]] | None,
    place: str,
) -> list[dict[str, Any]]:
    """Keep drawn areas that match the assigned EOR place label."""
    return [
        z
        for z in (zone_list or [])
        if isinstance(z, dict) and zone_matches_place(z, place)
    ]


def zones_by_ref(
    airport: dict[str, Any] | None,
    ref: str,
    runway: str | None = None,
    place: str | None = None,
) -> list[dict[str, Any]]:
    """
    Every zone a step is asking for, by id or by trigger tag.

    An id pins to one drawn area. A tag follows the active runway. For `eor`,
    `place` (the taxi-assigned EOR, e.g. "NW EOR") pins that box when it
    matches a drawn name; otherwise every EOR on that runway is used.
    """
    want = str(ref or "").strip()
    if not want:
        return []
    fold = want.casefold()
    for zone in zones(airport):
        if str(zone.get("id") or "").strip().casefold() == fold:
            return [zone]
    matches = list(zones_for(airport, want, runway))
    matches.sort(key=lambda z: 0 if z.get("runway") else 1)
    if place and fold == "eor":
        pinned = filter_zones_by_place(matches, place)
        if pinned:
            return pinned
    return matches


def zone_by_ref(
    airport: dict[str, Any] | None,
    ref: str,
    runway: str | None = None,
) -> dict[str, Any] | None:
    """
    The zone a step is asking for, by id or by trigger tag.

    An id pins to one drawn area; a tag follows the active runway, so one flow
    works off either end without editing. Id is tried first because it is the
    more specific of the two. Prefer `zones_by_ref` when a tag may match more
    than one drawn area.
    """
    matches = zones_by_ref(airport, ref, runway)
    return matches[0] if matches else None


def zones_ref_label(ref: str, zone_list: list[dict[str, Any]] | None) -> str:
    """What to say while waiting: hit name, or 'any eor area' for a tag."""
    zones_l = [z for z in (zone_list or []) if isinstance(z, dict)]
    if len(zones_l) == 1:
        return zone_label(zones_l[0])
    tag = str(ref or "").strip()
    if tag:
        return f"any {tag} area"
    if zones_l:
        return "any matching area"
    return "(no zone)"


def zone_alt_band_m(zone: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """
    The zone's altitude band in metres AGL, None either side for unbounded.

    A floor of zero means "surface", which has to include a jet reading slightly
    below it: field elevation is one number for a field that is not flat, so a
    parked aircraft routinely comes back at -20 ft AGL. Only a floor someone
    deliberately put in the air is enforced.
    """
    out: list[float | None] = []
    for key in ("min_alt_ft", "max_alt_ft"):
        try:
            out.append(float((zone or {})[key]) / FT_PER_M)
        except (KeyError, TypeError, ValueError):
            out.append(None)
    lo, hi = out
    if lo is not None and lo <= 0.0:
        lo = None
    return lo, hi


def _in_polygon(x: float, z: float, pts: list[tuple[float, float]]) -> bool:
    """Ray casting; points may be traced in either direction."""
    if len(pts) < 3:
        return False
    inside = False
    j = len(pts) - 1
    for i, (xi, zi) in enumerate(pts):
        xj, zj = pts[j]
        if (zi > z) != (zj > z):
            # x of the edge at this z
            t = (z - zi) / (zj - zi)
            if x < xi + t * (xj - xi):
                inside = not inside
        j = i
    return inside


def point_in_zone(x: float, z: float, zone: dict[str, Any] | None) -> bool:
    kind = str((zone or {}).get("kind") or "").strip().casefold()
    if kind == "circle":
        centre = zone_centre_xz(zone)
        if centre is None:
            return False
        try:
            radius = float((zone or {}).get("radius_m"))
        except (TypeError, ValueError):
            return False
        return math.hypot(x - centre[0], z - centre[1]) <= radius
    pts = zone_points_xz(zone)
    if kind in ("polygon", "poly", "box", "") and len(pts) >= 3:
        return _in_polygon(x, z, pts)
    return False


def in_any_zone(
    x: float, z: float, zone_list: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for zone in zone_list:
        if point_in_zone(x, z, zone):
            return zone
    return None


def _bearing_deg(dx_east: float, dz_north: float) -> float:
    """Compass bearing of a vector in the feed's x(east)/z(north) frame."""
    return (math.degrees(math.atan2(dx_east, dz_north)) + 360.0) % 360.0


def _runway_number_heading_deg(runway: str) -> float | None:
    """Magnetic-ish heading implied by the runway number (21R → 210)."""
    digits = re.sub(r"[^0-9]", "", str(runway or ""))
    if not digits:
        return None
    return float((int(digits) % 100) * 10 % 360)


def angle_diff(a: float, b: float) -> float:
    """Smallest absolute difference between two bearings, 0–180."""
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


@dataclass(frozen=True)
class RunwayFrame:
    """Runway centreline as an along/lateral coordinate system."""

    runway: str
    tx: float
    tz: float
    fx: float
    fz: float
    length_m: float
    heading_deg: float
    width_m: float

    @classmethod
    def build(
        cls, runway: str, geo: dict[str, Any] | None
    ) -> "RunwayFrame | None":
        if not isinstance(geo, dict):
            return None
        thr = point_xz(geo.get("threshold"))
        far = point_xz(geo.get("far_end"))
        if thr is None or far is None:
            return None
        dx, dz = far[0] - thr[0], far[1] - thr[1]
        length = math.hypot(dx, dz)
        if length < 100.0:  # not a runway; bad or half-captured geometry
            return None
        heading = _bearing_deg(dx, dz)
        # KML / drawn lines are sometimes stored departure-end last. If the
        # centreline points the wrong way vs the runway number, flip it so
        # lineup heading (21R ≈ 210) is not compared against the reciprocal.
        expected = _runway_number_heading_deg(runway)
        if expected is not None and angle_diff(heading, expected) > 90.0:
            thr, far = far, thr
            dx, dz = far[0] - thr[0], far[1] - thr[1]
            heading = _bearing_deg(dx, dz)
        try:
            width = float(geo.get("width_m") or 45.0)
        except (TypeError, ValueError):
            width = 45.0
        return cls(
            runway=str(runway),
            tx=thr[0],
            tz=thr[1],
            fx=far[0],
            fz=far[1],
            length_m=length,
            heading_deg=heading,
            width_m=width,
        )

    def project(self, x: float, z: float) -> tuple[float, float]:
        """(along, lateral) metres from the threshold; lateral +ve = right side."""
        dx, dz = self.fx - self.tx, self.fz - self.tz
        ux, uz = dx / self.length_m, dz / self.length_m
        px, pz = x - self.tx, z - self.tz
        along = px * ux + pz * uz
        lateral = px * uz - pz * ux
        return along, lateral


# --- live sampling --------------------------------------------------------


@dataclass
class UnitFix:
    """One aircraft measured against one runway."""

    unit_id: str
    label: str
    along_m: float
    lateral_m: float
    heading_err_deg: float | None
    alt_m: float | None
    height_m: float | None
    speed_mps: float | None
    # Raw feed position, kept so any zone can be tested against this fix later
    # without going back to the feed.
    x_m: float = 0.0
    z_m: float = 0.0
    own: bool = False
    on_runway: bool = False
    in_position: bool = False
    at_eor: bool = False
    eor_dist_m: float | None = None
    zone: str = ""

    def describe(self) -> str:
        bits = [f"{self.label}: {self.along_m:+.0f}m along, {self.lateral_m:+.0f}m off"]
        if self.zone:
            bits.append(f"in {self.zone}")
        if self.heading_err_deg is not None:
            bits.append(f"hdg {self.heading_err_deg:.0f}° off")
        if self.height_m is not None:
            bits.append(f"{self.height_m * FT_PER_M:.0f}ft agl")
        if self.speed_mps is not None:
            bits.append(f"{self.speed_mps:.0f}m/s")
        return " · ".join(bits)


def _eor_zone(zone: dict[str, Any] | None) -> bool:
    """True for an end-of-runway pad — heading vs takeoff course does not apply."""
    if not isinstance(zone, dict):
        return False
    trig = str(zone.get("trigger") or "").strip().lower()
    if trig == "eor":
        return True
    name = str(zone.get("name") or zone.get("id") or "").strip().lower()
    return "eor" in name


def _settled_heading_err_deg(
    zone: dict[str, Any] | None, fix: UnitFix
) -> float | None:
    """
    Heading error that can fail a settled zone.

    In-position uses runway takeoff heading. At EOR the jet is often parallel
    but reciprocal (or pad-oriented), which is not the lineup heading flip —
    accept runway heading or its reciprocal, and ignore heading when the pad
    is tagged EOR so a parked hammerhead still arms monitor tower.
    """
    err = fix.heading_err_deg
    if err is None:
        return None
    if _eor_zone(zone):
        return None
    return float(err)


def zone_admits(
    zone: dict[str, Any] | None,
    fix: UnitFix,
    *,
    settled: bool = True,
    hdg_tol: float = DEFAULTS["position_heading_tolerance_deg"],
    settled_speed: float = DEFAULTS["position_settled_speed_mps"],
    alt_tol: float = DEFAULTS["position_alt_tolerance_m"],
) -> bool:
    """
    Whether this aircraft counts as being in this zone.

    Containment, then the optional altitude band, then — when the step asks for it
    — stopped, on the deck and pointing down the runway, which is the difference
    between a jet that is lined up and one still taxiing across.

    Permissive where the feed is silent, as elsewhere in this module: a missing
    altitude or heading does not disqualify an aircraft that is plainly inside.
    EOR pads skip heading: the jet is holding, not lined up for takeoff.
    """
    if not point_in_zone(fix.x_m, fix.z_m, zone):
        return False
    lo, hi = zone_alt_band_m(zone)
    if fix.height_m is not None:
        if lo is not None and fix.height_m < lo:
            return False
        if hi is not None and fix.height_m > hi:
            return False
    if not settled:
        return True
    if fix.height_m is not None and abs(fix.height_m) > alt_tol:
        return False
    if fix.speed_mps is not None and fix.speed_mps > settled_speed:
        return False
    hdg_err = _settled_heading_err_deg(zone, fix)
    if hdg_err is not None and hdg_err > hdg_tol:
        return False
    return True


def zone_wait_reason(
    zone: dict[str, Any] | None,
    fix: UnitFix,
    *,
    settled: bool = True,
    hdg_tol: float = DEFAULTS["position_heading_tolerance_deg"],
    settled_speed: float = DEFAULTS["position_settled_speed_mps"],
    alt_tol: float = DEFAULTS["position_alt_tolerance_m"],
) -> str:
    """Why a jet that is over this area still does not count, or ''."""
    if not point_in_zone(fix.x_m, fix.z_m, zone):
        return ""
    bits: list[str] = []
    lo, hi = zone_alt_band_m(zone)
    if fix.height_m is not None:
        agl_ft = fix.height_m * FT_PER_M
        if lo is not None and fix.height_m < lo:
            bits.append(f"{agl_ft:.0f} ft AGL, zone min {lo * FT_PER_M:.0f} ft")
        elif hi is not None and fix.height_m > hi:
            bits.append(f"{agl_ft:.0f} ft AGL, zone max {hi * FT_PER_M:.0f} ft")
        elif settled and abs(fix.height_m) > alt_tol:
            bits.append(f"{agl_ft:.0f} ft AGL, need on deck")
    if settled:
        if fix.speed_mps is not None and fix.speed_mps > settled_speed:
            bits.append(f"{fix.speed_mps * 1.94384:.0f} kt, still moving")
        hdg_err = _settled_heading_err_deg(zone, fix)
        if hdg_err is not None and hdg_err > hdg_tol:
            bits.append(f"heading {hdg_err:.0f}° off")
    return ", ".join(bits)


@dataclass(frozen=True)
class ZoneCount:
    """How much of the flight is in one zone."""

    label: str = ""
    total: int = 0
    inside: int = 0
    qualified: int = 0
    settled: bool = True
    own_seen: bool = False
    own_inside: bool = False
    own_qualified: bool = False
    detail: str = ""

    def ok(self, *, need_full: bool = True) -> bool:
        if not self.total:
            return False
        if need_full:
            return self.qualified >= self.total
        # "Just me" has to mean me, not whichever wingman happens to be parked
        # in the right place.
        return self.own_qualified if self.own_seen else self.qualified >= 1

    def out_ok(self, *, need_full: bool = True) -> bool:
        """The mirror of `ok`, for a step that fires on leaving the zone."""
        if not self.total:
            return False
        if need_full:
            return self.inside == 0
        return not self.own_inside if self.own_seen else self.inside == 0

    def describe(self, *, need_full: bool = True) -> str:
        need = self.total if need_full else 1
        have = self.qualified if need_full else int(self.ok(need_full=False))
        word = "settled in" if self.settled else "in"
        out = f"{have}/{need} {word} {self.label}"
        if self.detail:
            out += f" ({self.detail})"
        elif self.settled and self.inside > self.qualified:
            out += f" ({self.inside} inside, still moving)"
        return out


@dataclass
class FlightStatus:
    """What the whole flight is doing relative to one runway."""

    runway: str = ""
    ok: bool = False
    reason: str = ""
    fixes: list[UnitFix] = field(default_factory=list)
    total: int = 0
    in_position: int = 0
    at_eor: int = 0
    own_label: str = ""
    calibrated: bool = True
    has_position_area: bool = False
    has_eor_area: bool = False
    runway_length_m: float = 0.0
    # Parallel the jet is actually on when that differs from `runway`
    # (21L rollout while the plan still says 21R).
    occupied_runway: str = ""
    # Thresholds evaluate() used, so a later in_zone() call matches the verdicts
    # above without the caller re-reading config.
    tuning: dict[str, float] = field(default_factory=dict)

    def in_zone(self, zone: dict[str, Any] | None, *, settled: bool = True) -> ZoneCount:
        """Count the flight against any drawn zone, not just the two built-in ones."""
        if zone is None:
            return ZoneCount(label="(no zone)", total=self.total, settled=settled)
        return self.in_zones([zone], settled=settled)

    def in_zones(
        self,
        zone_list: list[dict[str, Any]] | None,
        *,
        settled: bool = True,
        label: str = "",
    ) -> ZoneCount:
        """
        Count the flight against one or more zones.

        A member qualifies if they are in *any* of the areas (OR), so a tag that
        resolves to several EOR boxes on the same runway still arms the step.
        """
        zones_l = [z for z in (zone_list or []) if isinstance(z, dict)]
        if not zones_l:
            return ZoneCount(
                label=label or "(no zone)", total=self.total, settled=settled
            )
        tol = {
            "hdg_tol": self.tuning.get(
                "hdg_tol", DEFAULTS["position_heading_tolerance_deg"]
            ),
            "settled_speed": self.tuning.get(
                "settled_speed", DEFAULTS["position_settled_speed_mps"]
            ),
            "alt_tol": self.tuning.get("alt_tol", DEFAULTS["position_alt_tolerance_m"]),
        }
        inside = qualified = 0
        own_seen = own_inside = own_qualified = False
        hit_name = ""
        own_why = ""
        for fix in self.fixes:
            own_seen = own_seen or fix.own
            in_any = False
            ok_any = False
            for zone in zones_l:
                if not zone_admits(zone, fix, settled=False, **tol):
                    continue
                in_any = True
                if not hit_name:
                    hit_name = zone_label(zone)
                if not settled or zone_admits(zone, fix, settled=True, **tol):
                    ok_any = True
                    break
            if fix.own and not ok_any:
                for zone in zones_l:
                    why = zone_wait_reason(zone, fix, settled=settled, **tol)
                    if why:
                        own_why = why
                        if not hit_name:
                            hit_name = zone_label(zone)
                        break
            if not in_any:
                continue
            inside += 1
            qualified += int(ok_any)
            if fix.own:
                own_inside = True
                own_qualified = ok_any
        # One area → its name; several → the one they are in, else the tag label.
        if len(zones_l) == 1:
            display = zone_label(zones_l[0])
        elif hit_name:
            display = hit_name
        else:
            display = label or zones_ref_label("", zones_l)
        return ZoneCount(
            label=display,
            total=self.total,
            inside=inside,
            qualified=qualified,
            settled=settled,
            own_seen=own_seen,
            own_inside=own_inside,
            own_qualified=own_qualified,
            detail=own_why,
        )

    def summary(self, *, need_full: bool = True) -> str:
        if not self.ok:
            return self.reason or "no position data"
        need = self.total if need_full else 1
        return (
            f"in position {self.in_position}/{self.total}"
            f"  ·  at EOR {self.at_eor}/{self.total}"
            f"  ·  need {need}"
        )

    def all_in_position(self, *, need_full: bool = True) -> bool:
        if not self.ok or not self.total:
            return False
        return self.in_position >= (self.total if need_full else 1)

    def all_at_eor(self, *, need_full: bool = True) -> bool:
        if not self.ok or not self.total:
            return False
        return self.at_eor >= (self.total if need_full else 1)


# --- what arms a step -----------------------------------------------------
#
# Templates that fired off position before steps could name a zone themselves.
# Kept so flows written against the old behaviour keep working untouched.
LEGACY_TEMPLATE_TRIGGERS: dict[str, tuple[str, str]] = {
    # LUAW when the flight is in the runway / takeoff box (same area as clearance).
    "lineup": ("in_position", "auto_takeoff_clearance"),
    "clear_takeoff": ("in_position", "auto_takeoff_clearance"),
    "clear_takeoff_rolling": ("in_position", "auto_takeoff_clearance"),
    "clear_takeoff_intersection": ("in_position", "auto_takeoff_clearance"),
    "monitor_tower": ("eor", "auto_monitor_tower"),
}


@dataclass(frozen=True)
class StepTrigger:
    """A step's `trigger` block: zone and/or distance from the field."""

    zone: str = ""
    within_nm: float | None = None  # ownship ≤ this many NM from the field
    when: str = "inside"  # inside | leaving
    flight: str = ""  # all | me | "" to follow auto_clearance_require_full_flight
    settled: bool = True
    dwell_s: float | None = None  # None → auto_clearance_dwell_s
    gap_s: float = 0.0
    enabled_key: str = ""  # config toggle, legacy pairs only
    explicit: bool = True

    def need_full(self, config: dict[str, Any] | None) -> bool:
        if self.flight == "me":
            return False
        if self.flight == "all":
            return True
        return bool((config or {}).get("auto_clearance_require_full_flight", True))

    def dwell(self, config: dict[str, Any] | None) -> float:
        if self.dwell_s is None:
            return rule(config, "auto_clearance_dwell_s")
        return max(0.0, float(self.dwell_s))

    def enabled(self, config: dict[str, Any] | None) -> bool:
        if not self.enabled_key:
            return True
        return bool((config or {}).get(self.enabled_key, True))

    def describe(self) -> str:
        bits: list[str] = []
        if self.within_nm is not None:
            if self.when == "leaving":
                bits.append(f"beyond {self.within_nm:g} NM")
            else:
                bits.append(f"within {self.within_nm:g} NM")
        if self.zone:
            bits.append(f"{'leaves' if self.when == 'leaving' else 'in'} {self.zone}")
        if self.flight:
            bits.append("whole flight" if self.flight == "all" else "just me")
        if self.settled:
            bits.append("settled")
        if self.dwell_s:
            bits.append(f"{self.dwell_s:g}s dwell")
        if self.gap_s:
            bits.append(f"{self.gap_s:g}s gap")
        return ", ".join(bits) if bits else "armed"


def gap_remaining(
    trigger: StepTrigger | None,
    state: dict[str, Any] | None,
    now: float | None = None,
) -> float:
    """
    Seconds still to wait so this call does not tread on the previous transmission.

    Measured from when the previous call *finishes* speaking (last_tx_end_at),
    not from when it was queued. Falls back to an estimate from last_tx_text.
    """
    if trigger is None or trigger.gap_s <= 0:
        return 0.0
    return atc_phrase.radio_gap_remaining(state, float(trigger.gap_s), now=now)


def skip_auto_tx_already_played(
    *,
    fire_id: str,
    current_step_id: str,
    last_step_id: str,
    template: str = "",
    hold_for_landing: bool = False,
    playing_id: str = "",
) -> bool:
    """
    True when Watch must not transmit.

    Manual Play (or voice) already sent this step, or the cursor has moved on.
    play_id() would otherwise seek back and TX it a second time. Multi-ship
    landings are the exception: the same clear_land step fires once per seat
    after Play has finished talking.
    """
    want = str(fire_id or "").strip()
    if not want:
        return False
    current = str(current_step_id or "").strip()
    last = str(last_step_id or "").strip()
    playing = str(playing_id or "").strip()
    if playing == want:
        return True
    if current and current != want:
        return True
    if last == want and not (
        str(template or "").strip().lower() == "clear_land" and hold_for_landing
    ):
        return True
    return False


def _trigger_float(val: Any) -> float | None:
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if num >= 0.0 else None


def step_trigger(step: dict[str, Any] | None) -> StepTrigger | None:
    """
    The trigger a step is armed by, or None if it only fires when told to.

    An explicit `trigger` block wins; failing that a couple of templates carry the
    behaviour they had before triggers were a thing.
    """
    raw = (step or {}).get("trigger")
    if isinstance(raw, dict):
        zone = str(raw.get("zone") or "").strip()
        within = _trigger_float(raw.get("within_nm"))
        if zone or within is not None:
            flight = str(raw.get("flight") or "").strip().casefold()
            when = str(raw.get("when") or "").strip().casefold()
            return StepTrigger(
                zone=zone,
                within_nm=within,
                when="leaving" if when.startswith("leav") else "inside",
                flight=flight if flight in ("all", "me") else "",
                settled=bool(raw.get("settled", True)),
                dwell_s=_trigger_float(raw.get("dwell_s")),
                gap_s=_trigger_float(raw.get("gap_s")) or 0.0,
            )
    pair = LEGACY_TEMPLATE_TRIGGERS.get(str((step or {}).get("template") or ""))
    if not pair:
        return None
    zone, key = pair
    return StepTrigger(zone=zone, enabled_key=key, explicit=False)


def airport_field_latlon(airport: dict[str, Any] | None) -> tuple[float, float] | None:
    """
    Field reference point for distance gates (midpoint of the primary runway).
    """
    if not airport:
        return None
    for key in ("lat", "field_lat"):
        try:
            return float(airport[key]), float(airport.get("lon") or airport["field_lon"])
        except (KeyError, TypeError, ValueError):
            pass
    geo = airport_geometry(airport)
    runways = geo.get("runways") if isinstance(geo.get("runways"), dict) else {}
    # Prefer first listed airport runway, else any geometry runway.
    order = [str(r) for r in (airport.get("runways") or [])] + list(runways.keys())
    seen: set[str] = set()
    for rwy in order:
        if rwy in seen:
            continue
        seen.add(rwy)
        entry = runways.get(rwy) if isinstance(runways, dict) else None
        if not isinstance(entry, dict):
            continue
        thr = entry.get("threshold") or {}
        far = entry.get("far_end") or {}
        try:
            lat = (float(thr["lat"]) + float(far["lat"])) / 2.0
            lon = (float(thr["lon"]) + float(far["lon"])) / 2.0
            return lat, lon
        except (KeyError, TypeError, ValueError):
            continue
    return None


def ownship_distance_nm(
    airport: dict[str, Any] | None,
    *,
    config: dict[str, Any] | None = None,
    callsign: str | None = None,
    opus: Any = None,
    state: dict[str, Any] | None = None,
    own_ll: tuple[float, float] | None = None,
) -> float | None:
    """NM from ownship to the field reference, or None when unknown."""
    field = airport_field_latlon(airport)
    if field is None:
        return None
    pos = own_ll
    if pos is None and config is not None:
        pos = atc_phrase.ownship_latlon(
            config, callsign=callsign, opus=opus, state=state
        )
        # A bound seat that reported no fix has no position at all — the cached
        # one belongs to whoever the host last saw.
        if pos is None and atc_phrase.ownship_seat_bound(config):
            return None
    if pos is None:
        pos = atc_phrase._ownship_ll_from_state(state)
    if pos is None:
        return None
    return atc_phrase._haversine_nm(pos[0], pos[1], field[0], field[1])


def within_nm_held(
    trigger: StepTrigger | None,
    distance_nm: float | None,
) -> tuple[bool, str]:
    """
    Whether a within_nm gate is satisfied.

    Returns (held, waiting_summary).
    """
    if trigger is None or trigger.within_nm is None:
        return False, ""
    limit = float(trigger.within_nm)
    if distance_nm is None:
        need = (
            f"beyond {limit:g} NM"
            if trigger.when == "leaving"
            else f"≤ {limit:g} NM"
        )
        return False, f"waiting for position (need {need})"
    if trigger.when == "leaving":
        # State check, not a crossing — already outside (airborne spawn) counts.
        held = distance_nm > limit
        waiting = (
            f"{distance_nm:.1f} NM, already beyond {limit:g} NM"
            if held
            else f"{distance_nm:.1f} NM, still inside {limit:g} NM"
        )
    else:
        held = distance_nm <= limit
        waiting = (
            f"{distance_nm:.1f} NM (≤ {limit:g} NM)"
            if held
            else f"{distance_nm:.1f} NM, need ≤ {limit:g} NM"
        )
    return held, waiting


RUNWAY_END_ZONES = frozenset({"runway_end", "departure_end", "rollout_end"})


def occupied_runway_of_pair(
    airport: dict[str, Any] | None,
    assigned: str | None,
    x: float,
    z: float,
    *,
    max_lateral_m: float = 80.0,
) -> str:
    """
    Which parallel the jet is sitting on (assigned or its L/R twin).

    Empty when the point is not clearly on either strip. A 21L rollout
    while the plan still says 21R must still count as on 21L.
    """
    want = atc_phrase.normalize_runway(assigned) or str(assigned or "").strip().upper()
    if not want:
        return ""
    flipped = atc_phrase._flip_runway_side(want)
    best = ""
    best_lat: float | None = None
    for rwy in (want, flipped):
        if not rwy:
            continue
        geo = runway_geometry(airport, rwy)
        frame = RunwayFrame.build(rwy, geo)
        if frame is None:
            continue
        along, lateral = frame.project(x, z)
        half = max(frame.width_m / 2.0 + 20.0, max_lateral_m)
        on = abs(lateral) <= half and -60.0 <= along <= frame.length_m + 80.0
        if not on:
            continue
        lat_abs = abs(lateral)
        if best_lat is None or lat_abs < best_lat:
            best = rwy
            best_lat = lat_abs
    return best


def _runway_end_on_strip(
    along_m: float,
    lateral_m: float,
    length_m: float,
    *,
    height_m: float | None,
    speed_mps: float | None,
    on_runway: bool,
    config: dict[str, Any] | None,
) -> tuple[bool, str]:
    remaining = rule(config, "runway_end_remaining_m")
    max_agl = rule(config, "runway_end_max_agl_m")
    max_spd = rule(config, "runway_end_max_speed_mps")
    left = length_m - along_m
    airborne = height_m is not None and abs(height_m) > max_agl
    fast = speed_mps is not None and speed_mps > max_spd
    near = (length_m - remaining) <= along_m <= (length_m + 80.0)
    on_strip = on_runway or abs(lateral_m) <= 50.0
    if airborne:
        return False, "still airborne — rollout not started"
    if fast:
        return False, "too fast for rollout (go-around / low approach)"
    if near and on_strip:
        return True, f"departure end ({max(0.0, left):.0f} m remaining)"
    if along_m < 0:
        return False, "short of the threshold"
    if left > remaining:
        return False, f"{left:.0f} m remaining to the departure end"
    return False, "not on the landing runway"


def runway_end_held(
    trigger: StepTrigger,
    status: FlightStatus,
    *,
    config: dict[str, Any] | None = None,
    airport: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """
    Ownship rolling out near the departure end of the landing runway.

    Tower's exit / contact-Ground call. Uses the landing centreline, not a
    drawn box — 21L is offset from 21R when only one strip is traced.
    If the plan runway is the parallel (overhead on 21R, rollout on 21L),
    the occupied strip still counts.
    """
    del trigger
    if not status.ok:
        return False, status.reason or "no position data"
    length = float(status.runway_length_m or 0.0)
    if length < 100.0:
        return False, f"no centreline for {status.runway or 'the runway'}"
    own = next((fix for fix in status.fixes if fix.own), None)
    if own is None:
        return False, "own aircraft not in the flight sample"
    held, waiting = _runway_end_on_strip(
        own.along_m,
        own.lateral_m,
        length,
        height_m=own.height_m,
        speed_mps=own.speed_mps,
        on_runway=own.on_runway,
        config=config,
    )
    if held:
        status.occupied_runway = status.runway
        return held, waiting
    if airport is None:
        return held, waiting
    occupied = occupied_runway_of_pair(
        airport, status.runway, own.x_m, own.z_m
    )
    if not occupied or occupied == status.runway:
        return held, waiting
    geo = runway_geometry(airport, occupied)
    frame = RunwayFrame.build(occupied, geo)
    if frame is None:
        return held, waiting
    along, lateral = frame.project(own.x_m, own.z_m)
    held_p, waiting_p = _runway_end_on_strip(
        along,
        lateral,
        frame.length_m,
        height_m=own.height_m,
        speed_mps=own.speed_mps,
        on_runway=abs(lateral) <= 50.0,
        config=config,
    )
    if held_p:
        status.occupied_runway = occupied
        status.runway = occupied
        status.runway_length_m = frame.length_m
        own.along_m = along
        own.lateral_m = lateral
        own.on_runway = True
        return True, f"{occupied} {waiting_p}"
    return held, waiting


def field_proximity_applies(watch: list[dict[str, Any]] | None) -> bool:
    """
    Whether evaluate() should reject a track far from the field.

    EOR / in-position boxes sit on the airfield; a match 15 NM out is the
    wrong jet. Approach, tower, and range areas are supposed to score that
    far out, so the cutoff stays off while those are in `watch`.
    """
    extra = [z for z in (watch or []) if isinstance(z, dict)]
    if not extra:
        return True
    local = {"eor", "in_position"}
    for zone in extra:
        trig = str(zone.get("trigger") or "").strip().casefold()
        if trig not in local:
            return False
    return True


def condition_held(
    trigger: StepTrigger | None,
    status: FlightStatus,
    *,
    zones: list[dict[str, Any]] | None = None,
    distance_nm: float | None = None,
    config: dict[str, Any] | None = None,
    tracker: PositionTracker | None = None,
    leave_key: str = "",
    airport: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """
    Whether a step's distance and/or zone condition is true.

    Distance and zone are OR when both are set (range exit: approach area or
    ≤ N NM). A distance gate can hold even when evaluate() has no field
    verdict — the aircraft is not supposed to be "at the field" yet.
    Returns (held, waiting_summary).
    """
    if trigger is None or not trigger.enabled(config):
        return False, ""

    need_full = trigger.need_full(config)
    zone_list = [z for z in (zones or []) if isinstance(z, dict)]
    zone_name = str(trigger.zone or "").strip().casefold()
    if zone_name in RUNWAY_END_ZONES:
        return runway_end_held(trigger, status, config=config, airport=airport)

    held_dist = False
    waiting_dist = ""
    if trigger.within_nm is not None:
        held_dist, waiting_dist = within_nm_held(trigger, distance_nm)

    held_zone = False
    waiting_zone = ""
    if trigger.zone:
        if trigger.explicit:
            if not zone_list:
                waiting_zone = f"no zone called {trigger.zone!r} is drawn for this field"
            elif not status.ok:
                waiting_zone = status.reason or "no position data"
            else:
                tag_label = zones_ref_label(trigger.zone, zone_list)
                count = status.in_zones(
                    zone_list, settled=trigger.settled, label=tag_label
                )
                if trigger.when == "leaving":
                    inside_now = not count.out_ok(need_full=need_full)
                    if tracker is not None:
                        held_zone = tracker.has_left(leave_key or tag_label, inside_now)
                    zone_waiting = (
                        f"clear of {count.label}"
                        if held_zone
                        else f"in {count.label}, waiting to leave"
                        if inside_now
                        else f"has not reached {tag_label} yet"
                    )
                    waiting_zone = zone_waiting
                else:
                    held_zone = count.ok(need_full=need_full)
                    waiting_zone = count.describe(need_full=need_full)
                    if not held_zone and count.inside == 0 and len(zone_list) > 1:
                        waiting_zone = (
                            f"0/{count.total if need_full else 1} in {tag_label}"
                        )
        elif not status.ok:
            waiting_zone = status.reason or "no position data"
        elif not status.calibrated:
            waiting_zone = "geometry not calibrated, nothing will fire"
        else:
            held_zone = (
                status.all_in_position(need_full=need_full)
                if trigger.zone == "in_position"
                else status.all_at_eor(need_full=need_full)
            )
            waiting_zone = status.summary(need_full=need_full)

    has_dist = trigger.within_nm is not None
    has_zone = bool(trigger.zone)
    if has_dist and has_zone:
        held = held_dist or held_zone
        if held:
            waiting = waiting_dist if held_dist else waiting_zone
        else:
            waiting = "  ·  ".join(b for b in (waiting_dist, waiting_zone) if b)
        return held, waiting
    if has_dist:
        return held_dist, waiting_dist
    if has_zone:
        return held_zone, waiting_zone
    return False, ""


# Landing clearance distance by recovery (NM from field).
CLEAR_LAND_WITHIN_NM = {
    "visual_overhead": 2.0,
    "tactical_overhead": 2.0,
    "straight_in": 6.0,
    "instrument": 6.0,
}
CONTACT_TOWER_WITHIN_NM = 12.0
# After a go-around, land only on the approach side of the threshold.
_FINAL_PAST_THRESHOLD_M = 400.0
_FINAL_HDG_TOL_DEG = 100.0
_M_PER_NM = 1852.0


def own_unit_fix(status: FlightStatus | None) -> UnitFix | None:
    if status is None:
        return None
    for fix in status.fixes:
        if fix.own:
            return fix
    return status.fixes[0] if status.fixes else None


def on_base_or_short_final(
    status: FlightStatus | None,
    *,
    within_nm: float,
) -> tuple[bool, str]:
    """
    True on base / short final for the landing runway.

    Approach side of the threshold (not upwind / departure after a go-around),
    within `within_nm` of the threshold, heading not downwind.
    """
    fix = own_unit_fix(status)
    if fix is None:
        return False, "waiting for position (base / short final)"
    along = float(fix.along_m)
    if along > _FINAL_PAST_THRESHOLD_M:
        return (
            False,
            f"upwind / departure ({along / _M_PER_NM:+.1f} NM along) — "
            "need base / short final",
        )
    need_m = float(within_nm) * _M_PER_NM
    if along < -need_m:
        return (
            False,
            f"{-along / _M_PER_NM:.1f} NM from threshold, "
            f"need ≤ {within_nm:g} NM on final",
        )
    if fix.heading_err_deg is not None and float(fix.heading_err_deg) > _FINAL_HDG_TOL_DEG:
        return (
            False,
            f"hdg {fix.heading_err_deg:.0f}° off runway — need base / final",
        )
    dist_nm = abs(along) / _M_PER_NM
    return True, f"base / short final ({dist_nm:.1f} NM to threshold)"


# Overhead land: reject initial (centerline + runway heading at pattern alt).
_OVERHEAD_INITIAL_LATERAL_M = 450.0
_OVERHEAD_INITIAL_HDG_DEG = 40.0
_OVERHEAD_INITIAL_HEIGHT_FT = 1000.0
OVERHEAD_LAND_WITHIN_NM = 2.5


def on_overhead_base_or_final(
    status: FlightStatus | None,
    *,
    within_nm: float = OVERHEAD_LAND_WITHIN_NM,
) -> tuple[bool, str]:
    """
    True when turning base / on final after an overhead break — not on initial.

    Field-distance alone would clear to land while still inbound to the
    numbers. Require an off-centerline / off-heading break, or a lower final.
    """
    fix = own_unit_fix(status)
    if fix is None:
        return False, "waiting for position (base after break)"
    lateral = abs(float(fix.lateral_m))
    hdg = fix.heading_err_deg
    aligned = hdg is None or abs(float(hdg)) < _OVERHEAD_INITIAL_HDG_DEG
    on_line = lateral < _OVERHEAD_INITIAL_LATERAL_M
    height_ft = None
    if fix.height_m is not None:
        try:
            height_ft = float(fix.height_m) * FT_PER_M
        except (TypeError, ValueError):
            height_ft = None
    still_high = height_ft is None or height_ft > _OVERHEAD_INITIAL_HEIGHT_FT
    if on_line and aligned and still_high:
        return False, "on initial — clear to land when turning base"
    return on_base_or_short_final(status, within_nm=within_nm)
DEPARTURE_HANDOFF_BEYOND_NM = 18.0
CRUISE_CLIMB_BEYOND_NM = 10.0
# Blackjack → Approach only when near the field / APP boundary (not deep NTTR).
RANGE_EXIT_WITHIN_NM = 40.0


def resolve_step_trigger(
    step: dict[str, Any] | None,
    *,
    mission: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> StepTrigger | None:
    """
    Step trigger with recovery-aware within_nm for clear_land / contact tower /
    departure handoff (beyond NM).
    """
    base = step_trigger(step)
    tmpl = str((step or {}).get("template") or "").strip()
    raw = (step or {}).get("trigger") if isinstance((step or {}).get("trigger"), dict) else {}
    raw = raw if isinstance(raw, dict) else {}

    def _from_raw_or_base(
        *,
        within_nm: float,
        when: str | None = None,
    ) -> StepTrigger:
        gap = _trigger_float(raw.get("gap_s"))
        dwell = _trigger_float(raw.get("dwell_s"))
        flight = str(raw.get("flight") or "").strip().casefold()
        when_raw = str(raw.get("when") or "").strip().casefold()
        if when is None:
            when_use = "leaving" if when_raw.startswith("leav") else "inside"
        else:
            when_use = when
        if base is not None:
            return StepTrigger(
                zone=base.zone or "",
                within_nm=within_nm,
                when=when_use if when is not None else base.when,
                flight=base.flight,
                settled=base.settled,
                dwell_s=base.dwell_s if dwell is None else dwell,
                gap_s=float(base.gap_s if gap is None else gap),
                enabled_key=base.enabled_key,
                explicit=True,
            )
        return StepTrigger(
            zone=str(raw.get("zone") or "").strip(),
            within_nm=within_nm,
            when=when_use,
            flight=flight if flight in ("all", "me") else "",
            settled=bool(raw.get("settled", False)),
            dwell_s=dwell,
            gap_s=gap or 5.0,
            explicit=True,
        )

    # Explicit within_nm on the step always wins for contact/clear templates too.
    if base and base.within_nm is not None and "within_nm" in raw:
        if tmpl not in ("clear_land", "departure_handoff", "climb_cruise"):
            return base
        if tmpl in ("departure_handoff", "climb_cruise"):
            # Keep explicit NM; default when to leaving if omitted.
            when_raw = str(raw.get("when") or "").strip().casefold()
            if when_raw:
                return base
            return StepTrigger(
                zone=base.zone,
                within_nm=base.within_nm,
                when="leaving",
                flight=base.flight,
                settled=base.settled,
                dwell_s=base.dwell_s,
                gap_s=base.gap_s,
                enabled_key=base.enabled_key,
                explicit=True,
            )
        # clear_land: keep explicit override
        return base

    if tmpl in ("cleared_approach", "contact_tower"):
        nm = CONTACT_TOWER_WITHIN_NM
        if base and base.within_nm is not None:
            nm = float(base.within_nm)
        elif raw.get("within_nm") is not None:
            parsed = _trigger_float(raw.get("within_nm"))
            if parsed is not None:
                nm = parsed
        return _from_raw_or_base(within_nm=nm)

    if tmpl == "right_break":
        # Closed traffic go-around: still at the field — do not auto
        # "continue" on a leftover NM gate.
        if atc_phrase.closed_traffic_go_around_pending(state):
            return None
        # All recoveries: wait for "with you" / "initial" (or Play).
        # Contact-tower handoff stays on the 12 NM gate; landing clearance
        # remains 6 NM on clear_land after they check in.
        return None

    if tmpl == "climb_cruise":
        nm = CRUISE_CLIMB_BEYOND_NM
        if base and base.within_nm is not None:
            nm = float(base.within_nm)
        elif raw.get("within_nm") is not None:
            parsed = _trigger_float(raw.get("within_nm"))
            if parsed is not None:
                nm = parsed
        when_raw = str(raw.get("when") or "").strip().casefold()
        when_use = "leaving" if (not when_raw or when_raw.startswith("leav")) else "inside"
        return _from_raw_or_base(within_nm=nm, when=when_use)

    if tmpl == "departure_handoff":
        nm = DEPARTURE_HANDOFF_BEYOND_NM
        if base and base.within_nm is not None:
            nm = float(base.within_nm)
        elif raw.get("within_nm") is not None:
            parsed = _trigger_float(raw.get("within_nm"))
            if parsed is not None:
                nm = parsed
        when_raw = str(raw.get("when") or "").strip().casefold()
        when_use = "leaving" if (not when_raw or when_raw.startswith("leav")) else "inside"
        return _from_raw_or_base(within_nm=nm, when=when_use)

    if tmpl == "clear_land":
        rec = atc_phrase.resolve_active_recovery(step, mission, state=state)
        # After a VFR go-around they are already with Tower. Auto-land on
        # base / short final (2 NM closed, 6 NM Flex/Duck) — not a check-in.
        pattern_nm = atc_phrase.pattern_land_within_nm(state)
        if pattern_nm is not None:
            return _from_raw_or_base(within_nm=float(pattern_nm))
        # Overhead / TAC: auto when turning base / short final — never on a
        # bare field-distance gate (that clears while still on initial).
        if rec in (
            "visual_overhead",
            "tactical_overhead",
        ):
            if "within_nm" in raw:
                parsed = _trigger_float(raw.get("within_nm"))
                if parsed is not None:
                    return _from_raw_or_base(within_nm=parsed)
            return _from_raw_or_base(within_nm=OVERHEAD_LAND_WITHIN_NM)
        # SFO High Key / straight-in SFO: voice (Low Key / gear) unless explicit.
        if rec in (
            "sfo_overhead",
            "sfo_straight_in",
        ):
            if "within_nm" in raw:
                parsed = _trigger_float(raw.get("within_nm"))
                if parsed is not None:
                    return _from_raw_or_base(within_nm=parsed)
            if base and base.zone:
                return base
            return None
        nm = float(CLEAR_LAND_WITHIN_NM.get(rec, 6.0))
        if "within_nm" in raw:
            parsed = _trigger_float(raw.get("within_nm"))
            if parsed is not None:
                nm = parsed
        return _from_raw_or_base(within_nm=nm)

    if tmpl == "bj_range_exit":
        # Prefer a drawn approach / range_exit zone; else ≤40 NM from the field.
        if base and base.zone:
            return base
        zone = str(raw.get("zone") or "").strip() or "approach"
        nm = RANGE_EXIT_WITHIN_NM
        if base and base.within_nm is not None:
            nm = float(base.within_nm)
        elif raw.get("within_nm") is not None:
            parsed = _trigger_float(raw.get("within_nm"))
            if parsed is not None:
                nm = parsed
        gap = _trigger_float(raw.get("gap_s"))
        dwell = _trigger_float(raw.get("dwell_s"))
        flight = str(raw.get("flight") or "").strip().casefold()
        return StepTrigger(
            zone=zone,
            within_nm=nm,
            when="inside",
            flight=flight if flight in ("all", "me") else "",
            settled=bool(raw.get("settled", False)),
            dwell_s=dwell,
            gap_s=gap or 5.0,
            explicit=True,
        )

    if tmpl == "exit_runway":
        gap = _trigger_float(raw.get("gap_s"))
        dwell = _trigger_float(raw.get("dwell_s"))
        flight = str(raw.get("flight") or (base.flight if base else "") or "").strip().casefold()
        zone = ""
        if base and base.zone:
            zone = str(base.zone)
        else:
            zone = str(raw.get("zone") or "").strip()
        if zone and zone.casefold() not in RUNWAY_END_ZONES:
            return base
        gap_s = 5.0
        if gap is not None:
            gap_s = float(gap)
        elif base is not None:
            gap_s = float(base.gap_s or 5.0)
        return StepTrigger(
            zone="runway_end",
            when="inside",
            flight=flight if flight in ("all", "me") else "me",
            settled=False,
            dwell_s=0.0 if dwell is None else dwell,
            gap_s=gap_s,
            enabled_key=base.enabled_key if base else "",
            explicit=True,
        )

    return base


def flight_key(unit: dict[str, Any] | None) -> str:
    """Stable identity for the flight a track belongs to."""
    unit = unit or {}
    fid = unit.get("opusFlightId")
    if fid not in (None, ""):
        return f"fid:{fid}"
    for key in ("flightLabel", "unitCallsign", "groupName"):
        raw = str(unit.get(key) or "").strip()
        if "#" in raw:
            raw = raw.split("#", 1)[0].strip()
        if raw:
            return f"{key}:{raw.casefold()}"
    return ""


def flight_members(
    units: list[dict[str, Any]], own: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """
    Every track flying with `own`.

    Human flight members carry opusFlightId / flightLabel, so they group cleanly.
    AI wingmen often carry neither, in which case the flight collapses to the one
    track we can identify — hence `auto_clearance_require_full_flight` being a
    setting rather than an assumption.
    """
    if not own:
        return []
    key = flight_key(own)
    if not key:
        return [own]
    out = [u for u in units if flight_key(u) == key]
    if not any(u.get("id") == own.get("id") for u in out):
        out.append(own)
    return out


class PositionTracker:
    """
    Samples the CAOC feed and turns it into runway verdicts.

    Speed comes from position deltas because the feed's own groundSpeedMps reads
    zero even for aircraft that are plainly moving. Dwell timers stop a clearance
    from firing on a single lucky sample as the flight rolls across the numbers.
    """

    def __init__(self) -> None:
        self._last: dict[str, tuple[float, float, float]] = {}  # id -> (t, x, z)
        self._since: dict[str, float] = {}  # condition key -> first-true time
        self._fired: set[str] = set()
        self._entered: set[str] = set()
        self.pending_latch: str = ""
        self.playing_step_id: str = ""
        self._manual_consumed: dict[str, str] = {}

    # -- motion ----------------------------------------------------------
    def _speed_mps(self, unit: dict[str, Any], now: float) -> float | None:
        uid = str(unit.get("id") or "")
        pos = unit_xz(unit)
        if not uid or pos is None:
            return None
        # Map tester publishes the slider speed. CAOC groundSpeedMps is not
        # trusted — it reads 0 even when the jet is moving.
        if uid == atc_phrase.MAP_OWNSHIP_ID:
            for key in ("groundSpeedMps", "speedMps"):
                raw = unit.get(key)
                if raw is None:
                    continue
                try:
                    return max(0.0, float(raw))
                except (TypeError, ValueError):
                    continue
        prev = self._last.get(uid)
        self._last[uid] = (now, pos[0], pos[1])
        if not prev:
            return None
        dt = now - prev[0]
        if dt < 0.5:
            return None
        return math.hypot(pos[0] - prev[1], pos[1] - prev[2]) / dt

    # -- dwell / latch ---------------------------------------------------
    def held_for(self, key: str, ok: bool, seconds: float, now: float | None = None) -> bool:
        """True once `ok` has been continuously true for `seconds`."""
        now = time.time() if now is None else now
        if not ok:
            self._since.pop(key, None)
            return False
        first = self._since.setdefault(key, now)
        return (now - first) >= max(0.0, seconds)

    def held_since(self, key: str, now: float | None = None) -> float:
        """Seconds this condition has been true, 0 if it is not."""
        first = self._since.get(key)
        if first is None:
            return 0.0
        return max(0.0, (time.time() if now is None else now) - first)

    def has_left(self, key: str, inside: bool) -> bool:
        """
        True once this condition has been inside a zone and is now out of it.

        Without the memory, "leaving the runway" would be true before the flight
        ever got on it.
        """
        if inside:
            self._entered.add(key)
            return False
        return key in self._entered

    def fire_once(self, key: str) -> bool:
        """True the first time only — keeps a clearance from repeating."""
        if key in self._fired:
            return False
        self._fired.add(key)
        return True

    def mark_step_played(self, step_id: str) -> None:
        """Manual Play started — Watch must not TX this step until it finishes."""
        sid = str(step_id or "").strip()
        if not sid:
            return
        self.playing_step_id = sid
        pending = str(self.pending_latch or "")
        if pending and sid in pending.split(":"):
            self._fired.add(pending)
            self._manual_consumed[sid] = pending
            self.pending_latch = ""

    def unmark_step_played(self, step_id: str) -> None:
        """Play failed — let Watch fire this step again."""
        sid = str(step_id or "").strip()
        if self.playing_step_id == sid:
            self.playing_step_id = ""
        consumed = self._manual_consumed.pop(sid, "")
        if consumed:
            self._fired.discard(consumed)
            if not self.pending_latch:
                self.pending_latch = consumed

    def finish_manual_tx(self, step_id: str = "") -> None:
        """Play finished talking — Watch may arm the next seat / step."""
        sid = str(step_id or self.playing_step_id or "").strip()
        if self.playing_step_id == sid or not sid:
            self.playing_step_id = ""
        self._manual_consumed.pop(sid, None)

    def armed(self, key: str) -> bool:
        """Whether `fire_once` would still fire for this key."""
        token = str(key or "")
        if token in self._fired:
            return False
        parts = token.split(":")
        if len(parts) >= 2 and parts[0] == "fire":
            if self.playing_step_id and parts[1] == self.playing_step_id:
                return False
        return True

    def clear_fired(self, predicate: Any = None) -> int:
        """
        Drop fire-once latches so a second pattern / missed approach can re-arm.

        predicate: None clears all; callable(key)->bool keeps matching keys removed;
        str clears keys containing that substring.
        """
        if predicate is None:
            n = len(self._fired)
            self._fired.clear()
            return n
        if isinstance(predicate, str):
            needle = predicate
            doomed = [k for k in self._fired if needle in k]
        else:
            doomed = [k for k in self._fired if predicate(k)]
        for k in doomed:
            self._fired.discard(k)
        return len(doomed)

    def reset(self) -> None:
        self._since.clear()
        self._fired.clear()
        self._entered.clear()
        self.pending_latch = ""
        self.playing_step_id = ""
        self._manual_consumed.clear()

    # -- main evaluation -------------------------------------------------
    def evaluate(
        self,
        config: dict[str, Any],
        airport: dict[str, Any] | None,
        runway: str | None,
        *,
        callsign: str | None = None,
        opus: Any = None,
        max_age_s: float = 2.0,
        watch: list[dict[str, Any]] | None = None,
    ) -> FlightStatus:
        """
        `watch` names extra zones a step is interested in. Passing them keeps the
        evaluation alive at a field that has one custom area and no runway
        geometry at all, which is a legitimate way to use a zone trigger.
        """
        rwy = atc_phrase.normalize_runway(runway) or str(runway or "").strip().upper()
        status = FlightStatus(runway=rwy, calibrated=is_calibrated(airport))

        geo = runway_geometry(airport, rwy)
        # The centreline is only needed for heading and for the fallback box —
        # a drawn "in position" zone stands on its own.
        frame = RunwayFrame.build(rwy, geo)
        if frame is not None:
            status.runway_length_m = frame.length_m
        pos_zones = zones_for(airport, "in_position", rwy)
        eor_zones = zones_for(airport, "eor", rwy)
        extra = [z for z in (watch or []) if isinstance(z, dict)]
        anchor = None
        if frame is not None:
            anchor = (frame.tx, frame.tz)
        else:
            for zone in (*pos_zones, *eor_zones, *extra):
                anchor = zone_centre_xz(zone)
                if anchor is not None:
                    break
        if frame is None and not pos_zones and not eor_zones and not extra:
            status.reason = f"no runway geometry or zones for {rwy or '?'}"
            return status
        status.has_position_area = bool(pos_zones) or frame is not None
        status.has_eor_area = bool(eor_zones) or point_xz((geo or {}).get("eor")) is not None

        radar = atc_phrase.fetch_caoc_radar(config, max_age_s=max_age_s)
        map_own = atc_phrase.ownship_from_map_enabled(config)
        if not radar:
            status.reason = (
                "map jet not published — move the aircraft on the map"
                if map_own
                else "CAOC radar feed unavailable"
            )
            return status
        units = atc_phrase.caoc_air_units(list(radar.get("units") or []))
        if not units:
            status.reason = (
                "map jet not published — move the aircraft on the map"
                if map_own
                else "no air tracks in the feed"
            )
            return status

        own = atc_phrase.match_caoc_unit_for_flight(
            units, callsign=callsign, opus=opus, config=config
        )
        if not own or (map_own and str(own.get("id") or "") != atc_phrase.MAP_OWNSHIP_ID):
            status.reason = (
                "map jet not published — move the aircraft on the map"
                if map_own
                else "own aircraft not found in the feed"
            )
            return status

        status.own_label = atc_phrase.radio_callsign_from_caoc_unit(own)
        own_pos = unit_xz(own)
        if own_pos is None:
            status.reason = "own aircraft has no position"
            return status
        if anchor is not None and field_proximity_applies(extra):
            own_dist = math.hypot(own_pos[0] - anchor[0], own_pos[1] - anchor[1])
            if own_dist > rule(config, "own_max_distance_m"):
                status.reason = (
                    f"nearest match ({status.own_label}) is "
                    f"{own_dist / 1852.0:.0f} NM out — not at the field"
                )
                return status

        members = flight_members(units, own)
        status.total = len(members)
        status.ok = True

        elev = field_elev_m(airport)
        eor = point_xz((geo or {}).get("eor"))
        eor_radius = float((geo or {}).get("eor", {}).get("radius_m") or rule(config, "eor_radius_m"))
        now = time.time()

        box = rule(config, "position_box_m")
        behind = rule(config, "position_behind_threshold_m")
        hdg_tol = rule(config, "position_heading_tolerance_deg")
        hdg_off = rule(config, "position_heading_offset_deg")
        alt_tol = rule(config, "position_alt_tolerance_m")
        max_spd = rule(config, "position_max_speed_mps")
        eor_spd = rule(config, "eor_max_speed_mps")
        status.tuning = {
            "hdg_tol": hdg_tol,
            "alt_tol": alt_tol,
            "settled_speed": rule(config, "position_settled_speed_mps"),
        }
        half_width = (
            frame.width_m / 2.0 + rule(config, "position_lateral_margin_m")
            if frame is not None
            else 0.0
        )

        for unit in members:
            pos = unit_xz(unit)
            speed = self._speed_mps(unit, now)
            if pos is None:
                continue
            along, lateral = frame.project(*pos) if frame is not None else (0.0, 0.0)
            try:
                alt_m = float(unit.get("altMeters"))
            except (TypeError, ValueError):
                alt_m = None
            height = None if (alt_m is None or elev is None) else alt_m - elev
            hdg_err = None
            if frame is not None:
                try:
                    hdg_err = angle_diff(
                        float(unit["headingDeg"]) + hdg_off, frame.heading_deg
                    )
                except (KeyError, TypeError, ValueError):
                    hdg_err = None

            fix = UnitFix(
                unit_id=str(unit.get("id") or ""),
                label=atc_phrase.radio_callsign_from_caoc_unit(unit),
                along_m=along,
                lateral_m=lateral,
                heading_err_deg=hdg_err,
                alt_m=alt_m,
                height_m=height,
                speed_mps=speed,
                x_m=pos[0],
                z_m=pos[1],
                own=unit.get("id") == own.get("id"),
            )

            on_deck = height is None or abs(height) <= alt_tol
            slow = speed is None or speed <= max_spd
            aligned = hdg_err is None or hdg_err <= hdg_tol
            if frame is not None:
                fix.on_runway = (
                    abs(lateral) <= half_width and -behind <= along <= frame.length_m
                )

            # A traced area wins over the computed box: someone looked at the
            # field and drew where "in position" actually is.
            if pos_zones:
                hit = in_any_zone(pos[0], pos[1], pos_zones)
                fix.zone = str((hit or {}).get("name") or (hit or {}).get("id") or "")
                fix.in_position = bool(hit and on_deck and slow and aligned)
            elif frame is not None:
                fix.in_position = bool(
                    on_deck
                    and slow
                    and aligned
                    and abs(lateral) <= half_width
                    and -behind <= along <= box
                )

            if eor_zones:
                hit = in_any_zone(pos[0], pos[1], eor_zones)
                fix.at_eor = bool(hit and on_deck and (speed is None or speed <= eor_spd))
                if hit and not fix.zone:
                    fix.zone = str(hit.get("name") or hit.get("id") or "")
            elif eor is not None:
                dist = math.hypot(pos[0] - eor[0], pos[1] - eor[1])
                fix.eor_dist_m = dist
                fix.at_eor = bool(
                    on_deck
                    and dist <= eor_radius
                    and (speed is None or speed <= eor_spd)
                )
            if not fix.zone:
                for zone in extra:
                    if zone_admits(zone, fix, settled=False, hdg_tol=hdg_tol, alt_tol=alt_tol):
                        fix.zone = zone_label(zone)
                        break
            status.fixes.append(fix)

        status.in_position = sum(1 for f in status.fixes if f.in_position)
        status.at_eor = sum(1 for f in status.fixes if f.at_eor)
        if not status.has_eor_area:
            status.reason = f"no EOR area drawn for {rwy}"
        return status
