"""
HOTAS + mouse button input for Advance / Previous.

HOTAS uses the legacy winmm joystick API. Mouse buttons use GetAsyncKeyState —
the same focus-safe poll path as keyboard hotkeys.

Why not keyboard hotkeys alone: DCS and SRS run elevated (SRS ships
`RequireAdmin=true`). Windows UIPI refuses to deliver WM_HOTKEY to a
non-elevated process while an elevated window is focused, so RegisterHotKey
silently does nothing in-game. Reading joystick / mouse state is a device
query rather than an input-stream hook, so it is unaffected by focus or
integrity level.

joyGetPosEx exposes the first 32 buttons of up to 16 devices and costs ~0.5 us
per device per poll, so watching a bound device at 100 Hz is ~0.005% of one
core. Mouse button polls are equally cheap.

Device names: winmm's own product string is usually the useless
'Microsoft PC-joystick driver'. We resolve OEMName from the VID/PID registry
(HKLM then HKCU), keep USB hwid + button/axis counts on each binding, and
dedupe labels when several sticks still share a generic name. Learn can be
scoped to one winmm id so a press on the stick is not attributed to the throttle.

Sharing with SRS: this only ever reads state — no device handle, no DirectInput
acquisition, nothing exclusive — so SRS keeps transmitting on the same button.
Only devices with a binding are polled (all of them briefly, while learning).
"""

from __future__ import annotations

import ctypes
import sys
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
_GENERIC_NAMES = frozenset(
    {
        "",
        "microsoft pc-joystick driver",
        "microsoft pc joystick driver",
        "game port joystick",
    }
)

# Mouse buttons via GetAsyncKeyState (same focus-safe poll path as keyboard).
# Stored 1-based to match Windows / common mouse software numbering.
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06
_MOUSE_VK: dict[int, int] = {
    1: VK_LBUTTON,
    2: VK_RBUTTON,
    3: VK_MBUTTON,
    4: VK_XBUTTON1,
    5: VK_XBUTTON2,
}
_MOUSE_LABELS: dict[int, str] = {
    1: "Left",
    2: "Right",
    3: "Middle",
    4: "Button 4",
    5: "Button 5",
}
_KEY_DOWN = 0x8000


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


def _hwid(vid: int, pid: int) -> str:
    return f"VID_{int(vid) & 0xFFFF:04X}&PID_{int(pid) & 0xFFFF:04X}"


def _is_generic_name(name: str) -> bool:
    return str(name or "").strip().casefold() in _GENERIC_NAMES


def _oem_name(vid: int, pid: int, fallback: str = "") -> str:
    """
    Friendly product name from the joystick OEM registry.

    winmm's szPname is almost always 'Microsoft PC-joystick driver'; the real
    product string (WinWing, Thrustmaster, …) lives under OEM\\VID_&PID_.
    """
    key = _hwid(vid, pid)
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, f"{_OEM_REG_BASE}\\{key}") as k:
                name = str(winreg.QueryValueEx(k, "OEMName")[0]).strip()
                if name and not _is_generic_name(name):
                    return name
        except OSError:
            continue
    fb = str(fallback or "").strip()
    if fb and not _is_generic_name(fb):
        return fb
    return fb or key


def _dedupe_labels(devices: list[dict[str, Any]]) -> None:
    """Ensure each device has a distinct `label` for UI / bindings when names collide."""
    counts: dict[str, int] = {}
    for dev in devices:
        key = str(dev.get("name") or "").strip().casefold() or "joystick"
        counts[key] = counts.get(key, 0) + 1
    seen: dict[str, int] = {}
    for dev in devices:
        name = str(dev.get("name") or "").strip() or "Joystick"
        key = name.casefold()
        if counts.get(key, 0) <= 1 and not _is_generic_name(name):
            dev["label"] = name
            continue
        seen[key] = seen.get(key, 0) + 1
        hwid = str(dev.get("hwid") or "")
        bits = [f"#{seen[key]}", f"id {int(dev['id'])}"]
        if hwid and not hwid.startswith("VID_0000"):
            bits.append(hwid)
        bits.append(f"{int(dev.get('buttons') or 0)}btn")
        bits.append(f"{int(dev.get('axes') or 0)}ax")
        base = name if not _is_generic_name(name) else "Joystick"
        dev["label"] = f"{base} ({', '.join(bits)})"


def list_devices() -> list[dict[str, Any]]:
    """Connected joysticks as {id, name, label, hwid, vid, pid, buttons, axes}."""
    if _winmm is None:
        return []
    out: list[dict[str, Any]] = []
    for jid in range(MAX_DEVICES):
        caps = JOYCAPS()
        if _winmm.joyGetDevCapsW(jid, ctypes.byref(caps), ctypes.sizeof(caps)) != JOYERR_NOERROR:
            continue
        vid = int(caps.wMid)
        pid = int(caps.wPid)
        name = _oem_name(vid, pid, caps.szPname)
        out.append(
            {
                "id": jid,
                "name": name,
                "vid": vid,
                "pid": pid,
                "hwid": _hwid(vid, pid),
                "buttons": int(caps.wNumButtons),
                "axes": int(caps.wNumAxes),
            }
        )
    _dedupe_labels(out)
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


def read_mouse_buttons() -> int:
    """
    Bitmask of mouse buttons currently down: bit 0 = button 1 … bit 4 = button 5.
    Works while another app (DCS) has focus — same GetAsyncKeyState path as keys.
    """
    if sys.platform != "win32":
        return 0
    mask = 0
    user32 = ctypes.windll.user32
    for button, vk in _MOUSE_VK.items():
        if user32.GetAsyncKeyState(int(vk)) & _KEY_DOWN:
            mask |= 1 << (button - 1)
    return mask


def is_mouse_binding(binding: dict[str, Any] | None) -> bool:
    return bool(binding) and str(binding.get("kind") or "").strip().casefold() == "mouse"


# ---------- bindings ----------


def normalize_binding(raw: Any) -> dict[str, Any] | None:
    """
    Accepts HOTAS {"device": name, "id": n, "button": i, ...}, mouse
    {"kind": "mouse", "button": 1..5}, or the legacy "id:button" string.
    HOTAS `button` is a 0-based bit index (UI shows 1-based to match DCS).
    Mouse `button` is 1-based (Left/Right/Middle/4/5).
    """
    if not raw:
        return None
    if isinstance(raw, str):
        parts = raw.replace(" ", "").split(":")
        if len(parts) == 2 and parts[0].casefold() == "mouse" and parts[1].isdigit():
            button = int(parts[1])
            if button in _MOUSE_VK:
                return {"kind": "mouse", "button": button}
        if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
            return None
        return {"device": "", "id": int(parts[0]), "button": int(parts[1])}
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().casefold()
    try:
        button = int(raw.get("button"))
    except (TypeError, ValueError):
        return None
    if kind == "mouse" or str(raw.get("device") or "").strip().casefold() == "mouse":
        if button not in _MOUSE_VK:
            return None
        return {"kind": "mouse", "button": button}
    if not 0 <= button < MAX_BUTTONS:
        return None
    try:
        device_id = int(raw.get("id"))
    except (TypeError, ValueError):
        device_id = -1
    out: dict[str, Any] = {
        "device": str(raw.get("device") or ""),
        "id": device_id,
        "button": button,
    }
    # Stable identity extras — older configs omit these and still resolve by name/id.
    for key in ("label", "hwid", "vid", "pid", "buttons", "axes"):
        if key not in raw:
            continue
        val = raw[key]
        if val is None or val == "":
            continue
        if key in ("vid", "pid", "buttons", "axes"):
            try:
                out[key] = int(val)
            except (TypeError, ValueError):
                continue
        else:
            out[key] = str(val)
    if "hwid" not in out and "vid" in out and "pid" in out:
        out["hwid"] = _hwid(int(out["vid"]), int(out["pid"]))
    return out


def _score_device(binding: dict[str, Any], dev: dict[str, Any]) -> int:
    """Higher is better. Negative means hard mismatch on a known fingerprint."""
    score = 0
    hwid = str(binding.get("hwid") or "").strip().casefold()
    if hwid:
        if str(dev.get("hwid") or "").strip().casefold() == hwid:
            score += 100
        else:
            return -1
    else:
        try:
            vid = int(binding["vid"])
            pid = int(binding["pid"])
        except (KeyError, TypeError, ValueError):
            vid = pid = None
        if vid is not None and pid is not None:
            if int(dev.get("vid", -1)) == vid and int(dev.get("pid", -1)) == pid:
                score += 100
            else:
                return -1

    name = str(binding.get("device") or "").strip().casefold()
    label = str(binding.get("label") or "").strip().casefold()
    if label and str(dev.get("label") or "").strip().casefold() == label:
        score += 40
    if name and not _is_generic_name(name):
        if str(dev.get("name") or "").strip().casefold() == name:
            score += 30
        else:
            return -1
    elif name and str(dev.get("name") or "").strip().casefold() == name:
        score += 5  # weak — generic names collide across sticks

    for key, weight in (("buttons", 8), ("axes", 4)):
        try:
            want = int(binding[key])
        except (KeyError, TypeError, ValueError):
            continue
        if int(dev.get(key, -1)) == want:
            score += weight

    saved = binding.get("id")
    if isinstance(saved, int) and saved >= 0 and int(dev["id"]) == saved:
        score += 20
    return score


def resolve_device_id(binding: dict[str, Any]) -> int | None:
    """
    winmm ids shift when devices are unplugged. Prefer USB VID/PID + product
    fingerprint; never return the first of several identical generic names.
    """
    if is_mouse_binding(binding):
        return None
    devices = list_devices()
    if not devices:
        return None

    ranked: list[tuple[int, dict[str, Any]]] = []
    for dev in devices:
        score = _score_device(binding, dev)
        if score >= 0:
            ranked.append((score, dev))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], int(item[1]["id"])))
    best_score, best = ranked[0]
    # Require a real fingerprint when several generic devices are present,
    # otherwise the saved winmm id is the only disambiguator left.
    if best_score == 0:
        return None
    if best_score < 20 and len(ranked) > 1 and ranked[1][0] == best_score:
        saved = binding.get("id")
        if isinstance(saved, int) and saved >= 0:
            for score, dev in ranked:
                if int(dev["id"]) == saved:
                    return saved
        return None
    return int(best["id"])


def describe_binding(binding: dict[str, Any] | None) -> str:
    if not binding:
        return "(none)"
    if is_mouse_binding(binding):
        button = int(binding["button"])
        return f"Mouse · {_MOUSE_LABELS.get(button, f'Button {button}')}"
    name = (
        binding.get("label")
        or binding.get("device")
        or f"Joystick {binding.get('id')}"
    )
    return f"{name} · Btn {int(binding['button']) + 1}"


def binding_from_config(config: dict[str, Any], which: str) -> dict[str, Any] | None:
    return normalize_binding(config.get(f"joy_{which}"))


def binding_from_device(dev: dict[str, Any], button: int) -> dict[str, Any]:
    """Build a storeable HOTAS binding from a list_devices() row + button index."""
    return {
        "device": str(dev.get("name") or ""),
        "label": str(dev.get("label") or dev.get("name") or ""),
        "id": int(dev["id"]),
        "button": int(button),
        "vid": int(dev.get("vid") or 0),
        "pid": int(dev.get("pid") or 0),
        "hwid": str(dev.get("hwid") or _hwid(int(dev.get("vid") or 0), int(dev.get("pid") or 0))),
        "buttons": int(dev.get("buttons") or 0),
        "axes": int(dev.get("axes") or 0),
    }


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
    devices = list_devices()
    by_name: dict[str, list[dict[str, Any]]] = {}
    for d in devices:
        name_key = str(d["name"]).strip().casefold()
        by_name.setdefault(name_key, []).append(d)
        label_key = str(d.get("label") or "").strip().casefold()
        if label_key and label_key != name_key:
            by_name.setdefault(label_key, []).append(d)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for client_dir in SRS_CLIENT_DIRS:
        if not client_dir.is_dir():
            continue
        for cfg in sorted(client_dir.glob("*.cfg")):
            for entry in _parse_srs_profile(cfg):
                srs_name = entry["device"].strip()
                key_name = srs_name.casefold()
                candidates = by_name.get(key_name) or []
                if not candidates:
                    # SRS uses DirectInput names; winmm may only have OEM / generic.
                    candidates = [
                        d
                        for d in devices
                        if key_name
                        and (
                            key_name in str(d.get("name") or "").casefold()
                            or key_name in str(d.get("label") or "").casefold()
                            or str(d.get("name") or "").casefold() in key_name
                        )
                    ]
                if len(candidates) != 1:
                    continue
                dev = candidates[0]
                key = (str(dev.get("hwid") or dev["name"]).casefold(), entry["button"])
                if key in seen:
                    continue
                seen.add(key)
                binding = binding_from_device(dev, int(entry["button"]))
                binding["source"] = f"{cfg.stem} · {entry['section']}"
                out.append(binding)
    return out


# ---------- watcher ----------


class JoystickWatcher:
    """
    Polls bound HOTAS / mouse buttons on a daemon thread and fires callbacks on edges.

    Callbacks run on the polling thread — marshal to Tk with `after(0, ...)`.
    """

    def __init__(self, poll_hz: int = POLL_HZ) -> None:
        self._interval = 1.0 / max(10, int(poll_hz))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # name -> (kind, device_id_or_-1, button, on_press, on_release)
        # kind "joy": device_id + 0-based button bit; "mouse": button 1..5
        self._bindings: dict[
            str, tuple[str, int, int, Callable[[], None] | None, Callable[[], None] | None]
        ] = {}
        self._learn: Callable[[dict[str, Any]], None] | None = None
        self._learn_baseline: dict[int, int] = {}
        self._learn_mouse_baseline: int = 0
        self._learn_device_id: int | None = None  # None = any device; else winmm id only

    @property
    def supported(self) -> bool:
        # Mouse polling works on Windows even when no joystick is present.
        return supported() or sys.platform == "win32"

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
        if is_mouse_binding(binding):
            if sys.platform != "win32":
                return f"{describe_binding(binding)} — mouse buttons need Windows"
            with self._lock:
                self._bindings[name] = ("mouse", -1, int(binding["button"]), on_press, on_release)
            return None
        device_id = resolve_device_id(binding)
        if device_id is None:
            return f"{describe_binding(binding)} — device not connected"
        with self._lock:
            self._bindings[name] = ("joy", device_id, int(binding["button"]), on_press, on_release)
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

    def learn_next_press(
        self,
        callback: Callable[[dict[str, Any]], None] | None,
        *,
        device_id: int | None = None,
        allow_mouse: bool = True,
    ) -> None:
        """
        Report the next HOTAS or mouse button press.
        Buttons already held when learning starts are ignored.
        Pass `device_id` to ignore presses on every other stick (needed when
        several devices share the generic Microsoft winmm name).
        """
        with self._lock:
            if callback is None:
                self._learn = None
                self._learn_baseline = {}
                self._learn_mouse_baseline = 0
                self._learn_device_id = None
                return
            baseline: dict[int, int] = {}
            for dev in list_devices():
                jid = int(dev["id"])
                if device_id is not None and jid != int(device_id):
                    continue
                mask = read_buttons(jid)
                if mask is not None:
                    baseline[jid] = mask
            self._learn_baseline = baseline
            self._learn_mouse_baseline = read_mouse_buttons() if allow_mouse else 0
            self._learn_device_id = None if device_id is None else int(device_id)
            self._learn = callback
        self.start()

    def _devices_to_poll(self) -> tuple[set[int], bool, bool]:
        with self._lock:
            learning = self._learn is not None
            learn_id = self._learn_device_id
            ids = {
                device_id
                for kind, device_id, _b, _p, _r in self._bindings.values()
                if kind == "joy"
            }
            need_mouse = learning or any(kind == "mouse" for kind, *_ in self._bindings.values())
        if learning:
            if learn_id is not None:
                ids |= {int(learn_id)}
            else:
                ids |= {int(d["id"]) for d in list_devices()}
        return ids, learning, need_mouse

    def _run(self) -> None:
        previous: dict[int, int] = {}
        prev_mouse = 0
        while not self._stop.is_set():
            device_ids, learning, need_mouse = self._devices_to_poll()
            if not device_ids and not need_mouse:
                self._stop.wait(0.2)
                continue

            masks: dict[int, int] = {}
            for device_id in device_ids:
                mask = read_buttons(device_id)
                if mask is not None:
                    masks[device_id] = mask
            mouse_mask = read_mouse_buttons() if need_mouse else 0

            if learning:
                self._check_learn(masks, mouse_mask)

            with self._lock:
                bindings = list(self._bindings.values())
            for kind, device_id, button, on_press, on_release in bindings:
                if kind == "mouse":
                    bit = 1 << (button - 1)
                    was = bool(prev_mouse & bit)
                    now = bool(mouse_mask & bit)
                else:
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
            prev_mouse = mouse_mask
            self._stop.wait(self._interval)

    def _check_learn(self, masks: dict[int, int], mouse_mask: int) -> None:
        with self._lock:
            callback = self._learn
            baseline = dict(self._learn_baseline)
            mouse_baseline = self._learn_mouse_baseline
            learn_id = self._learn_device_id
        if not callback:
            return

        if learn_id is None:
            newly_mouse = mouse_mask & ~mouse_baseline
            if newly_mouse:
                button = (newly_mouse & -newly_mouse).bit_length()  # 1-based
                with self._lock:
                    self._learn = None
                    self._learn_baseline = {}
                    self._learn_mouse_baseline = 0
                    self._learn_device_id = None
                self._safe(lambda: callback({"kind": "mouse", "button": button}))
                return

        devices = {int(d["id"]): d for d in list_devices()}
        for device_id, mask in masks.items():
            if learn_id is not None and int(device_id) != int(learn_id):
                continue
            newly = mask & ~baseline.get(device_id, 0)
            if not newly:
                continue
            button = (newly & -newly).bit_length() - 1
            dev = devices.get(int(device_id))
            with self._lock:
                self._learn = None
                self._learn_baseline = {}
                self._learn_mouse_baseline = 0
                self._learn_device_id = None
            if dev:
                payload = binding_from_device(dev, button)
            else:
                payload = {
                    "device": "",
                    "id": device_id,
                    "button": button,
                }
            self._safe(lambda p=payload: callback(p))
            return
        # Track releases so a button held down before learning can still be picked.
        with self._lock:
            for device_id, mask in masks.items():
                if learn_id is not None and int(device_id) != int(learn_id):
                    continue
                self._learn_baseline[device_id] = self._learn_baseline.get(device_id, 0) & mask
            if learn_id is None:
                self._learn_mouse_baseline &= mouse_mask

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 — a bad callback must not kill polling
            pass
