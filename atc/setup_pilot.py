#!/usr/bin/env python3
"""
First-time machine setup for a tester or Host.

Creates config.json, installs voice packages, installs the DCS radio-export
hook, and optionally warms the Whisper model so flight night is offline-safe.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE / "config.example.json"
CONFIG = HERE / "config.json"
VOICE_REQ = HERE / "requirements-voice.txt"

# Shown when Setup-Pilot.cmd finishes so the operator can read it back.
NEXT_STEPS = """
Next (on this PC)
  1. PitBoss ATC should open. Use the First-run setup window if it appears.
  2. Opus username = your CAOC name. Client = squadron hop. Solo = this PC only.
  3. Clients: ATC address + shared token from the Host (port 8766, not SRS 5002).
  4. Title bar: pick your Opus flight. Restart DCS once if the radio export just installed.

Ask the Host for the address and token only — never the Google JSON key.
"""


def ensure_config() -> tuple[bool, str]:
    if CONFIG.is_file():
        return True, f"config.json already present ({CONFIG.name})"
    if not EXAMPLE.is_file():
        return False, "config.example.json is missing from the atc folder"
    try:
        shutil.copyfile(EXAMPLE, CONFIG)
    except OSError as exc:
        return False, f"could not write config.json: {exc}"
    return True, "created config.json from config.example.json"


def voice_imports_ok() -> tuple[bool, str]:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False, "numpy is not installed"
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, "faster-whisper is not installed"
    return True, "voice packages imported (numpy, faster-whisper)"


def install_voice_packages(*, warm_model: bool = False) -> tuple[bool, str]:
    ok, msg = voice_imports_ok()
    notes = [msg]
    if not ok:
        if not VOICE_REQ.is_file():
            return False, f"missing {VOICE_REQ.name}"
        print(f"Installing voice packages from {VOICE_REQ.name} …", flush=True)
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(VOICE_REQ)],
            check=False,
        )
        if proc.returncode != 0:
            return False, "pip install failed — check the log above (internet required once)"
        ok, msg = voice_imports_ok()
        notes = [msg]
        if not ok:
            return False, msg
    if warm_model:
        print("Warming Whisper base.en (~150 MB, first time only) …", flush=True)
        try:
            from faster_whisper import WhisperModel

            WhisperModel("base.en", device="cpu", compute_type="int8")
            notes.append("Whisper base.en is cached")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Whisper warm-up skipped: {exc}")
    return True, "; ".join(notes)


def install_radio_export() -> tuple[bool, str]:
    import install_dcs_radio_export

    results = install_dcs_radio_export.install_all()
    report = install_dcs_radio_export.format_install_report(results)
    ok = all(r.get("ok") for r in results) if results else True
    if not results:
        return True, "no DCS Saved Games profile yet — export hook will wait until DCS has run"
    return ok, report


def _line(ok: bool, text: str) -> str:
    mark = "ok" if ok else "FAIL"
    return f"  [{mark}] {text}"


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="First-time ATC pilot / Host machine setup")
    parser.add_argument(
        "--skip-voice",
        action="store_true",
        help="Do not pip-install faster-whisper / numpy",
    )
    parser.add_argument(
        "--skip-whisper-warm",
        action="store_true",
        help="Install packages but do not download the Whisper model yet",
    )
    parser.add_argument(
        "--skip-radio-export",
        action="store_true",
        help="Do not install the DCS Export.lua hook",
    )
    args = parser.parse_args(argv)

    print()
    print("=== ATC machine setup (testing) ===")
    print(f"Python  {sys.version.split()[0]}  ({sys.executable})")
    print()
    try:
        import app_diag

        app_diag.checkpoint(
            "setup_pilot_start",
            python=sys.version.split()[0],
        )
    except Exception:
        pass

    failed = False

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print(_line(False, "tkinter missing — reinstall Python and enable tcl/tk and IDLE"))
        failed = True
        try:
            import app_diag

            app_diag.error(app_diag.CAT_LIBRARY, "tkinter missing")
        except Exception:
            pass
    else:
        print(_line(True, "tkinter available"))
        try:
            import app_diag

            app_diag.info(app_diag.CAT_LIBRARY, "tkinter available")
        except Exception:
            pass

    ok, msg = ensure_config()
    print(_line(ok, msg))
    failed = failed or (not ok)
    try:
        import app_diag

        (app_diag.info if ok else app_diag.error)(app_diag.CAT_CONFIG, msg)
    except Exception:
        pass

    if args.skip_voice:
        print(_line(True, "voice packages skipped (--skip-voice)"))
    else:
        ok, msg = install_voice_packages(warm_model=not args.skip_whisper_warm)
        print(_line(ok, msg))
        failed = failed or (not ok)
        try:
            import app_diag

            (app_diag.info if ok else app_diag.error)(app_diag.CAT_LIBRARY, msg)
        except Exception:
            pass

    if args.skip_radio_export:
        print(_line(True, "DCS radio export skipped (--skip-radio-export)"))
    else:
        ok, msg = install_radio_export()
        print(_line(ok, msg))
        if not ok:
            failed = True
        try:
            import app_diag

            (app_diag.info if ok else app_diag.warn)(app_diag.CAT_CONFIG, msg)
        except Exception:
            pass

    print(NEXT_STEPS)
    try:
        import app_diag

        app_diag.checkpoint("setup_pilot_done", failed=failed)
    except Exception:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
