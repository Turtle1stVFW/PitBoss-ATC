"""
Shared multi-pilot ATC constants: roles, token header, session keys.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

TOKEN_HEADER = "X-ATC-Token"
DEFAULT_ATC_PORT = 8766
SESSION_TTL_S = 15 * 60
ROLES = ("solo", "host", "client")
FIREWALL_RULE_PREFIX = "DCS ATC Host"
# Fly NET LINK sparkline — health GET must finish before the next 1 Hz tick.
ATC_HOST_PROBE_TIMEOUT_S = 0.8
# ExternalAudio SRS target: Host speaks into local SRS; Solo/Client use squadron.
SQUADRON_SRS_HOST = "showtime.455aew.com"
HOST_LOCAL_SRS_HOST = "127.0.0.1"
_KNOWN_SRS_HOSTS = frozenset(
    {SQUADRON_SRS_HOST.lower(), HOST_LOCAL_SRS_HOST, "localhost", ""}
)


def role_of(config: dict[str, Any] | None) -> str:
    raw = str((config or {}).get("atc_role") or "solo").strip().lower()
    return raw if raw in ROLES else "solo"


def default_srs_host(role: str | None = None, *, config: dict[str, Any] | None = None) -> str:
    """127.0.0.1 while hosting; squadron hostname for Solo / Client."""
    r = role_of(config if config is not None else {"atc_role": role or "solo"})
    return HOST_LOCAL_SRS_HOST if r == "host" else SQUADRON_SRS_HOST


def apply_role_srs_host(current: str | None, role: str | None = None, *, config: dict[str, Any] | None = None) -> str:
    """
    Swap known defaults when the squadron role changes.
    Custom hosts (LAN IP, alternate server) are left alone.
    """
    want = default_srs_host(role, config=config)
    cur = str(current or "").strip()
    if cur.lower() in _KNOWN_SRS_HOSTS:
        return want
    return cur or want


def effective_srs_host(
    airport: dict[str, Any] | None,
    config: dict[str, Any] | None = None,
) -> str:
    """SRS host ExternalAudio should use for this role."""
    return apply_role_srs_host(
        str((airport or {}).get("srs_host") or ""),
        config=config,
    )


def token_of(config: dict[str, Any] | None) -> str:
    return str((config or {}).get("atc_token") or "").strip()


def tokens_match(expected: str, got: str) -> bool:
    want = (expected or "").strip()
    have = (got or "").strip()
    if not want:
        return False
    return hmac.compare_digest(want.encode("utf-8"), have.encode("utf-8"))


def _norm_id(value: Any) -> str:
    """Stable id token — int(55) and 55.0 both become \"55\"."""
    if value is None or value == "":
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value).strip()


def session_key(
    *,
    opus_flight_id: Any = None,
    opus_seat: Any = None,
    callsign: str = "",
    opus_user_name: str = "",
) -> str:
    """Per-seat connection id (radios, TTS cap, Traffic row)."""
    fid = _norm_id(opus_flight_id)
    seat = _norm_id(opus_seat)
    if fid:
        return f"flight:{fid}:{seat or '1'}"
    cs = _slug(callsign) or _slug(opus_user_name)
    if cs:
        return f"callsign:{cs}"
    return "anon"


def flow_key(
    *,
    opus_flight_id: Any = None,
    callsign: str = "",
    opus_user_name: str = "",
) -> str:
    """Shared timeline id — one cursor for every seat on the same Opus flight."""
    fid = _norm_id(opus_flight_id)
    if fid:
        return f"flight:{fid}"
    return session_key(
        opus_flight_id=None,
        opus_seat=None,
        callsign=callsign,
        opus_user_name=opus_user_name,
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


_IPCONFIG_ADAPTER = re.compile(
    r"^(?P<kind>.+?) adapter (?P<name>.+):\s*$", re.IGNORECASE
)
_IPCONFIG_IPV4 = re.compile(
    r"^\s*(?:Autoconfiguration )?IPv4 Address[.\s]*:\s*(?P<ip>\d+\.\d+\.\d+\.\d+)",
    re.IGNORECASE,
)


def _parse_ipconfig(text: str) -> list[dict[str, str]]:
    """Parse `ipconfig` into [{name, ip}, ...] (no loopback / APIPA)."""
    rows: list[dict[str, str]] = []
    adapter = ""
    for line in (text or "").splitlines():
        m_ad = _IPCONFIG_ADAPTER.match(line.rstrip())
        if m_ad:
            adapter = (m_ad.group("name") or "").strip()
            continue
        m_ip = _IPCONFIG_IPV4.match(line)
        if not m_ip:
            continue
        ip = m_ip.group("ip")
        if not ip or ip.startswith("127.") or ip.startswith("169.254."):
            continue
        rows.append({"name": adapter or "LAN", "ip": ip})
    return rows


def _windows_ipv4_interfaces() -> list[dict[str, str]]:
    try:
        raw = subprocess.check_output(
            ["ipconfig"],
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return _parse_ipconfig(raw)


def lan_ipv4_interfaces() -> list[dict[str, str]]:
    """Named IPv4 interfaces this PC owns (Windows ipconfig; else hostname)."""
    if os.name == "nt":
        rows = _windows_ipv4_interfaces()
        if rows:
            return rows
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(ip: str, name: str = "LAN") -> None:
        ip = (ip or "").strip()
        if not ip or ip.startswith("127.") or ip.startswith("169.254."):
            return
        if ip in seen:
            return
        seen.add(ip)
        found.append({"name": name, "ip": ip})

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        _add(probe.getsockname()[0], "default")
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except OSError:
        pass
    return found


def lan_ipv4_addresses() -> list[str]:
    """Non-loopback IPv4 addresses this PC can be reached at on the LAN."""
    return [row["ip"] for row in lan_ipv4_interfaces()]


def ipv4_same_lan(host_ip: str, local_ips: list[str] | None = None) -> bool:
    """True if host_ip shares a /24 with any local address (common home LAN)."""
    local_ips = list(local_ips if local_ips is not None else lan_ipv4_addresses())
    try:
        h = tuple(int(p) for p in str(host_ip).split("."))
        if len(h) != 4:
            return False
    except ValueError:
        return False
    for raw in local_ips:
        try:
            loc = tuple(int(p) for p in str(raw).split("."))
        except ValueError:
            continue
        if len(loc) != 4:
            continue
        if loc[:3] == h[:3]:
            return True
    return False


def host_from_url(url: str) -> str:
    m = re.match(r"^https?://([^/:]+)", (url or "").strip(), re.IGNORECASE)
    return (m.group(1) if m else "").strip()


def listen_urls(port: int) -> list[str]:
    port = int(port)
    urls = [f"http://{row['ip']}:{port}" for row in lan_ipv4_interfaces()]
    urls.append(f"http://127.0.0.1:{port}")
    return urls


def format_listen_summary(port: int) -> str:
    """Host UI line listing every NIC IP pilots might use."""
    port = int(port)
    rows = lan_ipv4_interfaces()
    if not rows:
        return f"HOST  listen ?:{port}  ·  no LAN IPv4 found"
    parts = [f"{row['ip']}:{port} ({row['name']})" for row in rows]
    return "HOST  listen " + "  |  ".join(parts)


def describe_connect_failure(
    reason: object,
    url: str,
    *,
    local_ips: list[str] | None = None,
) -> str:
    """Human hint for urllib URLError.reason (timeout vs refused vs other)."""
    text = str(reason or "").strip() or "unknown"
    low = text.casefold()
    url = (url or "").strip()
    host = host_from_url(url)
    loopback = host in {"127.0.0.1", "localhost"}
    locals_ = list(local_ips if local_ips is not None else lan_ipv4_addresses())
    mismatch = bool(host) and not loopback and locals_ and not ipv4_same_lan(host, locals_)
    route_hint = ""
    if mismatch:
        here = ", ".join(locals_) or "?"
        route_hint = (
            f" This PC is {here}; Host is {host}. Different subnets are fine if "
            f"TCP {DEFAULT_ATC_PORT} is routed or port-forwarded to the DCS server "
            f"the same way SRS is (do not use 192.168.50.x unless this PC can already "
            f"reach it). "
        )
    if "timed out" in low or "timeout" in low:
        hint = (
            "timed out — packets never reached the Host. "
            f"{route_hint}"
            "On the Host PC: allow Windows Firewall inbound TCP "
            f"{DEFAULT_ATC_PORT} (or python.exe). "
            "On this PC: Host address = the IP/hostname you already use for DCS/SRS, "
            f"ATC port {DEFAULT_ATC_PORT} (not SRS 5002)."
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


def short_connect_failure(reason: object) -> str:
    """Compact label for the Fly NET LINK status / sparkline (not the amber strip)."""
    low = str(reason or "").strip().casefold()
    if not low:
        return "fail"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if "refused" in low or "10061" in low:
        return "refused"
    if "10065" in low or "unreachable" in low:
        return "unreachable"
    # Keep it short for the Consolas status line.
    text = str(reason or "").strip()
    return (text[:32] + "…") if len(text) > 32 else (text or "fail")


def atc_host_probe(
    host: str,
    port: int = DEFAULT_ATC_PORT,
    *,
    timeout_s: float = ATC_HOST_PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """GET /v1/health RTT for the Fly NET LINK sparkline (Client role).

    No token — same lightweight check as AtcClient.probe_health, shorter timeout.
    """
    host = (host or "").strip() or "127.0.0.1"
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "rtt_ms": None, "error": "bad ATC port"}
    if port_i <= 0 or port_i > 65535:
        return {"ok": False, "rtt_ms": None, "error": "bad ATC port"}
    url = f"http://{host}:{port_i}/v1/health"
    req = urllib.request.Request(url, method="GET")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "rtt_ms": None,
            "error": short_connect_failure(f"{exc.code}"),
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "rtt_ms": None,
            "error": short_connect_failure(exc.reason),
        }
    except TimeoutError as exc:
        return {
            "ok": False,
            "rtt_ms": None,
            "error": short_connect_failure(exc),
        }
    except OSError as exc:
        return {
            "ok": False,
            "rtt_ms": None,
            "error": short_connect_failure(exc),
        }
    rtt_ms = (time.perf_counter() - t0) * 1000.0
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "rtt_ms": None, "error": "bad health JSON"}
    if not isinstance(payload, dict) or not payload.get("ok"):
        return {"ok": False, "rtt_ms": None, "error": "health not ok"}
    return {"ok": True, "rtt_ms": rtt_ms, "error": ""}


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
