#!/usr/bin/env python3
"""
Build PitBossATC-Setup.exe for open-beta testers.

The installer carries a private CPython, numpy, faster-whisper, and the
base.en weights. Testers do not install Python or run pip.

Output: <repo>\\dist\\PitBossATC-Setup-<version>.exe

Run from atc\\Build-Beta-Installer.cmd. Needs network on the build machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ATC = HERE.parent
ROOT = ATC.parent
sys.path.insert(0, str(ATC))

import pack_share  # noqa: E402
import version  # noqa: E402

DIST = ROOT / "dist"
STAGING = DIST / "staging"
CACHE = DIST / "cache"
ISS = HERE / "PitBossATC.iss"
DEFINES = HERE / "build_defines.iss"
VOICE_REQ = ATC / "requirements-voice.txt"
USER_AGENT = "PitBossATC-InstallerBuild"

# Plain x64 wheels. Skip freethreaded and x86-64-v3 builds.
_PY_SUFFIX = "-x86_64-pc-windows-msvc-install_only.tar.gz"
_PY_MAJORS = ((3, 12), (3, 11), (3, 13))


def say(text: str) -> None:
    print(text, flush=True)


def _on_rm_error(func, path, exc):  # noqa: ANN001
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise exc from None


def rmtree(path: Path) -> None:
    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_on_rm_error)
    else:
        shutil.rmtree(path, onerror=_on_rm_error)


def download(url: str, dest: Path, expected: int | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and expected and dest.stat().st_size == expected:
        say(f"  cached {dest.name}")
        return
    if dest.is_file() and expected is None and dest.stat().st_size > 0:
        # Caller can still force a refresh by deleting the cache file.
        say(f"  cached {dest.name}")
        return
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    say(f"  downloading {dest.name}")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, tmp.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            last = 0.0
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                now = time.monotonic()
                if total and now - last > 2:
                    say(f"    {dest.name}  {got / 1e6:.0f} / {total / 1e6:.0f} MB")
                    last = now
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"Download failed: {url}\n{exc}") from exc
    tmp.replace(dest)


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not fetch {url}\n{exc}") from exc


def _cpython_version(name: str) -> tuple[int, ...] | None:
    base = name.split("+", 1)[0]
    if not base.startswith("cpython-") or "freethreaded" in name:
        return None
    parts: list[int] = []
    for piece in base[len("cpython-") :].split("."):
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    return tuple(parts) if len(parts) >= 2 else None


def pick_python_asset(release: dict) -> dict:
    candidates: list[tuple[tuple[int, int], tuple[int, ...], dict]] = []
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        if not name.endswith(_PY_SUFFIX) or "freethreaded" in name:
            continue
        ver = _cpython_version(name)
        if ver is None:
            continue
        major = (ver[0], ver[1])
        if major not in _PY_MAJORS:
            continue
        candidates.append((_PY_MAJORS.index(major), ver, asset))
    if not candidates:
        raise SystemExit(
            "No CPython 3.11–3.13 Windows install_only build in the latest "
            "python-build-standalone release."
        )
    candidates.sort(key=lambda item: (item[0], tuple(-n for n in item[1])))
    return candidates[0][2]


def ensure_python_archive() -> Path:
    say("Finding a private CPython build…")
    release = fetch_json(
        "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
    )
    asset = pick_python_asset(release)
    name = str(asset["name"])
    url = str(asset["browser_download_url"])
    size = int(asset.get("size") or 0) or None
    dest = CACHE / name
    say(f"  {name}")
    download(url, dest, size)
    return dest


def extract_python(archive: Path, runtime: Path) -> None:
    say("Extracting Python…")
    tmp = Path(tempfile.mkdtemp(prefix="pitboss-py-", dir=str(CACHE)))
    try:
        with tarfile.open(archive, "r:gz") as tf:
            try:
                tf.extractall(tmp, filter="data")
            except TypeError:
                tf.extractall(tmp)
        exe = next((p for p in tmp.rglob("python.exe") if (p.parent / "Lib").is_dir()), None)
        if exe is None:
            raise SystemExit(f"python.exe not found inside {archive.name}")
        if runtime.exists():
            rmtree(runtime)
        runtime.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(exe.parent), str(runtime))
    finally:
        rmtree(tmp)


def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None) -> None:
    say("  " + " ".join(cmd[:6]))
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"Command failed ({proc.returncode}): {' '.join(cmd[:6])}")


def runtime_env(runtime: Path, *, hf_home: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["HF_HUB_DISABLE_SYMLINKS"] = "1"
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    if hf_home is not None:
        env["HF_HOME"] = str(hf_home)
        env["HUGGINGFACE_HUB_CACHE"] = str(hf_home / "hub")
    env["PATH"] = str(runtime) + os.pathsep + env.get("PATH", "")
    return env


def install_voice(runtime: Path) -> None:
    py = runtime / "python.exe"
    env = runtime_env(runtime)
    say("Installing numpy and faster-whisper into the private Python…")
    probe = subprocess.run([str(py), "-m", "pip", "--version"], env=env, check=False)
    if probe.returncode != 0:
        run([str(py), "-m", "ensurepip", "--upgrade"], env=env)
    run(
        [
            str(py),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "-r",
            str(VOICE_REQ),
        ],
        env=env,
    )
    for extra in (runtime / "Lib" / "test", runtime / "Lib" / "tkinter" / "test"):
        if extra.is_dir():
            rmtree(extra)


def materialize_model(runtime: Path) -> None:
    dest = runtime / "models" / "base.en"
    say("Downloading the Whisper base.en model (~150 MB)…")
    script = CACHE / "_materialize_model.py"
    script.write_text(
        """\
import shutil
import sys
from pathlib import Path

dest = Path(sys.argv[1])
src = None
try:
    from faster_whisper.utils import download_model
    src = Path(download_model("base.en"))
    print(f"download_model -> {src}", flush=True)
except Exception as exc:
    print(f"download_model failed ({exc}); trying the hub snapshot", flush=True)
    from huggingface_hub import snapshot_download
    src = Path(snapshot_download(repo_id="Systran/faster-whisper-base.en"))
    print(f"snapshot_download -> {src}", flush=True)
if not (src / "model.bin").is_file():
    hits = list(src.rglob("model.bin"))
    if not hits:
        raise SystemExit(f"model.bin not found under {src}")
    src = hits[0].parent
if dest.exists():
    shutil.rmtree(dest)
dest.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(src, dest, symlinks=False)
if not (dest / "model.bin").is_file():
    raise SystemExit(f"copy missed model.bin: {dest}")
print(f"bundled model {dest}", flush=True)
""",
        encoding="utf-8",
    )
    # Keep the hub cache out of the app folder. The smoke test fills runtime/hf
    # with the small VAD files only.
    hf_tmp = CACHE / "hf-download"
    hf_tmp.mkdir(parents=True, exist_ok=True)
    run(
        [str(runtime / "python.exe"), str(script), str(dest)],
        env=runtime_env(runtime, hf_home=hf_tmp),
    )


def write_bundle_stamp(runtime: Path) -> None:
    py = runtime / "python.exe"
    code = (
        "import importlib.metadata as m, platform\n"
        "print(platform.python_version())\n"
        "print(m.version('numpy'))\n"
        "print(m.version('faster-whisper'))\n"
    )
    proc = subprocess.run(
        [str(py), "-c", code],
        env=runtime_env(runtime),
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    stamp = ["PitBoss ATC bundled runtime"]
    if len(lines) >= 3 and proc.returncode == 0:
        stamp.append(f"python={lines[0]}")
        stamp.append(f"numpy={lines[1]}")
        stamp.append(f"faster-whisper={lines[2]}")
    else:
        stamp.append("versions=unknown")
        if proc.stderr:
            stamp.append(proc.stderr.strip()[:400])
    (runtime / "BUNDLE.txt").write_text("\n".join(stamp) + "\n", encoding="utf-8")
    say("  " + ", ".join(stamp[1:4]))


def smoke_test(runtime: Path) -> None:
    """Load tkinter, numpy, faster-whisper, and base.en from a different path.

    Confirms the folder still works after Inno copies it onto a tester PC.
    """
    say("Checking the bundled runtime from a different folder…")
    hold = Path(tempfile.mkdtemp(prefix="pitboss-rt-", dir=str(CACHE)))
    moved = hold / "runtime"
    shutil.move(str(runtime), str(moved))
    try:
        code = r"""
import os
import sys
from pathlib import Path
root = Path(sys.executable).resolve().parent
os.environ.setdefault("HF_HOME", str(root / "hf"))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(root / "hf" / "hub"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
import tkinter
import numpy
import faster_whisper
model = root / "models" / "base.en"
if not (model / "model.bin").is_file():
    raise SystemExit(f"missing {model / 'model.bin'}")
from faster_whisper import WhisperModel
WhisperModel(str(model), device="cpu", compute_type="int8")
print("runtime ok", numpy.__version__, faster_whisper.__version__)
"""
        run([str(moved / "python.exe"), "-c", code], env=runtime_env(moved, hf_home=moved / "hf"))
    finally:
        if runtime.exists():
            rmtree(runtime)
        shutil.move(str(moved), str(runtime))
        rmtree(hold)


def strip_pyc(runtime: Path) -> None:
    for folder in list(runtime.rglob("__pycache__")):
        if folder.is_dir():
            rmtree(folder)


def strip_ship_extras(runtime: Path) -> None:
    """Drop pip entry-point exes. They embed this PC's build path and username."""
    scripts = runtime / "Scripts"
    if scripts.is_dir():
        rmtree(scripts)
    strip_pyc(runtime)


def ship_file(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if pack_share._skip(rel):
        return False
    parts = [p.lower() for p in rel.parts]
    # Local map packs under tools/overlays are not part of the beta.
    if len(parts) >= 2 and parts[0] == "tools" and parts[1] == "overlays":
        return path.name.lower() == "readme.txt"
    return True


def stage_tree() -> None:
    say("Copying the app into the installer staging folder…")
    if STAGING.exists():
        rmtree(STAGING)
    count = 0
    for path in pack_share.iter_files():
        if not ship_file(path):
            continue
        dest = STAGING / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        count += 1
    (STAGING / "atc" / "BUILD").write_text(pack_share._build_stamp(), encoding="utf-8")
    say(f"  {count} files")


def build_runtime(reuse: bool) -> None:
    runtime = STAGING / "atc" / "runtime"
    ready = (runtime / "python.exe").is_file() and (runtime / "models" / "base.en" / "model.bin").is_file()
    if reuse and ready:
        say("Reusing the private Python already in dist\\staging.")
        return
    archive = ensure_python_archive()
    extract_python(archive, runtime)
    install_voice(runtime)
    materialize_model(runtime)
    write_bundle_stamp(runtime)
    smoke_test(runtime)
    strip_pyc(runtime)


def file_version(ver: str) -> str:
    parts = [p for p in ver.split(".") if p.isdigit()]
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts[:4])


def write_defines(ver: str, revision: str) -> str:
    base = f"PitBossATC-Setup-{ver}"
    if revision:
        base += f"-{revision}"
    text = "\n".join(
        [
            f'#define MyAppVersion "{ver}"',
            f'#define FileVersion "{file_version(ver)}"',
            f'#define OutputBase "{base}"',
            f'#define Staging "{STAGING.resolve().as_posix()}"',
            f'#define DistDir "{DIST.resolve().as_posix()}"',
            f'#define InstallerDir "{HERE.resolve().as_posix()}"',
            "",
        ]
    )
    DEFINES.write_text(text, encoding="utf-8")
    return base


def _version_key(text: str) -> tuple[int, ...] | None:
    parts: list[int] = []
    for piece in text.split("."):
        if not piece.isdigit():
            return None
        parts.append(int(piece))
    return tuple(parts) if parts else None


def ensure_iscc() -> Path:
    found = list(CACHE.glob("innosetup/*/ISCC.exe")) + list(CACHE.glob("innosetup/*/tools/ISCC.exe"))
    if found:
        return found[0]
    say("Downloading the Inno Setup compiler…")
    index = fetch_json("https://api.nuget.org/v3-flatcontainer/tools.innosetup/index.json")
    versions = []
    for raw in index.get("versions") or []:
        key = _version_key(str(raw))
        if key is not None:
            versions.append((key, str(raw)))
    if not versions:
        raise SystemExit("NuGet has no stable Tools.InnoSetup package.")
    versions.sort()
    chosen = versions[-1][1]
    nupkg = CACHE / f"Tools.InnoSetup.{chosen}.nupkg"
    download(
        f"https://api.nuget.org/v3-flatcontainer/tools.innosetup/{chosen}/tools.innosetup.{chosen}.nupkg",
        nupkg,
    )
    dest = CACHE / "innosetup" / chosen
    if dest.exists():
        rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(nupkg) as zf:
        zf.extractall(dest)
    hits = list(dest.rglob("ISCC.exe"))
    if not hits:
        raise SystemExit(f"ISCC.exe was not inside Tools.InnoSetup {chosen}.")
    return hits[0]


def compile_installer(base: str) -> Path:
    iscc = ensure_iscc()
    say(f"Compiling {base}.exe …")
    run([str(iscc), str(ISS)], cwd=HERE)
    out = DIST / f"{base}.exe"
    if not out.is_file():
        raise SystemExit(f"Inno Setup finished but {out} is missing.")
    return out


def check_disk() -> None:
    usage = shutil.disk_usage(ROOT)
    free_gb = usage.free / (1024**3)
    say(f"Free space on {ROOT.drive or ROOT}: {free_gb:.1f} GB")
    if usage.free < 6 * 1024**3:
        raise SystemExit("Need at least 6 GB free to build the installer (Python, wheels, model, setup exe).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the PitBoss ATC open-beta installer")
    parser.add_argument(
        "--reuse-runtime",
        action="store_true",
        help="Keep dist/staging/atc/runtime if python.exe and base.en are already there",
    )
    parser.add_argument(
        "--skip-compile",
        action="store_true",
        help="Stage the app and runtime, but do not run Inno Setup",
    )
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        raise SystemExit("Build the beta installer on Windows.")
    if not VOICE_REQ.is_file():
        raise SystemExit(f"Missing {VOICE_REQ}")
    if not (ROOT / "LICENSE").is_file():
        raise SystemExit("LICENSE is missing from the repo root.")

    started = time.monotonic()
    ver = version.version()
    rev = version.revision()
    say(f"PitBoss ATC {version.display()}")
    check_disk()
    CACHE.mkdir(parents=True, exist_ok=True)

    kept: Path | None = None
    runtime = STAGING / "atc" / "runtime"
    if args.reuse_runtime and (runtime / "python.exe").is_file():
        kept = CACHE / "_runtime_keep"
        if kept.exists():
            rmtree(kept)
        shutil.move(str(runtime), str(kept))

    try:
        stage_tree()
        if kept is not None:
            dest = STAGING / "atc" / "runtime"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(kept), str(dest))
            kept = None
        build_runtime(reuse=args.reuse_runtime)
        runtime_ready = STAGING / "atc" / "runtime"
        if (runtime_ready / "python.exe").is_file():
            strip_ship_extras(runtime_ready)
    finally:
        if kept is not None and kept.exists():
            # Staging failed after we set the runtime aside. Put it back.
            back = STAGING / "atc" / "runtime"
            back.parent.mkdir(parents=True, exist_ok=True)
            if not back.exists():
                shutil.move(str(kept), str(back))

    base = write_defines(ver, rev)
    if args.skip_compile:
        say(f"Staged at {STAGING}")
        say("Compile skipped (--skip-compile).")
        return 0
    out = compile_installer(base)
    mb = out.stat().st_size / (1024 * 1024)
    say("")
    say(f"Wrote {out}")
    say(f"Size {mb:.0f} MB")
    say(f"Build {version.display()} in {(time.monotonic() - started) / 60:.1f} min")
    say("Send that Setup exe, the ATC hostname, and the shared token.")
    say("Do not send config.json or atc\\secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
