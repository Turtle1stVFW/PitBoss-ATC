"""
Fly hotkeys for Next / Back — configurable strings + Windows global RegisterHotKey.

Config examples:
  "F13"
  "Ctrl+Shift+Right"
  "Alt+Page_Down"
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


class GlobalHotkeyListener:
    """
    Windows thread that RegisterHotKey's Next/Back and invokes callbacks.
    No-op on non-Windows.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._tid: int | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bindings: dict[int, Callable[[], None]] = {}
        self._specs: list[tuple[int, int, int, Callable[[], None]]] = []  # id, mods, vk, cb

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

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

        specs: list[tuple[int, int, int, Callable[[], None]]] = []
        for hid, raw, cb, label in (
            (1, next_hotkey, on_next, "Next"),
            (2, back_hotkey, on_back, "Back"),
        ):
            parsed = parse_hotkey(raw)
            if not parsed:
                warnings.append(f"Invalid {label} hotkey: {raw!r}")
                continue
            mods, vk, _seq = parsed
            specs.append((hid, mods, vk, cb))

        if not specs:
            return warnings

        self._specs = specs
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="atc-hotkeys", daemon=True)
        self._thread.start()
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
        self._tid = threading.get_ident()
        registered: list[int] = []
        try:
            for hid, mods, vk, cb in self._specs:
                ok = user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk)
                if not ok:
                    continue
                registered.append(hid)
                with self._lock:
                    self._bindings[hid] = cb
            msg = wintypes.MSG()
            while not self._stop.is_set():
                # Use Peek + sleep so we can exit cleanly; GetMessage blocks forever
                has = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)  # PM_REMOVE=1
                if has:
                    if msg.message == WM_QUIT:
                        break
                    if msg.message == WM_HOTKEY:
                        hid = int(msg.wParam)
                        with self._lock:
                            cb = self._bindings.get(hid)
                        if cb:
                            try:
                                cb()
                            except Exception:
                                pass
                    else:
                        user32.TranslateMessage(ctypes.byref(msg))
                        user32.DispatchMessageW(ctypes.byref(msg))
                else:
                    self._stop.wait(0.05)
        finally:
            for hid in registered:
                try:
                    user32.UnregisterHotKey(None, hid)
                except Exception:
                    pass
            with self._lock:
                self._bindings.clear()
