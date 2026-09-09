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
    """Synth TTS to WAV while the job waits in the channel queue."""
    try:
        if job.get("file_path") or not str(job.get("text") or "").strip():
            return
        if job.get("config", {}).get("dry_run"):
            return
        wav = atc_phrase.synthesize_tts_wav(
            job["config"],
            str(job["text"]),
            channel=job.get("channel"),
            voice_override=job.get("voice"),
            step=job.get("step"),
        )
        job["wav_path"] = wav
    except Exception as exc:  # noqa: BLE001
        job["prerender_error"] = str(exc)


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
        """Enqueue a TX job. Returns 1-based queue position on that channel."""
        ch = str(job.get("channel") or "other").strip().lower() or "other"
        if ch not in self._queues:
            ch = "other"
        job = dict(job)
        job["channel"] = ch
        job.setdefault("queued_at", time.time())
        job.setdefault("done", threading.Event())
        threading.Thread(
            target=_prerender, args=(job,), name="atc-tts-prep", daemon=True
        ).start()
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
                # Give pre-render a moment so the channel goes hot with a WAV.
                deadline = time.time() + 8.0
                while (
                    time.time() < deadline
                    and not job.get("wav_path")
                    and not job.get("prerender_error")
                    and str(job.get("text") or "").strip()
                    and not job.get("file_path")
                    and not (job.get("config") or {}).get("dry_run")
                ):
                    time.sleep(0.05)
                wav_obj = job.get("wav_path")
                wav = Path(str(wav_obj)) if wav_obj else None
                job["exit_code"] = int(self._transmit(job) or 0)
            except Exception as exc:  # noqa: BLE001
                job["error"] = str(exc)
                job["exit_code"] = 2
            finally:
                if wav is not None:
                    try:
                        wav.unlink(missing_ok=True)
                    except OSError:
                        pass
                done = job.get("done")
                if isinstance(done, threading.Event):
                    done.set()
                with self._lock:
                    self._busy[channel] = None
                q.task_done()
