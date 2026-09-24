"""
Voice control: hold the SRS PTT, talk, and ATC answers.

Pipeline — PTT press marks the mic ring buffer, PTT release hands the captured
audio to faster-whisper on a worker thread, the transcript goes through the
keyword grammar in `voice_intent`. If that misses, `voice_nlu` may map an
addressed call onto an allowed intent using the boom-chat LLM; it never writes
a new clearance. A match is then executed against the flow engine.

Whisper runs on CPU int8 on purpose: base.en costs ~350 ms for a typical radio
call and leaves the GPU entirely to DCS. The model is loaded once at startup so
no transmission pays the load cost. Ollama (NLU / boom chat) is pinned to CPU
the same way — a GPU load during a call has frozen the display and shut the PC
down.
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


def _context_seat(context: dict[str, Any] | None) -> int | None:
    raw = (context or {}).get("seat")
    if raw is None or raw == "":
        return None
    try:
        seat = int(raw)
    except (TypeError, ValueError):
        return None
    return seat if seat > 0 else None

# Steers Whisper towards callsigns and phraseology instead of plain English.
_BASE_PROMPT = (
    "Radio call to air traffic control. Nellis ground, tower, approach, departure, "
    "clearance delivery, Blackjack, Bandsaw, Magic. Request taxi, ready for departure, "
    "in position, cleared for takeoff, request handoff, established, initial, with you, "
    "request runway two one left, say winds, say altimeter, "
    "request picture, bogey dope, declare, alpha check, bullseye, angels, rolling departure, "
    "line up and wait, gear down full stop, tactical overhead, high key, low key, base key, "
    "SFO, simulated flameout, the option, on the go, say again. "
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
                try:
                    import app_diag

                    app_diag.error(app_diag.CAT_LIBRARY, self.error)
                except Exception:
                    pass
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
                try:
                    import app_diag

                    app_diag.error(
                        app_diag.CAT_LIBRARY,
                        f"WhisperModel load failed: {self.error}",
                        model=self.model_size,
                        device=self.device,
                    )
                except Exception:
                    pass
                return False
        self.error = ""
        self.ready.set()
        try:
            import app_diag

            app_diag.info(
                app_diag.CAT_LIBRARY,
                "WhisperModel loaded",
                model=self.model_size,
                device=self.device,
                compute=self.compute_type,
            )
        except Exception:
            pass
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
        self._config: dict[str, Any] = {}

    # ---- lifecycle -------------------------------------------------------

    def start(self, config: dict[str, Any]) -> list[str]:
        """(Re)configure and start listening. Returns warnings."""
        self.stop()
        warnings: list[str] = []
        if not config.get("voice_enabled"):
            return warnings
        self._config = dict(config)
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
            elapsed = time.perf_counter() - started
            self.on_status(
                f"Listening — {transcriber.model_size} ready "
                f"({elapsed:.1f}s)"
            )
            try:
                import app_diag

                app_diag.info(
                    app_diag.CAT_VOICE,
                    "Whisper warm complete",
                    model=transcriber.model_size,
                    seconds=round(elapsed, 2),
                )
            except Exception:
                pass

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

    @property
    def ptt_held(self) -> bool:
        """True while the voice PTT is down (pilot still on the radio)."""
        with self._lock:
            return self._held > 0

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
            try:
                import app_diag

                app_diag.error(app_diag.CAT_VOICE, f"transcribe failed: {exc}")
            except Exception:
                pass
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
            current_step_id=str(context.get("current_step_id") or ""),
            last_tx_text=str(context.get("last_tx_text") or ""),
            last_tx_at=float(context.get("last_tx_at") or 0.0),
            last_tx_channel=str(context.get("last_tx_channel") or ""),
            last_tx_template=str(context.get("last_tx_template") or ""),
            pending_contact=str(context.get("pending_contact") or ""),
            tanker_chat_choices=context.get("tanker_chat_choices")
            if isinstance(context.get("tanker_chat_choices"), list)
            else None,
            tanker_chat_session=bool(context.get("tanker_chat_session")),
            tanker_chat_awaiting_react=bool(
                context.get("tanker_chat_awaiting_react")
            ),
            tanker_chat_freeform=bool(context.get("tanker_chat_freeform")),
            tanker_chat_last_spoke=str(context.get("tanker_chat_last_spoke") or ""),
            tanker_chat_guard_until=float(context.get("tanker_chat_guard_until") or 0),
            seat=_context_seat(context),
            tuned_channel=str(context.get("tuned_channel") or "") or None,
            cursor_channel=str(context.get("cursor_channel") or ""),
            sfo_active=bool(context.get("sfo_active")),
        )
        if evaluation.match is None:
            try:
                import voice_nlu

                nlu_match = voice_nlu.classify(
                    evaluation,
                    config=self._config,
                    channel=str(context.get("channel") or ""),
                    phase=str(context.get("phase") or ""),
                    expected=str(context.get("expected") or ""),
                    callsign=str(context.get("callsign") or ""),
                    runways=context.get("runways")
                    if isinstance(context.get("runways"), list)
                    else None,
                    awaiting_readback=bool(context.get("awaiting_readback")),
                    steps=context.get("steps")
                    if isinstance(context.get("steps"), list)
                    else None,
                    current_step_id=str(context.get("current_step_id") or ""),
                )
            except Exception:
                nlu_match = None
            if nlu_match is not None:
                evaluation.match = nlu_match
                evaluation.reason = ""
                evaluation.advice = "understood via LLM"
                nlu_ms = (time.perf_counter() - started) * 1000
                self.on_status(f"{elapsed:.0f}ms + NLU {nlu_ms - elapsed:.0f}ms · {text}")
            else:
                self.on_status(f"{elapsed:.0f}ms · {text}")
        else:
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
    intent = match.intent
    if intent == "say_again":
        text = str((engine.state or {}).get("last_tx_text") or "").strip()
        last = (engine.state or {}).get("last_step_id")
        if not text and not last:
            return {"action": "none", "detail": "nothing to repeat"}
        if hasattr(engine, "replay_last_tx"):
            detail = engine.replay_last_tx()
            return {
                "action": "replay",
                "text": detail.get("text"),
                "channel": detail.get("channel"),
                "detail": detail,
            }
        if not last:
            return {"action": "none", "detail": "nothing to repeat"}
        return {"action": "replay", "detail": engine.play_id(last)}

    config = engine.config
    airport = engine.airport()
    opus, weather = atc_phrase.resolve_opus_and_metar(config, airport["icao"])
    if not opus:
        opus = atc_phrase.synthetic_flight_context(
            atc_phrase.callsign_override(config) or "CALLSIGN"
        )
    callsign = opus.radio_callsign

    try:
        import agencies as agencies_mod

        addressed = str((match.slots or {}).get("channel") or "").strip().lower()
        current_field = agencies_mod.field_agency(engine)
        if (
            addressed
            and agencies_mod.too_early_field(current_field, addressed)
            and intent
            not in (
                "acknowledge_readback",
                "say_again",
                "request_winds",
                "request_altimeter",
                "tower_check_in",
                "tower_initial",
            )
        ):
            text = agencies_mod.build_field_redirect(
                airport, callsign, addressed, current_field
            )
            for ch in (addressed, current_field):
                result = _transmit(engine, airport, text, ch)
                if result.get("action") != "blocked":
                    result["field_redirect"] = True
                    result["stay_with"] = current_field
                    return result
            return result
    except Exception:
        pass

    if intent in (
        "request_winds",
        "request_altimeter",
        "request_picture",
        "request_bogey_dope",
        "request_declare",
        "report_vid",
    ):
        return _speak_reply(intent, engine, airport, callsign, weather, opus, match)

    if intent == "request_bandsaw":
        text = atc_phrase.build_contact_bandsaw(airport, callsign)
        return _transmit(
            engine, airport, text, "blackjack", template="contact_bandsaw"
        )

    if intent == "request_joshua":
        text = atc_phrase.build_contact_joshua(airport, callsign)
        return _transmit(
            engine, airport, text, "blackjack", template="contact_joshua"
        )

    if intent == "request_control":
        ctrl = atc_phrase.control_channel_for_ownship(
            airport,
            config=getattr(engine, "config", None),
            callsign=callsign,
            opus=opus,
            state=getattr(engine, "state", None),
        )
        text = atc_phrase.build_contact_control(
            airport, callsign, handoff_channel=ctrl
        )
        st = getattr(engine, "state", None)
        if isinstance(st, dict) and ctrl:
            st["control_channel"] = ctrl
        return _transmit(
            engine, airport, text, "blackjack", template="contact_control"
        )

    if intent in (
        "request_tanker",
        "tanker_return",
        "tanker_check_in",
        "tanker_tacan",
        "tanker_freq",
        "tanker_bullseye",
        "tanker_astern",
        "tanker_contact",
        "tanker_disconnect",
        "tanker_depart",
        "tanker_chat_start",
        "tanker_chat_reply",
        "tanker_chat_stop",
    ):
        return execute_tanker_action(
            engine,
            intent,
            match=match,
            airport=airport,
            callsign=callsign,
            opus=opus,
        )

    if intent in (
        "ops_request_words",
        "ops_request_start",
        "ops_status",
        "ops_check_in",
    ):
        return execute_ops_action(
            engine,
            intent,
            match=match,
            airport=airport,
            callsign=callsign,
            opus=opus,
        )

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

    if intent in ("accept_rolling", "deny_rolling", "request_lineup", "request_rolling"):
        answering = _rolling_offer_open(engine)
        last_tx = str(engine.state.get("last_tx_template") or "")
        # Repeating LUAW after Tower already issued it is the readback, not a
        # new request (which would TX "expect line up and wait").
        if (
            intent == "request_lineup"
            and not answering
            and last_tx in ("lineup", "line_up_and_wait")
        ):
            return _acknowledge(engine, match)
        mode_change = atc_phrase.takeoff_request_changes_mode(
            intent, engine.mission, engine.state
        )
        atc_phrase.apply_pilot_request(intent, mission=engine.mission, state=engine.state)
        engine.save_state()
        # LUAW / takeoff readback is a different clearance — drop it so the
        # rolling (or LUAW) request is not stuck behind "say the runway".
        if (
            hasattr(engine, "clear_readback")
            and engine.state.get("awaiting_readback")
            and last_tx in (
                "lineup",
                "line_up_and_wait",
                "rolling_accept",
                "clear_takeoff",
                "clear_takeoff_rolling",
                "clear_takeoff_intersection",
            )
        ):
            engine.clear_readback()
        if hasattr(engine, "prepare_takeoff_cursor"):
            engine.prepare_takeoff_cursor()
            engine.save_state()
        if answering:
            played = play_rolling_offer_reply(engine, intent)
            if played.get("action") != "none":
                return played
        # Same takeoff type as already planned — they're ready; issue it.
        # "Expect…" is only for switching LUAW ↔ rolling.
        if not mode_change:
            if intent in ("request_lineup", "deny_rolling"):
                played = _play_lineup(engine)
                if played.get("action") != "none":
                    return played
            elif intent in ("request_rolling", "accept_rolling"):
                played = _play_clear_takeoff(engine)
                if played.get("action") != "none":
                    return played
        return _ack(intent, engine, airport, callsign, match=match)

    if intent == "request_unrestricted_climb":
        return _request_unrestricted_climb(engine, airport, callsign, match)

    if intent == "request_altitude_change":
        return _handle_altitude_change(
            engine, airport, callsign, match, opus=opus
        )

    if intent in ("request_point_vectors", "request_divert"):
        return _handle_navigation_request(
            intent, engine, airport, callsign, match, opus=opus
        )

    if intent == "request_alpha_check":
        fix = atc_phrase.resolve_alpha_bullseye(
            config,
            callsign=callsign,
            opus=opus,
            weather=weather,
            state=getattr(engine, "state", None),
        )
        channel = _resolve_tx_channel(engine, airport, match)
        if channel not in ("blackjack", "bandsaw", "joshua", "ops", "other"):
            channel = "blackjack"
        text = atc_phrase.build_standalone_alpha_check(
            callsign, (fix or {}).get("spoken"), agency=channel
        )
        return _transmit(engine, airport, text, channel)

    if intent == "acknowledge_readback":
        # Taxi readback often includes the destination ("taxi northwest EOR").
        # That is not arrival — monitor tower waits for the EOR zone, a manual
        # advance, or a later "at EOR" call.
        return _acknowledge(engine, match)

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

    if intent == "in_position":
        return _play_clear_takeoff(engine)

    if intent == "request_low_approach":
        # Readback of option / low-approach clearance — not a fresh request.
        if atc_phrase.awaiting_option_on_the_go(engine.state) or (
            atc_phrase.landing_already_cleared(engine.state)
            and atc_phrase.resolve_landing_intent(engine.state)
            == atc_phrase.LANDING_INTENT_LOW_APPROACH
        ):
            return _acknowledge(engine, match)
        atc_phrase.apply_pilot_request(
            "request_low_approach",
            mission=engine.mission,
            state=engine.state,
            airport=airport,
            weather=weather,
            opus=opus,
            config=getattr(engine, "config", None),
        )
        engine.save_state()
        # On SFO Base Key / SI final, or already due for pattern land — clear
        # the option now instead of only "expect the option".
        if _sfo_should_clear_now(engine) or _pattern_option_clear_due(engine):
            if atc_phrase.is_sfo_recovery(state=engine.state):
                phase = atc_phrase.sfo_phase(engine.state)
                if phase in ("high_key", "approved", "low_key", "base_key"):
                    atc_phrase.note_sfo_base_key(engine.state)
            played = _play_sfo_or_pattern_clear_land(engine)
            if played.get("action") != "none":
                atc_phrase.note_sfo_cleared(engine.state)
                engine.save_state()
                return played
        return _ack(
            "request_low_approach", engine, airport, callsign, match=match
        )

    if intent in (
        "request_sfo",
        "report_high_key",
        "report_low_key",
        "report_base_key",
        "report_sfo_final",
        "request_closed_traffic",
    ):
        return _handle_sfo_action(
            intent, engine, airport, callsign, weather, match, opus=opus
        )

    if intent == "going_around":
        if hasattr(engine, "execute_go_around"):
            slots = match.slots or {}
            after = str(slots.get("after") or "").strip().casefold()
            prefer = None
            if after in ("high_key", "sfo_continue"):
                prefer = "sfo_continue"
            elif after in ("closed_traffic", "closed"):
                prefer = "closed_traffic"
            slot_rwy = str(slots.get("runway") or "").strip() or None
            detail = engine.execute_go_around(prefer=prefer, runway=slot_rwy)
            if detail.get("acknowledged"):
                return {
                    "action": "acknowledged",
                    "detail": "go-around readback noted",
                    "channel": detail.get("channel") or "tower",
                }
            return {
                "action": "play" if detail.get("exit_code") == 0 else "transmit",
                "text": detail.get("text"),
                "channel": detail.get("channel") or "tower",
                "detail": detail,
            }
        return {"action": "none", "detail": "go-around not available"}

    if match.kind == "step":
        if (
            intent in ("ready_clearance", "ready_to_copy")
            or match.template in ("clearance", "clearance_amendment")
        ) and str(engine.state.get("last_tx_template") or "") == "clearance_amendment":
            engine.state["clearance_amendment_copied"] = True
            if hasattr(engine, "save_state"):
                engine.save_state()
        # Cleared for the option → full stop / clear of runway: no second land clear.
        if atc_phrase.awaiting_option_on_the_go(engine.state) and (
            intent in ("request_landing", "clear_of_runway", "request_taxi_ramp")
            or match.template in ("clear_land", "exit_runway", "taxi_in")
        ):
            if hasattr(engine, "accept_option_full_stop"):
                prefer: tuple[str, ...] = ("exit_runway", "taxi_in")
                if intent in ("clear_of_runway", "request_taxi_ramp") or match.template == "taxi_in":
                    prefer = ("taxi_in", "exit_runway")
                elif match.template == "exit_runway":
                    prefer = ("exit_runway", "taxi_in")
                detail = engine.accept_option_full_stop(prefer_templates=prefer)
                return {
                    "action": "play",
                    "text": detail.get("text"),
                    "channel": detail.get("channel") or "tower",
                    "detail": detail,
                    "option_full_stop": True,
                }
            atc_phrase.commit_option_full_stop(
                state=engine.state, mission=engine.mission
            )
            engine.save_state()
            # Fall through to normal step play (exit / taxi) without re-clearing.

        # After landing EOR: "request taxi to the ramp" → parking clearance.
        if intent == "request_taxi_ramp" or (
            match.template == "taxi_in"
            and intent == "request_taxi_ramp"
        ):
            engine.state["taxi_in_to_ramp"] = True
            engine.state["landing_eor_complete"] = True
            if hasattr(engine, "save_state"):
                engine.save_state()
            played = _play_template(engine, "taxi_in")
            if played.get("action") != "none":
                return played
            return _play_step(engine, match)

        # Clear of runway → landing EOR (not the ramp yet).
        if intent == "clear_of_runway" or (
            match.template == "taxi_in" and intent == "clear_of_runway"
        ):
            engine.state["taxi_in_to_ramp"] = False
            engine.state.pop("landing_eor_complete", None)
            if hasattr(engine, "save_state"):
                engine.save_state()

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
        # Ground already said monitor tower — repeating it / "at EOR" is the
        # readback, not another request that re-issues and walks onto Tower.
        if (
            (match.template == "monitor_tower" or intent == "at_eor")
            and str(engine.state.get("last_tx_template") or "") == "monitor_tower"
        ):
            return _acknowledge(engine, match)
        # Taxi + runway change in one transmission: apply the runway first so
        # the taxi clearance uses it.
        if match.template == "taxi" or intent == "ready_taxi":
            last_tx = str(engine.state.get("last_tx_template") or "")
            if engine.state.get("awaiting_readback"):
                # Repeating taxi instructions is the readback, not a new call.
                if last_tx == "taxi":
                    return _acknowledge(engine, match)
                return {
                    "action": "none",
                    "detail": "finish readback first",
                }
            rwy = _apply_runway_if_heard(engine, match, airport)
            result = _play_step(engine, match)
            if rwy:
                result["runway"] = rwy
            return result
        # Bandsaw check-in: reply on Bandsaw. Do not seek the shared flight
        # cursor — other seats and the Host Fly stay where the package is.
        # Picture / dope / declare follow the live Bandsaw radio instead.
        if intent == "bandsaw_check_in" or match.template == "bandsaw_check_in":
            import tanker as tanker_mod
            import tanker_chat as tanker_chat_mod

            cur = engine.current_step() or {}
            if voice_intent.step_is_authored(cur):
                return _play_step(engine, match)
            from_tanker = tanker_mod.tanker_needs_c2_checkin(
                engine.state
            ) or tanker_mod.tanker_overlay_active(engine.state)
            already_in = bool((engine.state or {}).get("bandsaw_checked_in"))
            heard = str(match.normalized or match.transcript or "")
            if already_in and not from_tanker and not voice_intent.sounds_like_c2_checkin(
                heard
            ):
                return {
                    "action": "none",
                    "detail": "already with Bandsaw — ignored",
                    "bandsaw_already_in": True,
                }
            if from_tanker:
                tanker_mod.leave_tanker_overlay(
                    engine, "bandsaw", checkin=True
                )
                tanker_chat_mod.end_chat(engine.state)
                if hasattr(engine, "save_state"):
                    engine.save_state()
            alpha_spoken = None
            fix = atc_phrase.resolve_alpha_bullseye(
                engine.config,
                callsign=callsign,
                opus=opus,
                weather=weather,
                state=getattr(engine, "state", None),
            )
            if fix and fix.get("spoken"):
                alpha_spoken = str(fix["spoken"])
            if already_in or from_tanker:
                text = atc_phrase.build_bandsaw_continue(
                    callsign, alpha_bullseye=alpha_spoken
                )
            else:
                text = atc_phrase.build_bandsaw_check_in(
                    callsign, alpha_bullseye=alpha_spoken
                )
            if isinstance(getattr(engine, "state", None), dict):
                engine.state["bandsaw_checked_in"] = True
            tmpl = (
                "bandsaw_continue"
                if already_in or from_tanker
                else "bandsaw_check_in"
            )
            return _transmit(engine, airport, text, "bandsaw", template=tmpl)
        # Bandsaw checkout advances past the optional Bandsaw steps.
        if intent == "bandsaw_check_out" or match.template == "bandsaw_check_out":
            played = _play_step(engine, match)
            if isinstance(getattr(engine, "state", None), dict):
                engine.state.pop("bandsaw_checked_in", None)
            if played.get("action") != "none":
                _advance_past_bandsaw(engine)
                return played
            # No bandsaw_check_out step in this mission — reply and skip ahead
            # past any remaining Bandsaw / Joshua cursor.
            text = atc_phrase.build_bandsaw_check_out(airport, callsign)
            result = _transmit(engine, airport, text, "bandsaw")
            if isinstance(getattr(engine, "state", None), dict):
                engine.state.pop("bandsaw_checked_in", None)
            if result.get("action") == "transmit":
                _advance_past_bandsaw(engine)
            return result
        if intent == "joshua_check_in" or match.template == "joshua_check_in":
            import tanker as tanker_mod
            import tanker_chat as tanker_chat_mod

            cur = engine.current_step() or {}
            if voice_intent.step_is_authored(cur):
                return _play_step(engine, match)
            if hasattr(engine, "_seek_template"):
                engine._seek_template("joshua_check_in")
            if tanker_mod.tanker_needs_c2_checkin(
                engine.state
            ) or tanker_mod.tanker_overlay_active(engine.state):
                tanker_mod.leave_tanker_overlay(
                    engine, "joshua", checkin=True
                )
                tanker_chat_mod.end_chat(engine.state)
                if hasattr(engine, "save_state"):
                    engine.save_state()
            text = atc_phrase.build_joshua_check_in(
                callsign,
                position=atc_phrase.agency_position_clause(
                    config,
                    agency="joshua",
                    callsign=callsign,
                    opus=opus,
                    state=engine.state,
                ),
            )
            return _transmit(engine, airport, text, "joshua")
        if intent == "joshua_check_out" or match.template == "joshua_check_out":
            played = _play_step(engine, match)
            if played.get("action") != "none":
                _advance_past_bandsaw(engine)
                return played
            text = atc_phrase.build_joshua_check_out(airport, callsign)
            result = _transmit(engine, airport, text, "joshua")
            if result.get("action") == "transmit":
                _advance_past_bandsaw(engine)
            return result
        if intent == "control_check_in" or match.template == "control_check_in":
            cur = engine.current_step() or {}
            if voice_intent.step_is_authored(cur):
                return _play_step(engine, match)
            if hasattr(engine, "_seek_template"):
                engine._seek_template("control_check_in")
            ch = _resolve_tx_channel(engine, airport, match) or "control_east"
            if ch not in ("control_east", "control_west"):
                ch = "control_east"
            plan = atc_phrase.assign_approach_plan(
                airport,
                weather,
                mission=getattr(engine, "mission", None),
                state=engine.state,
                opus=opus,
                force=False,
            )
            engine.state["control_checked_in"] = True
            engine.state["control_channel"] = ch
            if hasattr(engine, "save_state"):
                engine.save_state()
            text = atc_phrase.build_control_check_in(
                callsign,
                channel=ch,
                airport=airport,
                plan=plan,
                position=atc_phrase.agency_position_clause(
                    config,
                    agency=ch,
                    callsign=callsign,
                    opus=opus,
                    state=engine.state,
                ),
            )
            result = _transmit(engine, airport, text, ch)
            if hasattr(engine, "_seek_template"):
                engine._seek_template("control_handoff")
                if hasattr(engine, "_advance_past_skippable"):
                    engine._advance_past_skippable()
            if hasattr(engine, "save_state"):
                engine.save_state()
            return result
        if intent == "control_handoff" or match.template == "control_handoff":
            played = _play_step(engine, match)
            if played.get("action") != "none":
                return played
            ch = _resolve_tx_channel(engine, airport, match) or "control_east"
            if ch not in ("control_east", "control_west"):
                ch = "control_east"
            text = atc_phrase.build_control_handoff(
                airport, callsign, from_channel=ch
            )
            return _transmit(engine, airport, text, ch)
        if intent == "center_check_in" or match.template in (
            "center_check_in",
            "center_radar",
        ):
            cur = engine.current_step() or {}
            if voice_intent.step_is_authored(cur):
                return _play_step(engine, match)
            if hasattr(engine, "_seek_template"):
                if not engine._seek_template("center_check_in"):
                    engine._seek_template("center_radar")
            ch = _resolve_tx_channel(engine, airport, match) or "center"
            if ch not in ("center", "other"):
                ch = "center"
            text = atc_phrase.build_center_check_in(
                callsign,
                position=atc_phrase.agency_position_clause(
                    config,
                    agency=ch,
                    callsign=callsign,
                    opus=opus,
                    state=engine.state,
                ),
            )
            return _transmit(engine, airport, text, ch)
        # Back on Blackjack after Bandsaw, the tanker, or still on the range:
        # check-in is "continue", not a second range-entry / Approach handoff.
        # Full Blackjack check-in plays once per sortie; later "with you" /
        # mis-heard calls must not re-run the airspace / VUL script.
        if intent == "range_entry" or match.template == "bj_check_in":
            import tanker as tanker_mod
            import tanker_chat as tanker_chat_mod

            cur = engine.current_step() or {}
            tmpl = str(cur.get("template") or "")
            from_tanker = tanker_mod.tanker_needs_c2_checkin(
                engine.state
            ) or tanker_mod.tanker_overlay_active(engine.state)
            already_in = bool((engine.state or {}).get("blackjack_checked_in")) or str(
                (engine.state or {}).get("last_tx_template") or ""
            ) in (
                "bj_check_in",
                "bj_continue",
                "bj_alpha_check",
                "bj_range_entry",
            )
            heard = str(match.normalized or match.transcript or "")
            if already_in and not from_tanker and not voice_intent.sounds_like_c2_checkin(
                heard
            ):
                return {
                    "action": "none",
                    "detail": "already with Blackjack — ignored",
                    "blackjack_already_in": True,
                }
            if tmpl == "bj_range_exit" or from_tanker or already_in:
                if from_tanker:
                    tanker_mod.leave_tanker_overlay(
                        engine, "blackjack", checkin=True
                    )
                    tanker_chat_mod.end_chat(engine.state)
                    if hasattr(engine, "save_state"):
                        engine.save_state()
                if hasattr(engine, "acknowledge_blackjack_continue"):
                    detail = engine.acknowledge_blackjack_continue(
                        seek_range_exit=not from_tanker
                    )
                    return {
                        "action": "play",
                        "text": detail.get("text"),
                        "channel": "blackjack",
                        "detail": detail,
                        "blackjack_continue": True,
                    }
                text = atc_phrase.build_blackjack_continue(callsign)
                if isinstance(getattr(engine, "state", None), dict):
                    engine.state["blackjack_checked_in"] = True
                return _transmit(engine, airport, text, "blackjack")
            played = _play_step(engine, match)
            if played.get("action") != "none":
                if isinstance(getattr(engine, "state", None), dict):
                    engine.state["blackjack_checked_in"] = True
                return played
            if hasattr(engine, "_seek_template"):
                engine._seek_template("bj_check_in")
            if isinstance(getattr(engine, "state", None), dict):
                engine.state["blackjack_checked_in"] = True
            text = atc_phrase.build_blackjack_continue(callsign)
            return _transmit(engine, airport, text, "blackjack")
        if intent == "range_exit" or match.template == "bj_range_exit":
            played = _play_step(engine, match)
            if played.get("action") != "none":
                return played
            text = atc_phrase.build_blackjack_range_exit(airport, callsign)
            return _transmit(engine, airport, text, "blackjack")
        # Approach check-in: METAR / route auto-assign recovery / IAF; hold cursor.
        if intent == "inbound_recovery" or match.template == "approach_check_in":
            return _approach_check_in(
                engine, airport, callsign, weather, match, opus=opus
            )
        # Continue to tower / cleared approach — play step and leave hold window.
        if intent == "approach_continue" or match.template == "cleared_approach":
            # After instrument missed, stay on Approach until outside the rearm
            # bubble — do not hand to Tower / land early near the field.
            import runway_position as rp

            dist_nm = None
            try:
                dist_nm = rp.ownship_distance_nm(
                    airport,
                    config=getattr(engine, "config", None),
                    callsign=callsign,
                    opus=opus,
                    state=engine.state,
                )
            except Exception:
                dist_nm = None
            ok_rearm, wait_rearm = atc_phrase.tower_land_gates_allowed(
                engine.state, dist_nm
            )
            if ok_rearm:
                try:
                    engine.save_state()
                except Exception:
                    pass
            if not ok_rearm:
                plan = atc_phrase.approach_plan_from_state(
                    engine.state, airport=airport
                )
                iaf = str(plan.get("iaf_say") or plan.get("iaf") or "the IAF")
                text = (
                    f"{atc_phrase.speak_callsign(callsign)}, "
                    f"{airport['name']} Approach, "
                    f"negative tower, continue to {iaf}."
                )
                return _transmit(engine, airport, text, "approach")
            # Same 12 NM field gate Watch uses — voice must not hand to Tower
            # at 35 NM just because the pilot said "continue".
            need_nm = float(rp.CONTACT_TOWER_WITHIN_NM)
            if dist_nm is None:
                text = (
                    f"{atc_phrase.speak_callsign(callsign)}, "
                    f"{airport['name']} Approach, "
                    f"negative tower, remain this frequency."
                )
                return _transmit(engine, airport, text, "approach")
            if dist_nm > need_nm:
                text = (
                    f"{atc_phrase.speak_callsign(callsign)}, "
                    f"{airport['name']} Approach, "
                    f"negative tower, continue inbound, "
                    f"contact Tower at {int(need_nm)} miles."
                )
                return _transmit(engine, airport, text, "approach")
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
        if intent == "request_landing" or match.template == "clear_land":
            if atc_phrase.landing_already_cleared(
                engine.state,
                opus=opus,
                step=engine.current_step() if hasattr(engine, "current_step") else None,
                mission=getattr(engine, "mission", None),
            ):
                return {"action": "none", "detail": "already cleared to land"}
            if atc_phrase.awaiting_option_on_the_go(engine.state):
                # Handled above; keep a safe fallback.
                if hasattr(engine, "accept_option_full_stop"):
                    detail = engine.accept_option_full_stop()
                    return {
                        "action": "play",
                        "text": detail.get("text"),
                        "channel": detail.get("channel") or "tower",
                        "detail": detail,
                        "option_full_stop": True,
                    }
            atc_phrase.set_landing_intent(
                atc_phrase.LANDING_INTENT_FULL_STOP,
                state=engine.state,
                mission=engine.mission,
            )
            engine.save_state()
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
    played = engine.play_id(step.get("id"))
    if isinstance(played, dict):
        out = dict(played)
        out.setdefault("action", "transmit")
        return out
    return {"action": "play", "detail": played}


def _acknowledge(engine: Any, match: voice_intent.Match) -> dict[str, Any]:
    """
    Pilot read the last clearance back.

    A clearance has a scripted "readback correct" reply to play; taxi, takeoff
    and landing clearances have none, so the readback just closes the window
    and Fly stops asking for it.
    """
    confirm = str(engine.state.get("awaiting_confirm_template") or "")
    last = str(engine.state.get("last_tx_template") or "")
    if last == "clearance_amendment":
        engine.state["clearance_amendment_copied"] = True
        if not confirm:
            confirm = "clearance"
    engine.clear_readback()
    if confirm:
        index = int(engine.state.get("index") or 0)
        step = _find_step(engine.steps, confirm, index)
        if step is not None:
            # Return the play_id payload at the top level so Host queueing and
            # LAST HEARD see the spoken text (not just the step label).
            played = engine.play_id(step.get("id"))
            if isinstance(played, dict):
                out = dict(played)
                out.setdefault("action", "transmit")
                return out
            return {"action": "play", "detail": played}
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


def _radar_channel(
    engine: Any, airport: dict[str, Any], match: voice_intent.Match
) -> str:
    """Agency to answer an altitude / vector request on."""
    ch = _resolve_tx_channel(engine, airport, match)
    return ch if ch in voice_intent._RADAR_CHANNELS else "control_east"


def _current_altitude_ft(engine: Any, callsign: str, opus: Any) -> int | None:
    """Where the jet is now: live radar, else whatever it was last assigned."""
    live = atc_phrase.ownship_altitude_ft(
        engine.config, callsign=callsign, opus=opus
    )
    if live is not None:
        return live
    state = getattr(engine, "state", None) or {}
    try:
        assigned = int(state.get("assigned_altitude_ft"))
    except (TypeError, ValueError):
        assigned = None
    if assigned:
        return assigned
    return atc_phrase.effective_filed_altitude_ft(opus, state)


def _handle_altitude_change(
    engine: Any,
    airport: dict[str, Any],
    callsign: str,
    match: voice_intent.Match,
    *,
    opus: Any = None,
) -> dict[str, Any]:
    """
    'Request elevator one four thousand' — climb or descent to what they ask.

    ATC answers in feet and flight levels, Blackjack and Bandsaw in angels.
    Anything outside the approvable band gets 'unable, maintain …'.
    """
    channel = _radar_channel(engine, airport, match)
    raw = (match.slots or {}).get("altitude_ft")
    try:
        want_ft = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        want_ft = None
    if want_ft is None:
        text = atc_phrase.build_altitude_request_unheard(
            callsign, agency=channel, airport=airport
        )
        return _transmit(engine, airport, text, channel)

    current_ft = _current_altitude_ft(engine, callsign, opus)
    low, high = atc_phrase.altitude_request_band_ft(
        engine.config, getattr(engine, "mission", None)
    )
    if not low <= want_ft <= high:
        text = atc_phrase.build_altitude_change_unable(
            callsign, agency=channel, current_ft=current_ft, airport=airport
        )
        return _transmit(engine, airport, text, channel)

    engine.state["assigned_altitude_ft"] = want_ft
    # Keep the cruise pipeline in step — amended is what later calls read.
    engine.state["amended_altitude_ft"] = want_ft
    engine.save_state()
    text = atc_phrase.build_altitude_change_clearance(
        callsign,
        agency=channel,
        altitude_ft=want_ft,
        current_ft=current_ft,
        airport=airport,
    )
    result = _transmit(engine, airport, text, channel)
    result["altitude_ft"] = want_ft
    return result


def _handle_navigation_request(
    intent: str,
    engine: Any,
    airport: dict[str, Any],
    callsign: str,
    match: voice_intent.Match,
    *,
    opus: Any = None,
) -> dict[str, Any]:
    """
    'Vectors to Stryk' / 'vectors to the nearest divert'.

    ATC gives a heading to fly; Blackjack and Bandsaw give bearing and range,
    since C2 does not vector anybody.
    """
    import agencies as agencies_mod
    import navaids

    channel = _radar_channel(engine, airport, match)
    advisory = agencies_mod.uses_bullseye(channel)
    position = atc_phrase.ownship_latlon(
        engine.config, callsign=callsign, opus=opus, state=engine.state
    )
    if position is None:
        text = atc_phrase.build_position_unknown(
            callsign, agency=channel, airport=airport
        )
        return _transmit(engine, airport, text, channel)

    if intent == "request_divert":
        field = navaids.nearest_divert(
            position[0], position[1], config=engine.config
        )
        if field is None:
            text = atc_phrase.build_point_unknown(
                callsign, agency=channel, airport=airport
            )
            return _transmit(engine, airport, text, channel)
        text = atc_phrase.build_divert_vector_clearance(
            callsign,
            agency=channel,
            field_say=str(field["say"]),
            bearing_deg=int(field["bearing"]),
            range_nm=int(field["range_nm"]),
            airport=airport,
            advisory=advisory,
        )
        result = _transmit(engine, airport, text, channel)
        result["divert"] = field["id"]
        return result

    # Re-resolve with the airport so approach fixes bring their spoken name
    # ("Stryk", not "STRYK"); the scored slot is the fallback.
    point = None
    phrase = voice_intent.extract_nav_point(match.normalized or "")
    if phrase:
        point = navaids.resolve_point(phrase, airport=airport)
    if not isinstance(point, dict):
        slot = (match.slots or {}).get("nav_point")
        point = slot if isinstance(slot, dict) else None
    if not isinstance(point, dict):
        text = atc_phrase.build_point_unknown(
            callsign, agency=channel, airport=airport
        )
        return _transmit(engine, airport, text, channel)

    bearing, range_nm = navaids.bearing_range_nm(
        position[0],
        position[1],
        float(point["lat"]),
        float(point["lon"]),
        config=engine.config,
    )
    say = str(point.get("say") or point.get("id") or "the fix")
    if advisory:
        text = atc_phrase.build_point_bearing_advisory(
            callsign,
            agency=channel,
            point_say=say,
            bearing_deg=bearing,
            range_nm=range_nm,
            airport=airport,
        )
    else:
        text = atc_phrase.build_point_vector_clearance(
            callsign,
            agency=channel,
            point_say=say,
            heading_deg=bearing,
            range_nm=range_nm,
            airport=airport,
        )
    result = _transmit(engine, airport, text, channel)
    result["nav_point"] = point.get("id")
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


def _rolling_offer_open(engine: Any) -> bool:
    """True while Tower is waiting on 'will you accept rolling?'."""
    if atc_phrase.pending_takeoff_offer(getattr(engine, "state", None)) == "rolling":
        return True
    last = str((getattr(engine, "state", None) or {}).get("last_tx_template") or "")
    return last == "rolling_accept"


def play_rolling_offer_reply(engine: Any, intent: str) -> dict[str, Any]:
    """
    After the rolling offer: accept → takeoff clearance; decline → LUAW.

    Unique Tower wording is applied via state['rolling_offer_reply'].
    """
    kind = str(intent or "").strip().casefold()
    played: dict[str, Any]
    if kind in ("accept_rolling", "request_rolling"):
        if isinstance(engine.state, dict):
            engine.state["rolling_offer_reply"] = "accept"
            engine.save_state()
        played = _play_clear_takeoff(engine)
    elif kind in ("deny_rolling", "request_lineup"):
        if isinstance(engine.state, dict):
            engine.state["rolling_offer_reply"] = "deny"
            engine.save_state()
        played = _play_lineup(engine)
    else:
        return {"action": "none", "detail": "not a rolling-offer reply"}
    if isinstance(engine.state, dict):
        engine.state.pop("rolling_offer_reply", None)
        engine.save_state()
    return played


def _play_lineup(engine: Any) -> dict[str, Any]:
    """Transmit the line-up-and-wait step (used after declining rolling)."""
    if hasattr(engine, "prepare_takeoff_cursor"):
        engine.prepare_takeoff_cursor()
    steps = engine.steps
    if not steps:
        return {"action": "none", "detail": "no enabled steps"}
    for step in steps:
        if str(step.get("template") or "") == "lineup":
            return {"action": "play", "detail": engine.play_id(step.get("id"))}
    return {"action": "none", "detail": "no lineup step"}


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
        if channel not in ("blackjack", "bandsaw", "joshua", "ops", "other"):
            channel = "blackjack"
        if channel == "blackjack" and intent in (
            "request_picture",
            "request_bogey_dope",
            "request_declare",
            "report_vid",
        ):
            if intent == "request_bogey_dope":
                what = "bogey dope"
            elif intent == "request_declare":
                what = "declare"
            elif intent == "report_vid":
                what = "visual ID"
            else:
                what = "picture"
            text = atc_phrase.build_blackjack_c2_redirect(airport, callsign, what)
            return _transmit(engine, airport, text, "blackjack")
        agency = atc_phrase.speak_agency_name(channel)
        cs = atc_phrase.speak_callsign(callsign)
        try:
            if intent == "request_bogey_dope":
                text, _groups = voice_actions.build_bogey_dope_reply(
                    engine.config,
                    airport,
                    callsign,
                    agency=agency,
                    opus=opus,
                    state=engine.state,
                )
            elif intent == "request_declare":
                text, _groups = voice_actions.build_declare_reply(
                    engine.config,
                    airport,
                    callsign,
                    agency=agency,
                    channel=channel,
                    opus=opus,
                    transcript=match.normalized or match.transcript or "",
                    state=engine.state,
                )
            elif intent == "report_vid":
                text, _groups = voice_actions.build_vid_affiliation_reply(
                    engine.config,
                    airport,
                    callsign,
                    agency=agency,
                    channel=channel,
                    opus=opus,
                    transcript=match.normalized or match.transcript or "",
                    state=engine.state,
                )
            else:
                text, _groups = voice_actions.build_picture_reply(
                    engine.config,
                    airport,
                    callsign,
                    agency=agency,
                    opus=opus,
                    state=engine.state,
                )
            if hasattr(engine, "save_state"):
                engine.save_state()
        except voice_actions.RadarUnavailable:
            if intent == "request_bogey_dope":
                what = "bogey dope"
            elif intent == "request_declare":
                what = "declare"
            elif intent == "report_vid":
                what = "visual ID"
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


def _transmit(
    engine: Any,
    airport: dict[str, Any],
    text: str,
    channel: str,
    *,
    freq_mhz: float | None = None,
    template: str = "",
) -> dict[str, Any]:
    srs_radio.apply_config(engine.config)
    remote = getattr(engine, "remote_radios", None)
    allowed, msg, _result = srs_radio.check_freq_gate(
        engine.config,
        airport,
        None,
        channel=channel,
        target_mhz=freq_mhz,
        radio=remote if isinstance(remote, srs_radio.RadioState) else None,
    )
    if not allowed:
        return {"action": "blocked", "detail": msg, "channel": channel}
    freq, mod, tx_name = atc_phrase.channel_radio(airport, channel)
    if freq_mhz is not None:
        try:
            freq = float(freq_mhz)
        except (TypeError, ValueError):
            pass
    elif str(channel or "").strip().lower() == "tanker":
        try:
            import tanker as tanker_mod

            live = tanker_mod.effective_tanker_mhz(
                getattr(engine, "state", None), getattr(engine, "config", None)
            )
            if live is not None:
                freq = float(live)
        except Exception:
            pass
    voice_name, _ = atc_phrase.voice_for_step(engine.config, channel, None)
    if hasattr(engine, "emit_radio"):
        code = engine.emit_radio(
            text=text,
            tx_name=tx_name,
            freq=freq,
            mod=mod,
            channel=channel,
            voice_override=voice_name,
        )
    else:
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
    st = getattr(engine, "state", None)
    if isinstance(st, dict) and str(text or "").strip():
        tmpl = str(template or "").strip()
        if getattr(engine, "defer_tx", False):
            # Do not paint LAST HEARD until ExternalAudio exits 0.
            # Stash agency + phrase for commit_deferred_tx_success.
            if tmpl:
                pending_tmpl = tmpl
            elif not st.get("awaiting_readback"):
                pending_tmpl = ""
            else:
                pending_tmpl = str(st.get("last_tx_template") or "")
            engine._deferred_agency = (str(channel or ""), pending_tmpl)
            engine._deferred_voice_tx = {
                "text": text,
                "channel": str(channel or ""),
                "template": pending_tmpl,
            }
        else:
            st["last_tx_text"] = text
            st["last_tx_channel"] = str(channel or "")
            if tmpl:
                st["last_tx_template"] = tmpl
            elif not st.get("awaiting_readback"):
                st["last_tx_template"] = ""
            atc_phrase.stamp_last_tx(st, text=text, deferred=False)
            try:
                import agencies as agencies_mod

                agencies_mod.note_tx(st, channel, str(st.get("last_tx_template") or ""))
            except Exception:
                pass
    return {
        "action": "transmit",
        "text": text,
        "channel": channel,
        "exit_code": code,
        "freq": freq,
    }


def _seek_delivery_after_ops(engine: Any) -> None:
    """
    After WORDS / start, park the cursor on Clearance Delivery.

    Ops is step 1 now — without this the timeline stays on Ops after the
    call and Fly keeps tipping Backup UHF instead of Delivery.
    """
    if not hasattr(engine, "state") or not isinstance(engine.state, dict):
        return
    sought = False
    if hasattr(engine, "_seek_template"):
        sought = bool(
            engine._seek_template("clearance")
            or engine._seek_template("clearance_amendment")
        )
    if not sought:
        steps = list(getattr(engine, "steps", None) or [])
        idx = int(engine.state.get("index") or 0)
        if 0 <= idx < len(steps):
            ch = str((steps[idx] or {}).get("channel") or "").strip().lower()
            if ch == "ops":
                engine.state["index"] = idx + 1
                sought = True
    if sought and hasattr(engine, "save_state"):
        try:
            engine.save_state()
        except Exception:
            pass


def execute_ops_action(
    engine: Any,
    action: str,
    *,
    match: voice_intent.Match | None = None,
    airport: dict[str, Any] | None = None,
    callsign: str = "",
    opus: Any = None,
) -> dict[str, Any]:
    """WORDS / start approval / postflight codes. Advances to Delivery after start."""
    import ops as ops_mod

    ap = airport if isinstance(airport, dict) else engine.airport()
    if not callsign:
        if opus is None:
            opus, _wx = atc_phrase.resolve_opus_and_metar(
                engine.config, ap.get("icao") if isinstance(ap, dict) else ""
            )
        if not opus:
            opus = atc_phrase.synthetic_flight_context(
                atc_phrase.callsign_override(engine.config) or "CALLSIGN"
            )
        callsign = opus.radio_callsign

    config = getattr(engine, "config", None)
    state = getattr(engine, "state", None)
    if not isinstance(state, dict):
        state = {}
        try:
            engine.state = state
        except Exception:
            pass
    when = ops_mod.resolve_ops_clock(config)
    words = ops_mod.current_words(config, opus=opus, when=when)
    transcript = ""
    if match is not None:
        transcript = str(match.transcript or match.normalized or "")

    if action == "ops_check_in":
        text = ops_mod.build_ops_check_in(
            callsign, ap, opus=opus, config=config
        )
        return _transmit(engine, ap, text, "ops")

    existing = ops_mod.sortie_from_state(state)
    already = bool(existing and existing.start_utc)

    if action == "ops_request_words":
        if not already:
            ops_mod.approve_start(
                state,
                config=config,
                opus=opus,
                callsign=callsign,
                words=words,
                when=when,
            )
        elif words and existing and not existing.words_id:
            existing.words_id = words.id
            ops_mod.write_sortie(state, existing)
        text = ops_mod.build_words_reply(
            callsign,
            words,
            airport=ap,
            start=True,
            when=when,
            already_started=already,
            opus=opus,
            config=config,
        )
        if hasattr(engine, "save_state"):
            engine.save_state()
        result = _transmit(engine, ap, text, "ops", template="ops_words")
        _seek_delivery_after_ops(engine)
        return result

    if action == "ops_request_start":
        if not already:
            ops_mod.approve_start(
                state,
                config=config,
                opus=opus,
                callsign=callsign,
                words=words,
                when=when,
            )
        text = ops_mod.build_start_reply(
            callsign,
            airport=ap,
            words=words,
            when=when,
            already_started=already,
            opus=opus,
            config=config,
        )
        if hasattr(engine, "save_state"):
            engine.save_state()
        result = _transmit(engine, ap, text, "ops", template="ops_start")
        _seek_delivery_after_ops(engine)
        return result

    if action == "ops_status":
        codes = ops_mod.parse_aircraft_codes(transcript, flight_callsign=callsign)
        if not codes:
            return {
                "action": "none",
                "detail": "no aircraft codes heard",
            }
        merged = ops_mod.merge_pending_codes(state, codes)
        ready, pending, why = ops_mod.codes_collection_ready(state, opus=opus)
        if not ready:
            ack = ops_mod.build_codes_copy_ack(
                callsign, airport=ap, opus=opus, config=config
            )
            result = _transmit(engine, ap, ack, "ops", template="ops_status")
            result["deferred"] = {
                "kind": "ops_codes",
                "delay_s": float(ops_mod.CODES_IDLE_S),
                "detail": why,
            }
            if hasattr(engine, "save_state"):
                engine.save_state()
            return result
        sortie = ops_mod.record_status(
            state,
            pending or merged,
            config=config,
            opus=opus,
            callsign=callsign,
            when=when,
        )
        ops_mod.mark_codes_done(state)
        text = ops_mod.build_status_reply(
            callsign,
            sortie,
            airport=ap,
            when=when,
            opus=opus,
            config=config,
        )
        if hasattr(engine, "save_state"):
            engine.save_state()
        return _transmit(engine, ap, text, "ops", template="ops_status")

    if action == "ops_codes_finalize":
        ready, pending, why = ops_mod.codes_collection_ready(state, opus=opus)
        if not ready or not pending:
            result = {"action": "none", "detail": why or "waiting for codes"}
            if pending and not state.get(ops_mod._CODES_DONE_KEY):
                result["deferred"] = {
                    "kind": "ops_codes",
                    "delay_s": float(ops_mod.CODES_IDLE_S),
                    "detail": why,
                }
            return result
        sortie = ops_mod.record_status(
            state,
            pending,
            config=config,
            opus=opus,
            callsign=callsign,
            when=when,
        )
        ops_mod.mark_codes_done(state)
        text = ops_mod.build_status_reply(
            callsign,
            sortie,
            airport=ap,
            when=when,
            opus=opus,
            config=config,
        )
        if hasattr(engine, "save_state"):
            engine.save_state()
        return _transmit(engine, ap, text, "ops", template="ops_status")

    return {"action": "none", "detail": f"unknown OPS action {action}"}


def execute_tanker_action(
    engine: Any,
    action: str,
    *,
    match: voice_intent.Match | None = None,
    airport: dict[str, Any] | None = None,
    callsign: str = "",
    opus: Any = None,
) -> dict[str, Any]:
    """C2 vectors + missing join calls. DCS radio keeps cleared contact."""
    import tanker as tanker_mod
    import tanker_chat as tanker_chat_mod

    ap = airport if isinstance(airport, dict) else engine.airport()
    if not callsign:
        if opus is None:
            opus, _wx = atc_phrase.resolve_opus_and_metar(
                engine.config, ap.get("icao") if isinstance(ap, dict) else ""
            )
        if not opus:
            opus = atc_phrase.synthetic_flight_context(
                atc_phrase.callsign_override(engine.config) or "CALLSIGN"
            )
        callsign = opus.radio_callsign

    def _c2_channel() -> str:
        channel = "blackjack"
        if match is not None:
            channel = _resolve_tx_channel(engine, ap, match) or channel
        else:
            remote = getattr(engine, "remote_radios", None)
            tuned = srs_radio.channel_for_tuned_freq(
                ap,
                engine.config,
                state=remote if isinstance(remote, srs_radio.RadioState) else None,
            )
            if tuned:
                channel = tuned
        if channel not in (
            "blackjack",
            "bandsaw",
            "joshua",
            "ops",
            "control_east",
            "control_west",
        ):
            return "blackjack"
        return channel

    if action == "tanker_return":
        channel = _c2_channel()
        alpha_spoken = None
        fix = atc_phrase.resolve_alpha_bullseye(
            engine.config,
            callsign=callsign,
            opus=opus,
            weather=weather,
            state=getattr(engine, "state", None),
        )
        if fix and fix.get("spoken"):
            alpha_spoken = str(fix["spoken"])
        tanker_mod.leave_tanker_overlay(engine, channel, checkin=True)
        tanker_chat_mod.end_chat(engine.state)
        text = tanker_mod.build_tanker_return_checkin(
            channel, callsign, alpha_bullseye=alpha_spoken
        )
        if hasattr(engine, "save_state"):
            engine.save_state()
        return _transmit(engine, ap, text, channel)

    if (
        action in ("tanker_chat_start", "tanker_chat_reply", "tanker_chat_stop")
        or str(action).startswith("tanker_chat_choice_")
    ):
        named = ""
        own_ll = atc_phrase.ownship_latlon(
            engine.config, callsign=callsign, opus=opus, state=engine.state
        )
        tanker = tanker_mod.pick_tanker(
            engine.config,
            opus=opus,
            state=engine.state,
            name=named or None,
            own_ll=own_ll,
            boom_only=True,
        )
        if tanker:
            tanker_mod.remember_tanker(
                engine.state, tanker, config=getattr(engine, "config", None), opus=opus
            )
        text = ""
        schedule_break = False
        if action == "tanker_chat_stop":
            text = tanker_chat_mod.stop_chat(engine.state, callsign, tanker)
        elif action == "tanker_chat_start":
            text = tanker_chat_mod.start_chat(
                engine.state,
                callsign,
                tanker,
                config=getattr(engine, "config", None),
            )
            schedule_break = True
        elif action == "tanker_chat_reply" or str(action).startswith(
            "tanker_chat_choice_"
        ):
            cid = ""
            transcript = ""
            if str(action).startswith("tanker_chat_choice_"):
                cid = str(action)[len("tanker_chat_choice_") :]
            elif match is not None:
                cid = str((match.slots or {}).get("choice") or "")
                transcript = str(match.normalized or match.transcript or "")
            # Stop phrases win even while an A/B is outstanding.
            if transcript and tanker_chat_mod.match_stop(transcript):
                text = tanker_chat_mod.stop_chat(engine.state, callsign, tanker)
            else:
                text = tanker_chat_mod.answer_chat(
                    engine.state,
                    callsign,
                    tanker,
                    choice_id=cid,
                    transcript=transcript,
                    config=getattr(engine, "config", None),
                )
                if not text:
                    return {
                        "action": "none",
                        "detail": "no matching tanker chat reply",
                    }
                schedule_break = True
        freq = tanker_mod.tanker_target_mhz(engine.state)
        if freq is None and tanker:
            try:
                freq = float(tanker.get("freq_mhz") or 0) or None
            except (TypeError, ValueError):
                freq = None
        if hasattr(engine, "save_state"):
            engine.save_state()
        result = _transmit(engine, ap, text, "tanker", freq_mhz=freq)
        tanker_chat_mod.arm_tx_guard(engine.state, text)
        if hasattr(engine, "save_state"):
            engine.save_state()
        if schedule_break:
            result = tanker_chat_mod.attach_continuation_deferred(
                result if isinstance(result, dict) else {"action": "transmit"},
                engine.state,
            )
        return result

    named = ""
    transcript = ""
    if match is not None:
        transcript = str(match.normalized or match.transcript or "")
        named = tanker_mod.extract_tanker_name(transcript) or str(
            (match.slots or {}).get("tanker") or ""
        )
    own_ll = atc_phrase.ownship_latlon(
        engine.config, callsign=callsign, opus=opus, state=engine.state
    )
    # F-16 C2 always wants the KC-135 boom. Named ARCO / MPRS is skipped.
    boom_only = action == "request_tanker" or action in tanker_mod.C2_TANKER_INFO_ACTIONS
    prefer_remembered = True
    exclude = None
    pick_name = named or None
    if action == "request_tanker":
        # A new tanker request re-picks nearest. TACAN / freq / bullseye stay
        # on the assigned bird unless they named a different one.
        prefer_remembered = False
        if not tanker_mod.tanker_name_is_specific(named):
            pick_name = None
            if tanker_mod.wants_reassign_tanker(transcript) and isinstance(
                engine.state, dict
            ):
                exclude = str(
                    engine.state.get("tanker_callsign")
                    or engine.state.get("tanker_id")
                    or ""
                ) or None
    tanker = tanker_mod.pick_tanker(
        engine.config,
        opus=opus,
        state=engine.state,
        name=pick_name,
        own_ll=own_ll,
        boom_only=boom_only,
        prefer_remembered=prefer_remembered,
        exclude=exclude,
    )
    if tanker:
        tanker_mod.remember_tanker(
            engine.state, tanker, config=engine.config, opus=opus
        )
        if hasattr(engine, "save_state"):
            engine.save_state()

    if action == "request_tanker" or action in tanker_mod.C2_TANKER_INFO_ACTIONS:
        channel = _c2_channel()
        builders = {
            "request_tanker": tanker_mod.build_c2_tanker_vectors,
            "tanker_tacan": tanker_mod.build_tanker_tacan_reply,
            "tanker_freq": tanker_mod.build_tanker_freq_reply,
            "tanker_bullseye": tanker_mod.build_tanker_bullseye_reply,
        }
        text = builders[action](channel, callsign, tanker)
        if action == "request_tanker" and tanker:
            tanker_mod.apply_tanker_phase(engine.state, tanker_mod.PHASE_JOIN)
            tanker_mod.enter_tanker_overlay(engine)
            if hasattr(engine, "save_state"):
                engine.save_state()
        return _transmit(engine, ap, text, channel)

    if action in tanker_mod.DCS_TANKER_ACTIONS:
        tanker_mod.apply_tanker_phase(
            engine.state,
            {
                "tanker_astern": tanker_mod.PHASE_ASTERN,
                "tanker_observation": tanker_mod.PHASE_ASTERN,
                "tanker_contact": tanker_mod.PHASE_CONTACT,
                "tanker_dcs_precontact": tanker_mod.PHASE_ASTERN,
                "tanker_disconnect": tanker_mod.PHASE_RIGHT,
                "tanker_depart": tanker_mod.PHASE_DEPARTED,
                "tanker_dcs_abort": tanker_mod.PHASE_RIGHT,
            }.get(action, tanker_mod.PHASE_ASTERN),
        )
        if action in (
            "tanker_disconnect",
            "tanker_depart",
            "tanker_dcs_abort",
        ):
            tanker_chat_mod.end_chat(engine.state)
        if hasattr(engine, "save_state"):
            engine.save_state()
        if action == "tanker_depart":
            text = tanker_mod.build_tanker_depart_reply(callsign, tanker)
            freq = tanker_mod.tanker_target_mhz(engine.state)
            if freq is None and tanker:
                try:
                    freq = float(tanker.get("freq_mhz") or 0) or None
                except (TypeError, ValueError):
                    freq = None
            result = _transmit(engine, ap, text, "tanker", freq_mhz=freq)
            result["hint"] = tanker_mod.dcs_tanker_radio_hint(action)
            result["detail"] = result.get("hint")
            return result
        hint = tanker_mod.dcs_tanker_radio_hint(action)
        return {
            "action": "hint",
            "detail": hint,
            "channel": "tanker",
            "text": hint,
        }

    tanker_mod.apply_tanker_phase(
        engine.state,
        {
            "tanker_check_in": tanker_mod.PHASE_JOIN,
        }.get(action, tanker_mod.PHASE_JOIN),
    )
    if action == "tanker_check_in":
        tanker_mod.mark_rejoined(engine.state, True)
    builders = {
        "tanker_check_in": tanker_mod.build_tanker_check_in,
    }
    build = builders.get(action)
    if build is None:
        return {"action": "none", "detail": f"unknown tanker action {action}"}
    text = build(callsign, tanker)
    freq = tanker_mod.tanker_target_mhz(engine.state)
    if freq is None and tanker:
        try:
            freq = float(tanker.get("freq_mhz") or 0) or None
        except (TypeError, ValueError):
            freq = None
    if hasattr(engine, "save_state"):
        engine.save_state()
    return _transmit(engine, ap, text, "tanker", freq_mhz=freq)


def speak_tanker_line(engine: Any, text: str) -> dict[str, Any]:
    """TX a boom-chat line the Client already wrote (Ollama on the flying PC)."""
    import tanker as tanker_mod
    import tanker_chat as tanker_chat_mod

    spoken = " ".join(str(text or "").split()).strip()
    if not spoken:
        return {"action": "none", "detail": "empty tanker line"}
    ap = engine.airport()
    freq = tanker_mod.tanker_target_mhz(engine.state)
    tanker_chat_mod.append_history(engine.state, "boom", spoken)
    tanker_chat_mod.arm_tx_guard(engine.state, spoken)
    if hasattr(engine, "save_state"):
        engine.save_state()
    result = _transmit(engine, ap, spoken, "tanker", freq_mhz=freq)
    if hasattr(engine, "save_state"):
        engine.save_state()
    return tanker_chat_mod.attach_continuation_deferred(
        result if isinstance(result, dict) else {"action": "transmit", "text": spoken},
        engine.state,
    )


def resolve_tanker_chat(
    engine: Any, *, force: bool = False, continue_session: bool = False, transmit: bool = True
) -> dict[str, Any]:
    """
    Texaco starts (or continues) boom small talk.

    Auto first-open requires 0.1–0.5 NM after rejoin. Continuations inside an
    active session skip the range dwell — the chat already earned its seat.
    """
    import tanker as tanker_mod
    import tanker_chat as tanker_chat_mod

    if tanker_chat_mod.current_choices(engine.state):
        return {"action": "none", "detail": "tanker chat already open"}
    if (
        tanker_chat_mod.is_awaiting_react(engine.state)
        and not continue_session
        and not force
    ):
        delay = tanker_chat_mod.continuation_delay_s(engine.state)
        if delay is not None:
            return {
                "action": "none",
                "detail": f"chatting — next bit in {delay:.0f}s",
                "deferred": {
                    "kind": "tanker_chat_continue",
                    "delay_s": max(1.0, delay),
                },
            }
        return {"action": "none", "detail": "waiting on boom chat reply"}
    if str((engine.state or {}).get("tanker_phase") or "") == tanker_mod.PHASE_DEPARTED:
        tanker_chat_mod.end_chat(engine.state)
        if hasattr(engine, "save_state"):
            engine.save_state()
        return {"action": "none", "detail": "not on the tanker"}
    session = tanker_chat_mod.is_session_active(engine.state)
    continuing = continue_session or (
        session and tanker_chat_mod.continuation_due(engine.state)
    )
    if continuing and not session:
        return {"action": "none", "detail": "no tanker chat session"}
    if not force and not continuing and not tanker_mod.has_rejoined(engine.state):
        return {"action": "none", "detail": "waiting — request rejoin first"}
    if session and not continuing and not force:
        delay = tanker_chat_mod.continuation_delay_s(engine.state)
        if delay is not None:
            return {
                "action": "none",
                "detail": f"chatting — next bit in {delay:.0f}s",
                "deferred": {
                    "kind": "tanker_chat_continue",
                    "delay_s": max(1.0, delay),
                },
            }
    ap = engine.airport()
    opus, _wx = atc_phrase.resolve_opus_and_metar(
        engine.config, ap.get("icao") if isinstance(ap, dict) else ""
    )
    if not opus:
        opus = atc_phrase.synthetic_flight_context(
            atc_phrase.callsign_override(engine.config) or "CALLSIGN"
        )
    callsign = opus.radio_callsign
    own_ll = atc_phrase.ownship_latlon(
        engine.config, callsign=callsign, opus=opus, state=engine.state
    )
    tanker = tanker_mod.pick_tanker(
        engine.config,
        opus=opus,
        state=engine.state,
        own_ll=own_ll,
        boom_only=True,
    )
    if tanker:
        tanker_mod.remember_tanker(
            engine.state, tanker, config=getattr(engine, "config", None), opus=opus
        )
    gate: dict[str, Any] | None = None
    if not force and not continuing:
        dist = None
        if tanker and tanker.get("distance_nm") is not None:
            try:
                dist = float(tanker.get("distance_nm"))
            except (TypeError, ValueError):
                dist = None
        receivers = 0
        if tanker:
            receivers = tanker_mod.count_boom_receivers(
                engine.config, tanker, own_ll=own_ll, opus=opus
            )
        gate = tanker_mod.tick_boom_chat(
            engine.state,
            dist_nm=dist,
            receivers=receivers,
            chat_open=tanker_chat_mod.is_session_active(engine.state),
        )
        if not gate.get("ready"):
            if hasattr(engine, "save_state"):
                engine.save_state()
            return {
                "action": "none",
                "detail": str(gate.get("reason") or "not in boom range"),
                "boom": gate,
            }
    text = tanker_chat_mod.start_chat(
        engine.state,
        callsign,
        tanker,
        config=getattr(engine, "config", None),
    )
    if not force and not continuing:
        engine.state["tanker_chat_auto_done"] = True
    freq = tanker_mod.tanker_target_mhz(engine.state)
    if freq is None and tanker:
        try:
            freq = float(tanker.get("freq_mhz") or 0) or None
        except (TypeError, ValueError):
            freq = None
    if hasattr(engine, "save_state"):
        engine.save_state()
    if not transmit:
        tanker_chat_mod.arm_tx_guard(engine.state, text)
        if hasattr(engine, "save_state"):
            engine.save_state()
        result = {
            "action": "transmit",
            "text": text,
            "channel": "tanker",
            "detail": "client-generated, host will TX",
        }
        if gate is not None:
            result["boom"] = gate
        return tanker_chat_mod.attach_continuation_deferred(result, engine.state)
    result = _transmit(engine, ap, text, "tanker", freq_mhz=freq)
    tanker_chat_mod.arm_tx_guard(engine.state, text)
    if hasattr(engine, "save_state"):
        engine.save_state()
    if isinstance(result, dict) and gate is not None:
        result["boom"] = gate
    result = tanker_chat_mod.attach_continuation_deferred(
        result if isinstance(result, dict) else {"action": "transmit"},
        engine.state,
    )
    return result


def _advance_past_bandsaw(engine: Any) -> None:
    """Move the cursor past consecutive optional C2 steps (Bandsaw / Joshua)."""
    steps = list(engine.steps or [])
    idx = int(engine.state.get("index") or 0)
    skip_ch = frozenset({"bandsaw", "joshua"})
    while idx < len(steps):
        step = steps[idx]
        ch = str(step.get("channel") or "").lower()
        tmpl = str(step.get("template") or "")
        if ch in skip_ch or (
            (tmpl.startswith("bandsaw_") or tmpl.startswith("joshua_"))
            and not voice_intent.step_is_authored(step)
        ):
            idx += 1
            continue
        break
    # Checkout hands back to Blackjack range exit. Check-in holds the cursor
    # for the whole range, so stopping on the next non-C2 step left Fly on
    # "Blackjack check-in" (or Center) instead of range exit.
    exit_i = next(
        (
            i
            for i, row in enumerate(steps)
            if str(row.get("template") or "") == "bj_range_exit"
        ),
        None,
    )
    if exit_i is not None and idx < exit_i:
        idx = exit_i
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
    # Approach check-in closes the Tower missed-approach readback so Watch
    # can arm IAF / tower gates on the way back in.
    if atc_phrase.go_around_readback_open(engine.state) and hasattr(
        engine, "clear_readback"
    ):
        engine.clear_readback()
    # Prefer playing the flow step so last_tx / readback state stay consistent.
    played = _play_step(engine, match)
    if played.get("action") != "none":
        return played
    ll = atc_phrase.ownship_latlon(
        engine.config, callsign=callsign, opus=opus, state=engine.state
    )
    text = atc_phrase.build_approach_recovery(
        airport,
        callsign,
        weather,
        str(plan.get("runway") or ""),
        recovery=str(plan.get("pattern") or ""),
        plan=plan,
        distance_nm=atc_phrase.field_distance_nm(airport, ll),
        ownship_ll=ll,
        config=engine.config,
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
        recovery = atc_phrase.normalize_recovery_key(
            slots.get("recovery") or plan.get("pattern")
        )
        addressed = str(slots.get("channel") or "").strip().lower()
        step_ch = str((engine.current_step() or {}).get("channel") or "").strip().lower()
        vfr_ok = recovery in (
            "visual_overhead",
            "tactical_overhead",
            "straight_in",
            "sfo_overhead",
            "sfo_straight_in",
        )
        to_tower = vfr_ok and (
            addressed == "tower" or (not addressed and step_ch == "tower")
        )
        if to_tower and recovery in atc_phrase.SFO_RECOVERIES:
            plan_sfo = atc_phrase.assign_sfo_plan(
                airport,
                recovery=recovery,
                high_key_ft=None,
                runway=str(plan.get("runway") or ""),
                mission=engine.mission,
                state=engine.state,
            )
            engine.save_state()
            text = atc_phrase.build_sfo_approved(
                airport,
                callsign,
                runway=str(plan_sfo.get("runway") or plan.get("runway") or ""),
                recovery=recovery,
                high_key_ft=plan_sfo.get("sfo_high_key_ft"),
            )
            return _transmit(
                engine, airport, text, "tower", template="sfo_approve"
            )
        if to_tower:
            text = atc_phrase.build_tower_check_in(
                airport,
                callsign,
                str(plan.get("runway") or ""),
                plan=plan,
            )
            return _transmit(engine, airport, text, "tower")
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
        engine.state["approach_clearance_need_fix"] = True
        if atc_phrase.go_around_readback_open(engine.state) and hasattr(
            engine, "clear_readback"
        ):
            engine.clear_readback()
        step = engine.current_step() if hasattr(engine, "current_step") else None
        tmpl = str((step or {}).get("template") or "")
        if tmpl == "approach_check_in" and hasattr(engine, "_seek_template"):
            engine._seek_template("approach_procedure")
        engine.save_state()
        text = atc_phrase.build_vector_clearance(
            airport, callsign, plan=plan
        )
        return _transmit(engine, airport, text, "approach")

    return {"action": "none", "detail": f"unhandled approach action {intent}"}


def _sfo_should_clear_now(engine: Any) -> bool:
    st = getattr(engine, "state", None)
    if not atc_phrase.is_sfo_recovery(state=st):
        return False
    phase = atc_phrase.sfo_phase(st)
    return phase in ("low_key", "base_key", "sfo_final") or bool(
        isinstance(st, dict) and st.get("sfo_base_key_pending")
    )


def _pattern_option_clear_due(engine: Any) -> bool:
    """True after VFR go-around when Tower is waiting to clear on base/final."""
    st = getattr(engine, "state", None)
    if not isinstance(st, dict):
        return False
    if atc_phrase.landing_already_cleared(st):
        return False
    return bool(
        atc_phrase.closed_traffic_go_around_pending(st)
        or atc_phrase.reentry_go_around_pending(st)
    )


def _play_sfo_or_pattern_clear_land(engine: Any) -> dict[str, Any]:
    played = _play_template(engine, "clear_land")
    if played.get("action") != "none":
        return played
    # No flow step — build the clearance ad-hoc.
    try:
        airport = engine.airport()
    except Exception:
        return {"action": "none", "detail": "no airport"}
    callsign = atc_phrase.callsign_override(engine.config) or "CALLSIGN"
    opus, weather = atc_phrase.resolve_opus_and_metar(
        engine.config, airport.get("icao") or ""
    )
    if not opus:
        opus = atc_phrase.synthetic_flight_context(callsign)
    rwy = str(
        (engine.state or {}).get("approach_runway")
        or (atc_phrase.approach_plan_from_state(engine.state, airport=airport) or {}).get(
            "runway"
        )
        or ""
    )
    text = atc_phrase.build_clear_land(
        airport,
        callsign,
        weather,
        rwy,
        opus=opus,
        mission=getattr(engine, "mission", None),
        state=engine.state,
    )
    return _transmit(engine, airport, text, "tower", template="clear_land")


def _handle_sfo_action(
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
    st = engine.state if isinstance(engine.state, dict) else {}

    if intent == "request_sfo":
        recovery = atc_phrase.normalize_recovery_key(
            slots.get("recovery") or "sfo_overhead", default="sfo_overhead"
        )
        if recovery not in atc_phrase.SFO_RECOVERIES:
            recovery = "sfo_overhead"
        high_ft = slots.get("high_key_ft")
        try:
            high_n = int(high_ft) if high_ft is not None else None
        except (TypeError, ValueError):
            high_n = None
        plan = atc_phrase.assign_sfo_plan(
            airport,
            recovery=recovery,
            high_key_ft=high_n,
            mission=engine.mission,
            state=st,
        )
        # Seek clear_land so Play / Low Key can fire the option.
        if hasattr(engine, "_seek_template"):
            engine._seek_template("clear_land")
        engine.save_state()
        text = atc_phrase.build_sfo_approved(
            airport,
            callsign,
            runway=str(plan.get("runway") or ""),
            recovery=recovery,
            high_key_ft=plan.get("sfo_high_key_ft"),
        )
        return _transmit(engine, airport, text, "tower", template="sfo_approve")

    if intent == "report_high_key":
        ga = st.get("go_around_plan") if isinstance(st, dict) else None
        continuing = isinstance(ga, dict) and str(ga.get("kind") or "") == "sfo_continue"
        if not atc_phrase.is_sfo_recovery(state=st) and not continuing:
            # Treat as a late request.
            return _handle_sfo_action(
                "request_sfo",
                engine,
                airport,
                callsign,
                weather,
                match,
                opus=opus,
            )
        if continuing and not atc_phrase.is_sfo_recovery(state=st):
            atc_phrase.note_sfo_continue_after_go_around(
                st, mission=engine.mission
            )
        heard = slots.get("high_key_ft")
        if heard is None:
            # Optional altitude in the report itself.
            try:
                import voice_intent as vi

                alts = vi._heard_altitudes_ft(vi.normalize(match.transcript or ""))
                if alts:
                    heard = alts[-1]
            except Exception:
                heard = None
        alt = atc_phrase.normalize_sfo_high_key_ft(heard)
        if alt is not None:
            st["sfo_high_key_ft"] = alt
            plan = dict(st.get("approach_plan") or {})
            if plan:
                plan["sfo_high_key_ft"] = alt
                st["approach_plan"] = plan
        atc_phrase.note_sfo_high_key(st)
        engine.save_state()
        text = atc_phrase.build_sfo_high_key_ack(airport, callsign)
        return _transmit(engine, airport, text, "tower", template="sfo_high_key")

    landing = str(slots.get("landing_intent") or "").strip().casefold()
    if landing == "full_stop":
        atc_phrase.set_landing_intent(
            atc_phrase.LANDING_INTENT_FULL_STOP,
            state=st,
            mission=engine.mission,
        )
    elif landing == "low_approach" or (
        intent in ("report_low_key", "report_base_key", "report_sfo_final")
        and not landing
    ):
        # Default at Low Key / SI final: low approach (7110.65 3-10-13).
        atc_phrase.set_landing_intent(
            atc_phrase.LANDING_INTENT_LOW_APPROACH,
            state=st,
            mission=engine.mission,
        )

    if intent == "request_closed_traffic":
        rwy = str(
            st.get("approach_runway")
            or (atc_phrase.approach_plan_from_state(st, airport=airport) or {}).get(
                "runway"
            )
            or ""
        )
        atc_phrase.exit_sfo_for_closed_traffic(
            airport,
            mission=engine.mission,
            state=st,
            runway=rwy,
        )
        if hasattr(engine, "_seek_template"):
            engine._seek_template("clear_land")
        engine.save_state()
        text = atc_phrase.build_closed_traffic_approved(
            airport, callsign, runway=rwy
        )
        return _transmit(engine, airport, text, "tower", template="closed_traffic")

    if intent == "report_low_key":
        if not atc_phrase.is_sfo_recovery(state=st):
            atc_phrase.note_sfo_continue_after_go_around(
                st, mission=engine.mission
            )
        atc_phrase.set_landing_intent(
            atc_phrase.LANDING_INTENT_LOW_APPROACH,
            state=st,
            mission=engine.mission,
        )
        atc_phrase.note_sfo_low_key(st)
        engine.save_state()
        played = _play_sfo_or_pattern_clear_land(engine)
        if played.get("action") != "none":
            atc_phrase.note_sfo_cleared(st)
            engine.save_state()
            return played
        text = atc_phrase.build_sfo_low_key_ack(airport, callsign)
        return _transmit(engine, airport, text, "tower", template="sfo_low_key")

    if intent == "report_sfo_final":
        atc_phrase.note_sfo_final(st)
        engine.save_state()
        played = _play_sfo_or_pattern_clear_land(engine)
        atc_phrase.note_sfo_cleared(st)
        engine.save_state()
        return played

    if intent == "report_base_key":
        need_clear = atc_phrase.note_sfo_base_key(st)
        engine.save_state()
        if need_clear:
            played = _play_sfo_or_pattern_clear_land(engine)
            atc_phrase.note_sfo_cleared(st)
            engine.save_state()
            return played
        # Already cleared at Low Key — roger only.
        text = f"{atc_phrase.speak_callsign(callsign)}, roger."
        return _transmit(engine, airport, text, "tower", template="sfo_base_key")

    return {"action": "none", "detail": f"unhandled sfo action {intent}"}


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
    intent = str(match.intent or "") if match is not None else ""
    if intent.startswith("tanker_") and intent not in (
        "tanker_tacan",
        "tanker_freq",
        "tanker_bullseye",
    ):
        return "tanker"
    if intent.startswith("ops_"):
        return "ops"
    remote = getattr(engine, "remote_radios", None)
    tuned = srs_radio.channel_for_tuned_freq(
        airport,
        engine.config,
        state=remote if isinstance(remote, srs_radio.RadioState) else None,
    )
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
    "Ready for departure" answers LUAW (or "will you accept rolling?").

    Cleared takeoff is not voice-advanced by this call — that fires from the
    in-position runway zone, Play / Next, or the "in position" fallback.
    """
    del match  # intent routing only; play uses the flow cursor
    if hasattr(engine, "prepare_takeoff_cursor"):
        engine.prepare_takeoff_cursor()
    steps = engine.steps
    if not steps:
        return {"action": "none", "detail": "no enabled steps"}
    ready_tmpls = frozenset({"lineup", "rolling_accept"})
    index = int(engine.state.get("index") or 0)
    current = steps[index] if 0 <= index < len(steps) else None
    if current and str(current.get("template") or "") in ready_tmpls:
        if str(current.get("template") or "") == "lineup" and atc_phrase.should_skip_takeoff_step(
            current, engine.mission, engine.state
        ):
            pass
        else:
            return {"action": "play", "detail": engine.play_id(current.get("id"))}
    for i in range(max(0, index), len(steps)):
        step = steps[i]
        tmpl = str(step.get("template") or "")
        if tmpl not in ready_tmpls:
            continue
        if tmpl == "lineup" and atc_phrase.should_skip_takeoff_step(
            step, engine.mission, engine.state
        ):
            continue
        return {"action": "play", "detail": engine.play_id(step.get("id"))}
    return {"action": "none", "detail": "no line-up step ahead"}


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
    here = str((match.slots or {}).get("channel") or "").strip().lower()
    if not here:
        try:
            import srs_radio as srs_radio_mod

            radio = getattr(engine, "remote_radios", None)
            here = str(
                srs_radio_mod.channel_for_tuned_freq(
                    engine.airport(),
                    getattr(engine, "config", None),
                    state=radio if radio is not None else None,
                )
                or ""
            ).strip().lower()
        except Exception:
            here = ""
    tmpl = str(match.template or "").strip().lower()
    if here == "ground" and tmpl in (
        "lineup",
        "line_up_and_wait",
        "clear_takeoff",
        "clear_takeoff_rolling",
        "clear_takeoff_intersection",
        "rolling_accept",
        "right_break",
        "clear_land",
    ):
        return {
            "action": "none",
            "detail": "still on Ground — tune Tower before those calls",
        }

    # A phrase written on a step names that step outright — no template search.
    if match.step_id:
        if any(s.get("id") == match.step_id for s in steps):
            return {
                "action": "play",
                "detail": engine.play_id(match.step_id, force_advance=True),
            }
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
