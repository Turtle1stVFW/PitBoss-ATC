#!/usr/bin/env python3
"""
Offer a newer PitBoss ATC installer from GitHub Releases.

Only a published release that contains PitBossATC-Setup-….exe counts.
Commits, draft releases, and prereleases do not prompt testers.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

GITHUB_OWNER = "Turtle1stVFW"
GITHUB_REPO = "PitBoss-ATC"
USER_AGENT = "PitBossATC-UpdateCheck"
SNOOZE_KEY = "update_snooze_until"
SKIP_KEY = "update_skip_version"
SNOOZE_SECONDS = 24 * 60 * 60

_ASSET_NAME = re.compile(r"^PitBossATC-Setup-[A-Za-z0-9._+-]+\.exe$", re.IGNORECASE)
_ASSET_VERSION = re.compile(r"PitBossATC-Setup-(\d+(?:\.\d+)+)", re.IGNORECASE)
_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)

_busy = threading.Lock()
_busy_flag = False


class UpdateError(Exception):
    def __init__(self, message: str, *, quiet: bool = False) -> None:
        super().__init__(message)
        self.quiet = quiet


@dataclass(frozen=True)
class InstallerRelease:
    version: str
    version_key: tuple[int, ...]
    asset_name: str
    url: str
    notes: str
    page: str


def parse_version(text: str) -> tuple[int, ...] | None:
    """``v0.1.109``, ``0.1.109``, or ``0.1.109-abc1234`` → ``(0, 1, 109)``."""
    raw = (text or "").strip()
    if raw[:1] in {"v", "V"}:
        raw = raw[1:]
    raw = raw.split("+", 1)[0].split("-", 1)[0].strip()
    if not raw:
        return None
    parts: list[int] = []
    for piece in raw.split("."):
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    return tuple(parts) if parts else None


def version_text(key: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in key)


def allowed_download_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in _ALLOWED_HOSTS


def _notes_brief(text: str, limit: int = 500) -> str:
    lines: list[str] = []
    for line in (text or "").splitlines():
        piece = line.strip()
        if piece.startswith("#"):
            piece = piece.lstrip("#").strip()
        if piece:
            lines.append(piece)
    body = " ".join(lines)
    if len(body) > limit:
        return body[: limit - 1].rstrip() + "…"
    return body


def installer_from_release(rel: dict[str, Any]) -> InstallerRelease | None:
    """The Setup exe on one release, or None when testers should ignore it."""
    if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
        return None
    tag = str(rel.get("tag_name") or "")
    best: tuple[tuple[int, ...], int, InstallerRelease] | None = None
    for asset in rel.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if not _ASSET_NAME.match(name):
            continue
        match = _ASSET_VERSION.search(name)
        key = parse_version(match.group(1) if match else tag)
        if key is None:
            continue
        url = str(asset.get("browser_download_url") or "")
        if not allowed_download_url(url):
            continue
        try:
            size = int(asset.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        offer = InstallerRelease(
            version=version_text(key),
            version_key=key,
            asset_name=Path(name).name,
            url=url,
            notes=_notes_brief(str(rel.get("body") or "")),
            page=str(rel.get("html_url") or ""),
        )
        if best is None or key > best[0] or (key == best[0] and size > best[1]):
            best = (key, size, offer)
    return None if best is None else best[2]


def select_update(
    releases: list[dict[str, Any]],
    local_version: str,
    *,
    skip_version: str = "",
) -> InstallerRelease | None:
    """Newest published Setup exe that is newer than this install."""
    local = parse_version(local_version)
    if local is None:
        return None
    skip = parse_version(skip_version) if skip_version else None
    best: InstallerRelease | None = None
    for rel in releases:
        offer = installer_from_release(rel)
        if offer is None:
            continue
        if best is None or offer.version_key > best.version_key:
            best = offer
    if best is None or best.version_key <= local:
        return None
    if skip is not None and skip == best.version_key:
        return None
    return best


def _snoozed(config: dict[str, Any]) -> bool:
    try:
        until = float(config.get(SNOOZE_KEY) or 0)
    except (TypeError, ValueError):
        return False
    return time.time() < until


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if not allowed_download_url(newurl):
            raise urllib.error.URLError(f"blocked redirect: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_releases() -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases?per_page=20"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No published installer was found.", quiet=True) from exc
        if exc.code == 403:
            raise UpdateError("GitHub rate limit reached. Try again in a few minutes.") from exc
        raise UpdateError("Couldn't check for updates.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise UpdateError("Couldn't check for updates. Check the network and try again.") from exc
    if not isinstance(data, list):
        raise UpdateError("Couldn't check for updates.")
    return [item for item in data if isinstance(item, dict)]


def download_installer(
    offer: InstallerRelease,
    dest: Path,
    on_progress: Callable[[int, int], None],
    cancel: threading.Event,
) -> None:
    if not allowed_download_url(offer.url):
        raise UpdateError("The download link was not accepted.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    opener = urllib.request.build_opener(_RedirectGuard)
    req = urllib.request.Request(offer.url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=60) as resp, part.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                if cancel.is_set():
                    raise UpdateError("Download cancelled.", quiet=True)
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                on_progress(got, total)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise UpdateError("The installer download failed.") from exc
    if cancel.is_set():
        part.unlink(missing_ok=True)
        raise UpdateError("Download cancelled.", quiet=True)
    with part.open("rb") as handle:
        if handle.read(2) != b"MZ":
            part.unlink(missing_ok=True)
            raise UpdateError("The downloaded file is not a Windows installer.")
    part.replace(dest)


def launch_installer(path: Path) -> None:
    """Start the setup exe after a short delay so this process can exit first."""
    script = (
        "Start-Sleep -Seconds 2; "
        f"Start-Process -FilePath {json.dumps(str(path))}"
    )
    flags = 0
    flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-Command",
            script,
        ],
        creationflags=flags,
        close_fds=True,
    )


def _log(message: str, **fields: Any) -> None:
    try:
        import app_diag

        app_diag.info(app_diag.CAT_NETWORK, message, **fields)
    except Exception:
        return


def _set_busy(value: bool) -> None:
    global _busy_flag
    with _busy:
        _busy_flag = value


def _try_begin() -> bool:
    global _busy_flag
    with _busy:
        if _busy_flag:
            return False
        _busy_flag = True
        return True


def start_check(
    *,
    parent: tk.Misc,
    marshal: Callable[[Callable[[], None]], None],
    config: dict[str, Any],
    save: Callable[[], None],
    shutdown: Callable[[], None],
    local_version: str,
    manual: bool,
) -> None:
    """Background check. ``marshal`` runs the callback on the Tk thread."""
    if not manual and _snoozed(config):
        return
    if not _try_begin():
        if manual:
            marshal(lambda: messagebox.showinfo("Updates", "Already checking for an update.", parent=parent))
        return

    def work() -> None:
        try:
            releases = fetch_releases()
            offer = select_update(
                releases,
                local_version,
                skip_version="" if manual else str(config.get(SKIP_KEY) or ""),
            )
            outcome: InstallerRelease | None | UpdateError = offer
        except UpdateError as exc:
            outcome = exc
        except Exception as exc:  # noqa: BLE001 — show a plain message, log the cause
            _log("update check failed", error=str(exc))
            outcome = UpdateError("Couldn't check for updates.")

        def finish() -> None:
            try:
                _present(
                    parent,
                    outcome,
                    local_version=local_version,
                    manual=manual,
                    config=config,
                    save=save,
                    shutdown=shutdown,
                    marshal=marshal,
                )
            finally:
                _set_busy(False)

        marshal(finish)

    threading.Thread(target=work, name="pitboss-update", daemon=True).start()


def _present(
    parent: tk.Misc,
    outcome: InstallerRelease | None | UpdateError,
    *,
    local_version: str,
    manual: bool,
    config: dict[str, Any],
    save: Callable[[], None],
    shutdown: Callable[[], None],
    marshal: Callable[[Callable[[], None]], None],
) -> None:
    if isinstance(outcome, UpdateError):
        if manual:
            box = messagebox.showinfo if outcome.quiet else messagebox.showwarning
            box("Updates", str(outcome), parent=parent)
        elif not outcome.quiet:
            _log("update check failed", error=str(outcome))
        return
    if outcome is None:
        if manual:
            if parse_version(local_version) is None:
                messagebox.showinfo("Updates", "This build has no version number to compare.", parent=parent)
            else:
                messagebox.showinfo(
                    "Updates",
                    f"You're on the latest published installer ({local_version}).",
                    parent=parent,
                )
        return
    choice = _offer_dialog(parent, outcome, local_version)
    if choice == "later":
        config[SNOOZE_KEY] = time.time() + SNOOZE_SECONDS
        save()
        return
    if choice == "skip":
        config[SKIP_KEY] = outcome.version
        save()
        return
    if choice == "install":
        _download_dialog(parent, outcome, shutdown, marshal)


def _offer_dialog(parent: tk.Misc, offer: InstallerRelease, local_version: str) -> str:
    win = tk.Toplevel(parent)
    win.title("Update available")
    win.transient(parent)
    win.grab_set()
    win.resizable(False, False)
    bg = "#0c1117"
    try:
        bg = str(parent.cget("bg") or bg)
    except tk.TclError:
        pass
    win.configure(bg=bg)
    choice = {"value": "later"}

    pad = tk.Frame(win, bg=bg)
    pad.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)
    tk.Label(
        pad,
        text=f"PitBoss ATC {offer.version} is available",
        bg=bg,
        fg="#e8eef7",
        font=("Segoe UI Semibold", 12),
    ).pack(anchor="w")
    tk.Label(
        pad,
        text=(
            f"You are on {local_version}. Download the installer and run it?\n"
            "PitBoss ATC will close when the download finishes so setup can replace its files. "
            "Your settings on this PC stay."
        ),
        bg=bg,
        fg="#9aabc4",
        font=("Segoe UI", 9),
        wraplength=440,
        justify="left",
    ).pack(anchor="w", pady=(8, 8))
    if offer.notes:
        tk.Label(
            pad,
            text=offer.notes,
            bg=bg,
            fg="#e8eef7",
            font=("Segoe UI", 9),
            wraplength=440,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

    row = tk.Frame(pad, bg=bg)
    row.pack(fill=tk.X)

    def pick(value: str) -> None:
        choice["value"] = value
        win.destroy()

    ttk.Button(row, text="Download and install", command=lambda: pick("install")).pack(side=tk.RIGHT)
    ttk.Button(row, text="Later", command=lambda: pick("later")).pack(side=tk.RIGHT, padx=(0, 8))
    ttk.Button(row, text="Skip this version", command=lambda: pick("skip")).pack(side=tk.LEFT)
    win.protocol("WM_DELETE_WINDOW", lambda: pick("later"))
    win.update_idletasks()
    win.wait_window()
    return choice["value"]


def _download_dialog(
    parent: tk.Misc,
    offer: InstallerRelease,
    shutdown: Callable[[], None],
    marshal: Callable[[Callable[[], None]], None],
) -> None:
    win = tk.Toplevel(parent)
    win.title("Downloading update")
    win.transient(parent)
    win.resizable(False, False)
    bg = "#0c1117"
    try:
        bg = str(parent.cget("bg") or bg)
    except tk.TclError:
        pass
    win.configure(bg=bg)
    pad = tk.Frame(win, bg=bg)
    pad.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)
    status = tk.StringVar(value=f"Downloading {offer.asset_name}…")
    tk.Label(pad, textvariable=status, bg=bg, fg="#e8eef7", font=("Segoe UI", 10), wraplength=420).pack(anchor="w")
    bar = ttk.Progressbar(pad, length=420, mode="indeterminate")
    bar.pack(fill=tk.X, pady=(10, 0))
    bar.start(12)
    cancel = threading.Event()
    closed = {"done": False}

    dest = Path(tempfile.gettempdir()) / "PitBossATC" / offer.asset_name

    def close() -> None:
        if closed["done"]:
            return
        closed["done"] = True
        try:
            bar.stop()
        except tk.TclError:
            pass
        try:
            win.destroy()
        except tk.TclError:
            pass

    def on_cancel() -> None:
        cancel.set()
        status.set("Cancelling…")

    ttk.Button(pad, text="Cancel", command=on_cancel).pack(anchor="e", pady=(12, 0))
    win.protocol("WM_DELETE_WINDOW", on_cancel)

    last_paint = {"t": 0.0}

    def on_progress(got: int, total: int) -> None:
        now = time.monotonic()
        if now - last_paint["t"] < 0.15:
            return
        last_paint["t"] = now

        def paint(g: int = got, t: int = total) -> None:
            if closed["done"]:
                return
            if str(bar.cget("mode")) != "determinate" and t > 0:
                bar.stop()
                bar.configure(mode="determinate", maximum=max(t, 1))
            if t > 0:
                bar.configure(value=g)
                status.set(f"Downloaded {g / 1e6:.0f} / {t / 1e6:.0f} MB")
            else:
                status.set(f"Downloaded {g / 1e6:.0f} MB")

        marshal(paint)

    def work() -> None:
        try:
            download_installer(offer, dest, on_progress, cancel)
        except UpdateError as exc:
            def fail(message: str = str(exc), quiet: bool = exc.quiet) -> None:
                close()
                if not quiet:
                    messagebox.showwarning("Updates", message, parent=parent)

            marshal(fail)
            return
        except Exception as exc:  # noqa: BLE001
            _log("update download failed", error=str(exc))

            def fail_generic() -> None:
                close()
                messagebox.showwarning("Updates", "The installer download failed.", parent=parent)

            marshal(fail_generic)
            return

        def done() -> None:
            try:
                launch_installer(dest)
            except OSError as exc:
                close()
                messagebox.showerror("Updates", f"Couldn't start the installer.\n{exc}", parent=parent)
                return
            # Let the progress window close, then exit so the installer can replace files.
            parent.after(200, shutdown)
            close()

        marshal(done)

    threading.Thread(target=work, name="pitboss-update-download", daemon=True).start()
    win.wait_window()


def _self_check() -> None:
    assert parse_version("v0.1.109") == (0, 1, 109)
    assert parse_version("0.1.109-23ce961") == (0, 1, 109)
    assert parse_version("nope") is None
    assert not allowed_download_url("http://github.com/a.exe")
    assert allowed_download_url("https://github.com/Turtle1stVFW/PitBoss-ATC/releases/download/v0.1.2/PitBossATC-Setup-0.1.2.exe")
    assert not allowed_download_url("https://evil.example/setup.exe")

    def rel(tag: str, name: str, *, draft: bool = False, pre: bool = False, body: str = "") -> dict[str, Any]:
        return {
            "tag_name": tag,
            "draft": draft,
            "prerelease": pre,
            "body": body,
            "html_url": "https://github.com/Turtle1stVFW/PitBoss-ATC/releases/tag/" + tag,
            "assets": [
                {
                    "name": name,
                    "size": 10,
                    "browser_download_url": "https://github.com/Turtle1stVFW/PitBoss-ATC/releases/download/"
                    + tag
                    + "/"
                    + name,
                }
            ],
        }

    releases = [
        rel("v0.1.100", "PitBossATC-Setup-0.1.100.exe"),
        rel("v0.1.120", "notes-only.txt"),
        rel("v0.1.130", "PitBossATC-Setup-0.1.130.exe", pre=True),
        rel("v0.1.140", "PitBossATC-Setup-0.1.140.exe", draft=True),
        rel("v0.1.110", "PitBossATC-Setup-0.1.110-abc.exe", body="Radio fix"),
    ]
    offer = select_update(releases, "0.1.109")
    assert offer is not None and offer.version == "0.1.110"
    assert select_update(releases, "0.1.110") is None
    assert select_update(releases, "0.1.109", skip_version="0.1.110") is None
    assert select_update(releases, "0.1.200") is None
    print("update_check ok")


if __name__ == "__main__":
    _self_check()
