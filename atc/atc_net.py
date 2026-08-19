"""
Shared multi-pilot ATC constants: roles, token header, session keys.
"""

from __future__ import annotations

import hmac
import os
import re
import socket
import subprocess
from typing import Any

TOKEN_HEADER = "X-ATC-Token"
DEFAULT_ATC_PORT = 8766
SESSION_TTL_S = 15 * 60
ROLES = ("solo", "host", "client")
FIREWALL_RULE_PREFIX = "DCS ATC Host"


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


def lan_ipv4_addresses() -> list[str]:
    """Non-loopback IPv4 addresses this PC can be reached at on the LAN."""
    found: list[str] = []

    def _add(ip: str) -> None:
        ip = (ip or "").strip()
        if not ip or ip.startswith("127.") or ip.startswith("169.254."):
            return
        if ip not in found:
            found.append(ip)

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        _add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except OSError:
        pass
    return found


def listen_urls(port: int) -> list[str]:
    port = int(port)
    urls = [f"http://{ip}:{port}" for ip in lan_ipv4_addresses()]
    urls.append(f"http://127.0.0.1:{port}")
    return urls


def describe_connect_failure(reason: object, url: str) -> str:
    """Human hint for urllib URLError.reason (timeout vs refused vs other)."""
    text = str(reason or "").strip() or "unknown"
    low = text.casefold()
    url = (url or "").strip()
    loopback = "127.0.0.1" in url or "localhost" in url.casefold()
    if "timed out" in low or "timeout" in low:
        hint = (
            "timed out — packets never reached the Host. "
            "On the Host PC: Windows Firewall inbound TCP "
            f"{DEFAULT_ATC_PORT} (or allow python.exe). "
            "On this PC: Host address must be the server LAN IP "
            f"(not 127.0.0.1) and ATC port {DEFAULT_ATC_PORT} "
            "(not SRS 5002)."
        )
        if loopback:
            hint = (
                "timed out talking to 127.0.0.1 — that is THIS PC. "
                "Set Host address to the server's LAN IP shown on the Host Squadron tab."
            )
        return f"host unreachable ({hint}) [{url}]"
    if "refused" in low or "10061" in low:
        hint = (
            "connection refused — nothing is listening on that IP:port. "
            "Host role must be saved on the server, same ATC port, "
            "and not the SRS port."
        )
        if loopback:
            hint = (
                "connection refused on 127.0.0.1 — this PC has no Host. "
                "Use the dedicated server's LAN IP."
            )
        return f"host unreachable ({hint}) [{url}]"
    return f"host unreachable ({text}) [{url}]"


def ensure_inbound_tcp_firewall(port: int) -> str:
    """
    Add a Windows inbound TCP allow rule for the ATC Host port.

    Returns a short status for the UI. No-op on non-Windows. Failure is
    non-fatal (the Host still listens; the operator can allow Python once).
    """
    if os.name != "nt":
        return ""
    port = int(port)
    name = f"{FIREWALL_RULE_PREFIX} {port}"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        show = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
            capture_output=True,
            text=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        return f"firewall skipped ({exc})"
    combined = f"{show.stdout or ''}{show.stderr or ''}"
    if show.returncode == 0 and "No rules match" not in combined:
        return f"firewall OK ({name})"
    try:
        added = subprocess.run(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={name}",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"localport={port}",
                "enable=yes",
                "profile=any",
            ],
            capture_output=True,
            text=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        return f"firewall skipped ({exc})"
    if added.returncode == 0:
        return f"firewall allowed TCP {port}"
    return (
        f"firewall rule needs Administrator — allow python.exe when Windows asks, "
        f"or run: netsh advfirewall firewall add rule name=\"{name}\" dir=in "
        f"action=allow protocol=TCP localport={port}"
    )
