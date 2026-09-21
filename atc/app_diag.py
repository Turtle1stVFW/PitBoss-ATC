"""
Application troubleshooting log for PitBoss ATC.

Records launch checkpoints, library loads, connectivity results, and meaningful
failures. Intentionally omits ATC↔pilot radio call content (TX phrase text,
MIC transcripts). Safe to mirror to a local file for support / debugging.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

LEVEL_DEBUG = "DEBUG"
LEVEL_INFO = "INFO"
LEVEL_WARN = "WARN"
LEVEL_ERROR = "ERROR"

# Categories used by callers (free-form strings are also accepted).
CAT_LAUNCH = "launch"
CAT_LIBRARY = "library"
CAT_NETWORK = "network"
CAT_TX = "tx"
CAT_VOICE = "voice"
CAT_CONFIG = "config"
CAT_RUNTIME = "runtime"
CAT_HTTP = "http"

_MAX_MEMORY = 2000
_MAX_FILE_BYTES = 2_000_000
_LOCK = threading.RLock()
_ENTRIES: deque[DiagEntry] = deque(maxlen=_MAX_MEMORY)
_LISTENERS: list[Callable[[DiagEntry], None]] = []
_TRANSITIONS: dict[str, str] = {}
_STARTED = False
_LOG_PATH: Path | None = None


@dataclass(frozen=True)
class DiagEntry:
    ts: float
    level: str
    category: str
    message: str
    fields: dict[str, Any] = field(default_factory=dict)

    def format_line(self) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts))
        ms = int((self.ts % 1) * 1000)
        bits = [f"{stamp}.{ms:03d}", self.level, self.category, self.message]
        if self.fields:
            extras = " ".join(f"{k}={_fmt_field(v)}" for k, v in sorted(self.fields.items()))
            bits.append(extras)
        return " ".join(bits)


def _fmt_field(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 1000 else f"{value:.1f}"
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > 240:
        text = text[:237] + "..."
    if " " in text or "=" in text:
        return repr(text)
    return text


def log_path() -> Path:
    """Persistent log file (LOCALAPPDATA on Windows, else beside the package)."""
    global _LOG_PATH
    if _LOG_PATH is not None:
        return _LOG_PATH
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        folder = Path(base) / "PitBossATC"
    else:
        folder = HERE / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        folder = Path(os.environ.get("TEMP") or HERE) / "PitBossATC"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            folder = HERE
    _LOG_PATH = folder / "app.log"
    return _LOG_PATH


def _build_label() -> str:
    try:
        import version

        return version.label()
    except Exception:
        return ""


def ensure_started() -> None:
    """Idempotent session banner so the file has a clear launch boundary."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    info(
        CAT_LAUNCH,
        "diagnostics session start",
        path=str(log_path()),
        version=_build_label(),
    )


def subscribe(callback: Callable[[DiagEntry], None]) -> None:
    with _LOCK:
        _LISTENERS.append(callback)


def unsubscribe(callback: Callable[[DiagEntry], None]) -> None:
    with _LOCK:
        try:
            _LISTENERS.remove(callback)
        except ValueError:
            pass


def clear() -> None:
    with _LOCK:
        _ENTRIES.clear()
        _TRANSITIONS.clear()


def snapshot(*, min_level: str = LEVEL_DEBUG) -> list[DiagEntry]:
    rank = _level_rank(min_level)
    with _LOCK:
        return [e for e in list(_ENTRIES) if _level_rank(e.level) >= rank]


def format_text(*, min_level: str = LEVEL_DEBUG, limit: int | None = None) -> str:
    entries = snapshot(min_level=min_level)
    if limit is not None and limit > 0:
        entries = entries[-limit:]
    return "\n".join(e.format_line() for e in entries)


def log(
    level: str,
    category: str,
    message: str,
    *,
    also_stderr: bool = False,
    **fields: Any,
) -> DiagEntry:
    ensure_started()
    entry = DiagEntry(
        ts=time.time(),
        level=str(level or LEVEL_INFO).upper(),
        category=str(category or CAT_RUNTIME).strip() or CAT_RUNTIME,
        message=_scrub_message(str(message or "").strip() or "(empty)"),
        fields=_scrub_fields(fields),
    )
    listeners: list[Callable[[DiagEntry], None]]
    with _LOCK:
        _ENTRIES.append(entry)
        listeners = list(_LISTENERS)
    _append_file(entry)
    if also_stderr:
        try:
            import sys

            print(entry.format_line(), file=sys.stderr)
        except Exception:
            pass
    for cb in listeners:
        try:
            cb(entry)
        except Exception:
            pass
    return entry


def debug(category: str, message: str, **fields: Any) -> DiagEntry:
    return log(LEVEL_DEBUG, category, message, **fields)


def info(category: str, message: str, **fields: Any) -> DiagEntry:
    return log(LEVEL_INFO, category, message, **fields)


def warn(category: str, message: str, **fields: Any) -> DiagEntry:
    if "version" not in fields:
        fields["version"] = _build_label()
    return log(LEVEL_WARN, category, message, **fields)


def error(category: str, message: str, **fields: Any) -> DiagEntry:
    if "version" not in fields:
        fields["version"] = _build_label()
    return log(LEVEL_ERROR, category, message, **fields)


def checkpoint(name: str, **fields: Any) -> DiagEntry:
    """Launch / lifecycle milestone."""
    return info(CAT_LAUNCH, f"checkpoint: {name}", **fields)


def note_transition(
    key: str,
    state: str,
    message: str,
    *,
    category: str = CAT_NETWORK,
    level: str = LEVEL_INFO,
    **fields: Any,
) -> DiagEntry | None:
    """Log only when `state` changes for `key` (e.g. NET LINK ok→fail)."""
    state_s = str(state)
    with _LOCK:
        prev = _TRANSITIONS.get(key)
        if prev == state_s:
            return None
        _TRANSITIONS[key] = state_s
    payload = dict(fields)
    payload["state"] = state_s
    if prev is not None:
        payload["prev"] = prev
    return log(level, category, message, **payload)


def safe_url(url: str) -> str:
    """Host + path only — drop query/fragment that might carry tokens."""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
        host = parts.netloc or "(no-host)"
        path = parts.path or "/"
        return f"{parts.scheme or 'http'}://{host}{path}"
    except Exception:
        return "(bad-url)"


def scrub_external_audio_cmd(cmd: list[str] | tuple[str, ...]) -> str:
    """Join ExternalAudio argv with phrase text redacted."""
    out: list[str] = []
    for arg in cmd:
        s = str(arg)
        if s.startswith("--text="):
            out.append("--text=<redacted>")
        elif s.startswith("--file="):
            try:
                out.append(f"--file={Path(s.split('=', 1)[1]).name}")
            except Exception:
                out.append("--file=<redacted>")
            continue
        else:
            out.append(s)
    return " ".join(out)


def _level_rank(level: str) -> int:
    return {
        LEVEL_DEBUG: 10,
        LEVEL_INFO: 20,
        LEVEL_WARN: 30,
        LEVEL_ERROR: 40,
    }.get(str(level or "").upper(), 0)


_SECRET_KEYS = frozenset(
    {
        "token",
        "atc_token",
        "password",
        "secret",
        "credentials",
        "google_credentials",
        "private_key",
        "transcript",
        "text",
        "spoken",
        "phrase",
        "normalized",
    }
)


def _scrub_fields(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        k = str(key)
        if k.casefold() in _SECRET_KEYS or any(
            s in k.casefold() for s in ("token", "secret", "password", "transcript", "phrase")
        ):
            continue
        if k.casefold() in {"url", "metar_url", "opus_url"}:
            clean[k] = safe_url(str(value))
        elif k.casefold() in {"cmd", "command"} and isinstance(value, (list, tuple)):
            clean[k] = scrub_external_audio_cmd(value)
        elif k.casefold() in {"cmd", "command"} and isinstance(value, str):
            # Best-effort redact --text=... in a joined command string.
            parts = []
            for bit in value.split():
                if bit.startswith("--text="):
                    parts.append("--text=<redacted>")
                else:
                    parts.append(bit)
            clean[k] = " ".join(parts)
        else:
            clean[k] = value
    return clean


def _scrub_message(message: str) -> str:
    # Never leave raw --text= payloads in free-form messages.
    if "--text=" not in message:
        return message
    bits = []
    for bit in message.split():
        if bit.startswith("--text="):
            bits.append("--text=<redacted>")
        else:
            bits.append(bit)
    return " ".join(bits)


def _append_file(entry: DiagEntry) -> None:
    path = log_path()
    line = entry.format_line() + "\n"
    try:
        if path.is_file() and path.stat().st_size > _MAX_FILE_BYTES:
            # Keep the tail so recent troubleshooting context survives.
            raw = path.read_bytes()
            keep = raw[-(_MAX_FILE_BYTES // 2) :]
            path.write_bytes(b"... truncated ...\n" + keep)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
