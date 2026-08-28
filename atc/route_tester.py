"""
Offline route tester: a synthetic jet vs agency zones and dry ATC phrases.

The zone editor's /tester page POSTs ticks here. Nothing is written to
flow_state.json and nothing is transmitted on SRS.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent
FIXES_PATH = HERE / "fixes.json"
NAVPOINTS_PATH = HERE / "nttr_navpoints.json"
OVERLAYS_DIR = HERE.parent / "tools" / "overlays"
_TOOLS = HERE.parent / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import agencies  # noqa: E402
import atc_phrase  # noqa: E402
import flow_engine  # noqa: E402
import runway_position as rp  # noqa: E402

KT_TO_MPS = 0.514444
AGENCY_TRIGGERS: tuple[str, ...] = (
    "delivery",
    "ground",
    "tower",
    "departure",
    "approach",
    "blackjack",
    "joshua",
    "center",
    "control_east",
    "control_west",
)
FIELD_TRIGGERS: tuple[str, ...] = ("delivery", "ground", "tower")
AIRBORNE_TRIGGERS: tuple[str, ...] = (
    "blackjack",
    "joshua",
    "center",
    "control_west",
    "control_east",
    "approach",
    "departure",
)
CHECKIN_TEMPLATES: dict[str, str] = {
    "delivery": "clearance",
    "ground": "taxi",
    "tower": "lineup",
    "departure": "radar_contact",
    "blackjack": "bj_check_in",
    "bandsaw": "bandsaw_check_in",
    "joshua": "joshua_check_in",
    "control_east": "control_check_in",
    "control_west": "control_check_in",
    "center": "center_check_in",
    "approach": "approach_check_in",
    "ops": "ops_check_in",
}
HANDOFF_TEMPLATES: dict[str, str] = {
    "departure": "departure_handoff",
    "blackjack": "bj_range_exit",
    "control_east": "control_handoff",
    "control_west": "control_handoff",
    "center": "center_handoff",
    "joshua": "joshua_check_out",
    "bandsaw": "bandsaw_check_out",
}
TUNE_CHANNELS: tuple[str, ...] = (
    "delivery",
    "ground",
    "tower",
    "departure",
    "blackjack",
    "bandsaw",
    "joshua",
    "control_east",
    "control_west",
    "center",
    "approach",
    "ops",
    "tanker",
)


def _num(val: Any) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _token(raw: str) -> str:
    return agencies._norm_token(raw)


def _walk_latlon(obj: Any, out: dict[str, dict[str, Any]]) -> None:
    """Pull id/lat/lon (and aliases) out of an approaches catalog."""
    if isinstance(obj, list):
        for item in obj:
            _walk_latlon(item, out)
        return
    if not isinstance(obj, dict):
        return
    ident = _token(str(obj.get("id") or obj.get("direct_fix") or ""))
    lat = _num(obj.get("lat"))
    lon = _num(obj.get("lon"))
    if ident and lat is not None and lon is not None:
        say = str(obj.get("say") or obj.get("direct_say") or ident).strip()
        row = {"id": ident, "lat": lat, "lon": lon, "say": say, "source": "approaches"}
        out[ident] = row
        for alias in obj.get("aliases") or []:
            key = _token(str(alias))
            if key:
                out.setdefault(key, row)
    for value in obj.values():
        if isinstance(value, (dict, list)):
            _walk_latlon(value, out)


def _airport_icao_fixes(airports: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, airport in (airports or {}).items():
        if not isinstance(airport, dict):
            continue
        field = rp.airport_field_latlon(airport)
        if field is None:
            continue
        lat, lon = field
        icao = _token(str(airport.get("icao") or key))
        name = str(airport.get("name") or icao).strip()
        row = {"id": icao, "lat": lat, "lon": lon, "say": name, "source": "airport"}
        if icao:
            out.setdefault(icao, row)
        key_tok = _token(str(key))
        if key_tok:
            out.setdefault(key_tok, row)
    return out


def _fixes_file() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not FIXES_PATH.is_file():
        return out
    try:
        data = atc_phrase.load_json(FIXES_PATH)
    except (OSError, ValueError):
        return out
    rows = data.get("fixes") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return out
    for item in rows:
        if not isinstance(item, dict):
            continue
        ident = _token(str(item.get("id") or ""))
        lat = _num(item.get("lat"))
        lon = _num(item.get("lon"))
        if not ident or lat is None or lon is None:
            continue
        say = str(item.get("say") or ident).strip()
        row = {"id": ident, "lat": lat, "lon": lon, "say": say, "source": "fixes"}
        out.setdefault(ident, row)
        for alias in item.get("aliases") or []:
            key = _token(str(alias))
            if key:
                out.setdefault(key, row)
    return out


def _nav_kind(ident: str) -> str:
    return "fix" if re.fullmatch(r"[A-Z]{3,6}", ident or "") else "point"


def _ingest_nav_point(
    out: dict[str, dict[str, Any]],
    *,
    name: str,
    lat: float,
    lon: float,
    source: str,
) -> None:
    ident = _token(name)
    if len(ident) < 2 or ident[0].isdigit():
        return
    row = {
        "id": ident,
        "lat": lat,
        "lon": lon,
        "say": name if not name.isupper() else ident,
        "name": name,
        "kind": _nav_kind(ident),
        "source": source,
    }
    existing = out.get(ident)
    if existing is not None:
        existing.update(row)
        return
    out[ident] = row


def _points_from_kml_path(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    try:
        raw = path.read_bytes()
    except OSError:
        return out
    if path.suffix.lower() == ".kmz" or raw[:2] == b"PK":
        try:
            import zone_overlay

            raw = zone_overlay._kml_from_kmz(raw)
        except Exception:
            return out
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return out

    def local(tag: str) -> str:
        return str(tag).rsplit("}", 1)[-1]

    for pm in root.iter():
        if local(pm.tag) != "Placemark":
            continue
        name = ""
        coords: tuple[float, float] | None = None
        is_point = False
        for ch in pm.iter():
            kind = local(ch.tag)
            if kind == "name" and ch.text and not name:
                name = ch.text.strip()
            elif kind == "Point":
                is_point = True
            elif kind == "coordinates" and is_point and coords is None:
                chunk = (ch.text or "").strip().split()
                if not chunk:
                    continue
                parts = chunk[0].split(",")
                if len(parts) < 2:
                    continue
                try:
                    coords = (float(parts[1]), float(parts[0]))
                except ValueError:
                    coords = None
        if not (is_point and coords and name):
            continue
        _ingest_nav_point(
            out, name=name, lat=coords[0], lon=coords[1], source=path.name
        )
    return out


_NAV_CACHE: tuple[float, dict[str, dict[str, Any]]] | None = None


def _nttr_nav_catalog() -> dict[str, dict[str, Any]]:
    """Bundled NTTR.kml extract, then any tools/overlays/*.kml that is newer."""
    global _NAV_CACHE
    stamp = 0.0
    paths: list[Path] = []
    if NAVPOINTS_PATH.is_file():
        stamp = max(stamp, NAVPOINTS_PATH.stat().st_mtime)
        paths.append(NAVPOINTS_PATH)
    if OVERLAYS_DIR.is_dir():
        for path in sorted(OVERLAYS_DIR.iterdir()):
            if path.suffix.lower() in (".kml", ".kmz"):
                stamp = max(stamp, path.stat().st_mtime)
                paths.append(path)
    if _NAV_CACHE is not None and _NAV_CACHE[0] == stamp:
        return _NAV_CACHE[1]
    out: dict[str, dict[str, Any]] = {}
    if NAVPOINTS_PATH.is_file():
        try:
            data = atc_phrase.load_json(NAVPOINTS_PATH)
        except (OSError, ValueError):
            data = {}
        for item in (data.get("points") if isinstance(data, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            lat, lon = _num(item.get("lat")), _num(item.get("lon"))
            if lat is None or lon is None:
                continue
            _ingest_nav_point(
                out,
                name=str(item.get("name") or item.get("id") or ""),
                lat=lat,
                lon=lon,
                source="nttr",
            )
    json_mtime = NAVPOINTS_PATH.stat().st_mtime if NAVPOINTS_PATH.is_file() else 0.0
    for path in paths:
        if path.suffix.lower() not in (".kml", ".kmz"):
            continue
        # Bundled extract already came from NTTR.kml; only re-parse if the
        # overlay file is newer (user dropped an updated pack).
        if json_mtime and path.stat().st_mtime <= json_mtime + 2:
            continue
        overlay = _points_from_kml_path(path)
        for key, row in overlay.items():
            existing = out.get(key)
            if existing is not None:
                existing.update(row)
            else:
                out[key] = row
    _NAV_CACHE = (stamp, out)
    return out


def load_fix_catalog(
    airport: dict[str, Any] | None = None,
    airports: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Token → {id, lat, lon, say, source}.

    Merge order: atc/fixes.json (FAA navaids / SID fallbacks), then named
    points from NTTR.kml (bundled extract + tools/overlays), then airport
    ICAO coords, then the approaches catalog.
    """
    catalog = _fixes_file()
    for key, row in _nttr_nav_catalog().items():
        existing = catalog.get(key)
        if existing is not None:
            existing["lat"] = row["lat"]
            existing["lon"] = row["lon"]
            existing["source"] = row.get("source") or "nttr"
            existing["kind"] = row.get("kind") or _nav_kind(key)
            if row.get("name"):
                existing["name"] = row["name"]
        else:
            catalog[key] = row
    catalog.update(_airport_icao_fixes(airports or {}))
    if airport:
        try:
            _walk_latlon(atc_phrase.load_approach_catalog(airport), catalog)
        except Exception:
            pass
    return catalog


def resolve_route(
    route: str | None,
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    tokens = agencies._route_tokens(route)
    waypoints: list[dict[str, Any]] = []
    unknown: list[str] = []
    seen_xy: set[tuple[float, float]] = set()
    for tok in tokens:
        hit = catalog.get(tok)
        if tok.startswith("FLEX") and not hit:
            hit = catalog.get("FLEX")
        if not hit:
            if not tok.startswith("FLEX"):
                unknown.append(tok)
            continue
        lat, lon = float(hit["lat"]), float(hit["lon"])
        key = (round(lat, 5), round(lon, 5))
        if key in seen_xy and waypoints and waypoints[-1]["id"] == hit["id"]:
            continue
        seen_xy.add(key)
        waypoints.append(
            {
                "id": hit["id"],
                "token": tok,
                "lat": lat,
                "lon": lon,
                "say": hit.get("say") or hit["id"],
            }
        )
    return {"tokens": tokens, "waypoints": waypoints, "unknown": unknown, "route": route or ""}


def weather_from_body(body: dict[str, Any] | None) -> atc_phrase.Weather:
    """METAR string wins; otherwise discrete wind / altimeter / vis / ceiling."""
    raw = str((body or {}).get("metar") or "").strip()
    if raw:
        wx = atc_phrase.parse_metar(raw)
        if wx.wind_dir is None and wx.wind_speed_kt is None and wx.altimeter_inhg is None:
            # Typed numbers with a leftover METAR field — fall through.
            pass
        else:
            return wx
    wind_dir = None
    try:
        if (body or {}).get("wind_dir") not in (None, ""):
            wind_dir = int(float(body["wind_dir"]))  # type: ignore[index]
    except (TypeError, ValueError):
        wind_dir = 210
    if wind_dir is None:
        wind_dir = 210
    speed = _num((body or {}).get("wind_speed_kt"))
    if speed is None:
        speed = 5.0
    alt = _num((body or {}).get("altimeter_inhg"))
    if alt is None:
        alt = 29.92
    vis = _num((body or {}).get("visibility_sm"))
    ceil = _num((body or {}).get("ceiling_ft"))
    return atc_phrase.Weather(
        wind_dir,
        int(speed),
        alt,
        raw,
        ceiling_ft=None if ceil is None else int(ceil),
        visibility_sm=vis if vis is not None else 10.0,
    )


def weather_to_dict(wx: atc_phrase.Weather) -> dict[str, Any]:
    return {
        "wind_dir": wx.wind_dir,
        "wind_speed_kt": wx.wind_speed_kt,
        "altimeter_inhg": wx.altimeter_inhg,
        "visibility_sm": wx.visibility_sm,
        "ceiling_ft": wx.ceiling_ft,
        "metar": wx.raw or "",
    }


def _callsign(config: dict[str, Any] | None, body: dict[str, Any] | None = None) -> str:
    raw = str((body or {}).get("callsign") or "").strip()
    if raw:
        return raw
    override = atc_phrase.callsign_override(config or {})
    return override or "FLEECE 1"


def _opus_for_route(callsign: str, route: str | None) -> atc_phrase.OpusFlightContext:
    ctx = atc_phrase.synthetic_flight_context(callsign)
    tokens = agencies._route_tokens(route)
    dep = tokens[0] if tokens else None
    arr = tokens[-1] if len(tokens) >= 2 else dep
    ctx.fp_route_string = (route or "").strip() or None
    ctx.dep_icao = dep
    ctx.arr_icao = arr
    ctx.mode3 = "0551"
    ctx.fp_altitude = "FL230"
    return ctx


def _load_mission(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = config or {}
    try:
        path = flow_engine.resolve_flow_path(cfg, repair=False)
        if path.is_file():
            data = flow_engine.load_json(path)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    fallback = HERE / "flows" / "nellis_default.json"
    if fallback.is_file():
        data = flow_engine.load_json(fallback)
        if isinstance(data, dict):
            return data
    return {"steps": []}


def _runway(airport: dict[str, Any] | None, raw: str | None) -> str:
    want = atc_phrase.normalize_runway(raw) or str(raw or "").strip().upper()
    if want:
        return want
    listed = list((airport or {}).get("runways") or [])
    return str(listed[0] or "21R") if listed else "21R"


def _heading_err_deg(
    airport: dict[str, Any] | None,
    heading_deg: float | None,
    runway: str | None,
) -> float | None:
    """Smallest error vs any drawn centreline (reciprocals / swapped labels)."""
    if heading_deg is None:
        return None
    best: float | None = None
    geo = rp.airport_geometry(airport)
    runways = geo.get("runways") if isinstance(geo.get("runways"), dict) else {}
    names = [runway] if runway else []
    names.extend(str(k) for k in runways)
    seen: set[str] = set()
    for name in names:
        key = atc_phrase.normalize_runway(name) or str(name or "").strip().upper()
        if not key or key in seen:
            continue
        seen.add(key)
        frame = rp.RunwayFrame.build(key, rp.runway_geometry(airport, key))
        if frame is None:
            continue
        err = rp.angle_diff(float(heading_deg), frame.heading_deg)
        if best is None or err < best:
            best = err
    return best


def make_unit_fix(
    *,
    lat: float,
    lon: float,
    alt_ft_agl: float | None,
    heading_deg: float | None,
    speed_kt: float | None,
    airport: dict[str, Any] | None,
    runway: str | None = None,
) -> rp.UnitFix:
    x_m, z_m = atc_phrase.caoc_ll_to_xz(lat, lon)
    elev = rp.field_elev_m(airport)
    height_m = None if alt_ft_agl is None else float(alt_ft_agl) / rp.FT_PER_M
    alt_m = None if height_m is None or elev is None else elev + height_m
    speed_mps = None if speed_kt is None else float(speed_kt) * KT_TO_MPS
    rwy = _runway(airport, runway)
    heading_err = _heading_err_deg(airport, heading_deg, rwy)
    along_m = 0.0
    lateral_m = 0.0
    frame = rp.RunwayFrame.build(rwy, rp.runway_geometry(airport, rwy))
    if frame is not None:
        along_m, lateral_m = frame.project(x_m, z_m)
    return rp.UnitFix(
        unit_id="tester",
        label=_callsign(None),
        along_m=along_m,
        lateral_m=lateral_m,
        heading_err_deg=heading_err,
        alt_m=alt_m,
        height_m=height_m,
        speed_mps=speed_mps,
        x_m=x_m,
        z_m=z_m,
        own=True,
    )


def zones_containing(
    airport: dict[str, Any] | None,
    fix: rp.UnitFix,
    *,
    settled: bool = False,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for zone in rp.zones(airport):
        if rp.zone_admits(zone, fix, settled=settled):
            hits.append(zone)
    return hits


def _primary_owner(triggers: list[str], agl_ft: float | None) -> str:
    airborne = [t for t in AIRBORNE_TRIGGERS if t in triggers]
    field = [t for t in FIELD_TRIGGERS if t in triggers]
    high = agl_ft is not None and agl_ft > 1500.0
    if high and airborne:
        return airborne[0]
    if field:
        return field[-1]
    if airborne:
        return airborne[0]
    return ""


def _phrase_for(
    *,
    airport: dict[str, Any],
    channel: str,
    template: str,
    callsign: str,
    weather: atc_phrase.Weather,
    runway: str,
    opus: atc_phrase.OpusFlightContext,
    step: dict[str, Any] | None,
    mission: dict[str, Any],
    config: dict[str, Any],
) -> str:
    try:
        text, _tx, _freq, _mod = atc_phrase.build_flow_step_phrase(
            airport,
            channel,
            template,
            callsign,
            weather,
            runway,
            custom_text=(step or {}).get("text") if step else None,
            opus=opus,
            step=step,
            mission=mission,
            state=None,
            config=config,
        )
        return str(text or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(phrase failed: {exc})"


class TesterSession:
    """Enter/leave + dwell for one synthetic jet. Reset clears it."""

    def __init__(self) -> None:
        self.inside: set[str] = set()
        self.agencies: set[str] = set()
        self.since: dict[str, float] = {}
        self.fired: set[str] = set()
        self.was_inside: dict[str, bool] = {}
        self.last_phrase: str = ""
        self.last_channel: str = ""

    def reset(self) -> None:
        self.inside.clear()
        self.agencies.clear()
        self.since.clear()
        self.fired.clear()
        self.was_inside.clear()
        self.last_phrase = ""
        self.last_channel = ""


_SESSION = TesterSession()
_LOCK = threading.Lock()


def session() -> TesterSession:
    return _SESSION


def tick(
    body: dict[str, Any],
    *,
    airports: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    now: float | None = None,
    sess: TesterSession | None = None,
) -> dict[str, Any]:
    """
    Evaluate one fake-ownship sample.

    Body: lat, lon, alt_ft_agl, heading_deg, speed_kt, route, airport, runway,
    tune, reset, callsign, metar / wind_dir / wind_speed_kt / altimeter_inhg.
    """
    cfg = dict(config or {})
    if not cfg:
        try:
            cfg = flow_engine.load_app_config()
        except Exception:
            cfg = {}
    data = airports
    if data is None:
        data = atc_phrase.load_json(flow_engine.AIRPORTS_PATH)
    key = str(body.get("airport") or cfg.get("default_airport") or "nellis").strip()
    airport = data.get(key) if isinstance(data, dict) else None
    if not isinstance(airport, dict):
        airport = next((v for v in (data or {}).values() if isinstance(v, dict)), {}) or {}

    if body.get("reset"):
        (sess or _SESSION).reset()

    lat = _num(body.get("lat"))
    lon = _num(body.get("lon"))
    if lat is None or lon is None:
        raise ValueError("lat and lon are required")
    alt_ft = _num(body.get("alt_ft_agl"))
    heading = _num(body.get("heading_deg"))
    speed_kt = _num(body.get("speed_kt"))
    route = str(body.get("route") or "").strip()
    tune = str(body.get("tune") or "").strip().lower()
    tnow = time.time() if now is None else float(now)
    weather = weather_from_body(body)
    mission_hhmm = str(body.get("mission_hhmm") or "").strip()
    minutes = atc_phrase.parse_mission_local_minutes(mission_hhmm)
    want_rwy = str(body.get("runway") or "").strip()
    if want_rwy:
        runway = _runway(airport, want_rwy)
    elif body.get("override_weather"):
        runway = atc_phrase.pick_departure_runway(
            airport,
            weather,
            None,
            cfg,
            local_minutes=minutes,
        )
    else:
        runway = _runway(airport, None)
    runway_hdg = None
    try:
        frame = rp.RunwayFrame.build(runway, rp.runway_geometry(airport, runway))
        if frame is not None:
            runway_hdg = round(float(frame.heading_deg) % 360.0, 1)
    except (TypeError, ValueError, KeyError):
        runway_hdg = None

    catalog = load_fix_catalog(airport, data if isinstance(data, dict) else None)
    plotted = resolve_route(route, catalog)
    plan = agencies.infer_flight(
        route=route or None,
        airport=airport,
        dep_icao=(plotted["tokens"][0] if plotted["tokens"] else None),
        arr_icao=(plotted["tokens"][-1] if plotted["tokens"] else None),
    )
    hop = agencies.format_hop(plan)

    airborne = (alt_ft or 0.0) > 80.0 or (speed_kt or 0.0) > 40.0
    fix = make_unit_fix(
        lat=lat,
        lon=lon,
        alt_ft_agl=alt_ft,
        heading_deg=heading,
        speed_kt=speed_kt,
        airport=airport,
        runway=runway,
    )
    inside_loose = zones_containing(airport, fix, settled=False)
    inside_settled = {id(z) for z in zones_containing(airport, fix, settled=True)}

    inside_rows: list[dict[str, Any]] = []
    triggers: list[str] = []
    for zone in inside_loose:
        trig = str(zone.get("trigger") or "").strip().lower()
        row = {
            "id": str(zone.get("id") or ""),
            "trigger": trig,
            "label": rp.zone_label(zone),
            "settled": id(zone) in inside_settled,
        }
        inside_rows.append(row)
        if trig and trig not in triggers:
            triggers.append(trig)

    agencies_here = [t for t in triggers if t in AGENCY_TRIGGERS]
    owner = _primary_owner(triggers, alt_ft)
    sector = agencies.control_for_ll(airport, lat, lon)
    joshua_nm = agencies.nm_to_agency(airport, "joshua", lat, lon)
    dist_nm = rp.ownship_distance_nm(airport, own_ll=(lat, lon))

    from_ch = tune if tune in AGENCY_TRIGGERS else (owner or "departure")
    next_handoff = agencies.handoff_from_plan(
        from_ch, plan, airport=airport, lat=lat, lon=lon
    )

    callsign = _callsign(cfg, body)
    opus = _opus_for_route(callsign, route or None)
    alt_filed = str(body.get("fp_altitude") or "").strip()
    if alt_filed:
        opus.fp_altitude = alt_filed
    mission = _load_mission(cfg)

    armed: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    state = sess if sess is not None else _SESSION

    current_ids = {str(z.get("id") or z.get("trigger") or i) for i, z in enumerate(inside_loose)}
    entered = current_ids - state.inside
    left = state.inside - current_ids
    for zid in sorted(entered):
        match = next((r for r in inside_rows if (r["id"] or r["trigger"]) == zid), None)
        label = (match or {}).get("label") or zid
        events.append({"kind": "enter", "label": label, "trigger": (match or {}).get("trigger") or ""})
    for zid in sorted(left):
        events.append({"kind": "leave", "label": zid, "trigger": ""})

    current_agencies = set(agencies_here)
    new_agencies = current_agencies - state.agencies
    for ch in sorted(new_agencies):
        tmpl = CHECKIN_TEMPLATES.get(ch, "radio_check")
        phrase = _phrase_for(
            airport=airport,
            channel=ch,
            template=tmpl,
            callsign=callsign,
            weather=weather,
            runway=runway,
            opus=opus,
            step=None,
            mission=mission,
            config=cfg,
        )
        events.append(
            {
                "kind": "agency",
                "channel": ch,
                "label": agencies.spoken_name(ch, str(airport.get("name") or "")),
                "phrase": phrase,
            }
        )

    for step in flow_engine.enabled_steps(mission):
        trigger = rp.step_trigger(step)
        if trigger is None or not trigger.enabled(cfg):
            continue
        sid = str(step.get("id") or step.get("template") or "")
        zone_ok = True
        if trigger.zone:
            want = trigger.zone.strip().lower()
            hits = [
                z
                for z in inside_loose
                if str(z.get("trigger") or "").strip().lower() == want
                or str(z.get("id") or "").strip().lower() == want
            ]
            if trigger.settled:
                hits = [z for z in hits if id(z) in inside_settled]
            zone_ok = bool(hits)
        dist_ok, waiting = rp.within_nm_held(trigger, dist_nm)
        if trigger.within_nm is None:
            dist_ok = True
            waiting = ""
        held_now = zone_ok and dist_ok
        if trigger.when == "leaving":
            was = bool(state.was_inside.get(sid))
            held_now = was and not zone_ok
            state.was_inside[sid] = zone_ok
        else:
            state.was_inside[sid] = zone_ok

        dwell_need = trigger.dwell(cfg)
        if held_now:
            started = state.since.get(sid)
            if started is None:
                state.since[sid] = tnow
                started = tnow
            held_s = max(0.0, tnow - started)
            dwell_ok = held_s + 1e-6 >= dwell_need
            wait_dwell = "" if dwell_ok else f"dwell {held_s:.1f}/{dwell_need:g}s"
        else:
            state.since.pop(sid, None)
            dwell_ok = False
            wait_dwell = waiting or ("not in zone" if trigger.zone else "")
            held_s = 0.0

        phrase = ""
        ch = str(step.get("channel") or "other")
        tmpl = str(step.get("template") or "radio_check")
        if dwell_ok:
            phrase = _phrase_for(
                airport=airport,
                channel=ch,
                template=tmpl,
                callsign=callsign,
                weather=weather,
                runway=runway,
                opus=opus,
                step=step,
                mission=mission,
                config=cfg,
            )
            if sid not in state.fired:
                state.fired.add(sid)
                events.append(
                    {
                        "kind": "armed",
                        "id": sid,
                        "label": str(step.get("label") or tmpl),
                        "channel": ch,
                        "phrase": phrase,
                    }
                )
                state.last_phrase = phrase
                state.last_channel = ch
        armed.append(
            {
                "id": sid,
                "label": str(step.get("label") or tmpl),
                "channel": ch,
                "trigger": trigger.describe(),
                "held": held_now,
                "armed": dwell_ok,
                "waiting": wait_dwell,
                "held_s": round(held_s, 2),
                "phrase": phrase,
            }
        )

    tune_preview = ""
    if tune:
        tmpl = CHECKIN_TEMPLATES.get(tune, "radio_check")
        tune_preview = _phrase_for(
            airport=airport,
            channel=tune,
            template=tmpl,
            callsign=callsign,
            weather=weather,
            runway=runway,
            opus=opus,
            step=None,
            mission=mission,
            config=cfg,
        )

    handoff_preview = ""
    if next_handoff:
        from_tmpl = HANDOFF_TEMPLATES.get(from_ch, "")
        if from_tmpl:
            handoff_preview = _phrase_for(
                airport=airport,
                channel=from_ch,
                template=from_tmpl,
                callsign=callsign,
                weather=weather,
                runway=runway,
                opus=opus,
                step=None,
                mission=mission,
                config=cfg,
            )

    state.inside = current_ids
    state.agencies = current_agencies

    if body.get("drive_fly"):
        atc_phrase.write_ownship_inject(
            lat=lat,
            lon=lon,
            alt_ft_agl=alt_ft,
            heading_deg=heading,
            speed_kt=speed_kt,
            callsign=callsign,
            airport=airport,
            fp_route_string=route or None,
            fp_altitude=alt_filed or None,
        )
    elif "drive_fly" in body:
        atc_phrase.clear_ownship_inject()

    if body.get("override_weather"):
        atc_phrase.write_weather_inject(
            wind_dir=weather.wind_dir,
            wind_speed_kt=weather.wind_speed_kt,
            altimeter_inhg=weather.altimeter_inhg,
            visibility_sm=weather.visibility_sm,
            ceiling_ft=weather.ceiling_ft,
            metar=weather.raw or "",
            mission_hhmm=mission_hhmm,
        )
    elif "override_weather" in body:
        atc_phrase.clear_weather_inject()

    if "traffic" in body:
        raw_traffic = body.get("traffic")
        if isinstance(raw_traffic, list) and raw_traffic:
            atc_phrase.write_traffic_inject(raw_traffic, airport=airport)
        else:
            atc_phrase.clear_traffic_inject()

    return {
        "ok": True,
        "airport": key,
        "lat": lat,
        "lon": lon,
        "alt_ft_agl": alt_ft,
        "heading_deg": heading,
        "speed_kt": speed_kt,
        "runway": runway,
        "runway_hdg": runway_hdg,
        "callsign": callsign,
        "driving_fly": bool(body.get("drive_fly")),
        "airborne": airborne,
        "route": plotted,
        "hop": hop,
        "plan_areas": sorted(plan.areas),
        "inside": inside_rows,
        "agencies": agencies_here,
        "owner": owner,
        "owner_spoken": agencies.spoken_name(owner, str(airport.get("name") or "")) if owner else "",
        "sector": sector,
        "sector_spoken": agencies.spoken_name(sector, str(airport.get("name") or "")),
        "joshua_nm": None if joshua_nm is None else round(float(joshua_nm), 1),
        "field_nm": None if dist_nm is None else round(float(dist_nm), 1),
        "near_joshua": agencies.approaching_joshua(airport, lat, lon),
        "next_handoff": next_handoff or "",
        "next_handoff_spoken": (
            agencies.spoken_name(next_handoff, str(airport.get("name") or ""))
            if next_handoff
            else ""
        ),
        "armed": armed,
        "tune": tune,
        "tune_preview": tune_preview,
        "handoff_preview": handoff_preview,
        "events": events,
        "tunes": list(TUNE_CHANNELS),
        "weather": weather_to_dict(weather),
        "override_weather": bool(body.get("override_weather")),
        "mission_hhmm": mission_hhmm,
    }


def tester_state_fixes(catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in catalog.values():
        ident = str(row.get("id") or "")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        out.append(
            {
                "id": ident,
                "lat": row["lat"],
                "lon": row["lon"],
                "say": row.get("say") or ident,
                "name": row.get("name") or ident,
                "kind": row.get("kind") or _nav_kind(ident),
                "source": row.get("source") or "",
            }
        )
    out.sort(key=lambda r: r["id"])
    return out


def tester_state(
    airport_key: str = "",
    *,
    airports: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bootstrap payload for the tester page (zones + hop + catalog)."""
    cfg = dict(config or {})
    if not cfg:
        try:
            cfg = flow_engine.load_app_config()
        except Exception:
            cfg = {}
    data = airports
    if data is None:
        data = atc_phrase.load_json(flow_engine.AIRPORTS_PATH)
    key = airport_key or str(cfg.get("default_airport") or "") or "nellis"
    airport = data.get(key) if isinstance(data, dict) else None
    if not isinstance(airport, dict):
        key = next(iter(data), "nellis") if isinstance(data, dict) else "nellis"
        airport = data.get(key) if isinstance(data, dict) else {}
        airport = airport if isinstance(airport, dict) else {}
    try:
        import zone_geo

        geojson = zone_geo.geometry_to_geojson(airport)
        keys = [
            {
                "key": k,
                "name": str((v or {}).get("name") or k),
                "icao": str((v or {}).get("icao") or ""),
            }
            for k, v in (data or {}).items()
            if isinstance(v, dict)
        ]
    except Exception:
        geojson = {"type": "FeatureCollection", "features": []}
        keys = [
            {
                "key": key,
                "name": str(airport.get("name") or key),
                "icao": str(airport.get("icao") or ""),
            }
        ]
    field = rp.airport_field_latlon(airport) or (36.236, -115.034)
    lat, lon = field
    catalog = load_fix_catalog(airport, data if isinstance(data, dict) else None)
    default_route = "KLSV FLEX DREAM SARAH KLSV"
    plotted = resolve_route(default_route, catalog)
    return {
        "ok": True,
        "airports": keys,
        "airport": key,
        "name": str(airport.get("name") or key),
        "icao": str(airport.get("icao") or ""),
        "centre": [lat, lon],
        "runways": [
            atc_phrase.normalize_runway(r) or str(r)
            for r in (airport.get("runways") or [])
        ],
        "geojson": geojson,
        "tunes": list(TUNE_CHANNELS),
        "callsign": _callsign(cfg),
        "default_route": default_route,
        "route": plotted,
        "hop": agencies.format_hop(
            agencies.infer_flight(route=default_route, airport=airport)
        ),
        "fixes": tester_state_fixes(catalog),
        "weather": weather_to_dict(weather_from_body(None)),
    }


def opus_flight_rows(
    config: dict[str, Any] | None = None,
    *,
    include_detail: bool = False,
    flight_id: int | None = None,
) -> dict[str, Any]:
    """Compact Opus flight list for the tester picker.

    Default is the list endpoint only (fast). Pass flight_id for one row with
    route / altitude / crew.
    """
    cfg = dict(config or {})
    if not cfg:
        try:
            cfg = flow_engine.load_app_config()
        except Exception:
            cfg = {}
    try:
        if flight_id is not None:
            row = atc_phrase.fetch_opus_flight_detail(cfg, int(flight_id))
            rows = [row]
        else:
            rows = atc_phrase.list_opus_flights(cfg, include_detail=include_detail)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "flights": []}
    flights: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        flights.append(
            {
                "id": row.get("id"),
                "callsign": str(row.get("callsign") or "").strip(),
                "route": str(row.get("fp_route_string") or "").strip(),
                "altitude": str(row.get("fp_altitude") or "").strip(),
                "mission": str(row.get("mission") or row.get("mission_number") or "").strip(),
                "has_filed_plan": bool(row.get("has_filed_plan")),
            }
        )
    return {"ok": True, "flights": flights}


def live_metar(
    airport_key: str = "",
    *,
    airports: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(config or {})
    if not cfg:
        try:
            cfg = flow_engine.load_app_config()
        except Exception:
            cfg = {}
    data = airports or atc_phrase.load_json(flow_engine.AIRPORTS_PATH)
    key = airport_key or str(cfg.get("default_airport") or "nellis")
    airport = data.get(key) if isinstance(data, dict) else None
    icao = str((airport or {}).get("icao") or "KLSV")
    try:
        wx = atc_phrase.fetch_metar(cfg, icao, skip_inject=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "icao": icao}
    out = weather_to_dict(wx)
    out["ok"] = True
    out["icao"] = icao
    return out


def hear_wav_b64(
    config: dict[str, Any],
    text: str,
    *,
    channel: str = "",
) -> tuple[str, str]:
    """Render TTS to a WAV and return (base64, mime)."""
    phrase = (text or "").strip()
    if not phrase:
        raise ValueError("nothing to speak")
    path = atc_phrase.synthesize_tts_wav(config, phrase, channel=channel or None)
    try:
        raw = path.read_bytes()
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    import base64

    return base64.b64encode(raw).decode("ascii"), "audio/wav"


def _reply_for_match(
    match: Any,
    *,
    airport: dict[str, Any],
    weather: atc_phrase.Weather,
    callsign: str,
    opus: atc_phrase.OpusFlightContext,
    mission: dict[str, Any],
    config: dict[str, Any],
    runway: str,
    channel: str,
    sess: TesterSession,
) -> tuple[str, str]:
    import voice_actions

    intent = str(getattr(match, "intent", "") or "")
    slots = getattr(match, "slots", None) or {}
    ch = str(slots.get("channel") or channel or "").strip().lower()
    if intent == "say_again":
        return sess.last_phrase, sess.last_channel or ch
    if intent == "request_winds":
        return voice_actions.build_winds_reply(airport, callsign, weather), ch
    if intent == "request_altimeter":
        return voice_actions.build_altimeter_reply(airport, callsign, weather), ch
    tmpl = str(getattr(match, "template", "") or "").strip()
    step = None
    sid = str(getattr(match, "step_id", "") or "").strip()
    if sid:
        for row in flow_engine.enabled_steps(mission):
            if str(row.get("id") or "") == sid:
                step = row
                tmpl = str(row.get("template") or tmpl)
                ch = str(row.get("channel") or ch)
                break
    if not tmpl:
        tmpl = CHECKIN_TEMPLATES.get(ch, "radio_check")
    if not ch:
        ch = "other"
    text = _phrase_for(
        airport=airport,
        channel=ch,
        template=tmpl,
        callsign=callsign,
        weather=weather,
        runway=runway,
        opus=opus,
        step=step,
        mission=mission,
        config=config,
    )
    return text, ch


def handle_say(
    body: dict[str, Any],
    *,
    airports: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    sess: TesterSession | None = None,
) -> dict[str, Any]:
    """
    Run a typed/spoken radio call through the same grammar as Fly.

    Does not advance the real Fly cursor or transmit on SRS.
    """
    import voice_intent

    cfg = dict(config or {})
    if not cfg:
        try:
            cfg = flow_engine.load_app_config()
        except Exception:
            cfg = {}
    state = sess if sess is not None else _SESSION
    tick_out = tick(body, airports=airports, config=cfg, sess=state)
    transcript = str(body.get("transcript") or "").strip()
    if not transcript:
        return {
            "ok": False,
            "error": "nothing to say — type a radio call or hold to talk",
            "tick": tick_out,
        }
    data = airports or atc_phrase.load_json(flow_engine.AIRPORTS_PATH)
    key = str(tick_out.get("airport") or "nellis")
    airport = data.get(key) if isinstance(data, dict) else {}
    if not isinstance(airport, dict):
        airport = {}
    weather = weather_from_body(body)
    callsign = _callsign(cfg, body)
    opus = _opus_for_route(callsign, str(body.get("route") or ""))
    mission = _load_mission(cfg)
    runway = str(tick_out.get("runway") or _runway(airport, None))
    tune = str(body.get("tune") or tick_out.get("owner") or "")
    phase = "departure"
    if tick_out.get("airborne"):
        phase = "flight"
    if (tick_out.get("owner") or "") == "approach" or tune == "approach":
        phase = "approach"
    expected = ""
    for step in flow_engine.enabled_steps(mission):
        ch = str(step.get("channel") or "")
        if tune and ch == tune:
            expected = str(step.get("template") or "")
            break
        if not expected:
            expected = str(step.get("template") or "")
    evaluation = voice_intent.evaluate(
        transcript,
        channel=tune,
        phase=phase,
        expected=expected,
        callsign=callsign,
        runways=list(airport.get("runways") or []),
        min_confidence=float(cfg.get("voice_min_confidence") or 0.6),
        require_address=bool(cfg.get("voice_require_address", True)),
        steps=flow_engine.enabled_steps(mission),
        last_tx_text=state.last_phrase,
        last_tx_channel=state.last_channel,
    )
    phrase = ""
    channel = tune
    if evaluation.match is not None:
        phrase, channel = _reply_for_match(
            evaluation.match,
            airport=airport,
            weather=weather,
            callsign=callsign,
            opus=opus,
            mission=mission,
            config=cfg,
            runway=runway,
            channel=tune,
            sess=state,
        )
        if phrase:
            state.last_phrase = phrase
            state.last_channel = channel
    wav_b64 = ""
    mime = ""
    hear_err = ""
    if phrase and body.get("hear", True):
        try:
            wav_b64, mime = hear_wav_b64(cfg, phrase, channel=channel)
        except Exception as exc:  # noqa: BLE001
            hear_err = str(exc)
    return {
        "ok": True,
        "transcript": transcript,
        "fired": evaluation.fired,
        "intent": evaluation.match.intent if evaluation.match else "",
        "reason": evaluation.describe(),
        "advice": evaluation.advice,
        "phrase": phrase,
        "channel": channel,
        "wav_b64": wav_b64,
        "mime": mime,
        "hear_error": hear_err,
        "tick": tick_out,
    }
