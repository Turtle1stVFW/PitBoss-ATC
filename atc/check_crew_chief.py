#!/usr/bin/env python3
"""Offline Virtual Crew Chief tests — injected snapshots, no DCS / SRS."""

from __future__ import annotations

import time
from typing import Any

import crew_chief
import crew_chief_export
from crew_chief_export import AircraftState


def _snap(
    *,
    on_ground: bool = True,
    unit: str = "F-16C_50",
    fresh: bool = True,
    args: dict[int, float] | None = None,
    elevator: float = 0.0,
    aileron: float = 0.0,
    rudder: float = 0.0,
    speedbrakes: float = 0.0,
    wheelbrakes: float = 0.0,
    agl: float = 0.2,
    ias: float = 0.0,
) -> AircraftState:
    return AircraftState(
        source="inject",
        unit=unit,
        on_ground=on_ground,
        agl_m=agl,
        ias_mps=ias,
        mech={
            "elevator": [elevator, elevator],
            "aileron": [aileron, aileron],
            "rudder": [rudder, rudder],
            "speedbrakes": speedbrakes,
            "gear": 1.0,
            "canopy": 1.0,
            "wheelbrakes": wheelbrakes,
        },
        args=dict(args or {}),
        age_s=0.0,
        fresh=fresh,
    )


CFG = {"crew_chief_enabled": True, "dry_run": True}


def _engine(state: dict[str, Any] | None = None) -> Any:
    class E:
        pass

    e = E()
    e.config = dict(CFG)
    e.state = state if state is not None else {}
    return e


def _ok(cond: bool, label: str) -> int:
    print(f"{'ok  ' if cond else 'FAIL'} {label}")
    return 0 if cond else 1


def main() -> int:
    fails = 0
    crew_chief.reset_catalog_cache()

    m = crew_chief.match_voice("Nellis Ground ready to taxi")
    fails += _ok(m is None, "ATC taxi is not a VCC intent")

    m = crew_chief.match_voice("Hey Chief")
    fails += _ok(m is not None and m.intent == "connect", "hey chief starts listening")
    m = crew_chief.match_voice("Chief")
    fails += _ok(m is not None and m.intent == "connect", "chief starts listening")
    m = crew_chief.match_voice("connected")
    fails += _ok(m is not None and m.intent == "trim_conn", "connected is trim, not ICS")
    m = crew_chief.match_voice("ok disconnected")
    fails += _ok(m is not None and m.intent == "trim_disc", "disconnected is trim disc")
    m = crew_chief.match_voice("Check No Movement")
    fails += _ok(m is not None and m.intent == "trim_disc", "check no movement")
    m = crew_chief.match_voice("Hey Chief. How do you hear me?")
    fails += _ok(m is not None and m.intent == "radio_check", "how hear kneeboard")
    m = crew_chief.match_voice("Clear for start 2?")
    fails += _ok(m is not None and m.stage_id == "start", "clear for start 2")
    m = crew_chief.match_voice("You're cleared off")
    fails += _ok(m is not None and m.intent == "cleared_off", "cleared off")
    m = crew_chief.match_voice("ready trim")
    fails += _ok(m is not None and m.stage_id == "trim", "ready trim jumps trim")
    m = crew_chief.match_voice("skip bit")
    fails += _ok(m is not None and m.intent == "skip_named" and m.stage_id == "bit", "skip bit")
    m = crew_chief.match_voice("skip this")
    fails += _ok(m is not None and m.intent == "skip", "skip this")
    m = crew_chief.match_voice("standby")
    fails += _ok(m is not None and m.intent == "stop_listen", "standby stops listening")
    m = crew_chief.match_voice("Disregard Chief")
    fails += _ok(m is not None and m.intent == "stop_listen", "disregard chief")
    m = crew_chief.match_voice("How are you doing today Chief?")
    fails += _ok(m is not None and m.intent == "small_talk", "bonus how are you")
    m = crew_chief.match_voice("Flight Controls still Clear?")
    fails += _ok(m is not None and m.stage_id == "dbu", "still clear is DBU")
    m = crew_chief.match_voice("Flight Controls Clear?")
    fails += _ok(m is not None and m.stage_id == "sec", "fc clear after SEC")

    import voice_engine as voice_engine_mod

    ics, note = voice_engine_mod.resolve_ics_ptt({}, have_key=False)
    fails += _ok(not ics and "intercom" in note.lower(), "ICS PTT never falls back to SRS")
    ics, note = voice_engine_mod.resolve_ics_ptt(
        {"crew_chief_ptt": [{"kind": "mouse", "button": 4}]}, have_key=False
    )
    fails += _ok(len(ics) == 1 and not note, "ICS PTT from config")
    clash = voice_engine_mod.ics_overlaps_radio_ptt(
        [{"kind": "mouse", "button": 4}],
        "",
        [{"kind": "mouse", "button": 4}],
        "",
    )
    fails += _ok(bool(clash) and "SRS" in clash, "warn when ICS PTT is the radio PTT")
    no_clash = voice_engine_mod.ics_overlaps_radio_ptt(
        [{"kind": "mouse", "button": 5}],
        "F15",
        [{"kind": "mouse", "button": 4}],
        "F13",
    )
    fails += _ok(not no_clash, "spare ICS button is not a clash")

    engine = _engine()
    air = _snap()
    silent_conn = crew_chief.execute(
        engine, crew_chief.VoiceMatch("trim_conn", "trim"), air, play=False
    )
    fails += _ok(silent_conn.get("action") == "none", "connected does not start ICS")
    r = crew_chief.execute(
        engine, crew_chief.VoiceMatch("connect"), air, play=False
    )
    fails += _ok(r.get("action") == "crew_chief", "connect speaks")
    fails += _ok(crew_chief.ics_connected(engine.state), "ICS up")

    hear = _engine()
    hear_r = crew_chief.execute(
        hear,
        crew_chief.match_voice("Hey Chief. How do you hear me?"),
        air,
        play=False,
    )
    fails += _ok(hear_r.get("action") == "crew_chief", "how hear auto-connects")
    fails += _ok(crew_chief.ics_connected(hear.state), "how hear ICS up")
    fails += _ok(
        crew_chief._status_of(crew_chief.get_state(hear.state), "radio") == "done",
        "radio check completes",
    )

    airborne = _snap(on_ground=False, agl=200, ias=80)
    silent = crew_chief.execute(
        engine, crew_chief.VoiceMatch("ready_trim", "trim"), airborne, play=False
    )
    fails += _ok(silent.get("action") == "none", "airborne — silent")

    engine2 = _engine()
    dead = crew_chief.execute(
        engine2, crew_chief.VoiceMatch("ready_trim", "trim"), air, play=False
    )
    fails += _ok(dead.get("action") == "none", "disconnected — no stage")

    crew_chief.execute(engine, crew_chief.VoiceMatch("ready_trim", "trim"), air, play=False)
    fails += _ok(
        crew_chief.get_state(engine.state).get("active_stage") == "trim",
        "jump trim before SEC",
    )

    crew_chief.execute(engine, crew_chief.VoiceMatch("ready_big", "big"), air, play=False)
    # Stick NU via LoGetMechInfo (the MP path), not cockpit arg 736
    ticked = crew_chief.tick(engine.state, CFG, _snap(elevator=0.8), now=time.time())
    lines = [e.line_id for e in ticked]
    fails += _ok("nose_up" in lines, "elevator NU from mech surfaces")

    # Jump mid-wait to SEC; big left unfinished (not done)
    crew_chief.execute(engine, crew_chief.VoiceMatch("ready_sec", "sec"), air, play=False)
    st = crew_chief.get_state(engine.state)
    fails += _ok(st.get("active_stage") == "sec", "jump mid-wait to SEC")
    fails += _ok(crew_chief._status_of(st, "big") != "done", "big left unfinished")

    crew_chief.execute(
        engine, crew_chief.VoiceMatch("skip_named", "bit"), air, play=False
    )
    fails += _ok(
        crew_chief._status_of(crew_chief.get_state(engine.state), "bit") == "skipped",
        "skip named bit",
    )

    # Redo a done stage
    crew_chief.execute(engine, crew_chief.VoiceMatch("ready_bit", "bit"), air, play=False)
    # bit has no ticks → completes immediately
    fails += _ok(
        crew_chief._status_of(crew_chief.get_state(engine.state), "bit") == "done",
        "bit completes",
    )
    crew_chief.execute(engine, crew_chief.VoiceMatch("ready_bit", "bit"), air, play=False)
    fails += _ok(
        crew_chief._status_of(crew_chief.get_state(engine.state), "bit") == "done",
        "redo bit still completes",
    )

    # Timeout no-movement on big
    crew_chief.execute(engine, crew_chief.VoiceMatch("ready_big", "big"), air, play=False)
    later = time.time() + 20
    timed = crew_chief.tick(engine.state, CFG, air, now=later)
    fails += _ok(
        any(e.line_id == "no_movement" for e in timed),
        "no movement timeout",
    )

    # Aileron from args (SP-style) as well
    crew_chief.reset(engine.state)
    crew_chief.connect(engine.state, CFG, air)
    crew_chief.jump(engine.state, "big", CFG, air)
    roll = crew_chief.tick(
        engine.state, CFG, _snap(args={737: 0.95}), now=time.time()
    )
    fails += _ok(
        any(e.line_id == "roll_right" for e in roll),
        "aileron RR from arg 737",
    )

    # Off feature
    off = _engine()
    off.config["crew_chief_enabled"] = False
    nope = crew_chief.execute(off, crew_chief.VoiceMatch("connect"), air, play=False)
    fails += _ok(nope.get("action") == "none", "disabled feature is silent")

    # Parse helper
    parsed = crew_chief_export.parse_aircraft_payload(
        {
            "t": time.time(),
            "unit": "F-16C_50",
            "on_ground": True,
            "agl_m": 0.4,
            "ias_mps": 1.0,
            "mech": {"elevator": [-0.2, -0.2], "aileron": [0.1, 0.1], "rudder": [0, 0]},
            "args": {"736": 0.9},
        },
        mtime=time.time(),
    )
    parsed.fresh = True
    fails += _ok(parsed.is_f16 and parsed.on_ground, "payload parse F-16 on ground")
    fails += _ok(parsed.args.get(736) == 0.9, "payload arg 736")

    status = crew_chief.fly_status_line(CFG, engine.state, air)
    fails += _ok("ICS connected" in status, f"fly status: {status}")

    helper = crew_chief.checklist_helper(CFG, engine.state, air)
    fails += _ok(helper.get("ics") is True, "checklist helper ICS up")
    ids = [str(r.get("id")) for r in helper.get("stages") or []]
    fails += _ok("trim" in ids and "big" in ids, "checklist lists stages")
    fails += _ok(
        crew_chief.say_phrase_for_stage("trim") == "Bit Passed. Ready for trim check?",
        "say phrase for trim",
    )
    script_says = [str(r.get("say") or "") for r in helper.get("script") or []]
    fails += _ok(
        any("How do you hear me" in s for s in script_says),
        "helper includes kneeboard how-hear",
    )
    fails += _ok(
        any("cleared off" in s.lower() for s in script_says),
        "helper includes cleared off",
    )
    fp1 = crew_chief.checklist_fingerprint(helper)
    fp2 = crew_chief.checklist_fingerprint(helper)
    fails += _ok(fp1 == fp2 and bool(fp1), "checklist fingerprint stable")

    crew_chief.execute(engine, crew_chief.VoiceMatch("trim_disc", "trim"), air, play=False)
    fails += _ok(
        "disc" in crew_chief._done_ticks(crew_chief.get_state(engine.state), "trim"),
        "voice disconnected marks trim disc",
    )
    crew_chief.execute(engine, crew_chief.VoiceMatch("stop_listen"), air, play=False)
    fails += _ok(not crew_chief.ics_connected(engine.state), "standby stops listening")

    if fails:
        print(f"{fails} FAIL")
        return 1
    print("all ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
