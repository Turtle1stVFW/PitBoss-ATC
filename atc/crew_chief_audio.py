"""
Local-only Virtual Crew Chief playback.

Never calls ExternalAudio / SRS. TTS uses the existing Windows / Google
preview path. Optional Bogey Dope-style sound pack is WAV (or OGG next to
a converted WAV) under atc/crew_chief/sounds/.
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LINES_PATH = HERE / "crew_chief_lines.json"
SOUNDS_DIR = HERE / "crew_chief" / "sounds"

_lines_cache: dict[str, Any] | None = None


def load_lines(path: Path | None = None) -> dict[str, Any]:
    global _lines_cache
    src = path or LINES_PATH
    if _lines_cache is not None and path is None:
        return _lines_cache
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if path is None:
        _lines_cache = data
    return data


def reset_lines_cache() -> None:
    global _lines_cache
    _lines_cache = None


def line_text(line_id: str, *, lines: dict[str, Any] | None = None) -> str:
    catalog = lines if lines is not None else load_lines()
    entry = catalog.get(line_id)
    if isinstance(entry, dict):
        return str(entry.get("text") or "").strip()
    return ""


def _pack_files(line_id: str, *, lines: dict[str, Any] | None = None) -> list[str]:
    catalog = lines if lines is not None else load_lines()
    entry = catalog.get(line_id)
    if not isinstance(entry, dict):
        return []
    raw = entry.get("files") or []
    if isinstance(raw, str):
        return [raw]
    return [str(p) for p in raw if p]


def resolve_pack_wav(line_id: str, *, sounds_dir: Path | None = None) -> Path | None:
    root = sounds_dir or SOUNDS_DIR
    if not root.is_dir():
        return None
    for rel in _pack_files(line_id):
        rel = rel.replace("\\", "/").lstrip("/")
        ogg = root / rel
        wav = ogg.with_suffix(".wav")
        if wav.is_file():
            return wav
        # Some extracts already land as wav with the original stem.
        alt = root / Path(rel).with_suffix(".wav")
        if alt.is_file():
            return alt
    return None


def crew_chief_voice(config: dict[str, Any] | None) -> str:
    cfg = config or {}
    voices = cfg.get("tts_voices") if isinstance(cfg.get("tts_voices"), dict) else {}
    named = str(voices.get("crew_chief") or "").strip()
    if named:
        return named
    return str(cfg.get("tts_voice") or voices.get("default") or "").strip()


def use_sound_pack(config: dict[str, Any] | None) -> bool:
    mode = str((config or {}).get("crew_chief_voice") or "tts").strip().lower()
    return mode in {"pack", "sounds", "ogg", "file"}


def play_line(
    line_id: str,
    config: dict[str, Any] | None = None,
    *,
    blocking: bool = False,
    lines: dict[str, Any] | None = None,
    sounds_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Play one crew-chief line on local speakers.

    Returns {action, line_id, text, source} — action is "play", "silent", or "error".
    """
    text = line_text(line_id, lines=lines)
    if not line_id:
        return {"action": "silent", "line_id": line_id, "text": "", "source": ""}
    cfg = config or {}
    if bool(cfg.get("dry_run")):
        return {"action": "play", "line_id": line_id, "text": text, "source": "dry_run"}

    pack_wav: Path | None = None
    if use_sound_pack(cfg):
        pack_wav = resolve_pack_wav(line_id, sounds_dir=sounds_dir)

    def _run() -> dict[str, Any]:
        if pack_wav is not None:
            try:
                import winsound

                winsound.PlaySound(str(pack_wav), winsound.SND_FILENAME)
                return {
                    "action": "play",
                    "line_id": line_id,
                    "text": text,
                    "source": "pack",
                }
            except Exception as exc:  # noqa: BLE001
                if not text:
                    return {
                        "action": "error",
                        "line_id": line_id,
                        "text": "",
                        "source": "pack",
                        "detail": str(exc),
                    }
        if not text:
            return {"action": "silent", "line_id": line_id, "text": "", "source": "tts"}
        try:
            import atc_phrase

            creds = str(cfg.get("google_credentials") or "").strip() or None
            provider = str(cfg.get("tts_provider") or "windows").strip().lower()
            google = creds if provider == "google" else None
            atc_phrase.preview_voice_local(
                crew_chief_voice(cfg),
                text,
                volume=float(cfg.get("tts_volume") or 0.8),
                speed=cfg.get("tts_speed"),
                google_credentials=google,
            )
            return {"action": "play", "line_id": line_id, "text": text, "source": "tts"}
        except Exception as exc:  # noqa: BLE001
            return {
                "action": "error",
                "line_id": line_id,
                "text": text,
                "source": "tts",
                "detail": str(exc),
            }

    if blocking:
        return _run()
    threading.Thread(target=_run, daemon=True).start()
    source = "pack" if pack_wav is not None else "tts"
    return {"action": "play", "line_id": line_id, "text": text, "source": source}


def pick_line_variant(line_id: str, *, lines: dict[str, Any] | None = None) -> str:
    """Same id; random file choice is resolved at play time."""
    files = _pack_files(line_id, lines=lines)
    if files:
        random.choice(files)
    return line_id
