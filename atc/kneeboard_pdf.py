#!/usr/bin/env python3
"""
Generate OpenKneeboard PDF: full flow list with hyperlinks to localhost flow API.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
AIRPORTS_PATH = HERE / "airports.json"
DEFAULT_OUT = HERE / "kneeboard" / "flow_comms.pdf"
BASE = "http://127.0.0.1:8765"


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def channel_freq(airport: dict[str, Any], channel: str) -> str:
    ch = airport.get(channel) or {}
    try:
        return f"{float(ch.get('freq_mhz', 0)):g}"
    except (TypeError, ValueError):
        return "?"


def build_lines(mission: dict[str, Any], airport: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Return list of (display_text, url_or_None)."""
    lines: list[tuple[str, str | None]] = []
    lines.append(("ATC FLOW / COMMS", None))
    lines.append((mission.get("name") or "Mission", None))
    lines.append(("", None))
    lines.append(("CONTROLS", None))
    lines.append((">> PLAY NEXT", f"{BASE}/next"))
    lines.append(("<< PLAY PREVIOUS", f"{BASE}/back"))
    lines.append(("RESET", f"{BASE}/reset"))
    lines.append(("", None))

    lines.append(("TIMELINE", None))
    # Prefer unified steps; fall back to legacy outbound+inbound
    raw_steps = mission.get("steps")
    if not isinstance(raw_steps, list):
        raw_steps = list(mission.get("outbound") or []) + list(mission.get("inbound") or [])
    steps = [s for s in raw_steps if s.get("enabled", True)]
    if not steps:
        lines.append(("  (none)", None))
    for i, step in enumerate(steps, start=1):
        ch = step.get("channel") or step.get("phase") or "other"
        freq = channel_freq(airport, ch)
        label = step.get("label") or step.get("id")
        sid = step.get("id")
        text = f"  {i}. {str(ch).upper()}  {label}  {freq}"
        url = f"{BASE}/play?id={sid}" if sid else None
        lines.append((text, url))
    lines.append(("", None))

    lines.append(("Start Flow window before clicking links.", None))
    lines.append((f"Server: {BASE}", None))
    return lines


def write_pdf(path: Path, lines: list[tuple[str, str | None]]) -> None:
    """Minimal PDF 1.4 with text and URI link annotations (no external deps)."""
    page_w, page_h = 612, 792  # Letter
    margin = 36
    font_size = 11
    leading = 16
    y_start = page_h - margin - 20

    # Build content stream + collect link rects
    content_parts = ["BT", "/F1 11 Tf", "14 TL"]
    links: list[tuple[float, float, float, float, str]] = []  # x0,y0,x1,y1,uri

    y = y_start
    for text, url in lines:
        if y < margin + 40:
            break
        safe = pdf_escape(text[:90])
        content_parts.append(f"1 0 0 1 {margin} {y:.1f} Tm ({safe}) Tj")
        if url:
            # Approximate text width ~ 6*len for Helvetica 11
            tw = min(page_w - 2 * margin, 6.2 * len(text))
            links.append((margin, y - 3, margin + tw, y + font_size, url))
        y -= leading
    content_parts.append("ET")
    content = "\n".join(content_parts).encode("latin-1", errors="replace")

    objects: list[bytes] = []

    def add_obj(data: bytes) -> int:
        objects.append(data)
        return len(objects)

    # 1: Catalog
    add_obj(b"<< /Type /Catalog /Pages 2 0 R >>")
    # 2: Pages (filled later)
    pages_idx = add_obj(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    assert pages_idx == 2

    # Annotations
    annot_ids: list[int] = []
    for x0, y0, x1, y1, uri in links:
        uri_esc = uri.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        annot = (
            f"<< /Type /Annot /Subtype /Link "
            f"/Rect [{x0:.1f} {y0:.1f} {x1:.1f} {y1:.1f}] "
            f"/Border [0 0 1] "
            f"/A << /S /URI /URI ({uri_esc}) >> >>"
        ).encode("latin-1")
        annot_ids.append(add_obj(annot))

    annots_ref = " ".join(f"{i} 0 R" for i in annot_ids)
    # 3 will be page — reserve by building content first
    # Content stream object
    content_obj = (
        f"<< /Length {len(content)} >>\nstream\n".encode("latin-1")
        + content
        + b"\nendstream"
    )
    content_id = add_obj(content_obj)

    # Font
    font_id = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    # Page object — must be object 3 for Kids reference; we already used ids.
    # Rebuild properly with known order instead.
    # Simpler approach: rewrite with sequential assembly below.
    _ = (content_id, font_id, annots_ref)  # placate linter if rebuild

    # ---- Rebuild cleanly ----
    objects = []

    def obj(data: bytes) -> int:
        objects.append(data)
        return len(objects)

    obj(b"<< /Type /Catalog /Pages 2 0 R >>")  # 1
    obj(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")  # 2

    annot_ids = []
    for x0, y0, x1, y1, uri in links:
        uri_esc = uri.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        annot_ids.append(
            obj(
                (
                    f"<< /Type /Annot /Subtype /Link "
                    f"/Rect [{x0:.1f} {y0:.1f} {x1:.1f} {y1:.1f}] "
                    f"/Border [0 0 0] "
                    f"/C [0 0.4 0.9] "
                    f"/A << /S /URI /URI ({uri_esc}) >> >>"
                ).encode("latin-1")
            )
        )

    content_id = obj(
        f"<< /Length {len(content)} >>\nstream\n".encode("latin-1")
        + content
        + b"\nendstream"
    )
    font_id = obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    annots = "[" + " ".join(f"{i} 0 R" for i in annot_ids) + "]"
    # Insert page as object 3: shift by rebuilding again with page early.
    # Easiest fix: write page as last then patch Kids — use generation with page=3 reserved.

    # Final correct order:
    objects = []
    obj(b"<< /Type /Catalog /Pages 2 0 R >>")  # 1
    obj(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")  # 2
    # 3 page placeholder
    page_placeholder = len(objects)
    objects.append(b"")  # 3
    annot_ids = []
    for x0, y0, x1, y1, uri in links:
        uri_esc = uri.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        annot_ids.append(
            obj(
                (
                    f"<< /Type /Annot /Subtype /Link "
                    f"/Rect [{x0:.1f} {y0:.1f} {x1:.1f} {y1:.1f}] "
                    f"/Border [0 0 0] "
                    f"/A << /S /URI /URI ({uri_esc}) >> >>"
                ).encode("latin-1")
            )
        )
    content_id = obj(
        f"<< /Length {len(content)} >>\nstream\n".encode("latin-1")
        + content
        + b"\nendstream"
    )
    font_id = obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    annots = "[" + " ".join(f"{i} 0 R" for i in annot_ids) + "]"
    objects[page_placeholder] = (
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w} {page_h}] "
        f"/Contents {content_id} 0 R "
        f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
        f"/Annots {annots} >>"
    ).encode("latin-1")

    # Write xref
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, data in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode("latin-1"))
        out.extend(data)
        out.extend(b"\nendobj\n")
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("latin-1")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def generate(out_path: Path | None = None) -> Path:
    config = load_json(CONFIG_PATH)
    airports = load_json(AIRPORTS_PATH)
    flow_rel = config.get("flow_file") or "flows/nellis_default.json"
    flow_path = Path(flow_rel)
    if not flow_path.is_absolute():
        flow_path = HERE / flow_path
    mission = load_json(flow_path)
    key = mission.get("airport") or config.get("default_airport") or "nellis"
    airport = airports[key]
    lines = build_lines(mission, airport)
    dest = out_path or DEFAULT_OUT
    write_pdf(dest, lines)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    path = generate(Path(args.output))
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
