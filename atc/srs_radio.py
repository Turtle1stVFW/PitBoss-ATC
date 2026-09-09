"""
Pilot radio tune state for the Advance / TX frequency gate.

The gate matches if ANY tuned radio is on the step frequency. Selected / PTT
only chooses which radio you transmit on — keying intra-flight VHF must not
block UHF ATC / C2 triggers you are still receiving.

Sources (union when more than one is fresh):
  1. Fresh DCS Export file from ATC-RadioExport.lua (in-jet radio bank)
  2. Live SRS client UDP CombinedRadioState (full radios[] bank + selected)
  3. Manual in-app EAM radio strip (only when External AWACS mode is enabled)
  4. Unknown — caller should allow and warn

SRS SR-ClientRadio.exe already broadcasts CombinedRadioState JSON to
127.0.0.1:7080 and :7082 (~5 Hz) including RadioInfo.selected + radios[].freq.
Selected is used for TX / "TX" on the Fly line, not as the only tuned freq.
The External AWACS checkbox only enables the manual Fly EAM strip.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

HERE = Path(__file__).resolve().parent

DEFAULT_TOL_MHZ = 0.05
# Export writes ~1 Hz; allow a little slack so a missed tick is not "unknown".
DEFAULT_STALE_S = 3.0
# SRS client UDP is ~5 Hz; treat as stale quickly if the client stops.
SRS_UDP_STALE_S = 1.5
MIN_FREQ_HZ = 1_000_000  # ignore intercom / dead radios
MOD_INTERCOM = 3
# Prefer Info (7080) — that is where CombinedRadioState usually lands; Other (7082)
# is often quiet. We listen on every bindable port from the list.
DEFAULT_SRS_UDP_PORTS = (7080, 7082)  # OutgoingDCSUDPInfo, OutgoingDCSUDPOther

# Runtime EAM freqs (MHz) — updated by the Fly UI; also mirrored into config on save.
# The whole strip is the receive bank; selected index is TX only.
_eam_freqs_mhz: list[float] = []
_eam_active_index: int = 0
_eam_enabled: bool = False

# Live snapshot from SRS client UDP (CombinedRadioState).
_srs_udp_lock = threading.Lock()
_srs_udp_selected_mhz: float | None = None
_srs_udp_selected_index: int = -1
_srs_udp_radios_mhz: list[float] = []
_srs_udp_name: str = ""
_srs_udp_received_at: float = 0.0
_srs_udp_port: int | None = None
_srs_udp_ports_bound: list[int] = []
_srs_udp_error: str = ""
_srs_udp_thread: threading.Thread | None = None
_srs_udp_stop = threading.Event()

MatchResult = Literal["match", "mismatch", "unknown"]


@dataclass
class RadioState:
    source: str  # "dcs" | "srs" | "eam" | "none"
    freqs_mhz: list[float] = field(default_factory=list)
    selected_mhz: float | None = None  # keyed / common-PTT radio, if known
    unit: str = ""
    age_s: float | None = None
    fresh: bool = False
    path: Path | None = None


def set_eam_enabled(enabled: bool) -> None:
    global _eam_enabled
    _eam_enabled = bool(enabled)
    if _eam_enabled:
        ensure_srs_udp_listener()


def set_eam_freqs_mhz(freqs: list[float]) -> None:
    global _eam_freqs_mhz, _eam_active_index
    out: list[float] = []
    for raw in freqs:
        try:
            mhz = float(raw)
        except (TypeError, ValueError):
            continue
        if mhz >= 1.0:
            out.append(mhz)
    _eam_freqs_mhz = out
    if not _eam_freqs_mhz:
        _eam_active_index = 0
    else:
        _eam_active_index = max(0, min(_eam_active_index, len(_eam_freqs_mhz) - 1))


def set_eam_active_index(index: int) -> None:
    """Which EAM radio is the selected TX/RX for the gate (0-based)."""
    global _eam_active_index
    if not _eam_freqs_mhz:
        _eam_active_index = 0
        return
    _eam_active_index = max(0, min(int(index), len(_eam_freqs_mhz) - 1))


def eam_freqs_mhz() -> list[float]:
    return list(_eam_freqs_mhz)


def eam_active_index() -> int:
    return int(_eam_active_index)


def eam_active_mhz() -> float | None:
    if not _eam_freqs_mhz:
        return None
    idx = max(0, min(_eam_active_index, len(_eam_freqs_mhz) - 1))
    return float(_eam_freqs_mhz[idx])


def eam_enabled() -> bool:
    return _eam_enabled


def srs_client_global_cfg_paths() -> list[Path]:
    return [
        Path(r"C:\Program Files\DCS-SimpleRadio-Standalone\Client\global.cfg"),
        Path(r"C:\Program Files (x86)\DCS-SimpleRadio-Standalone\Client\global.cfg"),
    ]


def srs_udp_ports_from_cfg() -> list[int]:
    """Prefer ports from the installed SRS client global.cfg when present."""
    found: list[int] = []
    for path in srs_client_global_cfg_paths():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        values: dict[str, int] = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, raw = line.split("=", 1)
            key = key.strip()
            if key not in ("OutgoingDCSUDPOther", "OutgoingDCSUDPInfo"):
                continue
            try:
                values[key] = int(raw.strip())
            except ValueError:
                continue
        # Info first — CombinedRadioState is typically on OutgoingDCSUDPInfo.
        for key in ("OutgoingDCSUDPInfo", "OutgoingDCSUDPOther"):
            if key in values and values[key] not in found:
                found.append(values[key])
        if found:
            break
    return found or list(DEFAULT_SRS_UDP_PORTS)


def ensure_srs_udp_listener() -> None:
    """Start the background SRS CombinedRadioState listener if needed."""
    global _srs_udp_thread
    if _srs_udp_thread is not None and _srs_udp_thread.is_alive():
        return
    _srs_udp_stop.clear()
    _srs_udp_thread = threading.Thread(
        target=_srs_udp_loop, name="atc-srs-udp", daemon=True
    )
    _srs_udp_thread.start()


def stop_srs_udp_listener() -> None:
    _srs_udp_stop.set()


def srs_udp_status() -> dict[str, Any]:
    """Diagnostics for the Fly strip / Setup."""
    with _srs_udp_lock:
        age = (time.time() - _srs_udp_received_at) if _srs_udp_received_at else None
        return {
            "port": _srs_udp_port,
            "ports_bound": list(_srs_udp_ports_bound),
            "error": _srs_udp_error,
            "name": _srs_udp_name,
            "selected_index": _srs_udp_selected_index,
            "selected_mhz": _srs_udp_selected_mhz,
            "radios_mhz": list(_srs_udp_radios_mhz),
            "age_s": age,
            "fresh": bool(
                _srs_udp_selected_mhz is not None
                and age is not None
                and age <= SRS_UDP_STALE_S
            ),
        }


def read_srs_client_selected(*, stale_s: float = SRS_UDP_STALE_S) -> RadioState:
    """Keyed radio only — for EAM common-PTT TX, not the frequency gate."""
    bank = read_srs_client_radios(stale_s=stale_s)
    if not bank.fresh or bank.selected_mhz is None:
        return bank
    return RadioState(
        source=bank.source,
        freqs_mhz=[float(bank.selected_mhz)],
        selected_mhz=float(bank.selected_mhz),
        unit=bank.unit,
        age_s=bank.age_s,
        fresh=bank.fresh,
        path=bank.path,
    )


def read_srs_client_radios(*, stale_s: float = SRS_UDP_STALE_S) -> RadioState:
    """Every usable radio in the live SRS CombinedRadioState bank."""
    ensure_srs_udp_listener()
    with _srs_udp_lock:
        if not _srs_udp_received_at:
            return RadioState(
                source="srs",
                fresh=False,
                age_s=None,
                unit=_srs_udp_error or "SRS UDP quiet",
            )
        age = max(0.0, time.time() - _srs_udp_received_at)
        idx = _srs_udp_selected_index
        selected = (
            float(_srs_udp_selected_mhz) if _srs_udp_selected_mhz is not None else None
        )
        freqs = [float(x) for x in _srs_udp_radios_mhz]
        name = _srs_udp_name or "SRS"
        err = _srs_udp_error
    if not freqs and selected is not None:
        freqs = [selected]
    if not freqs:
        return RadioState(
            source="srs",
            fresh=False,
            age_s=age,
            unit=err or name,
        )
    return RadioState(
        source="srs",
        freqs_mhz=freqs,
        selected_mhz=selected,
        unit=f"{name} R{idx}" if idx >= 0 else name,
        age_s=age,
        fresh=age <= float(stale_s),
    )


def _srs_udp_loop() -> None:
    """Listen on every bindable SRS UDP port (Info + Other)."""
    global _srs_udp_port, _srs_udp_ports_bound, _srs_udp_error
    ports = srs_udp_ports_from_cfg()
    socks: list[socket.socket] = []
    bound: list[int] = []
    errors: list[str] = []
    for port in ports:
        candidate = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind(("127.0.0.1", int(port)))
            candidate.settimeout(0.35)
            socks.append(candidate)
            bound.append(int(port))
        except OSError as exc:
            errors.append(f"bind {port}: {exc}")
            try:
                candidate.close()
            except OSError:
                pass
    with _srs_udp_lock:
        _srs_udp_ports_bound = list(bound)
        _srs_udp_port = bound[0] if bound else None
        _srs_udp_error = "" if socks else ("; ".join(errors) or "no UDP port")
    if not socks:
        return
    try:
        while not _srs_udp_stop.is_set():
            got_any = False
            for sock in socks:
                try:
                    data, _addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError as exc:
                    with _srs_udp_lock:
                        _srs_udp_error = str(exc)
                    continue
                got_any = True
                # Remember which port last delivered a CombinedRadioState.
                try:
                    port_no = int(sock.getsockname()[1])
                except OSError:
                    port_no = None
                if port_no is not None:
                    with _srs_udp_lock:
                        _srs_udp_port = port_no
                _ingest_srs_udp_payload(data)
            if not got_any:
                # Brief yield when every socket timed out this pass.
                time.sleep(0.05)
    finally:
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass


def _ingest_srs_udp_payload(data: bytes) -> None:
    global _srs_udp_selected_mhz, _srs_udp_selected_index, _srs_udp_radios_mhz
    global _srs_udp_name, _srs_udp_received_at, _srs_udp_error
    try:
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return
        payload = json.loads(text)
    except (UnicodeError, json.JSONDecodeError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    info = payload.get("RadioInfo") or payload.get("radioInfo") or payload
    if not isinstance(info, dict):
        return
    radios = info.get("radios") or info.get("Radios") or []
    if not isinstance(radios, list):
        return
    selected_raw = info.get("selected")
    if selected_raw is None:
        selected_raw = info.get("Selected")
    try:
        selected = int(selected_raw) if selected_raw is not None else -1
    except (TypeError, ValueError):
        selected = -1
    bank: list[float] = []
    bank_indices: list[int] = []
    selected_mhz: float | None = None
    for i, entry in enumerate(radios):
        if not isinstance(entry, dict):
            continue
        try:
            mod = int(entry.get("modulation") if entry.get("modulation") is not None else entry.get("Modulation") or 0)
        except (TypeError, ValueError):
            mod = 0
        if mod == MOD_INTERCOM:
            continue
        try:
            hz = float(entry.get("freq") if entry.get("freq") is not None else entry.get("Freq") or 0)
        except (TypeError, ValueError):
            continue
        mhz = _hz_to_mhz(hz)
        if mhz is None:
            continue
        bank.append(mhz)
        bank_indices.append(i)
        if i == selected:
            selected_mhz = mhz
    # If selected was SATCOM/intercom / missing, fall back to first usable radio.
    if selected_mhz is None and bank:
        selected_mhz = bank[0]
        selected = bank_indices[0] if bank_indices else 0
    # Empty bank still marks receipt so diagnostics show the client is alive.
    with _srs_udp_lock:
        if selected_mhz is not None:
            _srs_udp_selected_mhz = selected_mhz
            _srs_udp_selected_index = selected
            _srs_udp_radios_mhz = bank
        _srs_udp_name = str(info.get("name") or info.get("Name") or "").strip()
        _srs_udp_received_at = time.time()
        _srs_udp_error = ""


def maybe_force_eam_tx_freq(
    config: dict[str, Any] | None,
    freq: float,
    mod: str,
    radio: RadioState | None = None,
) -> tuple[float, str]:
    """
    Common-PTT EAM mode: ExternalAudio must TX only on the selected radio.

    Prefer the calling pilot's radios (host session), then this PC's SRS
    client, then the manual EAM strip. A dedicated host must not steal the
    TX frequency from its own (or empty) radio bank.
    """
    apply_config(config)
    if not _eam_enabled:
        return float(freq), mod
    if radio is not None and radio.fresh:
        if radio.selected_mhz is not None:
            return float(radio.selected_mhz), mod
        if radio.freqs_mhz:
            return float(radio.freqs_mhz[0]), mod
    srs = read_srs_client_selected()
    if srs.fresh:
        if srs.selected_mhz is not None:
            return float(srs.selected_mhz), mod
        if srs.freqs_mhz:
            return float(srs.freqs_mhz[0]), mod
    active = eam_active_mhz()
    if active is None:
        return float(freq), mod
    return float(active), mod


def apply_config(config: dict[str, Any] | None) -> None:
    """Load EAM flags/freqs from config (call on startup / after Setup save).

    Only keys present in `config` are applied — a partial dict must not wipe
    the in-memory selected radio / bank used for common-PTT TX.
    """
    cfg = config or {}
    if "freq_gate_eam_enabled" in cfg:
        set_eam_enabled(bool(cfg.get("freq_gate_eam_enabled")))
    if "freq_gate_eam_freqs" in cfg and isinstance(cfg.get("freq_gate_eam_freqs"), list):
        set_eam_freqs_mhz(list(cfg["freq_gate_eam_freqs"]))
    if "freq_gate_eam_active" in cfg:
        try:
            set_eam_active_index(int(cfg.get("freq_gate_eam_active") or 0))
        except (TypeError, ValueError):
            set_eam_active_index(0)
    # Always listen for CombinedRadioState so Fly "YOU ARE ON" works even when
    # the frequency gate and External AWACS strip are both off.
    ensure_srs_udp_listener()


def saved_games_roots() -> list[Path]:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    base = home / "Saved Games"
    names = ("DCS", "DCS.openbeta", "DCS.openalpha")
    return [base / n for n in names if (base / n).is_dir()]


def radios_json_candidates() -> list[Path]:
    return [root / "ATC-ExternalAudio" / "radios.json" for root in saved_games_roots()]


def _hz_to_mhz(hz: float) -> float | None:
    if hz < MIN_FREQ_HZ:
        return None
    return hz / 1_000_000.0


def _parse_dcs_payload(data: dict[str, Any], *, mtime: float) -> RadioState:
    radios = data.get("radios") or []
    freqs: list[float] = []
    if isinstance(radios, list):
        for entry in radios:
            if not isinstance(entry, dict):
                continue
            try:
                mod = int(entry.get("modulation") or 0)
            except (TypeError, ValueError):
                mod = 0
            if mod == MOD_INTERCOM:
                continue
            for key in ("freq", "secFreq"):
                try:
                    hz = float(entry.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                mhz = _hz_to_mhz(hz)
                if mhz is not None and not any(abs(mhz - x) < 1e-6 for x in freqs):
                    freqs.append(mhz)
    age = max(0.0, time.time() - mtime)
    # Prefer file mtime; fall back to payload t if mtime looks wrong
    try:
        payload_t = float(data.get("t") or 0)
        if payload_t > 1_000_000_000:  # wall-clock seconds
            age = min(age, max(0.0, time.time() - payload_t))
    except (TypeError, ValueError):
        pass
    unit = str(data.get("unit") or "")
    return RadioState(
        source="dcs",
        freqs_mhz=freqs,
        unit=unit,
        age_s=age,
        fresh=False,  # caller sets with stale threshold
        path=None,
    )


def read_dcs_radios(*, stale_s: float = DEFAULT_STALE_S) -> RadioState:
    """Newest readable radios.json, or empty none-state."""
    best: RadioState | None = None
    best_mtime = -1.0
    for path in radios_json_candidates():
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
        state = _parse_dcs_payload(data, mtime=st.st_mtime)
        state.path = path
        state.fresh = (state.age_s or 999) <= stale_s and bool(state.unit or state.freqs_mhz)
        best = state
        best_mtime = st.st_mtime
    if best is None:
        return RadioState(source="none", fresh=False, age_s=None)
    return best


def _merge_mhz(into: list[float], freqs: list[float]) -> None:
    for mhz in freqs:
        try:
            value = float(mhz)
        except (TypeError, ValueError):
            continue
        if not any(abs(value - x) < 1e-6 for x in into):
            into.append(value)


def current_radio_state(
    config: dict[str, Any] | None = None,
    *,
    stale_s: float | None = None,
) -> RadioState:
    """
    Resolve the receive radio bank: union of a fresh DCS export and live SRS
    radios, else (when External AWACS mode is on) the whole EAM strip.

    Selected / PTT is recorded separately for TX; it does not replace the bank.
    """
    cfg = config or {}
    if config is not None:
        # Keep module EAM flags aligned when callers pass config
        if "freq_gate_eam_enabled" in cfg:
            set_eam_enabled(bool(cfg.get("freq_gate_eam_enabled")))
        if "freq_gate_eam_freqs" in cfg and isinstance(cfg.get("freq_gate_eam_freqs"), list):
            set_eam_freqs_mhz(list(cfg["freq_gate_eam_freqs"]))
        if "freq_gate_eam_active" in cfg:
            try:
                set_eam_active_index(int(cfg.get("freq_gate_eam_active") or 0))
            except (TypeError, ValueError):
                pass
    limit = float(cfg.get("freq_gate_stale_s") or stale_s or DEFAULT_STALE_S)
    dcs = read_dcs_radios(stale_s=limit)
    srs = read_srs_client_radios(stale_s=SRS_UDP_STALE_S)

    freqs: list[float] = []
    selected: float | None = None
    source = "none"
    unit = ""
    age: float | None = None
    path: Path | None = None
    fresh = False

    if dcs.fresh and (dcs.unit or dcs.freqs_mhz):
        _merge_mhz(freqs, dcs.freqs_mhz)
        source = "dcs"
        unit = dcs.unit
        age = dcs.age_s
        path = dcs.path
        fresh = True
    if srs.fresh and srs.freqs_mhz:
        _merge_mhz(freqs, srs.freqs_mhz)
        selected = srs.selected_mhz
        if source == "none":
            source = "srs"
            unit = srs.unit
            age = srs.age_s
            fresh = True
        elif srs.age_s is not None:
            age = min(age if age is not None else srs.age_s, srs.age_s)

    if fresh:
        return RadioState(
            source=source,
            freqs_mhz=freqs,
            selected_mhz=selected,
            unit=unit,
            age_s=age,
            fresh=True,
            path=path,
        )
    if _eam_enabled:
        # Whole strip is the receive bank; selected row is TX only.
        bank = eam_freqs_mhz()
        active = eam_active_mhz()
        if not bank and active is not None:
            bank = [active]
        return RadioState(
            source="eam",
            freqs_mhz=list(bank),
            selected_mhz=active,
            unit=f"EAM R{eam_active_index() + 1} (manual)",
            age_s=0.0,
            fresh=True,
        )
    if dcs.source == "dcs":
        dcs.fresh = False
        return dcs
    if srs.source == "srs" and (srs.freqs_mhz or srs.unit):
        srs.fresh = False
        return srs
    return RadioState(source="none", fresh=False, age_s=None)


def on_frequency(
    target_mhz: float,
    state: RadioState | None = None,
    *,
    tol_mhz: float = DEFAULT_TOL_MHZ,
    config: dict[str, Any] | None = None,
) -> MatchResult:
    """match / mismatch / unknown for a target MHz."""
    st = state if state is not None else current_radio_state(config)
    if not st.fresh:
        return "unknown"
    if not st.freqs_mhz:
        # In a unit but no readable radios — treat as unknown (allow)
        if st.source == "dcs":
            return "unknown"
        return "mismatch" if st.source in ("eam", "srs", "client") else "unknown"
    tol = max(0.001, float(tol_mhz))
    try:
        target = float(target_mhz)
    except (TypeError, ValueError):
        return "unknown"
    for freq in st.freqs_mhz:
        if abs(freq - target) <= tol:
            return "match"
    return "mismatch"


def format_mhz(mhz: float | None) -> str:
    """UHF-style display — always three decimals (378.225, not 378.23)."""
    try:
        return f"{float(mhz):.3f}"
    except (TypeError, ValueError):
        return "—"


def format_freqs(freqs_mhz: list[float]) -> str:
    if not freqs_mhz:
        return "(none)"
    return ", ".join(format_mhz(f) for f in freqs_mhz)


def check_freq_gate(
    config: dict[str, Any],
    airport: dict[str, Any],
    step: dict[str, Any] | None,
    *,
    target_mhz: float | None = None,
    channel: str | None = None,
    state: dict[str, Any] | None = None,
    radio: RadioState | None = None,
) -> tuple[bool, str, MatchResult]:
    """
    Returns (allowed, status_message, match_result).

    Unknown / gate disabled → allowed.
    Mismatch → not allowed.
    Pass `radio` to use a client's snapshot instead of the local DCS/SRS bank.
    """
    if not bool(config.get("freq_gate_enabled", True)):
        return True, "Freq gate off", "unknown"

    import atc_phrase  # local import — avoid circular at module load

    tol = float(config.get("freq_gate_tolerance_mhz") or DEFAULT_TOL_MHZ)
    stale = float(config.get("freq_gate_stale_s") or DEFAULT_STALE_S)
    if radio is None:
        radio = current_radio_state(config, stale_s=stale)

    ch = channel or ""
    freq = target_mhz
    if freq is None and step is not None:
        ch = ch or str(step.get("channel") or step.get("phase") or "other")
        try:
            freq, _mod, _name = atc_phrase.step_radio(
                airport, ch, step, state=state, config=config
            )
        except Exception:  # noqa: BLE001
            freq = None
    elif freq is None and ch:
        try:
            freq, _mod, _name = atc_phrase.channel_radio(airport, ch)
        except Exception:  # noqa: BLE001
            freq = None
        if str(ch).strip().lower() == "tanker" and freq is not None:
            try:
                import tanker as tanker_mod

                live = tanker_mod.effective_tanker_mhz(state, config)
                if live is not None:
                    freq = float(live)
            except Exception:
                pass

    if freq is None:
        return True, "Freq gate: no target freq", "unknown"

    result = on_frequency(float(freq), radio, tol_mhz=tol, config=config)
    label = (ch or "step").upper()
    target = format_mhz(freq)
    if result == "match":
        src = radio.source.upper()
        return True, f"On freq {target} ({label}) [{src}]", result
    if result == "unknown":
        return True, "Radio tune unknown — gate open", result
    return (
        False,
        f"Blocked: tune {target} ({label}) — radios {format_freqs(radio.freqs_mhz)}",
        result,
    )


def gate_status_line(
    config: dict[str, Any],
    airport: dict[str, Any],
    step: dict[str, Any] | None,
    *,
    state: dict[str, Any] | None = None,
) -> str:
    """Short Fly-tab status for the current step."""
    _allowed, msg, _result = check_freq_gate(config, airport, step, state=state)
    return msg


def seed_eam_from_airport(airport: dict[str, Any]) -> list[float]:
    """Agency freqs from airports.json for the EAM strip."""
    import atc_phrase

    freqs: list[float] = []
    # Backup / first EAM radio is OPS (preflight), then the rest of the strip.
    order = ["ops"] + [c for c in atc_phrase.CHANNELS if c != "ops"]
    for ch in order:
        try:
            mhz, _mod, _name = atc_phrase.channel_radio(airport, ch)
        except Exception:  # noqa: BLE001
            continue
        if mhz and not any(abs(float(mhz) - x) < 1e-6 for x in freqs):
            freqs.append(float(mhz))
    return freqs


def format_you_are_on(
    state: RadioState,
    airport: dict[str, Any] | None = None,
    *,
    tol_mhz: float = DEFAULT_TOL_MHZ,
    config: dict[str, Any] | None = None,
    flow_state: dict[str, Any] | None = None,
) -> str:
    """
    Fly 'YOU ARE ON' line: every tuned radio, with TX on the keyed one.

    Intra-flight VHF still shows, but no longer hides UHF agencies.
    Live tanker UHF (remembered / Opus) is labeled TANKER even when it differs
    from the airports.json placeholder.
    """
    if not state.freqs_mhz:
        return ""
    import atc_phrase  # local import — avoid circular at module load

    tol = max(0.001, float(tol_mhz))
    tanker_targets: list[float] = []
    try:
        import tanker as tanker_mod

        live = tanker_mod.effective_tanker_mhz(flow_state, config)
        if live is not None:
            tanker_targets.append(float(live))
        for mhz in tanker_mod.tanker_freqs_mhz(config):
            try:
                val = float(mhz)
            except (TypeError, ValueError):
                continue
            if val > 0 and not any(abs(val - x) <= tol for x in tanker_targets):
                tanker_targets.append(val)
    except Exception:
        pass

    parts: list[str] = []
    for freq in state.freqs_mhz:
        label = format_mhz(freq)
        matched = False
        if any(abs(float(t) - freq) <= tol for t in tanker_targets):
            label = f"TANKER {format_mhz(freq)}"
            matched = True
        if not matched and airport:
            for ch in atc_phrase.CHANNELS:
                try:
                    mhz, _mod, _name = atc_phrase.channel_radio(airport, ch)
                except Exception:  # noqa: BLE001
                    continue
                if mhz is None:
                    continue
                try:
                    if abs(float(mhz) - freq) <= tol:
                        # Don't brand the static 251 placeholder as TANKER when
                        # a live tanker UHF is already known and different.
                        if (
                            str(ch).lower() == "tanker"
                            and tanker_targets
                            and not any(abs(float(mhz) - t) <= tol for t in tanker_targets)
                        ):
                            continue
                        label = f"{str(ch).upper()} {format_mhz(freq)}"
                        break
                except (TypeError, ValueError):
                    continue
        if state.selected_mhz is not None and abs(freq - float(state.selected_mhz)) <= tol:
            label += " TX"
        parts.append(label)
    line = "YOU ARE ON  ·  " + "  ·  ".join(parts)
    if state.source:
        line += f"  [{state.source.upper()}"
        if not state.fresh:
            line += " STALE"
        line += "]"
    return line


def channel_for_tuned_freq(
    airport: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    tol_mhz: float | None = None,
    state: RadioState | None = None,
) -> str | None:
    """
    Agency whose published freq matches a currently tuned radio.

    Prefers the keyed / selected radio so voice replies TX on the PTT net.
    If that radio is intra-flight VHF (no agency) or the airports.json "other"
    catch-all (251.0), falls through to another tuned agency so Ops /
    Blackjack / Bandsaw tips still follow the UHF stack. Joshua / Center in
    the stack do not steal Fly — those are geographic (unless selected).
    """
    import atc_phrase

    cfg = config or {}
    tol = float(tol_mhz if tol_mhz is not None else cfg.get("freq_gate_tolerance_mhz") or DEFAULT_TOL_MHZ)
    st = state if state is not None else current_radio_state(cfg)
    if not st.fresh or not st.freqs_mhz:
        return None
    # Joshua / Center: geographic, ignore unless keyed. other: catch-all 251,
    # never a Fly tip even when keyed (Ops in the stack used to vanish behind it).
    skip_stack = frozenset({"joshua", "center", "other"})

    def _channel_for_mhz(target: float) -> str | None:
        for ch in atc_phrase.CHANNELS:
            if ch == "tanker":
                continue
            try:
                mhz, _mod, _name = atc_phrase.channel_radio(airport, ch)
            except Exception:  # noqa: BLE001
                continue
            if mhz is None:
                continue
            try:
                if abs(float(mhz) - target) <= tol:
                    return str(ch)
            except (TypeError, ValueError):
                continue
        try:
            import tanker as tanker_mod

            for mhz in tanker_mod.tanker_freqs_mhz(cfg):
                if abs(float(mhz) - target) <= tol:
                    return "tanker"
        except Exception:
            pass
        return None

    if st.selected_mhz is not None:
        keyed = _channel_for_mhz(float(st.selected_mhz))
        # "other" is the 251 placeholder, not a real agency — same as keyed VHF.
        if keyed and keyed != "other":
            return keyed
    for freq in st.freqs_mhz:
        matched = _channel_for_mhz(float(freq))
        if matched and matched not in skip_stack:
            return matched
    return None


def try_seed_from_srs_awacs() -> list[float]:
    """Optional seed from SRS awacs-radios*.json under Client install dirs."""
    roots = [
        Path(r"C:\Program Files\DCS-SimpleRadio-Standalone\Client"),
        Path(r"C:\Program Files (x86)\DCS-SimpleRadio-Standalone\Client"),
        Path(r"C:\Program Files\DCS-SimpleRadio-Standalone"),
        Path(r"C:\Program Files (x86)\DCS-SimpleRadio-Standalone"),
    ]
    names = ("awacs-radios-custom.json", "awacs-radios.json", "awacs-custom.json", "awacs.json")
    for root in roots:
        for name in names:
            path = root / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            radios = data if isinstance(data, list) else (data.get("Radios") or data.get("radios") or [])
            if not isinstance(radios, list):
                continue
            freqs: list[float] = []
            for entry in radios:
                if not isinstance(entry, dict):
                    continue
                try:
                    hz = float(entry.get("freq") or 0)
                except (TypeError, ValueError):
                    continue
                mhz = _hz_to_mhz(hz)
                if mhz is not None and not any(abs(mhz - x) < 1e-6 for x in freqs):
                    freqs.append(mhz)
            if freqs:
                return freqs
    return []
