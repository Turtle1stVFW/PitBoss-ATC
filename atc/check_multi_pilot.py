"""
Multi-pilot host: per-channel queues, session isolation, client freq gate.

Run: py -3 check_multi_pilot.py
"""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atc_net
import atc_phrase
import atc_server
import agencies
import channel_tx
import flow_engine
import runway_position
import srs_radio
import tanker
import voice_engine
import voice_intent

AIRPORTS = atc_phrase.load_json(atc_phrase.AIRPORTS_PATH)
MISSION = atc_phrase.load_json(HERE / "flows" / "nellis_default.json")
EXAMPLE = atc_phrase.load_json(HERE / "config.example.json")


def _host_config(**over: object) -> dict:
    cfg = copy.deepcopy(EXAMPLE)
    cfg["dry_run"] = True
    cfg["freq_gate_enabled"] = False
    cfg["atc_role"] = "host"
    cfg["atc_token"] = "test-token"
    cfg["opus_user_name"] = ""
    cfg["opus_flight_id"] = None
    cfg["opus_seat"] = None
    cfg["opus_backend_url"] = ""
    cfg["opus_metar_url"] = "http://127.0.0.1:1/metar?icao={icao}"
    cfg.update(over)
    return cfg


def test_channel_parallel_and_fifo() -> list[str]:
    fails: list[str] = []
    order: list[str] = []
    hold = threading.Event()
    tower_started = threading.Event()

    def tx(job: dict) -> int:
        ch = str(job.get("channel") or "")
        if ch == "ground":
            hold.wait(timeout=2)
            order.append(f"ground:{job.get('callsign')}")
        else:
            tower_started.set()
            order.append(f"{ch}:{job.get('callsign')}")
        return 0

    hub = channel_tx.ChannelTxHub(["ground", "tower", "other"], transmit_fn=tx)
    done_g = threading.Event()
    done_t = threading.Event()
    base = {
        "config": {"dry_run": True},
        "airport": {},
        "tx_name": "X",
        "freq": 251.0,
        "mod": "AM",
        "text": "x",
    }
    hub.submit({**base, "channel": "ground", "callsign": "A", "done": done_g})
    hub.submit({**base, "channel": "tower", "callsign": "B", "done": done_t})
    if not tower_started.wait(1.5):
        fails.append("Tower TX should start while Ground is still speaking")
    hold.set()
    if not done_t.wait(1.5):
        fails.append("Tower job did not finish")
    if not done_g.wait(1.5):
        fails.append("Ground job did not finish")
    if "tower:B" not in order:
        fails.append(f"Tower missing from order {order}")
    hub.stop()

    fifo: list[str] = []

    def tx_fifo(job: dict) -> int:
        fifo.append(str(job.get("callsign")))
        time.sleep(0.05)
        return 0

    hub2 = channel_tx.ChannelTxHub(["ground", "other"], transmit_fn=tx_fifo)
    d1, d2 = threading.Event(), threading.Event()
    hub2.submit({**base, "channel": "ground", "callsign": "Fleece1", "done": d1})
    hub2.submit({**base, "channel": "ground", "callsign": "Viper3", "done": d2})
    if not (d1.wait(2) and d2.wait(2)):
        fails.append("Ground FIFO jobs did not finish")
    elif fifo != ["Fleece1", "Viper3"]:
        fails.append(f"Ground FIFO expected Fleece1 then Viper3, got {fifo}")
    hub2.stop()
    return fails


def test_session_isolation() -> list[str]:
    fails: list[str] = []
    cfg = _host_config()
    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
    )
    a = server.hello(
        {
            "callsign_override": "Fleece 1",
            "opus_flight_id": 101,
            "opus_seat": 1,
            "radio_fresh": False,
        }
    )
    b = server.hello(
        {
            "callsign_override": "Viper 3",
            "opus_flight_id": 202,
            "opus_seat": 1,
            "radio_fresh": False,
        }
    )
    sa = server.get_session(a["session_id"])
    sb = server.get_session(b["session_id"])
    if sa is None or sb is None:
        return ["hello did not create sessions"]
    idx_b = int(sb.engine.state.get("index") or 0)
    server.handle_command(sa, "next", {"radio_fresh": False})
    if int(sb.engine.state.get("index") or 0) != idx_b:
        fails.append("Viper 3 cursor moved when Fleece 1 advanced")
    if int(sa.engine.state.get("index") or 0) == idx_b and not sa.engine.state.get(
        "last_step_id"
    ):
        fails.append("Fleece 1 next() did not mutate its own session")
    if a["session_id"] == b["session_id"]:
        fails.append("two flights should not share a session key")
    return fails


def test_clear_flight_cache_scoped() -> list[str]:
    fails: list[str] = []
    import ops as ops_mod

    cfg = _host_config()
    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
    )
    a = server.hello(
        {
            "callsign_override": "Fleece 1",
            "opus_flight_id": 101,
            "opus_seat": 1,
            "radio_fresh": False,
        }
    )
    b = server.hello(
        {
            "callsign_override": "Viper 3",
            "opus_flight_id": 202,
            "opus_seat": 1,
            "radio_fresh": False,
        }
    )
    sa = server.get_session(a["session_id"])
    sb = server.get_session(b["session_id"])
    if sa is None or sb is None:
        return ["cache scope: no sessions"]
    ops_mod.approve_start(sa.engine.state, callsign="Fleece 1")
    ops_mod.approve_start(sb.engine.state, callsign="Viper 3")
    sa.engine.state["pending_contact"] = "delivery"
    sa.engine.state["last_tx_template"] = "ops_words"
    sb.engine.state["pending_contact"] = "delivery"
    sb.engine.state["last_tx_template"] = "ops_words"
    idx_b = int(sb.engine.state.get("index") or 0)
    result = server.handle_command(sa, "clear_flight_cache", {"radio_fresh": False})
    if ops_mod.sortie_from_state(sa.engine.state) is not None:
        fails.append("Fleece cache reset must clear OPS start")
    if sa.engine.state.get("pending_contact"):
        fails.append("Fleece cache reset must clear pending Delivery")
    if sa.engine.state.get("last_tx_template"):
        fails.append("Fleece cache reset must clear last TX")
    if ops_mod.sortie_from_state(sb.engine.state) is None:
        fails.append("Viper OPS start must survive Fleece cache reset")
    if str(sb.engine.state.get("pending_contact") or "") != "delivery":
        fails.append("Viper pending Delivery must survive Fleece cache reset")
    if int(sb.engine.state.get("index") or 0) != idx_b:
        fails.append("Viper cursor moved when Fleece reset cache")
    fs = result.get("flow_state") if isinstance(result, dict) else None
    if isinstance(fs, dict) and fs.get("ops_sortie"):
        fails.append("host status still has ops_sortie after cache reset")
    return fails


def test_client_freq_gate() -> list[str]:
    fails: list[str] = []
    cfg = _host_config(freq_gate_enabled=True)
    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
    )
    hello = server.hello(
        {
            "callsign_override": "Fleece 1",
            "opus_flight_id": 303,
            "opus_seat": 1,
        }
    )
    sess = server.get_session(hello["session_id"])
    if sess is None:
        return ["gate test: no session"]
    try:
        server.handle_command(
            sess,
            "next",
            {"tuned_freqs_mhz": [132.65], "radio_fresh": True},
        )
        fails.append("next() should block when client is off Delivery")
    except RuntimeError as exc:
        if "Blocked" not in str(exc):
            fails.append(f"expected Blocked, got {exc}")
    # Ops is step 1 at Nellis, so the gate wants 269.025 before Delivery's 289.4.
    try:
        server.handle_command(
            sess,
            "next",
            {"tuned_freqs_mhz": [269.025], "radio_fresh": True},
        )
    except RuntimeError as exc:
        fails.append(f"on-freq Ops should allow: {exc}")
    # Ops holds the cursor until WORDS/start, so step to Delivery by hand.
    delivery_idx = next(
        (
            i
            for i, s in enumerate(MISSION.get("steps") or [])
            if str(s.get("channel") or "") == "delivery"
        ),
        -1,
    )
    if delivery_idx < 0:
        return fails + ["mission has no Delivery step"]
    server.handle_command(sess, "seek", {"index": delivery_idx})
    try:
        server.handle_command(
            sess,
            "next",
            {"tuned_freqs_mhz": [289.4], "radio_fresh": True},
        )
    except RuntimeError as exc:
        fails.append(f"on-freq Delivery should allow: {exc}")
    return fails


def test_empty_radio_snapshot_keeps_bank() -> list[str]:
    """Failed client radio reads must not wipe the Host's last good UHF bank."""
    fails: list[str] = []
    cfg = _host_config(freq_gate_enabled=True)
    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
    )
    hello = server.hello(
        {
            "callsign_override": "Fleece 1",
            "opus_flight_id": 404,
            "opus_seat": 1,
            "tuned_freqs_mhz": [273.55, 251.0],
            "radio_fresh": True,
        }
    )
    sess = server.get_session(hello["session_id"])
    if sess is None:
        return ["radio keep: no session"]
    if 273.55 not in sess.tuned_freqs_mhz:
        fails.append(f"hello should seed UHF, got {sess.tuned_freqs_mhz}")
    # Heartbeat with a failed empty read — bank must stay.
    server.heartbeat(sess, {"tuned_freqs_mhz": [], "radio_fresh": False})
    if 273.55 not in sess.tuned_freqs_mhz:
        fails.append(
            f"empty unfresh snapshot wiped bank: {sess.tuned_freqs_mhz}"
        )
    # Explicit fresh empty (pilot really has no radios) may clear.
    server.heartbeat(sess, {"tuned_freqs_mhz": [], "radio_fresh": True})
    if sess.tuned_freqs_mhz:
        fails.append(
            f"fresh empty should clear bank, got {sess.tuned_freqs_mhz}"
        )
    return fails


def test_session_key() -> list[str]:
    fails: list[str] = []
    k1 = atc_net.session_key(opus_flight_id=1, opus_seat=1)
    k2 = atc_net.session_key(opus_flight_id=1, opus_seat=2)
    k3 = atc_net.session_key(opus_flight_id=1, opus_seat=1)
    if k1 == k2:
        fails.append("dash-1 and dash-2 must be different connection keys")
    if k1 != k3:
        fails.append("same flight/seat should resume the same session key")
    if atc_net.session_key(opus_flight_id=55.0, opus_seat=1.0) != atc_net.session_key(
        opus_flight_id=55, opus_seat=1
    ):
        fails.append("flight/seat ids must normalize int vs float")
    if not agencies.is_default_sandbox({"flow_file": "flows/nellis_standard.json"}):
        fails.append("Nellis Standard must use the agency sandbox")
    if agencies.is_default_sandbox({"flow_file": "flows/untitled.json"}):
        fails.append("custom Untitled plans must not use the agency sandbox")
    f1 = atc_net.flow_key(opus_flight_id=1, callsign="Fleece 1")
    f2 = atc_net.flow_key(opus_flight_id=1, callsign="Fleece 1")
    f3 = atc_net.flow_key(opus_flight_id=2, callsign="Viper 3")
    if f1 != f2:
        fails.append("same Opus flight must share a flow key")
    if f1 == f3:
        fails.append("different Opus flights must not share a flow key")
    if f1 == k1:
        fails.append("flow key must not include the seat")
    if tanker.element_seats(1) != (1, 2) or tanker.element_seats(2) != (1, 2):
        fails.append("seats 1-2 should be the lead element")
    if tanker.element_seats(3) != (3, 4) or tanker.element_seats(4) != (3, 4):
        fails.append("seats 3-4 should be the second element")
    return fails


def test_hello_reclaims_identity_upgrade() -> list[str]:
    """Clear flight then reselect seat must not leave two Traffic rows."""
    fails: list[str] = []
    cfg = _host_config()
    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
    )
    first = server.hello(
        {
            "callsign_override": "RAZOR 1",
            "opus_flight_id": 55,
            # No seat yet — Traffic shows "?" but key still flight:55:1
            "radio_fresh": False,
            "tuned_freqs_mhz": [273.55],
        }
    )
    sid_flight = str(first.get("session_id") or "")
    cleared = server.hello(
        {
            "callsign_override": "RAZOR 1",
            "prior_session_id": sid_flight,
            "radio_fresh": False,
            "tuned_freqs_mhz": [273.55],
        }
    )
    sid_callsign = str(cleared.get("session_id") or "")
    if sid_callsign == sid_flight:
        fails.append("clearing Opus flight should rekey off flight:…")
    if sid_flight in server.sessions:
        fails.append("prior flight session should be dropped on clear-flight hello")
    seated = server.hello(
        {
            "callsign_override": "RAZOR 1",
            "opus_flight_id": 55,
            "opus_seat": 1,
            "prior_session_id": sid_callsign,
            "radio_fresh": True,
            "tuned_freqs_mhz": [269.025, 251.0],
        }
    )
    sid_seated = str(seated.get("session_id") or "")
    live = [
        s
        for s in server.sessions.values()
        if time.time() - s.last_seen <= atc_net.SESSION_TTL_S
    ]
    if len(live) != 1:
        fails.append(
            f"expected one live Traffic row after seat select, got {len(live)} "
            f"keys={[s.session_id for s in live]}"
        )
    if sid_callsign in server.sessions:
        fails.append("callsign-only orphan should be reclaimed when seat binds")
    if sid_seated != atc_net.session_key(opus_flight_id=55, opus_seat=1):
        fails.append(f"seated key unexpected: {sid_seated}")
    # Second seat on same flight must survive a lead re-hello
    server.hello(
        {
            "callsign_override": "RAZOR 1",
            "opus_flight_id": 55,
            "opus_seat": 2,
            "radio_fresh": True,
            "tuned_freqs_mhz": [269.025],
        }
    )
    server.hello(
        {
            "callsign_override": "RAZOR 1",
            "opus_flight_id": 55,
            "opus_seat": 1,
            "prior_session_id": sid_seated,
            "radio_fresh": True,
            "tuned_freqs_mhz": [269.025],
        }
    )
    seats = {
        s.identity.get("opus_seat")
        for s in server.sessions.values()
        if time.time() - s.last_seen <= atc_net.SESSION_TTL_S
    }
    if seats != {1, 2}:
        fails.append(f"dash-2 must stay when lead re-hellos, got seats={seats}")
    return fails


def test_flight_shared_cursor() -> list[str]:
    fails: list[str] = []
    cfg = _host_config()
    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
    )
    lead = server.hello(
        {
            "callsign_override": "Fleece 1",
            "opus_flight_id": 101,
            "opus_seat": 1,
            "radio_fresh": False,
        }
    )
    three = server.hello(
        {
            "callsign_override": "Fleece 1",
            "opus_flight_id": 101,
            "opus_seat": 3,
            "radio_fresh": False,
        }
    )
    other = server.hello(
        {
            "callsign_override": "Viper 3",
            "opus_flight_id": 202,
            "opus_seat": 1,
            "radio_fresh": False,
        }
    )
    sl = server.get_session(lead["session_id"])
    s3 = server.get_session(three["session_id"])
    so = server.get_session(other["session_id"])
    if sl is None or s3 is None or so is None:
        return ["hello did not create all sessions"]
    if lead["session_id"] == three["session_id"]:
        fails.append("dash-1 and dash-3 should keep separate connection ids")
    if sl.engine is not s3.engine:
        fails.append("same Opus flight should share one FlowEngine")
    if sl.engine is so.engine:
        fails.append("Viper 3 must not share Fleece 1's engine")
    idx_other = int(so.engine.state.get("index") or 0)
    server.handle_command(sl, "next", {"radio_fresh": False})
    if int(s3.engine.state.get("index") or 0) != int(sl.engine.state.get("index") or 0):
        fails.append("dash-3 cursor did not follow dash-1 next()")
    if int(so.engine.state.get("index") or 0) != idx_other:
        fails.append("Viper 3 cursor moved when Fleece 1 advanced")
    reps = server.unique_flow_sessions()
    if len(reps) != 2:
        fails.append(f"expected 2 unique flows, got {len(reps)}")
    return fails


def test_element_tanker_peel() -> list[str]:
    fails: list[str] = []
    cfg = _host_config()
    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
    )
    lead = server.hello(
        {
            "callsign_override": "Fleece 1",
            "opus_flight_id": 77,
            "opus_seat": 1,
            "radio_fresh": False,
        }
    )
    two = server.hello(
        {
            "callsign_override": "Fleece 1",
            "opus_flight_id": 77,
            "opus_seat": 2,
            "radio_fresh": False,
        }
    )
    three = server.hello(
        {
            "callsign_override": "Fleece 1",
            "opus_flight_id": 77,
            "opus_seat": 3,
            "radio_fresh": False,
        }
    )
    four = server.hello(
        {
            "callsign_override": "Fleece 1",
            "opus_flight_id": 77,
            "opus_seat": 4,
            "radio_fresh": False,
        }
    )
    sl = server.get_session(lead["session_id"])
    s2 = server.get_session(two["session_id"])
    s3 = server.get_session(three["session_id"])
    s4 = server.get_session(four["session_id"])
    if sl is None or s2 is None or s3 is None or s4 is None:
        return ["hello did not create element sessions"]
    idx0 = int(sl.engine.state.get("index") or 0)

    def peel(engine: flow_engine.FlowEngine) -> dict:
        tanker.enter_tanker_overlay(engine)
        return {"action": "ok"}

    server.run_action(s3, peel, {"radio_fresh": False})
    if not tanker.tanker_overlay_active(s3.local_state):
        fails.append("dash-3 should be on tanker overlay")
    if not tanker.tanker_overlay_active(s4.local_state):
        fails.append("dash-4 should peel with dash-3")
    if tanker.tanker_overlay_active(sl.local_state) or tanker.tanker_overlay_active(
        s2.local_state
    ):
        fails.append("lead element should stay on C2")
    if int(sl.engine.state.get("index") or 0) != idx0:
        fails.append("shared C2 cursor moved when the element went tanker")
    st3 = s3.public_status()
    st1 = sl.public_status()
    if str(st3.get("channel") or "").lower() != "tanker":
        fails.append(f"dash-3 status should show tanker, got {st3.get('channel')}")
    if str(st1.get("channel") or "").lower() == "tanker":
        fails.append("lead status should stay on C2")
    if not st3.get("on_tanker") or st1.get("on_tanker"):
        fails.append("on_tanker flag should be per element")

    server.handle_command(sl, "seek", {"index": idx0 + 1, "radio_fresh": False})
    idx1 = int(sl.engine.state.get("index") or 0)
    if idx1 == idx0:
        fails.append("lead should still be able to move the shared C2 cursor")
    if str(s3.public_status().get("channel") or "").lower() != "tanker":
        fails.append("dash-3 should stay on tanker while lead advances C2")

    def back(engine: flow_engine.FlowEngine) -> dict:
        tanker.leave_tanker_overlay(engine, "bandsaw", checkin=True)
        return {"action": "ok"}

    server.run_action(s3, back, {"radio_fresh": False})
    if tanker.tanker_overlay_active(s3.local_state) or tanker.tanker_overlay_active(
        s4.local_state
    ):
        fails.append("element return should clear overlay on 3 and 4")
    if int(sl.engine.state.get("index") or 0) != idx1:
        fails.append("element return must not rewind the flight's C2 cursor")
    if str(s3.public_status().get("channel") or "").lower() == "tanker":
        fails.append("dash-3 should rejoin the shared C2 step after tanker")
    return fails


def test_tanker_fly_freq_matches_pick() -> list[str]:
    """Blackjack can send Texaco 2; Fly must not show Texaco 1's 322.3."""
    fails: list[str] = []
    rows = [
        {
            "id": 1,
            "callsign": "TEXACO 1",
            "track": "ARLNS",
            "aircraft": "KC-135",
            "freq_mhz": 322.3,
            "boom": True,
        },
        {
            "id": 2,
            "callsign": "TEXACO 2",
            "track": "AR-625H/L",
            "aircraft": "KC-135",
            "freq_mhz": 319.8,
            "boom": True,
        },
    ]
    orig = tanker.fetch_opus_tankers
    tanker.fetch_opus_tankers = lambda *a, **k: list(rows)
    tanker._TANKER_CACHE["exp"] = 0.0
    try:
        cfg = _host_config()
        first = tanker.tanker_freqs_mhz(cfg)
        if not first or abs(float(first[0]) - 322.3) > 0.01:
            fails.append(f"catalog should list Texaco 1 first, got {first}")
        remembered = {
            "tanker_id": 2,
            "tanker_callsign": "TEXACO 2",
            "tanker_track": "AR-625H/L",
        }
        live = tanker.effective_tanker_mhz(remembered, cfg)
        if live is None or abs(float(live) - 319.8) > 0.01:
            fails.append(f"remembered Texaco 2 should be 319.8, got {live}")
        state: dict = {}
        tanker.remember_tanker(state, dict(rows[1]), config=cfg)
        if state.get("tanker_freq_mhz") is None or abs(
            float(state["tanker_freq_mhz"]) - 319.8
        ) > 0.01:
            fails.append(f"remember_tanker stored {state.get('tanker_freq_mhz')}")
        missing = {
            "id": 2,
            "callsign": "TEXACO 2",
            "track": "AR-625H/L",
            "aircraft": "KC-135",
        }
        tanker.remember_tanker(state, missing, config=cfg)
        if state.get("tanker_freq_mhz") is None or abs(
            float(state["tanker_freq_mhz"]) - 319.8
        ) > 0.01:
            fails.append(
                f"remember_tanker should look up 319.8, got {state.get('tanker_freq_mhz')}"
            )

        server = atc_server.AtcServer(
            cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
        )
        hello = server.hello(
            {
                "callsign_override": "Fleece 1",
                "opus_flight_id": 88,
                "opus_seat": 1,
                "radio_fresh": False,
            }
        )
        sess = server.get_session(hello["session_id"])
        if sess is None:
            return fails + ["hello did not create a tanker-freq session"]

        def peel(engine: flow_engine.FlowEngine) -> dict:
            tanker.remember_tanker(engine.state, dict(rows[1]), config=engine.config)
            tanker.enter_tanker_overlay(engine)
            return {"action": "ok", "channel": "blackjack", "freq": 251.0, "text": "texaco two"}

        result = server.run_action(sess, peel, {"radio_fresh": False})
        st = sess.public_status()
        attached = atc_server._attach_fly_status(result, st)
        mhz = attached.get("step_freq_mhz")
        if mhz is None or abs(float(mhz) - 319.8) > 0.01:
            fails.append(f"host Fly freq should be Texaco 2 319.8, got {mhz}")
        if str(attached.get("status_channel") or "").lower() != "tanker":
            fails.append(
                f"status_channel should be tanker, got {attached.get('status_channel')}"
            )
        if abs(float(attached.get("freq") or 0) - 251.0) > 0.01:
            fails.append("TX freq must stay Blackjack, not overwrite Fly tanker UHF")
        fs = st.get("flow_state") or {}
        if fs.get("tanker_freq_mhz") is None or abs(
            float(fs["tanker_freq_mhz"]) - 319.8
        ) > 0.01:
            fails.append(f"flow_state tanker_freq_mhz {fs.get('tanker_freq_mhz')}")
        # Client Fly used to skip host UHF when the shared cursor was still
        # Blackjack / Bandsaw — then catalog[0] (Texaco 1 / 322.3) won.
        local_ch = "blackjack"
        host_ch = str(attached.get("status_channel") or "").strip().lower()
        host_mhz = attached.get("step_freq_mhz")
        on_tanker = bool(attached.get("on_tanker"))
        use_host = host_mhz is not None and (
            on_tanker or not host_ch or host_ch == local_ch
        )
        if not on_tanker:
            fails.append("request tanker should mark on_tanker for the Client Fly page")
        elif not use_host or abs(float(host_mhz) - 319.8) > 0.01:
            fails.append(
                "client Fly must keep Texaco 2 319.8 when local cursor is still C2"
            )
    finally:
        tanker.fetch_opus_tankers = orig
        tanker._TANKER_CACHE["exp"] = 0.0
    return fails


def test_cross_flight_status_isolation() -> list[str]:
    """One flight's tanker / radios / identity must not stick on the Host engine."""
    fails: list[str] = []
    rows = [
        {
            "id": 2,
            "callsign": "TEXACO 2",
            "track": "AR-625H/L",
            "aircraft": "KC-135",
            "freq_mhz": 319.8,
            "boom": True,
        }
    ]
    orig = tanker.fetch_opus_tankers
    tanker.fetch_opus_tankers = lambda *a, **k: list(rows)
    tanker._TANKER_CACHE["exp"] = 0.0
    try:
        cfg = _host_config()
        cfg["opus_flight_id"] = 101
        host_eng = flow_engine.FlowEngine(
            config=cfg,
            persist_state=False,
            mission=copy.deepcopy(MISSION),
            airports=AIRPORTS,
        )
        host_eng.config["callsign_override"] = "HOST BOX"
        server = atc_server.AtcServer(
            cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
        )
        server.host_engine = host_eng
        fleece = server.hello(
            {
                "callsign_override": "Fleece 1",
                "opus_flight_id": 101,
                "opus_seat": 1,
                "radio_fresh": False,
            }
        )
        viper = server.hello(
            {
                "callsign_override": "Viper 3",
                "opus_flight_id": 202,
                "opus_seat": 1,
                "radio_fresh": False,
            }
        )
        sf = server.get_session(fleece["session_id"])
        sv = server.get_session(viper["session_id"])
        if sf is None or sv is None:
            return fails + ["hello did not create both flights"]
        if sf.engine is not host_eng:
            fails.append("Fleece should share the Host Fly engine")
        if sv.engine is host_eng:
            fails.append("Viper must not share Fleece/Host engine")
        idx_v = int(sv.engine.state.get("index") or 0)
        idx_f = int(sf.engine.state.get("index") or 0)

        def peel(engine: flow_engine.FlowEngine) -> dict:
            tanker.remember_tanker(engine.state, dict(rows[0]), config=engine.config)
            tanker.enter_tanker_overlay(engine)
            return {"action": "ok", "channel": "blackjack", "freq": 251.0}

        server.run_action(
            sf,
            peel,
            {
                "tuned_freqs_mhz": [251.25],
                "radio_fresh": True,
                "selected_mhz": 251.25,
            },
        )
        if tanker.tanker_overlay_active(sv.local_state):
            fails.append("Viper local_state inherited Fleece tanker overlay")
        pv = sv.public_status()
        if pv.get("on_tanker"):
            fails.append("Viper Fly status marked on_tanker after Fleece peel")
        if int(sv.engine.state.get("index") or 0) != idx_v:
            fails.append("Viper cursor moved when Fleece went tanker")
        if int(sf.engine.state.get("index") or 0) != idx_f:
            fails.append("Fleece shared C2 cursor stayed parked on tanker")
        if getattr(host_eng, "remote_radios", None) is not None:
            fails.append("Fleece radios stuck on the Host engine after the action")
        if str(host_eng.config.get("callsign_override") or "") != "HOST BOX":
            fails.append(
                f"Host identity became {host_eng.config.get('callsign_override')!r}"
            )
        if str(sv.engine.config.get("callsign_override") or "") == "Fleece 1":
            fails.append("Viper engine stamped with Fleece identity")

        pf = sf.public_status()
        attached = atc_server._attach_fly_status({"action": "ok"}, pf)
        if attached.get("session_id") != sf.session_id:
            fails.append("attached Fly status missing Fleece session_id")
        if attached.get("opus_flight_id") in (202, "202"):
            fails.append("Fleece status carried Viper's flight id")
        if pv.get("session_id") == pf.get("session_id"):
            fails.append("two flights shared a status session_id")

        match = voice_intent.Match(
            intent="bandsaw_check_in",
            kind="step",
            template="bandsaw_check_in",
            confidence=1.0,
            slots={"channel": "bandsaw"},
            transcript="Bandsaw, Fleece 1, checking in",
            normalized="bandsaw fleece 1 checking in",
        )

        def checkin(engine: flow_engine.FlowEngine) -> dict:
            return voice_engine.execute_intent(match, engine)

        server.run_action(sf, checkin, {"radio_fresh": False})
        if int(sf.engine.state.get("index") or 0) != idx_f:
            fails.append("Bandsaw check-in must not move the shared flight cursor")
        if int(sv.engine.state.get("index") or 0) != idx_v:
            fails.append("Viper cursor moved on Fleece Bandsaw check-in")
    finally:
        tanker.fetch_opus_tankers = orig
        tanker._TANKER_CACHE["exp"] = 0.0
    return fails


def test_connect_error_hints() -> list[str]:
    fails: list[str] = []
    timed = atc_net.describe_connect_failure(
        TimeoutError("timed out"),
        "http://192.168.1.9:8766/v1/intent",
        local_ips=["192.168.1.50"],
    )
    if "timed out" not in timed or "Firewall" not in timed:
        fails.append(f"timeout hint: {timed}")
    loop = atc_net.describe_connect_failure(
        TimeoutError("timed out"),
        "http://127.0.0.1:8766/v1/health",
        local_ips=["10.1.10.131"],
    )
    if "this pc" not in loop.casefold():
        fails.append(f"loopback hint: {loop}")
    refused = atc_net.describe_connect_failure(
        OSError("[WinError 10061] connection refused"),
        "http://10.0.0.5:8766/v1/hello",
        local_ips=["10.0.0.8"],
    )
    if "refused" not in refused.casefold():
        fails.append(f"refused hint: {refused}")
    mismatch = atc_net.describe_connect_failure(
        TimeoutError("timed out"),
        "http://192.168.50.20:8766/v1/health",
        local_ips=["10.1.10.131"],
    )
    if "10.1.10.131" not in mismatch or "8766" not in mismatch:
        fails.append(f"subnet hint: {mismatch}")
    sample = (
        "Wireless LAN adapter Wi-Fi:\n"
        "   IPv4 Address. . . . . . . . . . . : 10.1.10.131\n"
        "Ethernet adapter vEthernet (Default Switch):\n"
        "   IPv4 Address. . . . . . . . . . . : 192.168.50.20\n"
    )
    parsed = atc_net._parse_ipconfig(sample)
    ips = [r["ip"] for r in parsed]
    if ips != ["10.1.10.131", "192.168.50.20"]:
        fails.append(f"ipconfig parse: {parsed}")
    return fails


def test_persist_state_off() -> list[str]:
    fails: list[str] = []
    cfg = _host_config()
    eng = flow_engine.FlowEngine(
        config=cfg,
        persist_state=False,
        mission=copy.deepcopy(MISSION),
        airports=AIRPORTS,
    )
    eng.state["index"] = 4
    eng.save_state()
    disk = HERE / "flow_state.json"
    if disk.is_file():
        try:
            data = json.loads(disk.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if int(data.get("index") or 0) == 4:
            fails.append("session engine wrote index=4 into shared flow_state.json")
    return fails


def test_injected_radio_gate() -> list[str]:
    fails: list[str] = []
    radio = srs_radio.RadioState(
        source="client", freqs_mhz=[275.8], fresh=True, age_s=0.0
    )
    cfg = _host_config(freq_gate_enabled=True)
    airport = AIRPORTS["nellis"]
    step = {"channel": "ground", "phase": "departure"}
    allowed, msg, result = srs_radio.check_freq_gate(
        cfg, airport, step, radio=radio
    )
    if not allowed or result != "match":
        fails.append(f"client 275.8 should match Ground, got {result} {msg}")
    radio2 = srs_radio.RadioState(
        source="client", freqs_mhz=[132.65], fresh=True, age_s=0.0
    )
    allowed2, _msg2, result2 = srs_radio.check_freq_gate(
        cfg, airport, step, radio=radio2
    )
    if allowed2 or result2 != "mismatch":
        fails.append(f"client VHF should miss Ground, got {result2}")
    eam_cfg = _host_config(freq_gate_eam_enabled=True)
    srs_radio.apply_config(eam_cfg)
    client_sel = srs_radio.RadioState(
        source="client",
        freqs_mhz=[123.625],
        selected_mhz=123.625,
        fresh=True,
        age_s=0.0,
    )
    forced, _mod = srs_radio.maybe_force_eam_tx_freq(
        eam_cfg, 289.4, "AM", radio=client_sel
    )
    if abs(forced - 123.625) > 0.01:
        fails.append(f"EAM host TX should use client selected 123.625, got {forced}")
    srs_radio.apply_config(_host_config(freq_gate_eam_enabled=False))
    return fails


def test_secret_redaction_and_session_tts_cap() -> list[str]:
    import tempfile

    fails: list[str] = []
    leaked = atc_phrase.redact_secrets(
        {
            "ok": True,
            "google_credentials": "C:/secret.json",
            "nested": {"atc_token": "abc", "callsign": "Fleece 1"},
        }
    )
    if "google_credentials" in leaked:
        fails.append("google_credentials leaked")
    nested = leaked.get("nested") if isinstance(leaked.get("nested"), dict) else {}
    if "atc_token" in nested:
        fails.append("atc_token leaked")
    if leaked.get("ok") is not True or nested.get("callsign") != "Fleece 1":
        fails.append("redact stripped public fields")

    tmp = Path(tempfile.mkdtemp()) / "tts_usage.json"
    old = atc_phrase._tts_usage_path_override
    atc_phrase._tts_usage_path_override = tmp
    try:
        atc_phrase.record_google_tts_usage(
            "en-US-Neural2-D", "x" * 100, session_id="flight:1:1"
        )
        cfg = {"tts_session_char_cap": 50}
        if not atc_phrase.session_over_google_cap("flight:1:1", cfg):
            fails.append("expected session over cap at 100/50")
        if atc_phrase.session_over_google_cap("flight:2:1", cfg):
            fails.append("other session should still be under cap")
        if atc_phrase.session_over_google_cap("flight:1:1", {"tts_session_char_cap": 0}):
            fails.append("cap 0 should disable per-pilot limit")
    finally:
        atc_phrase._tts_usage_path_override = old
    return fails


def test_client_auto_play_from_watch() -> list[str]:
    """Flying-PC Watch asks the host to play; a second auto of the same step is refused."""
    fails: list[str] = []
    cfg = _host_config(auto_clearance_enabled=True)
    spoken: list[str] = []

    def tx(job: dict) -> int:
        spoken.append(str(job.get("text") or job.get("callsign") or "tx"))
        return 0

    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=tx
    )
    hello = server.hello(
        {
            "opus_flight_id": 9,
            "opus_seat": 1,
            "opus_flight_label": "WILD 6",
            "radio_fresh": False,
        }
    )
    if hello.get("auto_clearance_enabled") is not True:
        fails.append("hello should advertise host Watch on")
    sess = server.get_session(hello["session_id"])
    if sess is None:
        return fails + ["hello did not create a session"]
    sid = str((sess.engine.current_step() or {}).get("id") or "")
    if not sid:
        return fails + ["mission has no current step id"]
    played = server.handle_command(
        sess, "play", {"step_id": sid, "auto": True, "radio_fresh": False}
    )
    if played.get("action") == "blocked":
        fails.append(f"first auto play should TX, got {played.get('detail')}")
    if str(sess.engine.state.get("last_step_id") or "") != sid:
        fails.append("auto play should stamp last_step_id")
    again = server.handle_command(
        sess, "play", {"step_id": sid, "auto": True, "radio_fresh": False}
    )
    if again.get("action") != "blocked" or "already played" not in str(
        again.get("detail") or ""
    ).lower():
        fails.append(f"second auto play of {sid} should be already-played, got {again}")
    hb = server.heartbeat(sess, {"radio_fresh": False})
    if hb.get("auto_clearance_enabled") is not True:
        fails.append("heartbeat should keep advertising host Watch")
    return fails


def test_client_tanker_chat_gap() -> list[str]:
    """Client continue is refused until the break timer; a provided line TXes once."""
    fails: list[str] = []
    cfg = _host_config()
    spoken: list[str] = []

    def tx(job: dict) -> int:
        spoken.append(str(job.get("text") or ""))
        return 0

    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=tx
    )
    hello = server.hello(
        {
            "opus_flight_id": 11,
            "opus_seat": 1,
            "callsign_override": "WILD 6",
            "radio_fresh": False,
        }
    )
    sess = server.get_session(hello["session_id"])
    if sess is None:
        return ["hello did not create a session"]
    sess.local_state = {
        "tanker_overlay": True,
        "tanker_chat": {
            "session": True,
            "choices": [],
            "next_at": time.time() + 40,
        },
    }
    early = server.handle_command(
        sess,
        "tanker_chat",
        {"continue": True, "auto": True, "radio_fresh": False},
    )
    if early.get("action") != "none":
        fails.append(f"continue while waiting should be none, got {early}")
    if spoken:
        fails.append("host must not TX boom chat during the gap")
    sess.local_state["tanker_chat"] = {
        "session": True,
        "choices": [],
        "next_at": time.time() - 1,
    }
    # A boom line that asks a question holds the bit open for the pilot
    # instead of arming another opener on top of him.
    question = "Coffee holding up okay up there?"
    played = server.handle_command(
        sess,
        "tanker_chat",
        {
            "continue": True,
            "auto": True,
            "text": question,
            "radio_fresh": False,
        },
    )
    got = str(played.get("text") or "")
    if question not in got:
        fails.append(f"client-generated boom line should TX, got {played}")
    row = (sess.local_state or {}).get("tanker_chat") or {}
    if row.get("next_at") is not None or row.get("awaiting") != "react":
        fails.append(f"a boom question should wait for the pilot, got {row}")

    # A line that closes itself out arms the next gap.
    sess.local_state["tanker_chat"] = {
        "session": True,
        "choices": [],
        "next_at": time.time() - 1,
    }
    line = "Boom's showing a green light back here."
    played = server.handle_command(
        sess,
        "tanker_chat",
        {
            "continue": True,
            "auto": True,
            "text": line,
            "radio_fresh": False,
        },
    )
    if line not in str(played.get("text") or ""):
        fails.append(f"client-generated boom line should TX, got {played}")
    row = (sess.local_state or {}).get("tanker_chat") or {}
    try:
        nxt = float(row.get("next_at") or 0)
    except (TypeError, ValueError):
        nxt = 0.0
    if nxt <= time.time():
        fails.append("host should arm the next boom-chat gap after TX")
    return fails


def test_voice_tanker_chat_updates_flow_state() -> list[str]:
    """Voice start/stop must land on the seat flow_state Fly paints from."""
    fails: list[str] = []
    spoken: list[str] = []

    def tx(job: dict) -> int:
        spoken.append(str(job.get("text") or ""))
        return 0

    server = atc_server.AtcServer(
        _host_config(), AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=tx
    )
    hello = server.hello(
        {
            "opus_flight_id": 11,
            "opus_seat": 1,
            "callsign_override": "WILD 6",
            "radio_fresh": False,
        }
    )
    sess = server.get_session(hello["session_id"])
    if sess is None:
        return ["hello did not create a session"]
    sess.local_state = {
        "tanker_overlay": True,
        "tanker_callsign": "TEXACO 1",
        "tanker_freq_mhz": 317.5,
    }
    started = server.handle_intent(
        sess,
        {
            "intent": "tanker_chat_start",
            "kind": "request",
            "transcript": "how's it going",
            "normalized": "how's it going",
            "radio_fresh": False,
        },
    )
    fs = started.get("flow_state") if isinstance(started.get("flow_state"), dict) else {}
    row = fs.get("tanker_chat") if isinstance(fs.get("tanker_chat"), dict) else {}
    if not row.get("session"):
        fails.append(f"voice start should set tanker_chat.session on flow_state, got {fs}")
    stopped = server.handle_intent(
        sess,
        {
            "intent": "tanker_chat_stop",
            "kind": "request",
            "transcript": "talk later",
            "normalized": "talk later",
            "radio_fresh": False,
        },
    )
    fs2 = stopped.get("flow_state") if isinstance(stopped.get("flow_state"), dict) else {}
    if fs2.get("tanker_chat"):
        fails.append(f"voice stop should clear tanker_chat on flow_state, got {fs2}")
    return fails


def test_wild6_ownship_and_shared_cursor() -> list[str]:
    fails: list[str] = []
    if not atc_phrase.same_flight_callsign("WILD 6", "WILD 61"):
        fails.append("WILD 6 should match CAOC WILD 61")
    if not atc_phrase.same_flight_callsign("WILD 6 · KLSV DCT", "WILD 6-1"):
        fails.append("picker label should match WILD 6-1")
    if atc_phrase.same_flight_callsign("WILD 6", "VIPER 1"):
        fails.append("WILD 6 must not match VIPER 1")
    if atc_phrase.clean_flight_callsign("WILD 6 · seat 1") != "WILD 6":
        fails.append("clean_flight_callsign should take the first bit")

    units = [
        {
            "type": "air",
            "id": "viper",
            "name": "Viper",
            "flightLabel": "VIPER 11",
            "xMeters": 0,
            "zMeters": 0,
        },
        {
            "type": "air",
            "id": "wild",
            "name": "Wild",
            "flightLabel": "WILD 61",
            "xMeters": 10,
            "zMeters": 10,
        },
    ]
    own = atc_phrase.match_caoc_unit_for_flight(
        units,
        callsign="WILD 6",
        config={"opus_flight_label": "WILD 6 · KLSV"},
    )
    if not own or str(own.get("id")) != "wild":
        fails.append(f"ownship should be WILD 61, got {own}")

    by_pilot = atc_phrase.match_caoc_unit_for_flight(
        [
            {
                "type": "air",
                "id": "wrong-cs",
                "name": "F-16C_50",
                "flightLabel": "HOBO 11",
                "pilotName": "Sterling",
                "xMeters": 0,
                "zMeters": 0,
            },
            {
                "type": "air",
                "id": "other",
                "name": "Viper",
                "flightLabel": "VIPER 11",
                "xMeters": 1,
                "zMeters": 1,
            },
        ],
        callsign="WILD 6",
        config={"opus_user_name": "Sterling"},
    )
    if not by_pilot or str(by_pilot.get("id")) != "wrong-cs":
        fails.append(f"pilot-name fallback should find Sterling, got {by_pilot}")

    tagged = atc_phrase.match_caoc_unit_for_flight(
        [
            {
                "type": "air",
                "id": "viper",
                "name": "Sterling",
                "flightLabel": "VIPER 11",
                "opusFlightId": 99,
                "xMeters": 0,
                "zMeters": 0,
            },
            {
                "type": "air",
                "id": "wild",
                "name": "Wild",
                "flightLabel": "WILD 61",
                "opusFlightId": 42,
                "xMeters": 10,
                "zMeters": 10,
            },
        ],
        callsign="WILD 6",
        config={"opus_flight_id": 42, "opus_user_name": "Sterling"},
    )
    if not tagged or str(tagged.get("id")) != "wild":
        fails.append(f"opusFlightId must beat another flight's pilot name, got {tagged}")

    host_eng = flow_engine.FlowEngine(
        config=_host_config(),
        persist_state=False,
        mission=copy.deepcopy(MISSION),
        airports=AIRPORTS,
    )
    idx0 = int(host_eng.state.get("index") or 0)
    server = atc_server.AtcServer(
        _host_config(), AIRPORTS, lambda: copy.deepcopy(MISSION)
    )
    server.host_engine = host_eng
    hello = server.hello(
        {
            "opus_flight_id": 42,
            "opus_seat": 1,
            "opus_flight_label": "WILD 6 · KLSV DCT · seat 1",
            "opus_user_name": "tester",
            "radio_fresh": False,
        }
    )
    if str(hello.get("callsign") or "") != "WILD 6":
        fails.append(f"hello callsign should be WILD 6, got {hello.get('callsign')}")
    sess = server.get_session(hello["session_id"])
    if sess is None:
        return fails + ["hello did not create a Wild 6 session"]
    if sess.engine is not host_eng:
        fails.append("dedicated host should share the Fly engine with the only client")
    server.handle_command(sess, "seek_relative", {"delta": 1, "radio_fresh": False})
    if int(host_eng.state.get("index") or 0) == idx0:
        fails.append("client Step should move the host Fly cursor")
    return fails


def test_seat_position_beats_host_feed() -> list[str]:
    """
    The host must gate on the client's fix, never on its own map / CAOC feed.

    Host Fly seeing a parked contact 0.8 NM out is what fired the 12 NM tower
    handoff and the 6 NM landing clearance on a jet still 38 NM from Nellis.
    """
    fails: list[str] = []
    cfg = _host_config(ownship_from_map=True)
    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
    )
    hello = server.hello({"callsign_override": "Fleece 1", "opus_flight_id": 707})
    sess = server.get_session(hello["session_id"])
    if sess is None:
        return ["no session"]
    nellis = AIRPORTS["nellis"]
    field = runway_position.airport_field_latlon(nellis)

    # Host-side feed says the jet is on the field, and a stale cached fix agrees.
    parked = [field[0] + 0.012, field[1]]
    atc_phrase.write_ownship_inject(
        lat=parked[0], lon=parked[1], alt_ft_agl=0, heading_deg=210,
        speed_kt=0, callsign="Fleece 1", airport=nellis,
    )
    sess.engine.state["ownship_ll"] = list(parked)
    sess.engine.state["ownship_ll_t"] = time.time()
    try:
        # Client reports 38 NM out on the KRYSS side.
        inbound = (36.79, -114.72)
        sess.apply_ownship({"ownship_ll": [inbound[0], inbound[1]]})
        with atc_server._session_engine_binding(sess):
            dist = runway_position.ownship_distance_nm(
                nellis, config=sess.engine.config, state=sess.engine.state
            )
        if dist is None or abs(dist - 36.5) > 2.0:
            fails.append(f"host should gate on the client's 38 NM fix, got {dist}")

        # No client fix at all must hold the gates, not fall back to the host.
        sess.apply_ownship({"ownship_ll": None})
        with atc_server._session_engine_binding(sess):
            blind = runway_position.ownship_distance_nm(
                nellis, config=sess.engine.config, state=sess.engine.state
            )
        if blind is not None:
            fails.append(f"no client fix should read as unknown, got {blind}")
        trig = runway_position.resolve_step_trigger(
            {"template": "clear_land", "trigger": {"when": "inside"}},
            mission=MISSION,
            state={"approach_plan": {"pattern": "instrument"}},
        )
        held, _why = runway_position.within_nm_held(trig, blind)
        if held:
            fails.append("landing clearance must not fire without a position")

        # Outside the binding the host's own feed is fine again (solo Fly).
        solo = runway_position.ownship_distance_nm(
            nellis, config=sess.engine.config, state={}
        )
        if solo is None or solo > 3.0:
            fails.append(f"host solo Fly should still use its own feed, got {solo}")
    finally:
        atc_phrase.clear_ownship_inject()
    return fails


def test_client_sends_its_own_fix() -> list[str]:
    """Every client request carries this PC's fix so the host can gate on it."""
    fails: list[str] = []
    import atc_client

    cfg = _host_config(atc_role="client", ownship_from_map=True)
    nellis = AIRPORTS["nellis"]
    atc_phrase.write_ownship_inject(
        lat=36.79, lon=-114.72, alt_ft_agl=17000, heading_deg=210,
        speed_kt=400, callsign="Fleece 1", airport=nellis,
    )
    try:
        payload = atc_client._radio_payload(cfg)
        if "ownship_ll" not in payload:
            fails.append("radio payload should always carry ownship_ll")
        ll = payload.get("ownship_ll")
        if not isinstance(ll, list) or len(ll) != 2:
            fails.append(f"client should send its fix, got {ll}")
        elif abs(ll[0] - 36.79) > 0.01 or abs(ll[1] + 114.72) > 0.01:
            fails.append(f"client sent the wrong fix: {ll}")
    finally:
        atc_phrase.clear_ownship_inject()

    # Route Tester drive_fly still reports when "map is my jet" is off.
    cfg_off = _host_config(atc_role="client", ownship_from_map=False)
    atc_phrase.write_ownship_inject(
        lat=36.79, lon=-114.72, alt_ft_agl=17000, heading_deg=210,
        speed_kt=400, callsign="Fleece 1", airport=nellis,
    )
    try:
        via_inject = atc_client._radio_payload(cfg_off).get("ownship_ll")
        if not isinstance(via_inject, list) or abs(via_inject[0] - 36.79) > 0.01:
            fails.append(f"drive_fly inject should still report to host: {via_inject}")
    finally:
        atc_phrase.clear_ownship_inject()

    # Unknown position sends an explicit null, not a missing key.
    blind = atc_client._radio_payload(cfg_off)
    if "ownship_ll" not in blind:
        fails.append("unknown position should still send the key")
    elif blind.get("ownship_ll") is not None:
        fails.append(f"unknown position should send null, got {blind.get('ownship_ll')}")
    return fails


def test_host_config_not_polluted_by_client_flight() -> list[str]:
    """
    Host Fly shares its config dict with host_engine. unique_flow_sessions must
    not stamp a Client Opus flight onto that dict (top bar / 404 probe source).
    """
    fails: list[str] = []
    cfg = _host_config(opus_backend_url="https://opus.example/backend")
    cfg["opus_user_name"] = "Turtle"
    host_eng = flow_engine.FlowEngine(
        config=cfg,
        persist_state=False,
        mission=copy.deepcopy(MISSION),
        airports=AIRPORTS,
    )
    server = atc_server.AtcServer(
        cfg, AIRPORTS, lambda: copy.deepcopy(MISSION), transmit_fn=lambda _j: 0
    )
    server.host_engine = host_eng
    server.hello(
        {
            "callsign_override": "RAZOR 1",
            "opus_flight_id": 909,
            "opus_seat": 1,
            "opus_flight_label": "RAZOR 1",
            "radio_fresh": False,
        }
    )
    reps = server.unique_flow_sessions()
    if len(reps) != 1:
        fails.append(f"expected 1 unique flow, got {len(reps)}")
    if cfg.get("opus_flight_id") not in (None, ""):
        fails.append(
            f"Host config should stay flight-less, got opus_flight_id={cfg.get('opus_flight_id')}"
        )
    if cfg.get("opus_seat") not in (None, ""):
        fails.append(f"Host config should not inherit seat, got {cfg.get('opus_seat')}")
    if str(cfg.get("callsign_override") or "").strip():
        fails.append(
            f"Host config should not inherit callsign, got {cfg.get('callsign_override')}"
        )
    if atc_net.role_of(cfg) != "host":
        fails.append(f"Host role should stay host after unique_flow, got {cfg.get('atc_role')}")
    sess = reps[0] if reps else None
    if sess is not None:
        with atc_server._session_engine_binding(sess):
            if atc_phrase.configured_opus_flight_id(sess.engine.config) != 909:
                fails.append("bound seat should see client flight_id 909")
        if cfg.get("opus_flight_id") not in (None, ""):
            fails.append("Host config flight_id leaked after session bind")
        if atc_net.role_of(cfg) != "host":
            fails.append(
                f"Host role should restore after bind, got {cfg.get('atc_role')}"
            )
    # Host without a selected flight must not scan Opus by username.
    ctx = atc_phrase.resolve_active_opus_flight(cfg)
    if ctx is None or str(getattr(ctx, "radio_callsign", "") or "") != "HOST":
        fails.append(
            f"Host with no flight_id should use synthetic HOST, got "
            f"{None if ctx is None else ctx.radio_callsign}"
        )
    return fails


def main() -> int:
    tests = (
        test_session_key,
        test_hello_reclaims_identity_upgrade,
        test_connect_error_hints,
        test_persist_state_off,
        test_injected_radio_gate,
        test_channel_parallel_and_fifo,
        test_session_isolation,
        test_clear_flight_cache_scoped,
        test_flight_shared_cursor,
        test_element_tanker_peel,
        test_tanker_fly_freq_matches_pick,
        test_cross_flight_status_isolation,
        test_client_freq_gate,
        test_empty_radio_snapshot_keeps_bank,
        test_secret_redaction_and_session_tts_cap,
        test_client_auto_play_from_watch,
        test_client_tanker_chat_gap,
        test_voice_tanker_chat_updates_flow_state,
        test_wild6_ownship_and_shared_cursor,
        test_seat_position_beats_host_feed,
        test_client_sends_its_own_fix,
        test_host_config_not_polluted_by_client_flight,
    )
    bad = 0
    for fn in tests:
        fails = fn()
        if fails:
            bad += len(fails)
            for line in fails:
                print(f"  FAIL {fn.__name__}: {line}")
        else:
            print(f"  ok   {fn.__name__}")
    if bad:
        print(f"{bad} failure(s)")
        return 1
    print("check_multi_pilot: all ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
