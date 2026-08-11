#!/usr/bin/env python3
"""
Import zones drawn elsewhere into atc/airports.json. Internal tool.

For the Google Earth route: draw polygons and paths over the field, save the
folder as KML or KMZ, then

    py -3 tools/import_zones.py nellis KLSV-zones.kml

What each shape means is read from its placemark name — "21R in position",
"eor:21R:NW EOR", "runway 21R" — so nothing needs renaming by hand. Anything it
cannot read is reported and skipped rather than guessed at.

Also accepts GeoJSON, which is what the Leaflet editor exports.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import zone_geo


def describe(shape: zone_geo.Shape) -> str:
    what = shape.kind
    if shape.kind == "circle" and shape.radius_m:
        what = f"circle r{shape.radius_m:.0f}m"
    elif shape.points:
        what = f"{shape.kind} ({len(shape.points)} pts)"
    tags = " ".join(t for t in (shape.trigger or "UNTAGGED", shape.runway, shape.band) if t)
    return f"  {shape.label[:34]:34}  {what:20}  {tags}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("airport", help="airports.json key, e.g. nellis")
    ap.add_argument("path", type=Path, help=".kml, .kmz, .geojson or .json")
    ap.add_argument(
        "--merge",
        action="store_true",
        help="keep existing zones whose ids are not in the import",
    )
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    args = ap.parse_args(argv)

    if not args.path.is_file():
        print(f"no such file: {args.path}", file=sys.stderr)
        return 2

    shapes = zone_geo.parse_any(args.path.read_bytes(), args.path.name)
    if not shapes:
        print(f"no shapes found in {args.path.name}", file=sys.stderr)
        return 1

    print(f"{args.path.name}: {len(shapes)} shape(s)")
    for shape in shapes:
        print(describe(shape))

    data = zone_geo.load_airports()
    airport = data.get(args.airport)
    if not isinstance(airport, dict):
        print(
            f"unknown airport {args.airport!r}; have: {', '.join(sorted(data))}",
            file=sys.stderr,
        )
        return 2

    zones, runways, warnings = zone_geo.shapes_to_geometry(
        shapes, list(airport.get("runways") or [])
    )
    for warning in warnings:
        print(f"  ! {warning}")

    zone_geo.apply_to_airport(
        airport,
        zones,
        runways,
        replace=not args.merge,
        note=(
            f"Imported from {args.path.name} with tools/import_zones.py, "
            f"{time.strftime('%Y-%m-%d')}. Coordinates are lat/lon."
        ),
    )
    geo = airport["geometry"]
    print(
        f"\n{args.airport}: {len(geo.get('zones') or [])} zone(s), "
        f"runways {', '.join(sorted(geo.get('runways') or {})) or 'none'}, "
        f"calibrated={geo.get('calibrated')}"
    )

    if args.dry_run:
        print("\n--dry-run, nothing written. Geometry would be:")
        print(json.dumps(geo, indent=2)[:4000])
        return 0

    backup = zone_geo.save_airports(data)
    print(f"wrote {zone_geo.AIRPORTS_JSON}  (backup {backup.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
