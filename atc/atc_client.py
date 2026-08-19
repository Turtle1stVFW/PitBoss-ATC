"""
Thin pilot client: local STT / hotkeys, host owns ATC TX.

Local Stream Deck HTTP (8765) is wrapped by ClientEngineProxy so /next
reaches the host session instead of transmitting from this PC.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

import atc_net
import srs_radio
import voice_intent

class AtcClientError(RuntimeError):
    pass


class AtcClient:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.session_id = ""
        self.callsign = ""
        self.last_status: dict[str, Any] = {}
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        host = str(self.config.get("atc_host") or "127.0.0.1").strip() or "127.0.0.1"
        port = int(self.config.get("atc_port") or atc_net.DEFAULT_ATC_PORT)
        return f"http://{host}:{port}"

    def start(self) -> str:
        """Hello + heartbeat. Returns a warning string, or empty on success."""
        self._stop.clear()
        try:
            self.hello()
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return f"ATC host: {exc}"
        self._thread = threading.Thread(
            target=self._heartbeat_loop, name="atc-client-hb", daemon=True
        )
        self._thread.start()
        return ""

    def stop(self) -> None:
        self._stop.set()

    def hello(self) -> dict[str, Any]:
        body = _identity_payload(self.config)
        body.update(_radio_payload(self.config))
        data = self._request("POST", "/v1/hello", body)
        self.session_id = str(data.get("session_id") or "")
        self.callsign = str(data.get("callsign") or "")
        self.last_status = data
        self.last_error = ""
        return data

    def post_intent(self, match: voice_intent.Match) -> dict[str, Any]:
        body = {
            "session_id": self.session_id,
            "intent": match.intent,
            "kind": match.kind,
            "template": match.template,
            "confidence": match.confidence,
            "slots": dict(match.slots or {}),
            "transcript": match.transcript,
            "normalized": match.normalized,
            "step_id": match.step_id,
            "expected": match.expected,
            "summarised": match.summarised,
        }
        body.update(_radio_payload(self.config))
        return self._request("POST", "/v1/intent", body)

    def action(self, command: str, **fields: Any) -> dict[str, Any]:
        body = {"session_id": self.session_id}
        body.update(fields)
        body.update(_radio_payload(self.config))
        path = f"/v1/{command.strip().lower()}"
        return self._request("POST", path, body)

    def me(self) -> dict[str, Any]:
        data = self._request(
            "GET",
            "/v1/me",
            headers={"X-ATC-Session": self.session_id},
        )
        self.last_status = data
        return data

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                body = {"session_id": self.session_id}
                body.update(_radio_payload(self.config))
                data = self._request("POST", "/v1/heartbeat", body)
                self.last_status = data
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        token = atc_net.token_of(self.config)
        hdrs = {
            "Content-Type": "application/json; charset=utf-8",
            atc_net.TOKEN_HEADER: token,
        }
        if headers:
            hdrs.update(headers)
        data = None if body is None or method == "GET" else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=hdrs
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            try:
                parsed = json.loads(detail)
                msg = str(parsed.get("error") or detail)
            except Exception:
                msg = detail or str(exc)
            raise AtcClientError(f"{exc.code} {msg}") from exc
        except urllib.error.URLError as exc:
            raise AtcClientError(f"host unreachable ({exc.reason})") from exc
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            raise AtcClientError("host returned non-object JSON")
        return payload


class ClientEngineProxy:
    """Stand-in FlowEngine for localhost :8765 so Stream Deck hits the host."""

    def __init__(self, client: AtcClient) -> None:
        self.client = client
        self.config: dict[str, Any] = client.config
        self.mission: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self.airports: dict[str, Any] = {}

    def reload(self) -> None:
        return None

    def next(self, *, bypass_freq_gate: bool = False) -> dict[str, Any]:
        del bypass_freq_gate
        return self.client.action("next")

    def back(self, *, bypass_freq_gate: bool = False) -> dict[str, Any]:
        del bypass_freq_gate
        return self.client.action("back")

    def reset(self) -> dict[str, Any]:
        return self.client.action("reset")

    def flip(self) -> dict[str, Any]:
        return {"note": "Single timeline — flip is unused"}

    def play_id(self, step_id: str, **_kwargs: Any) -> dict[str, Any]:
        return self.client.action("play", step_id=step_id)

    def seek_relative(self, delta: int) -> dict[str, Any]:
        return self.client.action("seek_relative", delta=int(delta))

    def seek_number(self, number: int) -> dict[str, Any]:
        return self.client.action("seek_number", number=int(number))

    def seek(self, index: int) -> dict[str, Any]:
        return self.client.action("seek", index=int(index))

    def status(self) -> dict[str, Any]:
        return self.client.me()

    def current_step(self) -> dict[str, Any] | None:
        st = self.client.last_status or {}
        step = st.get("step")
        return step if isinstance(step, dict) else None


def _identity_payload(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "opus_user_name": str(config.get("opus_user_name") or "").strip(),
        "opus_flight_id": config.get("opus_flight_id"),
        "opus_seat": config.get("opus_seat"),
        "opus_flight_label": str(config.get("opus_flight_label") or "").strip(),
        "callsign_override": str(config.get("callsign_override") or "").strip(),
    }


def _radio_payload(config: dict[str, Any]) -> dict[str, Any]:
    try:
        radio = srs_radio.current_radio_state(config)
        return {
            "tuned_freqs_mhz": list(radio.freqs_mhz or []),
            "radio_fresh": bool(radio.fresh),
            "selected_mhz": radio.selected_mhz,
        }
    except Exception:
        return {"tuned_freqs_mhz": [], "radio_fresh": False, "selected_mhz": None}
