"""
Reference overlays for the zone editor (KML/KMZ → GeoJSON).

These are view-only: MOAs, restricted areas, MTRs, targets, etc. They are not
written into airports.json and do not become trigger zones. Drop a file in
tools/overlays/ or upload it from the editor; the first load builds a
.geojson cache next to the source so the next open is instant.
"""

from __future__ import annotations

import json
import zipfile
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent
OVERLAYS_DIR = HERE / "overlays"

# Top-level folders that are useful on by default when first loading NTTR-style packs.
DEFAULT_ON_FOLDERS = frozenset(
    {
        "airspace",
        "special use airspace",
        "military operations areas (moa)",
        "restricted areas",
        "prohibited areas",
        "warning areas",
        "alert areas",
        "danger areas",
        "airfields",
        "nttr targets",
        "bullseye points",
        "r-2508",
    }
)


def ensure_overlays_dir() -> Path:
    OVERLAYS_DIR.mkdir(parents=True, exist_ok=True)
    readme = OVERLAYS_DIR / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Drop KML/KMZ reference packs here (e.g. NTTR.kmz).\n"
            "They appear under Reference overlay in the zone editor.\n"
            "A .geojson cache is built beside each file on first load.\n",
            encoding="utf-8",
        )
    return OVERLAYS_DIR


def list_overlays() -> list[dict[str, Any]]:
    ensure_overlays_dir()
    out: list[dict[str, Any]] = []
    for path in sorted(OVERLAYS_DIR.iterdir()):
        if path.suffix.lower() not in (".kml", ".kmz"):
            continue
        cache = _cache_path(path)
        out.append(
            {
                "id": path.name,
                "name": path.stem,
                "path": str(path),
                "bytes": path.stat().st_size,
                "cached": cache.is_file(),
            }
        )
    return out


def resolve_overlay(name: str) -> Path:
    ensure_overlays_dir()
    raw = (name or "").strip().replace("\\", "/")
    if not raw or "/" in raw or raw in (".", ".."):
        raise ValueError("invalid overlay name")
    path = (OVERLAYS_DIR / Path(raw).name).resolve()
    if not path.is_relative_to(OVERLAYS_DIR.resolve()):
        raise ValueError("overlay outside overlays/")
    if not path.is_file():
        raise ValueError(f"unknown overlay {path.name!r}")
    if path.suffix.lower() not in (".kml", ".kmz"):
        raise ValueError("overlay must be .kml or .kmz")
    return path


def save_upload(filename: str, payload: bytes) -> Path:
    ensure_overlays_dir()
    name = Path(filename or "overlay.kmz").name
    suffix = Path(name).suffix.lower()
    if suffix not in (".kml", ".kmz"):
        name = name + ".kmz"
    dest = OVERLAYS_DIR / name
    dest.write_bytes(payload)
    cache = _cache_path(dest)
    if cache.exists():
        cache.unlink()
    return dest


def ensure_cache(name: str, *, rebuild: bool = False) -> Path:
    """Build or refresh the .geojson cache next to a KML/KMZ and return its path."""
    path = resolve_overlay(name)
    cache = _cache_path(path)
    if not rebuild and cache.is_file() and cache.stat().st_mtime >= path.stat().st_mtime:
        return cache
    data = kml_to_geojson(_read_kml_bytes(path))
    cache.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return cache


def load_overlay(
    name: str, *, rebuild: bool = False, include_features: bool = False
) -> dict[str, Any]:
    """Folder summary for the UI; optionally the FeatureCollection (large packs)."""
    path = resolve_overlay(name)
    cache = ensure_cache(name, rebuild=rebuild)
    data = json.loads(cache.read_text(encoding="utf-8"))

    features = data.get("features") or []
    folders = Counter(
        str((f.get("properties") or {}).get("folder") or "(root)") for f in features
    )
    types = Counter(
        str((f.get("geometry") or {}).get("type") or "?") for f in features
    )
    folder_list = [
        {
            "id": key,
            "count": count,
            "default_on": _default_on(key),
        }
        for key, count in sorted(folders.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    out: dict[str, Any] = {
        "ok": True,
        "id": path.name,
        "name": path.stem,
        "count": len(features),
        "folders": folder_list,
        "types": dict(types),
        "cached": str(cache),
        "geojson_url": f"/api/overlay/geojson?file={path.name}",
    }
    if include_features:
        out["features"] = features
    return out


def kml_to_geojson(payload: str | bytes) -> dict[str, Any]:
    if isinstance(payload, bytes) and payload[:2] == b"PK":
        payload = _kml_from_kmz(payload)
    root = ElementTree.fromstring(payload)
    features: list[dict[str, Any]] = []
    _walk(root, [], features)
    return {"type": "FeatureCollection", "features": features}


# --- internals --------------------------------------------------------------


def _cache_path(source: Path) -> Path:
    return source.with_suffix(source.suffix + ".geojson")


def _default_on(folder: str) -> bool:
    top = (folder or "").split("/", 1)[0].strip().casefold()
    return top in DEFAULT_ON_FOLDERS


def _read_kml_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix.lower() == ".kmz" or data[:2] == b"PK":
        return _kml_from_kmz(data)
    return data


def _kml_from_kmz(blob: bytes) -> bytes:
    with zipfile.ZipFile(BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
        if not names:
            raise ValueError("KMZ contains no .kml")
        names.sort(key=lambda n: (Path(n).name.lower() != "doc.kml", n))
        return zf.read(names[0])


def _tag(el: Any) -> str:
    return str(el.tag).rsplit("}", 1)[-1]


def _child_text(el: Any, name: str) -> str:
    for child in el:
        if _tag(child) == name:
            return (child.text or "").strip()
    return ""


def _coords(text: str) -> list[list[float]]:
    out: list[list[float]] = []
    for chunk in (text or "").split():
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            out.append([float(parts[0]), float(parts[1])])
        except ValueError:
            continue
    return out


def _walk(el: Any, folders: list[str], features: list[dict[str, Any]]) -> None:
    kind = _tag(el)
    if kind == "Folder":
        name = _child_text(el, "name")
        nested = folders + ([name] if name else [])
        for child in el:
            _walk(child, nested, features)
        return
    if kind == "Document":
        for child in el:
            _walk(child, folders, features)
        return
    if kind == "Placemark":
        features.extend(_features_from_placemark(el, folders))
        return
    for child in el:
        _walk(child, folders, features)


def _features_from_placemark(pm: Any, folders: list[str]) -> list[dict[str, Any]]:
    name = _child_text(pm, "name")
    folder = "/".join(folders) if folders else "(root)"
    geoms: list[tuple[str, Any]] = []

    for node in pm.iter():
        t = _tag(node)
        if t == "Polygon":
            ring = _outer_ring(node)
            if ring:
                geoms.append(("Polygon", [ring]))
        elif t == "LineString":
            line = _coords(_coords_text(node))
            if len(line) >= 2:
                geoms.append(("LineString", line))
        elif t == "Point":
            pts = _coords(_coords_text(node))
            if pts:
                geoms.append(("Point", pts[0]))

    kinds = {g[0] for g in geoms}
    if "Polygon" in kinds or "LineString" in kinds:
        geoms = [g for g in geoms if g[0] in ("Polygon", "LineString")]

    out: list[dict[str, Any]] = []
    for gtype, coordinates in geoms:
        out.append(
            {
                "type": "Feature",
                "properties": {"name": name, "folder": folder},
                "geometry": {"type": gtype, "coordinates": coordinates},
            }
        )
    return out


def _coords_text(el: Any) -> str:
    for child in el.iter():
        if _tag(child) == "coordinates":
            return child.text or ""
    return ""


def _outer_ring(poly: Any) -> list[list[float]] | None:
    for child in poly.iter():
        if _tag(child) != "outerBoundaryIs":
            continue
        ring = _coords(_coords_text(child))
        if len(ring) < 3:
            return None
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        return ring
    # Some exports put coordinates directly under Polygon/LinearRing.
    ring = _coords(_coords_text(poly))
    if len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring
