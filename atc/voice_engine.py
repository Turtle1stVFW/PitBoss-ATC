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

import threading
import time
from collections.abc import Callable
from typing import Any

try:
    import numpy as np
except ImportError:  # voice control is optional — the rest of the app still runs
    np = None

import atc_phrase
import hotkeys
import joystick
import mic_capture
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
    "clearance delivery, Blackjack, Magic. Request taxi, ready for departure, "
    "cleared for takeoff, request runway two one left, say winds, say altimeter, "
    "request picture, alpha check, bullseye, angels, rolling departure, "
    "line up and wait, gear down full stop, tactical overhead, say again."
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

    if intent in ("request_winds", "request_altimeter", "request_picture"):
        return _speak_reply(intent, engine, airport, callsign, weather, opus, match)

    if intent == "request_runway":
        runway = match.slots.get("runway")
        if not runway:
            return {"action": "none", "detail": "no runway heard"}
        atc_phrase.apply_pilot_request(
            f"request_runway_{runway}", mission=engine.mission, state=engine.state
        )
        engine.save_state()
        return _ack("request_runway", engine, airport, callsign, runway=runway, match=match)

    if intent in ("accept_rolling", "deny_rolling", "request_lineup"):
        atc_phrase.apply_pilot_request(intent, mission=engine.mission, state=engine.state)
        engine.save_state()
        return _ack(intent, engine, airport, callsign, match=match)

    if intent == "request_alpha_check":
        fix = atc_phrase.resolve_alpha_bullseye(config, callsign=callsign, opus=opus)
        text = atc_phrase.build_standalone_alpha_check(
            callsign, (fix or {}).get("spoken")
        )
        return _transmit(engine, airport, text, "blackjack")

    if intent == "acknowledge_readback":
        return _acknowledge(engine, match)

    if match.kind == "step":
        return _play_step(engine, match)

    return {"action": "none", "detail": f"unhandled intent {intent}"}


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


def _speak_reply(
    intent: str,
    engine: Any,
    airport: dict[str, Any],
    callsign: str,
    weather: atc_phrase.Weather,
    opus: Any,
    match: voice_intent.Match,
) -> dict[str, Any]:
    channel = match.slots.get("channel") or _current_channel(engine) or "tower"
    if intent == "request_winds":
        runway = atc_phrase.active_runway(airport.get("runways") or [], weather.wind_dir)
        text = voice_actions.build_winds_reply(airport, callsign, weather, runway)
    elif intent == "request_altimeter":
        text = voice_actions.build_altimeter_reply(airport, callsign, weather)
    else:
        channel = "blackjack"
        agency = "Blackjack"
        try:
            text, _groups = voice_actions.build_picture_reply(
                engine.config, airport, callsign, agency=agency, opus=opus
            )
        except voice_actions.RadarUnavailable:
            text = (
                f"{atc_phrase.speak_callsign(callsign)}, {agency}, "
                "unable picture, radar is down."
            )
    return _transmit(engine, airport, text, channel)


def _ack(
    kind: str,
    engine: Any,
    airport: dict[str, Any],
    callsign: str,
    runway: str | None = None,
    match: voice_intent.Match | None = None,
) -> dict[str, Any]:
    addressed = (match.slots.get("channel") if match else None) or ""
    channel = addressed or _current_channel(engine) or "tower"
    text = atc_phrase.build_pilot_request_ack(
        kind, airport, callsign, runway=runway, channel=channel
    )
    return _transmit(engine, airport, text, channel)


def _transmit(engine: Any, airport: dict[str, Any], text: str, channel: str) -> dict[str, Any]:
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


def _current_channel(engine: Any) -> str:
    step = engine.current_step()
    if not step:
        return ""
    return str(step.get("channel") or step.get("phase") or "")


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
