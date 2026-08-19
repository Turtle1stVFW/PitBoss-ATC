"""
Multi-pilot ATC host: per-pilot flow sessions + per-channel TX queues.

Binds LAN HTTP (default :8766). Clients STT locally and POST intents here.
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
import voice_engine  # noqa: E402
import voice_intent  # noqa: E402

MAX_BODY = 256_000


class PilotSession:
    def __init__(
        self,
        session_id: str,
        engine: flow_engine.FlowEngine,
        identity: dict[str, Any],
    ) -> None:
        self.session_id = session_id
        self.engine = engine
        self.identity = dict(identity)
        self.lock = threading.RLock()
        self.last_seen = time.time()
        self.tuned_freqs_mhz: list[float] = []
        self.radio_fresh = False
        self.selected_mhz: float | None = None
        self.last_result: dict[str, Any] | None = None
        self.token: str = ""

    @property
    def callsign(self) -> str:
        return str(self.identity.get("callsign") or "CALLSIGN")

    def touch(self) -> None:
        self.last_seen = time.time()

    def apply_radios(self, body: dict[str, Any]) -> None:
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
        self.engine.set_remote_radios(
            self.tuned_freqs_mhz,
            fresh=self.radio_fresh,
            selected_mhz=self.selected_mhz,
        )

    def public_status(self, queues: dict[str, Any] | None = None) -> dict[str, Any]:
        st = self.engine.status()
        step = st.get("step") if isinstance(st.get("step"), dict) else {}
        channel = str((step or {}).get("channel") or "")
        qinfo = (queues or {}).get(channel or "other") or {}
        return {
            "session_id": self.session_id,
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
            "last_tx_text": str((self.engine.state or {}).get("last_tx_text") or ""),
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
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.bind_host = "0.0.0.0"
        self.port = int(config.get("atc_port") or atc_net.DEFAULT_ATC_PORT)

    def start(self, host: str | None = None, port: int | None = None) -> None:
        self.bind_host = host or "0.0.0.0"
        if port is not None:
            self.port = int(port)
        self._httpd = ThreadingHTTPServer((self.bind_host, self.port), _make_handler(self))
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="atc-host", daemon=True
        )
        self._thread.start()
        print(f"ATC host listening on http://{self.bind_host}:{self.port}")

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
        with self._lock:
            sess = self.sessions.get(key)
            if sess is None or time.time() - sess.last_seen > atc_net.SESSION_TTL_S:
                engine = _new_session_engine(
                    self.config, self.airports, self.mission_provider(), identity
                )
                sess = PilotSession(key, engine, identity)
                self.sessions[key] = sess
            else:
                sess.identity.update(identity)
                _apply_identity_to_config(sess.engine.config, identity)
        sess.touch()
        sess.apply_radios(body)
        return {"ok": True, "session_id": key, **sess.public_status(self.hub.snapshot())}

    def run_action(
        self,
        sess: PilotSession,
        fn: Callable[[flow_engine.FlowEngine], dict[str, Any]],
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sess.touch()
        if body:
            sess.apply_radios(body)
        with sess.lock:
            sess.engine.defer_tx = True
            sess.engine.pending_tx = None
            try:
                result = fn(sess.engine) or {}
                job = sess.engine.take_pending_tx()
            finally:
                sess.engine.defer_tx = False
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
        return result

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
                return engine.play_id(sid)
            if cmd == "seek":
                return engine.seek(int(body.get("index") or 0))
            if cmd == "seek_relative":
                return engine.seek_relative(int(body.get("delta") or 0))
            if cmd == "seek_number":
                return engine.seek_number(int(body.get("number") or body.get("n") or 1))
            raise RuntimeError(f"unknown command: {cmd}")

        result = self.run_action(sess, work, body)
        result["ok"] = True
        return result

    def heartbeat(self, sess: PilotSession, body: dict[str, Any]) -> dict[str, Any]:
        sess.touch()
        sess.apply_radios(body)
        return {"ok": True, **sess.public_status(self.hub.snapshot())}

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
            "sessions": [s.public_status(queues) for s in live],
        }


def _identity_from_hello(body: dict[str, Any], host_config: dict[str, Any]) -> dict[str, Any]:
    ident = {
        "opus_user_name": str(body.get("opus_user_name") or "").strip(),
        "opus_flight_id": body.get("opus_flight_id"),
        "opus_seat": body.get("opus_seat"),
        "opus_flight_label": str(body.get("opus_flight_label") or "").strip(),
        "callsign_override": str(body.get("callsign_override") or "").strip(),
    }
    cfg = dict(host_config)
    _apply_identity_to_config(cfg, ident)
    callsign = ident["callsign_override"]
    if not callsign:
        try:
            callsign = str(atc_phrase.cached_radio_callsign(cfg) or "")
        except Exception:
            callsign = ""
        if not callsign:
            callsign = ident["opus_flight_label"] or ident["opus_user_name"] or "CALLSIGN"
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
                    self._send(200, {"ok": True, **sess.public_status(server.hub.snapshot())})
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
