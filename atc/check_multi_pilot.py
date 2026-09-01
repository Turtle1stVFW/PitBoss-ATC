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
import channel_tx
import flow_engine
import srs_radio
import tanker

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
    # Delivery is 289.4 at Nellis
    try:
        server.handle_command(
            sess,
            "next",
            {"tuned_freqs_mhz": [289.4], "radio_fresh": True},
        )
    except RuntimeError as exc:
        fails.append(f"on-freq Delivery should allow: {exc}")
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


def main() -> int:
    tests = (
        test_session_key,
        test_connect_error_hints,
        test_persist_state_off,
        test_injected_radio_gate,
        test_channel_parallel_and_fifo,
        test_session_isolation,
        test_flight_shared_cursor,
        test_element_tanker_peel,
        test_client_freq_gate,
        test_secret_redaction_and_session_tts_cap,
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
