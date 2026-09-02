"""
Read the ownship aircraft snapshot written by ATC-RadioExport.lua.

`Saved Games\\DCS*\\ATC-ExternalAudio\\aircraft.json` — works in SP and MP
for the local player's jet. Stale after ~1 s so a parked file cannot fake
an intercom session.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import srs_radio

DEFAULT_STALE_S = 1.2
F16_UNITS = frozenset({"F-16C_50", "F-16C"})


@dataclass
class AircraftState:
    source: str  # "dcs" | "none" | "inject"
    unit: str = ""
    on_ground: bool = False
    agl_m: float | None = None
    ias_mps: float | None = None
    mech: dict[str, Any] = field(default_factory=dict)
    args: dict[int, float] = field(default_factory=dict)
    age_s: float | None = None
    fresh: bool = False
    path: Path | None = None

    @property
    def is_f16(self) -> bool:
        name = (self.unit or "").strip()
        return name in F16_UNITS or name.startswith("F-16C")


def aircraft_json_candidates() -> list[Path]:
    return [root / "ATC-ExternalAudio" / "aircraft.json" for root in srs_radio.saved_games_roots()]


def _pair(raw: Any) -> tuple[float, float]:
    if isinstance(raw, (list, tuple)) and raw:
        try:
            left = float(raw[0])
        except (TypeError, ValueError):
            left = 0.0
        try:
            right = float(raw[1]) if len(raw) > 1 else left
        except (TypeError, ValueError):
            right = left
        return left, right
    try:
        n = float(raw or 0)
    except (TypeError, ValueError):
        n = 0.0
    return n, n


def _parse_args(raw: Any) -> dict[int, float]:
    out: dict[int, float] = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        try:
            aid = int(key)
            out[aid] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def parse_aircraft_payload(data: dict[str, Any], *, mtime: float) -> AircraftState:
    age = max(0.0, time.time() - mtime)
    try:
        payload_t = float(data.get("t") or 0)
        if payload_t > 1_000_000_000:
            age = min(age, max(0.0, time.time() - payload_t))
    except (TypeError, ValueError):
        pass
    mech_raw = data.get("mech") if isinstance(data.get("mech"), dict) else {}
    mech: dict[str, Any] = {}
    for key in ("elevator", "aileron", "rudder"):
        left, right = _pair(mech_raw.get(key))
        mech[key] = [left, right]
    for key in ("speedbrakes", "gear", "canopy", "wheelbrakes"):
        try:
            mech[key] = float(mech_raw.get(key) or 0)
        except (TypeError, ValueError):
            mech[key] = 0.0
    agl: float | None
    ias: float | None
    try:
        agl = float(data.get("agl_m"))
    except (TypeError, ValueError):
        agl = None
    try:
        ias = float(data.get("ias_mps"))
    except (TypeError, ValueError):
        ias = None
    return AircraftState(
        source="dcs",
        unit=str(data.get("unit") or ""),
        on_ground=bool(data.get("on_ground")),
        agl_m=agl,
        ias_mps=ias,
        mech=mech,
        args=_parse_args(data.get("args")),
        age_s=age,
        fresh=False,
    )


def read_aircraft(
    *,
    stale_s: float = DEFAULT_STALE_S,
    inject: AircraftState | None = None,
) -> AircraftState:
    """Newest readable aircraft.json, or an empty none-state."""
    if inject is not None:
        return inject
    best: AircraftState | None = None
    best_mtime = -1.0
    for path in aircraft_json_candidates():
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime < best_mtime:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        state = parse_aircraft_payload(data, mtime=st.st_mtime)
        state.path = path
        state.fresh = (state.age_s or 999) <= stale_s and bool(state.unit)
        best = state
        best_mtime = st.st_mtime
    if best is None:
        return AircraftState(source="none", fresh=False, age_s=None)
    return best


def arg_value(snap: AircraftState, arg_id: int) -> float | None:
    return snap.args.get(int(arg_id))


def arg_in_range(snap: AircraftState, arg_id: int, lo: float, hi: float) -> bool:
    val = arg_value(snap, arg_id)
    if val is None:
        return False
    return lo <= val <= hi


def mech_axis(snap: AircraftState, name: str) -> float:
    """Average of left/right for elevator / aileron / rudder."""
    pair = snap.mech.get(name)
    if isinstance(pair, (list, tuple)) and pair:
        try:
            left = float(pair[0])
            right = float(pair[1]) if len(pair) > 1 else left
            return (left + right) / 2.0
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(pair or 0)
    except (TypeError, ValueError):
        return 0.0


def mech_scalar(snap: AircraftState, name: str) -> float:
    try:
        return float(snap.mech.get(name) or 0)
    except (TypeError, ValueError):
        return 0.0


def f16_on_ground(snap: AircraftState) -> bool:
    """Hard gate: fresh F-16 snapshot that reports on the ground."""
    if not snap.fresh or not snap.is_f16:
        return False
    return bool(snap.on_ground)
