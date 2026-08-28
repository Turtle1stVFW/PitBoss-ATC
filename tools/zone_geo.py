"""
Shapes drawn on a map → runway geometry in atc/airports.json.

Internal tooling. The flow app never imports this; it only reads the geometry
this writes.

Two drawing paths land here:
  * the Leaflet editor (`zone_server.py`), which POSTs GeoJSON
  * Google Earth, which saves KML/KMZ

Both give lat/lon, which is exactly what `runway_position.point_xz` accepts, so
nothing here has to know about DCS metres or fit a projection — the map already
knows where every pixel is.

What a shape means is taken from its tags. GeoJSON carries them as properties;
Google Earth has no property editor, so names are read instead:

    "in_position 21R"      → trigger in_position, runway 21R
    "eor:21R:NW EOR"       → trigger eor, runway 21R, label "NW EOR"
    "21R hold short"       → trigger hold_short, runway 21R

A LineString tagged `runway` is the centreline: first point is the threshold,
last is the far end, and the reciprocal direction is filled in from the same
line reversed, since it is the same strip of concrete.
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent
ATC_DIR = HERE.parent / "atc"
AIRPORTS_JSON = ATC_DIR / "airports.json"

if str(ATC_DIR) not in sys.path:
    sys.path.insert(0, str(ATC_DIR))

import atc_phrase  # noqa: E402  (needs the path above)

# Nellis reference point — the CAOC x/z origin, and a sane place to open the map
# when an airport has no geometry yet.
FALLBACK_CENTRE = (36.2362, -115.0343)

# Triggers the editor offers as presets. `runway` is the centreline rather than an
# area; the rest are zones a step can watch. This list is convenience only — a
# step's `trigger.zone` is matched as a plain string, so a name typed into the
# editor's "custom" box works without anything here changing.
TRIGGERS: tuple[str, ...] = (
    "runway",
    "in_position",
    "eor",
    "hold_short",
    "parking",
    # Agency areas: entering one is the cue to talk to that agency.
    "delivery",
    "ground",
    "tower",
    "departure",
    "approach",
    "joshua",
    "control_east",
    "control_west",
    "blackjack",
    "center",
    "other",
)

# Longest phrasings first so "end of runway" is not read as "runway".
_TRIGGER_WORDS: tuple[tuple[str, str], ...] = (
    ("in_position", "in_position"),
    ("in position", "in_position"),
    ("inposition", "in_position"),
    ("line up", "in_position"),
    ("lineup", "in_position"),
    ("luaw", "in_position"),
    ("takeoff position", "in_position"),
    ("end of runway", "eor"),
    ("hold_short", "hold_short"),
    ("hold short", "hold_short"),
    ("holdshort", "hold_short"),
    ("arming", "eor"),
    ("control east", "control_east"),
    ("control_east", "control_east"),
    ("control west", "control_west"),
    ("control_west", "control_west"),
    ("lee corridor", "control_west"),
    ("blackjack", "blackjack"),
    ("joshua", "joshua"),
    ("sally", "control_east"),
    ("eor", "eor"),
    ("parking", "parking"),
    ("ramp", "parking"),
    ("apron", "parking"),
    ("centreline", "runway"),
    ("centerline", "runway"),
    ("runway", "runway"),
    ("rwy", "runway"),
)

_RUNWAY_IN_TEXT = re.compile(r"(?<![0-9])([0-3]?[0-9])\s*([LRC])?(?![0-9A-Za-z])")
_RUNWAY_WORD = re.compile(r"\b(?:rwy|runway)\b", re.I)


@dataclass
class Shape:
    """One thing someone drew, before it becomes airport geometry."""

    kind: str  # polygon | circle | line
    points: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon)
    radius_m: float | None = None
    name: str = ""
    trigger: str = ""
    runway: str = ""
    source: str = ""
    # Optional altitude band, feet AGL. A drawn area is otherwise infinitely tall,
    # so overflying the field would count as being on the ramp.
    min_alt_ft: float | None = None
    max_alt_ft: float | None = None
    # Stable id from the editor when re-saving; empty means mint one.
    id: str = ""
    # Locked zones are shipped defaults — the editor blocks reshape/delete until
    # someone unlocks them on purpose.
    locked: bool = False

    @property
    def label(self) -> str:
        return self.name or f"{self.trigger or 'zone'} {self.runway}".strip()

    @property
    def band(self) -> str:
        if self.min_alt_ft is None and self.max_alt_ft is None:
            return ""
        lo = "surface" if not self.min_alt_ft else f"{self.min_alt_ft:.0f}"
        hi = "unlimited" if self.max_alt_ft is None else f"{self.max_alt_ft:.0f}"
        return f"{lo}-{hi} ft AGL"


# --- tags -----------------------------------------------------------------


def _expand_sides(text: str) -> str:
    text = re.sub(r"\bleft\b", "L", text, flags=re.I)
    text = re.sub(r"\bright\b", "R", text, flags=re.I)
    return re.sub(r"\bcent(?:er|re)\b", "C", text, flags=re.I)


def normalize_runway(raw: Any) -> str:
    """
    A runway someone stated outright: '3l' / '21 Right' / 'RWY 21R' → '03L' / '21R'.

    Lenient, because the caller already said this value is a runway — a bare '09'
    at a single-runway field is taken at face value.
    """
    text = _expand_sides(_RUNWAY_WORD.sub(" ", str(raw or "").strip()))
    if not text.strip():
        return ""
    for num, side in _RUNWAY_IN_TEXT.findall(text):
        if 1 <= int(num) <= 36:
            return atc_phrase.normalize_runway(f"{int(num):02d}{side or ''}") or ""
    return ""


def runway_in_text(raw: Any) -> str:
    """
    A runway found inside a free-text name, or '' if it is not clearly one.

    Stricter than `normalize_runway`: a bare number only counts when it carries a
    side letter or the text says runway, so "Row 18 parking" stays a parking area
    rather than becoming geometry scoped to a runway 18 that does not exist.
    """
    text = str(raw or "")
    spelled_out = _RUNWAY_WORD.search(text) is not None
    for num, side in _RUNWAY_IN_TEXT.findall(_expand_sides(_RUNWAY_WORD.sub(" ", text))):
        if not 1 <= int(num) <= 36 or not (side or spelled_out):
            continue
        return atc_phrase.normalize_runway(f"{int(num):02d}{side or ''}") or ""
    return ""


def reciprocal_runway(runway: str) -> str:
    """'21R' → '03L'. The other way to use the same concrete."""
    norm = normalize_runway(runway)
    m = re.fullmatch(r"(\d{2})([LRC])?", norm)
    if not m:
        return ""
    num = (int(m.group(1)) + 18 - 1) % 36 + 1
    side = {"L": "R", "R": "L"}.get(m.group(2) or "", m.group(2) or "")
    return f"{num:02d}{side}"


def infer_tags(name: str) -> tuple[str, str, str]:
    """
    (trigger, runway, label) read out of a placemark name.

    Accepts the explicit `trigger:runway:label` form and also plain English, so a
    Google Earth polygon called "21R in position" needs no renaming.
    """
    raw = str(name or "").strip()
    if not raw:
        return "", "", ""
    parts = [p.strip() for p in raw.split(":")]
    if len(parts) >= 2 and _match_trigger(parts[0]):
        trigger = _match_trigger(parts[0])
        return trigger, normalize_runway(parts[1]), ":".join(parts[2:]).strip()
    return _match_trigger(raw), runway_in_text(raw), raw


def _match_trigger(text: str) -> str:
    low = str(text or "").casefold()
    for word, trigger in _TRIGGER_WORDS:
        if word in low:
            return trigger
    return ""


# --- reading what was drawn ----------------------------------------------


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    text = str(val or "").strip().casefold()
    return text in ("1", "true", "yes", "y", "on", "locked")


def _opt_float(val: Any) -> float | None:
    """A number if there is one; None for blank, missing or nonsense."""
    if val is None or (isinstance(val, str) and not val.strip()):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _coord_pair(seq: Any) -> tuple[float, float] | None:
    """GeoJSON stores [lon, lat]; we keep (lat, lon)."""
    try:
        lon, lat = float(seq[0]), float(seq[1])
    except (TypeError, ValueError, IndexError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _open_ring(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """A closed ring repeats its first point; the zone model does not want that."""
    if len(pts) > 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _ring(coords: Any) -> list[tuple[float, float]]:
    return _open_ring([p for p in (_coord_pair(c) for c in coords or []) if p])


def parse_geojson(text: str | bytes) -> list[Shape]:
    data = json.loads(text)
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        feats = [data] if isinstance(data, dict) else []
    out: list[Shape] = []
    for feat in feats:
        out.extend(_shapes_from_feature(feat))
    return out


def _shapes_from_feature(feat: Any) -> list[Shape]:
    if not isinstance(feat, dict):
        return []
    geom = feat.get("geometry") if feat.get("type") == "Feature" else feat
    if not isinstance(geom, dict):
        return []
    props = feat.get("properties") if isinstance(feat.get("properties"), dict) else {}
    name = str(props.get("name") or props.get("Name") or "").strip()
    guess_trigger, guess_runway, guess_label = infer_tags(name)
    # An explicit property wins as written, so a trigger this tool has never heard
    # of still round-trips.
    tagged = str(props.get("trigger") or "").strip().casefold().replace(" ", "_")
    trigger = tagged or guess_trigger
    runway = normalize_runway(props.get("runway")) or guess_runway
    label = str(props.get("label") or "").strip() or guess_label or name
    radius = props.get("radius_m", props.get("radius"))
    try:
        radius_m = float(radius) if radius is not None else None
    except (TypeError, ValueError):
        radius_m = None

    gtype = str(geom.get("type") or "")
    coords = geom.get("coordinates")
    common = {
        "name": label,
        "trigger": trigger,
        "runway": runway,
        "source": "geojson",
        "min_alt_ft": _opt_float(props.get("min_alt_ft")),
        "max_alt_ft": _opt_float(props.get("max_alt_ft")),
        "id": str(props.get("id") or "").strip(),
        "locked": _truthy(props.get("locked")),
    }

    if gtype == "Polygon":
        rings = coords if isinstance(coords, list) else []
        pts = _ring(rings[0] if rings else [])
        return [Shape(kind="polygon", points=pts, **common)] if len(pts) >= 3 else []
    if gtype == "MultiPolygon":
        out = []
        for poly in coords or []:
            pts = _ring((poly or [None])[0])
            if len(pts) >= 3:
                out.append(Shape(kind="polygon", points=pts, **common))
        return out
    if gtype in ("LineString", "MultiLineString"):
        lines = [coords] if gtype == "LineString" else (coords or [])
        out = []
        for line in lines:
            pts = [p for p in (_coord_pair(c) for c in line or []) if p]
            if len(pts) >= 2:
                out.append(Shape(kind="line", points=pts, **common))
        return out
    if gtype == "Point":
        pt = _coord_pair(coords)
        if pt is None:
            return []
        # A point is only meaningful as a zone if it carries a radius.
        return [Shape(kind="circle", points=[pt], radius_m=radius_m or 0.0, **common)]
    if gtype == "GeometryCollection":
        out = []
        for sub in geom.get("geometries") or []:
            out.extend(_shapes_from_feature({"geometry": sub, "properties": props}))
        return out
    return []


def parse_kml(payload: str | bytes) -> list[Shape]:
    """Google Earth save-as. Accepts KML text or KMZ bytes."""
    if isinstance(payload, bytes) and payload[:2] == b"PK":
        payload = _kml_from_kmz(payload)
    root = ElementTree.fromstring(payload)
    out: list[Shape] = []
    for placemark in root.iter():
        if _tag(placemark) != "Placemark":
            continue
        out.extend(_shapes_from_placemark(placemark))
    return out


def _kml_from_kmz(blob: bytes) -> bytes:
    import io

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
        if not names:
            raise ValueError("KMZ contains no .kml")
        # doc.kml is the entry point when there are several.
        names.sort(key=lambda n: (Path(n).name.lower() != "doc.kml", n))
        return zf.read(names[0])


def _tag(el: Any) -> str:
    return str(el.tag).rsplit("}", 1)[-1]


def _find_all(el: Any, name: str) -> list[Any]:
    return [child for child in el.iter() if _tag(child) == name]


def _text(el: Any, name: str) -> str:
    for child in el.iter():
        if _tag(child) == name:
            return (child.text or "").strip()
    return ""


def _kml_coords(text: str) -> list[tuple[float, float]]:
    out = []
    for chunk in re.split(r"\s+", (text or "").strip()):
        if not chunk:
            continue
        parts = chunk.split(",")
        pt = _coord_pair(parts) if len(parts) >= 2 else None
        if pt:
            out.append(pt)
    return out


def _shapes_from_placemark(pm: Any) -> list[Shape]:
    name = _text(pm, "name")
    trigger, runway, label = infer_tags(name)
    extended: dict[str, str] = {}
    for data in _find_all(pm, "Data"):
        key = str(data.get("name") or "").strip().casefold()
        if key:
            extended[key] = _text(data, "value")
    # An ExtendedData trigger is taken as written, so a custom agency name drawn in
    # Google Earth survives even though _match_trigger has never heard of it.
    tagged = str(extended.get("trigger") or "").strip().casefold().replace(" ", "_")
    trigger = tagged or trigger
    runway = normalize_runway(extended.get("runway", "")) or runway
    radius_m = _opt_float(extended.get("radius_m"))
    common = {
        "name": label or name,
        "trigger": trigger,
        "runway": runway,
        "source": "kml",
        "min_alt_ft": _opt_float(extended.get("min_alt_ft")),
        "max_alt_ft": _opt_float(extended.get("max_alt_ft")),
        "id": str(extended.get("id") or "").strip(),
        "locked": _truthy(extended.get("locked")),
    }

    out: list[Shape] = []
    for poly in _find_all(pm, "Polygon"):
        outer = _find_all(poly, "outerBoundaryIs")
        coords = _text(outer[0] if outer else poly, "coordinates")
        pts = _open_ring(_kml_coords(coords))
        if len(pts) >= 3:
            out.append(Shape(kind="polygon", points=pts, **common))
    for line in _find_all(pm, "LineString"):
        pts = _kml_coords(_text(line, "coordinates"))
        if len(pts) >= 2:
            out.append(Shape(kind="line", points=pts, **common))
    if not out:
        for point in _find_all(pm, "Point"):
            pts = _kml_coords(_text(point, "coordinates"))
            if pts:
                out.append(
                    Shape(kind="circle", points=pts[:1], radius_m=radius_m, **common)
                )
    return out


def parse_any(payload: str | bytes, filename: str = "") -> list[Shape]:
    suffix = Path(filename or "").suffix.lower()
    if suffix in (".kml", ".kmz"):
        return parse_kml(payload)
    if suffix in (".json", ".geojson"):
        return parse_geojson(payload)
    head = payload[:400] if isinstance(payload, bytes) else payload[:400].encode()
    if head[:2] == b"PK" or b"<kml" in head or b"<?xml" in head:
        return parse_kml(payload)
    return parse_geojson(payload)


# --- shapes → geometry ----------------------------------------------------


def _ll(pt: tuple[float, float]) -> dict[str, float]:
    return {"lat": round(pt[0], 7), "lon": round(pt[1], 7)}


def zone_id(trigger: str, runway: str, taken: set[str]) -> str:
    base = "-".join(p for p in (trigger or "zone", (runway or "").lower()) if p)
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    taken.add(candidate)
    return candidate


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def shapes_to_geometry(
    shapes: list[Shape],
    known_runways: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """
    (zones, runways, warnings).

    Runway centrelines become `runways[rwy] = {threshold, far_end, length_m}` for
    both directions; everything else becomes a zone. Pass the airport's runway
    list to have mistagged shapes called out.
    """
    zones: list[dict[str, Any]] = []
    runways: dict[str, Any] = {}
    warnings: list[str] = []
    taken: set[str] = set()
    known = {normalize_runway(r) for r in (known_runways or []) if normalize_runway(r)}

    for shape in shapes:
        if shape.runway and known and shape.runway not in known:
            warnings.append(
                f"{shape.label!r} is tagged runway {shape.runway}, "
                f"which this field does not have ({', '.join(sorted(known))})"
            )
        # A takeoff or EOR area with no runway applies to every runway, which is
        # how you get cleared for a runway you are not lined up on.
        if shape.trigger in ("in_position", "eor") and not shape.runway:
            warnings.append(f"{shape.label!r} has no runway, so it applies to all of them")

        trigger = shape.trigger or ("runway" if shape.kind == "line" else "")
        if not trigger:
            warnings.append(f"skipped {shape.label!r}: no trigger tag")
            continue

        if shape.kind == "line" or trigger == "runway":
            # An area tagged as a runway would otherwise turn two of its corners
            # into a threshold and a far end, which is nonsense geometry.
            if shape.kind != "line":
                warnings.append(
                    f"skipped {shape.label!r}: a runway has to be a line, "
                    f"threshold to far end, not a {shape.kind}"
                )
                continue
            if len(shape.points) < 2:
                warnings.append(f"skipped {shape.label!r}: centreline needs two points")
                continue
            if not shape.runway:
                warnings.append(f"skipped centreline {shape.label!r}: no runway tag")
                continue
            thr, far = shape.points[0], shape.points[-1]
            length = round(haversine_m(thr, far), 1)
            runways[shape.runway] = {
                "threshold": _ll(thr),
                "far_end": _ll(far),
                "length_m": length,
            }
            other = reciprocal_runway(shape.runway)
            if other and other not in runways:
                runways[other] = {
                    "threshold": _ll(far),
                    "far_end": _ll(thr),
                    "length_m": length,
                }
            continue

        zid = (shape.id or "").strip()
        if not zid or zid in taken:
            zid = zone_id(trigger, shape.runway, taken)
        else:
            taken.add(zid)
        zone: dict[str, Any] = {
            "id": zid,
            "trigger": trigger,
            "kind": "circle" if shape.kind == "circle" else "polygon",
        }
        if shape.name:
            zone["name"] = shape.name
        if shape.runway:
            zone["runway"] = shape.runway
        if shape.locked:
            zone["locked"] = True
        for key, val in (("min_alt_ft", shape.min_alt_ft), ("max_alt_ft", shape.max_alt_ft)):
            if val is not None:
                zone[key] = round(float(val), 1)
        if (
            shape.min_alt_ft is not None
            and shape.max_alt_ft is not None
            and shape.min_alt_ft >= shape.max_alt_ft
        ):
            warnings.append(
                f"{shape.label!r} has an upside-down altitude band "
                f"({shape.min_alt_ft:.0f} to {shape.max_alt_ft:.0f} ft), so nothing is inside it"
            )
        if shape.kind == "circle":
            if not shape.points or not shape.radius_m:
                warnings.append(f"skipped {shape.label!r}: circle needs a radius")
                continue
            zone["centre"] = _ll(shape.points[0])
            zone["radius_m"] = round(float(shape.radius_m), 1)
        else:
            if len(shape.points) < 3:
                warnings.append(f"skipped {shape.label!r}: polygon needs three points")
                continue
            zone["points"] = [_ll(p) for p in shape.points]
        zones.append(zone)

    return zones, runways, warnings


def shapes_to_geojson(shapes: list[Shape]) -> dict[str, Any]:
    """Parsed shapes back out as drawable features, tags included."""
    feats = []
    for shape in shapes:
        props: dict[str, Any] = {
            "name": shape.name,
            "trigger": shape.trigger,
            "runway": shape.runway,
            "min_alt_ft": shape.min_alt_ft,
            "max_alt_ft": shape.max_alt_ft,
            "locked": bool(shape.locked),
        }
        if shape.id:
            props["id"] = shape.id
        if shape.kind == "circle":
            if not shape.points:
                continue
            props["radius_m"] = shape.radius_m or 0.0
            geom = {
                "type": "Point",
                "coordinates": [shape.points[0][1], shape.points[0][0]],
            }
        elif shape.kind == "line":
            if len(shape.points) < 2:
                continue
            geom = {
                "type": "LineString",
                "coordinates": [[p[1], p[0]] for p in shape.points],
            }
        else:
            if len(shape.points) < 3:
                continue
            ring = [[p[1], p[0]] for p in shape.points]
            ring.append(ring[0])
            geom = {"type": "Polygon", "coordinates": [ring]}
        feats.append({"type": "Feature", "properties": props, "geometry": geom})
    return {"type": "FeatureCollection", "features": feats}


def merge_runways(existing: Any, incoming: dict[str, Any]) -> dict[str, Any]:
    """
    Lay drawn centrelines over what is already there.

    A runway entry carries things no map editor can know — width, the EOR
    fallback point, anything hand-tuned — so incoming keys overwrite rather than
    the whole entry being swapped out.
    """
    if not isinstance(existing, dict):
        return dict(incoming)
    out = dict(existing)
    for rwy, geom in incoming.items():
        prior = out.get(rwy)
        out[rwy] = {**prior, **geom} if isinstance(prior, dict) else geom
    return out


def apply_to_airport(
    airport: dict[str, Any],
    zones: list[dict[str, Any]],
    runways: dict[str, Any],
    *,
    replace: bool = True,
    note: str = "",
) -> dict[str, Any]:
    """Fold drawn geometry into one airport entry, in place, and return it."""
    geo = airport.get("geometry")
    if not isinstance(geo, dict):
        geo = {}
        airport["geometry"] = geo

    if replace:
        geo["zones"] = list(zones)
    else:
        keep = {z.get("id") for z in zones if z.get("id")}
        prior = [
            z
            for z in (geo.get("zones") or [])
            if isinstance(z, dict) and z.get("id") not in keep
        ]
        geo["zones"] = prior + list(zones)
    if runways:
        geo["runways"] = merge_runways(geo.get("runways"), runways)

    geo["calibrated"] = is_usable(geo)
    geo["note"] = note or (
        f"Drawn on a map with tools/zone_server.py, "
        f"{time.strftime('%Y-%m-%d')}. Coordinates are lat/lon."
    )
    return airport


def is_usable(geometry: dict[str, Any]) -> bool:
    """
    True once the geometry can actually drive a clearance.

    That means a takeoff area to sit in and a runway centreline to be aligned
    with — an EOR zone alone only gets you the tower handoff.
    """
    triggers = {
        str(z.get("trigger") or "")
        for z in (geometry.get("zones") or [])
        if isinstance(z, dict)
    }
    runways = geometry.get("runways")
    has_line = isinstance(runways, dict) and any(
        isinstance(v, dict) and v.get("threshold") and v.get("far_end")
        for v in runways.values()
    )
    return "in_position" in triggers and has_line


# --- airports.json --------------------------------------------------------


def load_airports(path: Path | None = None) -> dict[str, Any]:
    # Resolved per call, not bound as a default, so a test can point the whole
    # module at a scratch file and be sure nothing reaches the real one.
    path = path or AIRPORTS_JSON
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not an object")
    return data


def save_airports(data: dict[str, Any], path: Path | None = None) -> Path:
    """Write back with a one-shot backup, matching the file's 2-space style."""
    path = path or AIRPORTS_JSON
    backup = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        backup.write_bytes(path.read_bytes())
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)
    return backup


def airport_centre_ll(airport: dict[str, Any] | None) -> tuple[float, float]:
    """Where to open the map: the field if we know it, Nellis if we do not."""
    pts: list[tuple[float, float]] = []
    geo = (airport or {}).get("geometry")
    geo = geo if isinstance(geo, dict) else {}
    for rwy in (geo.get("runways") or {}).values():
        if not isinstance(rwy, dict):
            continue
        for key in ("threshold", "far_end"):
            pt = _point_ll(rwy.get(key))
            if pt:
                pts.append(pt)
    for zone in geo.get("zones") or []:
        if not isinstance(zone, dict):
            continue
        pt = _point_ll(zone.get("centre") or zone.get("center"))
        if pt:
            pts.append(pt)
        for p in zone.get("points") or []:
            pt = _point_ll(p)
            if pt:
                pts.append(pt)
    if not pts:
        return FALLBACK_CENTRE
    return (
        sum(p[0] for p in pts) / len(pts),
        sum(p[1] for p in pts) / len(pts),
    )


def _rounded(pt: tuple[float, float]) -> tuple[float, float]:
    return round(pt[0], 6), round(pt[1], 6)


def _point_ll(obj: Any) -> tuple[float, float] | None:
    """A stored geometry point as lat/lon, whether it was saved as lat/lon or x/z."""
    if not isinstance(obj, dict):
        return None
    try:
        return float(obj["lat"]), float(obj["lon"])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return atc_phrase.caoc_xz_to_ll(float(obj["x"]), float(obj["z"]))
    except (KeyError, TypeError, ValueError):
        return None


def geometry_to_geojson(airport: dict[str, Any] | None) -> dict[str, Any]:
    """Existing geometry as drawable features, so the editor can reopen it."""
    geo = (airport or {}).get("geometry")
    geo = geo if isinstance(geo, dict) else {}
    feats: list[dict[str, Any]] = []

    drawn: set[frozenset[tuple[float, float]]] = set()
    for rwy, spec in (geo.get("runways") or {}).items():
        if not isinstance(spec, dict):
            continue
        thr = _point_ll(spec.get("threshold"))
        far = _point_ll(spec.get("far_end"))
        if not thr or not far:
            continue
        # 21R and 03L are one line drawn once; showing both would stack two
        # identical, separately editable lines on the same concrete.
        ends = frozenset({_rounded(thr), _rounded(far)})
        if ends in drawn:
            continue
        drawn.add(ends)
        feats.append(
            {
                "type": "Feature",
                "properties": {
                    "id": f"runway-{str(rwy).lower()}",
                    "trigger": "runway",
                    "runway": normalize_runway(rwy),
                    "name": f"Runway {normalize_runway(rwy)}",
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[thr[1], thr[0]], [far[1], far[0]]],
                },
            }
        )

    for zone in geo.get("zones") or []:
        if not isinstance(zone, dict):
            continue
        props = {
            "id": zone.get("id") or "",
            "trigger": str(zone.get("trigger") or ""),
            "runway": normalize_runway(zone.get("runway")),
            "name": str(zone.get("name") or ""),
            "min_alt_ft": _opt_float(zone.get("min_alt_ft")),
            "max_alt_ft": _opt_float(zone.get("max_alt_ft")),
            "locked": _truthy(zone.get("locked")),
        }
        kind = str(zone.get("kind") or "polygon").casefold()
        if kind == "circle":
            centre = _point_ll(zone.get("centre") or zone.get("center"))
            if not centre:
                continue
            props["radius_m"] = zone.get("radius_m")
            feats.append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": {
                        "type": "Point",
                        "coordinates": [centre[1], centre[0]],
                    },
                }
            )
            continue
        pts = [p for p in (_point_ll(p) for p in zone.get("points") or []) if p]
        if len(pts) < 3:
            continue
        ring = [[p[1], p[0]] for p in pts]
        ring.append(ring[0])
        feats.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )

    return {"type": "FeatureCollection", "features": feats}
