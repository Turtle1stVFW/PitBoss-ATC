#!/usr/bin/env python3
"""
Checks for the zone importer: py -3 tools/check_zone_geo.py

Covers the parts that would silently produce geometry pointing at the wrong patch
of desert — coordinate order, tag inference, reciprocal runways — and the round
trip through airports.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Windows consoles still default to cp1252; a check run should not die on a dash.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, OSError):
    pass

import zone_geo  # noqa: E402

sys.path.insert(0, str(zone_geo.ATC_DIR))
import runway_position as rp  # noqa: E402

# A rough box on the Nellis 21R threshold and a centreline down the runway.
THRESHOLD = (36.2478, -115.0247)
FAR_END = (36.2245, -115.0432)


def show(title: str) -> None:
    print(f"\n--- {title} ---")


def check_tags() -> int:
    bad = 0
    show("tags read from names")
    cases = [
        ("in_position:21R", ("in_position", "21R")),
        ("21R in position", ("in_position", "21R")),
        ("eor:03L:NW EOR", ("eor", "03L")),
        ("Runway 3L", ("runway", "03L")),
        ("hold short 21R", ("hold_short", "21R")),
        ("21 Right line up", ("in_position", "21R")),
        ("End of runway 21R", ("eor", "21R")),
        ("Row 18 parking", ("parking", "")),
        ("spot 7 hold short", ("hold_short", "")),
        ("hold short runway 9", ("hold_short", "09")),
        ("some polygon", ("", "")),
    ]
    for name, want in cases:
        trigger, runway, _ = zone_geo.infer_tags(name)
        got = (trigger, runway)
        ok = got == want
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {name!r:26} -> {got}" + ("" if ok else f" want {want}"))

    # A dropdown value is stated outright, so a bare number is fine there.
    if zone_geo.normalize_runway("09") != "09":
        print("  FAIL an explicit runway value should not need a side letter")
        bad += 1
    else:
        print("ok   explicit '09' accepted, inferred '18' in 'Row 18' is not")

    show("reciprocal runways")
    for rwy, want in (("21R", "03L"), ("03L", "21R"), ("36", "18"), ("18C", "36C")):
        got = zone_geo.reciprocal_runway(rwy)
        ok = got == want
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {rwy} -> {got}" + ("" if ok else f" want {want}"))
    return bad


def check_geojson() -> int:
    bad = 0
    show("GeoJSON -> geometry")
    collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"trigger": "runway", "runway": "21R"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [THRESHOLD[1], THRESHOLD[0]],
                        [FAR_END[1], FAR_END[0]],
                    ],
                },
            },
            {
                "type": "Feature",
                "properties": {"trigger": "in_position", "runway": "21R", "name": "21R box"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [_box_ring(THRESHOLD, 0.0025)],
                },
            },
            {
                "type": "Feature",
                "properties": {"trigger": "eor", "runway": "21R", "radius_m": 240},
                "geometry": {"type": "Point", "coordinates": [-115.0262, 36.2489]},
            },
        ],
    }
    shapes = zone_geo.parse_geojson(json.dumps(collection))
    kinds = sorted(s.kind for s in shapes)
    if kinds != ["circle", "line", "polygon"]:
        print(f"  FAIL expected one of each shape, got {kinds}")
        bad += 1
    else:
        print("three shapes parsed - ok")

    zones, runways, warnings = zone_geo.shapes_to_geometry(shapes)
    if warnings:
        print(f"  FAIL unexpected warnings: {warnings}")
        bad += 1

    # The reciprocal is the same concrete, so drawing 21R must fill in 03L.
    if sorted(runways) != ["03L", "21R"]:
        print(f"  FAIL runways {sorted(runways)}")
        bad += 1
    else:
        print(f"21R centreline filled in 03L, {runways['21R']['length_m']:.0f} m long - ok")
    if runways["03L"]["threshold"] != runways["21R"]["far_end"]:
        print("  FAIL 03L threshold should be 21R's far end")
        bad += 1

    # Latitude must not have been swapped for longitude anywhere on the way in.
    thr = runways["21R"]["threshold"]
    if abs(thr["lat"] - THRESHOLD[0]) > 1e-6 or abs(thr["lon"] - THRESHOLD[1]) > 1e-6:
        print(f"  FAIL threshold round trip: {thr}")
        bad += 1
    else:
        print("lat/lon order preserved - ok")

    if len(zones) != 2:
        print(f"  FAIL expected 2 zones, got {len(zones)}")
        bad += 1
    ids = [z["id"] for z in zones]
    if ids != ["in_position-21r", "eor-21r"]:
        print(f"  FAIL zone ids {ids}")
        bad += 1
    else:
        print(f"zone ids {ids} - ok")

    # The runtime has to agree that a jet on the threshold is inside the box.
    airport = {"geometry": {"zones": zones, "runways": runways}}
    x, z = rp.point_xz({"lat": THRESHOLD[0], "lon": THRESHOLD[1]})
    hit = rp.in_any_zone(x, z, rp.zones_for(airport, "in_position", "21R"))
    if not hit:
        print("  FAIL runway_position does not see the threshold inside the drawn box")
        bad += 1
    else:
        print(f"runway_position agrees: threshold is in {hit['id']} - ok")

    outside = rp.point_xz({"lat": THRESHOLD[0] + 0.02, "lon": THRESHOLD[1]})
    if rp.in_any_zone(*outside, rp.zones_for(airport, "in_position", "21R")):
        print("  FAIL a point 2 km north should not be in the box")
        bad += 1
    else:
        print("point well outside the box is outside - ok")
    return bad


def _box_ring(centre: tuple[float, float], half: float) -> list[list[float]]:
    lat, lon = centre
    ring = [
        [lon - half, lat - half],
        [lon + half, lat - half],
        [lon + half, lat + half],
        [lon - half, lat + half],
    ]
    return ring + [ring[0]]


def check_kml() -> int:
    bad = 0
    show("Google Earth KML")
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Placemark><name>Runway 21R</name><LineString><coordinates>
    {THRESHOLD[1]},{THRESHOLD[0]},0 {FAR_END[1]},{FAR_END[0]},0
  </coordinates></LineString></Placemark>
  <Placemark><name>21R in position</name><Polygon><outerBoundaryIs><LinearRing>
    <coordinates>
      -115.0250,36.2470,0 -115.0240,36.2470,0 -115.0240,36.2486,0
      -115.0250,36.2486,0 -115.0250,36.2470,0
    </coordinates>
  </LinearRing></outerBoundaryIs></Polygon></Placemark>
  <Placemark><name>eor:21R:NW EOR</name>
    <ExtendedData><Data name="radius_m"><value>250</value></Data></ExtendedData>
    <Point><coordinates>-115.0262,36.2489,0</coordinates></Point></Placemark>
</Document></kml>"""
    shapes = zone_geo.parse_kml(kml)
    if len(shapes) != 3:
        print(f"  FAIL expected 3 placemarks, got {len(shapes)}")
        return bad + 1
    by_trigger = {s.trigger: s for s in shapes}
    if sorted(by_trigger) != ["eor", "in_position", "runway"]:
        print(f"  FAIL triggers {sorted(by_trigger)}")
        bad += 1
    else:
        print("triggers read from placemark names - ok")

    line = by_trigger["runway"]
    if abs(line.points[0][0] - THRESHOLD[0]) > 1e-6:
        print(f"  FAIL KML coordinates are lon,lat — got {line.points[0]}")
        bad += 1
    else:
        print("KML lon,lat decoded correctly - ok")

    eor = by_trigger["eor"]
    if eor.kind != "circle" or eor.radius_m != 250.0 or eor.name != "NW EOR":
        print(f"  FAIL eor placemark: {eor}")
        bad += 1
    else:
        print("point + radius_m became a circle named NW EOR - ok")

    poly = by_trigger["in_position"]
    if poly.kind != "polygon" or len(poly.points) != 4:
        print(f"  FAIL closing point should be dropped: {len(poly.points)} points")
        bad += 1
    else:
        print("polygon ring opened to 4 points - ok")

    if zone_geo.parse_any(kml.encode(), "zones.kml") == []:
        print("  FAIL parse_any should route .kml to the KML reader")
        bad += 1
    return bad


def check_apply() -> int:
    bad = 0
    show("writing into an airport entry")
    shapes = [
        zone_geo.Shape(
            kind="line",
            points=[THRESHOLD, FAR_END],
            trigger="runway",
            runway="21R",
            name="Runway 21R",
        ),
        zone_geo.Shape(
            kind="polygon",
            points=[
                (36.2470, -115.0250),
                (36.2470, -115.0240),
                (36.2486, -115.0240),
                (36.2486, -115.0250),
            ],
            trigger="in_position",
            runway="21R",
            name="21R box",
        ),
    ]
    zones, runways, _ = zone_geo.shapes_to_geometry(shapes)

    airport = {
        "geometry": {
            "field_elev_ft": 1870,
            "runways": {"21R": {"width_m": 45, "threshold": {"x": 1.0, "z": 2.0}}},
            "zones": [{"id": "parking-old", "trigger": "parking", "kind": "polygon"}],
        }
    }
    zone_geo.apply_to_airport(airport, zones, runways, replace=True)
    geo = airport["geometry"]

    if geo["field_elev_ft"] != 1870:
        print("  FAIL field elevation should survive a save")
        bad += 1
    if geo["runways"]["21R"].get("width_m") != 45:
        print("  FAIL hand-set width_m should survive a save")
        bad += 1
    else:
        print("existing width_m and field elevation kept - ok")
    if "x" in geo["runways"]["21R"]["threshold"]:
        print("  FAIL threshold should have been replaced with lat/lon")
        bad += 1
    else:
        print("stale x/z threshold replaced by lat/lon - ok")
    if any(z["id"] == "parking-old" for z in geo["zones"]):
        print("  FAIL replace=True should drop old zones")
        bad += 1
    if not geo["calibrated"]:
        print("  FAIL centreline + in_position should read as calibrated")
        bad += 1
    else:
        print("calibrated flag set - ok")

    # Merge keeps what it is not replacing.
    airport2 = {
        "geometry": {"zones": [{"id": "parking-old", "trigger": "parking"}]}
    }
    zone_geo.apply_to_airport(airport2, zones, runways, replace=False)
    if not any(z["id"] == "parking-old" for z in airport2["geometry"]["zones"]):
        print("  FAIL merge should keep unrelated zones")
        bad += 1
    else:
        print("merge kept the unrelated zone - ok")

    # EOR on its own cannot clear anyone for takeoff.
    eor_only = {"zones": [{"trigger": "eor"}], "runways": {}}
    if zone_geo.is_usable(eor_only):
        print("  FAIL an EOR zone alone is not enough to be calibrated")
        bad += 1
    else:
        print("EOR alone does not count as calibrated - ok")

    show("warnings for mistagged shapes")
    stray = [
        zone_geo.Shape(kind="polygon", points=shapes[1].points, trigger="in_position",
                       runway="09L", name="09L box"),
        zone_geo.Shape(kind="polygon", points=shapes[1].points, trigger="eor",
                       name="unscoped EOR"),
    ]
    stray.append(
        zone_geo.Shape(kind="polygon", points=shapes[1].points, trigger="runway",
                       runway="21R", name="21R as a blob")
    )
    _, stray_runways, warns = zone_geo.shapes_to_geometry(stray, ["21R", "03L"])
    if not any("does not have" in w for w in warns):
        print("  FAIL a runway the field lacks should be called out")
        bad += 1
    if not any("applies to all" in w for w in warns):
        print("  FAIL an unscoped takeoff/EOR area should be called out")
        bad += 1
    # Two corners of a polygon are not a threshold and a far end.
    if stray_runways or not any("has to be a line" in w for w in warns):
        print("  FAIL an area tagged as a runway should be refused, not measured")
        bad += 1
    for w in warns:
        print(f"  ! {w}")
    return bad


def check_round_trip() -> int:
    """Save to a scratch file and read it back the way the flow app would."""
    import tempfile

    bad = 0
    show("save, reload, then measure it like the flow app does")
    shapes = zone_geo.parse_kml(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Placemark><name>Runway 21R</name><LineString><coordinates>
    {THRESHOLD[1]},{THRESHOLD[0]},0 {FAR_END[1]},{FAR_END[0]},0
  </coordinates></LineString></Placemark>
</Document></kml>"""
    )
    zones, runways, _ = zone_geo.shapes_to_geometry(shapes, ["21R", "03L"])
    data = zone_geo.load_airports()
    zone_geo.apply_to_airport(data["nellis"], zones, runways, replace=False)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "airports.json"
        zone_geo.save_airports(data, path)
        reloaded = zone_geo.load_airports(path)

    airport = reloaded["nellis"]
    frame = rp.RunwayFrame.build("21R", rp.runway_geometry(airport, "21R"))
    if frame is None:
        print("  FAIL runway_position could not build a frame from the saved geometry")
        return bad + 1
    print(
        f"21R reads {frame.length_m:.0f} m long on {frame.heading_deg:.1f} deg true, "
        f"width {frame.width_m:.0f} m"
    )
    if rp.angle_diff(frame.heading_deg, 212.6) > 2.0:
        print("  FAIL 21R should run about 212.6 deg true")
        bad += 1
    if not 2900 <= frame.length_m <= 3300:
        print("  FAIL 21R should be about 10,100 ft long")
        bad += 1
    if frame.width_m != 45.0:
        print("  FAIL hand-set width should have survived the save")
        bad += 1
    if not bad:
        print("geometry survives the round trip - ok")

    rev = rp.RunwayFrame.build("03L", rp.runway_geometry(airport, "03L"))
    if rev is None or rp.angle_diff(rev.heading_deg, frame.heading_deg + 180.0) > 1.0:
        print("  FAIL 03L should come out as the reciprocal")
        bad += 1
    else:
        print(f"03L came back at {rev.heading_deg:.1f} deg without being drawn - ok")
    return bad


def check_alt_band() -> int:
    """An altitude band has to survive every hop between the editor and the flow."""
    bad = 0
    show("altitude bands")

    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": "Tower area",
                    "trigger": "tower",
                    "min_alt_ft": 0,
                    "max_alt_ft": 5000,
                    "radius_m": 9260,
                },
                "geometry": {"type": "Point", "coordinates": [-115.0343, 36.2362]},
            },
            {
                "type": "Feature",
                "properties": {"name": "Row 18 parking", "trigger": "parking"},
                "geometry": {"type": "Polygon", "coordinates": [_box_ring(THRESHOLD, 0.002)]},
            },
        ],
    }
    shapes = zone_geo.parse_geojson(json.dumps(gj))
    tower = next(s for s in shapes if s.trigger == "tower")
    ramp = next(s for s in shapes if s.trigger == "parking")
    if (tower.min_alt_ft, tower.max_alt_ft) != (0.0, 5000.0):
        print(f"  FAIL band read from GeoJSON as {tower.min_alt_ft}-{tower.max_alt_ft}")
        bad += 1
    else:
        print(f"GeoJSON properties -> {tower.band} - ok")
    if (ramp.min_alt_ft, ramp.max_alt_ft) != (None, None):
        print("  FAIL a shape with no band should not invent one")
        bad += 1
    else:
        print("no band stays unbounded - ok")

    zones, _runways, warnings = zone_geo.shapes_to_geometry(shapes)
    band_zone = next(z for z in zones if z["trigger"] == "tower")
    if band_zone.get("min_alt_ft") != 0 or band_zone.get("max_alt_ft") != 5000:
        print(f"  FAIL band lost writing the zone: {band_zone}")
        bad += 1
    if "min_alt_ft" in next(z for z in zones if z["trigger"] == "parking"):
        print("  FAIL an unbounded zone should have no altitude keys at all")
        bad += 1
    else:
        print("zone written with the band, unbounded zones left clean - ok")

    # And back out again, so reopening the editor shows the band you set.
    reopened = zone_geo.parse_geojson(
        json.dumps(zone_geo.geometry_to_geojson({"geometry": {"zones": zones}}))
    )
    again = next(s for s in reopened if s.trigger == "tower")
    if (again.min_alt_ft, again.max_alt_ft) != (0.0, 5000.0):
        print(f"  FAIL band lost reopening: {again.min_alt_ft}-{again.max_alt_ft}")
        bad += 1
    else:
        print("survives the trip back to the editor - ok")

    # The flow app reads the same band, in metres, off the saved zone. A floor of
    # zero comes back unbounded: field elevation is one number for a field that is
    # not flat, so a parked jet reads a little below the surface and still has to
    # count as being on it.
    lo, hi = rp.zone_alt_band_m(band_zone)
    if lo is not None or abs(hi - 1524.0) > 1.0:
        print(f"  FAIL runway_position reads the band as {lo}-{hi} m")
        bad += 1
    else:
        print(f"runway_position sees surface-{hi:.0f} m AGL - ok")
    floor = rp.zone_alt_band_m(dict(band_zone, min_alt_ft=1500))[0]
    if floor is None or abs(floor - 457.2) > 1.0:
        print(f"  FAIL a floor of 1500 ft should read as 457 m, got {floor}")
        bad += 1
    else:
        print(f"a floor above the surface reads as {floor:.0f} m - ok")

    show("altitude bands read from Google Earth")
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Placemark><name>Overhead break</name>
    <ExtendedData>
      <Data name="trigger"><value>tower</value></Data>
      <Data name="min_alt_ft"><value>1500</value></Data>
      <Data name="max_alt_ft"><value>5000</value></Data>
      <Data name="radius_m"><value>3000</value></Data>
    </ExtendedData>
    <Point><coordinates>-115.0343,36.2362,0</coordinates></Point></Placemark>
</Document></kml>"""
    shape = zone_geo.parse_kml(kml)[0]
    if (shape.min_alt_ft, shape.max_alt_ft) != (1500.0, 5000.0) or shape.trigger != "tower":
        print(f"  FAIL KML ExtendedData: {shape}")
        bad += 1
    else:
        print(f"ExtendedData -> {shape.trigger}, {shape.band} - ok")

    # A trigger nobody coded still round-trips, which is how a custom agency area
    # gets drawn without touching this file.
    custom = zone_geo.parse_geojson(
        json.dumps(
            {
                "type": "Feature",
                "properties": {"name": "Range control", "trigger": "range_control"},
                "geometry": {"type": "Polygon", "coordinates": [_box_ring(THRESHOLD, 0.003)]},
            }
        )
    )[0]
    if custom.trigger != "range_control":
        print(f"  FAIL a custom trigger name was rewritten to {custom.trigger!r}")
        bad += 1
    else:
        print("custom trigger name kept as written - ok")

    show("a band that admits nothing is called out")
    _z, _r, warnings = zone_geo.shapes_to_geometry(
        [
            zone_geo.Shape(
                kind="circle",
                points=[THRESHOLD],
                radius_m=500,
                trigger="tower",
                name="upside down",
                min_alt_ft=5000,
                max_alt_ft=1000,
            )
        ]
    )
    if not any("upside-down" in w for w in warnings):
        print("  FAIL a max below the min should be flagged")
        bad += 1
    for warn in warnings:
        print(f"  ! {warn}")
    return bad


def check_live_file() -> int:
    bad = 0
    show("the real airports.json")
    data = zone_geo.load_airports()
    for key, airport in data.items():
        centre = zone_geo.airport_centre_ll(airport)
        gj = zone_geo.geometry_to_geojson(airport)
        print(f"{key}: centre {centre[0]:.4f},{centre[1]:.4f}, {len(gj['features'])} feature(s)")
        # Reopening must give back what is on disk, whether stored as x/z or lat/lon.
        shapes = zone_geo.parse_geojson(json.dumps(gj))
        if len(shapes) != len(gj["features"]):
            print(f"  FAIL {key}: {len(gj['features'])} features → {len(shapes)} shapes")
            bad += 1
        for shape in shapes:
            if not shape.trigger:
                print(f"  FAIL {key}: reopened shape {shape.label!r} lost its trigger")
                bad += 1
    return bad


def main() -> int:
    bad = 0
    checks = (
        check_tags,
        check_geojson,
        check_kml,
        check_apply,
        check_round_trip,
        check_alt_band,
        check_live_file,
    )
    for check in checks:
        bad += check()
    print("\n" + ("all good" if not bad else f"{bad} problem(s)"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
