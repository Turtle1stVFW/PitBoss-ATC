"""
HOTAS button input via the legacy winmm joystick API.

Why not keyboard hotkeys: DCS and SRS run elevated (SRS ships `RequireAdmin=true`).
Windows UIPI refuses to deliver WM_HOTKEY to a non-elevated process while an
elevated window is focused, so RegisterHotKey silently does nothing in-game.
Reading joystick state is a device query rather than an input-stream hook, so it
is unaffected by focus or integrity level.

joyGetPosEx exposes the first 32 buttons of up to 16 devices and costs ~0.5 us per
device per poll, so watching a bound device at 100 Hz is ~0.005% of one core.

Sharing with SRS: this only ever reads state — no device handle, no DirectInput
acquisition, nothing exclusive — so SRS keeps transmitting on the same button.
Only devices with a binding are polled (all of them briefly, while learning).
"""

from __future__ import annotations

import ctypes
import threading
import winreg
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

MAX_DEVICES = 16
MAX_BUTTONS = 32
POLL_HZ = 100

JOY_RETURNBUTTONS = 0x00000080
JOYERR_NOERROR = 0

_OEM_REG_BASE = r"System\CurrentControlSet\Control\MediaProperties\PrivateProperties\Joystick\OEM"


class JOYCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("szPname", wintypes.WCHAR * 32),
        ("wXmin", wintypes.UINT),
        ("wXmax", wintypes.UINT),
        ("wYmin", wintypes.UINT),
        ("wYmax", wintypes.UINT),
        ("wZmin", wintypes.UINT),
        ("wZmax", wintypes.UINT),
        ("wNumButtons", wintypes.UINT),
        ("wPeriodMin", wintypes.UINT),
        ("wPeriodMax", wintypes.UINT),
        ("wRmin", wintypes.UINT),
        ("wRmax", wintypes.UINT),
        ("wUmin", wintypes.UINT),
        ("wUmax", wintypes.UINT),
        ("wVmin", wintypes.UINT),
        ("wVmax", wintypes.UINT),
        ("wCaps", wintypes.UINT),
        ("wMaxAxes", wintypes.UINT),
        ("wNumAxes", wintypes.UINT),
        ("wMaxButtons", wintypes.UINT),
        ("szRegKey", wintypes.WCHAR * 32),
        ("szOEMVxD", wintypes.WCHAR * 260),
    ]


class JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("dwXpos", wintypes.DWORD),
        ("dwYpos", wintypes.DWORD),
        ("dwZpos", wintypes.DWORD),
        ("dwRpos", wintypes.DWORD),
        ("dwUpos", wintypes.DWORD),
        ("dwVpos", wintypes.DWORD),
        ("dwButtons", wintypes.DWORD),
        ("dwButtonNumber", wintypes.DWORD),
        ("dwPOV", wintypes.DWORD),
        ("dwReserved1", wintypes.DWORD),
        ("dwReserved2", wintypes.DWORD),
    ]


try:
    _winmm = ctypes.windll.winmm
except (AttributeError, OSError):  # non-Windows
    _winmm = None


def supported() -> bool:
    return _winmm is not None


def _oem_name(caps: JOYCAPS) -> str:
    """Friendly product name; winmm itself only reports 'Microsoft PC-joystick driver'."""
    key = f"VID_{caps.wMid:04X}&PID_{caps.wPid:04X}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{_OEM_REG_BASE}\\{key}") as k:
            name = str(winreg.QueryValueEx(k, "OEMName")[0]).strip()
            if name:
                return name
    except OSError:
        pass
    return caps.szPname or key


def list_devices() -> list[dict[str, Any]]:
    """Connected joysticks as {id, name, buttons, axes}."""
    if _winmm is None:
        return []
    out: list[dict[str, Any]] = []
    for jid in range(MAX_DEVICES):
        caps = JOYCAPS()
        if _winmm.joyGetDevCapsW(jid, ctypes.byref(caps), ctypes.sizeof(caps)) != JOYERR_NOERROR:
            continue
        out.append(
            {
                "id": jid,
                "name": _oem_name(caps),
                "buttons": int(caps.wNumButtons),
                "axes": int(caps.wNumAxes),
            }
        )
    return out


def read_buttons(device_id: int) -> int | None:
    """Button bitmask for one device, or None if it is not present."""
    if _winmm is None:
        return None
    info = JOYINFOEX()
    info.dwSize = ctypes.sizeof(info)
    info.dwFlags = JOY_RETURNBUTTONS
    if _winmm.joyGetPosEx(int(device_id), ctypes.byref(info)) != JOYERR_NOERROR:
        return None
    return int(info.dwButtons)


# ---------- bindings ----------


def normalize_binding(raw: Any) -> dict[str, Any] | None:
    """
    Accepts {"device": name, "id": n, "button": i} or the legacy "id:button" string.
    `button` is a 0-based bit index; the UI shows it 1-based to match DCS.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        parts = raw.replace(" ", "").split(":")
        if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
            return None
        return {"device": "", "id": int(parts[0]), "button": int(parts[1])}
    if not isinstance(raw, dict):
        return None
    try:
        button = int(raw.get("button"))
    except (TypeError, ValueError):
        return None
    if not 0 <= button < MAX_BUTTONS:
        return None
    try:
        device_id = int(raw.get("id"))
    except (TypeError, ValueError):
        device_id = -1
    return {
        "device": str(raw.get("device") or ""),
        "id": device_id,
        "button": button,
    }


def resolve_device_id(binding: dict[str, Any]) -> int | None:
    """
    winmm ids shift when devices are unplugged, so prefer matching the saved
    product name and fall back to the saved id.
    """
    devices = list_devices()
    if not devices:
        return None
    name = str(binding.get("device") or "").strip().casefold()
    if name:
        for dev in devices:
            if str(dev["name"]).strip().casefold() == name:
                return int(dev["id"])
    saved = binding.get("id")
    if isinstance(saved, int) and saved >= 0:
        for dev in devices:
            if int(dev["id"]) == saved:
                return saved
    return None


def describe_binding(binding: dict[str, Any] | None) -> str:
    if not binding:
        return "(none)"
    name = binding.get("device") or f"Joystick {binding.get('id')}"
    return f"{name} · Btn {int(binding['button']) + 1}"


def binding_from_config(config: dict[str, Any], which: str) -> dict[str, Any] | None:
    return normalize_binding(config.get(f"joy_{which}"))


# ---------- SRS PTT discovery ----------

SRS_CLIENT_DIRS = (
    Path(r"C:\Program Files\DCS-SimpleRadio-Standalone\Client"),
    Path(r"C:\Program Files (x86)\DCS-SimpleRadio-Standalone\Client"),
)

# Sections that transmit. RadioSwitchIsPTT makes the radio selector switches
# double as PTT, which is how most SRS profiles are set up.
_PTT_SECTIONS = ("PTT", "Switch1", "Switch2", "Switch3", "Switch4", "Intercom")


def _parse_srs_profile(path: Path) -> list[dict[str, Any]]:
    """Joystick bindings from an SRS input profile (.cfg)."""
    found: list[dict[str, Any]] = []
    section = ""
    current: dict[str, str] = {}

    def flush() -> None:
        if section in _PTT_SECTIONS and current.get("button", "").isdigit():
            found.append(
                {
                    "section": section,
                    "device": current.get("name", "").strip('"'),
                    "button": int(current["button"]),
                }
            )

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return found
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            flush()
            section = line[1:-1]
            current = {}
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            current[key.strip()] = value.strip()
    flush()
    return found


def discover_srs_ptt() -> list[dict[str, Any]]:
    """
    Candidate SRS PTT buttons, matched against connected devices.
    Returns bindings ready to store, each with a 'source' label.
    """
    devices = {str(d["name"]).strip().casefold(): d for d in list_devices()}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for client_dir in SRS_CLIENT_DIRS:
        if not client_dir.is_dir():
            continue
        for cfg in sorted(client_dir.glob("*.cfg")):
            for entry in _parse_srs_profile(cfg):
                dev = devices.get(entry["device"].strip().casefold())
                if not dev:
                    continue
                key = (entry["device"].casefold(), entry["button"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "device": dev["name"],
                        "id": int(dev["id"]),
                        "button": int(entry["button"]),
                        "source": f"{cfg.stem} · {entry['section']}",
                    }
                )
    return out


# ---------- watcher ----------


class JoystickWatcher:
    """
    Polls bound buttons on a daemon thread and fires callbacks on edges.

    Callbacks run on the polling thread — marshal to Tk with `after(0, ...)`.
    """

    def __init__(self, poll_hz: int = POLL_HZ) -> None:
        self._interval = 1.0 / max(10, int(poll_hz))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # name -> (device_id, button, on_press, on_release)
        self._bindings: dict[str, tuple[int, int, Callable[[], None] | None, Callable[[], None] | None]] = {}
        self._learn: Callable[[dict[str, Any]], None] | None = None
        self._learn_baseline: dict[int, int] = {}

    @property
    def supported(self) -> bool:
        return supported()

    def set_binding(
        self,
        name: str,
        binding: dict[str, Any] | None,
        *,
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[], None] | None = None,
    ) -> str | None:
        """Bind (or clear) one action. Returns a warning string when unresolvable."""
        with self._lock:
            self._bindings.pop(name, None)
        if not binding:
            return None
        device_id = resolve_device_id(binding)
        if device_id is None:
            return f"{describe_binding(binding)} — device not connected"
        with self._lock:
            self._bindings[name] = (device_id, int(binding["button"]), on_press, on_release)
        return None

    def clear_bindings(self) -> None:
        with self._lock:
            self._bindings.clear()

    def start(self) -> None:
        if not self.supported or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="atc-joystick", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None

    def learn_next_press(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        """
        Report the next button press on any device as {device, id, button}.
        Buttons already held when learning starts are ignored.
        """
        with self._lock:
            if callback is None:
                self._learn = None
                self._learn_baseline = {}
                return
            baseline: dict[int, int] = {}
            for dev in list_devices():
                mask = read_buttons(int(dev["id"]))
                if mask is not None:
                    baseline[int(dev["id"])] = mask
            self._learn_baseline = baseline
            self._learn = callback
        self.start()

    def _devices_to_poll(self) -> tuple[set[int], bool]:
        with self._lock:
            learning = self._learn is not None
            ids = {device_id for device_id, _b, _p, _r in self._bindings.values()}
        if learning:
            ids |= {int(d["id"]) for d in list_devices()}
        return ids, learning

    def _run(self) -> None:
        previous: dict[int, int] = {}
        while not self._stop.is_set():
            device_ids, learning = self._devices_to_poll()
            if not device_ids:
                self._stop.wait(0.2)
                continue

            masks: dict[int, int] = {}
            for device_id in device_ids:
                mask = read_buttons(device_id)
                if mask is not None:
                    masks[device_id] = mask

            if learning:
                self._check_learn(masks)

            with self._lock:
                bindings = list(self._bindings.values())
            for device_id, button, on_press, on_release in bindings:
                mask = masks.get(device_id)
                if mask is None:
                    continue
                bit = 1 << button
                was = bool(previous.get(device_id, 0) & bit)
                now = bool(mask & bit)
                if now and not was and on_press:
                    self._safe(on_press)
                elif was and not now and on_release:
                    self._safe(on_release)

            previous = masks
            self._stop.wait(self._interval)

    def _check_learn(self, masks: dict[int, int]) -> None:
        with self._lock:
            callback = self._learn
            baseline = dict(self._learn_baseline)
        if not callback:
            return
        names = {int(d["id"]): str(d["name"]) for d in list_devices()}
        for device_id, mask in masks.items():
            newly = mask & ~baseline.get(device_id, 0)
            if not newly:
                continue
            button = (newly & -newly).bit_length() - 1
            with self._lock:
                self._learn = None
                self._learn_baseline = {}
            self._safe(
                lambda: callback(
                    {
                        "device": names.get(device_id, ""),
                        "id": device_id,
                        "button": button,
                    }
                )
            )
            return
        # Track releases so a button held down before learning can still be picked.
        with self._lock:
            for device_id, mask in masks.items():
                self._learn_baseline[device_id] = self._learn_baseline.get(device_id, 0) & mask

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 — a bad callback must not kill polling
            pass
