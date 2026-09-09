#!/usr/bin/env python3
"""
Build a tester zip that omits local secrets and runtime state.

Run from atc\\Pack-Share-Zip.cmd. Output lands next to the repo folder.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

SKIP_DIR_NAMES = {
    ".git",
    ".vs",
    ".idea",
    "__pycache__",
    ".cursor",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".bak", ".user"}
SKIP_NAMES = {
    "config.json",
    "flow_state.json",
    "tts_usage.json",
    "ownship_inject.json",
    "ownship_inject.json.tmp",
    "weather_inject.json",
    "weather_inject.json.tmp",
    "traffic_inject.json",
    "traffic_inject.json.tmp",
    "state.json",
    "thumbs.db",
    ".ds_store",
}


def _skip(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    if parts & {n.lower() for n in SKIP_DIR_NAMES}:
        return True
    if path.name.lower() in {n.lower() for n in SKIP_NAMES}:
        return True
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    # Real Google / Azure keys only. Keep secrets/README.txt.
    if path.parent.name.lower() == "secrets" and path.suffix.lower() == ".json":
        return True
    return False


def iter_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if _skip(rel):
            continue
        files.append(path)
    return files


def pack(dest: Path | None = None) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d")
    out = dest or (ROOT.parent / f"PitBoss-ATC-Share-{stamp}.zip")
    files = iter_files()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(ROOT).as_posix())
    return out


def main() -> int:
    out = pack()
    print(f"Wrote {out}")
    print("Send that zip plus PILOT-SETUP.md, the ATC hostname, and the shared token.")
    print("Do not add config.json or atc/secrets/*.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
