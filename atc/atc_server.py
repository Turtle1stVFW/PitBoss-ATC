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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
import version  # noqa: E402

MAX_BODY = 256_000

# Cursor the whole flight should see. Tanker AAR lives on PilotSession.local_state.
# atc_role is intentionally NOT in this list — Host role must never be flipped
# to solo during seat bind (Traffic desync + wrong ExternalAudio SRS target).
_IDENTITY_CONFIG_KEYS = (
    "opus_user_name",
    "opus_flight_id",
    "opus_seat",
    "opus_flight_label",
    "callsign_override",
)
# Client Fly copies these, then drops any key the host omitted so a cache
# reset does not leave the last sortie (OPS start, pending Delivery, …).
SHARED_FLOW_KEYS = (
    "index",
    "last_step_id",
    "awaiting_readback",
    "readback_items",
    "last_tx_text",
    "last_tx_at",
    "last_tx_end_at",
    "last_tx_channel",
    "last_tx_template",
    "last_tx_confirmed",
    "last_agency",
    "contact_phase",
    "pending_contact",
    "control_checked_in",
    "control_channel",
    "blackjack_checked_in",
    "bandsaw_checked_in",
    "clearance_amendment_copied",
    "amended_altitude_ft",
    "assigned_altitude_ft",
    "approach_plan",
    "approach_checked_in",
    "approach_runway",
    "active_recovery",
    "landing_intent",
    "awaiting_on_the_go",
    "pattern_land_needs_leave",
    "go_around_plan",
    "sfo_pattern_open",
    "sfo_phase",
    "sfo_high_key_ft",
    "sfo_base_key_pending",
    "manual_cursor",
    "manual_step_view",
    "active_takeoff_mode",
    "pending_takeoff_offer",
    "takeoff_offer_rolled",
    "ops_sortie",
    "ops_codes_pending",
    "ops_codes_last_at",
    "ops_codes_done",
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
        self.ownship_ll: tuple[float, float] | None = None
        self.ownship_ll_t: float = 0.0
        self.ownship_alt_ft: float | None = None

    @property
    def callsign(self) -> str:
        return str(self.identity.get("callsign") or "CALLSIGN")

    def touch(self) -> None:
        self.last_seen = time.time()

    def apply_ownship(self, body: dict[str, Any]) -> None:
        """
        Record where this seat says it is.

        Stamped on receipt rather than from the client's clock, so a skewed PC
        cannot make its fix look fresh (or stale) to the host's gates.
        """
        if "ownship_ll" not in body:
            return
        raw = body.get("ownship_ll")
        ll: tuple[float, float] | None = None
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try:
                ll = (float(raw[0]), float(raw[1]))
            except (TypeError, ValueError):
                ll = None
        self.ownship_ll = ll
        self.ownship_ll_t = time.time() if ll is not None else 0.0
        raw_alt = body.get("ownship_alt_ft")
        try:
            self.ownship_alt_ft = (
                float(raw_alt) if raw_alt not in (None, "") else None
            )
        except (TypeError, ValueError):
            self.ownship_alt_ft = None

    def fresh_ownship_ll(self) -> tuple[float, float] | None:
        """This seat's fix while it is recent enough to gate on."""
        if self.ownship_ll is None:
            return None
        if (time.time() - self.ownship_ll_t) > atc_phrase.OWNSHIP_FIX_MAX_AGE_S:
            return None
        return self.ownship_ll

    def apply_radios(self, body: dict[str, Any], *, inject: bool = True) -> None:
        freqs = body.get("tuned_freqs_mhz")
        if isinstance(freqs, list):
            out: list[float] = []
            for item in freqs:
                try:
                    out.append(float(item))
                except (TypeError, ValueError):
                    continue
            fresh = bool(body.get("radio_fresh", True))
            # Empty + not fresh = failed client read. Keep the last good bank
            # so a blip cannot freq-gate the whole sortie.
            if out or fresh:
                self.tuned_freqs_mhz = out
                self.radio_fresh = fresh
            else:
                self.radio_fresh = False
        elif "radio_fresh" in body:
            self.radio_fresh = bool(body.get("radio_fresh"))
        if "selected_mhz" in body:
            try:
                sel = body.get("selected_mhz")
                self.selected_mhz = float(sel) if sel is not None else None
            except (TypeError, ValueError):
                self.selected_mhz = None
        self.apply_ownship(body)
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
            "client_version": str(self.identity.get("client_version") or ""),
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
        try:
            import app_diag

            app_diag.info(
                app_diag.CAT_NETWORK,
                "ATC Host started",
                bind=self.bind_host,
                port=self.port,
                firewall=fw or "ok",
                version=version.label(),
            )
        except Exception:
            pass

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

    def _reclaim_stale_sessions(
        self,
        keep_key: str,
        identity: dict[str, Any],
        *,
        prior_session_id: str = "",
    ) -> None:
        """
        Drop orphan Traffic rows when one PC re-hellos under a new key.

        Clearing the Opus flight then picking a seat again used to leave the old
        ``flight:…`` or ``callsign:…`` session alive until SESSION_TTL (15 min),
        so Host Traffic showed two RAZOR 1 rows (seat ? + seat 1). Only drop
        callsign-keyed orphans for the same callsign — never another seat's
        ``flight:fid:N`` row.
        """
        drop: list[str] = []
        prior = str(prior_session_id or "").strip()
        if prior and prior != keep_key and prior in self.sessions:
            drop.append(prior)
        cs = atc_net._slug(
            str(identity.get("callsign") or identity.get("opus_user_name") or "")
        )
        fid = str(identity.get("opus_flight_id") or "").strip()
        if cs and fid:
            for sid, sess in self.sessions.items():
                if sid == keep_key or sid in drop:
                    continue
                if not sid.startswith("callsign:"):
                    continue
                other = atc_net._slug(
                    str(
                        sess.identity.get("callsign")
                        or sess.identity.get("opus_user_name")
                        or ""
                    )
                )
                if other == cs:
                    drop.append(sid)
        for sid in drop:
            self.sessions.pop(sid, None)

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
        prior = str(
            body.get("prior_session_id") or body.get("replace_session_id") or ""
        ).strip()
        with self._lock:
            self._reclaim_stale_sessions(key, identity, prior_session_id=prior)
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
        client_ver = str(body.get("client_version") or "").strip()
        if client_ver:
            sess.identity["client_version"] = client_ver
        self._align_element_overlay(sess)
        try:
            import app_diag

            app_diag.note_transition(
                f"client-build:{key}",
                client_ver or "unknown",
                "ATC client connected",
                category=app_diag.CAT_NETWORK,
                client_version=client_ver or "unknown",
                host_version=version.label(),
                callsign=str(identity.get("callsign") or ""),
            )
        except Exception:
            pass
        return {"ok": True, "session_id": key, **self._host_status(sess)}

    def _host_status(self, sess: PilotSession) -> dict[str, Any]:
        status = sess.public_status(self.hub.snapshot())
        status["auto_clearance_enabled"] = bool(self.config.get("auto_clearance_enabled"))
        status["host_version"] = version.label()
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
            # Do not stamp this seat onto the shared Host engine — identity
            # and radios bind only for the duration of that seat's action.
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
        """One live session per shared timeline (prefer seat 1) for CAOC auto-clearance.

        Do not stamp client Opus identity or radios onto the engine here. Host
        Fly shares this process's config dict — writing a client flight onto it
        made the top bar adopt that callsign and hammer Opus with 404s after the
        sortie ended. Callers bind seat identity for the duration of an action
        via ``_session_engine_binding``.
        """
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
            with _session_engine_binding(sess, body=body, inject_radios=True):
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
                    # Arrows park the shared flight. Do not snap the index back
                    # to the pre-AAR cursor — that made ◀ ▶ a no-op on tanker.
                    manual_nav = bool(
                        isinstance(sess.engine.state, dict)
                        and sess.engine.state.get("manual_step_view")
                    )
                    if manual_nav and (
                        on_aar or tanker.tanker_overlay_active(sess.engine.state)
                    ):
                        try:
                            tanker.reconcile_aar_overlay(sess.engine)
                        except Exception:
                            tanker.clear_aar_state(sess.engine.state)
                    sess.local_state = tanker.snapshot_seat_state(sess.engine.state)
                    tanker.strip_seat_state(sess.engine.state)
                    # AAR parks on the tanker step only for this element. Always
                    # put the shared flight cursor back — seat tanker position
                    # lives in local_state (including ◀ ▶). Skipping restore when
                    # leave_tanker set manual_step_view left bandsaw on C2 for
                    # every seat.
                    if on_aar or tanker.tanker_overlay_active(sess.local_state):
                        sess.engine.state["index"] = shared_index
        self._sync_element_overlay(sess)
        if not isinstance(result, dict):
            result = {"detail": result}
        if job:
            job["session_id"] = sess.session_id
            job["callsign"] = sess.callsign
            # So the channel worker can stamp when speech actually ends.
            job["flow_state"] = sess.engine.state
            # Host hub always speaks into local SRS (127.0.0.1), never the
            # squadron hostname Clients use. pin_host_tx_target also runs in
            # hub.submit as a second belt.
            try:
                import atc_net

                atc_net.pin_host_tx_target(job)
            except Exception:
                raw_cfg = job.get("config")
                if isinstance(raw_cfg, dict):
                    tx_cfg = dict(raw_cfg)
                    tx_cfg["atc_role"] = "host"
                    job["config"] = tx_cfg
            pos = self.hub.submit(job)
            result = dict(result)
            result["queue_pos"] = pos
            # Speak before the Client paints the card. Otherwise Fly shows
            # "contact Ground" / the readback and the pilot leaves the
            # frequency while Tower is still talking.
            done = job.get("done")
            finished = True
            if isinstance(done, threading.Event):
                finished = done.wait(timeout=100.0)
            code = job.get("exit_code")
            err = str(job.get("error") or job.get("prerender_error") or "").strip()
            try:
                failed = code is not None and int(code) != 0
            except (TypeError, ValueError):
                failed = True
            if not finished or failed or code is None:
                result["queued"] = False
                result["action"] = "blocked"
                result["detail"] = (
                    err
                    or ("radio transmit timed out" if not finished else "")
                    or f"radio transmit failed ({code})"
                )
                # play_step may have queued readback/advance — undo so Clients
                # do not paint a phrase ExternalAudio never spoke, and the
                # cursor stays on the step that still needs a successful TX.
                try:
                    if hasattr(sess.engine, "abandon_deferred_tx"):
                        sess.engine.abandon_deferred_tx()
                    else:
                        atc_phrase.revert_failed_radio_tx(sess.engine.state)
                        if hasattr(sess.engine, "save_state"):
                            sess.engine.save_state()
                except Exception:
                    pass
                try:
                    import app_diag

                    app_diag.error(
                        app_diag.CAT_TX,
                        "Host TX blocked after radio failure",
                        channel=str(job.get("channel") or ""),
                        exit_code=code,
                        detail=str(result.get("detail") or "")[:200],
                    )
                except Exception:
                    pass
            else:
                result["queued"] = True
                result["exit_code"] = int(code)
                # Cursor / LAST HEARD / readback / agency handoff waited for
                # ExternalAudio exit 0 — apply them now.
                # Element AAR: run_action already restored the shared C2 index.
                # Do not let a deferred advance walk the flight timeline.
                apply_advance = not (
                    on_aar or tanker.tanker_overlay_active(sess.local_state)
                )
                try:
                    if hasattr(sess.engine, "commit_deferred_tx_success"):
                        sess.engine.commit_deferred_tx_success(
                            apply_advance=apply_advance
                        )
                except Exception:
                    pass
            result["channel"] = job.get("channel")
            # Voice often wraps play_id as action=play + detail={...}. Promote
            # spoken text so Clients log/hear the real phrase, not the label.
            detail = result.get("detail")
            if isinstance(detail, dict):
                for key in ("text", "label", "freq", "step_id", "callsign"):
                    if result.get(key) in (None, "") and detail.get(key) not in (
                        None,
                        "",
                    ):
                        result[key] = detail[key]
            if not result.get("text") and job.get("text"):
                result["text"] = job.get("text")
            if result.get("action") == "blocked":
                pass
            elif int(result.get("exit_code") or -1) == 0 and result.get("queued"):
                # Hub waited for a real exit 0 — report transmit, not a
                # premature "queued" success that Fly can misread as fired.
                result["action"] = "transmit"
            elif result.get("action") in (None, "", "play", "none"):
                result["action"] = "queued"
            else:
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
            if cmd in ("clear_flight_cache", "reset_flight_cache"):
                return engine.clear_flight_cache()
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
            if cmd == "ops_codes_finalize":
                # Client schedules codes idle on the flying PC, then Host TXes.
                return voice_engine.execute_ops_action(engine, "ops_codes_finalize")
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
        if body.get("await_pilot") or tanker_chat_mod.invites_reply(text):
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
    out["session_id"] = status.get("session_id")
    out["flow_key"] = status.get("flow_key")
    out["opus_flight_id"] = status.get("opus_flight_id")
    out["opus_seat"] = status.get("opus_seat")
    return out


def _shared_flow_state(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {"index": 0, "awaiting_readback": False}
    out = {key: state[key] for key in SHARED_FLOW_KEYS if key in state}
    out.setdefault("index", int(state.get("index") or 0))
    out.setdefault("awaiting_readback", False)
    out.setdefault("blackjack_checked_in", False)
    out.setdefault("bandsaw_checked_in", False)
    out.setdefault("contact_phase", "field")
    return out


def _status_view(sess: PilotSession) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flight cursor, plus this seat's tanker overlay when they peeled off."""
    engine = sess.engine
    lock = getattr(engine, "mutex", None)

    def _compute() -> tuple[dict[str, Any], dict[str, Any]]:
        with _session_engine_binding(sess):
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
    # Keep atc_role as Host. Session engines (and the shared Host Fly engine
    # during bind) used to flip to solo so Opus would resolve the client's
    # flight_id — but Host-without-flight_id already skips Opus only when
    # selected_id is None, and with the client's flight_id bound it resolves.
    # Stamping solo onto the shared config made Traffic paint "Solo" while
    # Setup still showed Host, and ExternalAudio targeted squadron SRS
    # instead of 127.0.0.1 — silent TX on every agency. Never mutate role.


@contextmanager
def _session_engine_binding(
    sess: PilotSession,
    *,
    body: dict[str, Any] | None = None,
    inject_radios: bool = False,
) -> Iterator[None]:
    """
    Bind this seat's identity and radios for one host action / status view.

    The shared FlowEngine may be the Host Fly timeline or another seat on the
    same Opus flight. Never leave this jet's callsign or SRS stack on it.
    """
    engine = sess.engine
    cfg = engine.config if isinstance(getattr(engine, "config", None), dict) else {}
    saved_radios = getattr(engine, "remote_radios", None)
    saved_ident = {key: cfg.get(key) for key in _IDENTITY_CONFIG_KEYS}
    saved_tts = cfg.get("_tts_session_id")
    saved_seat_pos = {key: cfg.get(key) for key in _SEAT_POSITION_CONFIG_KEYS}
    try:
        _apply_identity_to_config(cfg, sess.identity)
        cfg["_tts_session_id"] = sess.session_id
        if body is not None:
            sess.apply_radios(body, inject=inject_radios)
        elif inject_radios:
            engine.set_remote_radios(
                sess.tuned_freqs_mhz,
                fresh=sess.radio_fresh,
                selected_mhz=sess.selected_mhz,
            )
        _bind_seat_position(cfg, sess, engine)
        yield
    finally:
        engine.remote_radios = saved_radios
        for key, value in saved_ident.items():
            if value is None:
                cfg.pop(key, None)
            else:
                cfg[key] = value
        for key, value in saved_seat_pos.items():
            if value is None:
                cfg.pop(key, None)
            else:
                cfg[key] = value
        if saved_tts is None:
            cfg.pop("_tts_session_id", None)
        else:
            cfg["_tts_session_id"] = saved_tts


_SEAT_POSITION_CONFIG_KEYS = (
    atc_phrase.OWNSHIP_SEAT_BOUND_KEY,
    atc_phrase.OWNSHIP_SEAT_LL_KEY,
    atc_phrase.OWNSHIP_SEAT_ALT_KEY,
)


def _bind_seat_position(
    cfg: dict[str, Any],
    sess: PilotSession,
    engine: flow_engine.FlowEngine,
) -> None:
    """
    Make this seat's own fix the only position for the bound action.

    Host Fly's map inject and CAOC feed watch the host PC. Reading them for a
    client's jet is how a 38 NM recovery fired the 12 NM tower handoff and the
    6 NM landing clearance off a parked contact 0.8 NM from the field.

    The cached fix on the shared flow state is overwritten too, so gates that
    read state before config cannot fall back to the host's position.
    """
    cfg[atc_phrase.OWNSHIP_SEAT_BOUND_KEY] = True
    ll = sess.fresh_ownship_ll()
    state = engine.state if isinstance(getattr(engine, "state", None), dict) else None
    if ll is None:
        cfg.pop(atc_phrase.OWNSHIP_SEAT_LL_KEY, None)
        cfg.pop(atc_phrase.OWNSHIP_SEAT_ALT_KEY, None)
        if state is not None:
            state.pop("ownship_ll", None)
            state.pop("ownship_ll_t", None)
        return
    cfg[atc_phrase.OWNSHIP_SEAT_LL_KEY] = [ll[0], ll[1]]
    alt = getattr(sess, "ownship_alt_ft", None)
    if alt is not None:
        cfg[atc_phrase.OWNSHIP_SEAT_ALT_KEY] = alt
    else:
        cfg.pop(atc_phrase.OWNSHIP_SEAT_ALT_KEY, None)
    if state is not None:
        state["ownship_ll"] = [ll[0], ll[1]]
        state["ownship_ll_t"] = sess.ownship_ll_t


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
