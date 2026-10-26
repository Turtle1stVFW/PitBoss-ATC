"""Local DCS radar/weather snapshots replace the Opus position poll."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import atc_phrase


def _reset() -> None:
    atc_phrase._caoc_radar_cache["t"] = 0.0
    atc_phrase._caoc_radar_cache["data"] = None
    atc_phrase._caoc_radar_cache["src"] = ""
    atc_phrase._affiliation_by_unit.clear()
    atc_phrase._json_snap_cache.clear()
    atc_phrase._OPUS_CACHE.clear()
    atc_phrase._OPUS_FLIGHTS_LIST.clear()
    atc_phrase.invalidate_metar_cache()


def _cfg(**extra: object) -> dict:
    base = {
        "atc_role": "host",
        "opus_backend_url": "http://127.0.0.1:9/backend",
        "opus_metar_url": "http://127.0.0.1:9/metar?icao={icao}",
        "user_agent": "snapshot-test",
        "caoc_radar_network_min_s": 15,
    }
    base.update(extra)
    return base


def _radar(path: Path, units: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "source": "dcs",
                "missionTimeZulu": "17:06:14 Z",
                "units": units,
            }
        ),
        encoding="utf-8",
    )


def test_radar_path_accepts_profile_or_logs_folder() -> list[str]:
    fails: list[str] = []
    _reset()
    calls: list[str] = []

    def boom(url: str, _ua: str):
        calls.append(url)
        raise AssertionError(url)

    orig = atc_phrase.http_get_json
    atc_phrase.http_get_json = boom  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "455AEWDS1" / "Logs"
            logs.mkdir(parents=True)
            _radar(
                logs / "radar-snapshot.json",
                [{"id": "9", "name": "From455", "type": "air", "lat": 36.2, "lon": -115.0}],
            )
            for label, pointed in (
                ("logs", logs),
                ("profile", logs.parent),
            ):
                calls.clear()
                atc_phrase._json_snap_cache.clear()
                cfg = _cfg(radar_snapshot_path=str(pointed))
                data = atc_phrase.fetch_caoc_radar(cfg, max_age_s=0.0)
                name = ((data or {}).get("units") or [{}])[0].get("name")
                if name != "From455":
                    fails.append(f"{label} path did not load the export, got {name}")
                if calls:
                    fails.append(f"{label} path still called opus: {calls}")
    finally:
        atc_phrase.http_get_json = orig  # type: ignore[assignment]
    return fails


def test_search_finds_non_dcs_profile() -> list[str]:
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        saved = Path(tmp) / "Saved Games"
        logs = saved / "455AEWDS1" / "Logs"
        logs.mkdir(parents=True)
        snap = logs / "radar-snapshot.json"
        snap.write_text("{}", encoding="utf-8")
        decoy = saved / "SomethingElse"
        decoy.mkdir()
        found = atc_phrase._snapshot_files_under(saved, "radar-snapshot.json")
        if found != [snap]:
            fails.append(f"expected only 455AEWDS1, got {found}")
    return fails


def test_fresh_radar_skips_opus() -> list[str]:
    fails: list[str] = []
    _reset()
    calls: list[str] = []

    def boom(url: str, _ua: str):
        calls.append(url)
        raise AssertionError(url)

    orig = atc_phrase.http_get_json
    atc_phrase.http_get_json = boom  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "radar-snapshot.json"
            _radar(
                path,
                [
                    {
                        "id": "1",
                        "name": "Turtle",
                        "pilotName": "Turtle",
                        "groupName": "GOLF 3-7",
                        "type": "air",
                        "lat": 36.24,
                        "lon": -115.03,
                        "objectName": "F-16C_50",
                    }
                ],
            )
            cfg = _cfg(radar_snapshot_path=str(path))
            data = atc_phrase.fetch_caoc_radar(cfg, max_age_s=0.0)
            if not data or data.get("units", [{}])[0].get("pilotName") != "Turtle":
                fails.append(f"local radar not used: {data}")
            if calls:
                fails.append(f"opus radar was fetched: {calls}")
            atc_phrase._affiliation_by_unit["1"] = "BANDIT"
            again = atc_phrase.fetch_caoc_radar(cfg, max_age_s=0.0)
            unit = (again or {}).get("units", [{}])[0]
            if unit.get("affiliation") != "BANDIT":
                fails.append(f"affiliation overlay missing: {unit}")
            if calls:
                fails.append(f"second read hit opus: {calls}")
    finally:
        atc_phrase.http_get_json = orig  # type: ignore[assignment]
    return fails


def test_stale_radar_uses_network_floor() -> list[str]:
    fails: list[str] = []
    _reset()
    calls: list[str] = []

    def fake(url: str, _ua: str):
        calls.append(url)
        return {"units": [{"id": "net", "name": "FROM-OPUS", "type": "air"}]}

    orig = atc_phrase.http_get_json
    atc_phrase.http_get_json = fake  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "radar-snapshot.json"
            _radar(path, [{"id": "old", "name": "STALE", "type": "air"}])
            os.utime(path, (time.time() - 120, time.time() - 120))
            cfg = _cfg(radar_snapshot_path=str(path))
            data = atc_phrase.fetch_caoc_radar(cfg, max_age_s=0.0)
            name = ((data or {}).get("units") or [{}])[0].get("name")
            if name != "FROM-OPUS":
                fails.append(f"stale file should fall back to opus, got {name}")
            atc_phrase.fetch_caoc_radar(cfg, max_age_s=0.0)
            if len(calls) != 1:
                fails.append(f"host should not re-pull radar inside 15s, calls={len(calls)}")
    finally:
        atc_phrase.http_get_json = orig  # type: ignore[assignment]
    return fails


def test_client_does_not_auto_read_saved_games() -> list[str]:
    fails: list[str] = []
    _reset()
    calls: list[str] = []

    def fake(url: str, _ua: str):
        calls.append(url)
        return {"units": [{"id": "net", "name": "OPUS", "type": "air"}]}

    orig_http = atc_phrase.http_get_json
    orig_search = atc_phrase._snapshot_search_paths
    atc_phrase.http_get_json = fake  # type: ignore[assignment]

    def search(_filename: str) -> list[Path]:
        return [fresh]

    atc_phrase._snapshot_search_paths = search  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "radar-snapshot.json"
            _radar(fresh, [{"id": "local", "name": "LOCAL", "type": "air"}])
            cfg = _cfg(atc_role="client")
            data = atc_phrase.fetch_caoc_radar(cfg, max_age_s=0.0)
            name = ((data or {}).get("units") or [{}])[0].get("name")
            if name != "OPUS":
                fails.append(f"client should keep opus radar, got {name}")
            if not calls:
                fails.append("client made no opus request")
    finally:
        atc_phrase.http_get_json = orig_http  # type: ignore[assignment]
        atc_phrase._snapshot_search_paths = orig_search  # type: ignore[assignment]
    return fails


def test_metar_cached_for_half_hour() -> list[str]:
    fails: list[str] = []
    _reset()
    calls: list[str] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"metar":{"raw":"KLSV 261853Z 21008KT 10SM CLR 32/00 A2992"}}'

    def fake_open(_req: object, timeout: float = 10) -> _Resp:
        del timeout
        calls.append("metar")
        return _Resp()

    orig = atc_phrase.urllib.request.urlopen
    atc_phrase.urllib.request.urlopen = fake_open  # type: ignore[assignment]
    try:
        cfg = _cfg()
        first = atc_phrase.fetch_metar(cfg, "KLSV")
        second = atc_phrase.fetch_metar(cfg, "KLSV")
        if first.wind_dir != 210 or first.wind_speed_kt != 8:
            fails.append(f"metar parse {first.wind_dir}/{first.wind_speed_kt}")
        if second.raw != first.raw:
            fails.append("second metar was not the cached copy")
        if len(calls) != 1:
            fails.append(f"expected one metar GET, got {len(calls)}")
        slot = next(iter(atc_phrase._METAR_CACHE.values()), {})
        remain = float(slot.get("exp") or 0) - time.time()
        if not (1700 <= remain <= 1900):
            fails.append(f"metar cache should last ~30 min, remain={remain:.0f}s")
    finally:
        atc_phrase.urllib.request.urlopen = orig  # type: ignore[assignment]
    return fails


def test_affiliations_only_when_requested() -> list[str]:
    fails: list[str] = []
    _reset()
    calls: list[str] = []

    def fake(url: str, _ua: str):
        calls.append(url)
        return {"units": [{"id": "1", "affiliation": "HOSTILE", "name": "OPUS"}]}

    orig = atc_phrase.http_get_json
    atc_phrase.http_get_json = fake  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "radar-snapshot.json"
            _radar(
                path,
                [
                    {
                        "id": "1",
                        "name": "Turtle",
                        "type": "air",
                        "lat": 36.24,
                        "lon": -115.03,
                    }
                ],
            )
            cfg = _cfg(radar_snapshot_path=str(path))
            quiet = atc_phrase.fetch_caoc_radar(cfg, max_age_s=0.0)
            if calls:
                fails.append(f"position read pulled opus: {calls}")
            if ((quiet or {}).get("units") or [{}])[0].get("name") != "Turtle":
                fails.append("local track was replaced")
            asked = atc_phrase.fetch_caoc_radar(cfg, affiliations=True)
            unit = ((asked or {}).get("units") or [{}])[0]
            if unit.get("name") != "Turtle":
                fails.append(f"picture pull replaced the local track: {unit}")
            if unit.get("affiliation") != "HOSTILE":
                fails.append(f"affiliation not copied: {unit}")
            if len(calls) != 1:
                fails.append(f"expected one picture pull, got {calls}")
            atc_phrase.fetch_caoc_radar(cfg, affiliations=True)
            if len(calls) != 2:
                fails.append(f"a later picture call should pull again, got {len(calls)}")
    finally:
        atc_phrase.http_get_json = orig  # type: ignore[assignment]
    return fails


def test_flight_cache_keeps_every_seat() -> list[str]:
    fails: list[str] = []
    _reset()
    calls: list[str] = []

    def fake(url: str, _ua: str):
        calls.append(url)
        return [{"id": 1, "callsign": "GOLF"}, {"id": 2, "callsign": "VIPER"}]

    orig = atc_phrase.http_get_json
    atc_phrase.http_get_json = fake  # type: ignore[assignment]
    try:
        cfg_a = _cfg(opus_flight_id=1, opus_flight_cache_s=180)
        cfg_b = _cfg(opus_flight_id=2, opus_flight_cache_s=180)
        golf = atc_phrase.synthetic_flight_context("GOLF 3")
        viper = atc_phrase.synthetic_flight_context("VIPER 1")
        atc_phrase._opus_cache_put(cfg_a, golf)
        atc_phrase._opus_cache_put(cfg_b, viper)
        if atc_phrase.cached_radio_callsign(cfg_a) != "GOLF 3":
            fails.append("golf callsign evicted")
        if atc_phrase.cached_radio_callsign(cfg_b) != "VIPER 1":
            fails.append("viper callsign evicted")
        rows = atc_phrase._cached_opus_flights(cfg_a)
        again = atc_phrase._cached_opus_flights(cfg_b)
        if rows is not again:
            fails.append("flights list was downloaded twice")
        if len(calls) != 1:
            fails.append(f"expected one flights-list GET, got {calls}")
        found = atc_phrase._opus_flights_list_row(cfg_a, 2)
        if not found or found.get("callsign") != "VIPER":
            fails.append(f"list row lookup failed: {found}")
        if len(calls) != 1:
            fails.append(f"list row refetched the catalog: {calls}")
    finally:
        atc_phrase.http_get_json = orig  # type: ignore[assignment]
    return fails


def main() -> int:
    bad = 0
    for fn in (
        test_radar_path_accepts_profile_or_logs_folder,
        test_search_finds_non_dcs_profile,
        test_fresh_radar_skips_opus,
        test_stale_radar_uses_network_floor,
        test_client_does_not_auto_read_saved_games,
        test_metar_cached_for_half_hour,
        test_affiliations_only_when_requested,
        test_flight_cache_keeps_every_seat,
    ):
        fails = fn()
        if fails:
            bad += 1
            print(f"FAIL {fn.__name__}")
            for line in fails:
                print(f"  {line}")
        else:
            print(f"ok   {fn.__name__}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
