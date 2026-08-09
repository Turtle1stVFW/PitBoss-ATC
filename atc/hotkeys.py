"""
Fly hotkeys for Next / Back — configurable strings + Windows global RegisterHotKey.

Config examples:
  "F13"
  "Ctrl+Shift+Right"
  "Alt+Page_Down"

Two ways to read the keyboard, because RegisterHotKey alone is not enough:

  * `GlobalHotkeyListener` — RegisterHotKey. Swallows the keystroke so it never
    reaches the game, but Windows UIPI will not deliver WM_HOTKEY to a
    normal-integrity process while an elevated window is focused, and DCS and
    SRS both run as administrator. Hence `elevation_warning()`.
  * `KeyWatcher` — polls GetAsyncKeyState. Survives DCS having focus without
    elevation and reports release as well as press, so it can drive
    hold-to-talk. Does not consume the keystroke.

Both run at once for Next / Back; the UI debounces so a press fires once.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

# Defaults work well with Stream Deck Hotkey actions (rarely collide with DCS)
DEFAULT_HOTKEY_NEXT = "F13"
DEFAULT_HOTKEY_BACK = "F14"

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

ERROR_HOTKEY_ALREADY_REGISTERED = 1409
QS_ALLINPUT = 0x04FF
WAIT_OBJECT_0 = 0x00000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5

_VK_BY_NAME: dict[str, int] = {
    **{f"f{i}": 0x70 + (i - 1) for i in range(1, 25)},
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "page_up": 0x21,
    "pageup": 0x21,
    "prior": 0x21,
    "page_down": 0x22,
    "pagedown": 0x22,
    "next": 0x22,
    "home": 0x24,
    "end": 0x23,
    "insert": 0x2D,
    "delete": 0x2E,
    "space": 0x20,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "return": 0x0D,
    "enter": 0x0D,
    "backspace": 0x08,
    "plus": 0xBB,
    "minus": 0xBD,
    "comma": 0xBC,
    "period": 0xBE,
}

_NAME_BY_VK: dict[int, str] = {v: k.upper() if len(k) <= 3 else k.replace("_", "_").title().replace("Page_Up", "Page_Up") for k, v in _VK_BY_NAME.items()}
for i in range(1, 25):
    _NAME_BY_VK[0x70 + (i - 1)] = f"F{i}"
_NAME_BY_VK[0x25] = "Left"
_NAME_BY_VK[0x26] = "Up"
_NAME_BY_VK[0x27] = "Right"
_NAME_BY_VK[0x28] = "Down"
_NAME_BY_VK[0x21] = "Page_Up"
_NAME_BY_VK[0x22] = "Page_Down"
_NAME_BY_VK[0x24] = "Home"
_NAME_BY_VK[0x23] = "End"
_NAME_BY_VK[0x2D] = "Insert"
_NAME_BY_VK[0x2E] = "Delete"
_NAME_BY_VK[0x20] = "Space"


def normalize_hotkey(raw: str | None, *, default: str = "") -> str:
    """Canonical display form: Ctrl+Shift+F13."""
    text = str(raw or "").strip()
    if not text:
        return default
    text = text.replace("-", "+").replace(" ", "")
    parts = [p for p in text.split("+") if p]
    if not parts:
        return default
    mods: list[str] = []
    key = ""
    for p in parts:
        low = p.casefold()
        if low in ("ctrl", "control", "ctl"):
            mods.append("Ctrl")
        elif low in ("alt", "option"):
            mods.append("Alt")
        elif low in ("shift",):
            mods.append("Shift")
        elif low in ("win", "windows", "super", "meta", "cmd", "command"):
            mods.append("Win")
        else:
            key = p
    if not key:
        return default
    # Normalize key token
    kl = key.casefold().replace(" ", "")
    if kl in _VK_BY_NAME:
        key = _NAME_BY_VK.get(_VK_BY_NAME[kl], key.upper() if len(key) == 1 else key)
    elif len(key) == 1:
        key = key.upper()
    else:
        key = key[0].upper() + key[1:]
    order = ["Ctrl", "Alt", "Shift", "Win"]
    mods_sorted = [m for m in order if m in mods]
    return "+".join(mods_sorted + [key])


def parse_hotkey(raw: str | None) -> tuple[int, int, str] | None:
    """
    Returns (win_mods, vk, tk_sequence) or None if invalid.
    tk_sequence like '<Control-Shift-F13>' for bind_all fallback.
    """
    canon = normalize_hotkey(raw)
    if not canon:
        return None
    parts = canon.split("+")
    mods = 0
    tk_mods: list[str] = []
    key = parts[-1]
    for p in parts[:-1]:
        if p == "Ctrl":
            mods |= MOD_CONTROL
            tk_mods.append("Control")
        elif p == "Alt":
            mods |= MOD_ALT
            tk_mods.append("Alt")
        elif p == "Shift":
            mods |= MOD_SHIFT
            tk_mods.append("Shift")
        elif p == "Win":
            mods |= MOD_WIN
            tk_mods.append("Win")
    kl = key.casefold().replace(" ", "")
    if kl in _VK_BY_NAME:
        vk = _VK_BY_NAME[kl]
        tk_key = key if key.startswith("F") or "_" in key or key in {
            "Left", "Right", "Up", "Down", "Home", "End", "Insert", "Delete", "Space", "Tab", "Escape", "Return"
        } else key
        # Tk uses Page_Up / Page_Down / Next
        if kl in ("page_up", "pageup", "prior"):
            tk_key = "Prior"
        elif kl in ("page_down", "pagedown", "next"):
            tk_key = "Next"
        elif kl == "enter":
            tk_key = "Return"
        elif kl == "esc":
            tk_key = "Escape"
    elif len(key) == 1 and key.isalpha():
        vk = ord(key.upper())
        tk_key = key.lower()
    elif len(key) == 1 and key.isdigit():
        vk = ord(key)
        tk_key = key
    else:
        return None
    seq = "<" + "-".join(tk_mods + [tk_key]) + ">"
    return mods, vk, seq


def is_elevated() -> bool:
    """True when this process runs with an administrator token."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def foreground_process() -> tuple[str, bool]:
    """
    (executable name, looks_elevated) for the focused window.

    A normal-integrity process cannot even query a higher-integrity one, so
    ERROR_ACCESS_DENIED from OpenProcess is a reliable signal that the
    foreground app outranks us.
    """
    if sys.platform != "win32":
        return "", False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return "", False
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return "", kernel32.GetLastError() == ERROR_ACCESS_DENIED
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1], False
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass
    return "", False


def elevation_warning() -> str:
    """Non-empty when RegisterHotKey alone would be swallowed in-game."""
    if sys.platform != "win32" or is_elevated():
        return ""
    return (
        "Not elevated, so DCS/SRS (which run as admin) swallow registered hotkeys. "
        "Key polling covers this — if a key still does nothing in-game, restart as "
        "administrator or bind a HOTAS button."
    )


def hotkey_from_config(config: dict[str, Any], which: str) -> str:
    if which == "next":
        return normalize_hotkey(config.get("hotkey_next"), default=DEFAULT_HOTKEY_NEXT)
    if which == "back":
        return normalize_hotkey(config.get("hotkey_back"), default=DEFAULT_HOTKEY_BACK)
    return ""


def format_event(event: Any) -> str:
    """Build a hotkey string from a Tk key event (for Capture)."""
    mods: list[str] = []
    state = int(getattr(event, "state", 0) or 0)
    # Tk state bits: Shift=0x0001, Control=0x0004, Alt/Mod1=0x20000 (platform varies)
    if state & 0x4:
        mods.append("Ctrl")
    if state & 0x20000 or state & 0x8:  # Alt / Mod1
        mods.append("Alt")
    if state & 0x1:
        mods.append("Shift")
    if state & 0x40000:  # Mod4 / Win on some builds
        mods.append("Win")
    keysym = str(getattr(event, "keysym", "") or "")
    if not keysym or keysym in (
        "Control_L", "Control_R", "Shift_L", "Shift_R",
        "Alt_L", "Alt_R", "Meta_L", "Meta_R", "Win_L", "Win_R", "Super_L", "Super_R",
    ):
        return ""
    # Map Tk keysyms to our names
    ks = keysym
    if ks in ("Prior",):
        ks = "Page_Up"
    elif ks in ("Next",):
        ks = "Page_Down"
    elif ks.startswith("F") and ks[1:].isdigit():
        pass
    elif len(ks) == 1:
        ks = ks.upper()
    return normalize_hotkey("+".join(mods + [ks]))


VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C
KEY_POLL_HZ = 100
_KEY_DOWN = 0x8000


def _key_down(vk: int) -> bool:
    """Current physical state of a key, ignoring the 'pressed since last call' bit."""
    return bool(ctypes.windll.user32.GetAsyncKeyState(int(vk)) & _KEY_DOWN)


def _win_down() -> bool:
    return _key_down(VK_LWIN) or _key_down(VK_RWIN)


class KeyWatcher:
    """
    Keyboard triggers by polling key state instead of registering a hotkey.

    Two things RegisterHotKey cannot do. It delivers WM_HOTKEY through the
    message queue, which UIPI closes off while an elevated window is focused —
    the reason hotkeys die inside DCS. And it only ever reports a press, so
    hold-to-talk is impossible. GetAsyncKeyState just reports the state of the
    keyboard, exactly as joyGetPosEx reports the state of a stick, so it keeps
    working in-game unelevated and gives both edges.

    The trade-off is that polling does not swallow the keystroke: the key still
    reaches DCS, so pick a combination DCS does not use.

    Callbacks run on the polling thread — marshal to Tk with `after(0, ...)`.
    """

    def __init__(self, poll_hz: int = KEY_POLL_HZ) -> None:
        self._interval = 1.0 / max(10, int(poll_hz))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # name -> (mods, vk, on_press, on_release)
        self._bindings: dict[str, tuple[int, int, Callable[[], None] | None, Callable[[], None] | None]] = {}

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def set_binding(
        self,
        name: str,
        hotkey: str | None,
        *,
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[], None] | None = None,
    ) -> str:
        """Bind a hotkey string. Returns a warning, or "" when it took."""
        parsed = parse_hotkey(hotkey)
        if not parsed:
            with self._lock:
                self._bindings.pop(name, None)
            return f"Could not read the key combination “{hotkey}”." if hotkey else ""
        mods, vk, _seq = parsed
        with self._lock:
            self._bindings[name] = (mods, vk, on_press, on_release)
        return ""

    def clear_bindings(self) -> None:
        with self._lock:
            self._bindings.clear()

    def start(self) -> None:
        if not self.supported or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="atc-keys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def _safe(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass

    def _combo_down(self, mods: int, vk: int) -> bool:
        """Exact match, so Ctrl+N does not fire while Ctrl+Shift+N is held."""
        if not _key_down(vk):
            return False
        return (
            _key_down(VK_CONTROL) == bool(mods & MOD_CONTROL)
            and _key_down(VK_MENU) == bool(mods & MOD_ALT)
            and _key_down(VK_SHIFT) == bool(mods & MOD_SHIFT)
            and _win_down() == bool(mods & MOD_WIN)
        )

    def _run(self) -> None:
        previous: dict[str, bool] = {}
        while not self._stop.is_set():
            with self._lock:
                bindings = list(self._bindings.items())
            if not bindings:
                self._stop.wait(0.2)
                continue

            for name, (mods, vk, on_press, on_release) in bindings:
                now = self._combo_down(mods, vk)
                was = previous.get(name, False)
                if now and not was and on_press:
                    self._safe(on_press)
                elif was and not now and on_release:
                    self._safe(on_release)
                previous[name] = now

            self._stop.wait(self._interval)


class GlobalHotkeyListener:
    """
    Windows thread that RegisterHotKey's Next/Back and invokes callbacks.
    No-op on non-Windows.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._tid: int | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._bindings: dict[int, Callable[[], None]] = {}
        self._specs: list[tuple[int, int, int, str, Callable[[], None]]] = []
        self._register_errors: list[str] = []
        self._registered_labels: list[str] = []
        self.on_trigger: Callable[[str], None] | None = None

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    @property
    def registered(self) -> list[str]:
        """Labels of hotkeys the OS actually accepted."""
        with self._lock:
            return list(self._registered_labels)

    def start(
        self,
        *,
        next_hotkey: str,
        back_hotkey: str,
        on_next: Callable[[], None],
        on_back: Callable[[], None],
    ) -> list[str]:
        """(Re)start listener. Returns list of warning strings."""
        warnings: list[str] = []
        self.stop()
        if not self.supported:
            warnings.append("Global hotkeys require Windows; using in-app binds only.")
            return warnings

        specs: list[tuple[int, int, int, str, Callable[[], None]]] = []
        for hid, raw, cb, label in (
            (1, next_hotkey, on_next, "Next"),
            (2, back_hotkey, on_back, "Back"),
        ):
            parsed = parse_hotkey(raw)
            if not parsed:
                warnings.append(f"Invalid {label} hotkey: {raw!r}")
                continue
            mods, vk, _seq = parsed
            specs.append((hid, mods, vk, f"{label} {raw}", cb))

        if not specs:
            return warnings

        self._specs = specs
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="atc-hotkeys", daemon=True)
        self._thread.start()

        # RegisterHotKey happens on the listener thread; wait for its verdict so
        # failures reach the UI instead of being silently swallowed.
        self._ready.wait(timeout=2.0)
        with self._lock:
            warnings.extend(self._register_errors)

        note = elevation_warning()
        if note:
            warnings.append(note)
        return warnings

    def stop(self) -> None:
        if not self.supported:
            return
        self._stop.set()
        tid = self._tid
        if tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None
        self._tid = None
        self._bindings.clear()

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._tid = int(kernel32.GetCurrentThreadId())
        registered: list[int] = []
        errors: list[str] = []
        labels: list[str] = []
        try:
            for hid, mods, vk, label, cb in self._specs:
                if user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                    registered.append(hid)
                    labels.append(label)
                    with self._lock:
                        self._bindings[hid] = cb
                    continue
                err = kernel32.GetLastError()
                if err == ERROR_HOTKEY_ALREADY_REGISTERED:
                    errors.append(f"{label} is already taken by another app")
                else:
                    errors.append(f"{label} could not be registered (error {err})")
            with self._lock:
                self._register_errors = errors
                self._registered_labels = labels
            self._ready.set()

            msg = wintypes.MSG()
            while not self._stop.is_set():
                # Drain before waiting: MsgWaitForMultipleObjects only signals on
                # input that arrives after the call, so anything already queued
                # would otherwise sit there until the next unrelated wake-up.
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                    if msg.message == WM_QUIT:
                        return
                    if msg.message == WM_HOTKEY:
                        hid = int(msg.wParam)
                        with self._lock:
                            cb = self._bindings.get(hid)
                        if cb:
                            self._notify(hid)
                            try:
                                cb()
                            except Exception:
                                pass
                    else:
                        user32.TranslateMessage(ctypes.byref(msg))
                        user32.DispatchMessageW(ctypes.byref(msg))
                # Block until new input, waking periodically to notice stop().
                user32.MsgWaitForMultipleObjectsEx(0, None, 250, QS_ALLINPUT, 0)
        finally:
            self._ready.set()
            for hid in registered:
                try:
                    user32.UnregisterHotKey(None, hid)
                except Exception:
                    pass
            with self._lock:
                self._bindings.clear()
                self._registered_labels = []

    def _notify(self, hid: int) -> None:
        observer = self.on_trigger
        if not observer:
            return
        try:
            observer("Next" if hid == 1 else "Back")
        except Exception:  # noqa: BLE001
            pass
