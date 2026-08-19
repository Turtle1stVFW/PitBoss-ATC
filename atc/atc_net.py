"""
Shared multi-pilot ATC constants: roles, token header, session keys.
"""

from __future__ import annotations

import hmac
import re
from typing import Any

TOKEN_HEADER = "X-ATC-Token"
DEFAULT_ATC_PORT = 8766
SESSION_TTL_S = 15 * 60
ROLES = ("solo", "host", "client")


def role_of(config: dict[str, Any] | None) -> str:
    raw = str((config or {}).get("atc_role") or "solo").strip().lower()
    return raw if raw in ROLES else "solo"


def token_of(config: dict[str, Any] | None) -> str:
    return str((config or {}).get("atc_token") or "").strip()


def tokens_match(expected: str, got: str) -> bool:
    want = (expected or "").strip()
    have = (got or "").strip()
    if not want:
        return False
    return hmac.compare_digest(want.encode("utf-8"), have.encode("utf-8"))


def session_key(
    *,
    opus_flight_id: Any = None,
    opus_seat: Any = None,
    callsign: str = "",
    opus_user_name: str = "",
) -> str:
    fid = str(opus_flight_id or "").strip()
    seat = str(opus_seat if opus_seat is not None else "").strip()
    if fid:
        return f"flight:{fid}:{seat or '1'}"
    cs = _slug(callsign) or _slug(opus_user_name)
    if cs:
        return f"callsign:{cs}"
    return "anon"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
