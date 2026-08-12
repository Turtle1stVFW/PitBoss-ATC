"""
Voice control: hold the SRS PTT, talk, and ATC answers.

Pipeline — PTT press marks the mic ring buffer, PTT release hands the captured
audio to faster-whisper on a worker thread, the transcript goes through the
keyword grammar in `voice_intent`, and a confident match is executed against
the flow engine.

Whisper runs on CPU int8 on purpose: base.en costs ~350 ms for a typical radio
call and leaves the GPU entirely to DCS. The model is loaded once at startup so
no transmission pays the load cost.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
import warnings
from collections.abc import Callable
from typing import Any

try:
    import numpy as np
except ImportError:  # voice control is optional — the rest of the app still runs
    np = None


def _quiet_huggingface_hub() -> None:
    """
    End users do not need an HF account or Windows Developer Mode.

    First model download otherwise spams the console about missing HF_TOKEN
    and symlink cache limits — both safe to ignore for our offline CPU path.
    """
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    # Hub occasionally logs auth/rate-limit tips at WARNING.
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    logging.getLogger("huggingface_hub.file_download").setLevel(logging.ERROR)

import atc_phrase
import hotkeys
import joystick
import mic_capture
import srs_radio
import voice_actions
import voice_intent

DEFAULT_MODEL = "base.en"
MODEL_CHOICES = ("tiny.en", "base.en", "small.en", "distil-small.en")
MIN_UTTERANCE_S = 0.4
MAX_UTTERANCE_S = 20.0
DEFAULT_MIN_CONFIDENCE = 0.6

# Steers Whisper towards callsigns and phraseology instead of plain English.
_BASE_PROMPT = (
    "Radio call to air traffic control. Nellis ground, tower, approach, departure, "
    "clearance delivery, Blackjack, Bandsaw, Magic. Request taxi, ready for departure, "
    "cleared for takeoff, request runway two one left, say winds, say altimeter, "
    "request picture, bogey dope, declare, alpha check, bullseye, angels, rolling departure, "
    "line up and wait, gear down full stop, tactical overhead, say again. "
    "Bandsaw, Bandsaw, band saw."
)


def build_prompt(airport: dict[str, Any] | None, callsign: str = "") -> str:
    extra: list[str] = []
    if airport:
        name = str(airport.get("name") or "").strip()
        if name:
            extra.append(name)
        runways = airport.get("runways") or []
        if runways:
            extra.append("runways " + ", ".join(str(r) for r in runways))
    if callsign:
        extra.append(str(callsign))
    return _BASE_PROMPT + (" " + ". ".join(extra) + "." if extra else "")


class Transcriber:
    """Lazily loaded faster-whisper model, warmed on a background thread."""

    def __init__(self, model_size: str = DEFAULT_MODEL, device: str = "cpu", compute_type: str = "int8") -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model: Any = None
        self._lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self.error = ""
        self.ready = threading.Event()

    def load(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                self.error = f"faster-whisper is not installed ({exc})"
                return False
            try:
                _quiet_huggingface_hub()
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message=r".*symlinks.*",
                        category=UserWarning,
                    )
                    warnings.filterwarnings(
                        "ignore",
                        message=r".*HF_TOKEN.*",
                        category=UserWarning,
                    )
                    self._model = WhisperModel(
                        self.model_size,
                        device=self.device,
                        compute_type=self.compute_type,
                    )
            except Exception as exc:  # noqa: BLE001 — model download/CUDA/etc
                self.error = f"{type(exc).__name__}: {exc}"
                return False
        self.error = ""
        self.ready.set()
        return True

    def warm(self) -> None:
        """Load and run one tiny inference so the first real call is fast."""
        if not self.load():
            return
        try:
            self._model.transcribe(
                np.zeros(mic_capture.SAMPLE_RATE // 2, dtype=np.float32),
                language="en",
                beam_size=1,
            )
        except Exception:  # noqa: BLE001
            pass

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> str:
        if self._model is None and not self.load():
            return ""
        # A CTranslate2 model is not safe to drive from two threads at once,
        # and back-to-back PTT taps each spawn their own worker.
        with self._infer_lock:
            segments, _info = self._model.transcribe(
                audio,
                language="en",
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
                initial_prompt=prompt or None,
            )
            return " ".join(segment.text for segment in segments).strip()

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self.ready.clear()


class VoiceController:
    """
    Owns the mic, the PTT watcher and the model.

    `on_intent(match)` fires on a worker thread only for calls that pass the
    gate; `on_status(text)` and `on_transcript(evaluation)` are for UI feedback,
    the latter including transmissions that were deliberately ignored.
    """

    def __init__(
        self,
        *,
        on_intent: Callable[[voice_intent.Match], None],
        on_status: Callable[[str], None] | None = None,
        on_transcript: Callable[[voice_intent.Evaluation], None] | None = None,
        context: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.on_intent = on_intent
        self.on_status = on_status or (lambda _t: None)
        self.on_transcript = on_transcript or (lambda _e: None)
        self.context = context or dict

        self.enabled = False
        self.min_confidence = DEFAULT_MIN_CONFIDENCE
        self.require_address = True
        self._recorder: mic_capture.MicRecorder | None = None
        self._watcher = joystick.JoystickWatcher()
        self._keys = hotkeys.KeyWatcher()
        self._transcriber: Transcriber | None = None
        self._ptt_bindings: list[dict[str, Any]] = []
        self._ptt_key = ""
        self._held = 0
        self._lock = threading.Lock()
        self.last_error = ""

    # ---- lifecycle -------------------------------------------------------

    def start(self, config: dict[str, Any]) -> list[str]:
        """(Re)configure and start listening. Returns warnings."""
        self.stop()
        warnings: list[str] = []
        if not config.get("voice_enabled"):
            return warnings
        if np is None:
            return ["Voice control needs numpy — run: pip install faster-whisper numpy"]
        if not mic_capture.supported():
            return ["Voice control requires Windows."]

        self.min_confidence = float(
            config.get("voice_min_confidence") or DEFAULT_MIN_CONFIDENCE
        )
        self.require_address = bool(config.get("voice_require_address", True))

        device_index, device_name, note = resolve_mic(config)
        if note:
            warnings.append(note)
        recorder = mic_capture.MicRecorder(device_index=device_index)
        if not recorder.start():
            return warnings + [f"Microphone failed to open: {recorder.last_error or 'unknown error'}"]
        self._recorder = recorder
        self.on_status(f"Mic: {device_name}")

        self._ptt_key = hotkeys.normalize_hotkey(config.get("voice_ptt_key"))
        bindings, ptt_note = resolve_ptt(config, have_key=bool(self._ptt_key))
        if ptt_note:
            warnings.append(ptt_note)
        self._ptt_bindings = bindings
        for i, binding in enumerate(bindings):
            warning = self._watcher.set_binding(
                f"ptt{i}", binding, on_press=self._on_ptt_down, on_release=self._on_ptt_up
            )
            if warning:
                warnings.append(f"PTT: {warning}")
        if bindings:
            self._watcher.start()

        # A key works as well as a button — polled, so it gives a release edge
        # and survives DCS having focus.
        if self._ptt_key:
            warning = self._keys.set_binding(
                "ptt", self._ptt_key, on_press=self._on_ptt_down, on_release=self._on_ptt_up
            )
            if warning:
                warnings.append(f"PTT key: {warning}")
                self._ptt_key = ""
            else:
                self._keys.start()

        if not bindings and not self._ptt_key:
            warnings.append("No PTT bound — voice control will not trigger.")

        self._transcriber = Transcriber(
            model_size=str(config.get("voice_model") or DEFAULT_MODEL),
            device=str(config.get("voice_device") or "cpu"),
            compute_type=str(config.get("voice_compute") or "int8"),
        )
        threading.Thread(target=self._warm, name="atc-voice-warm", daemon=True).start()

        self.enabled = True
        return warnings

    def _warm(self) -> None:
        transcriber = self._transcriber
        if not transcriber:
            return
        self.on_status(f"Loading {transcriber.model_size}…")
        started = time.perf_counter()
        transcriber.warm()
        if transcriber.error:
            self.last_error = transcriber.error
            self.on_status(f"Model failed: {transcriber.error}")
        else:
            self.on_status(
                f"Listening — {transcriber.model_size} ready "
                f"({time.perf_counter() - started:.1f}s)"
            )

    def stop(self) -> None:
        self.enabled = False
        self._watcher.clear_bindings()
        self._watcher.stop()
        self._keys.clear_bindings()
        self._keys.stop()
        if self._recorder:
            self._recorder.stop()
            self._recorder = None
        if self._transcriber:
            self._transcriber.unload()
            self._transcriber = None
        self._held = 0

    @property
    def running(self) -> bool:
        return self.enabled and bool(self._recorder and self._recorder.running)

    def level(self) -> float:
        return self._recorder.level() if self._recorder else 0.0

    # ---- PTT edges -------------------------------------------------------

    def _on_ptt_down(self) -> None:
        with self._lock:
            self._held += 1
            first = self._held == 1
        if first and self._recorder:
            self._recorder.begin_utterance()
            self.on_status("Listening…")

    def _on_ptt_up(self) -> None:
        with self._lock:
            self._held = max(0, self._held - 1)
            last = self._held == 0
        if not last or not self._recorder:
            return
        audio = self._recorder.end_utterance()
        threading.Thread(
            target=self._process, args=(audio,), name="atc-voice-stt", daemon=True
        ).start()

    def _process(self, audio: np.ndarray) -> None:
        seconds = audio.size / float(mic_capture.SAMPLE_RATE)
        if seconds < MIN_UTTERANCE_S:
            self.on_status("Listening (too short)")
            return
        if seconds > MAX_UTTERANCE_S:
            audio = audio[-int(MAX_UTTERANCE_S * mic_capture.SAMPLE_RATE):]

        transcriber = self._transcriber
        if not transcriber:
            return
        context = self.context() or {}
        started = time.perf_counter()
        try:
            text = transcriber.transcribe(
                audio, prompt=build_prompt(context.get("airport"), context.get("callsign", ""))
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.on_status(f"Transcribe failed: {exc}")
            return
        elapsed = (time.perf_counter() - started) * 1000

        if not text:
            self.on_status(f"Heard nothing ({seconds:.1f}s)")
            self.on_transcript(voice_intent.Evaluation(reason="nothing heard"))
            return

        evaluation = voice_intent.evaluate(
            text,
            channel=str(context.get("channel") or ""),
            phase=str(context.get("phase") or ""),
            expected=str(context.get("expected") or ""),
            callsign=str(context.get("callsign") or ""),
            runways=context.get("runways"),
            min_confidence=self.min_confidence,
            require_address=self.require_address,
            awaiting_readback=bool(context.get("awaiting_readback")),
            readback_items=context.get("readback_items")
            if isinstance(context.get("readback_items"), list)
            else None,
            steps=context.get("steps") if isinstance(context.get("steps"), list) else None,
        )
        self.on_status(f"{elapsed:.0f}ms · {text}")
        self.on_transcript(evaluation)
        if evaluation.match:
            self.on_intent(evaluation.match)


# ---- configuration helpers ----------------------------------------------


def resolve_mic(config: dict[str, Any]) -> tuple[int, str, str]:
    """(waveIn index, display name, warning). -1 means the Windows default."""
    configured = config.get("voice_mic_device")
    devices = {int(d["index"]): str(d["name"]) for d in mic_capture.list_input_devices()}
    if isinstance(configured, int) and configured >= 0:
        if configured in devices:
            return configured, devices[configured], ""
        return -1, devices.get(-1, "(default)"), (
            f"Configured microphone #{configured} is gone; using the Windows default."
        )

    index, name = mic_capture.resolve_srs_device(str(config.get("srs_audio_input_id") or ""))
    if index is None:
        index, name = srs_mic_from_client_config()
    if index is not None:
        return index, name, ""
    return -1, devices.get(-1, "(default)"), ""


def srs_mic_from_client_config() -> tuple[int | None, str]:
    """Read AudioInputDeviceId straight out of the SRS client's global.cfg."""
    for client_dir in joystick.SRS_CLIENT_DIRS:
        path = client_dir / "global.cfg"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.strip().startswith("AudioInputDeviceId="):
                device_id = line.split("=", 1)[1].strip()
                index, name = mic_capture.resolve_srs_device(device_id)
                if index is not None:
                    return index, name
    return None, ""


def resolve_ptt(
    config: dict[str, Any], *, have_key: bool = False
) -> tuple[list[dict[str, Any]], str]:
    """
    Configured PTT buttons, falling back to whatever SRS transmits on.

    `have_key` suppresses the "nothing bound" nudge when a keyboard PTT is set,
    since a key alone is a perfectly good way to run this.
    """
    raw = config.get("voice_ptt")
    bindings: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for entry in raw:
            binding = joystick.normalize_binding(entry)
            if binding:
                bindings.append(binding)
    if bindings:
        return bindings, ""
    if have_key:
        return [], ""

    discovered = joystick.discover_srs_ptt()
    if discovered:
        return [joystick.normalize_binding(b) for b in discovered if joystick.normalize_binding(b)], (
            "Using SRS PTT buttons: "
            + ", ".join(joystick.describe_binding(b) for b in discovered)
        )
    return [], "No SRS PTT binding found — set a button or a key on the Controls tab."


# ---- intent execution ----------------------------------------------------


def execute_intent(
    match: voice_intent.Match, engine: Any
) -> dict[str, Any]:
    """
    Carry out a matched intent against a FlowEngine.

    Returns {action, detail, text} describing what happened. Kept free of UI so
    it can be exercised from tests and the CLI.
    """
    config = engine.config
    airport = engine.airport()
    opus, weather = atc_phrase.resolve_opus_and_metar(config, airport["icao"])
    if not opus:
        opus = atc_phrase.synthetic_flight_context(
            atc_phrase.callsign_override(config) or "CALLSIGN"
        )
    callsign = opus.radio_callsign
    intent = match.intent

    if intent == "say_again":
        last = engine.state.get("last_step_id")
        if not last:
            return {"action": "none", "detail": "nothing to repeat"}
        return {"action": "replay", "detail": engine.play_id(last)}

    if intent in (
        "request_winds",
        "request_altimeter",
        "request_picture",
        "request_bogey_dope",
        "request_declare",
    ):
        return _speak_reply(intent, engine, airport, callsign, weather, opus, match)

    if intent == "request_bandsaw":
        text = atc_phrase.build_contact_bandsaw(airport, callsign)
        return _transmit(engine, airport, text, "blackjack")

    if intent in (
        "request_approach",
        "request_hold",
        "cancel_hold",
        "request_vectors",
    ):
        return _handle_approach_action(
            intent, engine, airport, callsign, weather, match, opus=opus
        )

    if intent == "request_runway":
        runway = match.slots.get("runway")
        if not runway:
            return {"action": "none", "detail": "no runway heard"}
        current = (
            atc_phrase.requested_runway(mission=engine.mission, state=engine.state)
            or str((engine.state or {}).get("runway") or "").strip()
        )
        same = voice_intent._normalize_runway_token(runway) == voice_intent._normalize_runway_token(
            current
        )
        # Saying the assigned runway again during readback is a readback, not a change.
        if same and engine.state.get("awaiting_readback"):
            return _acknowledge(engine, match)
        atc_phrase.apply_pilot_request(
            f"request_runway_{runway}", mission=engine.mission, state=engine.state
        )
        engine.save_state()
        # Same call with taxi, or a runway change during/after taxi instructions
        # → re-issue taxi on the new runway, then a fresh readback.
        if _should_reissue_taxi(engine, match):
            played = _play_template(engine, "taxi")
            played["runway"] = runway
            played["detail"] = f"runway {runway}; {played.get('detail')}"
            return played
        return _ack("request_runway", engine, airport, callsign, runway=runway, match=match)

    if intent in ("accept_rolling", "deny_rolling", "request_lineup"):
        atc_phrase.apply_pilot_request(intent, mission=engine.mission, state=engine.state)
        engine.save_state()
        return _ack(intent, engine, airport, callsign, match=match)

    if intent == "request_unrestricted_climb":
        return _request_unrestricted_climb(engine, airport, callsign, match)

    if intent == "request_alpha_check":
        fix = atc_phrase.resolve_alpha_bullseye(config, callsign=callsign, opus=opus)
        text = atc_phrase.build_standalone_alpha_check(
            callsign, (fix or {}).get("spoken")
        )
        channel = _resolve_tx_channel(engine, airport, match)
        if channel not in ("blackjack", "bandsaw", "ops", "other"):
            channel = "blackjack"
        return _transmit(engine, airport, text, channel)

    if intent == "acknowledge_readback":
        last_tmpl = str(engine.state.get("last_tx_template") or "")
        result = _acknowledge(engine, match)
        # "Runway 21R, at EOR" — close the readback and hand off in one call.
        if last_tmpl == "taxi" and _eor_also_heard(match):
            played = _play_template(engine, "monitor_tower")
            played["detail"] = f"readback noted; {played.get('detail')}"
            return played
        return result

    if intent == "correct_climb_readback":
        climb_ft = match.slots.get("climb_ft")
        try:
            climb_n = int(climb_ft) if climb_ft is not None else None
        except (TypeError, ValueError):
            climb_n = None
        if climb_n is None:
            climb_n = atc_phrase.resolve_shared_climb_ft(
                step=None,
                mission=getattr(engine, "mission", None),
                state=getattr(engine, "state", None),
            )
        text = atc_phrase.build_climb_correction(callsign, climb_n)
        channel = _resolve_tx_channel(engine, airport, match) or "departure"
        # Keep the readback window open so the pilot can try again.
        return _transmit(engine, airport, text, channel)

    if intent == "ready_departure":
        return _play_departure_ready(engine, match)

    if match.kind == "step":
        # Do not hand to tower until the taxi runway readback is done.
        if (
            (match.template == "monitor_tower" or intent == "at_eor")
            and engine.state.get("awaiting_readback")
            and str(engine.state.get("last_tx_template") or "") == "taxi"
        ):
            return {
                "action": "none",
                "detail": "finish taxi readback first (say the runway or taxi to EOR)",
            }
        # Taxi + runway change in one transmission: apply the runway first so
        # the taxi clearance uses it.
        if match.template == "taxi" or intent == "ready_taxi":
            rwy = _apply_runway_if_heard(engine, match, airport)
            result = _play_step(engine, match)
            if rwy:
                result["runway"] = rwy
            return result
        # Bandsaw check-in: talk to them, but stay on the Bandsaw step until
        # checkout — picture / declare / dope happen in that window.
        if intent == "bandsaw_check_in" or match.template == "bandsaw_check_in":
            alpha_spoken = None
            fix = atc_phrase.resolve_alpha_bullseye(
                engine.config, callsign=callsign, opus=opus
            )
            if fix and fix.get("spoken"):
                alpha_spoken = str(fix["spoken"])
            text = atc_phrase.build_bandsaw_check_in(
                callsign, alpha_bullseye=alpha_spoken
            )
            return _transmit(engine, airport, text, "bandsaw")
        # Bandsaw checkout advances past the optional Bandsaw steps.
        if intent == "bandsaw_check_out" or match.template == "bandsaw_check_out":
            played = _play_step(engine, match)
            if played.get("action") != "none":
                return played
            # No bandsaw_check_out step in this mission — reply and skip ahead
            # past any remaining Bandsaw cursor (check-in holds until checkout).
            text = atc_phrase.build_bandsaw_check_out(airport, callsign)
            result = _transmit(engine, airport, text, "bandsaw")
            if result.get("action") == "transmit":
                _advance_past_bandsaw(engine)
            return result
        # Approach check-in: METAR / route auto-assign recovery / IAF; hold cursor.
        if intent == "inbound_recovery" or match.template == "approach_check_in":
            return _approach_check_in(
                engine, airport, callsign, weather, match, opus=opus
            )
        # Continue to tower / cleared approach — play step and leave hold window.
        if intent == "approach_continue" or match.template == "cleared_approach":
            played = _play_step(engine, match)
            if played.get("action") != "none":
                return played
            plan = atc_phrase.assign_approach_plan(
                airport,
                weather,
                mission=engine.mission,
                state=engine.state,
                opus=opus,
            )
            text = atc_phrase.build_approach_tower_handoff(
                airport, callsign, plan=plan
            )
            result = _transmit(engine, airport, text, "approach")
            if result.get("action") == "transmit":
                _advance_to_tower_approach(engine)
            return result
        return _play_step(engine, match)

    return {"action": "none", "detail": f"unhandled intent {intent}"}


def _call_text(match: voice_intent.Match) -> str:
    return str(match.normalized or voice_intent.normalize(match.transcript) or "")


def _usable_runways(airport: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            atc_phrase.airport_ops_runways(airport)
            + atc_phrase.airport_instrument_runways(airport)
        )
    )


def _taxi_also_heard(match: voice_intent.Match) -> bool:
    """True when the same call also asks to taxi (not taxi-in)."""
    text = _call_text(match)
    if not text:
        return False
    if voice_intent._group_hit(
        text, ("taxi in", "clear of the", "off the active"), fuzzy=False
    ):
        return False
    return bool(voice_intent._group_hit(text, ("taxi",)))


def _eor_also_heard(match: voice_intent.Match) -> bool:
    """True when the same call also reports at the EOR / holding short."""
    text = _call_text(match)
    if not text:
        return False
    return bool(
        voice_intent._group_hit(
            text,
            (
                "at eor",
                "at the eor",
                "ready at eor",
                "parked eor",
                "parked at eor",
                "holding eor",
                "holding at eor",
                "holding short",
                "at the end",
                "we re at eor",
                "we are at eor",
            ),
            fuzzy=False,
        )
    )


def _should_reissue_taxi(engine: Any, match: voice_intent.Match) -> bool:
    """
    Re-issue taxi instructions when the pilot changes runway during the
    Ground taxi / readback phase (not after monitor-tower / airborne).
    """
    if _taxi_also_heard(match):
        return True
    last = str(engine.state.get("last_tx_template") or "")
    if last != "taxi":
        return False
    if engine.state.get("awaiting_readback"):
        return True
    step = engine.current_step() or {}
    return str(step.get("template") or "") in ("taxi", "monitor_tower")


def _apply_runway_if_heard(
    engine: Any, match: voice_intent.Match, airport: dict[str, Any]
) -> str | None:
    """Apply a runway change heard in the same call; return the runway or None."""
    text = _call_text(match)
    runway = voice_intent.extract_runway(text, _usable_runways(airport))
    if not runway:
        return None
    atc_phrase.apply_pilot_request(
        f"request_runway_{runway}", mission=engine.mission, state=engine.state
    )
    engine.save_state()
    return runway


def _play_template(engine: Any, template: str) -> dict[str, Any]:
    """Play the nearest enabled step with this phrase template."""
    index = int(engine.state.get("index") or 0)
    step = _find_step(engine.steps, template, index)
    if step is None:
        return {"action": "none", "detail": f"no {template} step"}
    return {"action": "play", "detail": engine.play_id(step.get("id"))}


def _acknowledge(engine: Any, match: voice_intent.Match) -> dict[str, Any]:
    """
    Pilot read the last clearance back.

    A clearance has a scripted "readback correct" reply to play; taxi, takeoff
    and landing clearances have none, so the readback just closes the window
    and Fly stops asking for it.
    """
    confirm = str(engine.state.get("awaiting_confirm_template") or "")
    engine.clear_readback()
    if confirm:
        index = int(engine.state.get("index") or 0)
        step = _find_step(engine.steps, confirm, index)
        if step is not None:
            return {"action": "play", "detail": engine.play_id(step.get("id"))}
    return {"action": "acknowledged", "detail": "readback noted"}


def _request_unrestricted_climb(
    engine: Any,
    airport: dict[str, Any],
    callsign: str,
    match: voice_intent.Match,
) -> dict[str, Any]:
    """
    Tower: 'on request, standby' — then a short pause while he 'coordinates'.

    UI schedules the follow-up via the deferred payload (approve / unable, then
    cleared-for-takeoff).
    """
    del match
    if not atc_phrase.unrestricted_climb_enabled(engine.config, engine.mission):
        return {"action": "none", "detail": "unrestricted climb disabled"}
    if not atc_phrase.unrestricted_climb_window_open(engine.state):
        return {"action": "none", "detail": "already cleared for takeoff"}

    # Already coordinating — remind them without stacking another timer.
    if engine.state.get("pending_unrestricted_climb"):
        text = atc_phrase.build_unrestricted_climb_standby(callsign)
        return _transmit(engine, airport, text, "tower")

    engine.state["pending_unrestricted_climb"] = True
    engine.save_state()
    text = atc_phrase.build_unrestricted_climb_standby(callsign)
    result = _transmit(engine, airport, text, "tower")
    result["deferred"] = {
        "kind": "unrestricted_climb",
        "delay_s": round(random.uniform(4.0, 8.0), 1),
        "approve_chance": atc_phrase.unrestricted_climb_approve_chance(
            engine.config, engine.mission
        ),
    }
    return result


def resolve_unrestricted_climb(
    engine: Any,
    *,
    approve_chance: float | None = None,
) -> dict[str, Any]:
    """
    After the coordination pause: approve or deny, then move to takeoff clearance.
    """
    airport = engine.airport()
    opus, _weather = atc_phrase.resolve_opus_and_metar(engine.config, airport["icao"])
    if not opus:
        opus = atc_phrase.synthetic_flight_context(
            atc_phrase.callsign_override(engine.config) or "CALLSIGN"
        )
    callsign = opus.radio_callsign

    if not engine.state.get("pending_unrestricted_climb"):
        return {"action": "none", "detail": "no pending unrestricted climb"}
    if not atc_phrase.unrestricted_climb_window_open(engine.state):
        engine.state["pending_unrestricted_climb"] = False
        engine.save_state()
        return {"action": "none", "detail": "already cleared for takeoff"}

    chance = (
        float(approve_chance)
        if approve_chance is not None
        else atc_phrase.unrestricted_climb_approve_chance(engine.config, engine.mission)
    )
    # Ceiling is the filed altitude — never invent a higher one.
    climb_ft = atc_phrase.unrestricted_climb_ceiling_ft(opus)
    approved = bool(climb_ft) and random.random() < max(0.0, min(1.0, chance))
    if approved and climb_ft is not None:
        atc_phrase.stick_shared_climb_ft(
            climb_ft, step=None, mission=engine.mission
        )
        engine.state["unrestricted_climb"] = "approved"
        engine.state["unrestricted_climb_ft"] = climb_ft
        engine.state["initial_climb_ft"] = climb_ft
    else:
        approved = False
        climb_ft = None
        engine.state["unrestricted_climb"] = "denied"
        engine.state.pop("unrestricted_climb_ft", None)

    engine.state["pending_unrestricted_climb"] = False
    engine.save_state()

    # Takeoff clearance replaces any outstanding LUAW / prior readback.
    if hasattr(engine, "clear_readback") and engine.state.get("awaiting_readback"):
        engine.clear_readback()

    if approved:
        # One call: climb unrestricted up to FL…, winds…, cleared for takeoff.
        played = _play_clear_takeoff(engine)
        text = ""
        if isinstance(played, dict):
            detail = played.get("detail")
            if isinstance(detail, dict):
                text = str(detail.get("text") or "")
            elif played.get("text"):
                text = str(played.get("text") or "")
        return {
            "action": "play" if played.get("action") == "play" else "transmit",
            "text": text,
            "channel": "tower",
            "approved": True,
            "climb_ft": climb_ft,
            "detail": played.get("detail") if isinstance(played, dict) else played,
            "takeoff": played,
        }

    unable = atc_phrase.build_unrestricted_climb_unable(callsign)
    tx = _transmit(engine, airport, unable, "tower")
    played = _play_clear_takeoff(engine)
    return {
        "action": "transmit",
        "text": unable,
        "channel": "tower",
        "exit_code": tx.get("exit_code"),
        "approved": False,
        "climb_ft": None,
        "detail": played.get("detail") if isinstance(played, dict) else played,
        "takeoff": played,
    }


def _play_clear_takeoff(engine: Any) -> dict[str, Any]:
    """Advance to the cleared-takeoff step and transmit it."""
    if hasattr(engine, "prepare_takeoff_cursor"):
        engine.prepare_takeoff_cursor()
    steps = engine.steps
    if not steps:
        return {"action": "none", "detail": "no enabled steps"}
    index = int(engine.state.get("index") or 0)
    for i in range(max(0, index), len(steps)):
        step = steps[i]
        tmpl = str(step.get("template") or "")
        if tmpl == "lineup" and atc_phrase.should_skip_takeoff_step(
            step, engine.mission, engine.state
        ):
            continue
        if tmpl in ("clear_takeoff", "clear_takeoff_rolling", "clear_takeoff_intersection"):
            return {"action": "play", "detail": engine.play_id(step.get("id"))}
        # Still on LUAW / rolling offer — skip ahead to takeoff clearance.
        if tmpl in ("lineup", "rolling_accept"):
            continue
    # Look anywhere if the cursor already passed a takeoff step somehow.
    for step in steps:
        tmpl = str(step.get("template") or "")
        if tmpl in ("clear_takeoff", "clear_takeoff_rolling", "clear_takeoff_intersection"):
            return {"action": "play", "detail": engine.play_id(step.get("id"))}
    return {"action": "none", "detail": "no clear_takeoff step"}


def _speak_reply(
    intent: str,
    engine: Any,
    airport: dict[str, Any],
    callsign: str,
    weather: atc_phrase.Weather,
    opus: Any,
    match: voice_intent.Match,
) -> dict[str, Any]:
    channel = _resolve_tx_channel(engine, airport, match)
    if intent == "request_winds":
        text = voice_actions.build_winds_reply(airport, callsign, weather)
    elif intent == "request_altimeter":
        text = voice_actions.build_altimeter_reply(airport, callsign, weather)
    else:
        if channel not in ("blackjack", "bandsaw", "ops", "other"):
            channel = "blackjack"
        agency = atc_phrase.speak_agency_name(channel)
        cs = atc_phrase.speak_callsign(callsign)
        try:
            if intent == "request_bogey_dope":
                text, _groups = voice_actions.build_bogey_dope_reply(
                    engine.config, airport, callsign, agency=agency, opus=opus
                )
            elif intent == "request_declare":
                text, _groups = voice_actions.build_declare_reply(
                    engine.config,
                    airport,
                    callsign,
                    agency=agency,
                    opus=opus,
                    transcript=match.normalized or match.transcript or "",
                )
            else:
                text, _groups = voice_actions.build_picture_reply(
                    engine.config, airport, callsign, agency=agency, opus=opus
                )
        except voice_actions.RadarUnavailable:
            if intent == "request_bogey_dope":
                what = "bogey dope"
            elif intent == "request_declare":
                what = "declare"
            else:
                what = "picture"
            text = f"{cs}, {agency}, unable {what}, radar is down."
    return _transmit(engine, airport, text, channel)


def _ack(
    kind: str,
    engine: Any,
    airport: dict[str, Any],
    callsign: str,
    runway: str | None = None,
    match: voice_intent.Match | None = None,
) -> dict[str, Any]:
    channel = _resolve_tx_channel(engine, airport, match)
    text = atc_phrase.build_pilot_request_ack(
        kind, airport, callsign, runway=runway, channel=channel
    )
    return _transmit(engine, airport, text, channel)


def _transmit(engine: Any, airport: dict[str, Any], text: str, channel: str) -> dict[str, Any]:
    srs_radio.apply_config(engine.config)
    allowed, msg, _result = srs_radio.check_freq_gate(
        engine.config, airport, None, channel=channel
    )
    if not allowed:
        return {"action": "blocked", "detail": msg, "channel": channel}
    freq, mod, tx_name = atc_phrase.channel_radio(airport, channel)
    voice_name, _ = atc_phrase.voice_for_step(engine.config, channel, None)
    code = atc_phrase.transmit(
        engine.config,
        airport,
        text,
        tx_name,
        freq,
        mod,
        channel=channel,
        voice_override=voice_name,
    )
    return {"action": "transmit", "text": text, "channel": channel, "exit_code": code}


def _advance_past_bandsaw(engine: Any) -> None:
    """Move the cursor past consecutive Bandsaw steps (check-in / check-out)."""
    steps = list(engine.steps or [])
    idx = int(engine.state.get("index") or 0)
    while idx < len(steps):
        step = steps[idx]
        ch = str(step.get("channel") or "").lower()
        tmpl = str(step.get("template") or "")
        if ch == "bandsaw" or tmpl.startswith("bandsaw_"):
            idx += 1
            continue
        break
    engine.state["index"] = idx
    if hasattr(engine, "_advance_past_skippable"):
        engine._advance_past_skippable()
    engine.save_state()


def _advance_to_tower_approach(engine: Any) -> None:
    """Leave Approach hold steps; land on tower / cleared-approach successor."""
    steps = list(engine.steps or [])
    idx = int(engine.state.get("index") or 0)
    while idx < len(steps):
        tmpl = str(steps[idx].get("template") or "")
        ch = str(steps[idx].get("channel") or "").lower()
        if tmpl in ("approach_check_in", "approach_procedure", "approach_iaf") or (
            ch == "approach" and tmpl != "cleared_approach"
        ):
            idx += 1
            continue
        break
    # If still on cleared_approach, play_id path usually advances after TX;
    # here we only skipped the hold window — leave index on cleared or tower.
    if idx < len(steps) and str(steps[idx].get("template") or "") == "cleared_approach":
        idx += 1
    engine.state["index"] = idx
    if hasattr(engine, "_advance_past_skippable"):
        engine._advance_past_skippable()
    engine.save_state()


def _approach_check_in(
    engine: Any,
    airport: dict[str, Any],
    callsign: str,
    weather: Any,
    match: voice_intent.Match,
    *,
    opus: Any = None,
) -> dict[str, Any]:
    """Assign (or refresh from slots / filed route) and transmit Approach check-in."""
    slots = match.slots or {}
    plan = atc_phrase.assign_approach_plan(
        airport,
        weather,
        mission=engine.mission,
        state=engine.state,
        opus=opus,
        force=bool(slots.get("recovery") or slots.get("vfr_recovery") or slots.get("iaf")),
        recovery=slots.get("recovery"),
        vfr_recovery=slots.get("vfr_recovery"),
        iaf=slots.get("iaf"),
        position=atc_phrase.ownship_latlon(
            engine.config, callsign=callsign, opus=opus, state=engine.state
        ),
    )
    engine.save_state()
    # Prefer playing the flow step so last_tx / readback state stay consistent.
    played = _play_step(engine, match)
    if played.get("action") != "none":
        return played
    text = atc_phrase.build_approach_recovery(
        airport,
        callsign,
        weather,
        str(plan.get("runway") or ""),
        recovery=str(plan.get("pattern") or ""),
        plan=plan,
    )
    return _transmit(engine, airport, text, "approach")


def _handle_approach_action(
    intent: str,
    engine: Any,
    airport: dict[str, Any],
    callsign: str,
    weather: Any,
    match: voice_intent.Match,
    *,
    opus: Any = None,
) -> dict[str, Any]:
    slots = match.slots or {}
    if intent == "request_approach":
        # No fixed plate here — a named IAF selects its own procedure, and a
        # bare "request instrument" is resolved from the route or position.
        plan = atc_phrase.assign_approach_plan(
            airport,
            weather,
            mission=engine.mission,
            state=engine.state,
            opus=opus,
            force=True,
            recovery=slots.get("recovery"),
            vfr_recovery=slots.get("vfr_recovery"),
            iaf=slots.get("iaf"),
            position=atc_phrase.ownship_latlon(
                engine.config, callsign=callsign, opus=opus, state=engine.state
            ),
        )
        engine.save_state()
        text = atc_phrase.build_approach_change(
            airport, callsign, weather, plan=plan
        )
        return _transmit(engine, airport, text, "approach")

    plan = atc_phrase.assign_approach_plan(
        airport,
        weather,
        mission=engine.mission,
        state=engine.state,
        opus=opus,
    )

    if intent == "request_hold":
        catalog = atc_phrase.load_approach_catalog(airport)
        hold_tok = slots.get("iaf") or slots.get("vfr_recovery") or "IAF"
        hold = atc_phrase.find_hold(catalog, str(hold_tok))
        if hold is None and plan.get("iaf"):
            hold = atc_phrase.find_hold(catalog, "IAF")
        engine.state["hold_active"] = True
        engine.save_state()
        text = atc_phrase.build_hold_clearance(
            airport, callsign, hold=hold, plan=plan, efc_minutes=5
        )
        return _transmit(engine, airport, text, "approach")

    if intent == "cancel_hold":
        engine.state["hold_active"] = False
        engine.save_state()
        text = atc_phrase.build_leave_hold_clearance(
            airport, callsign, plan=plan
        )
        return _transmit(engine, airport, text, "approach")

    if intent == "request_vectors":
        engine.state["vectors_active"] = True
        engine.save_state()
        text = atc_phrase.build_vector_clearance(
            airport, callsign, plan=plan
        )
        return _transmit(engine, airport, text, "approach")

    return {"action": "none", "detail": f"unhandled approach action {intent}"}


def _resolve_tx_channel(
    engine: Any,
    airport: dict[str, Any],
    match: voice_intent.Match | None,
) -> str:
    """
    Agency to transmit on for ad-hoc voice replies.

    Prefer the agency the pilot addressed; otherwise the agency matching the
    active radio tune (EAM selected / DCS export); never the flow cursor alone
    when that would TX on a different frequency than the pilot is on.
    """
    addressed = ""
    if match is not None:
        addressed = str(match.slots.get("channel") or "").strip().lower()
    if addressed:
        return addressed
    tuned = srs_radio.channel_for_tuned_freq(airport, engine.config)
    if tuned:
        return tuned
    return _current_channel(engine) or "tower"


def _current_channel(engine: Any) -> str:
    step = engine.current_step()
    if not step:
        return ""
    return str(step.get("channel") or step.get("phase") or "")


def _play_departure_ready(engine: Any, match: voice_intent.Match) -> dict[str, Any]:
    """
    "Ready for departure" answers the current Tower takeoff step.

    Normally that is line-up-and-wait (or "will you accept rolling?"). Cleared
    takeoff only plays once the cursor has advanced there — e.g. after LUAW,
    or when rolling mode has skipped LUAW.
    """
    del match  # intent routing only; play uses the flow cursor
    if hasattr(engine, "prepare_takeoff_cursor"):
        engine.prepare_takeoff_cursor()
    steps = engine.steps
    if not steps:
        return {"action": "none", "detail": "no enabled steps"}
    index = int(engine.state.get("index") or 0)
    current = steps[index] if 0 <= index < len(steps) else None
    if current and atc_phrase.is_takeoff_related_template(current.get("template")):
        return {"action": "play", "detail": engine.play_id(current.get("id"))}
    for i in range(max(0, index), len(steps)):
        step = steps[i]
        tmpl = str(step.get("template") or "")
        if tmpl == "lineup" and atc_phrase.should_skip_takeoff_step(
            step, engine.mission, engine.state
        ):
            continue
        if atc_phrase.is_takeoff_related_template(tmpl):
            return {"action": "play", "detail": engine.play_id(step.get("id"))}
    return {"action": "none", "detail": "no takeoff step ahead"}


def _play_step(engine: Any, match: voice_intent.Match) -> dict[str, Any]:
    """
    Fire the flow step this pilot call answers.

    Prefers the step under the cursor so a generic check-in advances normally,
    then looks ahead for the matching template.
    """
    steps = engine.steps
    if not steps:
        return {"action": "none", "detail": "no enabled steps"}
    index = int(engine.state.get("index") or 0)

    # A phrase written on a step names that step outright — no template search.
    if match.step_id:
        if any(s.get("id") == match.step_id for s in steps):
            return {"action": "play", "detail": engine.play_id(match.step_id)}
        return {"action": "none", "detail": f"step {match.step_id} is not enabled"}

    if match.template:
        current = steps[index] if 0 <= index < len(steps) else None
        if not current or current.get("template") != match.template:
            target = _find_step(steps, match.template, index)
            if target is None:
                return {"action": "none", "detail": f"no step for {match.template}"}
            return {"action": "play", "detail": engine.play_id(target.get("id"))}

    if index >= len(steps):
        return {"action": "none", "detail": "end of flow"}
    return {"action": "next", "detail": engine.next()}


def _find_step(
    steps: list[dict[str, Any]], template: str, start: int
) -> dict[str, Any] | None:
    """Nearest step with this template: forward from the cursor, else anywhere."""
    for step in steps[start:]:
        if step.get("template") == template:
            return step
    for step in steps:
        if step.get("template") == template:
            return step
    return None
