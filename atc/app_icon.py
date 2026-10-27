"""PitBoss ATC window icon.

The Start menu and desktop shortcuts point at a .cmd launcher, which starts
pythonw.exe. Windows would otherwise put the Python icon on the taskbar.
Claiming an app id before the first window, then setting that window's icon,
makes the taskbar use this logo instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ICON_PNG = HERE / "PitBossATC.png"
ICON_ICO = HERE / "PitBossATC.ico"

# Same id as the installer shortcuts, so a pinned Start or desktop icon
# groups with the running window.
APP_USER_MODEL_ID = "PitBossATC.FlightFlow"

_claimed = False


def claim_taskbar_identity() -> None:
    """Tell Windows this process is PitBoss ATC, not pythonw.exe.

    Call this before the first Tk window is created. A later call does not
    change a window that is already on the taskbar.
    """
    global _claimed
    if _claimed or sys.platform != "win32":
        return
    _claimed = True
    try:
        import ctypes

        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.HRESULT
        shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        return


def apply_window_icon(root) -> None:
    """Set the title-bar and taskbar icon. Missing art must not stop startup."""
    if ICON_PNG.is_file():
        try:
            import tkinter as tk

            photo = tk.PhotoImage(file=str(ICON_PNG))
            root.iconphoto(True, photo)
            # Tk drops the icon if this PhotoImage is garbage-collected.
            root._pitboss_icon = photo
            return
        except Exception:
            pass
    if ICON_ICO.is_file():
        try:
            root.iconbitmap(default=str(ICON_ICO))
        except Exception:
            return
