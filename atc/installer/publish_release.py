#!/usr/bin/env python3
"""
Attach dist\\PitBossATC-Setup-….exe to a GitHub Release.

Without --publish the release is a draft. Testers are prompted only after it
is published, and only when the Setup exe version is newer than theirs.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ATC = HERE.parent
ROOT = ATC.parent
sys.path.insert(0, str(ATC))

import update_check  # noqa: E402

DIST = ROOT / "dist"
REPO = f"{update_check.GITHUB_OWNER}/{update_check.GITHUB_REPO}"
_VER = re.compile(r"PitBossATC-Setup-(\d+(?:\.\d+)+)", re.IGNORECASE)


def say(text: str) -> None:
    print(text, flush=True)


def newest_exe(explicit: Path | None) -> Path:
    if explicit is not None:
        path = explicit if explicit.is_absolute() else ROOT / explicit
        if not path.is_file():
            raise SystemExit(f"Installer not found: {path}")
        return path
    found = [p for p in DIST.glob("PitBossATC-Setup-*.exe") if p.is_file()]
    if not found:
        raise SystemExit(f"No PitBossATC-Setup-*.exe in {DIST}. Run Build-Beta-Installer.cmd first.")
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0]


def exe_version(path: Path) -> str:
    match = _VER.search(path.name)
    if not match or update_check.parse_version(match.group(1)) is None:
        raise SystemExit(f"Cannot read a version from {path.name}")
    return match.group(1)


def gh(args: list[str], *, capture: bool) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *args],
            check=False,
            capture_output=capture,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("GitHub CLI (gh) is not installed or not on PATH.") from exc


def view(tag: str) -> dict | None:
    proc = gh(
        ["release", "view", tag, "--repo", REPO, "--json", "isDraft,tagName,url"],
        capture=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "") + (proc.stdout or "")
        if "release not found" in err.lower() or "not found" in err.lower():
            return None
        say(err.strip() or f"gh release view failed ({proc.returncode})")
        raise SystemExit(proc.returncode or 1)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not read gh release view output: {exc}") from exc
    return data if isinstance(data, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a PitBoss ATC installer on GitHub Releases")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish the release. Without this, testers are not prompted (draft).",
    )
    parser.add_argument("--exe", type=Path, default=None, help="Setup exe (default: newest file in dist\\)")
    parser.add_argument("--notes", default="", help="Release notes. Shown in the update prompt.")
    args = parser.parse_args(argv)

    path = newest_exe(args.exe)
    ver = exe_version(path)
    tag = f"v{ver}"
    title = f"PitBoss ATC {ver}"
    notes = args.notes.strip() or f"Installer update for PitBoss ATC {ver}."
    say(f"Installer {path.name}")
    say(f"Release  {REPO}  {tag}")

    existing = view(tag)
    if existing and existing.get("isDraft") and args.publish:
        say("Publishing the existing draft…")
        proc = gh(
            ["release", "upload", tag, str(path), "--repo", REPO, "--clobber"],
            capture=False,
        )
        if proc.returncode != 0:
            return proc.returncode
        proc = gh(
            ["release", "edit", tag, "--repo", REPO, "--draft=false", "--title", title, "--notes", notes],
            capture=False,
        )
        if proc.returncode != 0:
            return proc.returncode
        say("Published. Testers on an older build will be offered this installer.")
        return 0

    if existing and existing.get("isDraft"):
        say(f"Draft {tag} already exists. Testers will not see it.")
        say(r"Run Publish-Release.cmd --publish when you want them to.")
        return 0

    if existing:
        say(f"{tag} is already published. A running copy updates only when the version number is higher.")
        say("Build again after new commits, then publish that newer Setup exe.")
        return 1

    cmd = [
        "release",
        "create",
        tag,
        str(path),
        "--repo",
        REPO,
        "--title",
        title,
        "--notes",
        notes,
    ]
    if not args.publish:
        cmd.append("--draft")
    proc = gh(cmd, capture=False)
    if proc.returncode != 0:
        return proc.returncode
    if args.publish:
        say("Published. Testers on an older build will be offered this installer.")
    else:
        say("Draft created. Testers are not prompted yet.")
        say(r"Run Publish-Release.cmd --publish when this build should go out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
