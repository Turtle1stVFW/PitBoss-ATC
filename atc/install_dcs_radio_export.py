#!/usr/bin/env python3
"""
Install ATC-RadioExport.lua into DCS Saved Games profiles and patch Export.lua.

Idempotent — safe to run from Setup UI, CLI, or a future Windows installer.

  py -3 install_dcs_radio_export.py
  py -3 install_dcs_radio_export.py --status
  py -3 install_dcs_radio_export.py --uninstall
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SOURCE_LUA = HERE / "dcs" / "ATC-RadioExport.lua"
LUA_NAME = "ATC-RadioExport.lua"

# Unique markers so we never duplicate or clobber the SRS Export.lua line.
MARKER_BEGIN = "-- ATC ExternalAudio radio gate (begin)"
MARKER_END = "-- ATC ExternalAudio radio gate (end)"
EXPORT_BLOCK = (
    f"{MARKER_BEGIN}\n"
    "pcall(function() local lfs=require('lfs'); "
    "dofile(lfs.writedir()..[[Scripts\\ATC-RadioExport.lua]]) end)\n"
    f"{MARKER_END}\n"
)

# Also detect older one-liner installs without markers.
LEGACY_SNIPPET = "ATC-RadioExport.lua"

PROFILE_NAMES = ("DCS", "DCS.openbeta", "DCS.openalpha")


def saved_games_root() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / "Saved Games"


def dcs_profiles(*, only_existing: bool = True) -> list[Path]:
    root = saved_games_root()
    out: list[Path] = []
    for name in PROFILE_NAMES:
        path = root / name
        if only_existing and not path.is_dir():
            continue
        out.append(path)
    return out


def _strip_our_block(text: str) -> str:
    """Remove marked block and any legacy one-liner that loads our script."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == MARKER_BEGIN:
            skipping = True
            continue
        if stripped == MARKER_END:
            skipping = False
            continue
        if skipping:
            continue
        if LEGACY_SNIPPET in line and "dofile" in line and "ATC ExternalAudio" not in line:
            # Drop bare legacy installs of our script only
            if "ATC-RadioExport" in line:
                continue
        out.append(line)
    return "".join(out)


def _export_has_our_hook(text: str) -> bool:
    return MARKER_BEGIN in text or (
        LEGACY_SNIPPET in text and "dofile" in text and "ATC-RadioExport" in text
    )


def install_profile(profile: Path, *, source_lua: Path = SOURCE_LUA) -> dict[str, Any]:
    """Copy Lua + ensure Export.lua hook. Returns a result dict."""
    result: dict[str, Any] = {
        "profile": str(profile),
        "ok": False,
        "copied": False,
        "export_created": False,
        "export_patched": False,
        "already": False,
        "error": "",
    }
    if not source_lua.is_file():
        result["error"] = f"Missing source script: {source_lua}"
        return result

    scripts = profile / "Scripts"
    try:
        scripts.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result["error"] = f"Cannot create Scripts: {exc}"
        return result

    dest_lua = scripts / LUA_NAME
    try:
        shutil.copy2(source_lua, dest_lua)
        result["copied"] = True
    except OSError as exc:
        result["error"] = f"Cannot copy {LUA_NAME}: {exc}"
        return result

    export_path = scripts / "Export.lua"
    try:
        if export_path.is_file():
            raw = export_path.read_text(encoding="utf-8", errors="replace")
        else:
            raw = ""
            result["export_created"] = True
    except OSError as exc:
        result["error"] = f"Cannot read Export.lua: {exc}"
        return result

    if _export_has_our_hook(raw) and MARKER_BEGIN in raw:
        # Refresh Lua file is enough; hook already present with markers
        result["already"] = True
        result["ok"] = True
        return result

    cleaned = _strip_our_block(raw)
    if cleaned and not cleaned.endswith("\n"):
        cleaned += "\n"
    if cleaned and not cleaned.endswith("\n\n"):
        # Keep a blank line before our block when appending to existing content
        if not cleaned.endswith("\n"):
            cleaned += "\n"
        cleaned += "\n"
    new_text = cleaned + EXPORT_BLOCK
    try:
        export_path.write_text(new_text, encoding="utf-8", newline="\n")
    except OSError as exc:
        result["error"] = f"Cannot write Export.lua: {exc}"
        return result

    result["export_patched"] = True
    result["ok"] = True
    return result


def uninstall_profile(profile: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile": str(profile),
        "ok": False,
        "removed_lua": False,
        "export_cleaned": False,
        "error": "",
    }
    scripts = profile / "Scripts"
    dest_lua = scripts / LUA_NAME
    if dest_lua.is_file():
        try:
            dest_lua.unlink()
            result["removed_lua"] = True
        except OSError as exc:
            result["error"] = f"Cannot remove {LUA_NAME}: {exc}"
            return result

    export_path = scripts / "Export.lua"
    if export_path.is_file():
        try:
            raw = export_path.read_text(encoding="utf-8", errors="replace")
            cleaned = _strip_our_block(raw)
            if cleaned != raw:
                export_path.write_text(cleaned, encoding="utf-8", newline="\n")
                result["export_cleaned"] = True
        except OSError as exc:
            result["error"] = f"Cannot clean Export.lua: {exc}"
            return result

    result["ok"] = True
    return result


def status_profile(profile: Path) -> dict[str, Any]:
    scripts = profile / "Scripts"
    dest_lua = scripts / LUA_NAME
    export_path = scripts / "Export.lua"
    hooked = False
    if export_path.is_file():
        try:
            hooked = _export_has_our_hook(
                export_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            hooked = False
    return {
        "profile": str(profile),
        "lua_present": dest_lua.is_file(),
        "export_hooked": hooked,
        "ready": dest_lua.is_file() and hooked,
    }


def install_all(*, source_lua: Path | None = None) -> list[dict[str, Any]]:
    src = source_lua or SOURCE_LUA
    profiles = dcs_profiles(only_existing=True)
    if not profiles:
        # Create under default DCS profile so first launch has somewhere to land
        default = saved_games_root() / "DCS"
        default.mkdir(parents=True, exist_ok=True)
        profiles = [default]
    return [install_profile(p, source_lua=src) for p in profiles]


def uninstall_all() -> list[dict[str, Any]]:
    return [uninstall_profile(p) for p in dcs_profiles(only_existing=True)]


def status_all() -> list[dict[str, Any]]:
    return [status_profile(p) for p in dcs_profiles(only_existing=True)]


def format_install_report(results: list[dict[str, Any]]) -> str:
    if not results:
        return "No DCS Saved Games profiles found."
    lines: list[str] = []
    for r in results:
        name = Path(r["profile"]).name
        if r.get("error"):
            lines.append(f"{name}: FAILED — {r['error']}")
            continue
        bits = []
        if r.get("already"):
            bits.append("already installed")
        if r.get("copied"):
            bits.append("script copied")
        if r.get("export_created"):
            bits.append("Export.lua created")
        if r.get("export_patched"):
            bits.append("Export.lua patched")
        if r.get("removed_lua"):
            bits.append("script removed")
        if r.get("export_cleaned"):
            bits.append("Export.lua cleaned")
        lines.append(f"{name}: " + (", ".join(bits) if bits else "ok"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install ATC DCS radio export for the freq gate.")
    parser.add_argument("--status", action="store_true", help="Show install status only")
    parser.add_argument("--uninstall", action="store_true", help="Remove script + Export.lua hook")
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Override path to ATC-RadioExport.lua (installer staging dir)",
    )
    args = parser.parse_args(argv)

    if args.status:
        rows = status_all()
        if not rows:
            print("No DCS Saved Games profiles found.")
            return 1
        for row in rows:
            flag = "READY" if row["ready"] else "MISSING"
            print(
                f"{Path(row['profile']).name}: {flag}  "
                f"(lua={row['lua_present']} hook={row['export_hooked']})"
            )
        return 0 if all(r["ready"] for r in rows) else 2

    if args.uninstall:
        results = uninstall_all()
        print(format_install_report(results))
        return 0 if all(r.get("ok") for r in results) else 1

    results = install_all(source_lua=args.source)
    print(format_install_report(results))
    ok = all(r.get("ok") for r in results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
