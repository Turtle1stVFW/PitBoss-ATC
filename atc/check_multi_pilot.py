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
        fails.append("dash-1 and dash-2 must be different sessions")
    if k1 != k3:
        fails.append("same flight/seat should resume the same session key")
    return fails


def test_connect_error_hints() -> list[str]:
    fails: list[str] = []
    timed = atc_net.describe_connect_failure(
        TimeoutError("timed out"), "http://192.168.1.9:8766/v1/intent"
    )
    if "timed out" not in timed or "Firewall" not in timed:
        fails.append(f"timeout hint: {timed}")
    loop = atc_net.describe_connect_failure(
        TimeoutError("timed out"), "http://127.0.0.1:8766/v1/health"
    )
    if "this pc" not in loop.casefold():
        fails.append(f"loopback hint: {loop}")
    refused = atc_net.describe_connect_failure(
        OSError("[WinError 10061] connection refused"),
        "http://10.0.0.5:8766/v1/hello",
    )
    if "refused" not in refused.casefold():
        fails.append(f"refused hint: {refused}")
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
