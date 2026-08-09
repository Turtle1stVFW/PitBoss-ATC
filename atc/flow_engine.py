#!/usr/bin/env python3
"""
Mission flight flow engine: next/back/reset/flip/play + localhost HTTP control.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atc_phrase  # noqa: E402

CONFIG_PATH = HERE / "config.json"
AIRPORTS_PATH = HERE / "airports.json"
STATE_PATH = HERE / "flow_state.json"


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def resolve_flow_path(config: dict[str, Any]) -> Path:
    rel = config.get("flow_file") or "flows/nellis_default.json"
    path = Path(rel)
    if not path.is_absolute():
        path = HERE / path
    return path


def mission_steps(mission: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Full timeline steps.
    Prefers mission['steps']; legacy outbound+inbound are concatenated.
    """
    if isinstance(mission.get("steps"), list):
        return mission["steps"]
    outbound = list(mission.get("outbound") or [])
    inbound = list(mission.get("inbound") or [])
    return outbound + inbound


def enabled_steps(mission: dict[str, Any], direction: str | None = None) -> list[dict[str, Any]]:
    """Enabled steps from the full timeline. `direction` ignored (legacy compat)."""
    del direction  # unused — single timeline
    return [s for s in mission_steps(mission) if s.get("enabled", True)]


def normalize_mission_to_steps(mission: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize mission to a single steps list (drops outbound/inbound keys)."""
    steps = mission_steps(mission)
    mission["steps"] = steps
    mission.pop("outbound", None)
    mission.pop("inbound", None)
    return mission


class FlowEngine:
    def __init__(self, config: dict[str, Any] | None = None, dry_run: bool = False) -> None:
        self.config = dict(config or load_json(CONFIG_PATH))
        if dry_run:
            self.config["dry_run"] = True
        self.airports = load_json(AIRPORTS_PATH)
        self.mission = load_json(resolve_flow_path(self.config))
        # In-memory unify; disk migrates on next UI save
        if "steps" not in self.mission and ("outbound" in self.mission or "inbound" in self.mission):
            self.mission["steps"] = mission_steps(self.mission)
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if STATE_PATH.is_file():
            try:
                return load_json(STATE_PATH)
            except json.JSONDecodeError:
                pass
        return {
            "index": 0,
            "last_step_id": None,
            "active_takeoff_mode": None,
            "pending_takeoff_offer": None,
            "takeoff_offer_rolled": False,
        }

    def save_state(self) -> None:
        # Drop legacy direction from state if present
        self.state.pop("direction", None)
        save_json(STATE_PATH, self.state)

    def _advance_past_skippable(self) -> None:
        """Skip lineup when rolling takeoff is active (no LUAW)."""
        steps = self.steps
        if not steps:
            return
        idx = int(self.state.get("index") or 0)
        if idx < 0:
            idx = 0
        while idx < len(steps) and atc_phrase.should_skip_takeoff_step(
            steps[idx], self.mission, self.state
        ):
            idx += 1
        self.state["index"] = idx

    def _maybe_roll_takeoff_offer(self, step: dict[str, Any] | None) -> None:
        """
        Once per sortie, when arriving at a tower takeoff step, maybe offer rolling.
        Sets pending_takeoff_offer=rolling so Fly can Accept/Deny (and Play asks).
        """
        if not step or self.state.get("takeoff_offer_rolled"):
            return
        if not atc_phrase.is_takeoff_related_template(step.get("template")):
            return
        # Already chose rolling (pilot request) — don't re-offer
        if str(self.state.get("active_takeoff_mode") or "").strip().casefold() == "rolling":
            self.state["takeoff_offer_rolled"] = True
            self.save_state()
            return
        if atc_phrase.pending_takeoff_offer(self.state):
            self.state["takeoff_offer_rolled"] = True
            self.save_state()
            return
        chance = atc_phrase.takeoff_offer_chance(self.config, self.mission)
        self.state["takeoff_offer_rolled"] = True
        if random.random() < chance:
            self.state["pending_takeoff_offer"] = "rolling"
        self.save_state()

    def prepare_takeoff_cursor(self) -> None:
        """Skip LUAW if needed + maybe arm a rolling offer for the current step."""
        self._advance_past_skippable()
        steps = self.steps
        idx = int(self.state.get("index") or 0)
        step = steps[idx] if steps and 0 <= idx < len(steps) else None
        self._maybe_roll_takeoff_offer(step)

    def reload(self) -> None:
        self.config = load_json(CONFIG_PATH)
        if self.config.get("dry_run"):
            pass
        self.airports = load_json(AIRPORTS_PATH)
        self.mission = load_json(resolve_flow_path(self.config))
        if "steps" not in self.mission and ("outbound" in self.mission or "inbound" in self.mission):
            self.mission["steps"] = mission_steps(self.mission)

    @property
    def steps(self) -> list[dict[str, Any]]:
        return enabled_steps(self.mission)

    def current_step(self) -> dict[str, Any] | None:
        steps = self.steps
        if not steps:
            return None
        self.prepare_takeoff_cursor()
        idx = int(self.state.get("index") or 0)
        if idx < 0:
            idx = 0
        if idx >= len(steps):
            idx = len(steps) - 1
        self.state["index"] = idx
        return steps[idx]

    def status(self) -> dict[str, Any]:
        steps = self.steps
        self.prepare_takeoff_cursor()
        idx = int(self.state.get("index") or 0)
        if idx < 0:
            idx = 0
        at_end = bool(steps) and idx >= len(steps)
        step = None if at_end or not steps else steps[min(idx, len(steps) - 1)]
        takeoff_mode = atc_phrase.resolve_active_takeoff_mode(self.mission, self.state)
        pending = atc_phrase.pending_takeoff_offer(self.state)
        return {
            "mission": self.mission.get("name"),
            "index": idx,
            "step_number": None if at_end or not steps else idx + 1,  # 1-based for UI
            "total": len(steps),
            "at_end": at_end,
            "step": step,
            "label": None if not step else f"{step.get('phase', '').upper()} · {step.get('label')}",
            "active_takeoff_mode": takeoff_mode,
            "pending_takeoff_offer": pending,
            "takeoff_mode_label": atc_phrase.takeoff_mode_label(takeoff_mode),
            "steps": [
                {
                    "index": i,
                    "number": i + 1,
                    "id": s.get("id"),
                    "label": f"{s.get('phase', '').upper()} · {s.get('label')}",
                    "channel": s.get("channel"),
                }
                for i, s in enumerate(steps)
            ],
        }

    def seek(self, index: int) -> dict[str, Any]:
        """Move cursor to step index (0-based) without transmitting."""
        steps = self.steps
        if not steps:
            raise RuntimeError("No enabled steps")
        idx = max(0, min(int(index), len(steps)))  # len(steps) == past end
        self.state["index"] = idx
        self.prepare_takeoff_cursor()
        # Arrows move the cursor without TX — keep the READ BACK card in sync
        # with the step ATC would have just said (the one before the cursor).
        self.sync_readback_for_cursor()
        self.save_state()
        st = self.status()
        st["seeked"] = True
        return st

    def seek_relative(self, delta: int) -> dict[str, Any]:
        idx = int(self.state.get("index") or 0) + int(delta)
        return self.seek(idx)

    def seek_number(self, number: int) -> dict[str, Any]:
        """Jump to 1-based step number without transmitting."""
        return self.seek(int(number) - 1)

    def airport(self) -> dict[str, Any]:
        key = self.mission.get("airport") or self.config.get("default_airport") or "nellis"
        if key not in self.airports:
            raise KeyError(f"Airport '{key}' not in airports.json")
        return self.airports[key]

    def play_step(self, step: dict[str, Any]) -> dict[str, Any]:
        airport = self.airport()
        opus, weather = atc_phrase.resolve_opus_and_metar(self.config, airport["icao"])
        if not opus:
            # Opus configured but lookup failed — allow TX with manual/fallback callsign.
            override = atc_phrase.callsign_override(self.config)
            label = override or "CALLSIGN"
            print(
                f"WARNING: Opus lookup failed; using {label} "
                f"(set Manual callsign or fix Opus signup)",
                file=sys.stderr,
            )
            opus = atc_phrase.synthetic_flight_context(label)
        callsign = opus.radio_callsign
        channel = step.get("channel") or step.get("phase") or "other"
        runway = atc_phrase.pick_departure_runway(
            airport,
            weather,
            opus,
            self.config,
            step=step,
            mission=self.mission,
            state=self.state,
            template=step.get("template"),
        )
        freq, mod, tx_name = atc_phrase.step_radio(airport, channel, step)
        mode = (step.get("mode") or "tts").lower()
        voice_name, _ = atc_phrase.voice_for_step(self.config, channel, step)

        detail = {
            "step_id": step.get("id"),
            "label": step.get("label"),
            "channel": channel,
            "freq": freq,
            "callsign": callsign,
            "runway": runway,
            "mode": mode,
            "voice": voice_name,
            "route": opus.fp_route_string,
            "altitude": opus.fp_altitude,
            "squawk": opus.mode3,
        }

        if mode == "file":
            file_path = step.get("file")
            if not file_path:
                raise RuntimeError(f"Step {step.get('id')} is mode=file but no file set")
            code = atc_phrase.transmit_file(self.config, airport, file_path, tx_name, freq, mod)
            detail["file"] = file_path
        else:
            template = step.get("template") or "radio_check"
            custom_text = step.get("text")
            text, tx_name, _freq_ignored, _mod_ignored = atc_phrase.build_flow_step_phrase(
                airport,
                channel,
                template,
                callsign,
                weather,
                runway,
                custom_text=str(custom_text) if custom_text else None,
                opus=opus,
                step=step,
                mission=self.mission,
                state=self.state,
                config=self.config,
            )
            # Prefer per-step freq/mod (e.g. unique "other" freqs) over airport defaults
            code = atc_phrase.transmit(
                self.config,
                airport,
                text,
                tx_name,
                freq,
                mod,
                channel=channel,
                voice_override=voice_name,
                step=step,
            )
            detail["text"] = text
            detail["tts_speed"] = atc_phrase.tts_speed_for_step(self.config, step=step)

        detail["exit_code"] = code
        self.state["last_step_id"] = step.get("id")
        self._record_readback_expectation(step, detail, airport, opus, weather, runway)
        self.save_state()
        return detail

    def _record_readback_expectation(
        self,
        step: dict[str, Any],
        detail: dict[str, Any],
        airport: dict[str, Any],
        opus: Any,
        weather: Any,
        runway: str,
    ) -> None:
        """
        After ATC transmits, remember what the pilot should read back.

        Clearance (and similar) open a short window where the acknowledge intent
        does not need an agency opener — the exchange is already live.
        """
        template = str(step.get("template") or "").strip()
        climb_ft = atc_phrase.resolve_shared_climb_ft(step=step, mission=self.mission)
        items = atc_phrase.build_readback_checklist(
            template,
            airport,
            opus,
            weather,
            runway,
            climb_ft=climb_ft,
        )
        self.state["last_tx_text"] = str(detail.get("text") or "")
        self.state["last_tx_template"] = template
        self.state["last_tx_channel"] = str(detail.get("channel") or "")
        self.state["last_tx_at"] = time.time()
        detail["climb_ft"] = climb_ft
        detail["readback_items"] = items

        if template in atc_phrase.READBACK_CONFIRM_TEMPLATES:
            self.state["awaiting_readback"] = False
            self.state["readback_items"] = []
            self.state["awaiting_confirm_template"] = ""
            return

        if template in atc_phrase.AWAITING_READBACK_TEMPLATES and items:
            self.state["readback_items"] = items
            self.state["awaiting_readback"] = True
            # Clearance → expect the readback-correct step; others just prompt.
            confirm = "clearance_readback" if template == "clearance" else ""
            self.state["awaiting_confirm_template"] = confirm
        elif not items:
            # Non-readback call — don't wipe a pending checklist until confirmed.
            pass

    def clear_readback(self) -> None:
        """Pilot has read it back: stop Fly asking and close the window."""
        self._clear_readback_state()
        self.save_state()

    def _clear_readback_state(self) -> None:
        self.state["awaiting_readback"] = False
        self.state["readback_items"] = []
        self.state["awaiting_confirm_template"] = ""

    def sync_readback_for_cursor(self) -> None:
        """
        Align the READ BACK card with the flow cursor (seek / arrows).

        Play advances past the step it just transmitted, so the step *before*
        the cursor is what ATC would have said. Rebuild that checklist — or
        clear the card when that step does not open a readback window — so
        browsing with the arrows never leaves a stale strip on screen.
        """
        steps = self.steps
        idx = int(self.state.get("index") or 0)
        prev = steps[idx - 1] if steps and 1 <= idx <= len(steps) else None
        if not prev:
            self._clear_readback_state()
            return

        template = str(prev.get("template") or "").strip()
        if template in atc_phrase.READBACK_CONFIRM_TEMPLATES:
            self._clear_readback_state()
            return
        if template not in atc_phrase.AWAITING_READBACK_TEMPLATES:
            self._clear_readback_state()
            return

        try:
            airport = self.airport()
        except Exception:  # noqa: BLE001
            self._clear_readback_state()
            return
        opus, weather = atc_phrase.resolve_opus_and_metar(self.config, airport["icao"])
        if not opus:
            opus = atc_phrase.synthetic_flight_context(
                atc_phrase.callsign_override(self.config) or "CALLSIGN"
            )
        runway = atc_phrase.pick_departure_runway(
            airport,
            weather,
            opus,
            self.config,
            step=prev,
            mission=self.mission,
            state=self.state,
            template=template,
        )
        climb_ft = atc_phrase.resolve_shared_climb_ft(step=prev, mission=self.mission)
        items = atc_phrase.build_readback_checklist(
            template,
            airport,
            opus,
            weather,
            runway,
            climb_ft=climb_ft,
        )
        if not items:
            self._clear_readback_state()
            return
        self.state["readback_items"] = items
        self.state["awaiting_readback"] = True
        self.state["awaiting_confirm_template"] = (
            "clearance_readback" if template == "clearance" else ""
        )
        self.state["last_tx_template"] = template
        self.state["last_tx_channel"] = str(
            prev.get("channel") or prev.get("phase") or ""
        )

    def next(self) -> dict[str, Any]:
        steps = self.steps
        if not steps:
            raise RuntimeError("No enabled steps")
        self.prepare_takeoff_cursor()
        idx = int(self.state.get("index") or 0)
        if idx < 0:
            idx = 0
        if idx >= len(steps):
            raise RuntimeError("End of flow — seek or reset")
        step = steps[idx]
        result = self.play_step(step)
        self.state["index"] = idx + 1
        self._advance_past_skippable()
        self.save_state()
        result["advanced_to_index"] = self.state["index"]
        result["active_takeoff_mode"] = atc_phrase.resolve_active_takeoff_mode(
            self.mission, self.state
        )
        result["pending_takeoff_offer"] = atc_phrase.pending_takeoff_offer(self.state)
        return result

    def back(self) -> dict[str, Any]:
        steps = self.steps
        if not steps:
            raise RuntimeError("No enabled steps")
        idx = int(self.state.get("index") or 0) - 1
        if idx < 0:
            idx = 0
        self.state["index"] = idx
        self.prepare_takeoff_cursor()
        self.save_state()
        step = self.current_step()
        if step is None:
            raise RuntimeError("No enabled steps")
        result = self.play_step(step)
        # After replaying, leave cursor on next after this step
        idx = int(self.state.get("index") or 0)
        self.state["index"] = min(idx + 1, len(steps))
        self._advance_past_skippable()
        self.save_state()
        return result

    def reset(self) -> dict[str, Any]:
        self.state["index"] = 0
        self.state["last_step_id"] = None
        self.state["active_takeoff_mode"] = None
        self.state["pending_takeoff_offer"] = None
        self.state["takeoff_offer_rolled"] = False
        self._clear_readback_state()
        if "active_takeoff_mode" in self.mission:
            self.mission["active_takeoff_mode"] = atc_phrase.DEFAULT_TAKEOFF_MODE
        self.save_state()
        return self.status()

    def flip(self) -> dict[str, Any]:
        """Legacy no-op — timeline is a single list now."""
        st = self.status()
        st["note"] = "Single timeline — flip is unused"
        return st

    def play_id(self, step_id: str) -> dict[str, Any]:
        steps = enabled_steps(self.mission)
        for i, step in enumerate(steps):
            if step.get("id") == step_id:
                self.state["index"] = i
                self.prepare_takeoff_cursor()
                self.save_state()
                # Cursor may have skipped past this id (rolling → skip lineup)
                cur = self.current_step()
                play = cur if cur is not None else step
                result = self.play_step(play)
                self.state["index"] = int(self.state.get("index") or 0) + 1
                self._advance_past_skippable()
                self.save_state()
                return result
        raise KeyError(f"Unknown step id: {step_id}")


def _html_ok(title: str, body: str) -> bytes:
    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:sans-serif;background:#111;color:#eee;padding:2rem}} a{{color:#8cf}}</style>
</head><body><h1>{title}</h1><p>{body}</p>
<p><a href="/status">status</a> · <a href="/next">next</a> · <a href="/back">back</a></p>
<script>setTimeout(function(){{window.close&&window.close()}},800)</script>
</body></html>"""
    return page.encode("utf-8")


def make_handler(engine: FlowEngine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            print("[http]", fmt % args)

        def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)
            try:
                engine.reload()
                if path == "/status":
                    data = json.dumps(engine.status(), indent=2).encode("utf-8")
                    self._send(200, data, "application/json")
                    return
                if path == "/next":
                    r = engine.next()
                    self._send(200, _html_ok("Next", r.get("label") or r.get("text") or "OK"))
                    return
                if path == "/back":
                    r = engine.back()
                    self._send(200, _html_ok("Back", r.get("label") or "OK"))
                    return
                if path == "/reset":
                    r = engine.reset()
                    self._send(200, _html_ok("Reset", "Index 0"))
                    return
                if path == "/flip":
                    r = engine.flip()
                    self._send(200, _html_ok("Flip", r.get("note") or "Single timeline"))
                    return
                if path == "/play":
                    sid = (qs.get("id") or [None])[0]
                    if not sid:
                        self._send(400, _html_ok("Error", "Missing id"))
                        return
                    r = engine.play_id(sid)
                    self._send(200, _html_ok("Play", r.get("label") or sid))
                    return
                if path == "/seek":
                    if qs.get("n"):
                        r = engine.seek_number(int(qs["n"][0]))
                    elif qs.get("index"):
                        r = engine.seek(int(qs["index"][0]))
                    elif qs.get("delta"):
                        r = engine.seek_relative(int(qs["delta"][0]))
                    else:
                        self._send(400, _html_ok("Error", "Use ?n=7 (1-based) or ?index=6 or ?delta=1"))
                        return
                    label = r.get("label") or ("(end)" if r.get("at_end") else "OK")
                    self._send(
                        200,
                        _html_ok("Seek", f"Now step {r.get('step_number')}/{r.get('total')}: {label}"),
                    )
                    return
                self._send(
                    200,
                    _html_ok(
                        "ATC Flow",
                        "Endpoints: /next /back /reset /flip /play?id=... /seek?n=7 /status",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self._send(500, _html_ok("Error", str(exc)))

    return Handler


def start_http_server(engine: FlowEngine, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Flow HTTP listening on http://127.0.0.1:{port}")
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Flight flow control")
    parser.add_argument(
        "action",
        choices=["next", "back", "reset", "flip", "status", "play", "serve"],
    )
    parser.add_argument("--id", default=None, help="Step id for play")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    engine = FlowEngine(dry_run=args.dry_run)
    if args.action == "serve":
        port = args.port or int(engine.config.get("flow_http_port") or 8765)
        start_http_server(engine, port)
        print("Serving forever — Ctrl+C to stop")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            return 0
        return 0

    try:
        if args.action == "next":
            print(json.dumps(engine.next(), indent=2))
        elif args.action == "back":
            print(json.dumps(engine.back(), indent=2))
        elif args.action == "reset":
            print(json.dumps(engine.reset(), indent=2))
        elif args.action == "flip":
            print(json.dumps(engine.flip(), indent=2))
        elif args.action == "status":
            print(json.dumps(engine.status(), indent=2))
        elif args.action == "play":
            if not args.id:
                print("--id required for play", file=sys.stderr)
                return 2
            print(json.dumps(engine.play_id(args.id), indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
