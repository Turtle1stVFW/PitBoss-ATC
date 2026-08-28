#!/usr/bin/env python3
"""
Checks the editor's HTTP surface: py -3 tools/check_zone_server.py

Starts the server on a spare port in this process, so nothing needs to be running
first and no browser is involved. Saving is exercised against a scratch copy of
airports.json — the real one is never touched.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, OSError):
    pass

import zone_geo  # noqa: E402
import zone_server  # noqa: E402

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Placemark><name>Runway 21R</name><LineString><coordinates>
    -115.0247,36.2478,0 -115.0432,36.2245,0
  </coordinates></LineString></Placemark>
  <Placemark><name>21R in position</name><Polygon><outerBoundaryIs><LinearRing>
    <coordinates>
      -115.0250,36.2470,0 -115.0240,36.2470,0 -115.0240,36.2486,0
      -115.0250,36.2486,0 -115.0250,36.2470,0
    </coordinates>
  </LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>"""


class QuietHandler(zone_server.Handler):
    """Same handler, without the access log interleaving with the results."""

    def log_message(self, fmt: str, *args: object) -> None:
        pass


class Client:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"

    def __call__(
        self, path: str, data: bytes | None = None, ctype: str = "application/json"
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, data=data)
        if data is not None:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


def check_page_wiring() -> int:
    """Every element the script reaches for has to exist in the page."""
    import re

    bad = 0
    html = (HERE / "zone_map.html").read_text(encoding="utf-8")
    js = (HERE / "zone_map.js").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    wanted = set(re.findall(r'el\("([^"]+)"\)', js))
    missing = sorted(wanted - ids)
    if missing:
        print(f"FAIL script looks up ids the page does not have: {missing}")
        bad += 1
    else:
        print(f"ok   {len(wanted)} element lookups all resolve")

    for src, name in ((html, "zone_map.html"), (js, "zone_map.js")):
        for asset in re.findall(r'(?:src|href)="(/[^"]+)"', src):
            if not (HERE / asset.lstrip("/")).is_file():
                print(f"FAIL {name} references a missing file: {asset}")
                bad += 1
    return bad


def check_tester_wiring() -> int:
    """Every element the tester script reaches for has to exist in the page."""
    import re

    bad = 0
    html = (HERE / "tester_map.html").read_text(encoding="utf-8")
    js = (HERE / "tester_map.js").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    wanted = set(re.findall(r'el\("([^"]+)"\)', js))
    missing = sorted(wanted - ids)
    if missing:
        print(f"FAIL tester script looks up ids the page does not have: {missing}")
        bad += 1
    else:
        print(f"ok   tester {len(wanted)} element lookups all resolve")

    for src, name in ((html, "tester_map.html"), (js, "tester_map.js")):
        for asset in re.findall(r'(?:src|href)="(/[^"]+)"', src):
            if asset == "/":
                continue
            if not (HERE / asset.lstrip("/")).is_file():
                print(f"FAIL {name} references a missing file: {asset}")
                bad += 1
    return bad


def main() -> int:
    bad = 0
    scratch = Path(tempfile.mkdtemp(prefix="zones-")) / "airports.json"
    shutil.copy2(zone_geo.AIRPORTS_JSON, scratch)
    real = zone_geo.AIRPORTS_JSON
    real_mtime = real.stat().st_mtime
    zone_geo.AIRPORTS_JSON = scratch  # everything below writes here instead

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    get = Client(port)
    print(f"server on port {port}, writing {scratch}")

    try:
        print("\n--- static files ---")
        status, body = get("/")
        ok = status == 200 and b"Runway zones" in body
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} GET /  ({len(body)} bytes)")
        for path in (
            "/zone_map.js",
            "/zone_map.css",
            "/tester",
            "/tester_map.js",
            "/tester_map.css",
            "/vendor/leaflet.js",
            "/vendor/leaflet-draw.js",
            "/vendor/leaflet.css",
            "/vendor/images/spritesheet.png",
        ):
            status, body = get(path)
            ok = status == 200 and bool(body)
            bad += 0 if ok else 1
            print(f"{'ok  ' if ok else 'FAIL'} GET {path}  ({len(body)} bytes)")

        status, _ = get("/vendor/../zone_server.py")
        bad += 0 if status == 404 else 1
        print(f"{'ok  ' if status == 404 else 'FAIL'} climbing out of /vendor/ is refused")

        print("\n--- page wiring ---")
        bad += check_page_wiring()
        bad += check_tester_wiring()

        print("\n--- route tester ---")
        status, body = get("/tester")
        ok = status == 200 and b"Map jet" in body
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} GET /tester  ({len(body)} bytes)")

        status, body = get("/api/tester/state?airport=nellis")
        tstate = json.loads(body)
        ok = status == 200 and tstate.get("ok") and tstate.get("geojson")
        bad += 0 if ok else 1
        print(
            f"{'ok  ' if ok else 'FAIL'} /api/tester/state hop={tstate.get('hop')!r} "
            f"fixes={len(tstate.get('fixes') or [])}"
        )

        status, body = get("/api/tester/route?airport=nellis&q=KLSV+DREAM+SARAH+KLSV")
        plotted = json.loads(body)
        wps = plotted.get("waypoints") or []
        ok = status == 200 and plotted.get("ok") and len(wps) >= 2
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} /api/tester/route {len(wps)} waypoint(s) hop={plotted.get('hop')!r}")

        payload = json.dumps(
            {
                "airport": "nellis",
                "lat": 36.235,
                "lon": -115.038,
                "alt_ft_agl": 1000,
                "heading_deg": 210,
                "speed_kt": 200,
                "route": "KLSV FLEX DREAM SARAH KLSV",
                "reset": True,
            }
        ).encode()
        status, body = get("/api/tester/tick", payload)
        tick = json.loads(body)
        trigs = [z.get("trigger") for z in (tick.get("inside") or [])]
        ok = status == 200 and tick.get("ok") and "tower" in trigs
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} POST /api/tester/tick tower in {trigs}")

        status, body = get("/api/tester/opus-flights?detail=0")
        opus = json.loads(body)
        ok = status == 200 and isinstance(opus.get("flights"), list)
        bad += 0 if ok else 1
        print(
            f"{'ok  ' if ok else 'FAIL'} GET /api/tester/opus-flights "
            f"ok={opus.get('ok')} n={len(opus.get('flights') or [])}"
        )

        status, body = get("/api/tester/metar?airport=nellis")
        metar = json.loads(body)
        ok = status == 200 and "ok" in metar
        bad += 0 if ok else 1
        print(
            f"{'ok  ' if ok else 'FAIL'} GET /api/tester/metar "
            f"ok={metar.get('ok')} icao={metar.get('icao')!r}"
        )

        say_payload = json.dumps(
            {
                "airport": "nellis",
                "lat": 36.235,
                "lon": -115.038,
                "alt_ft_agl": 0,
                "heading_deg": 210,
                "speed_kt": 0,
                "route": "KLSV FLEX DREAM SARAH KLSV",
                "tune": "ground",
                "transcript": "Nellis Ground, Fleece 1, ready to taxi",
                "hear": False,
                "reset": True,
            }
        ).encode()
        status, body = get("/api/tester/say", say_payload)
        said = json.loads(body)
        ok = (
            status == 200
            and said.get("ok")
            and said.get("fired")
            and said.get("intent") == "ready_taxi"
        )
        bad += 0 if ok else 1
        print(
            f"{'ok  ' if ok else 'FAIL'} POST /api/tester/say "
            f"intent={said.get('intent')!r} fired={said.get('fired')}"
        )

        print("\n--- state ---")
        status, body = get("/api/state")
        state = json.loads(body)
        ok = status == 200 and state.get("airport") and state.get("triggers")
        bad += 0 if ok else 1
        print(
            f"{'ok  ' if ok else 'FAIL'} {state.get('airport')}: runways "
            f"{state.get('runways')}, centre {state.get('centre')}, "
            f"{len(state['geojson']['features'])} existing feature(s)"
        )

        print("\n--- parse a Google Earth file ---")
        status, body = get("/api/parse?filename=drawn.kml", KML.encode(), "application/xml")
        out = json.loads(body)
        feats = out.get("geojson", {}).get("features", [])
        ok = out.get("ok") and len(feats) == 2 and not out.get("warnings")
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} 2 placemarks -> {len(feats)} feature(s)")
        for feat in feats:
            print(f"       {json.dumps(feat['properties'])}")

        print("\n--- save ---")
        # Plus an agency area with an altitude band, which is the case the flow app
        # needs so overflying traffic does not read as being on the field.
        payload = dict(out["geojson"])
        payload["features"] = list(payload["features"]) + [
            {
                "type": "Feature",
                "properties": {
                    "name": "Tower area",
                    "trigger": "tower",
                    "radius_m": 9260,
                    "min_alt_ft": 0,
                    "max_alt_ft": 5000,
                },
                "geometry": {"type": "Point", "coordinates": [-115.0343, 36.2362]},
            }
        ]
        status, body = get(
            "/api/save",
            json.dumps(
                {"airport": state["airport"], "geojson": payload, "replace": True}
            ).encode(),
        )
        saved = json.loads(body)
        ok = saved.get("ok") and saved.get("calibrated") and saved.get("runways") == ["03L", "21R"]
        bad += 0 if ok else 1
        print(
            f"{'ok  ' if ok else 'FAIL'} {saved.get('zones')} zone(s), runways "
            f"{saved.get('runways')}, calibrated={saved.get('calibrated')}, "
            f"warnings={saved.get('warnings')}"
        )

        on_disk = zone_geo.load_airports(scratch)[state["airport"]]["geometry"]
        ok = on_disk.get("calibrated") and len(on_disk.get("zones") or []) == 2
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} scratch file has the geometry after the save")

        tower = next((z for z in on_disk.get("zones") or [] if z["trigger"] == "tower"), {})
        ok = tower.get("min_alt_ft") == 0 and tower.get("max_alt_ft") == 5000
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} the band was written: {tower.get('min_alt_ft')}-{tower.get('max_alt_ft')} ft AGL")

        status, body = get("/api/state")
        reopened = json.loads(body)
        feats = reopened["geojson"]["features"]
        lines = [f for f in feats if f["geometry"]["type"] == "LineString"]
        # One centreline, not one per direction, or reopening would stack two
        # identical lines on the same runway.
        ok = len(feats) == 3 and len(lines) == 1 and reopened["calibrated"]
        bad += 0 if ok else 1
        print(
            f"{'ok  ' if ok else 'FAIL'} reopening gives back {len(feats)} shape(s), "
            f"{len(lines)} centreline"
        )

        band = next(
            (f["properties"] for f in feats if f["properties"].get("trigger") == "tower"), {}
        )
        ok = band.get("min_alt_ft") == 0 and band.get("max_alt_ft") == 5000
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} the editor gets the band back to edit")

        untouched = real.stat().st_mtime == real_mtime and not real.with_suffix(
            ".json.bak"
        ).exists()
        bad += 0 if untouched else 1
        print(f"{'ok  ' if untouched else 'FAIL'} the real airports.json was never written")

        print("\n--- reference overlay ---")
        import zone_overlay

        status, body = get("/api/overlays")
        listed = json.loads(body)
        ok = status == 200 and listed.get("ok") and isinstance(listed.get("overlays"), list)
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} GET /api/overlays ({len(listed.get('overlays') or [])} pack(s))")

        tiny = KML.replace("Runway 21R", "Ref line").replace("21R in position", "Ref poly")
        status, body = get(
            "/api/overlay?filename=_check_overlay.kml",
            tiny.encode(),
            "application/xml",
        )
        meta = json.loads(body)
        ok = meta.get("ok") and meta.get("count") == 2 and meta.get("geojson_url")
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} upload overlay -> {meta.get('count')} feature(s)")

        status, body = get(meta.get("geojson_url") or "/api/overlay/geojson?file=_check_overlay.kml")
        gj = json.loads(body)
        ok = status == 200 and len(gj.get("features") or []) == 2
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} GET geojson ({len(gj.get('features') or [])} feature(s))")

        # Do not leave the scratch overlay in the real overlays folder.
        try:
            path = zone_overlay.resolve_overlay("_check_overlay.kml")
            path.unlink(missing_ok=True)
            zone_overlay._cache_path(path).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL could not remove scratch overlay: {exc}")
            bad += 1

        print("\n--- refusals ---")
        for path, payload, want in (
            ("/api/save", b'{"airport":"nope","geojson":{}}', 400),
            ("/api/parse?filename=x.kml", b"not xml", 400),
            ("/api/nope", None, 404),
        ):
            status, _ = get(path, payload)
            bad += 0 if status == want else 1
            print(f"{'ok  ' if status == want else 'FAIL'} {path} -> {status}")

        print("\n--- live feed (needs Opus running) ---")
        status, body = get("/api/tracks")
        out = json.loads(body)
        bad += 0 if status == 200 else 1
        print(
            f"{'ok  ' if status == 200 else 'FAIL'} /api/tracks: "
            + (f"{out.get('count')} track(s)" if out.get("ok") else f"no feed ({out.get('reason')})")
        )
        tracks = out.get("tracks") or []
        if tracks:
            # An altitude band is drawn in feet AGL, so the tracks you read it off
            # have to be reported the same way.
            elev_ft = float(on_disk.get("field_elev_ft") or 0.0)
            with_alt = [t for t in tracks if t.get("alt_ft") is not None]
            with_agl = [t for t in tracks if t.get("agl_ft") is not None]
            ok = bool(with_alt) and len(with_agl) == len(with_alt)
            bad += 0 if ok else 1
            print(
                f"{'ok  ' if ok else 'FAIL'} {len(with_alt)}/{len(tracks)} track(s) "
                f"report altitude, {len(with_agl)} as AGL"
            )
            off = [
                t for t in with_agl if abs((t["alt_ft"] - elev_ft) - t["agl_ft"]) > 1.0
            ]
            bad += 0 if not off else 1
            print(
                f"{'ok  ' if not off else 'FAIL'} AGL is MSL minus the "
                f"{elev_ft:.0f} ft field elevation"
            )
            sample = with_agl[0] if with_agl else tracks[0]
            print(
                f"       {sample['label']}: {sample.get('alt_ft')} ft MSL, "
                f"{sample.get('agl_ft')} ft AGL, zone {sample.get('zone') or '(none)'}"
            )
    finally:
        httpd.shutdown()
        httpd.server_close()
        zone_geo.AIRPORTS_JSON = real
        shutil.rmtree(scratch.parent, ignore_errors=True)

    print("\n" + ("all good" if not bad else f"{bad} problem(s)"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
