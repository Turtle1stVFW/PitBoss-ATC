"""
One-PC fake pilots: talk to a running Host UI without a second computer.

1. Open the Flow app → Setup → Squadron → Host → Save
2. Switch to the Traffic tab
3. Run:  py -3 fake_pilots.py
   or double-click Test-Fake-Pilots.cmd
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atc_net  # noqa: E402

CONFIG_PATH = HERE / "config.json"

PILOTS = (
    {
        "callsign_override": "Fleece 1",
        "opus_flight_id": 9001,
        "opus_seat": 1,
        "opus_flight_label": "Fleece",
    },
    {
        "callsign_override": "Viper 3",
        "opus_flight_id": 9002,
        "opus_seat": 1,
        "opus_flight_label": "Viper",
    },
)


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise SystemExit(
            f"No {CONFIG_PATH.name}. Copy config.example.json to config.json first."
        )
    with CONFIG_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit("config.json is not an object")
    return data


def _request(
    base: str,
    method: str,
    path: str,
    token: str,
    body: dict[str, Any] | None = None,
    *,
    session_id: str = "",
) -> dict[str, Any]:
    headers = {atc_net.TOKEN_HEADER: token}
    data = None
    if body is not None and method != "GET":
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(body).encode("utf-8")
    if session_id:
        headers["X-ATC-Session"] = session_id
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit(f"{exc.code} {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            "Host is not running on "
            f"{base}.\n\n"
            "1. Double-click Open-ATC-Setup.cmd\n"
            "2. Setup → Squadron → Host → Save\n"
            "3. Open the Traffic tab, then run this again."
        ) from exc
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        raise SystemExit(f"{path} returned non-object JSON")
    return payload


def _health(base: str) -> None:
    req = urllib.request.Request(base + "/v1/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.URLError as exc:
        raise SystemExit(
            "Host is not running on "
            f"{base}.\n\n"
            "1. Double-click Open-ATC-Setup.cmd\n"
            "2. Setup → Squadron → Host → Save\n"
            "3. Open the Traffic tab, then run this again."
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Fake two pilots against a local ATC host")
    parser.add_argument(
        "--hold",
        type=float,
        default=20.0,
        help="seconds to keep heartbeating so Traffic stays populated (default 20)",
    )
    args = parser.parse_args()

    cfg = _load_config()
    role = atc_net.role_of(cfg)
    token = atc_net.token_of(cfg)
    port = int(cfg.get("atc_port") or atc_net.DEFAULT_ATC_PORT)
    base = f"http://127.0.0.1:{port}"

    if role != "host":
        print(
            f"config.json atc_role is '{role}', not host.\n"
            "Open the app → Setup → Squadron → Host → Save, then run this again."
        )
        return 2
    if not token:
        print(
            "No atc_token in config.json. Host → Save once so a token is written, "
            "then run this again."
        )
        return 2

    print(f"Checking {base} …")
    _health(base)

    sessions: list[dict[str, Any]] = []
    for ident in PILOTS:
        body = dict(ident)
        body["radio_fresh"] = False
        body["tuned_freqs_mhz"] = []
        hello = _request(base, "POST", "/v1/hello", token, body)
        sid = str(hello.get("session_id") or "")
        callsign = str(hello.get("callsign") or ident["callsign_override"])
        print(f"  hello  {callsign}  session={sid}")
        sessions.append({"id": sid, "callsign": callsign, **ident})

    print("Both pilots Advance (same first step = Delivery — should queue FIFO) …")
    for sess in sessions:
        result = _request(
            base,
            "POST",
            "/v1/next",
            token,
            {
                "session_id": sess["id"],
                "radio_fresh": False,
                "tuned_freqs_mhz": [],
            },
        )
        q = result.get("queue_pos")
        ch = result.get("channel") or ""
        label = result.get("label") or result.get("text") or result.get("action")
        extra = f"  queue {q}" if q else ""
        print(f"  next   {sess['callsign']}  {ch}  {label}{extra}")

    traffic = _request(base, "GET", "/v1/traffic", token)
    print("")
    print("Traffic tab should now show:")
    for sess in traffic.get("sessions") or []:
        print(
            f"  {sess.get('callsign')}  step {sess.get('step_number')}/"
            f"{sess.get('total')}  {sess.get('channel') or '—'}"
        )
    queues = traffic.get("queues") or {}
    busy = [
        f"  {ch}: speaking {(info.get('speaking') or {}).get('callsign') or 'idle'}  "
        f"queued {info.get('queued') or 0}"
        for ch, info in queues.items()
        if info.get("speaking") or info.get("queued")
    ]
    if busy:
        print("Channels:")
        print("\n".join(busy))
    if cfg.get("dry_run"):
        print("\ndry_run=true — no SRS audio, queue still runs.")
    else:
        print("\nIf SRS is running locally you should hear Delivery speak twice, in order.")

    hold = max(0.0, float(args.hold))
    if hold:
        print(f"\nKeeping them connected for {hold:.0f}s — watch Traffic …")
        deadline = time.time() + hold
        while time.time() < deadline:
            for sess in sessions:
                try:
                    _request(
                        base,
                        "POST",
                        "/v1/heartbeat",
                        token,
                        {
                            "session_id": sess["id"],
                            "radio_fresh": False,
                            "tuned_freqs_mhz": [],
                        },
                    )
                except SystemExit as exc:
                    print(exc)
                    return 1
            time.sleep(1.0)
        print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
