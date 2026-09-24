"""
Per-agency SRS transmit queues.

Ground talks to one jet at a time. Tower can speak while Ground is still
transmitting because they are different frequencies.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import atc_phrase

TransmitFn = Callable[[dict[str, Any]], int]


def _default_transmit(job: dict[str, Any]) -> int:
    config = job["config"]
    airport = job["airport"]
    wav = job.get("wav_path")
    file_path = str(job.get("file_path") or "")
    radio = job.get("radio")
    if wav:
        return atc_phrase.transmit_file(
            config,
            airport,
            str(wav),
            job["tx_name"],
            job["freq"],
            job["mod"],
            radio=radio,
        )
    if file_path:
        return atc_phrase.transmit_file(
            config,
            airport,
            file_path,
            job["tx_name"],
            job["freq"],
            job["mod"],
            radio=radio,
        )
    return atc_phrase.transmit(
        config,
        airport,
        str(job.get("text") or ""),
        job["tx_name"],
        job["freq"],
        job["mod"],
        channel=job.get("channel"),
        voice_override=job.get("voice"),
        step=job.get("step"),
        radio=radio,
    )


def _prerender(job: dict[str, Any]) -> None:
    """Pre-render is disabled — ExternalAudio --file was exiting before audio
    was hearable on SRS while Fly already painted LAST HEARD. Live --text /
    Google synth (same path as Play Previous) is the reliable Host TX."""
    del job


class ChannelTxHub:
    def __init__(
        self,
        channels: list[str],
        *,
        transmit_fn: TransmitFn | None = None,
    ) -> None:
        self._transmit = transmit_fn or _default_transmit
        self._queues: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._busy: dict[str, dict[str, Any] | None] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        for ch in channels:
            key = str(ch or "other").strip().lower() or "other"
            if key in self._queues:
                continue
            q: queue.Queue[dict[str, Any]] = queue.Queue()
            self._queues[key] = q
            self._busy[key] = None
            t = threading.Thread(
                target=self._worker, args=(key, q), name=f"atc-tx-{key}", daemon=True
            )
            self._threads.append(t)
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for q in self._queues.values():
            try:
                q.put_nowait({"_stop": True})
            except Exception:
                pass

    def submit(self, job: dict[str, Any]) -> int:
        """Enqueue a TX job. Returns 1-based queue position on that channel.

        Mutates and enqueues the same dict the Host holds so run_action can
        wait on ``done`` / read ``exit_code``. A shallow copy broke that: Fly
        painted \"sent\" while ExternalAudio was still speaking (or had failed).
        """
        ch = str(job.get("channel") or "other").strip().lower() or "other"
        if ch not in self._queues:
            ch = "other"
        job["channel"] = ch
        job.setdefault("queued_at", time.time())
        job.setdefault("done", threading.Event())
        # Hub exists only on the Host PC. Pin --ip=127.0.0.1 even if Setup /
        # seat-bind left atc_role=solo or airport srs_host=showtime (that was
        # the intermittent silent-ATC path: TX logged, SRS never heard it).
        try:
            import atc_net

            atc_net.pin_host_tx_target(job)
        except Exception:
            pass
        # Do not pre-render to WAV. Queued --file TX was the silent-success
        # path (exit 0 / LAST HEARD, nothing on frequency); Play Previous
        # worked because it used a fresh live TTS launch.
        q = self._queues[ch]
        q.put(job)
        with self._lock:
            busy = self._busy.get(ch) is not None
        return q.qsize() + (1 if busy else 0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {}
            for ch, q in self._queues.items():
                speaking = self._busy.get(ch)
                out[ch] = {
                    "queued": q.qsize(),
                    "speaking": None
                    if not speaking
                    else {
                        "callsign": speaking.get("callsign") or "",
                        "session_id": speaking.get("session_id") or "",
                        "text": str(speaking.get("text") or "")[:80],
                    },
                }
            return out

    def _worker(self, channel: str, q: queue.Queue[dict[str, Any]]) -> None:
        while not self._stop.is_set():
            try:
                job = q.get(timeout=0.25)
            except queue.Empty:
                continue
            if job.get("_stop"):
                return
            with self._lock:
                self._busy[channel] = job
            wav: Path | None = None
            try:
                # Prefer live TTS (--text / Google synth). Ignore any leftover
                # prerender WAV so we never take the silent --file path.
                job.pop("wav_path", None)
                raw = self._transmit(job)
                try:
                    code = int(raw) if raw is not None else 2
                except (TypeError, ValueError):
                    code = 2
                # ExternalAudio sometimes exits non-zero on a transient SRS
                # glitch while the next identical launch works. One retry.
                if code != 0 and str(job.get("text") or "").strip():
                    try:
                        import app_diag

                        app_diag.warn(
                            app_diag.CAT_TX,
                            "channel TX retry after non-zero exit",
                            channel=channel,
                            exit_code=code,
                        )
                    except Exception:
                        pass
                    time.sleep(0.5)
                    job.pop("wav_path", None)
                    raw = self._transmit(job)
                    try:
                        code = int(raw) if raw is not None else 2
                    except (TypeError, ValueError):
                        code = 2
                job["exit_code"] = code
                try:
                    import app_diag

                    code = int(job.get("exit_code") or 0)
                    if code != 0:
                        app_diag.error(
                            app_diag.CAT_TX,
                            "channel TX non-zero exit",
                            channel=channel,
                            exit_code=code,
                            prerender_error=str(job.get("prerender_error") or ""),
                        )
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                job["error"] = str(exc)
                job["exit_code"] = 2
                try:
                    import app_diag

                    app_diag.error(
                        app_diag.CAT_TX,
                        f"channel TX worker failed: {exc}",
                        channel=channel,
                    )
                except Exception:
                    pass
            finally:
                if wav is not None:
                    try:
                        wav.unlink(missing_ok=True)
                    except OSError:
                        pass
                done = job.get("done")
                if isinstance(done, threading.Event):
                    done.set()
                # Confirm speech end only when LAST HEARD was already stamped
                # (solo / non-deferred). Host deferred TX leaves last_tx_text
                # empty until run_action.commit_deferred_tx_success after exit 0
                # — painting confirmed here early made Clients show TX success
                # before the phrase was committed (or after a silent wrong-SRS).
                flow_state = job.get("flow_state")
                if isinstance(flow_state, dict):
                    try:
                        ok = int(job.get("exit_code") or 0) == 0 and not job.get("error")
                    except (TypeError, ValueError):
                        ok = False
                    if ok and str(flow_state.get("last_tx_text") or "").strip():
                        try:
                            import atc_phrase as atc_phrase_mod

                            atc_phrase_mod.note_tx_finished(flow_state)
                        except Exception:
                            flow_state["last_tx_end_at"] = time.time()
                            flow_state["last_tx_confirmed"] = True
                with self._lock:
                    self._busy[channel] = None
                q.task_done()
