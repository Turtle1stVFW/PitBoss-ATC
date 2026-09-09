#!/usr/bin/env python3
"""
Mission flight flow engine: next/back/reset/flip/play + localhost HTTP control.
"""

from __future__ import annotations

import argparse
import copy
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
import srs_radio  # noqa: E402
import voice_intent  # noqa: E402

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


def load_app_config(path: Path | None = None) -> dict[str, Any]:
    """
    Load config.json, creating it from config.example.json on first run.

    Dedicated-server copies often have no config.json yet; the example is enough
    to open Setup and switch the role to Host.
    """
    cfg_path = path or CONFIG_PATH
    if cfg_path.is_file():
        data = load_json(cfg_path)
        if isinstance(data, dict):
            return data
        raise ValueError(f"{cfg_path.name} is not a JSON object")
    example = cfg_path.parent / "config.example.json"
    if example.is_file():
        data = load_json(example)
        if not isinstance(data, dict):
            raise ValueError("config.example.json is not a JSON object")
        try:
            save_json(cfg_path, data)
            print(f"Created {cfg_path.name} from config.example.json", file=sys.stderr)
        except OSError as exc:
            print(f"WARNING: could not write {cfg_path.name}: {exc}", file=sys.stderr)
        return data
    raise FileNotFoundError(
        f"Missing {cfg_path.name}. Copy config.example.json to config.json."
    )


_DEFAULT_FLOW_REL = "flows/nellis_default.json"


def resolve_flow_path(config: dict[str, Any], *, repair: bool = True) -> Path:
    """
    Active mission path from config.

    If the saved flow_file is missing (renamed / deleted) and repair=True, fall
    back to the Nellis base template and rewrite config['flow_file'] so the app
    still opens instead of crashing on launch.
    """
    rel = str(config.get("flow_file") or "").strip() or _DEFAULT_FLOW_REL
    path = Path(rel)
    if not path.is_absolute():
        path = HERE / path
    if path.is_file():
        return path
    fallback = HERE / _DEFAULT_FLOW_REL
    if (
        repair
        and fallback.is_file()
        and fallback.resolve() != path.resolve()
    ):
        print(
            f"WARNING: flow file missing ({path}); using {_DEFAULT_FLOW_REL}",
            file=sys.stderr,
        )
        if isinstance(config, dict):
            config["flow_file"] = _DEFAULT_FLOW_REL
            config["_flow_file_repaired"] = True
        return fallback
    return path


def is_base_flow_path(path: Path | str) -> bool:
    """Base templates are flows named *_default.json — not user-saved plans."""
    return Path(path).name.endswith("_default.json")


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
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        dry_run: bool = False,
        *,
        persist_state: bool = True,
        initial_state: dict[str, Any] | None = None,
        mission: dict[str, Any] | None = None,
        airports: dict[str, Any] | None = None,
    ) -> None:
        self.config = dict(config or load_app_config())
        if dry_run:
            self.config["dry_run"] = True
        srs_radio.apply_config(self.config)
        self.mutex = threading.RLock()
        self.persist_state = bool(persist_state)
        self.airports = dict(airports) if airports is not None else load_json(AIRPORTS_PATH)
        if mission is not None:
            self.mission = copy.deepcopy(mission)
        else:
            self.mission = load_json(resolve_flow_path(self.config))
        # In-memory unify; disk migrates on next UI save
        if "steps" not in self.mission and ("outbound" in self.mission or "inbound" in self.mission):
            self.mission["steps"] = mission_steps(self.mission)
        if initial_state is not None:
            self.state = dict(initial_state)
        elif self.persist_state:
            self.state = self._load_state()
        else:
            self.state = self._blank_state()
        # Client-injected radios for a host with no local DCS/SRS.
        self.remote_radios: srs_radio.RadioState | None = None
        # Host queues TX per channel; mutation still runs immediately.
        self.defer_tx = False
        self.pending_tx: dict[str, Any] | None = None
        self.sync_requested_runway_from_mission()
        try:
            import tanker as tanker_mod

            tanker_mod.reconcile_aar_overlay(self)
        except Exception:
            pass
        # Persist cleared/restored runway so a stale flow_state.json does not linger.
        try:
            self.save_state()
        except Exception:
            pass

    @staticmethod
    def _blank_state() -> dict[str, Any]:
        return {
            "index": 0,
            "last_step_id": None,
            "active_takeoff_mode": None,
            "pending_takeoff_offer": None,
            "takeoff_offer_rolled": False,
            "contact_phase": "field",
            "last_agency": "delivery",
        }

    def _load_state(self) -> dict[str, Any]:
        if STATE_PATH.is_file():
            try:
                return load_json(STATE_PATH)
            except json.JSONDecodeError:
                pass
        return self._blank_state()

    def set_remote_radios(
        self,
        freqs_mhz: list[float] | None,
        *,
        fresh: bool = True,
        selected_mhz: float | None = None,
    ) -> None:
        """Use a client's tuned radios instead of the host's local SRS/DCS."""
        if freqs_mhz is None:
            self.remote_radios = None
            return
        self.remote_radios = srs_radio.RadioState(
            source="client",
            freqs_mhz=[float(f) for f in freqs_mhz],
            selected_mhz=selected_mhz,
            unit="client",
            age_s=0.0,
            fresh=bool(fresh),
        )

    def emit_radio(
        self,
        *,
        text: str = "",
        file_path: str = "",
        tx_name: str,
        freq: float,
        mod: str,
        channel: str | None = None,
        voice_override: str | None = None,
        step: dict[str, Any] | None = None,
    ) -> int:
        """TX on SRS, or stash the payload when the host is queueing the channel."""
        payload: dict[str, Any] = {
            "text": text,
            "file_path": file_path,
            "tx_name": tx_name,
            "freq": freq,
            "mod": mod,
            "channel": str(channel or "other"),
            "voice": voice_override,
            "step": step,
            "config": self.config,
            "airport": self.airport(),
            "radio": self.remote_radios,
        }
        if self.defer_tx:
            self.pending_tx = payload
            return 0
        if file_path:
            return atc_phrase.transmit_file(
                self.config,
                self.airport(),
                file_path,
                tx_name,
                freq,
                mod,
                radio=self.remote_radios,
            )
        return atc_phrase.transmit(
            self.config,
            self.airport(),
            text,
            tx_name,
            freq,
            mod,
            channel=channel,
            voice_override=voice_override,
            step=step,
            radio=self.remote_radios,
        )

    def take_pending_tx(self) -> dict[str, Any] | None:
        job = self.pending_tx
        self.pending_tx = None
        return job

    def sync_requested_runway_from_mission(self) -> None:
        """
        Pilot runway stickiness:
        - Base / default flow → always clear (wind / ops default).
        - Saved mission → restore whatever that file has (or clear if none).
        Prevents flow_state.json from remembering a test request across new flights.
        """
        path = resolve_flow_path(self.config)
        if is_base_flow_path(path):
            atc_phrase.set_requested_runway(None, mission=self.mission, state=self.state)
            return
        req = atc_phrase.normalize_runway(
            (self.mission or {}).get("requested_runway")
        )
        atc_phrase.set_requested_runway(req, mission=self.mission, state=self.state)

    def save_state(self) -> None:
        # Drop legacy direction from state if present
        self.state.pop("direction", None)
        if not self.persist_state:
            return
        save_json(STATE_PATH, self.state)

    def _advance_past_skippable(self) -> None:
        """Skip lineup when rolling; honor any approach skip hooks."""
        steps = self.steps
        if not steps:
            return
        idx = int(self.state.get("index") or 0)
        if idx < 0:
            idx = 0
        while idx < len(steps) and self._step_is_skippable(steps[idx]):
            idx += 1
        self.state["index"] = idx
        self._park_pending_departure_handoff()

    def _step_is_skippable(self, step: dict[str, Any] | None) -> bool:
        if not step:
            return False
        tanker_skip = False
        try:
            import tanker as tanker_mod

            tanker_skip = tanker_mod.should_skip_tanker_step(step, self.state)
        except Exception:
            tanker_skip = False
        return bool(
            tanker_skip
            or atc_phrase.should_skip_takeoff_step(step, self.mission, self.state)
            or atc_phrase.should_skip_approach_step(step, self.mission, self.state)
            or atc_phrase.should_skip_control_step(step, self.state)
            or atc_phrase.should_skip_joshua_step(
                step,
                airport=self.airport(),
                state=self.state,
                config=self.config,
            )
            or atc_phrase.should_skip_cruise_climb_step(step, self.mission, self.state)
        )

    def _retreat_past_skippable(self, idx: int) -> int:
        """
        Walk backward to a step that is actually playable.

        Forward-only skipping (e.g. rolling skips LUAW) used to trap Back/seek
        when retreating onto a skipped step immediately jumped forward again.
        """
        steps = self.steps
        if not steps:
            return 0
        idx = min(max(0, int(idx)), len(steps) - 1)
        while idx > 0 and self._step_is_skippable(steps[idx]):
            idx -= 1
        return idx

    def _clear_landing_progress_if_before_clear_land(self, idx: int) -> None:
        """Reset per-ship landing tracking when cursor moves to/before clear_land."""
        steps = self.steps
        if not steps or not isinstance(self.state, dict):
            return
        clear_i = next(
            (
                i
                for i, s in enumerate(steps)
                if str(s.get("template") or "") == "clear_land"
            ),
            None,
        )
        if clear_i is None or int(idx) > clear_i:
            return
        for key in (
            "landing_cleared_seats",
            "landing_ships_total",
            "_landing_clear_built_seat",
        ):
            self.state.pop(key, None)

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

    def _departure_handoff_index(self) -> int | None:
        return next(
            (
                i
                for i, step in enumerate(self.steps)
                if str(step.get("template") or "") == "departure_handoff"
            ),
            None,
        )

    def _remember_manual_cursor(self, idx: int) -> None:
        """Arrows / seek past Blackjack handoff must not be yanked back."""
        handoff_i = self._departure_handoff_index()
        if handoff_i is None:
            return
        if idx > handoff_i:
            self.state["manual_cursor"] = True
        else:
            self.state.pop("manual_cursor", None)

    def _park_pending_departure_handoff(self) -> None:
        """
        After Departure radar / climb, stay on the Blackjack handoff.

        Skipping the unused climb-to-cruise step must not walk the cursor onto
        Control → Approach. A leftover recovery cursor plus a filed TORYE
        (outbound local hop) made Watch hand to Approach instead of Blackjack.
        An explicit seek past the handoff (Fly arrows) is left alone.
        """
        if self.state.get("manual_cursor"):
            return
        last_id = str(self.state.get("last_step_id") or "").strip()
        last_tmpl = str(self.state.get("last_tx_template") or "").strip().lower()
        played_tmpl = last_tmpl
        for step in self.steps:
            if str(step.get("id") or "").strip() == last_id:
                played_tmpl = str(step.get("template") or last_tmpl).strip().lower()
                break
        if played_tmpl not in ("radar_contact", "climb_cruise"):
            return
        handoff_i = self._departure_handoff_index()
        if handoff_i is None:
            return
        idx = int(self.state.get("index") or 0)
        if idx > handoff_i:
            self.state["index"] = handoff_i

    def prepare_takeoff_cursor(self) -> None:
        """Skip LUAW if needed + maybe arm a rolling offer for the current step."""
        self._advance_past_skippable()
        steps = self.steps
        idx = int(self.state.get("index") or 0)
        step = steps[idx] if steps and 0 <= idx < len(steps) else None
        self._maybe_roll_takeoff_offer(step)

    def reload(self) -> None:
        if not self.persist_state:
            # Multi-pilot sessions own their config/mission; do not clobber from disk.
            return
        self.config = load_app_config()
        if self.config.get("dry_run"):
            pass
        self.airports = load_json(AIRPORTS_PATH)
        self.mission = load_json(resolve_flow_path(self.config))
        if "steps" not in self.mission and ("outbound" in self.mission or "inbound" in self.mission):
            self.mission["steps"] = mission_steps(self.mission)
        self.sync_requested_runway_from_mission()
        try:
            self.save_state()
        except Exception:
            pass

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
        prev = int(self.state.get("index") or 0)
        idx = max(0, min(int(index), len(steps)))  # len(steps) == past end
        self.state["index"] = idx
        # Mark before prepare_takeoff_cursor so Watch-park cannot snap a
        # forward skip back onto departure_handoff.
        self._remember_manual_cursor(idx)
        if idx < len(steps):
            if idx < prev:
                # Moving earlier — do not bounce forward over skipped approach steps.
                self.state["index"] = self._retreat_past_skippable(idx)
                self._remember_manual_cursor(int(self.state.get("index") or 0))
            else:
                self.prepare_takeoff_cursor()
        self._clear_landing_progress_if_before_clear_land(
            int(self.state.get("index") or 0)
        )
        # Arrows move the cursor without TX — keep the READ BACK card in sync
        # with the step ATC would have just said (the one before the cursor).
        self.sync_readback_for_cursor()
        self.save_state()
        st = self.status()
        st["seeked"] = True
        return st

    def seek_relative(self, delta: int) -> dict[str, Any]:
        idx = int(self.state.get("index") or 0) + int(delta)
        if int(delta) < 0:
            # One Back click should land on the previous *playable* step.
            idx = self._retreat_past_skippable(idx)
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
        filed = atc_phrase.filed_altitude_feet(opus.fp_altitude)
        if filed is not None:
            self.state["filed_altitude_ft"] = filed
        channel = step.get("channel") or step.get("phase") or "other"
        tmpl = str(step.get("template") or "")
        if tmpl in ("control_check_in", "control_handoff"):
            live = str(self.state.get("control_channel") or "").strip().lower()
            if live in ("control_east", "control_west"):
                channel = live
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
        freq, mod, tx_name = atc_phrase.step_radio(
            airport, channel, step, state=self.state, config=self.config
        )
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
            # Stamp before emit so Watch cannot TX the same step during Play.
            self.state["last_step_id"] = step.get("id")
            code = self.emit_radio(
                file_path=str(file_path),
                tx_name=tx_name,
                freq=freq,
                mod=mod,
                channel=channel,
            )
            detail["file"] = file_path
            # File audio has no TTS string — keep the label so a spoken
            # readback of the instruction can be recognized as an echo.
            detail["text"] = str(step.get("text") or step.get("label") or "")
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
            # Stamp before emit so Watch cannot TX the same step during Play.
            self.state["last_step_id"] = step.get("id")
            # Prefer per-step freq/mod (e.g. unique "other" freqs) over airport defaults
            code = self.emit_radio(
                text=text,
                tx_name=tx_name,
                freq=freq,
                mod=mod,
                channel=channel,
                voice_override=voice_name,
                step=step,
            )
            detail["text"] = text
            detail["tts_speed"] = atc_phrase.tts_speed_for_step(self.config, step=step)
            if str(step.get("template") or "") == "clear_land":
                seat = atc_phrase.commit_landing_clearance(self.state)
                if seat is not None:
                    detail["landing_cleared_seat"] = seat
                    detail["landing_cleared_seats"] = list(
                        atc_phrase.landing_cleared_seats(self.state)
                    )
            if str(step.get("template") or "") == "bj_range_exit":
                self.state["range_exit_approved"] = True
            if str(step.get("template") or "") in ("approach_procedure", "approach_iaf"):
                self.state.pop("approach_clearance_need_fix", None)
                self.state["vectors_active"] = False

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
        template = atc_phrase.readback_template_for_step(
            step, mission=self.mission, state=self.state
        )
        climb_ft = atc_phrase.climb_ft_for_readback(
            template,
            opus=opus,
            mission=self.mission,
            state=self.state,
            step=step,
        )
        items = atc_phrase.build_readback_checklist(
            template,
            airport,
            opus,
            weather,
            runway,
            climb_ft=climb_ft,
            state=self.state,
        )
        self.state["last_tx_text"] = str(detail.get("text") or "")
        self.state["last_tx_template"] = template
        self.state["last_tx_channel"] = str(detail.get("channel") or "")
        self.state["last_tx_at"] = time.time()
        try:
            import agencies as agencies_mod

            agencies_mod.note_tx(
                self.state,
                str(detail.get("channel") or ""),
                template,
            )
        except Exception:
            pass
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

        Go-around is not a flow step (cursor seeks land / Approach). Keep that
        card until the pilot reads back the instruction.
        """
        if atc_phrase.go_around_readback_open(self.state):
            return
        steps = self.steps
        idx = int(self.state.get("index") or 0)
        prev_i = idx - 1
        while prev_i > 0 and self._step_is_skippable(steps[prev_i]):
            prev_i -= 1
        prev = steps[prev_i] if steps and 0 <= prev_i < len(steps) else None
        if not prev:
            self._clear_readback_state()
            return

        template = atc_phrase.readback_template_for_step(
            prev, mission=self.mission, state=self.state
        )
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
        climb_ft = atc_phrase.climb_ft_for_readback(
            template,
            opus=opus,
            mission=self.mission,
            state=self.state,
            step=prev,
        )
        items = atc_phrase.build_readback_checklist(
            template,
            airport,
            opus,
            weather,
            runway,
            climb_ft=climb_ft,
            state=self.state,
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

    def _freq_gate_or_raise(self, step: dict[str, Any] | None, *, bypass: bool = False) -> None:
        """Block external Advance/TX when the pilot is known to be off frequency."""
        if bypass:
            return
        srs_radio.apply_config(self.config)
        allowed, msg, _result = srs_radio.check_freq_gate(
            self.config,
            self.airport(),
            step,
            state=self.state,
            radio=self.remote_radios,
        )
        if not allowed:
            raise RuntimeError(msg)

    @staticmethod
    def _hold_cursor_after_play(step: dict[str, Any] | None) -> bool:
        """Stay on this step after Play when the step asks to hold (Bandsaw, or hold=true)."""
        return voice_intent.step_holds_after_play(step)

    def _hold_cursor_after_tx(self, step: dict[str, Any] | None) -> bool:
        """Hold after TX when more per-ship landing clearances remain."""
        if self._hold_cursor_after_play(step):
            return True
        tmpl = str((step or {}).get("template") or "")
        if tmpl != "clear_land":
            return False
        if bool(self.state.get("awaiting_on_the_go")):
            return True
        return atc_phrase.should_hold_for_landing_clearances(
            self.state, step=step, mission=self.mission
        )

    def _seek_template(self, template: str) -> bool:
        """Move cursor to the first enabled step with this template (no TX)."""
        want = str(template or "").strip()
        if not want:
            return False
        for i, step in enumerate(self.steps):
            if str(step.get("template") or "") == want:
                self.state["index"] = i
                return True
        return False

    def accept_option_full_stop(
        self,
        *,
        play: bool = True,
        bypass_freq_gate: bool = False,
        prefer_templates: tuple[str, ...] = ("exit_runway", "taxi_in"),
    ) -> dict[str, Any]:
        """
        After cleared-for-the-option, pilot lands full stop.

        Drops the on-the-go wait (no second landing clearance) and continues
        to Exit runway / Taxi in.
        """
        atc_phrase.commit_option_full_stop(
            state=self.state, mission=self.mission
        )
        sought = ""
        for tmpl in prefer_templates:
            if self._seek_template(tmpl):
                sought = tmpl
                break
        if not sought:
            # Past clear_land if present; otherwise leave cursor alone.
            for i, step in enumerate(self.steps):
                if str(step.get("template") or "") == "clear_land":
                    self.state["index"] = min(i + 1, len(self.steps))
                    self._advance_past_skippable()
                    break
        self.save_state()
        if not play or not sought:
            st = self.status()
            st["option_full_stop"] = True
            st["sought_template"] = sought or None
            return st
        step = self.current_step()
        if step is None:
            st = self.status()
            st["option_full_stop"] = True
            return st
        self._freq_gate_or_raise(step, bypass=bypass_freq_gate)
        result = self.play_step(step)
        if not self._hold_cursor_after_tx(step):
            self.state["index"] = int(self.state.get("index") or 0) + 1
            self._advance_past_skippable()
        self.save_state()
        result["option_full_stop"] = True
        result["sought_template"] = sought
        result["advanced_to_index"] = self.state.get("index")
        return result

    def execute_go_around(
        self,
        *,
        prefer: str | None = None,
        bypass_freq_gate: bool = False,
    ) -> dict[str, Any]:
        """
        Transmit go-around / missed approach and rewind the timeline.

        VFR closed / Flex / Duck → stay on Tower; seek land (already
        checked in). Watch fires on base / short final, not on the go-around.
        Instrument → published missed, seek Approach check-in, arm
        rearm_tower_outside_nm so contact-tower / land cannot auto-fire while
        still near the field.

        Repeating “going around” while the instruction readback is open
        acknowledges (closed traffic / Flex / missed) — it does not re-issue.
        """
        if atc_phrase.go_around_readback_open(self.state):
            ga = dict(self.state.get("go_around_plan") or {})
            self._clear_readback_state()
            self.save_state()
            return {
                "label": "Go around readback",
                "text": "",
                "channel": "tower",
                "exit_code": 0,
                "acknowledged": True,
                "go_around_plan": ga,
            }
        airport = self.airport()
        opus, weather = atc_phrase.resolve_opus_and_metar(self.config, airport["icao"])
        if not opus:
            opus = atc_phrase.synthetic_flight_context(
                atc_phrase.callsign_override(self.config) or "CALLSIGN"
            )
        callsign = opus.radio_callsign
        runway = atc_phrase.pick_departure_runway(
            airport,
            weather,
            opus,
            self.config,
            mission=self.mission,
            state=self.state,
            template="go_around",
        )
        text = atc_phrase.build_go_around(
            airport,
            callsign,
            runway,
            mission=self.mission,
            state=self.state,
            prefer=prefer,
        )
        ga = dict(self.state.get("go_around_plan") or {})
        channel = "tower"
        freq, mod, tx_name = atc_phrase.step_radio(airport, channel, None)
        step = {
            "id": "twr_go_around",
            "label": "Go around / missed",
            "channel": channel,
            "phase": "approach",
            "template": "go_around",
            "mode": "tts",
        }
        self._freq_gate_or_raise(step, bypass=bypass_freq_gate)
        voice_name, _ = atc_phrase.voice_for_step(self.config, channel, step)
        code = self.emit_radio(
            text=text,
            tx_name=tx_name,
            freq=freq,
            mod=mod,
            channel=channel,
            voice_override=voice_name,
            step=step,
        )
        kind = str(ga.get("kind") or "")
        seek = str(ga.get("seek_template") or "")
        # Re-arm only what the next cycle may auto-fire. Closed traffic is
        # still at the field — do not re-arm 12/6 NM straight-in / land.
        if kind == "instrument_missed":
            self.state["clear_position_fire_substrings"] = [
                "clear_land",
                "right_break",
                "cleared_approach",
                "approach_clearance",
                "app_procedure",
                "approach_iaf",
                "app_tower",
                "twr_clear",
                "twr_right",
                "exit_runway",
                "twr_exit",
            ]
        else:
            # VFR closed / Flex / Duck: already with Tower — new land on final.
            self.state["clear_position_fire_substrings"] = [
                "clear_land",
                "twr_clear",
                "exit_runway",
                "twr_exit",
            ]
        if kind == "instrument_missed":
            if not self._seek_template("approach_check_in"):
                self._seek_template("approach_procedure")
        else:
            # Already with Tower — skip check-in / initial; wait for land.
            if not seek or not self._seek_template(seek):
                self._seek_template("clear_land")
        # Replace the land readback with the go-around instruction card.
        self._record_readback_expectation(
            step,
            {"text": text, "channel": channel},
            airport,
            opus,
            weather,
            runway,
        )
        self.state["last_step_id"] = "twr_go_around"
        self.save_state()
        return {
            "label": step["label"],
            "text": text,
            "channel": channel,
            "freq": freq,
            "exit_code": code,
            "go_around_plan": ga,
            "advanced_to_index": self.state.get("index"),
        }

    def range_exit_ready(self) -> tuple[bool, str]:
        """
        True when Blackjack may hand to Nellis Control.

        Range exit is an early NATCF push — voice / Play always hand off.
        Watch still uses the leaving-blackjack trigger separately.
        """
        return True, ""

    def acknowledge_blackjack_continue(
        self, *, bypass_freq_gate: bool = False, seek_range_exit: bool = True
    ) -> dict[str, Any]:
        """Radar contact / remain this freq — not range exit yet."""
        airport = self.airport()
        opus, _wx = atc_phrase.resolve_opus_and_metar(self.config, airport["icao"])
        if not opus:
            opus = atc_phrase.synthetic_flight_context(
                atc_phrase.callsign_override(self.config) or "CALLSIGN"
            )
        callsign = opus.radio_callsign
        alpha_spoken = None
        try:
            fix = atc_phrase.resolve_alpha_bullseye(
                self.config, callsign=callsign, opus=opus
            )
            if fix and fix.get("spoken"):
                alpha_spoken = str(fix["spoken"])
        except Exception:
            alpha_spoken = None
        text = atc_phrase.build_blackjack_continue(
            callsign, alpha_bullseye=alpha_spoken
        )
        channel = "blackjack"
        step = {
            "id": "bj_continue",
            "label": "Blackjack continue",
            "channel": channel,
            "phase": "flight",
            "template": "bj_continue",
            "mode": "tts",
        }
        self._freq_gate_or_raise(step, bypass=bypass_freq_gate)
        freq, mod, tx_name = atc_phrase.step_radio(airport, channel, None)
        voice_name, _ = atc_phrase.voice_for_step(self.config, channel, step)
        code = self.emit_radio(
            text=text,
            tx_name=tx_name,
            freq=freq,
            mod=mod,
            channel=channel,
            voice_override=voice_name,
            step=step,
        )
        # Stay on bj_range_exit until inside the Approach gate — unless this
        # continue is a tanker return, which parks back on Blackjack C2.
        if seek_range_exit and not self._seek_template("bj_range_exit"):
            pass
        self.state["last_step_id"] = "bj_continue"
        self.state["last_tx_text"] = text
        self.state["last_tx_template"] = "bj_continue"
        self.state["last_tx_channel"] = channel
        self.state["last_tx_at"] = time.time()
        self.save_state()
        return {
            "label": "Blackjack continue",
            "text": text,
            "channel": channel,
            "freq": freq,
            "exit_code": code,
            "blackjack_continue": True,
            "advanced_to_index": self.state.get("index"),
        }

    def release_range_exit(self, *, bypass_freq_gate: bool = False) -> dict[str, Any]:
        """Range complete: hand to Nellis Control immediately."""
        airport = self.airport()
        opus, weather = atc_phrase.resolve_opus_and_metar(self.config, airport["icao"])
        if not opus:
            opus = atc_phrase.synthetic_flight_context(
                atc_phrase.callsign_override(self.config) or "CALLSIGN"
            )
        callsign = opus.radio_callsign
        plan = atc_phrase.assign_approach_plan(
            airport,
            weather,
            mission=self.mission,
            state=self.state,
            opus=opus,
            force=False,
        )
        text = atc_phrase.build_blackjack_range_exit(
            airport, callsign, plan=plan, include_handoff=True
        )
        channel = "blackjack"
        cur = self.current_step() or {}
        step = {
            "id": str(cur.get("id") or "bj_range_exit"),
            "label": str(cur.get("label") or "Blackjack range exit"),
            "channel": channel,
            "phase": "flight",
            "template": "bj_range_exit",
            "mode": "tts",
        }
        self._freq_gate_or_raise(step, bypass=bypass_freq_gate)
        freq, mod, tx_name = atc_phrase.step_radio(airport, channel, None)
        voice_name, _ = atc_phrase.voice_for_step(self.config, channel, step)
        code = self.emit_radio(
            text=text,
            tx_name=tx_name,
            freq=freq,
            mod=mod,
            channel=channel,
            voice_override=voice_name,
            step=step,
        )
        if not self._seek_template("bj_range_exit"):
            pass
        self.state["range_exit_approved"] = True
        self.state["last_step_id"] = step["id"]
        self.state["last_tx_text"] = text
        self.state["last_tx_template"] = "bj_range_exit"
        self.state["last_tx_channel"] = channel
        self.state["last_tx_at"] = time.time()
        self.save_state()
        return {
            "label": step["label"],
            "text": text,
            "channel": channel,
            "freq": freq,
            "exit_code": code,
            "range_exit_released": True,
            "advanced_to_index": self.state.get("index"),
        }

    def _range_exit_if_not_ready(
        self,
        step: dict[str, Any] | None,
        *,
        bypass_freq_gate: bool = False,
    ) -> dict[str, Any] | None:
        """Play/voice always hands to NATCF — Watch uses the leaving-range trigger."""
        del step, bypass_freq_gate
        return None

    def replay_last_tx(self, *, bypass_freq_gate: bool = False) -> dict[str, Any]:
        """
        Say again — retransmit the last ATC call.

        Do not re-run the step. play_id on bj_range_exit after a far-out
        release would become 'radar contact, remain this frequency'.
        """
        text = str(self.state.get("last_tx_text") or "").strip()
        if not text:
            last = str(self.state.get("last_step_id") or "").strip()
            if last:
                return self.play_id(last, bypass_freq_gate=bypass_freq_gate)
            raise RuntimeError("Nothing to repeat")
        airport = self.airport()
        channel = str(self.state.get("last_tx_channel") or "").strip().lower()
        if not channel:
            channel = str((self.current_step() or {}).get("channel") or "other")
            channel = channel.strip().lower() or "other"
        tmpl = str(self.state.get("last_tx_template") or "replay")
        step = {
            "id": str(self.state.get("last_step_id") or "replay"),
            "label": "Say again",
            "channel": channel,
            "phase": str((self.current_step() or {}).get("phase") or channel),
            "template": tmpl,
            "mode": "tts",
        }
        self._freq_gate_or_raise(step, bypass=bypass_freq_gate)
        freq, mod, tx_name = atc_phrase.step_radio(airport, channel, None)
        voice_name, _ = atc_phrase.voice_for_step(self.config, channel, step)
        code = self.emit_radio(
            text=text,
            tx_name=tx_name,
            freq=freq,
            mod=mod,
            channel=channel,
            voice_override=voice_name,
            step=step,
        )
        self.state["last_tx_at"] = time.time()
        self.save_state()
        return {
            "label": "Say again",
            "text": text,
            "channel": channel,
            "freq": freq,
            "exit_code": code,
            "replayed": True,
        }

    def answer_rolling_offer(
        self, *, accept: bool, bypass_freq_gate: bool = False
    ) -> dict[str, Any]:
        """
        HOTAS / Next / Back answer to 'will you accept rolling?'.

        Accept → cleared takeoff. Decline → line up and wait.
        """
        import voice_engine  # local — avoid import cycle at module load

        step = self.current_step()
        self._freq_gate_or_raise(step, bypass=bypass_freq_gate)
        intent = "accept_rolling" if accept else "deny_rolling"
        atc_phrase.apply_pilot_request(
            intent, mission=self.mission, state=self.state
        )
        self.save_state()
        if self.state.get("awaiting_readback"):
            self._clear_readback_state()
        self.prepare_takeoff_cursor()
        self.save_state()
        played = voice_engine.play_rolling_offer_reply(self, intent)
        if not isinstance(played, dict) or played.get("action") == "none":
            detail = (
                played.get("detail") if isinstance(played, dict) else None
            ) or "no takeoff step"
            raise RuntimeError(str(detail))
        result = played.get("detail")
        if not isinstance(result, dict):
            result = dict(played)
        result["rolling_offer_reply"] = "accept" if accept else "deny"
        result["pending_takeoff_offer"] = atc_phrase.pending_takeoff_offer(
            self.state
        )
        return result

    def next(self, *, bypass_freq_gate: bool = False) -> dict[str, Any]:
        # After Tower asked 'will you accept rolling?', Next accepts — do not
        # treat the ANSWER card as a normal readback dismiss.
        if atc_phrase.rolling_offer_awaiting_reply(self.state):
            return self.answer_rolling_offer(
                accept=True, bypass_freq_gate=bypass_freq_gate
            )
        # READ BACK card is the active step — close it, do not TX the next call.
        if self.state.get("awaiting_readback") and self.state.get("readback_items"):
            self.clear_readback()
            return {
                "acknowledged": True,
                "label": "Readback noted",
                "readback_cleared": True,
            }
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
        # Cleared for the option + Next → take the full stop (exit/taxi), not
        # a second "cleared for the option" transmission.
        if (
            atc_phrase.awaiting_option_on_the_go(self.state)
            and str(step.get("template") or "") == "clear_land"
        ):
            return self.accept_option_full_stop(bypass_freq_gate=bypass_freq_gate)
        # Still outside 40 NM — ARCOE release only; Approach waits.
        held = self._range_exit_if_not_ready(step, bypass_freq_gate=bypass_freq_gate)
        if held is not None:
            return held
        self._freq_gate_or_raise(step, bypass=bypass_freq_gate)
        result = self.play_step(step)
        if not self._hold_cursor_after_tx(step):
            self.state["index"] = idx + 1
            self._advance_past_skippable()
        self.save_state()
        result["advanced_to_index"] = self.state["index"]
        result["active_takeoff_mode"] = atc_phrase.resolve_active_takeoff_mode(
            self.mission, self.state
        )
        result["pending_takeoff_offer"] = atc_phrase.pending_takeoff_offer(self.state)
        return result

    def back(self, *, bypass_freq_gate: bool = False) -> dict[str, Any]:
        """
        Move to the previous playable step, transmit it, leave cursor after it.

        Does not call forward-only prepare_takeoff_cursor before TX (that was
        bouncing the cursor when retreating onto a skippable step). After TX,
        advances one step without skipping — Next will skip forward as usual.
        """
        if atc_phrase.rolling_offer_awaiting_reply(self.state):
            return self.answer_rolling_offer(
                accept=False, bypass_freq_gate=bypass_freq_gate
            )
        steps = self.steps
        if not steps:
            raise RuntimeError("No enabled steps")
        target = self._retreat_past_skippable(int(self.state.get("index") or 0) - 1)
        self.state["index"] = target
        self._clear_landing_progress_if_before_clear_land(target)
        self.save_state()
        step = steps[target]
        self._freq_gate_or_raise(step, bypass=bypass_freq_gate)
        # Offer rolling only when backing onto a takeoff step
        self._maybe_roll_takeoff_offer(step)
        result = self.play_step(step)
        if self._hold_cursor_after_tx(step):
            self.state["index"] = target
        else:
            # Stay one past the replayed step; do not skip forward (avoids
            # bouncing back onto the step we just left).
            self.state["index"] = min(target + 1, len(steps))
        self.save_state()
        result["advanced_to_index"] = self.state["index"]
        return result

    def reset(self) -> dict[str, Any]:
        """Seek to the start of the flow and clear sticky sortie cache."""
        atc_phrase.invalidate_flight_lookups()
        atc_phrase.clear_flight_session_cache(
            mission=self.mission,
            state=self.state,
            config=self.config,
            reset_runway=False,
            invalidate_lookups=False,
        )
        # Fresh METAR/winds so runway returns to wind default.
        try:
            airport = self.airport()
            opus, weather = atc_phrase.resolve_opus_and_metar(
                self.config, airport["icao"]
            )
            atc_phrase.reset_runway_to_winds(
                airport,
                weather,
                mission=self.mission,
                state=self.state,
                opus=opus,
                config=self.config,
            )
        except Exception:
            atc_phrase.set_requested_runway(
                None, mission=self.mission, state=self.state
            )
        self.state["index"] = 0
        self.state["last_step_id"] = None
        self.state.pop("manual_cursor", None)
        try:
            import agencies as agencies_mod

            agencies_mod.reset_contact(self.state)
        except Exception:
            self.state["contact_phase"] = "field"
            self.state["last_agency"] = "delivery"
        self._clear_readback_state()
        try:
            import tanker as tanker_mod

            tanker_mod.clear_aar_state(self.state)
        except Exception:
            pass
        if "active_takeoff_mode" in self.mission:
            self.mission["active_takeoff_mode"] = atc_phrase.DEFAULT_TAKEOFF_MODE
        self.save_state()
        return self.status()

    def clear_flight_cache(self) -> dict[str, Any]:
        """
        Plan/Fly helper: drop unrestricted climb, approach, runway, and lookup
        caches, then seek to the start of the timeline.
        """
        atc_phrase.invalidate_flight_lookups()
        airport = self.airport()
        try:
            opus, weather = atc_phrase.resolve_opus_and_metar(
                self.config, airport["icao"]
            )
        except Exception:
            opus, weather = None, atc_phrase.Weather(None, None, None, "")
        detail = atc_phrase.clear_flight_session_cache(
            mission=self.mission,
            state=self.state,
            config=self.config,
            airport=airport,
            weather=weather,
            opus=opus,
            reset_runway=True,
            invalidate_lookups=False,
        )
        self.state["index"] = 0
        self.state["last_step_id"] = None
        self.state.pop("manual_cursor", None)
        try:
            import agencies as agencies_mod

            agencies_mod.reset_contact(self.state)
        except Exception:
            self.state["contact_phase"] = "field"
            self.state["last_agency"] = "delivery"
        self._clear_readback_state()
        try:
            import tanker as tanker_mod

            tanker_mod.clear_aar_state(self.state)
        except Exception:
            pass
        if "active_takeoff_mode" in self.mission:
            self.mission["active_takeoff_mode"] = atc_phrase.DEFAULT_TAKEOFF_MODE
        self.save_state()
        status = self.status()
        status["cache_cleared"] = True
        status["runway"] = detail.get("runway") or status.get("runway")
        return status

    def flip(self) -> dict[str, Any]:
        """Legacy no-op — timeline is a single list now."""
        st = self.status()
        st["note"] = "Single timeline — flip is unused"
        return st

    def play_id(
        self,
        step_id: str,
        *,
        bypass_freq_gate: bool = False,
        force_advance: bool = False,
    ) -> dict[str, Any]:
        steps = enabled_steps(self.mission)
        for i, step in enumerate(steps):
            if step.get("id") == step_id:
                self.state["index"] = i
                self.prepare_takeoff_cursor()
                self.save_state()
                # Cursor may have skipped past this id (rolling → skip lineup)
                cur = self.current_step()
                play = cur if cur is not None else step
                held = self._range_exit_if_not_ready(
                    play, bypass_freq_gate=bypass_freq_gate
                )
                if held is not None:
                    return held
                self._freq_gate_or_raise(play, bypass=bypass_freq_gate)
                result = self.play_step(play)
                if force_advance or not self._hold_cursor_after_tx(play):
                    self.state["index"] = int(self.state.get("index") or 0) + 1
                    self._advance_past_skippable()
                self.save_state()
                return result
        raise KeyError(f"Unknown step id: {step_id}")

    def play_template(
        self, template: str, *, bypass_freq_gate: bool = False
    ) -> dict[str, Any]:
        """Play the nearest enabled step with this phrase template."""
        steps = enabled_steps(self.mission)
        start = int(self.state.get("index") or 0)
        pool = list(steps[start:]) + list(steps)
        for step in pool:
            if str(step.get("template") or "") == str(template):
                return self.play_id(
                    str(step.get("id")), bypass_freq_gate=bypass_freq_gate
                )
        raise KeyError(f"No enabled step for template: {template}")


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
                if path == "/seek_next":
                    r = engine.seek_relative(1)
                    label = r.get("label") or ("(end)" if r.get("at_end") else "OK")
                    self._send(
                        200,
                        _html_ok("Step forward", f"Now step {r.get('step_number')}/{r.get('total')}: {label}"),
                    )
                    return
                if path == "/seek_prev":
                    r = engine.seek_relative(-1)
                    label = r.get("label") or ("(end)" if r.get("at_end") else "OK")
                    self._send(
                        200,
                        _html_ok("Step back", f"Now step {r.get('step_number')}/{r.get('total')}: {label}"),
                    )
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
                        "Endpoints: /next /back /reset /flip /play?id=... /seek?n=7 /seek_next /seek_prev /status",
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
