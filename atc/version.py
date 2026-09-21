#!/usr/bin/env python3
"""
PitBoss ATC build identity.

The last number is the git commit count, so it moves on its own. ``atc/VERSION``
is only the major.minor prefix (``0.1``). ``Pack-Share-Zip`` freezes the number
and commit into the zip so a pilot PC (no .git) still shows that build.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
_VERSION_FILE = HERE / "VERSION"
_BUILD_FILE = HERE / "BUILD"


def _prefix() -> str:
    """Major.minor from atc/VERSION. The patch is not stored here."""
    try:
        text = _VERSION_FILE.read_text(encoding="utf-8")
    except OSError:
        return "0.1"
    token = ""
    for line in text.splitlines():
        piece = line.strip().split()[0] if line.strip() else ""
        if piece and not piece.startswith("#"):
            token = piece
            break
    parts = [p for p in token.split(".") if p.isdigit()]
    if len(parts) >= 2:
        return f"{int(parts[0])}.{int(parts[1])}"
    if len(parts) == 1:
        return f"{int(parts[0])}.0"
    return "0.1"


def _git_run(args: list[str]) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(HERE),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def _git_info() -> tuple[str, int] | None:
    """(short commit, commit count) when this tree is a git checkout."""
    rev = _git_run(["rev-parse", "--short", "HEAD"])
    count_s = _git_run(["rev-list", "--count", "HEAD"])
    if not rev or not count_s.isdigit():
        return None
    if not all(c in "0123456789abcdef" for c in rev.lower()):
        return None
    return rev, int(count_s)


def _read_build() -> tuple[str, str]:
    """(version, revision) frozen into a tester zip, or ('', '')."""
    try:
        text = _BUILD_FILE.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    ver = ""
    rev = ""
    for line in text.splitlines():
        key, _, value = line.strip().partition("=")
        if key == "version":
            ver = value.strip()
        elif key == "revision":
            rev = value.strip()
    if rev and not all(c in "0123456789abcdef" for c in rev.lower()):
        rev = ""
    return ver, rev


@lru_cache(maxsize=1)
def _resolved() -> tuple[str, str]:
    """(version, revision). Git wins so a checkout stays current."""
    git = _git_info()
    if git is not None:
        rev, count = git
        return f"{_prefix()}.{count}", rev
    ver, rev = _read_build()
    if ver:
        return ver, rev
    return _prefix(), ""


def version() -> str:
    """``0.1.184`` — prefix plus how many commits are in this checkout."""
    return _resolved()[0]


def revision() -> str:
    """Short commit for this build, or '' when it cannot be known."""
    return _resolved()[1]


def label() -> str:
    """Machine-readable build: ``0.1.184`` or ``0.1.184+abc1234``."""
    ver, rev = _resolved()
    return f"{ver}+{rev}" if rev else ver


def display() -> str:
    """Title-bar form: ``0.1.184 (abc1234)`` or ``0.1.184``."""
    ver, rev = _resolved()
    return f"{ver} ({rev})" if rev else ver
