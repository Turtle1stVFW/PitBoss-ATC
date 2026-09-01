#!/usr/bin/env python3
"""
Refresh NTTR agency polygons in atc/airports.json from FAA AIS Special Use Airspace.

    py -3 tools/import_nttr_agencies.py           # cached GeoJSON if present, else fetch
    py -3 tools/import_nttr_agencies.py --fetch   # always re-query FAA
    py -3 tools/export_agency_airspace.py         # write tools/agency_airspace.kml

Sally (Control East, IFG ch 7 / 317.525) = Desert MOA + R-4806E + Reveille.
Lee (Control West, IFG ch 8 / 254.400) = R-4807A/B + R-4808S + R-4809 + an
inferred Lee Corridor (NELLISAFBI 11-250; not a published SUA).
Blackjack copies the working restricted areas so range GCI and NATCF can overlap.

Not a substitute for the IFG chart.
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
GEOJSON = HERE / "_nttr_sua.geojson"
AIRPORTS = HERE.parent / "atc" / "airports.json"

FAA_URL = (
    "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
    "Special_Use_Airspace/FeatureServer/0/query"
)
FAA_WHERE = (
    "NAME IN ('DESERT MOA','R-4806E','R-4806W','R-4807A','R-4807B',"
    "'R-4808S','R-4809','REVEILLE NORTH MOA','REVEILLE SOUTH MOA')"
)

# trigger, zone_id, display name, max_alt_ft, source
ASSIGN = {
    "DESERT MOA": ("control_east", "sally-desert-moa", "Sally — Desert MOA", 60000, "faa_sua"),
    "R-4806E": ("control_east", "sally-r4806e", "Sally — R-4806E", 60000, "faa_sua"),
    "REVEILLE NORTH MOA": ("control_east", "sally-reveille-n", "Sally — Reveille North MOA", 60000, "faa_sua"),
    "REVEILLE SOUTH MOA": ("control_east", "sally-reveille-s", "Sally — Reveille South MOA", 60000, "faa_sua"),
    "R-4807A": ("control_west", "lee-r4807a", "Lee — R-4807A", 60000, "faa_sua"),
    "R-4807B": ("control_west", "lee-r4807b", "Lee — R-4807B", 60000, "faa_sua"),
    "R-4808S": ("control_west", "lee-r4808s", "Lee — R-4808S", 60000, "faa_sua"),
    "R-4809": ("control_west", "lee-r4809", "Lee — R-4809", 60000, "faa_sua"),
    "R-4806W": ("blackjack", "bj-r4806w", "Blackjack — R-4806W", 60000, "faa_sua"),
}

# Working-range copies so Blackjack and Control can overlap (range vs NATCF).
BLACKJACK_ALSO = {
    "R-4806E": ("blackjack", "bj-r4806e", "Blackjack — R-4806E", 60000, "faa_sua"),
    "R-4807A": ("blackjack", "bj-r4807a", "Blackjack — R-4807A", 60000, "faa_sua"),
    "R-4807B": ("blackjack", "bj-r4807b", "Blackjack — R-4807B", 60000, "faa_sua"),
    "R-4809": ("blackjack", "bj-r4809", "Blackjack — R-4809", 60000, "faa_sua"),
}

# 11-250 / EIS: Lee Corridor is south of the NAFR between Nellis and R-4808S / R-4807.
LEE_CORRIDOR = [
    (36.32, -115.18),
    (36.48, -115.52),
    (36.59, -115.67),
    (36.68, -116.25),
    (36.78, -116.50),
    (36.55, -115.95),
    (36.38, -115.45),
]

AGENCY_IDS = {spec[1] for spec in list(ASSIGN.values()) + list(BLACKJACK_ALSO.values())}
AGENCY_IDS.add("lee-corridor")

GEOMETRY_NOTE = (
    "Field boxes drawn in zone_server.py. NTTR agency polygons from FAA AIS "
    "Special Use Airspace (generalized NASR). Control East/Sally = Desert MOA "
    "+ R-4806E + Reveille. Control West/Lee = R-4807A/B + R-4808S + R-4809 + "
    "an inferred Lee Corridor (11-250; not a published SUA). Blackjack = "
    "working restricted areas. Joshua = R-2508 sketch. Not a substitute for "
    "the IFG chart."
)


def fetch_faa() -> dict:
    qs = urllib.parse.urlencode(
        {
            "where": FAA_WHERE,
            "outFields": "NAME,TYPE_CODE,SECTOR,CONT_AGENT",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
            "maxAllowableOffset": 0.015,
            "geometryPrecision": 4,
            "returnZ": "false",
        }
    )
    url = FAA_URL + "?" + qs
    with urllib.request.urlopen(url, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    GEOJSON.write_text(json.dumps(data), encoding="utf-8")
    feats = data.get("features") or []
    print(f"fetched {len(feats)} FAA SUA feature(s) → {GEOJSON.name}")
    return data


def load_geojson(*, force_fetch: bool) -> dict:
    if force_fetch or not GEOJSON.is_file():
        return fetch_faa()
    print(f"using cache {GEOJSON}")
    return json.loads(GEOJSON.read_text(encoding="utf-8"))


def ring_to_points(ring: list) -> list[dict[str, float]]:
    pts: list[dict[str, float]] = []
    for lon, lat, *_rest in ring:
        pt = {"lat": round(float(lat), 4), "lon": round(float(lon), 4)}
        if pts and pts[-1] == pt:
            continue
        pts.append(pt)
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def zone_dict(zid: str, trigger: str, name: str, points: list, max_alt: float, source: str) -> dict:
    return {
        "id": zid,
        "trigger": trigger,
        "kind": "polygon",
        "name": name,
        "max_alt_ft": max_alt,
        "source": source,
        "points": points,
        "locked": True,
    }


def outer_ring(geom: dict) -> list | None:
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon" and coords:
        return coords[0]
    if gtype == "MultiPolygon" and coords:
        rings = coords[0]
        return rings[0] if rings else None
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetch", action="store_true", help="re-query FAA AIS even if a cache exists")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write airports.json")
    args = ap.parse_args(argv)

    data = load_geojson(force_fetch=args.fetch)
    airports = json.loads(AIRPORTS.read_text(encoding="utf-8"))
    zones: list[dict] = list(airports["nellis"]["geometry"]["zones"])

    by_name: dict[str, list] = {}
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        if props.get("SECTOR"):
            continue
        name = str(props.get("NAME") or "")
        ring = outer_ring(feat.get("geometry") or {})
        if not name or not ring:
            continue
        by_name[name] = ring_to_points(ring)

    new_zones: list[dict] = []
    missing: list[str] = []
    for name, spec in list(ASSIGN.items()) + list(BLACKJACK_ALSO.items()):
        trigger, zid, label, alt, source = spec
        pts = by_name.get(name)
        if not pts:
            missing.append(name)
            continue
        new_zones.append(zone_dict(zid, trigger, label, pts, alt, source))

    new_zones.append(
        zone_dict(
            "lee-corridor",
            "control_west",
            "Lee Corridor (inferred 11-250)",
            [{"lat": lat, "lon": lon} for lat, lon in LEE_CORRIDOR],
            27000.0,
            "inferred_11_250",
        )
    )

    if missing:
        print("missing FAA polygons:", ", ".join(sorted(set(missing))))
        return 1

    kept = [z for z in zones if str(z.get("id") or "") not in AGENCY_IDS]
    joshua = [z for z in kept if str(z.get("id") or "") == "joshua"]
    core = [z for z in kept if str(z.get("id") or "") != "joshua"]
    merged = core + new_zones + joshua

    print(f"{len(new_zones)} agency zone(s); total {len(merged)}")
    for z in new_zones:
        print(f"  {z['id']:18} {z['trigger']:13} {len(z['points']):3} pts")

    if args.dry_run:
        return 0

    airports["nellis"]["geometry"]["zones"] = merged
    airports["nellis"]["geometry"]["note"] = GEOMETRY_NOTE
    AIRPORTS.write_text(json.dumps(airports, indent=2) + "\n", encoding="utf-8")
    print("wrote", AIRPORTS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
