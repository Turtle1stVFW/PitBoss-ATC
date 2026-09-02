"""
Virtual Crew Chief — skippable, reorderable F-16 ground stages.

Hard gates: feature enabled, ICS connected, on the ground, F-16C.
Does not transmit on squadron SRS. Audio is local-only.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import crew_chief_audio
import crew_chief_export
from crew_chief_export import AircraftState

HERE = Path(__file__).resolve().parent
STAGES_PATH = HERE / "crew_chief_f16.json"
STATE_KEY = "crew_chief"

_STAGES_CACHE: dict[str, Any] | None = None


@dataclass(frozen=True)
class VoiceMatch:
    intent: str
    stage_id: str = ""
    transcript: str = ""
    confidence: float = 0.9


@dataclass
class Event:
    line_id: str = ""
    detail: str = ""
    action: str = "speak"


def enabled(config: dict[str, Any] | None) -> bool:
    return bool((config or {}).get("crew_chief_enabled"))


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    global _STAGES_CACHE
    src = path or STAGES_PATH
    if _STAGES_CACHE is not None and path is None:
        return _STAGES_CACHE
    data = json.loads(src.read_text(encoding="utf-8"))
    if path is None:
        _STAGES_CACHE = data
    return data


def reset_catalog_cache() -> None:
    global _STAGES_CACHE
    _STAGES_CACHE = None


def stage_defs(catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = catalog if catalog is not None else load_catalog()
    raw = data.get("stages") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict) and s.get("id")]


def stage_order(catalog: dict[str, Any] | None = None) -> list[str]:
    return [str(s["id"]) for s in stage_defs(catalog)]


def stage_by_id(stage_id: str, catalog: dict[str, Any] | None = None) -> dict[str, Any] | None:
    want = (stage_id or "").strip().lower()
    for spec in stage_defs(catalog):
        if str(spec.get("id") or "").lower() == want:
            return spec
    return None


def _empty_stages(catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sid in stage_order(catalog):
        out[sid] = {"status": "idle", "ticks": {}}
    return out


def ensure_state(engine_state: dict[str, Any] | None, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    bag = engine_state if isinstance(engine_state, dict) else {}
    raw = bag.get(STATE_KEY)
    if not isinstance(raw, dict):
        raw = {}
        bag[STATE_KEY] = raw
    raw.setdefault("ics_connected", False)
    raw.setdefault("active_stage", None)
    raw.setdefault("wait_started_at", 0.0)
    raw.setdefault("hinted", [])
    stages = raw.get("stages")
    if not isinstance(stages, dict):
        stages = {}
        raw["stages"] = stages
    for sid in stage_order(catalog):
        if sid not in stages or not isinstance(stages[sid], dict):
            stages[sid] = {"status": "idle", "ticks": {}}
        stages[sid].setdefault("status", "idle")
        stages[sid].setdefault("ticks", {})
    return raw


def get_state(engine_state: dict[str, Any] | None) -> dict[str, Any]:
    bag = engine_state if isinstance(engine_state, dict) else {}
    raw = bag.get(STATE_KEY)
    return raw if isinstance(raw, dict) else {}


def ics_connected(engine_state: dict[str, Any] | None) -> bool:
    return bool(get_state(engine_state).get("ics_connected"))


def should_poll(config: dict[str, Any] | None, engine_state: dict[str, Any] | None) -> bool:
    return enabled(config) and ics_connected(engine_state)


def gate_reason(config: dict[str, Any] | None, snap: AircraftState) -> str:
    if not enabled(config):
        return "off"
    if not snap.fresh:
        return "no export"
    if not snap.is_f16:
        return "not F-16"
    if not snap.on_ground:
        return "airborne"
    return ""


def live_ok(config: dict[str, Any] | None, snap: AircraftState) -> bool:
    return gate_reason(config, snap) == ""


def _status_of(vcc: dict[str, Any], stage_id: str) -> str:
    row = (vcc.get("stages") or {}).get(stage_id) or {}
    return str(row.get("status") or "idle")


def _set_status(vcc: dict[str, Any], stage_id: str, status: str) -> None:
    stages = vcc.setdefault("stages", {})
    row = stages.setdefault(stage_id, {"status": "idle", "ticks": {}})
    row["status"] = status


def _clear_ticks(vcc: dict[str, Any], stage_id: str) -> None:
    stages = vcc.setdefault("stages", {})
    row = stages.setdefault(stage_id, {"status": "idle", "ticks": {}})
    row["ticks"] = {}


def _done_ticks(vcc: dict[str, Any], stage_id: str) -> set[str]:
    row = (vcc.get("stages") or {}).get(stage_id) or {}
    ticks = row.get("ticks") if isinstance(row.get("ticks"), dict) else {}
    return {k for k, v in ticks.items() if v}


def _mark_tick(vcc: dict[str, Any], stage_id: str, tick_id: str) -> None:
    stages = vcc.setdefault("stages", {})
    row = stages.setdefault(stage_id, {"status": "idle", "ticks": {}})
    ticks = row.setdefault("ticks", {})
    ticks[tick_id] = True


def unfinished_ids(vcc: dict[str, Any], catalog: dict[str, Any] | None = None) -> list[str]:
    out: list[str] = []
    for sid in stage_order(catalog):
        if _status_of(vcc, sid) not in {"done", "skipped"}:
            out.append(sid)
    return out


def next_unfinished(vcc: dict[str, Any], catalog: dict[str, Any] | None = None) -> str:
    pending = unfinished_ids(vcc, catalog)
    return pending[0] if pending else ""


def fly_status_line(
    config: dict[str, Any] | None,
    engine_state: dict[str, Any] | None,
    snap: AircraftState | None = None,
) -> str:
    if not enabled(config):
        return "VCC off"
    aircraft = snap if snap is not None else crew_chief_export.read_aircraft()
    reason = gate_reason(config, aircraft)
    vcc = get_state(engine_state)
    if not vcc.get("ics_connected"):
        extra = f" · {reason}" if reason and reason != "off" else ""
        return f"ICS disconnected{extra}"
    if reason == "airborne":
        return "airborne — silent"
    if reason:
        return f"ICS connected · {reason}"
    active = str(vcc.get("active_stage") or "")
    bits: list[str] = []
    for sid in stage_order():
        st = _status_of(vcc, sid)
        if st == "done":
            bits.append(f"{sid} done")
        elif st == "skipped":
            bits.append(f"{sid} skipped")
        elif st == "active" or sid == active:
            pending = _pending_labels(vcc, sid)
            if pending:
                bits.append(f"waiting {sid} {pending[0]}")
            else:
                bits.append(f"{sid} active")
    body = " · ".join(bits[:6]) if bits else "idle"
    return f"ICS connected · {body}"


def say_phrase_for_stage(stage_id: str) -> str:
    """Kneeboard line to jump into this stage."""
    spec = stage_by_id(stage_id)
    if spec:
        say = str(spec.get("say") or "").strip()
        if say:
            return say
    ready = ""
    any_phrase = ""
    for _intent, phrase, sid in _VOICE_PHRASES:
        if sid != stage_id:
            continue
        if not any_phrase:
            any_phrase = phrase
        if phrase.startswith("ready") and (not ready or len(phrase) < len(ready)):
            ready = phrase
    return ready or any_phrase or stage_id


def _script_status(
    vcc: dict[str, Any],
    item: dict[str, Any],
) -> str:
    kind = str(item.get("kind") or "")
    sid = str(item.get("stage") or "")
    tid = str(item.get("tick") or "")
    if kind == "connect":
        return "done" if vcc.get("ics_connected") else "idle"
    if kind in {"cleared_off", "disconnect"}:
        return "idle"
    if tid and sid:
        if _status_of(vcc, sid) in {"done", "skipped"}:
            return _status_of(vcc, sid)
        if tid in _done_ticks(vcc, sid):
            return "done"
        if str(vcc.get("active_stage") or "") == sid:
            return "active"
        return "idle"
    if sid:
        status = _status_of(vcc, sid)
        if sid == str(vcc.get("active_stage") or "") and status == "idle":
            return "active"
        return status
    return "idle"


def script_rows(vcc: dict[str, Any], catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = catalog if catalog is not None else load_catalog()
    raw = data.get("script") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    current_set = False
    for item in raw:
        if not isinstance(item, dict):
            continue
        optional = bool(item.get("optional"))
        status = _script_status(vcc, item)
        is_current = False
        if not current_set and status not in {"done", "skipped"} and not optional:
            is_current = True
            current_set = True
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "say": str(item.get("say") or ""),
                "say_alt": str(item.get("say_alt") or ""),
                "stage": str(item.get("stage") or ""),
                "tick": str(item.get("tick") or ""),
                "intent": str(item.get("intent") or ""),
                "kind": str(item.get("kind") or ""),
                "status": status,
                "optional": optional,
                "current": is_current,
            }
        )
    return rows


def checklist_helper(
    config: dict[str, Any] | None,
    engine_state: dict[str, Any] | None,
    snap: AircraftState | None = None,
) -> dict[str, Any]:
    """Kneeboard view: OpenKneeboard VCC script + waiting ticks."""
    aircraft = snap if snap is not None else crew_chief_export.read_aircraft()
    vcc = get_state(engine_state)
    catalog = load_catalog()
    active = str(vcc.get("active_stage") or "")
    stages: list[dict[str, Any]] = []
    for spec in stage_defs(catalog):
        sid = str(spec["id"])
        status = _status_of(vcc, sid)
        if sid == active and status == "idle":
            status = "active"
        pending = _pending_labels(vcc, sid) if status == "active" else []
        stages.append(
            {
                "id": sid,
                "label": str(spec.get("label") or sid),
                "status": status,
                "say": say_phrase_for_stage(sid),
                "say_alt": str(spec.get("say_alt") or ""),
                "optional": bool(spec.get("optional")),
                "pending": pending,
            }
        )
    script = script_rows(vcc, catalog)
    current = next((row for row in script if row.get("current")), None)
    waiting = _pending_labels(vcc, active) if active else []
    now_say = ""
    now_alt = ""
    if current:
        now_say = str(current.get("say") or "")
        now_alt = str(current.get("say_alt") or "")
    elif active:
        now_say = say_phrase_for_stage(active)
    return {
        "enabled": enabled(config),
        "ics": bool(vcc.get("ics_connected")),
        "gate": gate_reason(config, aircraft) if enabled(config) else "off",
        "active": active,
        "waiting": waiting,
        "now_say": now_say,
        "now_alt": now_alt,
        "script": script,
        "stages": stages,
        "controls": [
            ("skip", "skip this"),
            ("next", "next"),
            ("stop_listen", "standby"),
            ("cleared_off", "you're cleared off"),
        ],
    }


def checklist_fingerprint(view: dict[str, Any] | None) -> str:
    data = view or {}
    parts = [
        "1" if data.get("enabled") else "0",
        "1" if data.get("ics") else "0",
        str(data.get("gate") or ""),
        str(data.get("active") or ""),
        str(data.get("now_say") or ""),
        ",".join(str(x) for x in (data.get("waiting") or [])),
    ]
    for row in data.get("script") or []:
        if not isinstance(row, dict):
            continue
        parts.append(
            f"s{row.get('id')}:{row.get('status')}:{'1' if row.get('current') else '0'}"
        )
    for row in data.get("stages") or []:
        if not isinstance(row, dict):
            continue
        parts.append(
            f"{row.get('id')}:{row.get('status')}:{','.join(row.get('pending') or [])}"
        )
    return "|".join(parts)


def _pending_labels(vcc: dict[str, Any], stage_id: str) -> list[str]:
    spec = stage_by_id(stage_id)
    if not spec:
        return []
    done = _done_ticks(vcc, stage_id)
    labels: list[str] = []
    for tick in spec.get("ticks") or []:
        if not isinstance(tick, dict):
            continue
        tid = str(tick.get("id") or "")
        if tid and tid not in done:
            labels.append(str(tick.get("say") or tick.get("label") or tid))
    return labels


def _alias_map(catalog: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for spec in stage_defs(catalog):
        sid = str(spec["id"])
        aliases = [sid, str(spec.get("label") or "")]
        extra = spec.get("aliases") or []
        if isinstance(extra, list):
            aliases.extend(str(a) for a in extra)
        for alias in aliases:
            a = _norm(alias)
            if a:
                pairs.append((a, sid))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


def _norm(text: str) -> str:
    t = (text or "").lower()
    t = t.replace("5x5", "5 by 5")
    t = t.replace("five by five", "5 by 5")
    t = t.replace("speed breaks", "speedbrakes")
    t = t.replace("speed brakes", "speedbrakes")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _has_phrase(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    return re.search(r"(?:^|\s)" + re.escape(phrase) + r"(?:\s|$)", text) is not None


# OpenKneeboard Virtual Crew Chief Checklist — longer phrases first.
# "Disconnected" / "Connected" are TRIM steps, not ICS.
# Start listening: "Hey Chief" / "Chief". Stop: "Standby" / "Disregard".
_VOICE_PHRASES: list[tuple[str, str, str]] = [
    ("cleared_off", "everything looks good up here", ""),
    ("cleared_off", "you are cleared off", ""),
    ("cleared_off", "youre cleared off", ""),
    ("cleared_off", "cleared off", ""),
    ("stop_listen", "standby chief", ""),
    ("stop_listen", "disregard chief", ""),
    ("stop_listen", "standby", ""),
    ("stop_listen", "disregard", ""),
    ("radio_check", "how do you hear me", "radio"),
    ("radio_check", "how do you hear", "radio"),
    ("radio_check", "how hear me", "radio"),
    ("radio_check", "how hear", "radio"),
    ("radio_check", "radio check", "radio"),
    ("loud_clear", "loud and clear", "radio"),
    ("loud_clear", "5 by 5", "radio"),
    ("start", "clear for start 2", "start"),
    ("start", "ready for start 2", "start"),
    ("start", "clear for start", "start"),
    ("start", "ready for start", "start"),
    ("start", "ready start", "start"),
    ("good_start", "good start", "start"),
    ("pull_pin", "cleared to remove the epu pin", "epu"),
    ("pull_pin", "cleared to pull the epu pin", "epu"),
    ("pull_pin", "remove the epu pin", "epu"),
    ("pull_pin", "pull the epu pin", "epu"),
    ("pull_pin", "pull the pin", "epu"),
    ("pull_pin", "pull pin", "epu"),
    ("check_flow", "cleared to check for flow", "epu"),
    ("check_flow", "check for flow", "epu"),
    ("check_flow", "check the flow", "epu"),
    ("check_flow", "check flow", "epu"),
    ("runup", "clear to run up", "epu"),
    ("runup", "clear for run up", "epu"),
    ("runup", "cleared to run up", "epu"),
    ("ready_sec", "ready for sec check", "sec"),
    ("ready_sec", "clear for sec", "sec"),
    ("ready_sec", "ready for sec", "sec"),
    ("ready_sec", "ready secondary", "sec"),
    ("ready_sec", "ready sec", "sec"),
    ("sec_good", "sec check good", "sec"),
    ("sec_good", "secondary good", "sec"),
    ("sec_good", "sec good", "sec"),
    ("sec_good", "back to pri", "sec"),
    ("fc_clear", "speedbrakes and flight controls clear", "sec"),
    ("fc_clear", "speedbrakes and flight controls", "sec"),
    ("fc_clear", "flight controls still clear", "dbu"),
    ("fc_clear", "flight controls clear", "sec"),
    ("ready_bit", "ready for bit check", "bit"),
    ("ready_bit", "cleared for bit", "bit"),
    ("ready_bit", "ready for bit", "bit"),
    ("ready_bit", "ready bit", "bit"),
    ("ready_trim", "bit passed ready for trim", "trim"),
    ("ready_trim", "cleared for trim check", "trim"),
    ("ready_trim", "ready for trim check", "trim"),
    ("ready_trim", "ready for trim", "trim"),
    ("ready_trim", "trim check", "trim"),
    ("ready_trim", "ready trim", "trim"),
    ("trim_disc", "check no movement", "trim"),
    ("trim_disc", "ok disconnected", "trim"),
    ("trim_disc", "disconnected", "trim"),
    ("trim_conn", "ok connected", "trim"),
    ("trim_conn", "connected", "trim"),
    ("ready_big", "cleared for big movements", "big"),
    ("ready_big", "ready for big movements", "big"),
    ("ready_big", "big movements", "big"),
    ("ready_big", "ready big", "big"),
    ("ready_aar", "ready air refuel", "aar"),
    ("ready_aar", "ready aar", "aar"),
    ("ready_aar", "aar door", "aar"),
    ("left_brake", "ready on the left brake", "brakes"),
    ("left_brake", "ready on left brake", "brakes"),
    ("left_brake", "ready left brake", "brakes"),
    ("left_brake", "left brake", "brakes"),
    ("right_brake", "ready on the right brake", "brakes"),
    ("right_brake", "ready on right brake", "brakes"),
    ("right_brake", "ready right brake", "brakes"),
    ("right_brake", "right brake", "brakes"),
    ("channel_1", "channel one", "brakes"),
    ("channel_1", "channel 1", "brakes"),
    ("channel_2", "channel two", "brakes"),
    ("channel_2", "channel 2", "brakes"),
    ("dbu", "dbu still clear", "dbu"),
    ("dbu", "still clear", "dbu"),
    ("dbu", "dbu clear", "dbu"),
    ("dbu", "ready dbu", "dbu"),
    ("dbu", "dbu", "dbu"),
    ("connect", "hey chief", ""),
    ("connect", "hook up", ""),
    ("connect", "hookup", ""),
    ("small_talk", "how are you doing today", ""),
    ("small_talk", "how are you doing tonight", ""),
    ("small_talk", "how you doin", ""),
    ("small_talk", "hows it goin", ""),
    ("small_talk", "how is it going", ""),
    ("next", "next check", ""),
    ("next", "next", ""),
    ("halt", "hold on", ""),
    ("reset", "reset crew chief", ""),
    ("reset", "reset vcc", ""),
]


def match_voice(transcript: str, catalog: dict[str, Any] | None = None) -> VoiceMatch | None:
    text = _norm(transcript)
    if not text:
        return None
    if text.startswith("skip ") or text == "skip":
        rest = text[4:].strip()
        if rest in {"", "this", "it", "that"}:
            return VoiceMatch("skip", "", transcript)
        for alias, sid in _alias_map(catalog):
            if rest == alias or rest.endswith(alias):
                return VoiceMatch("skip_named", sid, transcript)
        return VoiceMatch("skip", "", transcript)
    for intent, phrase, stage_id in _VOICE_PHRASES:
        if _has_phrase(text, phrase):
            return VoiceMatch(intent, stage_id, transcript)
    if text == "chief":
        return VoiceMatch("connect", "", transcript)
    return None


def _tick_hit(snap: AircraftState, tick: dict[str, Any]) -> bool:
    arg_ok = False
    args = tick.get("args") or []
    if isinstance(args, list) and args:
        arg_ok = True
        for spec in args:
            if not isinstance(spec, dict):
                arg_ok = False
                break
            try:
                aid = int(spec["id"])
                lo = float(spec["lo"])
                hi = float(spec["hi"])
            except (KeyError, TypeError, ValueError):
                arg_ok = False
                break
            if not crew_chief_export.arg_in_range(snap, aid, lo, hi):
                arg_ok = False
                break
    mech_ok = False
    mechs = tick.get("mech") or []
    if isinstance(mechs, list) and mechs:
        for spec in mechs:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name") or "")
            try:
                lo = float(spec["lo"])
                hi = float(spec["hi"])
            except (KeyError, TypeError, ValueError):
                continue
            val = crew_chief_export.mech_axis(snap, name)
            if name not in {"elevator", "aileron", "rudder"}:
                val = crew_chief_export.mech_scalar(snap, name)
            if lo <= val <= hi:
                mech_ok = True
                break
    if args and mechs:
        return arg_ok or mech_ok
    if args:
        return arg_ok
    if mechs:
        return mech_ok
    return False


def _timeout_s(spec: dict[str, Any], catalog: dict[str, Any] | None = None) -> float:
    try:
        if spec.get("timeout_s") is not None:
            return float(spec["timeout_s"])
    except (TypeError, ValueError):
        pass
    data = catalog if catalog is not None else load_catalog()
    try:
        return float(data.get("timeout_s") or 8.0)
    except (TypeError, ValueError):
        return 8.0


def _soft_hint(vcc: dict[str, Any], spec: dict[str, Any]) -> Event | None:
    prereq = spec.get("prereq") or []
    if not isinstance(prereq, list) or not prereq:
        return None
    hinted = vcc.setdefault("hinted", [])
    sid = str(spec.get("id") or "")
    if sid in hinted:
        return None
    missing = [p for p in prereq if _status_of(vcc, str(p)) not in {"done", "skipped"}]
    if not missing:
        return None
    hinted.append(sid)
    return Event("soft_prereq", f"usual first: {missing[0]}", "hint")


def _activate(
    vcc: dict[str, Any], stage_id: str, *, redo: bool = False, silent: bool = False
) -> list[Event]:
    events: list[Event] = []
    prev = str(vcc.get("active_stage") or "")
    if prev and prev != stage_id and _status_of(vcc, prev) == "active":
        _set_status(vcc, prev, "idle")
    spec = stage_by_id(stage_id)
    if not spec:
        return [Event("", f"unknown stage {stage_id}", "none")]
    if redo or _status_of(vcc, stage_id) in {"done", "skipped"}:
        _clear_ticks(vcc, stage_id)
    _set_status(vcc, stage_id, "active")
    vcc["active_stage"] = stage_id
    vcc["wait_started_at"] = time.time()
    hint = _soft_hint(vcc, spec)
    if hint:
        events.append(hint)
    open_line = str(spec.get("open_line") or "").strip()
    if open_line and not silent:
        events.append(Event(open_line, f"open {stage_id}", "speak"))
    ticks = spec.get("ticks") or []
    if not ticks:
        _set_status(vcc, stage_id, "done")
        vcc["active_stage"] = None
        events.append(Event("", f"{stage_id} done", "done"))
    return events


def connect(
    engine_state: dict[str, Any],
    config: dict[str, Any] | None,
    snap: AircraftState,
) -> list[Event]:
    if not live_ok(config, snap):
        return []
    vcc = ensure_state(engine_state)
    vcc["ics_connected"] = True
    return [Event("loud_and_clear", "ics up", "speak")]


def disconnect(engine_state: dict[str, Any], *, line_id: str = "safe_flight") -> list[Event]:
    vcc = ensure_state(engine_state)
    was = bool(vcc.get("ics_connected"))
    vcc["ics_connected"] = False
    active = str(vcc.get("active_stage") or "")
    if active and _status_of(vcc, active) == "active":
        _set_status(vcc, active, "idle")
    vcc["active_stage"] = None
    if not was:
        return []
    return [Event(line_id, "ics down", "speak")]


def stop_listen(engine_state: dict[str, Any]) -> list[Event]:
    """Kneeboard: Standby / Disregard stops the crew chief listening."""
    return disconnect(engine_state, line_id="standby")


def reset(engine_state: dict[str, Any]) -> list[Event]:
    bag = engine_state if isinstance(engine_state, dict) else {}
    bag[STATE_KEY] = {}
    ensure_state(bag)
    return [Event("copy", "reset", "speak")]


def jump(
    engine_state: dict[str, Any],
    stage_id: str,
    config: dict[str, Any] | None,
    snap: AircraftState,
) -> list[Event]:
    if not live_ok(config, snap) or not ics_connected(engine_state):
        return []
    vcc = ensure_state(engine_state)
    return _activate(vcc, stage_id, redo=True)


def skip_stage(
    engine_state: dict[str, Any],
    stage_id: str | None,
    config: dict[str, Any] | None,
    snap: AircraftState,
) -> list[Event]:
    if not live_ok(config, snap) or not ics_connected(engine_state):
        return []
    vcc = ensure_state(engine_state)
    target = (stage_id or "").strip() or str(vcc.get("active_stage") or "")
    events: list[Event] = [Event("skipped", f"skip {target or 'none'}", "speak")]
    if target:
        _set_status(vcc, target, "skipped")
        if vcc.get("active_stage") == target:
            vcc["active_stage"] = None
    nxt = next_unfinished(vcc)
    if nxt:
        events.extend(_activate(vcc, nxt, redo=False))
    return events


def next_stage(
    engine_state: dict[str, Any],
    config: dict[str, Any] | None,
    snap: AircraftState,
) -> list[Event]:
    if not live_ok(config, snap) or not ics_connected(engine_state):
        return []
    vcc = ensure_state(engine_state)
    active = str(vcc.get("active_stage") or "")
    if active:
        _set_status(vcc, active, "skipped")
        vcc["active_stage"] = None
    nxt = next_unfinished(vcc)
    if not nxt:
        return [Event("copy", "all stages finished", "speak")]
    return _activate(vcc, nxt, redo=False)


def standby(engine_state: dict[str, Any]) -> list[Event]:
    vcc = ensure_state(engine_state)
    active = str(vcc.get("active_stage") or "")
    if active and _status_of(vcc, active) == "active":
        _set_status(vcc, active, "idle")
    vcc["active_stage"] = None
    return [Event("standby", "halt wait", "speak")]


def tick(
    engine_state: dict[str, Any],
    config: dict[str, Any] | None,
    snap: AircraftState | None = None,
    *,
    now: float | None = None,
) -> list[Event]:
    if not enabled(config) or not ics_connected(engine_state):
        return []
    aircraft = snap if snap is not None else crew_chief_export.read_aircraft()
    if not live_ok(config, aircraft):
        return []
    vcc = ensure_state(engine_state)
    stage_id = str(vcc.get("active_stage") or "")
    if not stage_id:
        return []
    spec = stage_by_id(stage_id)
    if not spec:
        return []
    ticks = [t for t in (spec.get("ticks") or []) if isinstance(t, dict) and t.get("id")]
    if not ticks:
        return []
    events: list[Event] = []
    done = _done_ticks(vcc, stage_id)
    for tick_spec in ticks:
        tid = str(tick_spec["id"])
        if tid in done:
            continue
        if _tick_hit(aircraft, tick_spec):
            _mark_tick(vcc, stage_id, tid)
            line = str(tick_spec.get("line") or "copy")
            events.append(Event(line, f"{stage_id}:{tid}", "speak"))
            vcc["wait_started_at"] = time.time() if now is None else now
    done = _done_ticks(vcc, stage_id)
    if all(str(t["id"]) in done for t in ticks):
        _set_status(vcc, stage_id, "done")
        vcc["active_stage"] = None
        events.append(Event("yes_sir", f"{stage_id} done", "done"))
        return events
    clock = time.time() if now is None else now
    started = float(vcc.get("wait_started_at") or 0)
    limit = _timeout_s(spec)
    if started and (clock - started) >= limit:
        events.append(Event("no_movement", f"{stage_id} timeout", "timeout"))
        vcc["wait_started_at"] = clock
    return events


def _voice_mark_tick(
    engine_state: dict[str, Any],
    config: dict[str, Any] | None,
    snap: AircraftState,
    stage_id: str,
    tick_id: str,
) -> list[Event]:
    if not live_ok(config, snap) or not ics_connected(engine_state):
        return []
    vcc = ensure_state(engine_state)
    events: list[Event] = []
    if str(vcc.get("active_stage") or "") != stage_id:
        events.extend(_activate(vcc, stage_id, redo=True, silent=True))
    spec = stage_by_id(stage_id)
    tick_spec = None
    for row in (spec or {}).get("ticks") or []:
        if isinstance(row, dict) and str(row.get("id") or "") == tick_id:
            tick_spec = row
            break
    line = str((tick_spec or {}).get("line") or "copy")
    _mark_tick(vcc, stage_id, tick_id)
    events.append(Event(line, f"{stage_id}:{tick_id}", "speak"))
    vcc["wait_started_at"] = time.time()
    ticks = [t for t in ((spec or {}).get("ticks") or []) if isinstance(t, dict) and t.get("id")]
    done = _done_ticks(vcc, stage_id)
    if ticks and all(str(t["id"]) in done for t in ticks):
        _set_status(vcc, stage_id, "done")
        vcc["active_stage"] = None
        events.append(Event("yes_sir", f"{stage_id} done", "done"))
    return events


def _ensure_ics(
    engine_state: dict[str, Any],
    config: dict[str, Any] | None,
    snap: AircraftState,
) -> list[Event]:
    if ics_connected(engine_state):
        return []
    return connect(engine_state, config, snap)


def handle(
    engine_state: dict[str, Any],
    config: dict[str, Any] | None,
    match: VoiceMatch,
    snap: AircraftState | None = None,
) -> list[Event]:
    aircraft = snap if snap is not None else crew_chief_export.read_aircraft()
    intent = match.intent
    if intent == "reset":
        return reset(engine_state)
    if intent == "connect":
        return connect(engine_state, config, aircraft)
    if intent in {"cleared_off", "disconnect"}:
        return disconnect(engine_state)
    if intent == "stop_listen":
        return stop_listen(engine_state)
    if intent in {"radio_check", "loud_clear"}:
        events = _ensure_ics(engine_state, config, aircraft)
        if not ics_connected(engine_state):
            return events
        events.extend(jump(engine_state, "radio", config, aircraft))
        if intent == "loud_clear":
            vcc = ensure_state(engine_state)
            _set_status(vcc, "radio", "done")
            vcc["active_stage"] = None
        return events
    if not ics_connected(engine_state):
        return []
    if not live_ok(config, aircraft):
        return []
    if intent in {"standby", "halt"}:
        return standby(engine_state)
    if intent == "small_talk":
        return [Event("yes_sir", "small talk", "speak")]
    if intent == "skip":
        return skip_stage(engine_state, None, config, aircraft)
    if intent == "skip_named":
        return skip_stage(engine_state, match.stage_id, config, aircraft)
    if intent == "next":
        return next_stage(engine_state, config, aircraft)
    if intent == "trim_disc":
        return _voice_mark_tick(engine_state, config, aircraft, "trim", "disc")
    if intent == "trim_conn":
        return _voice_mark_tick(engine_state, config, aircraft, "trim", "connected")
    if intent == "channel_1":
        return _voice_mark_tick(engine_state, config, aircraft, "brakes", "ch1")
    if intent == "channel_2":
        return _voice_mark_tick(engine_state, config, aircraft, "brakes", "ch2")
    if intent == "jump" and match.stage_id:
        return jump(engine_state, match.stage_id, config, aircraft)
    if match.stage_id:
        vcc = ensure_state(engine_state)
        if str(vcc.get("active_stage") or "") == match.stage_id:
            return [Event("copy", f"ack {match.stage_id}", "speak")]
        return jump(engine_state, match.stage_id, config, aircraft)
    return []


def play_events(
    events: list[Event],
    config: dict[str, Any] | None,
    *,
    blocking: bool = False,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events:
        if ev.action in {"speak", "hint", "timeout"} and ev.line_id:
            played = crew_chief_audio.play_line(
                ev.line_id, config, blocking=blocking
            )
            played["detail"] = ev.detail
            out.append(played)
        else:
            out.append(
                {
                    "action": ev.action,
                    "line_id": ev.line_id,
                    "text": "",
                    "source": "",
                    "detail": ev.detail,
                }
            )
    return out


def execute(
    engine: Any,
    match: VoiceMatch,
    snap: AircraftState | None = None,
    *,
    play: bool = True,
) -> dict[str, Any]:
    state = getattr(engine, "state", None)
    if not isinstance(state, dict):
        state = {}
        engine.state = state
    config = getattr(engine, "config", None)
    if isinstance(config, dict) and not enabled(config):
        return {"action": "none", "detail": "crew chief off"}
    events = handle(state, config if isinstance(config, dict) else {}, match, snap)
    played: list[dict[str, Any]] = []
    if play:
        played = play_events(events, config if isinstance(config, dict) else {})
    first = events[0] if events else None
    return {
        "action": "crew_chief" if events else "none",
        "detail": first.detail if first else "no vcc match",
        "text": crew_chief_audio.line_text(first.line_id) if first else "",
        "events": [
            {"line_id": e.line_id, "detail": e.detail, "action": e.action}
            for e in events
        ],
        "played": played,
        "channel": "crew_chief",
    }


def tick_engine(engine: Any, snap: AircraftState | None = None, *, play: bool = True) -> dict[str, Any]:
    state = getattr(engine, "state", None)
    if not isinstance(state, dict):
        return {"action": "none", "events": []}
    config = getattr(engine, "config", None)
    events = tick(state, config if isinstance(config, dict) else {}, snap)
    played: list[dict[str, Any]] = []
    if play and events:
        played = play_events(events, config if isinstance(config, dict) else {})
    return {
        "action": "crew_chief" if events else "none",
        "events": [
            {"line_id": e.line_id, "detail": e.detail, "action": e.action}
            for e in events
        ],
        "played": played,
        "channel": "crew_chief",
    }
