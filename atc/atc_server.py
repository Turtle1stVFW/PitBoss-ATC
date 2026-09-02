"""
Multi-pilot ATC host: per-flight shared timeline + per-seat radios.

Binds LAN HTTP (default :8766). Clients STT locally and POST intents here.
Same Opus flight_id shares one FlowEngine cursor; each seat keeps its own
connection, identity, and freq-gate radios.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atc_net  # noqa: E402
import atc_phrase  # noqa: E402
import channel_tx  # noqa: E402
import flow_engine  # noqa: E402
import runway_position  # noqa: E402
import srs_radio  # noqa: E402
import tanker  # noqa: E402
import voice_engine  # noqa: E402
import voice_intent  # noqa: E402

MAX_BODY = 256_000

# Cursor the whole flight should see. Tanker AAR lives on PilotSession.local_state.
_SHARED_FLOW_KEYS = (
    "index",
    "last_step_id",
    "awaiting_readback",
    "readback_items",
    "last_tx_text",
    "last_tx_at",
    "last_tx_channel",
    "last_tx_template",
    "active_takeoff_mode",
    "pending_takeoff_offer",
    "takeoff_offer_rolled",
)


class PilotSession:
    def __init__(
        self,
        session_id: str,
        engine: flow_engine.FlowEngine,
        identity: dict[str, Any],
    ) -> None:
        self.session_id = session_id
        self.flow_key = session_id
        self.engine = engine
        self.identity = dict(identity)
        self.lock = threading.RLock()
        self.last_seen = time.time()
        self.tuned_freqs_mhz: list[float] = []
        self.radio_fresh = False
        self.selected_mhz: float | None = None
        self.last_result: dict[str, Any] | None = None
        self.token: str = ""
        self.local_state: dict[str, Any] = {}

    @property
    def callsign(self) -> str:
        return str(self.identity.get("callsign") or "CALLSIGN")

    def touch(self) -> None:
        self.last_seen = time.time()

    def apply_radios(self, body: dict[str, Any], *, inject: bool = True) -> None:
        freqs = body.get("tuned_freqs_mhz")
        if isinstance(freqs, list):
            out: list[float] = []
            for item in freqs:
                try:
                    out.append(float(item))
                except (TypeError, ValueError):
                    continue
            self.tuned_freqs_mhz = out
        self.radio_fresh = bool(body.get("radio_fresh", True if freqs is not None else False))
        try:
            sel = body.get("selected_mhz")
            self.selected_mhz = float(sel) if sel is not None else None
        except (TypeError, ValueError):
            self.selected_mhz = None
        if inject:
            self.engine.set_remote_radios(
                self.tuned_freqs_mhz,
                fresh=self.radio_fresh,
                selected_mhz=self.selected_mhz,
            )

    def public_status(self, queues: dict[str, Any] | None = None) -> dict[str, Any]:
        st, flow_state = _status_view(self)
        step = st.get("step") if isinstance(st.get("step"), dict) else {}
        channel = str((step or {}).get("channel") or "")
        qinfo = (queues or {}).get(channel or "other") or {}
        step_freq = None
        step_mod = ""
        try:
            step_freq, step_mod, _tx = atc_phrase.step_radio(
                self.engine.airport(),
                channel,
                step,
                state=flow_state,
                config=self.engine.config,
            )
        except Exception:
            step_freq = None
            step_mod = ""
        return {
            "session_id": self.session_id,
            "flow_key": self.flow_key,
            "callsign": self.callsign,
            "opus_flight_id": self.identity.get("opus_flight_id"),
            "opus_seat": self.identity.get("opus_seat"),
            "alive_s": round(time.time() - self.last_seen, 1),
            "tuned_freqs_mhz": list(self.tuned_freqs_mhz),
            "radio_fresh": self.radio_fresh,
            "selected_mhz": self.selected_mhz,
            "mission": st.get("mission"),
            "index": st.get("index"),
            "step_number": st.get("step_number"),
            "total": st.get("total"),
            "at_end": st.get("at_end"),
            "label": st.get("label"),
            "channel": channel,
            "step": step,
            "step_freq_mhz": step_freq,
            "step_mod": step_mod,
            "on_tanker": bool(flow_state.get("tanker_overlay")),
            "flow_state": flow_state,
            "awaiting_readback": bool(flow_state.get("awaiting_readback")),
            "last_tx_text": str(flow_state.get("last_tx_text") or ""),
            "auto_clearance_enabled": bool(
                (self.engine.config or {}).get("auto_clearance_enabled")
            ),
            "queue": qinfo,
            "last_result": self.last_result,
        }


class AtcServer:
    def __init__(
        self,
        config: dict[str, Any],
        airports: dict[str, Any],
        mission_provider: Callable[[], dict[str, Any]],
        *,
        transmit_fn: channel_tx.TransmitFn | None = None,
    ) -> None:
        self.config = config
        self.airports = airports
        self.mission_provider = mission_provider
        self.hub = channel_tx.ChannelTxHub(
            list(atc_phrase.CHANNELS), transmit_fn=transmit_fn
        )
        self.sessions: dict[str, PilotSession] = {}
        self.engines: dict[str, flow_engine.FlowEngine] = {}
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.bind_host = "0.0.0.0"
        self.port = int(config.get("atc_port") or atc_net.DEFAULT_ATC_PORT)
        self.firewall_status = ""
        self.host_engine: flow_engine.FlowEngine | None = None

    def start(self, host: str | None = None, port: int | None = None) -> None:
        self.bind_host = host or "0.0.0.0"
        if port is not None:
            self.port = int(port)
        self._httpd = ThreadingHTTPServer((self.bind_host, self.port), _make_handler(self))
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="atc-host", daemon=True
        )
        self._thread.start()
        urls = "  ".join(atc_net.listen_urls(self.port))
        print(f"ATC host listening on http://{self.bind_host}:{self.port}")
        print(f"Clients use a LAN URL, not 127.0.0.1: {urls}")
        fw = atc_net.ensure_inbound_tcp_firewall(self.port)
        if fw:
            print(f"ATC host {fw}")
        self.firewall_status = fw

    def stop(self) -> None:
        self.hub.stop()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            self._httpd = None

    def token(self) -> str:
        return atc_net.token_of(self.config)

    def get_session(self, session_id: str) -> PilotSession | None:
        with self._lock:
            sess = self.sessions.get(session_id)
        if sess is None:
            return None
        if time.time() - sess.last_seen > atc_net.SESSION_TTL_S:
            return None
        return sess

    def hello(self, body: dict[str, Any]) -> dict[str, Any]:
        identity = _identity_from_hello(body, self.config)
        key = atc_net.session_key(
            opus_flight_id=identity.get("opus_flight_id"),
            opus_seat=identity.get("opus_seat"),
            callsign=str(identity.get("callsign") or ""),
            opus_user_name=str(identity.get("opus_user_name") or ""),
        )
        flow = atc_net.flow_key(
            opus_flight_id=identity.get("opus_flight_id"),
            callsign=str(identity.get("callsign") or ""),
            opus_user_name=str(identity.get("opus_user_name") or ""),
        )
        with self._lock:
            engine = self._engine_for_flow(flow, identity)
            sess = self.sessions.get(key)
            if sess is None or time.time() - sess.last_seen > atc_net.SESSION_TTL_S:
                sess = PilotSession(key, engine, identity)
                sess.flow_key = flow
                self.sessions[key] = sess
            else:
                sess.identity.update(identity)
                sess.engine = engine
                sess.flow_key = flow
        sess.touch()
        sess.apply_radios(body, inject=False)
        self._align_element_overlay(sess)
        return {"ok": True, "session_id": key, **self._host_status(sess)}

    def _host_status(self, sess: PilotSession) -> dict[str, Any]:
        status = sess.public_status(self.hub.snapshot())
        status["auto_clearance_enabled"] = bool(self.config.get("auto_clearance_enabled"))
        return status

    def _flow_alive(self, flow: str) -> bool:
        now = time.time()
        for sess in self.sessions.values():
            if sess.flow_key == flow and now - sess.last_seen <= atc_net.SESSION_TTL_S:
                return True
        return False

    def _engine_for_flow(
        self, flow: str, identity: dict[str, Any]
    ) -> flow_engine.FlowEngine:
        engine = self.engines.get(flow)
        if engine is not None and self._flow_alive(flow):
            return engine
        shared = self._maybe_host_engine(flow, identity)
        if shared is not None:
            _apply_identity_to_config(shared.config, identity)
            self.engines[flow] = shared
            return shared
        engine = _new_session_engine(
            self.config, self.airports, self.mission_provider(), identity
        )
        self.engines[flow] = engine
        return engine

    def _maybe_host_engine(
        self, flow: str, identity: dict[str, Any]
    ) -> flow_engine.FlowEngine | None:
        """
        Reuse the Host Fly timeline so a client's Step ▶ moves the same cursor.

        Share when the host picked the same Opus flight, or the host has no
        flight of its own (dedicated box watching that client).
        """
        host_eng = self.host_engine
        if host_eng is None:
            return None
        host_fid = atc_phrase.configured_opus_flight_id(self.config)
        sess_fid = identity.get("opus_flight_id")
        try:
            same = (
                host_fid is not None
                and sess_fid not in (None, "")
                and int(host_fid) == int(sess_fid)
            )
        except (TypeError, ValueError):
            same = False
        if same:
            return host_eng
        if host_fid is None:
            live_flows = {
                s.flow_key
                for s in self.sessions.values()
                if time.time() - s.last_seen <= atc_net.SESSION_TTL_S
            }
            live_flows.add(flow)
            if len(live_flows) <= 1:
                return host_eng
        return None

    def unique_flow_sessions(self) -> list[PilotSession]:
        """One live session per shared timeline (prefer seat 1) for CAOC auto-clearance."""
        now = time.time()
        groups: dict[str, list[PilotSession]] = {}
        with self._lock:
            live = [
                s for s in self.sessions.values() if now - s.last_seen <= atc_net.SESSION_TTL_S
            ]
        for sess in live:
            groups.setdefault(sess.flow_key, []).append(sess)
        out: list[PilotSession] = []
        for rows in groups.values():
            pick = rows[0]
            for sess in rows:
                try:
                    if int(sess.identity.get("opus_seat") or 0) == 1:
                        pick = sess
                        break
                except (TypeError, ValueError):
                    continue
            _apply_identity_to_config(pick.engine.config, pick.identity)
            pick.engine.set_remote_radios(
                pick.tuned_freqs_mhz,
                fresh=pick.radio_fresh,
                selected_mhz=pick.selected_mhz,
            )
            out.append(pick)
        return out

    def _live_on_flow(self, flow: str) -> list[PilotSession]:
        now = time.time()
        with self._lock:
            return [
                s
                for s in self.sessions.values()
                if s.flow_key == flow and now - s.last_seen <= atc_net.SESSION_TTL_S
            ]

    def _align_element_overlay(self, sess: PilotSession) -> None:
        """A late-joining dash-4 inherits dash-3's tanker side trip."""
        party = tanker.element_seats(sess.identity.get("opus_seat"))
        for other in self._live_on_flow(sess.flow_key):
            if other.session_id == sess.session_id:
                continue
            if tanker.element_seats(other.identity.get("opus_seat")) != party:
                continue
            if tanker.tanker_overlay_active(other.local_state):
                tanker.copy_overlay(sess.local_state, other.local_state)
                return

    def _sync_element_overlay(self, sess: PilotSession) -> None:
        """Lead element (1-2) or second element (3-4) share the AAR parking spot."""
        party = tanker.element_seats(sess.identity.get("opus_seat"))
        for other in self._live_on_flow(sess.flow_key):
            if other.session_id == sess.session_id:
                continue
            if tanker.element_seats(other.identity.get("opus_seat")) != party:
                continue
            tanker.copy_overlay(other.local_state, sess.local_state)

    def _follow_tanker_tune(self, sess: PilotSession) -> None:
        if not tanker.tanker_overlay_active(sess.local_state):
            return
        radio = srs_radio.RadioState(
            source="client",
            freqs_mhz=list(sess.tuned_freqs_mhz),
            fresh=sess.radio_fresh,
            selected_mhz=sess.selected_mhz,
        )
        try:
            tuned = srs_radio.channel_for_tuned_freq(
                sess.engine.airport(),
                sess.engine.config,
                state=radio,
            )
        except Exception:
            return
        if not tanker.note_tanker_tune(sess.local_state, tuned):
            return
        sess.local_state["tanker_overlay"] = False
        sess.local_state.pop("tanker_resume_index", None)
        sess.local_state.pop("tanker_resume_step_id", None)
        sess.local_state.pop("tanker_resume_channel", None)
        sess.local_state.pop("tanker_seen_tune", None)
        self._sync_element_overlay(sess)

    def run_action(
        self,
        sess: PilotSession,
        fn: Callable[[flow_engine.FlowEngine], dict[str, Any]],
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sess.touch()
        lock = getattr(sess.engine, "mutex", None) or sess.lock
        shared_index = int(sess.engine.state.get("index") or 0)
        on_aar = tanker.tanker_overlay_active(sess.local_state)
        with lock:
            _apply_identity_to_config(sess.engine.config, sess.identity)
            sess.engine.config["_tts_session_id"] = sess.session_id
            if body:
                sess.apply_radios(body, inject=True)
            tanker.apply_seat_state(sess.engine.state, sess.local_state)
            if tanker.tanker_overlay_active(sess.engine.state):
                tanker.park_tanker_index(sess.engine)
            sess.engine.defer_tx = True
            sess.engine.pending_tx = None
            try:
                result = fn(sess.engine) or {}
                job = sess.engine.take_pending_tx()
            finally:
                sess.engine.defer_tx = False
                sess.local_state = tanker.snapshot_seat_state(sess.engine.state)
                tanker.strip_seat_state(sess.engine.state)
                # AAR parks on the tanker step only for this element. Do not
                # write that (or leave_tanker seeking C2) onto the flight cursor.
                if on_aar or tanker.tanker_overlay_active(sess.local_state):
                    sess.engine.state["index"] = shared_index
        self._sync_element_overlay(sess)
        if not isinstance(result, dict):
            result = {"detail": result}
        if job:
            job["session_id"] = sess.session_id
            job["callsign"] = sess.callsign
            pos = self.hub.submit(job)
            result = dict(result)
            result["queued"] = True
            result["queue_pos"] = pos
            result["channel"] = job.get("channel")
            result["action"] = result.get("action") or "queued"
        sess.last_result = {
            "action": result.get("action"),
            "label": result.get("label"),
            "text": str(result.get("text") or "")[:200],
            "channel": result.get("channel"),
            "queue_pos": result.get("queue_pos"),
            "at": time.time(),
        }
        return result

    def handle_intent(self, sess: PilotSession, body: dict[str, Any]) -> dict[str, Any]:
        match = _match_from_body(body)
        if not match.intent:
            return {"ok": False, "error": "missing intent"}

        def work(engine: flow_engine.FlowEngine) -> dict[str, Any]:
            return voice_engine.execute_intent(match, engine)

        result = self.run_action(sess, work, body)
        result["ok"] = result.get("action") != "blocked"
        return _attach_fly_status(result, self._host_status(sess))

    def handle_command(
        self, sess: PilotSession, command: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        cmd = command.strip().lower()

        def work(engine: flow_engine.FlowEngine) -> dict[str, Any]:
            if cmd == "next":
                return engine.next()
            if cmd == "back":
                return engine.back()
            if cmd == "reset":
                return engine.reset()
            if cmd == "play":
                sid = str(body.get("step_id") or body.get("id") or "")
                if not sid:
                    raise RuntimeError("play needs step_id")
                if body.get("auto"):
                    blocked = auto_play_block_reason(engine, sid, self.config)
                    if blocked:
                        return {"action": "blocked", "detail": blocked}
                return engine.play_id(sid)
            if cmd == "tanker_chat":
                return run_tanker_chat_command(engine, body)
            if cmd == "seek":
                return engine.seek(int(body.get("index") or 0))
            if cmd == "seek_relative":
                return engine.seek_relative(int(body.get("delta") or 0))
            if cmd == "seek_number":
                return engine.seek_number(int(body.get("number") or body.get("n") or 1))
            raise RuntimeError(f"unknown command: {cmd}")

        result = self.run_action(sess, work, body)
        result["ok"] = result.get("action") != "blocked"
        return _attach_fly_status(result, self._host_status(sess))

    def heartbeat(self, sess: PilotSession, body: dict[str, Any]) -> dict[str, Any]:
        sess.touch()
        sess.apply_radios(body, inject=False)
        self._follow_tanker_tune(sess)
        return {"ok": True, **self._host_status(sess)}

    def traffic(self) -> dict[str, Any]:
        queues = self.hub.snapshot()
        with self._lock:
            sessions = list(self.sessions.values())
        now = time.time()
        live = [s for s in sessions if now - s.last_seen <= atc_net.SESSION_TTL_S]
        return {
            "ok": True,
            "role": "host",
            "port": self.port,
            "queues": queues,
            "sessions": [
                {
                    **s.public_status(queues),
                    "auto_clearance_enabled": bool(self.config.get("auto_clearance_enabled")),
                }
                for s in live
            ],
        }


def auto_play_block_reason(
    engine: flow_engine.FlowEngine,
    step_id: str,
    config: dict[str, Any] | None = None,
) -> str:
    """
    Why a client Watch auto-play must not transmit.

    Clients evaluate CAOC on the flying PC (where the jets actually are) and
    POST play with auto=True. The host still owns TX, so refuse a second
    copy of the same step.
    """
    _ = config
    want = str(step_id or "").strip()
    if not want:
        return "play needs step_id"
    cur: dict[str, Any] = {}
    try:
        cur = engine.current_step() or {}
    except Exception:
        cur = {}
    tmpl = str(cur.get("template") or "")
    more_land = False
    if tmpl == "clear_land":
        try:
            more_land = atc_phrase.should_hold_for_landing_clearances(
                engine.state, step=cur, mission=getattr(engine, "mission", None)
            )
        except Exception:
            more_land = False
    if runway_position.skip_auto_tx_already_played(
        fire_id=want,
        current_step_id=str(cur.get("id") or cur.get("template") or ""),
        last_step_id=str((engine.state or {}).get("last_step_id") or ""),
        template=tmpl,
        hold_for_landing=more_land,
    ):
        return "already played"
    return ""


def run_tanker_chat_command(
    engine: flow_engine.FlowEngine, body: dict[str, Any]
) -> dict[str, Any]:
    """
    Host TX for boom chat. Client may send a locally generated line (Ollama
    on the flying PC); otherwise the host writes the next bit itself.
    """
    import tanker_chat as tanker_chat_mod
    import voice_engine as voice_engine_mod

    continuing = bool(body.get("continue"))
    auto = bool(body.get("auto", True))
    text = str(body.get("text") or "").strip()
    if (
        auto
        and continuing
        and tanker_chat_mod.is_session_active(engine.state)
        and not tanker_chat_mod.continuation_due(engine.state)
    ):
        return {"action": "none", "detail": "tanker chat not due"}
    if text:
        if body.get("await_pilot"):
            tanker_chat_mod.hold_for_pilot(engine.state, text)
        else:
            tanker_chat_mod.schedule_next_question(engine.state, llm=True)
        return voice_engine_mod.speak_tanker_line(engine, text)
    return voice_engine_mod.resolve_tanker_chat(
        engine, continue_session=continuing, force=not continuing and not auto
    )


def _attach_fly_status(
    result: dict[str, Any], status: dict[str, Any]
) -> dict[str, Any]:
    """
    Keep TX radio/text on the result, and add the Fly cursor the Client paints.

    Request tanker transmits on Blackjack; NEXT TX FREQUENCY is the parked
    tanker UHF. Do not let the C2 TX freq overwrite that.
    """
    out = dict(result or {})
    for key, value in (status or {}).items():
        if key in ("freq", "channel", "text", "action", "ok") and key in out:
            continue
        out.setdefault(key, value)
    out["flow_state"] = status.get("flow_state")
    out["step_freq_mhz"] = status.get("step_freq_mhz")
    out["step_mod"] = status.get("step_mod")
    out["on_tanker"] = status.get("on_tanker")
    out["index"] = status.get("index")
    out["step_number"] = status.get("step_number")
    out["label"] = status.get("label")
    out["step"] = status.get("step")
    out["status_channel"] = status.get("channel")
    return out


def _shared_flow_state(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {"index": 0, "awaiting_readback": False}
    out = {key: state[key] for key in _SHARED_FLOW_KEYS if key in state}
    out.setdefault("index", int(state.get("index") or 0))
    out.setdefault("awaiting_readback", False)
    return out


def _status_view(sess: PilotSession) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flight cursor, plus this seat's tanker overlay when they peeled off."""
    engine = sess.engine
    lock = getattr(engine, "mutex", None)

    def _compute() -> tuple[dict[str, Any], dict[str, Any]]:
        fs = _shared_flow_state(engine.state)
        fs.update(dict(sess.local_state))
        if not tanker.tanker_overlay_active(sess.local_state):
            return engine.status(), fs
        saved = int(engine.state.get("index") or 0)
        tanker.apply_seat_state(engine.state, sess.local_state)
        tanker.park_tanker_index(engine)
        try:
            st = engine.status()
            fs = _shared_flow_state(engine.state)
            fs.update(tanker.snapshot_seat_state(engine.state))
            return st, fs
        finally:
            tanker.strip_seat_state(engine.state)
            engine.state["index"] = saved

    if lock is not None:
        with lock:
            return _compute()
    return _compute()


def _identity_from_hello(body: dict[str, Any], host_config: dict[str, Any]) -> dict[str, Any]:
    ident = {
        "opus_user_name": str(body.get("opus_user_name") or "").strip(),
        "opus_flight_id": body.get("opus_flight_id"),
        "opus_seat": body.get("opus_seat"),
        "opus_flight_label": str(body.get("opus_flight_label") or "").strip(),
        "callsign_override": str(body.get("callsign_override") or "").strip(),
    }
    # Do not use the host process Opus cache — that is whoever the host
    # last resolved, not this client's Wild 6 / Fleece 1.
    callsign = ident["callsign_override"]
    if not callsign:
        callsign = atc_phrase.clean_flight_callsign(ident["opus_flight_label"])
    if not callsign:
        cfg = dict(host_config)
        _apply_identity_to_config(cfg, ident)
        try:
            ctx = atc_phrase.resolve_active_opus_flight(cfg)
            callsign = str(getattr(ctx, "radio_callsign", "") or "")
        except Exception:
            callsign = ""
    if not callsign:
        callsign = ident["opus_user_name"] or "CALLSIGN"
    ident["callsign"] = callsign
    return ident


def _apply_identity_to_config(config: dict[str, Any], identity: dict[str, Any]) -> None:
    config["opus_user_name"] = identity.get("opus_user_name") or ""
    config["opus_flight_id"] = identity.get("opus_flight_id")
    config["opus_seat"] = identity.get("opus_seat")
    config["opus_flight_label"] = identity.get("opus_flight_label") or ""
    config["callsign_override"] = identity.get("callsign_override") or ""


def _new_session_engine(
    host_config: dict[str, Any],
    airports: dict[str, Any],
    mission: dict[str, Any],
    identity: dict[str, Any],
) -> flow_engine.FlowEngine:
    cfg = copy.deepcopy(host_config)
    _apply_identity_to_config(cfg, identity)
    cfg["_tts_session_id"] = atc_net.session_key(
        opus_flight_id=identity.get("opus_flight_id"),
        opus_seat=identity.get("opus_seat"),
        callsign=str(identity.get("callsign") or identity.get("callsign_override") or ""),
        opus_user_name=str(identity.get("opus_user_name") or ""),
    )
    return flow_engine.FlowEngine(
        config=cfg,
        persist_state=False,
        mission=mission,
        airports=airports,
    )


def _match_from_body(body: dict[str, Any]) -> voice_intent.Match:
    slots = body.get("slots") if isinstance(body.get("slots"), dict) else {}
    try:
        conf = float(body.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return voice_intent.Match(
        intent=str(body.get("intent") or ""),
        kind=str(body.get("kind") or "step"),
        template=str(body.get("template") or ""),
        confidence=conf,
        slots=dict(slots),
        transcript=str(body.get("transcript") or ""),
        normalized=str(body.get("normalized") or ""),
        step_id=str(body.get("step_id") or ""),
        expected=bool(body.get("expected")),
        summarised=bool(body.get("summarised")),
    )


def _make_handler(server: AtcServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            print("[atc-host]", fmt % args)

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(atc_phrase.redact_secrets(payload)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY:
                raise RuntimeError("body too large")
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise RuntimeError("JSON object required")
            return data

        def _auth(self) -> bool:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path in ("/v1/health", "/health"):
                return True
            got = self.headers.get(atc_net.TOKEN_HEADER) or ""
            if atc_net.tokens_match(server.token(), got):
                return True
            self._send(401, {"ok": False, "error": "bad or missing token"})
            return False

        def _session(self, body: dict[str, Any]) -> PilotSession | None:
            sid = str(
                self.headers.get("X-ATC-Session")
                or body.get("session_id")
                or ""
            ).strip()
            sess = server.get_session(sid) if sid else None
            if sess is None:
                self._send(404, {"ok": False, "error": "unknown session — POST /v1/hello"})
            return sess

        def do_GET(self) -> None:  # noqa: N802
            if not self._auth():
                return
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if path in ("/v1/health", "/health"):
                    self._send(200, {"ok": True, "service": "atc-host", "port": server.port})
                    return
                if path in ("/v1/traffic", "/traffic"):
                    self._send(200, server.traffic())
                    return
                if path in ("/v1/me", "/me"):
                    sess = self._session({})
                    if sess is None:
                        return
                    self._send(200, {"ok": True, **server._host_status(sess)})
                    return
                self._send(404, {"ok": False, "error": f"unknown {path}"})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"ok": False, "error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._auth():
                return
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                body = self._read_json()
            except Exception as exc:  # noqa: BLE001
                self._send(400, {"ok": False, "error": str(exc)})
                return
            try:
                if path in ("/v1/hello", "/hello"):
                    self._send(200, server.hello(body))
                    return
                sess = self._session(body)
                if sess is None:
                    return
                if path in ("/v1/intent", "/intent"):
                    self._send(200, server.handle_intent(sess, body))
                    return
                if path in ("/v1/heartbeat", "/heartbeat"):
                    self._send(200, server.heartbeat(sess, body))
                    return
                if path.startswith("/v1/"):
                    cmd = path.split("/")[-1]
                    self._send(200, server.handle_command(sess, cmd, body))
                    return
                self._send(404, {"ok": False, "error": f"unknown {path}"})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"ok": False, "error": str(exc)})

    return Handler
