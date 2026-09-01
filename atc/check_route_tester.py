"""
Synthetic checks for the offline route tester (no DCS, no HTTP).

    py -3 check_route_tester.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, OSError):
    pass

import agencies  # noqa: E402
import atc_phrase  # noqa: E402
import flow_engine  # noqa: E402
import route_tester as rt  # noqa: E402

AIRPORTS = atc_phrase.load_json(flow_engine.AIRPORTS_PATH)
NELLIS = AIRPORTS["nellis"]
STATE = flow_engine.STATE_PATH
CFG = {
    "callsign_override": "FLEECE 1",
    "auto_clearance_dwell_s": 0.0,
    "auto_monitor_tower": True,
    "auto_takeoff_clearance": True,
    "flow_file": "flows/nellis_default.json",
}


def show(title: str) -> None:
    print(f"\n-- {title}")


def main() -> int:
    bad = 0
    mtime_before = STATE.stat().st_mtime if STATE.is_file() else None

    show("fix catalog")
    catalog = rt.load_fix_catalog(NELLIS, AIRPORTS)
    for tok in ("KLSV", "SARAH", "ARCOE", "DREAM", "BTY", "EDW"):
        hit = catalog.get(tok)
        ok = hit is not None and hit.get("lat") is not None
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {tok} -> {hit}")
    klsv = catalog.get("KLSV") or {}
    ok = abs(float(klsv.get("lat") or 0) - 36.24) < 0.05
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} KLSV is at the field ({klsv.get('lat')}, {klsv.get('lon')})")
    dream = catalog.get("DREAM") or {}
    ok = abs(float(dream.get("lat") or 0) - 37.172) < 0.02
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} DREAM is the NTTR point ({dream.get('lat')}, {dream.get('lon')})")

    show("route plot")
    plotted = rt.resolve_route("KLSV FLEX DREAM SARAH KLSV", catalog)
    ids = [w["id"] for w in plotted["waypoints"]]
    ok = "KLSV" in ids and "SARAH" in ids and "DREAM" in ids
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} waypoints {ids} unknown {plotted['unknown']}")
    dream_wp = next((w for w in plotted["waypoints"] if w["id"] == "DREAM"), None)
    ok = dream_wp is not None and abs(float(dream_wp["lat"]) - 37.172) < 0.02
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} plotted DREAM at {None if not dream_wp else (dream_wp['lat'], dream_wp['lon'])}")
    hop = agencies.format_hop(agencies.infer_flight(route="KLSV FLEX DREAM SARAH KLSV", airport=NELLIS))
    ok = "Blackjack" in hop and "Nellis Control" in hop
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} hop {hop}")

    r2508 = agencies.infer_flight(route="KLSV BTY EDW", airport=NELLIS)
    hop2 = agencies.format_hop(r2508)
    ok = r2508.going_r2508 and "Joshua" in hop2 and "LA Center" in hop2
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} R-2508 hop {hop2}")

    def tick_at(lat: float, lon: float, **extra: object) -> dict:
        sess = extra.pop("sess", None) or rt.TesterSession()
        body = {
            "airport": "nellis",
            "lat": lat,
            "lon": lon,
            "alt_ft_agl": extra.pop("alt_ft_agl", 1000),
            "heading_deg": extra.pop("heading_deg", 210),
            "speed_kt": extra.pop("speed_kt", 0),
            "route": extra.pop("route", "KLSV FLEX DREAM SARAH KLSV"),
            "reset": extra.pop("reset", True),
        }
        body.update(extra)
        return rt.tick(body, airports=AIRPORTS, config=CFG, sess=sess, now=1000.0)

    show("tower circle")
    out = tick_at(36.235, -115.038, alt_ft_agl=1000, speed_kt=200)
    trigs = [z["trigger"] for z in out["inside"]]
    ok = "tower" in trigs
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} tower in {trigs} owner={out['owner']}")

    show("EOR settled")
    sess = rt.TesterSession()
    out = tick_at(
        36.24724,
        -115.02877,
        alt_ft_agl=0,
        speed_kt=0,
        heading_deg=210,
        sess=sess,
        reset=True,
    )
    trigs = [z["trigger"] for z in out["inside"]]
    ok = "eor" in trigs
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} eor in {trigs}")
    ok = out.get("runway_hdg") is not None
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} runway_hdg={out.get('runway_hdg')}")
    armed = [s["id"] for s in out["armed"] if s["armed"]]
    waiting = {s["id"]: s.get("waiting") for s in out["armed"] if s["id"] == "gnd_monitor_tower"}
    ok = "gnd_monitor_tower" in armed
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} monitor tower armed {armed} wait={waiting}")
    phrases = [e.get("phrase") for e in out["events"] if e.get("kind") == "armed"]
    ok = any(phrases)
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} armed phrase: {(phrases[:1] or ['(none)'])[0][:80]}")

    show("in position")
    out = tick_at(36.24408, -115.02795, alt_ft_agl=0, speed_kt=0, heading_deg=210)
    trigs = [z["trigger"] for z in out["inside"]]
    ok = "in_position" in trigs
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} in_position in {trigs}")

    show("Control West")
    out = tick_at(37.60, -116.00, alt_ft_agl=18000, speed_kt=400)
    ok = "control_west" in out["agencies"] and out["sector"] == "control_west"
    bad += 0 if ok else 1
    print(
        f"{'ok  ' if ok else 'FAIL'} agencies={out['agencies']} sector={out['sector']} "
        f"owner={out['owner']}"
    )

    show("approaching Joshua")
    out = tick_at(
        34.905,
        -117.884,
        alt_ft_agl=18000,
        speed_kt=400,
        route="KLSV BTY EDW",
        tune="center",
    )
    ok = out["near_joshua"] and out["next_handoff"] == "joshua"
    bad += 0 if ok else 1
    print(
        f"{'ok  ' if ok else 'FAIL'} near_joshua={out['near_joshua']} "
        f"handoff={out['next_handoff']} joshua_nm={out['joshua_nm']}"
    )

    show("weather")
    wx = rt.weather_from_body({"metar": "KLSV 05015KT 10SM SKC A3011"})
    ok = wx.wind_dir == 50 and wx.wind_speed_kt == 15 and abs((wx.altimeter_inhg or 0) - 30.11) < 0.01
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} METAR wind={wx.wind_dir}/{wx.wind_speed_kt} A{wx.altimeter_inhg}")
    wx2 = rt.weather_from_body({"wind_dir": 360, "wind_speed_kt": 12, "altimeter_inhg": 29.85})
    ok = wx2.wind_dir == 360 and wx2.wind_speed_kt == 12
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} discrete wind={wx2.wind_dir}/{wx2.wind_speed_kt}")
    out_wx = tick_at(
        36.235,
        -115.038,
        metar="KLSV 05015KT 10SM SKC A3011",
        reset=True,
    )
    got = (out_wx.get("weather") or {}).get("wind_dir")
    ok = got == 50
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} tick weather wind_dir={got}")

    show("weather inject")
    atc_phrase.clear_weather_inject()
    try:
        atc_phrase.write_weather_inject(
            wind_dir=30,
            wind_speed_kt=12,
            altimeter_inhg=29.85,
            visibility_sm=3.0,
            ceiling_ft=800,
            mission_hhmm="2300",
        )
        inj = atc_phrase.read_weather_inject()
        ok = inj is not None and inj.get("wind_dir") == 30
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} weather inject file wind={None if not inj else inj.get('wind_dir')}")
        wx = atc_phrase.fetch_metar(CFG, "KLSV")
        ok = (
            wx.wind_dir == 30
            and wx.wind_speed_kt == 12
            and abs((wx.altimeter_inhg or 0) - 29.85) < 0.01
            and wx.ceiling_ft == 800
        )
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} fetch_metar inject {wx.wind_dir}/{wx.wind_speed_kt} A{wx.altimeter_inhg} cig={wx.ceiling_ft}")
        minutes = atc_phrase.caoc_mission_local_minutes(CFG, radar={"missionTimeZulu": "13:00:00 Z"})
        ok = minutes == 23 * 60
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} mission clock inject={minutes}")
        spoken = atc_phrase.speak_zulu_now()
        ok = spoken == atc_phrase.speak_zulu_clock("2300")
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} speak_zulu_now={spoken!r}")
        night = tick_at(
            36.235,
            -115.038,
            override_weather=True,
            wind_dir=210,
            wind_speed_kt=5,
            metar="",
            mission_hhmm="2300",
            reset=True,
        )
        rwy = str(night.get("runway") or "")
        ok = rwy.startswith("03")
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} night calm-wind dep runway={rwy}")
        off = tick_at(
            36.235,
            -115.038,
            override_weather=False,
            reset=False,
        )
        ok = atc_phrase.read_weather_inject() is None
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} override off clears inject (tick ok={off.get('ok')})")
        opus_clock = atc_phrase.caoc_mission_local_minutes(
            None, radar={"missionTimeZulu": "13:00:00 Z"}
        )
        ok = opus_clock == 13 * 60
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} CAOC clock after clear={opus_clock}")
    finally:
        atc_phrase.clear_weather_inject()

    show("radio call")
    sess = rt.TesterSession()
    say_body = {
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
    said = rt.handle_say(say_body, airports=AIRPORTS, config=CFG, sess=sess)
    ok = said.get("ok") and said.get("fired") and said.get("intent") == "ready_taxi" and bool(said.get("phrase"))
    bad += 0 if ok else 1
    print(
        f"{'ok  ' if ok else 'FAIL'} taxi fired={said.get('fired')} "
        f"intent={said.get('intent')!r} phrase={(said.get('phrase') or '')[:70]!r}"
    )

    winds_body = dict(say_body)
    winds_body.update(
        {
            "tune": "approach",
            "transcript": "Nellis Approach, Fleece 1, say winds",
            "metar": "KLSV 05015KT 10SM SKC A3011",
            "reset": False,
        }
    )
    winds = rt.handle_say(winds_body, airports=AIRPORTS, config=CFG, sess=sess)
    phrase = winds.get("phrase") or ""
    ok = (
        winds.get("ok")
        and winds.get("intent") == "request_winds"
        and "wind" in phrase.lower()
        and "zero fife zero" in phrase.lower()
    )
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} winds intent={winds.get('intent')!r} phrase={phrase[:80]!r}")

    show("map inject")
    map_cfg = dict(CFG)
    map_cfg["ownship_from_map"] = True
    opus_cfg = dict(CFG)
    opus_cfg["ownship_from_map"] = False
    atc_phrase.clear_ownship_inject()
    try:
        atc_phrase.write_ownship_inject(
            lat=36.235,
            lon=-115.038,
            alt_ft_agl=0,
            heading_deg=210,
            speed_kt=0,
            callsign="FLEECE 1",
            airport=NELLIS,
            fp_route_string="KLSV FLEX DREAM SARAH KLSV",
            fp_altitude="FL240",
        )
        inj = atc_phrase.read_ownship_inject()
        ok = inj is not None and abs(float(inj["lat"]) - 36.235) < 1e-6
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} inject file lat={None if not inj else inj.get('lat')}")
        radar = atc_phrase.merge_ownship_inject(None, map_cfg)
        own = atc_phrase.match_caoc_unit_for_flight(
            (radar or {}).get("units") or [], config=map_cfg
        )
        ok = bool(own) and own.get("id") == atc_phrase.MAP_OWNSHIP_ID
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} inject unit id={None if not own else own.get('id')}")
        ll = atc_phrase.ownship_latlon(map_cfg)
        ok = ll is not None and abs(ll[0] - 36.235) < 1e-6
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} ownship_latlon={ll}")
        ctx = atc_phrase.resolve_active_opus_flight(map_cfg)
        ok = (
            ctx is not None
            and "FLEX" in str(ctx.fp_route_string or "")
            and str(ctx.fp_altitude or "") == "FL240"
            and str(ctx.radio_callsign) == "FLEECE 1"
        )
        bad += 0 if ok else 1
        print(
            f"{'ok  ' if ok else 'FAIL'} map flight "
            f"cs={None if not ctx else ctx.radio_callsign} "
            f"route={None if not ctx else ctx.fp_route_string} "
            f"alt={None if not ctx else ctx.fp_altitude}"
        )
        ghost = {
            "id": "dcs-fleece",
            "type": "air",
            "name": "FLEECE 1",
            "xMeters": 1,
            "zMeters": 1,
            "altMeters": 1000,
        }
        merged = atc_phrase.merge_ownship_inject({"units": [ghost]}, map_cfg)
        ids = [u.get("id") for u in (merged or {}).get("units") or []]
        ok = atc_phrase.MAP_OWNSHIP_ID in ids and "dcs-fleece" not in ids
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} CAOC ownship replaced, tracks={ids}")
        ignored = atc_phrase.merge_ownship_inject({"units": [ghost]}, opus_cfg)
        ids_off = [u.get("id") for u in (ignored or {}).get("units") or []]
        ok = (
            atc_phrase.read_ownship_inject(config=opus_cfg) is None
            and atc_phrase.MAP_OWNSHIP_ID not in ids_off
            and "dcs-fleece" in ids_off
        )
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} Opus mode ignores inject, tracks={ids_off}")
        raw = json.loads(atc_phrase.OWNSHIP_INJECT_PATH.read_text(encoding="utf-8"))
        raw["t"] = time.time() - 60.0
        atc_phrase.OWNSHIP_INJECT_PATH.write_text(json.dumps(raw), encoding="utf-8")
        stale_on = atc_phrase.read_ownship_inject(config=map_cfg)
        stale_off = atc_phrase.read_ownship_inject()
        ok = stale_on is not None and stale_off is None
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} stale inject kept only when map-ownship is on")
    finally:
        atc_phrase.clear_ownship_inject()
    ok = atc_phrase.read_ownship_inject() is None
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} inject cleared")

    show("traffic inject")
    map_cfg = dict(CFG)
    map_cfg["ownship_from_map"] = True
    opus_cfg = dict(CFG)
    opus_cfg["ownship_from_map"] = False
    atc_phrase.clear_traffic_inject()
    try:
        atc_phrase.write_traffic_inject(
            [
                {
                    "id": "map-traffic-1",
                    "lat": 37.17,
                    "lon": -114.99,
                    "alt_ft_agl": 20000,
                    "heading_deg": 180,
                    "speed_kt": 420,
                    "coalition": "red",
                    "callsign": "BANDIT 1",
                },
                {
                    "id": "map-traffic-2",
                    "lat": 36.8,
                    "lon": -115.2,
                    "alt_ft_agl": 18000,
                    "heading_deg": 90,
                    "speed_kt": 400,
                    "coalition": "blue",
                    "callsign": "FRIENDLY 1",
                },
            ],
            airport=NELLIS,
        )
        radar = atc_phrase.merge_traffic_inject({"units": []}, map_cfg)
        units = (radar or {}).get("units") or []
        red = next((u for u in units if u.get("coalition") == "red"), None)
        blue = next((u for u in units if u.get("coalition") == "blue"), None)
        ok = (
            red is not None
            and "BANDIT" in str(red.get("name") or "")
            and blue is not None
            and "FRIENDLY" in str(blue.get("name") or "")
        )
        bad += 0 if ok else 1
        print(
            f"{'ok  ' if ok else 'FAIL'} traffic units "
            f"red={None if not red else red.get('name')} "
            f"blue={None if not blue else blue.get('name')}"
        )
        air = atc_phrase.caoc_air_units(units)
        ok = len(air) == 2
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} picture-eligible air tracks={len(air)}")
        ignored = atc_phrase.merge_traffic_inject({"units": []}, opus_cfg)
        ok = not ((ignored or {}).get("units") or [])
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} Opus mode ignores tester traffic")
    finally:
        atc_phrase.clear_traffic_inject()
    ok = atc_phrase.read_traffic_inject() == []
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} traffic inject cleared")

    show("no flow_state write")
    mtime_after = STATE.stat().st_mtime if STATE.is_file() else None
    ok = mtime_before == mtime_after
    bad += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} flow_state.json mtime unchanged")

    print("\n" + ("all good" if not bad else f"{bad} problem(s)"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
