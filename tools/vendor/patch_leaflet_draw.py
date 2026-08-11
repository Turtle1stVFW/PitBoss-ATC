"""
Declare `type` in Leaflet.draw's readableArea.

Leaflet.draw 1.0.4 assigns to an undeclared `type` there, which is fine in a
sloppy-mode script tag but throws the moment anything runs strict — and it runs
on every mouse move while drawing a polygon with showArea on. Upstream has had
the one-line fix open for years (Leaflet/Leaflet.draw#899), so patch the vendored
copy instead of relying on sloppy mode.

Idempotent: run it again and it reports that there is nothing to do.
"""

from __future__ import annotations

import pathlib
import re
import sys

TARGET = pathlib.Path(__file__).resolve().parent / "leaflet-draw.js"
# Minified: `readableArea:function(t,e,i){var o,a,...` then later `a=typeof e`.
NEEDLE = re.compile(r"(readableArea:function\([^)]*\)\{var )([a-zA-Z$_,]+)(,[a-zA-Z$_]+=L\.Util\.extend)")


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")
    print(f"'use strict' in file: {'use strict' in src}")

    match = NEEDLE.search(src)
    if not match:
        print("readableArea not found in the expected shape", file=sys.stderr)
        return 2

    head, names, tail = match.groups()
    print(f"declared locals: {names}")
    body_start = match.end()
    body = src[body_start : body_start + 600]
    assigned = set(re.findall(r"([a-zA-Z$_]+)=typeof ", body))
    print(f"assigned via typeof: {sorted(assigned) or 'none'}")

    missing = sorted(n for n in assigned if n not in names.split(","))
    if not missing:
        print("nothing to patch — every assignment is already declared")
        return 0

    patched = src[: match.start()] + head + names + "," + ",".join(missing) + tail
    patched += src[match.end() :]
    TARGET.write_text(patched, encoding="utf-8", newline="\n")
    print(f"declared {', '.join(missing)}; wrote {TARGET.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
