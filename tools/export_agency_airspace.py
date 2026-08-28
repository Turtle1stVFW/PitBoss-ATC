#!/usr/bin/env python3
"""
Write tools/agency_airspace.kml from the agency polygons in atc/airports.json.

Open in Google Earth, or drop a copy in tools/overlays/ for the zone editor.
ExtendedData trigger tags match tools/import_zones.py.

    py -3 tools/export_agency_airspace.py
"""
from __future__ import annotations

import argparse
import json
import math
from html import escape
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
AIRPORTS = HERE.parent / "atc" / "airports.json"
OUT = HERE / "agency_airspace.kml"

# KML poly color is aabbggrr.
FOLDERS: list[tuple[str, tuple[str, ...], str]] = [
    ("Sally / Control East (ch 7 317.525)", ("control_east",), "7f00aa55"),
    ("Lee / Control West (ch 8 254.400)", ("control_west",), "7f0066ff"),
    ("Blackjack (range)", ("blackjack",), "7f0000aa"),
    ("Approach / Departure", ("approach", "departure"), "7faa5500"),
    ("Joshua (R-2508 sketch)", ("joshua",), "7faa00aa"),
    ("Tower", ("tower",), "7f555555"),
]

CIRCLE_STEPS = 48


def circle_ring(lat: float, lon: float, radius_m: float) -> list[tuple[float, float]]:
    dlat = radius_m / 111_320.0
    pts: list[tuple[float, float]] = []
    for i in range(CIRCLE_STEPS):
        ang = 2.0 * math.pi * i / CIRCLE_STEPS
        la = lat + dlat * math.sin(ang)
        cos_lat = math.cos(math.radians(lat)) or 1e-6
        dlon = radius_m / (111_320.0 * cos_lat)
        lo = lon + dlon * math.cos(ang)
        pts.append((la, lo))
    pts.append(pts[0])
    return pts


def coords_xml(pts: list[tuple[float, float]]) -> str:
    return " ".join(f"{lon:.5f},{lat:.5f},0" for lat, lon in pts)


def zone_ring(zone: dict[str, Any]) -> list[tuple[float, float]] | None:
    kind = str(zone.get("kind") or "polygon").strip().lower()
    if kind == "circle":
        centre = zone.get("centre") or zone.get("center") or {}
        try:
            lat = float(centre["lat"])
            lon = float(centre["lon"])
            radius_m = float(zone.get("radius_m") or 0)
        except (KeyError, TypeError, ValueError):
            return None
        if radius_m <= 0:
            return None
        return circle_ring(lat, lon, radius_m)
    pts: list[tuple[float, float]] = []
    for pt in zone.get("points") or []:
        if not isinstance(pt, dict):
            continue
        try:
            pts.append((float(pt["lat"]), float(pt["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(pts) < 3:
        return None
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def placemark(zone: dict[str, Any], color: str) -> str | None:
    ring = zone_ring(zone)
    if not ring:
        return None
    zid = str(zone.get("id") or "")
    name = str(zone.get("name") or zid)
    trigger = str(zone.get("trigger") or "")
    source = str(zone.get("source") or "")
    max_alt = zone.get("max_alt_ft")
    desc_bits = [b for b in (zid, trigger, source) if b]
    desc = escape(" · ".join(desc_bits))
    max_xml = (
        f"        <Data name=\"max_alt_ft\"><value>{escape(str(max_alt))}</value></Data>\n"
        if max_alt is not None
        else ""
    )
    src_xml = (
        f"        <Data name=\"source\"><value>{escape(source)}</value></Data>\n"
        if source
        else ""
    )
    return (
        f"      <Placemark>\n"
        f"        <name>{escape(name)}</name>\n"
        f"        <description>{desc}</description>\n"
        f"        <Style><PolyStyle><color>{color}</color>"
        f"<fill>1</fill><outline>1</outline></PolyStyle>"
        f"<LineStyle><color>ff{color[2:]}</color><width>2</width></LineStyle></Style>\n"
        f"        <ExtendedData>\n"
        f"        <Data name=\"trigger\"><value>{escape(trigger)}</value></Data>\n"
        f"        <Data name=\"id\"><value>{escape(zid)}</value></Data>\n"
        f"{max_xml}{src_xml}"
        f"        </ExtendedData>\n"
        f"        <Polygon><altitudeMode>clampToGround</altitudeMode>"
        f"<outerBoundaryIs><LinearRing><coordinates>\n"
        f"          {coords_xml(ring)}\n"
        f"        </coordinates></LinearRing></outerBoundaryIs></Polygon>\n"
        f"      </Placemark>\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT, help="KML path")
    args = ap.parse_args(argv)

    airports = json.loads(AIRPORTS.read_text(encoding="utf-8"))
    zones = list((airports.get("nellis") or {}).get("geometry", {}).get("zones") or [])
    by_trigger: dict[str, list[dict[str, Any]]] = {}
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        by_trigger.setdefault(str(zone.get("trigger") or ""), []).append(zone)

    note = str((airports.get("nellis") or {}).get("geometry", {}).get("note") or "")
    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n',
        "  <Document>\n",
        "    <name>NTTR agency airspace (Sally / Lee / Blackjack)</name>\n",
        f"    <description>{escape(note)}</description>\n",
    ]
    n = 0
    for title, triggers, color in FOLDERS:
        marks = []
        for trig in triggers:
            for zone in by_trigger.get(trig, []):
                xml = placemark(zone, color)
                if xml:
                    marks.append(xml)
                    n += 1
        if not marks:
            continue
        chunks.append(f"    <Folder><name>{escape(title)}</name>\n")
        chunks.extend(marks)
        chunks.append("    </Folder>\n")
    chunks.append("  </Document>\n</kml>\n")
    args.out.write_text("".join(chunks), encoding="utf-8")
    print(f"wrote {args.out} ({n} placemarks)")
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
