#!/usr/bin/env python3
"""
Copy Sounds/CC from a DCS .miz (or a folder) into atc/crew_chief/sounds/.

OGG files stay as-is; a sibling WAV is written when ffmpeg is on PATH so
Windows can play the pack without extra Python deps.

  py -3 crew_chief_extract.py
  py -3 crew_chief_extract.py --miz "C:\\path\\Mission1.001.miz"
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEST = HERE / "crew_chief" / "sounds"
DEFAULT_MIZ = Path(
    r"c:\Users\sterl\iCloudDrive\DCS Stuff\AFGH Campaign\Mission1.001.miz"
)


def _convert_ogg_to_wav(ogg: Path) -> Path | None:
    wav = ogg.with_suffix(".wav")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(ogg), "-ac", "1", "-ar", "22050", str(wav)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return wav if wav.is_file() else None


def extract_from_miz(miz: Path, dest: Path = DEST) -> dict[str, int]:
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    converted = 0
    with zipfile.ZipFile(miz) as zf:
        for name in zf.namelist():
            norm = name.replace("\\", "/")
            if not norm.lower().startswith("sounds/cc/"):
                continue
            if norm.endswith("/"):
                continue
            rel = norm[len("Sounds/CC/") :] if norm.startswith("Sounds/CC/") else norm[len("sounds/cc/") :]
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(name))
            copied += 1
            if out.suffix.lower() == ".ogg" and _convert_ogg_to_wav(out):
                converted += 1
    return {"copied": copied, "converted": converted}


def copy_from_dir(src: Path, dest: Path = DEST) -> dict[str, int]:
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    converted = 0
    root = src / "Sounds" / "CC" if (src / "Sounds" / "CC").is_dir() else src
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
        copied += 1
        if out.suffix.lower() == ".ogg" and _convert_ogg_to_wav(out):
            converted += 1
    return {"copied": copied, "converted": converted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract Crew Chief sound pack.")
    parser.add_argument("--miz", type=Path, default=None, help="Path to a .miz")
    parser.add_argument("--dir", type=Path, default=None, help="Folder of Sounds/CC files")
    parser.add_argument("--dest", type=Path, default=DEST, help="Output folder")
    args = parser.parse_args(argv)

    if args.dir:
        stats = copy_from_dir(args.dir, args.dest)
    else:
        miz = args.miz or DEFAULT_MIZ
        if not miz.is_file():
            print(f"No .miz at {miz}. Pass --miz or --dir.", file=sys.stderr)
            return 1
        stats = extract_from_miz(miz, args.dest)
    print(
        f"Copied {stats['copied']} files to {args.dest} "
        f"({stats['converted']} converted to WAV)."
    )
    if stats["copied"] and stats["converted"] == 0:
        print("Install ffmpeg to convert OGG → WAV for the Windows player.")
    return 0 if stats["copied"] else 1


if __name__ == "__main__":
    sys.exit(main())
