"""
IFG agency catalog: who exists on the radio, independent of the Plan cursor.

A mission JSON is an optional overlay (wording, auto-fire zones, scripted
events). The Host answers whoever you tune and address. Field agencies stay
sequenced; once airborne the sandbox is open.

On Nellis Default, the filed route + Opus reserved airspace pick which
control areas this hop needs (Blackjack, Joshua, LA Center, Nellis Control).
Bandsaw and tanker stay opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import re
from typing import Any

FIELD_ORDER: tuple[str, ...] = ("delivery", "ground", "tower", "departure")
FIELD: frozenset[str] = frozenset(FIELD_ORDER)
CONTROL: frozenset[str] = frozenset({"control_east", "control_west"})
# Airborne / recovery agencies that answer without the cursor sitting on them.
AIRBORNE: frozenset[str] = frozenset(
    {
        "blackjack",
        "bandsaw",
        "joshua",
        "ops",
        "tanker",
        "other",
        "center",
        "control_east",
        "control_west",
        "approach",
    }
)
DEFAULT_FLOW_NAME = "nellis_default.json"

# kind: field | natcf | control | c2 | tanker | center | gci | ops
_CORE: dict[str, dict[str, Any]] = {
    "delivery": {
        "kind": "field",
        "spoken": "{ap} Delivery",
        "handoff": "ground",
        "services": ("clearance",),
    },
    "ground": {
        "kind": "field",
        "spoken": "{ap} Ground",
        "handoff": "tower",
        "services": ("taxi",),
    },
    "tower": {
        "kind": "field",
        "spoken": "{ap} Tower",
        "handoff": "departure",
        "services": ("takeoff", "land"),
    },
    "departure": {
        "kind": "field",
        "spoken": "{ap} Departure",
        "handoff": "blackjack",
        "services": ("radar",),
    },
    "approach": {
        "kind": "field",
        "spoken": "{ap} Approach",
        "handoff": "tower",
        "services": ("recovery",),
    },
    "blackjack": {
        "kind": "c2",
        "spoken": "Blackjack",
        "handoff": "control_east",
        "services": ("range", "tanker"),
    },
    "bandsaw": {
        "kind": "gci",
        "spoken": "Bandsaw",
        "handoff": "blackjack",
        "services": ("picture", "bogey_dope", "declare"),
    },
    "joshua": {
        "kind": "control",
        "spoken": "Joshua",
        "handoff": "control_east",
        "services": ("transit", "tanker"),
    },
    "control_east": {
        "kind": "natcf",
        "spoken": "Nellis Control",
        "handoff": "approach",
        "services": ("pickup", "vectors"),
    },
    "control_west": {
        "kind": "natcf",
        "spoken": "Nellis Control",
        "handoff": "approach",
        "services": ("pickup", "vectors"),
    },
    "center": {
        "kind": "center",
        "spoken": "Los Angeles Center",
        "handoff": "control_east",
        "services": ("radar",),
    },
    "tanker": {
        "kind": "tanker",
        "spoken": "Tanker",
        "handoff": "blackjack",
        "services": ("aar",),
    },
    "ops": {
        "kind": "ops",
        "spoken": "Ops",
        "handoff": "delivery",
        "services": ("words", "start", "status"),
    },
    "other": {
        "kind": "center",
        "spoken": "Control",
        "handoff": "blackjack",
        "services": ("radar",),
    },
}

TAKEOFF_TEMPLATES: frozenset[str] = frozenset(
    {
        "clear_takeoff",
        "clear_takeoff_rolling",
        "clear_takeoff_intersection",
        "radar_contact",
        "climb_cruise",
        "departure_handoff",
    }
)
RECOVERY_TEMPLATES: frozenset[str] = frozenset(
    {
        "approach_check_in",
        "approach_procedure",
        "cleared_approach",
        "control_handoff",
    }
)

# After ATC says "contact X", Fly shows SWITCH TO until that UHF is tuned.
HANDOFF_PENDING_BY_TEMPLATE: dict[str, str] = {
    "contact_bandsaw": "bandsaw",
    "contact_joshua": "joshua",
    "control_handoff": "approach",
    "cleared_approach": "tower",
    "contact_tower": "tower",
    "departure_handoff": "blackjack",
    "bandsaw_check_out": "blackjack",
    "joshua_check_out": "blackjack",
    # OPS start / WORDS+start → Clearance Delivery (sandbox, not a flow step).
    "ops_words": "delivery",
    "ops_start": "delivery",
}


@dataclass(frozen=True)
class Agency:
    channel: str
    kind: str
    spoken: str
    handoff: str
    services: tuple[str, ...]


def get(channel: str) -> Agency | None:
    ch = (channel or "").strip().lower()
    row = _CORE.get(ch)
    if not row:
        return None
    return Agency(
        channel=ch,
        kind=str(row["kind"]),
        spoken=str(row["spoken"]),
        handoff=str(row["handoff"]),
        services=tuple(row["services"]),
    )


def spoken_name(channel: str, airport_name: str = "") -> str:
    ch = (channel or "").strip().lower()
    if ch == "ops":
        return "Ops"
    row = get(channel)
    if row is None:
        return (channel or "").replace("_", " ").title()
    return row.spoken.format(ap=(airport_name or "").strip()).strip()


def default_handoff(channel: str) -> str:
    row = get(channel)
    return row.handoff if row else "blackjack"


def is_field(channel: str) -> bool:
    return (channel or "").strip().lower() in FIELD


def is_airborne(channel: str) -> bool:
    return (channel or "").strip().lower() in AIRBORNE


def is_control(channel: str) -> bool:
    return (channel or "").strip().lower() in CONTROL


def is_default_sandbox(config: dict[str, Any] | None) -> bool:
    """True when the Host is on Nellis Default (agency sandbox, not a custom Plan)."""
    raw = str((config or {}).get("flow_file") or "").strip() or DEFAULT_FLOW_NAME
    return Path(raw).name.lower() == DEFAULT_FLOW_NAME


# After these, Fly should show the next call on that agency (checkout / handoff).
_AGENCY_FOLLOW_ON = frozenset(
    {
        "bandsaw_check_out",
        "joshua_check_out",
        "control_handoff",
    }
)


def _agency_step_channels(channel: str) -> frozenset[str]:
    ch = str(channel or "").strip().lower()
    if ch in CONTROL:
        return CONTROL
    return frozenset({ch}) if ch else frozenset()


def display_step_for_agency(
    steps: list[dict[str, Any]] | None,
    channel: str,
    *,
    cursor_index: int = 0,
    last_tx_template: str = "",
) -> dict[str, Any] | None:
    """
    Timeline step Fly should show when the radio is on this agency.

    Blackjack check-in holds the shared cursor so optional Bandsaw does not
    steal it. Tune to Bandsaw → Bandsaw check-in, not the Blackjack hold.
    """
    want = _agency_step_channels(channel)
    if not want:
        return None
    matches: list[tuple[int, dict[str, Any]]] = []
    for i, step in enumerate(steps or []):
        if not isinstance(step, dict):
            continue
        if step.get("enabled", True) is False:
            continue
        ch = str(step.get("channel") or "").strip().lower()
        if ch in want:
            matches.append((i, step))
    if not matches:
        return None
    try:
        cur = int(cursor_index or 0)
    except (TypeError, ValueError):
        cur = 0
    pick = matches[0][1]
    for i, step in matches:
        if i >= cur:
            pick = step
            break
    last = str(last_tx_template or "").strip().lower()
    if not last:
        return pick
    for i, step in matches:
        tmpl = str(step.get("template") or "").strip().lower()
        if tmpl != last:
            continue
        later = [m for m in matches if m[0] > i]
        if later and str(later[0][1].get("template") or "").strip().lower() in (
            _AGENCY_FOLLOW_ON
        ):
            return later[0][1]
        return step
    return pick


# Control areas inferred from a filed route + Opus reserved airspace.
# Bandsaw / tanker / Ops are never inferred — those are tasked, not filed.
AREA_CONTROL = "control"
AREA_BLACKJACK = "blackjack"
AREA_JOSHUA = "joshua"
AREA_CENTER = "center"
_HOP_ORDER: tuple[str, ...] = (
    AREA_BLACKJACK,
    AREA_CONTROL,
    AREA_CENTER,
    AREA_JOSHUA,
)
JOSHUA_APPROACH_NM = 15.0
_NELLIS_ICAO = frozenset({"KLSV", "LSV", "NELLIS"})
_R2508_ICAO = frozenset({"KEDW", "EDW", "KNID", "NID", "KPMD", "PMD", "KNXP", "NXP", "KNJK", "NJK"})
_NTTR_FIXES = frozenset(
    {
        "ARCOE",
        "TORYE",
        "STRYK",
        "MINTT",
        "DUDBE",
        "LUCIL",
        "SHEET",
        "KRYSS",
        "ROTSE",
        "FYTTR",
        "FYTTR7",
        "DREAM",
        "DREAM7",
        "JUNNO",
        "COYOT",
        "COYOTE",
        "FLUSH",
        "MMM",
        "MMM8",
        "ALAMO",
        "CALIENTE",
        "REVEILLE",
        "ELGIN",
        "SALLY",
        "LEE",
        "PEAKS",
        "HAYFD",
        "HAYFORD",
        "HAYFORDPK",
        "MORPK",
        "MORMON",
        "MORMONPK",
        "MORMONPEAK",
        "STLOUIS",
    }
)
_JOSHUA_FIXES = frozenset(
    {
        "EDW",
        "KEDW",
        "NID",
        "KNID",
        "DAG",
        "PMD",
        "KPMD",
        "HEC",
        "LHS",
        "GFS",
        "NJK",
        "KNJK",
        "NXP",
        "KNXP",
        "OWENS",
        "SALINE",
        "ISABELLA",
        "PANAMINT",
        "JOSHUA",
        "R2508",
        "EDWARDS",
        "CHINALAKE",
    }
)
_CENTER_FIXES = frozenset(
    {
        "BAM",
        "MLF",
        "DTA",
        "PUC",
        "HVE",
        "MTU",
        "FFU",
        "OGD",
        "SLC",
        "KSLC",
        "ILC",
        "ELY",
        "TPH",
        "RNO",
        "KRNO",
        "FAT",
        "KFAT",
        "BFL",
        "KBFL",
        "SAC",
        "KSAC",
    }
)
_JOSHUA_AIRSPACE = ("OWENS", "SALINE", "ISABELLA", "PANAMINT", "JOSHUA", "R2508")
_HOP_SPOKEN = {
    AREA_BLACKJACK: "Blackjack",
    AREA_JOSHUA: "Joshua",
    AREA_CENTER: "LA Center",
    AREA_CONTROL: "Nellis Control",
}


@dataclass(frozen=True)
class FlightAgencies:
    """Control areas this hop needs. East/West Control is picked at handoff time."""

    areas: frozenset[str]
    recover_nellis: bool = False
    going_r2508: bool = False

    def uses(self, area: str) -> bool:
        want = (area or "").strip().lower()
        if want in CONTROL:
            return AREA_CONTROL in self.areas
        return want in self.areas


def _norm_token(tok: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (tok or "").upper())


def _route_tokens(route: str | None) -> list[str]:
    try:
        import atc_phrase

        raw = atc_phrase.parse_route_tokens(route)
    except Exception:
        raw = re.split(r"[\s./\\|+_,;]+", str(route or "").strip())
    out = [_norm_token(t) for t in raw]
    # Dotted "ST LOUIS" becomes two tokens.
    joined: list[str] = []
    skip = False
    for i, tok in enumerate(out):
        if skip:
            skip = False
            continue
        if tok == "ST" and i + 1 < len(out) and out[i + 1] == "LOUIS":
            joined.append("STLOUIS")
            skip = True
            continue
        if tok:
            joined.append(tok)
    return joined


def _is_nellis(code: str | None) -> bool:
    return _norm_token(code or "") in _NELLIS_ICAO


def _is_r2508_field(code: str | None) -> bool:
    return _norm_token(code or "") in _R2508_ICAO


def _token_hits_nttr(tokens: list[str]) -> bool:
    for tok in tokens:
        if tok in _NTTR_FIXES:
            return True
        if tok.startswith("FLEX") or tok.startswith("PEAKS"):
            return True
    return False


def _areas_from_airspace(names: list[str] | None) -> set[str]:
    found: set[str] = set()
    if not names:
        return found
    classify = None
    try:
        import atc_phrase

        classify = atc_phrase.classify_airspace_zone
    except Exception:
        classify = None
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        key = re.sub(r"[\s\-_]+", "", name.upper())
        if any(tag in key for tag in _JOSHUA_AIRSPACE):
            found.add(AREA_JOSHUA)
            continue
        if classify is not None:
            gid, _, _ = classify(name)
            if gid in ("desert_moa", "r4806", "r4807", "r4808", "r4809", "pahute", "special"):
                found.add(AREA_CONTROL)
                found.add(AREA_BLACKJACK)
            elif key.startswith("LEE") or key.startswith("SALLY"):
                found.add(AREA_CONTROL)
        elif key.startswith("LEE") or key.startswith("SALLY"):
            found.add(AREA_CONTROL)
    return found


def infer_flight(
    *,
    route: str | None = None,
    dep_icao: str | None = None,
    arr_icao: str | None = None,
    airspace_names: list[str] | None = None,
    airport: dict[str, Any] | None = None,
) -> FlightAgencies:
    """
    Which control areas this hop should include.

    Local Nellis strip with no extra clues → Blackjack + Nellis Control (today's
    default). R-2508 fixes/fields add Joshua. Enroute VORs add LA Center.
    Bandsaw and tanker are never inferred.
    """
    home = str((airport or {}).get("icao") or "")
    dep = dep_icao or home
    arr = arr_icao or ""
    tokens = _route_tokens(route)
    meaningful = [t for t in tokens if t not in _NELLIS_ICAO]
    areas = _areas_from_airspace(airspace_names)
    nttr = _token_hits_nttr(tokens)
    joshua = (
        any(t in _JOSHUA_FIXES for t in tokens)
        or _is_r2508_field(arr)
        or AREA_JOSHUA in areas
    )
    if "BTY" in tokens and any(t in _JOSHUA_FIXES for t in tokens):
        joshua = True
    center_hits = sum(1 for t in tokens if t in _CENTER_FIXES)
    center = center_hits >= 2 or AREA_CENTER in areas
    nellis = _is_nellis(dep) or _is_nellis(arr) or _is_nellis(home)

    if nttr:
        areas.add(AREA_BLACKJACK)
        areas.add(AREA_CONTROL)
    if joshua:
        areas.add(AREA_JOSHUA)
        if nellis:
            areas.add(AREA_CONTROL)
            # R-2508 is ~100 NM from the Nellis departure zone — Center owns the
            # transit. Joshua is only the handoff when approaching that complex.
            areas.add(AREA_CENTER)
    if center:
        areas.add(AREA_CENTER)
        if nellis:
            areas.add(AREA_CONTROL)
    if nellis and not meaningful and not areas:
        areas.update({AREA_BLACKJACK, AREA_CONTROL})
    if nellis and not areas:
        areas.update({AREA_BLACKJACK, AREA_CONTROL})
    if not areas:
        areas.update({AREA_BLACKJACK, AREA_CONTROL})
    recover = _is_nellis(arr) or (nellis and not arr and not _is_r2508_field(arr))
    return FlightAgencies(
        areas=frozenset(areas),
        recover_nellis=recover,
        going_r2508=bool(joshua),
    )


def infer_from_context(
    *,
    airport: dict[str, Any] | None = None,
    opus: Any = None,
    config: dict[str, Any] | None = None,
) -> FlightAgencies:
    names: list[str] = []
    try:
        import atc_phrase

        names = list(atc_phrase.airspace_areas_for_flight(config, opus) or [])
    except Exception:
        names = []
    route = getattr(opus, "fp_route_string", None) if opus is not None else None
    dep = getattr(opus, "dep_icao", None) if opus is not None else None
    arr = getattr(opus, "arr_icao", None) if opus is not None else None
    return infer_flight(
        route=route,
        dep_icao=dep,
        arr_icao=arr,
        airspace_names=names,
        airport=airport,
    )


def format_hop(plan: FlightAgencies) -> str:
    bits = [_HOP_SPOKEN[a] for a in _HOP_ORDER if a in plan.areas]
    if plan.recover_nellis:
        bits.append("Approach")
    return ", ".join(bits)


def _nm_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    return (
        (lon - lon0) * 60.0 * math.cos(math.radians(lat0)),
        (lat - lat0) * 60.0,
    )


def _point_seg_nm(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def nm_to_agency(
    airport: dict[str, Any] | None,
    trigger: str,
    lat: float | None,
    lon: float | None,
) -> float | None:
    """NM to a trigger polygon (0 if inside). None when the zone is missing."""
    try:
        lat_f = float(lat)  # type: ignore[arg-type]
        lon_f = float(lon)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    best: float | None = None
    for zone in zones_for_trigger(airport, trigger):
        if str(zone.get("kind") or "").strip().lower() != "polygon":
            continue
        pts = zone.get("points")
        if not isinstance(pts, list) or len(pts) < 3:
            continue
        if _in_latlon_polygon(lat_f, lon_f, pts):
            return 0.0
        try:
            lat0 = float(pts[0]["lat"])
            lon0 = float(pts[0]["lon"])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        xy: list[tuple[float, float]] = []
        for pt in pts:
            if not isinstance(pt, dict):
                continue
            try:
                xy.append(_nm_xy(float(pt["lat"]), float(pt["lon"]), lat0, lon0))
            except (KeyError, TypeError, ValueError):
                continue
        if len(xy) < 3:
            continue
        px, py = _nm_xy(lat_f, lon_f, lat0, lon0)
        for i, (ax, ay) in enumerate(xy):
            bx, by = xy[(i + 1) % len(xy)]
            dist = _point_seg_nm(px, py, ax, ay, bx, by)
            if best is None or dist < best:
                best = dist
    return best


def approaching_joshua(
    airport: dict[str, Any] | None,
    lat: float | None,
    lon: float | None,
    *,
    within_nm: float = JOSHUA_APPROACH_NM,
) -> bool:
    dist = nm_to_agency(airport, "joshua", lat, lon)
    if dist is None:
        return False
    return dist <= float(within_nm)


def handoff_from_plan(
    from_channel: str,
    plan: FlightAgencies,
    *,
    airport: dict[str, Any] | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> str | None:
    """
    Next control area from the inferred hop.

    Departure never names Joshua — R-2508 is ~100 NM outside the departure
    zone. Transit is Nellis Control and/or LA Center; Joshua only when the
    jet is approaching that complex.
    """
    areas = set(plan.areas)
    from_ch = (from_channel or "").strip().lower()
    has_fix = lat is not None and lon is not None
    near_joshua = bool(
        AREA_JOSHUA in areas and approaching_joshua(airport, lat, lon)
    )

    def control_ch() -> str:
        if AREA_CONTROL not in areas:
            return "approach"
        return control_for_ll(airport, lat, lon)

    def transit() -> str:
        if AREA_BLACKJACK in areas:
            return AREA_BLACKJACK
        if AREA_CONTROL in areas:
            return control_ch()
        if AREA_CENTER in areas:
            return AREA_CENTER
        return AREA_BLACKJACK

    if from_ch == "departure":
        return transit()
    if from_ch == AREA_BLACKJACK:
        if AREA_CONTROL in areas:
            return control_ch()
        if AREA_CENTER in areas:
            return AREA_CENTER
        if near_joshua:
            return AREA_JOSHUA
        return control_ch() if plan.recover_nellis else transit()
    if from_ch in CONTROL:
        if near_joshua:
            return AREA_JOSHUA
        if plan.going_r2508 and AREA_CENTER in areas:
            return AREA_CENTER
        if plan.recover_nellis:
            return "approach"
        if AREA_CENTER in areas:
            return AREA_CENTER
        return "approach"
    if from_ch in ("center", "other"):
        if near_joshua:
            return AREA_JOSHUA
        if plan.going_r2508 and not has_fix:
            # Already on Center; without a fix, Joshua is the planned next.
            return AREA_JOSHUA
        if plan.going_r2508 and has_fix:
            # Still in the 100 NM gap — stay with Center until the R-2508 ring.
            return AREA_CENTER
        if plan.recover_nellis and AREA_CONTROL in areas:
            return control_ch()
        return "approach"
    if from_ch == AREA_JOSHUA:
        if plan.recover_nellis and AREA_CENTER in areas:
            return AREA_CENTER
        if plan.recover_nellis and AREA_CONTROL in areas:
            return control_ch()
        if AREA_CENTER in areas:
            return AREA_CENTER
        return "approach"
    return None


def contact_phase(state: dict[str, Any] | None) -> str:
    raw = str((state or {}).get("contact_phase") or "").strip().lower()
    if raw in ("field", "airborne", "recovery"):
        return raw
    return "field"


def last_agency(state: dict[str, Any] | None) -> str:
    return str((state or {}).get("last_agency") or "").strip().lower()


def pending_contact(state: dict[str, Any] | None) -> str:
    """Agency ATC just sent them to, until they tune or check in there."""
    return str((state or {}).get("pending_contact") or "").strip().lower()


def note_tx(
    state: dict[str, Any] | None,
    channel: str,
    template: str = "",
) -> None:
    """Remember who we last spoke as, and whether the flight is airborne."""
    if not isinstance(state, dict):
        return
    ch = (channel or "").strip().lower()
    tmpl = (template or "").strip().lower()
    if ch:
        state["last_agency"] = ch
        if pending_contact(state) == ch:
            state.pop("pending_contact", None)
    dest = ""
    if tmpl == "bj_range_exit":
        dest = str(state.get("control_channel") or "control_east").strip().lower()
    elif tmpl == "contact_control":
        dest = str(state.get("control_channel") or "control_east").strip().lower()
    else:
        dest = str(HANDOFF_PENDING_BY_TEMPLATE.get(tmpl) or "").strip().lower()
    if dest:
        state["pending_contact"] = dest
    phase = contact_phase(state)
    if tmpl in TAKEOFF_TEMPLATES or (
        ch == "departure" and tmpl in ("radar_contact", "climb_cruise", "departure_handoff")
    ):
        state["contact_phase"] = "airborne"
        return
    if tmpl in RECOVERY_TEMPLATES or ch == "approach":
        if phase != "field":
            state["contact_phase"] = "recovery"
        return
    if ch in AIRBORNE and ch != "approach":
        state["contact_phase"] = "airborne"
        return
    if ch in FIELD and phase != "airborne":
        state["contact_phase"] = "field"


def reset_contact(state: dict[str, Any] | None) -> None:
    if not isinstance(state, dict):
        return
    state["contact_phase"] = "field"
    state["last_agency"] = "delivery"
    state.pop("control_checked_in", None)
    state.pop("control_channel", None)
    state.pop("blackjack_checked_in", None)
    state.pop("clearance_amendment_copied", None)
    state.pop("amended_altitude_ft", None)
    state.pop("pending_contact", None)


def field_agency(engine: Any) -> str:
    """Current sequenced field agency from the cursor (or last_agency)."""
    step = engine.current_step() if engine is not None and hasattr(engine, "current_step") else None
    ch = str((step or {}).get("channel") or "").strip().lower()
    if ch in FIELD:
        return ch
    last = last_agency(getattr(engine, "state", None))
    if last in FIELD:
        return last
    return "delivery"


def too_early_field(current: str, addressed: str) -> bool:
    """True when they called a later field agency than the one they still need."""
    cur = (current or "").strip().lower()
    addr = (addressed or "").strip().lower()
    if cur not in FIELD or addr not in FIELD:
        return False
    return FIELD_ORDER.index(addr) > FIELD_ORDER.index(cur)


def build_field_redirect(
    airport: dict[str, Any],
    callsign: str,
    addressed: str,
    current: str,
) -> str:
    """
    They called Tower too early: answer as Tower, send them back to Ground.

    '{cs}, Nellis Tower, contact Ground.'
    """
    try:
        import atc_phrase
    except Exception:
        cs = callsign
        called = spoken_name(addressed, str((airport or {}).get("name") or ""))
        stay = spoken_name(current, str((airport or {}).get("name") or ""))
        return f"{cs}, {called}, contact {stay}."

    cs = atc_phrase.speak_callsign(callsign)
    ap = str((airport or {}).get("name") or "").strip()
    called = spoken_name(addressed, ap)
    stay_ch = (current or "").strip().lower()
    stay = atc_phrase.speak_agency_name(stay_ch) if stay_ch else spoken_name(current, ap)
    return f"{cs}, {called}, contact {stay}."


def _remap_control(
    addressed: str | None,
    tuned: str | None,
    transcript: str = "",
) -> str | None:
    addr = (addressed or "").strip().lower() or None
    tun = (tuned or "").strip().lower() or None
    head = " ".join((transcript or "").lower().split()[:8])
    if re.search(r"(?<!\w)sally(?!\w)", head):
        return "control_east"
    if re.search(r"(?<!\w)lee(?!\w)", head):
        return "control_west"
    if addr in CONTROL:
        if tun in CONTROL:
            return tun
        return addr
    if addr in ("control", "other") and tun in CONTROL:
        return tun
    if addr == "control":
        return tun if tun in CONTROL else "control_east"
    return addr


def resolve(
    *,
    tuned_channel: str | None = None,
    addressed: str | None = None,
    cursor_channel: str = "",
    mission_phase: str = "",
    transcript: str = "",
) -> str:
    """
    Who should answer / whose tips to show.

    Address wins (with Control East/West remapped from the live UHF).
    Airborne tips follow the tuned radio. Field stays on the cursor.
    """
    tun = (tuned_channel or "").strip().lower() or None
    cursor = (cursor_channel or "").strip().lower()
    phase = (mission_phase or "").strip().lower()
    addr = (addressed or "").strip().lower() or None
    if not addr and transcript:
        try:
            import voice_intent

            addr = voice_intent.extract_channel(transcript)
        except Exception:
            addr = None
    remapped = _remap_control(addr, tun, transcript=transcript)
    if remapped:
        return remapped
    if addr:
        return addr
    # Preflight backup radio: OPS answers on the ramp before Delivery.
    if tun == "ops":
        return "ops"
    # Field sequence: tips stay on the cursor even if they jumped the radio.
    if phase in ("", "departure") and cursor in FIELD:
        return cursor
    if tun and is_airborne(tun) and phase in ("flight", "approach"):
        return tun
    if tun and is_airborne(tun) and is_airborne(cursor):
        return tun
    return cursor or tun or ""


def sandbox_mission_phase(
    *,
    cursor_phase: str,
    cursor_channel: str,
    tuned_channel: str | None,
    state: dict[str, Any] | None,
) -> str:
    """
    Mission phase for voice scoring / Fly tips.

    After takeoff the sandbox is open even if the cursor is still on Departure.
    """
    cursor_p = (cursor_phase or "").strip().lower()
    tuned = (tuned_channel or "").strip().lower()
    cursor_ch = (cursor_channel or "").strip().lower()
    phase = contact_phase(state)
    # Landing / taxi-in stay Approach even if we never flipped contact_phase.
    if cursor_p == "approach":
        return "approach"
    if phase == "recovery":
        return "approach"
    if phase == "airborne":
        if tuned == "approach" or cursor_ch == "approach":
            return "approach"
        return "flight"
    return cursor_p


def _in_latlon_polygon(lat: float, lon: float, points: list[dict[str, Any]]) -> bool:
    """Ray-cast on geographic degrees. Fine for NTTR-sized polygons."""
    pts: list[tuple[float, float]] = []
    for pt in points:
        try:
            pts.append((float(pt["lon"]), float(pt["lat"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(pts) < 3:
        return False
    inside = False
    j = len(pts) - 1
    x, z = lon, lat
    for i, (xi, zi) in enumerate(pts):
        xj, zj = pts[j]
        if (zi > z) != (zj > z):
            t = (z - zi) / (zj - zi) if zj != zi else 0.0
            if x < xi + t * (xj - xi):
                inside = not inside
        j = i
    return inside


def zones_for_trigger(airport: dict[str, Any] | None, trigger: str) -> list[dict[str, Any]]:
    want = (trigger or "").strip().lower()
    geo = (airport or {}).get("geometry") if isinstance(airport, dict) else None
    zones = (geo or {}).get("zones") if isinstance(geo, dict) else None
    if not want or not isinstance(zones, list):
        return []
    out: list[dict[str, Any]] = []
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        if str(zone.get("trigger") or "").strip().lower() == want:
            out.append(zone)
    return out


def ll_in_agency(airport: dict[str, Any] | None, trigger: str, lat: float, lon: float) -> bool:
    for zone in zones_for_trigger(airport, trigger):
        if str(zone.get("kind") or "").strip().lower() != "polygon":
            continue
        pts = zone.get("points")
        if isinstance(pts, list) and _in_latlon_polygon(lat, lon, pts):
            return True
    return False


def control_for_ll(
    airport: dict[str, Any] | None,
    lat: float | None,
    lon: float | None,
    *,
    default: str = "control_east",
) -> str:
    """
    Which NATCF sector owns this lat/lon.

    Lee (west) first so the western restricted areas / Lee Corridor win over a
    default Sally. No match → Control East (IFG ch 7).
    """
    try:
        lat_f = float(lat)  # type: ignore[arg-type]
        lon_f = float(lon)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if ll_in_agency(airport, "control_west", lat_f, lon_f):
        return "control_west"
    if ll_in_agency(airport, "control_east", lat_f, lon_f):
        return "control_east"
    return default


def owning_agency_for_ll(
    airport: dict[str, Any] | None,
    lat: float | None,
    lon: float | None,
) -> str:
    """
    Who owns this lat/lon right now: Joshua, NATCF East/West, or Blackjack.

    Empty when the fix is missing or outside those polygons. Does not guess
    Joshua from a filed R-2508 hop — that is a later handoff, not who you
    are with over the NTTR.
    """
    try:
        lat_f = float(lat)  # type: ignore[arg-type]
        lon_f = float(lon)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if ll_in_agency(airport, "joshua", lat_f, lon_f):
        return "joshua"
    if ll_in_agency(airport, "blackjack", lat_f, lon_f):
        return "blackjack"
    if ll_in_agency(airport, "control_west", lat_f, lon_f):
        return "control_west"
    if ll_in_agency(airport, "control_east", lat_f, lon_f):
        return "control_east"
    return ""


def joshua_is_live(
    airport: dict[str, Any] | None,
    state: dict[str, Any] | None,
    *,
    lat: float | None = None,
    lon: float | None = None,
    tuned_channel: str | None = None,
) -> bool:
    """True when this sortie is actually with / going to Joshua."""
    tun = str(tuned_channel or "").strip().lower()
    if tun == "joshua":
        return True
    if last_agency(state) == "joshua" or pending_contact(state) == "joshua":
        return True
    if lat is None or lon is None:
        return False
    return ll_in_agency(airport, "joshua", lat, lon) or approaching_joshua(
        airport, lat, lon
    )


def fly_label(channel: str, airport_name: str = "") -> str:
    """Fly hero / cue header. NATCF keeps East vs West; radio calls stay Nellis Control."""
    ch = (channel or "").strip().lower()
    if ch == "control_east":
        return "Nellis Control East"
    if ch == "control_west":
        return "Nellis Control West"
    return spoken_name(ch, airport_name)


def blackjack_exit_handoff(
    airport: dict[str, Any] | None,
    lat: float | None,
    lon: float | None,
    picked: str,
) -> str:
    """
    Blackjack range exit: Nellis Control until inside Approach airspace.

    Custom plans sometimes list Approach as the next agency. That is only
    correct once the jet is in the Approach circle — 75 NM out is still NATCF.
    """
    want = (picked or "").strip().lower()
    if want != "approach":
        return want or "control_east"
    dist = nm_to_agency(airport, "approach", lat, lon)
    if dist is None or dist > 0.0:
        return control_for_ll(airport, lat, lon)
    return "approach"

