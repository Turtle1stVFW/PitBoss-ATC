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
    # match_caoc_unit_for_flight returns its best guess even on thin evidence, so
    # with nobody flying it will happily hand back some AI flight. Anything this
    # far from the field is not the jet about to depart, whatever it matched.
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
    """Geometry for one runway direction, tolerant of 21R / 21r / rwy 21R."""
    runways = airport_geometry(airport).get("runways")
    if not isinstance(runways, dict):
        return None
    want = atc_phrase.normalize_runway(runway) or str(runway or "").strip().upper()
    if not want:
        return None
    for key, val in runways.items():
        if not isinstance(val, dict):
            continue
        if (atc_phrase.normalize_runway(key) or str(key).strip().upper()) == want:
            return val
    return None


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
    try:
        return float(unit["xMeters"]), float(unit["zMeters"])
    except (KeyError, TypeError, ValueError):
        return None


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


def zones_by_ref(
    airport: dict[str, Any] | None,
    ref: str,
    runway: str | None = None,
) -> list[dict[str, Any]]:
    """
    Every zone a step is asking for, by id or by trigger tag.

    An id pins to one drawn area. A tag follows the active runway and returns
    *all* matching areas (e.g. both AS and AN EOR on 03L), so the flight can
    sit in any of them. Runway-specific areas are listed before field-wide ones.
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
            heading_deg=_bearing_deg(dx, dz),
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
    if fix.heading_err_deg is not None and fix.heading_err_deg > hdg_tol:
        return False
    return True


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
        # Inside but not settled is the interesting case: the flight is where it
        # should be and the call is waiting on the jets, not on the geometry.
        if self.settled and self.inside > self.qualified:
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
    "clear_takeoff": ("in_position", "auto_takeoff_clearance"),
    "clear_takeoff_rolling": ("in_position", "auto_takeoff_clearance"),
    "clear_takeoff_intersection": ("in_position", "auto_takeoff_clearance"),
    "monitor_tower": ("eor", "auto_monitor_tower"),
}


@dataclass(frozen=True)
class StepTrigger:
    """A step's `trigger` block: which zone arms it, and how patiently."""

    zone: str
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
        bits = [f"{'leaves' if self.when == 'leaving' else 'in'} {self.zone}"]
        if self.flight:
            bits.append("whole flight" if self.flight == "all" else "just me")
        if self.settled:
            bits.append("settled")
        if self.dwell_s:
            bits.append(f"{self.dwell_s:g}s dwell")
        if self.gap_s:
            bits.append(f"{self.gap_s:g}s gap")
        return ", ".join(bits)


def gap_remaining(
    trigger: StepTrigger | None,
    state: dict[str, Any] | None,
    now: float | None = None,
) -> float:
    """
    Seconds still to wait so this call does not tread on the previous transmission.

    Measured from the `last_tx_at` the flow engine stamps on every step it plays,
    so no new state is needed. Zero when nothing has been said yet.
    """
    if trigger is None or trigger.gap_s <= 0:
        return 0.0
    try:
        last = float((state or {}).get("last_tx_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if last <= 0.0:
        return 0.0
    elapsed = (time.time() if now is None else now) - last
    return max(0.0, trigger.gap_s - elapsed)


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
        if zone:
            flight = str(raw.get("flight") or "").strip().casefold()
            when = str(raw.get("when") or "").strip().casefold()
            return StepTrigger(
                zone=zone,
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

    # -- motion ----------------------------------------------------------
    def _speed_mps(self, unit: dict[str, Any], now: float) -> float | None:
        uid = str(unit.get("id") or "")
        pos = unit_xz(unit)
        if not uid or pos is None:
            return None
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

    def armed(self, key: str) -> bool:
        """Whether `fire_once` would still fire for this key."""
        return key not in self._fired

    def reset(self) -> None:
        self._since.clear()
        self._fired.clear()
        self._entered.clear()

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
        if not radar:
            status.reason = "CAOC radar feed unavailable"
            return status
        units = atc_phrase.caoc_air_units(list(radar.get("units") or []))
        if not units:
            status.reason = "no air tracks in the feed"
            return status

        own = atc_phrase.match_caoc_unit_for_flight(
            units, callsign=callsign, opus=opus, config=config
        )
        if not own:
            status.reason = "own aircraft not found in the feed"
            return status

        status.own_label = atc_phrase.radio_callsign_from_caoc_unit(own)
        own_pos = unit_xz(own)
        if own_pos is None:
            status.reason = "own aircraft has no position"
            return status
        if anchor is not None:
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
