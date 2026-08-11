#!/usr/bin/env python3
"""
Runway zone editor.

    py -3 tools/zone_server.py [--port 8777] [--airport nellis] [--no-browser]

Launched from the app by Setup -> Draw zones on a map, by Draw one beside Fires
when on Plan Flight, or on its own by atc/Open-Zone-Editor.cmd.

Opens a Leaflet map on satellite imagery at the airport, you trace the areas the
automatic clearances watch, press Save, and it writes atc/airports.json. Because
the map is georeferenced, every click already has a lat/lon — there is no image
to calibrate and no reference points to enter.

The browser talks to this process rather than the internet so it can reach
airports.json and the Opus CAOC feed (which would otherwise be blocked by CORS).
Basemap tiles are proxied the same way — a localhost page often cannot load
Esri or OSM tiles directly. Live tracks drawn on the same map are how you check
that a jet sitting on the runway in DCS also sits on the runway here.

Binds to localhost only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import zone_geo
import zone_overlay

# Basemap tiles are fetched here and served same-origin. Browsers on a localhost
# page often block arcgisonline / openstreetmap as third-party trackers, which
# leaves the map a blank black rectangle with only the Leaflet chrome visible.
TILE_SOURCES = {
    "esri": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "image/jpeg",
    ),
    "esri-labels": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        "image/png",
    ),
    "osm": (
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "image/png",
    ),
}
TILE_UA = "ATC-ZoneEditor/1.0 (localhost basemap proxy; +https://github.com)"
_tile_cache: dict[str, tuple[bytes, str]] = {}
_tile_cache_lock = threading.Lock()
_TILE_CACHE_MAX = 512

HERE = Path(__file__).resolve().parent
ATC_DIR = HERE.parent / "atc"
CONFIG_PATH = ATC_DIR / "config.json"
zone_overlay.ensure_overlays_dir()

if str(ATC_DIR) not in sys.path:
    sys.path.insert(0, str(ATC_DIR))

import atc_phrase  # noqa: E402
import runway_position as rp  # noqa: E402

STATIC = {
    "/": ("zone_map.html", "text/html; charset=utf-8"),
    "/zone_map.html": ("zone_map.html", "text/html; charset=utf-8"),
    "/zone_map.js": ("zone_map.js", "text/javascript; charset=utf-8"),
    "/zone_map.css": ("zone_map.css", "text/css; charset=utf-8"),
}
VENDOR_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}
_lock = threading.Lock()


def _config() -> dict[str, Any]:
    try:
        return atc_phrase.load_json(CONFIG_PATH) or {}
    except Exception:  # noqa: BLE001 — the editor is useful without a config
        return {}


def _airport_keys(data: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "key": key,
            "name": str((val or {}).get("name") or key),
            "icao": str((val or {}).get("icao") or ""),
            "calibrated": bool(
                (((val or {}).get("geometry") or {}) if isinstance(val, dict) else {}).get(
                    "calibrated"
                )
            ),
        }
        for key, val in data.items()
        if isinstance(val, dict)
    ]


def _state(airport_key: str) -> dict[str, Any]:
    data = zone_geo.load_airports()
    cfg = _config()
    key = airport_key or str(cfg.get("default_airport") or "") or next(iter(data), "")
    airport = data.get(key) if isinstance(data.get(key), dict) else {}
    lat, lon = zone_geo.airport_centre_ll(airport)
    return {
        "airports": _airport_keys(data),
        "airport": key,
        "name": str(airport.get("name") or key),
        "runways": [
            zone_geo.normalize_runway(r) or str(r)
            for r in (airport.get("runways") or [])
        ],
        "triggers": list(zone_geo.TRIGGERS),
        "centre": [lat, lon],
        "geojson": zone_geo.geometry_to_geojson(airport),
        "calibrated": bool((airport.get("geometry") or {}).get("calibrated")),
        "note": str((airport.get("geometry") or {}).get("note") or ""),
        "path": str(zone_geo.AIRPORTS_JSON),
    }


def _save(body: dict[str, Any]) -> dict[str, Any]:
    key = str(body.get("airport") or "").strip()
    if not key:
        raise ValueError("no airport given")
    shapes = zone_geo.parse_geojson(json.dumps(body.get("geojson") or {}))

    with _lock:
        data = zone_geo.load_airports()
        airport = data.get(key)
        if not isinstance(airport, dict):
            raise ValueError(f"unknown airport {key!r}")
        zones, runways, warnings = zone_geo.shapes_to_geometry(
            shapes, list(airport.get("runways") or [])
        )
        zone_geo.apply_to_airport(
            airport, zones, runways, replace=bool(body.get("replace", True))
        )
        backup = zone_geo.save_airports(data)

    return {
        "ok": True,
        "zones": len(zones),
        "runways": sorted(runways),
        "calibrated": bool(airport["geometry"].get("calibrated")),
        "warnings": warnings,
        "backup": backup.name,
        "path": str(zone_geo.AIRPORTS_JSON),
    }


def _parse(raw: bytes, filename: str) -> dict[str, Any]:
    """A dropped GeoJSON or Google Earth KML/KMZ, as drawable features."""
    shapes = zone_geo.parse_any(raw, filename)
    if not shapes:
        raise ValueError(f"no shapes found in {filename or 'file'}")
    untagged = [s.label for s in shapes if not s.trigger]
    return {
        "ok": True,
        "geojson": zone_geo.shapes_to_geojson(shapes),
        "warnings": (
            [f"no trigger read from: {', '.join(untagged[:4])} — set it before saving"]
            if untagged
            else []
        ),
    }


def _tracks(airport_key: str) -> dict[str, Any]:
    """Live CAOC air picture as lat/lon, plus which zone the flight's jet is in."""
    cfg = _config()
    radar = atc_phrase.fetch_caoc_radar(cfg, max_age_s=1.0)
    if not radar:
        return {"ok": False, "reason": "no CAOC radar feed", "tracks": []}
    units = atc_phrase.caoc_air_units(list(radar.get("units") or []))
    own = atc_phrase.match_caoc_unit_for_flight(
        units,
        callsign=atc_phrase.cached_radio_callsign(cfg),
        config=cfg,
    )
    own_id = (own or {}).get("id")

    data = zone_geo.load_airports()
    key = airport_key or str(cfg.get("default_airport") or "") or next(iter(data), "")
    airport = data.get(key) if isinstance(data.get(key), dict) else {}

    elev_m = rp.field_elev_m(airport)
    zones = rp.zones(airport)

    out = []
    for unit in units:
        xz = rp.unit_xz(unit)
        if xz is None:
            continue
        lat, lon = atc_phrase.caoc_xz_to_ll(*xz)
        alt_m = _num(unit.get("altMeters"))
        # Altitude bands are drawn in feet AGL, so that is the number worth
        # reading off a track while setting one.
        height_m = None if (alt_m is None or elev_m is None) else alt_m - elev_m
        fix = rp.UnitFix(
            unit_id=str(unit.get("id") or ""),
            label="",
            along_m=0.0,
            lateral_m=0.0,
            heading_err_deg=None,
            alt_m=alt_m,
            height_m=height_m,
            speed_mps=None,
            x_m=xz[0],
            z_m=xz[1],
        )
        hit = next((z for z in zones if rp.zone_admits(z, fix, settled=False)), None)
        out.append(
            {
                "id": unit.get("id"),
                "label": atc_phrase.radio_callsign_from_caoc_unit(unit)
                or str(unit.get("unitCallsign") or unit.get("type") or "?"),
                "lat": lat,
                "lon": lon,
                "heading": _num(unit.get("headingDeg")),
                "alt_ft": None if alt_m is None else round(alt_m * rp.FT_PER_M, 1),
                "agl_ft": None if height_m is None else round(height_m * rp.FT_PER_M, 1),
                "own": unit.get("id") == own_id,
                "zone": rp.zone_label(hit) if hit else "",
                "trigger": str((hit or {}).get("trigger") or ""),
            }
        )
    return {"ok": True, "tracks": out, "count": len(out)}


def _num(val: Any, scale: float = 1.0) -> float | None:
    try:
        return round(float(val) * scale, 1)
    except (TypeError, ValueError):
        return None


def _tile(kind: str, z: str, a: str, b: str) -> tuple[bytes, str]:
    """Fetch one basemap tile, cached.

    Path segments after the kind match the upstream URL order:
    Esri uses z/y/x, OSM uses z/x/y.
    """
    source = TILE_SOURCES.get(kind)
    if not source:
        raise ValueError(f"unknown tile source {kind!r}")
    try:
        zi, ai, bi = int(z), int(a), int(b)
    except ValueError as exc:
        raise ValueError("tile coordinates must be integers") from exc
    if not (0 <= zi <= 22 and ai >= 0 and bi >= 0):
        raise ValueError("tile coordinates out of range")

    key = f"{kind}/{zi}/{ai}/{bi}"
    with _tile_cache_lock:
        hit = _tile_cache.get(key)
        if hit:
            return hit

    template, ctype = source
    if kind == "osm":
        url = template.format(z=zi, x=ai, y=bi)
    else:
        url = template.format(z=zi, y=ai, x=bi)

    req = urllib.request.Request(url, headers={"User-Agent": TILE_UA})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read()
            ctype = resp.headers.get_content_type() or ctype
    except urllib.error.HTTPError as exc:
        raise ValueError(f"upstream {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"upstream unreachable: {exc.reason}") from exc

    with _tile_cache_lock:
        if len(_tile_cache) >= _TILE_CACHE_MAX:
            # Drop an arbitrary oldest-ish entry; order is insertion order on 3.7+.
            _tile_cache.pop(next(iter(_tile_cache)), None)
        _tile_cache[key] = (body, ctype)
    return body, ctype


class Handler(BaseHTTPRequestHandler):
    server_version = "ZoneEditor/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if "/api/tracks" in str(args[0] if args else ""):
            return  # polls every second; would bury anything worth reading
        super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        airport = (query.get("airport") or [""])[0]

        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            return self._file(HERE / name, ctype)
        if url.path.startswith("/vendor/"):
            vendor = (HERE / "vendor").resolve()
            target = (vendor / url.path[len("/vendor/") :]).resolve()
            if not target.is_relative_to(vendor) or not target.is_file():
                return self._send(404, b"not found", "text/plain")
            return self._file(target, VENDOR_TYPES.get(target.suffix, "application/octet-stream"))
        if url.path.startswith("/tiles/"):
            # /tiles/<kind>/<z>/<a>/<b>  — a/b order matches the upstream template
            parts = [p for p in url.path.split("/") if p]
            if len(parts) != 5:
                return self._send(404, b"not found", "text/plain")
            _, kind, z, a, b = parts
            b = b.split(".", 1)[0]  # allow .../y.png from the OSM template
            try:
                body, ctype = _tile(kind, z, a, b)
            except ValueError as exc:
                return self._send(404, str(exc).encode("utf-8"), "text/plain")
            return self._send(200, body, ctype, cache="public, max-age=86400")
        if url.path == "/api/state":
            return self._json(_state(airport))
        if url.path == "/api/tracks":
            return self._json(_tracks(airport))
        if url.path == "/api/overlays":
            return self._json({"ok": True, "overlays": zone_overlay.list_overlays()})
        if url.path == "/api/overlay":
            name = (query.get("file") or [""])[0]
            rebuild = (query.get("rebuild") or [""])[0] in ("1", "true", "yes")
            try:
                return self._json(zone_overlay.load_overlay(name, rebuild=rebuild))
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": str(exc)}, status=400)
        if url.path == "/api/overlay/geojson":
            name = (query.get("file") or [""])[0]
            rebuild = (query.get("rebuild") or [""])[0] in ("1", "true", "yes")
            try:
                cache = zone_overlay.ensure_cache(name, rebuild=rebuild)
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": str(exc)}, status=400)
            return self._file(cache, "application/geo+json; charset=utf-8")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path not in ("/api/save", "/api/parse", "/api/overlay"):
            return self._send(404, b"not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            if url.path == "/api/parse":
                name = (parse_qs(url.query).get("filename") or [""])[0]
                return self._json(_parse(raw, name))
            if url.path == "/api/overlay":
                name = (parse_qs(url.query).get("filename") or ["overlay.kmz"])[0]
                path = zone_overlay.save_upload(name, raw)
                return self._json(zone_overlay.load_overlay(path.name, rebuild=True))
            return self._json(_save(json.loads(raw or b"{}")))
        except Exception as exc:  # noqa: BLE001 — report it in the page
            return self._json({"ok": False, "error": str(exc)}, status=400)

    # -- plumbing --------------------------------------------------------
    def _file(self, path: Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            return self._send(404, b"not found", "text/plain")
        self._send(200, body, ctype)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)


def _overlays_api_ok(port: int) -> bool:
    """True when the process on this port is a build that lists reference packs."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/overlays")
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            return int(getattr(resp, "status", 200) or 200) == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def _kill_port_listeners(port: int) -> None:
    """Stop whatever is LISTENING on the zone-editor port (Windows + POSIX)."""
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"],
                text=True,
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return
        pids: set[int] = set()
        suffix = f":{port}"
        for line in out.splitlines():
            if "LISTENING" not in line.upper():
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            if not parts[1].endswith(suffix):
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid > 0:
                pids.add(pid)
        for pid in pids:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        return
    try:
        out = subprocess.check_output(["lsof", "-ti", f"TCP:{port}"], text=True, errors="replace")
    except (OSError, subprocess.CalledProcessError):
        return
    for raw in out.split():
        try:
            os.kill(int(raw), 15)
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--airport", default="", help="airports.json key to open")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument(
        "--replace",
        action="store_true",
        help="kill whatever is already on --port, then start this copy",
    )
    args = ap.parse_args(argv)

    if not zone_geo.AIRPORTS_JSON.exists():
        print(f"no {zone_geo.AIRPORTS_JSON}", file=sys.stderr)
        return 2

    url = f"http://127.0.0.1:{args.port}/"
    if args.airport:
        url += f"?airport={args.airport}"

    def bind() -> ThreadingHTTPServer:
        return ThreadingHTTPServer(("127.0.0.1", args.port), Handler)

    try:
        httpd = bind()
    except OSError:
        # Reuse a current editor; replace a stale one that predates /api/overlays
        # (that old process leaves the Packs dropdown empty).
        if not args.replace and _overlays_api_ok(args.port):
            print(
                f"port {args.port} already in use — opening the editor that is already running"
            )
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        print(f"port {args.port} held by an old editor — replacing it")
        _kill_port_listeners(args.port)
        time.sleep(0.5)
        try:
            httpd = bind()
        except OSError as exc:
            print(f"could not bind port {args.port}: {exc}", file=sys.stderr)
            return 1

    print(f"zone editor on {url}   (writes {zone_geo.AIRPORTS_JSON})")
    print(f"reference packs: {zone_overlay.OVERLAYS_DIR}")
    print("Ctrl-C to stop")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
