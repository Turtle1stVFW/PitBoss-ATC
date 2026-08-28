#!/usr/bin/env python3
"""
455 Mission Flow Planner — one timeline, plan then fly with Play Next.
No JSON editing required.
"""

from __future__ import annotations

import copy
import ctypes
import json
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atc_phrase  # noqa: E402
import atc_client  # noqa: E402
import atc_net  # noqa: E402
import atc_server  # noqa: E402
import flow_engine  # noqa: E402
import hotkeys  # noqa: E402
import joystick  # noqa: E402
import kneeboard_pdf  # noqa: E402
import install_dcs_radio_export  # noqa: E402
import mic_capture  # noqa: E402
import runway_position  # noqa: E402
import srs_radio  # noqa: E402
import voice_actions  # noqa: E402
import voice_engine  # noqa: E402
import voice_intent  # noqa: E402

CONFIG_PATH = HERE / "config.json"
AIRPORTS_PATH = HERE / "airports.json"

# The zone drawing tool is a separate program: a small web server that puts a map
# in the browser. Setup and the step editor both launch it, and Open-Zone-Editor.cmd
# starts the same thing without the app.
ZONE_TOOL = HERE.parent / "tools" / "zone_server.py"
ZONE_EDITOR_PORT = 8777
ZONE_EDITOR_LOG = Path(tempfile.gettempdir()) / "atc-zone-editor.log"

# Shown when a step has no zone trigger, in the picker and on the button.
TRIG_NONE = "(nothing — this step waits to be asked)"

# Trigger tags the zone editor offers, so the picker can show which of them have
# an area drawn for this field and which are still gaps. Anything else found in
# airports.json is listed too — a tag is just a string both ends agree on.
ZONE_TAGS = (
    "in_position",
    "eor",
    "hold_short",
    "parking",
    "delivery",
    "ground",
    "tower",
    "departure",
    "approach",
)

# One press reaches us by several routes at once; ignore the echoes.
TRIGGER_DEBOUNCE_S = 0.25

# Dark aviation palette
C_BG = "#0c1117"
C_PANEL = "#151c27"
C_CARD = "#1c2636"
C_BORDER = "#2a3648"
C_TEXT = "#e8eef7"
C_LABEL = "#c8d4e6"  # form labels — higher contrast than muted
C_MUTED = "#9aabc4"
C_ACCENT = "#4c9afe"
C_GREEN = "#3dd68c"
C_AMBER = "#f0b429"
C_RED = "#f07178"

CHANNEL_COLORS = {
    "delivery": "#7aa2f7",
    "ground": "#9ece6a",
    "tower": "#e0af68",
    "departure": "#bb9af7",
    "approach": "#7dcfff",
    "blackjack": "#f7768e",
    "ops": "#ff9e64",
    "other": "#c0caf5",
}


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def slug_id(label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "step"
    return f"{base}_{uuid.uuid4().hex[:6]}"


class MissionPlanner(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("455 Mission Flow Planner")
        self.geometry("1240x820")
        self.minsize(1020, 700)
        self.configure(bg=C_BG)

        self.engine = flow_engine.FlowEngine()
        self.mission = flow_engine.normalize_mission_to_steps(self.engine.mission)
        self.engine.mission = self.mission
        self.config_data = self.engine.config
        # Google retired en-CA WaveNet; rewrite saved ids before anything synthesizes.
        voice_notes = atc_phrase.migrate_retired_google_voices(self.config_data)
        for note in voice_notes:
            print(note, file=sys.stderr)
        # Persist a repaired flow_file (or retired-voice remap) so the next launch
        # does not hit the same missing-mission crash.
        repaired = bool(self.config_data.pop("_flow_file_repaired", False))
        if voice_notes or repaired:
            try:
                save_json(CONFIG_PATH, self.config_data)
            except Exception:
                pass
            if repaired:
                print(
                    f"Restored missing mission → {self.config_data.get('flow_file')}",
                    file=sys.stderr,
                )
        self.airports = self.engine.airports
        self.selected_index: int | None = None
        self._loading = False
        self._setup_fields_loaded = False

        self.http = None
        try:
            port = int(self.config_data.get("flow_http_port") or 8765)
            self.http = flow_engine.start_http_server(self.engine, port)
        except OSError:
            pass

        self._hotkey_listener = hotkeys.GlobalHotkeyListener()
        self._hotkey_listener.on_trigger = self._on_trigger_received
        self._tk_hotkey_binds: list[str] = []
        self._keys = hotkeys.KeyWatcher()
        self._joystick = joystick.JoystickWatcher()
        self._joy_bindings: dict[str, dict[str, Any] | None] = {
            "next": None,
            "back": None,
            "seek_next": None,
            "seek_prev": None,
        }
        self._trigger_lock = threading.Lock()
        self._trigger_last: dict[str, float] = {}
        self._voice: voice_engine.VoiceController | None = None
        self._zone_proc: subprocess.Popen[bytes] | None = None
        self._airports_mtime = self._airports_mtime_now()
        self._atc_server: atc_server.AtcServer | None = None
        self._atc_client: atc_client.AtcClient | None = None
        self._pos_trackers: dict[str, runway_position.PositionTracker] = {}

        srs_radio.apply_config(self.config_data)
        srs_radio.ensure_srs_udp_listener()
        self._style()
        self._build()
        self._load_setup_fields()
        self.refresh_timeline()
        self._apply_hotkeys()
        self._apply_voice()
        self._sync_atc_runtime()
        self._schedule_freq_gate_poll()
        self._schedule_position_poll()
        self._schedule_airports_poll()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # Combobox popdown list (Windows often stays white without this)
        self.option_add("*TCombobox*Listbox.background", C_CARD)
        self.option_add("*TCombobox*Listbox.foreground", C_TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", C_ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#061018")
        self.option_add("*TCombobox*Listbox.font", "Segoe UI 10")

        style.configure(".", background=C_BG, foreground=C_TEXT, fieldbackground=C_CARD, bordercolor=C_BORDER)
        style.configure("TFrame", background=C_BG)
        style.configure("Card.TFrame", background=C_PANEL)
        style.configure("TLabel", background=C_BG, foreground=C_TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=C_BG, foreground=C_TEXT, font=("Segoe UI Semibold", 18))
        style.configure("Header.TLabel", background=C_PANEL, foreground=C_TEXT, font=("Segoe UI Semibold", 12))

        style.configure(
            "TButton",
            font=("Segoe UI", 10),
            padding=8,
            background=C_CARD,
            foreground=C_TEXT,
            bordercolor=C_BORDER,
            lightcolor=C_CARD,
            darkcolor=C_BORDER,
            focuscolor=C_BORDER,
        )
        style.map(
            "TButton",
            background=[("active", C_BORDER), ("pressed", "#243044"), ("disabled", C_PANEL)],
            foreground=[("disabled", C_MUTED)],
        )
        style.configure(
            "Accent.TButton",
            font=("Segoe UI Semibold", 11),
            padding=10,
            background=C_ACCENT,
            foreground="#061018",
            bordercolor=C_ACCENT,
            lightcolor=C_ACCENT,
            darkcolor=C_ACCENT,
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#6eb0ff"), ("pressed", "#3a7fd6")],
            foreground=[("disabled", C_MUTED)],
        )
        style.configure(
            "Big.TButton",
            font=("Segoe UI Semibold", 14),
            padding=(16, 12),
            background=C_CARD,
            foreground=C_TEXT,
            bordercolor=C_BORDER,
        )
        style.configure(
            "FlyPlay.TButton",
            font=("Segoe UI Semibold", 10),
            padding=(10, 6),
            background=C_ACCENT,
            foreground="#061018",
            bordercolor=C_ACCENT,
            lightcolor=C_ACCENT,
            darkcolor=C_ACCENT,
        )
        style.map(
            "FlyPlay.TButton",
            background=[("active", "#6aafff"), ("pressed", "#3a7fd6")],
            foreground=[("disabled", C_MUTED)],
        )
        style.configure(
            "FlyBack.TButton",
            font=("Segoe UI Semibold", 10),
            padding=(10, 6),
            background=C_CARD,
            foreground=C_TEXT,
            bordercolor=C_BORDER,
        )

        style.configure("TNotebook", background=C_BG, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=C_CARD,
            foreground=C_MUTED,
            padding=(16, 8),
            font=("Segoe UI", 10),
            lightcolor=C_CARD,
            darkcolor=C_CARD,
            bordercolor=C_BORDER,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", C_PANEL), ("active", C_BORDER)],
            foreground=[("selected", C_TEXT), ("active", C_TEXT)],
        )

        style.configure(
            "TEntry",
            fieldbackground=C_CARD,
            foreground=C_TEXT,
            insertcolor=C_TEXT,
            bordercolor=C_BORDER,
            lightcolor=C_BORDER,
            darkcolor=C_BORDER,
            padding=5,
        )
        style.map(
            "TEntry",
            fieldbackground=[("!disabled", C_CARD), ("readonly", C_CARD), ("disabled", C_PANEL)],
            foreground=[("!disabled", C_TEXT), ("readonly", C_TEXT), ("disabled", C_MUTED)],
            bordercolor=[("focus", C_ACCENT)],
        )

        style.configure(
            "TCombobox",
            fieldbackground=C_CARD,
            foreground=C_TEXT,
            background=C_CARD,
            arrowcolor=C_TEXT,
            bordercolor=C_BORDER,
            lightcolor=C_BORDER,
            darkcolor=C_BORDER,
            padding=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", C_CARD), ("!disabled", C_CARD), ("disabled", C_PANEL)],
            foreground=[("readonly", C_TEXT), ("!disabled", C_TEXT), ("disabled", C_MUTED)],
            background=[("readonly", C_CARD), ("active", C_BORDER)],
            arrowcolor=[("disabled", C_MUTED), ("!disabled", C_TEXT)],
            selectbackground=[("readonly", C_CARD), ("!disabled", C_CARD)],
            selectforeground=[("readonly", C_TEXT), ("!disabled", C_TEXT)],
        )

        style.configure(
            "TCheckbutton",
            background=C_BG,
            foreground=C_TEXT,
            focuscolor=C_BG,
            indicatorbackground=C_CARD,
            indicatorforeground=C_TEXT,
        )
        style.map(
            "TCheckbutton",
            background=[("active", C_BG)],
            foreground=[("disabled", C_MUTED)],
            indicatorbackground=[("selected", C_ACCENT), ("!selected", C_CARD)],
        )
        style.configure(
            "TRadiobutton",
            background=C_BG,
            foreground=C_TEXT,
            focuscolor=C_BG,
            indicatorbackground=C_CARD,
            indicatorforeground=C_TEXT,
        )
        style.map(
            "TRadiobutton",
            background=[("active", C_BG), ("!disabled", C_BG)],
            foreground=[("disabled", C_MUTED)],
            indicatorbackground=[("selected", C_ACCENT), ("!selected", C_CARD)],
        )
        # Radios/checks sitting on panel cards
        style.configure("Panel.TRadiobutton", background=C_PANEL, foreground=C_TEXT, focuscolor=C_PANEL)
        style.map("Panel.TRadiobutton", background=[("active", C_PANEL), ("!disabled", C_PANEL)])
        style.configure("Panel.TCheckbutton", background=C_PANEL, foreground=C_TEXT, focuscolor=C_PANEL)
        style.map("Panel.TCheckbutton", background=[("active", C_PANEL), ("!disabled", C_PANEL)])

        style.configure(
            "Horizontal.TScale",
            background=C_PANEL,
            troughcolor=C_CARD,
            bordercolor=C_BORDER,
            lightcolor=C_ACCENT,
            darkcolor=C_ACCENT,
        )
        style.configure(
            "Vertical.TScrollbar",
            background=C_CARD,
            troughcolor=C_PANEL,
            bordercolor=C_BORDER,
            arrowcolor=C_TEXT,
        )
        style.map("Vertical.TScrollbar", background=[("active", C_BORDER)])

        # Treeview (Opus flight picker, etc.) — clam defaults are nearly unreadable on dark
        style.configure(
            "Treeview",
            background=C_CARD,
            foreground=C_TEXT,
            fieldbackground=C_CARD,
            bordercolor=C_BORDER,
            lightcolor=C_BORDER,
            darkcolor=C_BORDER,
            rowheight=26,
            font=("Segoe UI", 10),
        )
        style.map(
            "Treeview",
            background=[("selected", C_ACCENT), ("!selected", C_CARD)],
            foreground=[("selected", "#061018"), ("!selected", C_TEXT)],
        )
        style.configure(
            "Treeview.Heading",
            background=C_PANEL,
            foreground=C_LABEL,
            bordercolor=C_BORDER,
            lightcolor=C_PANEL,
            darkcolor=C_BORDER,
            relief="flat",
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "Treeview.Heading",
            background=[("active", C_BORDER)],
            foreground=[("active", C_TEXT)],
        )

    def _on_close(self) -> None:
        try:
            self._clear_tk_hotkeys()
            self._hotkey_listener.stop()
        except Exception:
            pass
        try:
            self._keys.stop()
        except Exception:
            pass
        try:
            self._joystick.stop()
        except Exception:
            pass
        try:
            if self._voice:
                self._voice.stop()
        except Exception:
            pass
        try:
            srs_radio.stop_srs_udp_listener()
        except Exception:
            pass
        # The zone editor has no window of its own, so closing the app has to be
        # what stops it — otherwise it holds the port and the next launch reuses
        # a server that outlived the session.
        if self._zone_proc and self._zone_proc.poll() is None:
            try:
                self._zone_proc.terminate()
            except Exception:
                pass
        try:
            if self._atc_client:
                self._atc_client.stop()
        except Exception:
            pass
        try:
            if self._atc_server:
                self._atc_server.stop()
        except Exception:
            pass
        if self.http:
            try:
                self.http.shutdown()
            except Exception:
                pass
        self.destroy()

    def _update_fly_hotkey_hint(self) -> None:
        if not hasattr(self, "fly_hotkey_hint"):
            return
        nxt = hotkeys.hotkey_from_config(self.config_data, "next")
        bak = hotkeys.hotkey_from_config(self.config_data, "back")
        seek_n = hotkeys.hotkey_from_config(self.config_data, "seek_next")
        seek_p = hotkeys.hotkey_from_config(self.config_data, "seek_prev")
        parts = [f"Hotkeys  Next {nxt}  ·  Back {bak}"]
        if seek_n or seek_p:
            parts[0] += f"  ·  Step {seek_p or '—'} / {seek_n or '—'}"
        joy_bits = []
        for key, label in (
            ("next", "Next"),
            ("back", "Back"),
            ("seek_next", "Step ▶"),
            ("seek_prev", "Step ◀"),
        ):
            binding = self._joy_bindings.get(key)
            if binding:
                joy_bits.append(f"{label} {joystick.describe_binding(binding)}")
        if joy_bits:
            parts.append("Controller  " + "  ·  ".join(joy_bits))
        self.fly_hotkey_hint.set("      ".join(parts))

    def _on_trigger_received(self, label: str) -> None:
        """Live proof that an external trigger reached the app (any input source)."""
        if not hasattr(self, "var_trigger_seen"):
            return
        self._ui_call(lambda: self.var_trigger_seen.set(f"Last trigger: {label}"))

    def _trigger(self, action: str) -> None:
        """
        Run Next / Back once, whichever input source got there first.

        A single press can arrive several times over: RegisterHotKey and the key
        poller both see it, and the Tk binding fires too when this window has
        focus. Collapsing them here means every source can stay armed.
        """
        now = time.monotonic()
        with self._trigger_lock:
            if now - self._trigger_last.get(action, 0.0) < TRIGGER_DEBOUNCE_S:
                return
            self._trigger_last[action] = now

        def run() -> None:
            allowed, msg = self._external_freq_gate_allows(action)
            if not allowed:
                step = None
                try:
                    eng = getattr(self, "engine", None)
                    step = eng.current_step() if eng is not None else None
                except Exception:  # noqa: BLE001
                    step = None
                self._note_no_tx(msg, action=action, step=step)
                return
            self._fly(action, bypass_freq_gate=False)

        self._ui_call(run)

    def _external_freq_gate_allows(self, action: str) -> tuple[bool, str]:
        """Freq gate for hotkey / HOTAS / mouse. On-screen Play uses the same check."""
        if action not in ("next", "back"):
            return True, ""
        self._sync_eam_freqs_to_config()
        srs_radio.apply_config(self.config_data)
        step = None
        eng = getattr(self, "engine", None)
        flow_state = getattr(eng, "state", None) if eng is not None else None
        try:
            if eng is not None:
                # Answering a live rolling offer TXes on Tower — not the
                # previous step Back would otherwise replay.
                if atc_phrase.rolling_offer_awaiting_reply(eng.state):
                    step = eng.current_step()
                elif action == "back":
                    steps = eng.steps
                    idx = max(0, int(eng.state.get("index") or 0) - 1)
                    step = steps[idx] if steps and idx < len(steps) else eng.current_step()
                else:
                    step = eng.current_step()
                airport = eng.airport()
            else:
                airport = self._airport()
        except Exception:  # noqa: BLE001
            airport = self._airport()
        allowed, msg, _result = srs_radio.check_freq_gate(
            self.config_data,
            airport,
            step,
            state=flow_state,
        )
        return allowed, msg

    def _sync_eam_freqs_to_config(self) -> None:
        """Push Fly EAM strip + Setup checkboxes into config / srs_radio."""
        if hasattr(self, "var_freq_gate_enabled"):
            self.config_data["freq_gate_enabled"] = bool(self.var_freq_gate_enabled.get())
        if hasattr(self, "var_freq_gate_eam"):
            self.config_data["freq_gate_eam_enabled"] = bool(self.var_freq_gate_eam.get())
        freqs: list[float] = []
        for var in getattr(self, "_eam_freq_vars", []) or []:
            try:
                mhz = float(str(var.get()).strip())
            except (TypeError, ValueError):
                continue
            if mhz >= 1.0:
                freqs.append(mhz)
        if freqs or getattr(self, "_eam_freq_vars", None) is not None:
            self.config_data["freq_gate_eam_freqs"] = freqs
        if hasattr(self, "var_eam_active"):
            try:
                active = int(self.var_eam_active.get())
            except (TypeError, ValueError, tk.TclError):
                active = 0
            if freqs:
                active = max(0, min(active, len(freqs) - 1))
            self.config_data["freq_gate_eam_active"] = active
        srs_radio.apply_config(self.config_data)

    def _on_freq_gate_options_changed(self) -> None:
        self._sync_eam_freqs_to_config()
        self._sync_fly_eam_ui()
        if hasattr(self, "fly_freq_gate"):
            self._update_fly_freq_gate_status()

    def _on_auto_clearance_changed(self) -> None:
        self.config_data["auto_clearance_enabled"] = bool(self.var_auto_clearance.get())
        self.config_data["auto_takeoff_clearance"] = bool(self.var_auto_takeoff.get())
        self.config_data["auto_monitor_tower"] = bool(self.var_auto_monitor.get())
        self.config_data["auto_clearance_require_full_flight"] = bool(
            self.var_auto_full_flight.get()
        )
        save_json(CONFIG_PATH, self.config_data)
        self._position_tracker().reset()
        if hasattr(self, "fly_position") and not self.config_data["auto_clearance_enabled"]:
            self.fly_position.set("Automatic clearances off — turn on in Setup")
            if hasattr(self, "_fly_position_lbl"):
                self._fly_position_lbl.configure(fg=C_MUTED)

    def _install_dcs_radio_export(self) -> None:
        """Copy ATC-RadioExport.lua and patch Export.lua in each DCS profile."""
        results = install_dcs_radio_export.install_all()
        report = install_dcs_radio_export.format_install_report(results)
        ok = all(r.get("ok") for r in results) if results else False
        if ok:
            messagebox.showinfo(
                "DCS radio export",
                "Installed for in-jet frequency gate.\n\n"
                f"{report}\n\n"
                "Restart DCS if it is already running. The SRS Export.lua line is left alone.",
            )
        else:
            messagebox.showerror("DCS radio export", report or "Install failed.")

    def _status_dcs_radio_export(self) -> None:
        rows = install_dcs_radio_export.status_all()
        if not rows:
            messagebox.showinfo(
                "DCS radio export",
                "No DCS Saved Games profiles found yet.\n"
                "Run Install DCS radio export… to create the default profile hooks.",
            )
            return
        lines = []
        for row in rows:
            name = Path(row["profile"]).name
            state = "ready" if row["ready"] else "not installed"
            lines.append(
                f"{name}: {state}  (script={row['lua_present']}, Export.lua hook={row['export_hooked']})"
            )
        messagebox.showinfo("DCS radio export", "\n".join(lines))

    def _eam_seed_freqs(self) -> list[float]:
        saved = self.config_data.get("freq_gate_eam_freqs") or []
        freqs: list[float] = []
        if isinstance(saved, list):
            for raw in saved:
                try:
                    mhz = float(raw)
                except (TypeError, ValueError):
                    continue
                if mhz >= 1.0:
                    freqs.append(mhz)
        if freqs:
            return freqs
        awacs = srs_radio.try_seed_from_srs_awacs()
        if awacs:
            return awacs
        return srs_radio.seed_eam_from_airport(self._airport())

    def _select_eam_active(self, index: int) -> None:
        """Mark one EAM radio as the tuned TX freq for the gate / voice replies."""
        if not hasattr(self, "var_eam_active"):
            self.var_eam_active = tk.IntVar(value=0)
        freqs: list[float] = []
        for var in getattr(self, "_eam_freq_vars", []) or []:
            try:
                freqs.append(float(str(var.get()).strip()))
            except (TypeError, ValueError):
                pass
        if not freqs:
            self.var_eam_active.set(0)
            self._sync_eam_freqs_to_config()
            self._update_fly_freq_gate_status()
            return
        active = max(0, min(int(index), len(freqs) - 1))
        self.var_eam_active.set(active)
        self._rebuild_eam_freq_rows(freqs)
        self._sync_eam_freqs_to_config()
        self._update_fly_freq_gate_status()

    def _rebuild_eam_freq_rows(self, freqs: list[float]) -> None:
        if not hasattr(self, "_fly_eam_rows"):
            return
        for child in self._fly_eam_rows.winfo_children():
            child.destroy()
        self._eam_freq_vars = []
        if not freqs:
            freqs = [251.0]
        if not hasattr(self, "var_eam_active"):
            try:
                seed = int(self.config_data.get("freq_gate_eam_active", 0) or 0)
            except (TypeError, ValueError):
                seed = 0
            self.var_eam_active = tk.IntVar(value=seed)
        active = int(self.var_eam_active.get())
        if active >= len(freqs[:10]):
            active = 0
            self.var_eam_active.set(0)
        for i, mhz in enumerate(freqs[:10]):
            row = tk.Frame(self._fly_eam_rows, bg=C_PANEL)
            row.pack(fill=tk.X, pady=2)
            rb = tk.Radiobutton(
                row,
                text=f"R{i + 1}",
                variable=self.var_eam_active,
                value=i,
                command=lambda idx=i: self._select_eam_active(idx),
                bg=C_PANEL,
                fg=C_TEXT,
                selectcolor=C_CARD,
                activebackground=C_PANEL,
                activeforeground=C_ACCENT,
                font=("Segoe UI Semibold", 9),
                width=4,
                anchor="w",
                indicatoron=True,
            )
            rb.pack(side=tk.LEFT)
            var = tk.StringVar(value=f"{float(mhz):.3f}".rstrip("0").rstrip("."))
            self._eam_freq_vars.append(var)
            ent = ttk.Entry(row, textvariable=var, width=10)
            ent.pack(side=tk.LEFT, padx=(4, 0))
            ent.bind("<FocusOut>", lambda _e: self._sync_eam_freqs_to_config())
            ent.bind("<Return>", lambda _e: self._sync_eam_freqs_to_config())
            tk.Label(row, text="MHz", bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 9)).pack(
                side=tk.LEFT, padx=(4, 0)
            )
            badge = tk.Label(
                row,
                text="TX" if i == active else "",
                bg=C_PANEL,
                fg=C_GREEN,
                font=("Segoe UI Semibold", 9),
                width=3,
                anchor="w",
            )
            badge.pack(side=tk.LEFT, padx=(8, 0))

    def _sync_fly_eam_ui(self) -> None:
        if not hasattr(self, "_fly_eam_box"):
            return
        enabled = bool(self.config_data.get("freq_gate_eam_enabled")) or (
            hasattr(self, "var_freq_gate_eam") and bool(self.var_freq_gate_eam.get())
        )
        try:
            self._fly_eam_box.pack_forget()
        except tk.TclError:
            pass
        if enabled:
            if not self._eam_freq_vars:
                self._rebuild_eam_freq_rows(self._eam_seed_freqs())
            # Keep EAM below the kneeboard prompts (voice cues → expected response
            # → readback) so the radio strip does not bury them.
            anchor = None
            for name in (
                "_fly_freq_box",
                "fly_readback_frame",
                "fly_say_frame",
                "_fly_will_say_box",
            ):
                widget = getattr(self, name, None)
                if widget is not None and widget.winfo_manager():
                    anchor = widget
            if anchor is not None:
                self._fly_eam_box.pack(
                    fill=tk.X, padx=20, pady=(0, 8), after=anchor
                )
            else:
                self._fly_eam_box.pack(fill=tk.X, padx=20, pady=(0, 8))
        self.after_idle(self._fly_update_scrollregion)

    def _reseed_eam_freqs(self) -> None:
        freqs = srs_radio.try_seed_from_srs_awacs() or srs_radio.seed_eam_from_airport(
            self._airport()
        )
        self._rebuild_eam_freq_rows(freqs)
        self._sync_eam_freqs_to_config()
        self._update_fly_freq_gate_status()

    def _tune_eam_to_step(self) -> None:
        """Set the selected EAM radio to the current step frequency and keep it active."""
        step = None
        try:
            step = self.engine.current_step()
            airport = self.engine.airport()
        except Exception:  # noqa: BLE001
            airport = self._airport()
        if not step:
            messagebox.showinfo("EAM", "No current step to tune.")
            return
        ch = str(step.get("channel") or step.get("phase") or "other")
        freq, _mod, _name = atc_phrase.step_radio(airport, ch, step)
        freqs = []
        for var in self._eam_freq_vars:
            try:
                freqs.append(float(str(var.get()).strip()))
            except (TypeError, ValueError):
                pass
        if not hasattr(self, "var_eam_active"):
            self.var_eam_active = tk.IntVar(value=0)
        try:
            active = int(self.var_eam_active.get())
        except (TypeError, ValueError, tk.TclError):
            active = 0
        if freqs:
            active = max(0, min(active, len(freqs) - 1))
            freqs[active] = float(freq)
        else:
            freqs = [float(freq)]
            active = 0
        self.var_eam_active.set(active)
        self._rebuild_eam_freq_rows(freqs)
        self._sync_eam_freqs_to_config()
        self._update_fly_freq_gate_status()
        self._on_trigger_received(
            f"EAM R{active + 1} tuned {srs_radio.format_mhz(freq)} ({ch.upper()}) — selected for TX"
        )

    def _update_fly_freq_gate_status(self) -> None:
        if not hasattr(self, "fly_freq_gate"):
            return
        self._sync_eam_freqs_to_config()
        self._sync_eam_strip_from_srs()
        airport = self._airport()
        try:
            airport = self.engine.airport()
        except Exception:  # noqa: BLE001
            pass
        try:
            tuned = srs_radio.channel_for_tuned_freq(airport, self.config_data) or ""
        except Exception:  # noqa: BLE001
            tuned = ""
        prev = getattr(self, "_last_tip_tuned_channel", None)
        if tuned != prev:
            self._last_tip_tuned_channel = tuned
            self._maybe_follow_tanker_tune(tuned)
            if hasattr(self, "fly_say_frame"):
                self._refresh_voice_prompts()
            if hasattr(self, "_sync_fly_pilot_request_ui"):
                self._sync_fly_pilot_request_ui(tuned or None)
        step = None
        try:
            step = self.engine.current_step()
        except Exception:  # noqa: BLE001
            step = None
        _ok, msg, result = srs_radio.check_freq_gate(
            self.config_data,
            airport,
            step,
            state=getattr(self.engine, "state", None),
        )
        # Richer Fly lines: live tune vs next-step agency / mission phase.
        tuned_line, next_line, gate_line, gate_color = self._fly_radio_status_lines(
            airport, step, gate_msg=msg, gate_result=result
        )
        if hasattr(self, "fly_tuned_now"):
            self.fly_tuned_now.set(tuned_line)
        if hasattr(self, "fly_next_radio"):
            self.fly_next_radio.set(next_line)
        self.fly_freq_gate.set(gate_line)
        if hasattr(self, "_fly_freq_gate_lbl"):
            self._fly_freq_gate_lbl.configure(fg=gate_color)
        if hasattr(self, "_fly_tuned_now_lbl"):
            self._fly_tuned_now_lbl.configure(
                fg=C_GREEN if "YOU ARE ON" in tuned_line else C_MUTED
            )

    def _maybe_follow_tanker_tune(self, tuned: str) -> None:
        """AAR is a side trip — after tanker UHF, retune Blackjack or Bandsaw to resume C2."""
        if self._atc_role() == "client":
            # Host heartbeat owns per-element overlay so dash-3 leaving
            # tanker does not yank dash-1 off Bandsaw.
            return
        try:
            import tanker as tanker_mod
        except Exception:
            return
        st = getattr(self.engine, "state", None)
        if not tanker_mod.note_tanker_tune(st, tuned):
            if tanker_mod.tanker_overlay_active(st) and str(tuned or "") == "tanker":
                try:
                    self.engine.save_state()
                except Exception:
                    pass
            return
        tanker_mod.leave_tanker_overlay(self.engine, tuned, checkin=False)
        try:
            self._refresh_fly_status()
        except Exception:
            pass

    def _fly_radio_status_lines(
        self,
        airport: dict[str, Any],
        step: dict[str, Any] | None,
        *,
        gate_msg: str,
        gate_result: str,
    ) -> tuple[str, str, str, str]:
        """
        (you_are_on, next_step_line, gate_line, gate_color) for the Fly freq box.
        """
        state = srs_radio.current_radio_state(self.config_data)
        if state.freqs_mhz:
            try:
                tol = float(self.config_data.get("freq_gate_tolerance_mhz") or 0.05)
            except (TypeError, ValueError):
                tol = 0.05
            tuned_line = srs_radio.format_you_are_on(
                state,
                airport,
                tol_mhz=tol,
                config=self.config_data,
                flow_state=getattr(self.engine, "state", None),
            )
        else:
            udp = srs_radio.srs_udp_status()
            hint = ""
            if udp.get("error"):
                hint = f"  ({udp['error']})"
            elif udp.get("ports_bound") and not udp.get("fresh"):
                hint = f"  (SRS UDP quiet on {', '.join(str(p) for p in udp['ports_bound'])})"
            tuned_line = f"YOU ARE ON  ·  radio tune unknown{hint}"

        if step:
            ch = str(step.get("channel") or "other").strip().lower()
            phase = voice_intent.normalize_mission_phase(
                str(step.get("phase") or ""), channel=ch
            )
            phase_lbl = voice_intent.MISSION_PHASE_LABELS.get(phase, phase or "?")
            label = str(step.get("label") or step.get("id") or ch).strip()
            try:
                need_mhz, _mod, _ = atc_phrase.step_radio(
                    airport,
                    ch,
                    step,
                    state=getattr(self.engine, "state", None),
                    config=self.config_data,
                )
                need = srs_radio.format_mhz(need_mhz)
            except Exception:  # noqa: BLE001
                need = "—"
            tmpl = str(step.get("template") or "")
            handoff = ""
            if tmpl == "bj_range_exit":
                handoff = "  →  Approach (≤40 NM)"
            elif tmpl == "bandsaw_check_out":
                handoff = "  →  Blackjack"
            elif tmpl == "climb_cruise":
                handoff = "  →  cruise (≥10 NM)"
            elif tmpl == "departure_handoff":
                handoff = "  →  Blackjack (≥18 NM)"
            next_line = (
                f"NEXT STEP  ·  {phase_lbl}  ·  {label}  ({ch.upper()} {need}){handoff}"
            )
            try:
                import tanker as tanker_mod

                flow_st = getattr(self.engine, "state", None)
                if tanker_mod.tanker_overlay_active(flow_st):
                    resume = str(
                        (flow_st or {}).get("tanker_resume_channel") or "C2"
                    ).strip().upper() or "C2"
                    next_line = (
                        f"TANKER  ·  AAR side trip  ·  resume {resume} "
                        f"when you tune Blackjack or Bandsaw"
                    )
            except Exception:
                pass
            try:
                st = getattr(self.engine, "state", None)
                if atc_phrase.rolling_offer_awaiting_reply(st):
                    next_line = (
                        "NEXT  ·  accept rolling    PREV  ·  decline"
                    )
            except Exception:  # noqa: BLE001
                pass
        else:
            next_line = "NEXT STEP  ·  —"

        color = (
            C_GREEN if gate_result == "match"
            else (C_AMBER if gate_result == "mismatch" else C_MUTED)
        )
        return tuned_line, next_line, gate_msg, color

    def _schedule_freq_gate_poll(self) -> None:
        """Keep Fly gate / tuned-radio line in sync with the live SRS radio bank."""
        try:
            if hasattr(self, "fly_freq_gate"):
                self._update_fly_freq_gate_status()
        except Exception:  # noqa: BLE001
            pass
        self.after(500, self._schedule_freq_gate_poll)

    # --- live aircraft position (CAOC) ----------------------------------

    def _schedule_position_poll(self) -> None:
        """
        Watch where the flight actually is and fire the clearance it has earned.

        Runs off the UI thread because it hits the Opus backend. The feed itself
        is cached, so polling every 2 s costs one request every few seconds.
        """
        try:
            self._position_tick()
        except Exception:  # noqa: BLE001
            pass
        self.after(2000, self._schedule_position_poll)

    def _position_tracker(self) -> runway_position.PositionTracker:
        tracker = getattr(self, "_pos_tracker", None)
        if tracker is None:
            tracker = runway_position.PositionTracker()
            self._pos_tracker = tracker
        return tracker

    def _position_tracker_for(self, session_id: str) -> runway_position.PositionTracker:
        bag = getattr(self, "_pos_trackers", None)
        if bag is None:
            bag = {}
            self._pos_trackers = bag
        tracker = bag.get(session_id)
        if tracker is None:
            tracker = runway_position.PositionTracker()
            bag[session_id] = tracker
        return tracker

    def _watch_off_position_line(self) -> str:
        """When Watch is off, still say why the current distance gate would fire."""
        base = "Automatic clearances off — turn on in Setup"
        try:
            engine = getattr(self, "engine", None)
            step = engine.current_step() if engine is not None else None
            if not step:
                return base
            trigger = runway_position.resolve_step_trigger(
                step, mission=engine.mission, state=engine.state
            )
            if trigger is None or trigger.within_nm is None:
                return base
            dist = runway_position.ownship_distance_nm(
                engine.airport(),
                config=self.config_data,
                state=engine.state,
            )
            _held, waiting = runway_position.within_nm_held(trigger, dist)
            label = str(step.get("label") or step.get("template") or "step")
            extra = waiting or trigger.describe()
            return f"{base}  ·  {label}: {extra}"
        except Exception:
            return base

    def _position_tick(self) -> None:
        if not hasattr(self, "fly_position"):
            return
        if self._atc_role() == "client":
            self._kick_tanker_boom_watch()
            return
        if not bool(self.config_data.get("auto_clearance_enabled")):
            self.fly_position.set(self._watch_off_position_line())
            if hasattr(self, "_fly_position_lbl"):
                self._fly_position_lbl.configure(fg=C_MUTED)
            self._kick_tanker_boom_watch()
            return
        if not getattr(self, "_pos_busy", False):
            self._pos_busy = True
            threading.Thread(target=self._position_work, daemon=True).start()
        if (
            self._atc_role() == "host"
            and self._atc_server is not None
            and not getattr(self, "_host_pos_busy", False)
        ):
            self._host_pos_busy = True
            threading.Thread(target=self._host_position_work, daemon=True).start()

    def _atc_role(self) -> str:
        return atc_net.role_of(self.config_data)

    def _sync_atc_runtime(self) -> None:
        """Start or stop host/client services to match Setup role. Solo is a no-op."""
        role = self._atc_role()
        if getattr(self, "_atc_client", None) is not None:
            try:
                self._atc_client.stop()
            except Exception:
                pass
            self._atc_client = None
        if getattr(self, "_atc_server", None) is not None:
            try:
                self._atc_server.stop()
            except Exception:
                pass
            self._atc_server = None
        # Restore loopback flow HTTP onto the local engine unless client mode wraps it.
        self._ensure_local_http(self.engine)
        if role == "host":
            token = atc_net.token_of(self.config_data)
            if not token:
                import secrets

                token = secrets.token_urlsafe(12)
                self.config_data["atc_token"] = token
                if hasattr(self, "var_atc_token"):
                    self.var_atc_token.set(token)
                try:
                    save_json(CONFIG_PATH, self.config_data)
                except Exception:
                    pass
            self._atc_server = atc_server.AtcServer(
                self.config_data,
                self.airports,
                lambda: self.mission,
            )
            try:
                self._atc_server.start(
                    port=int(self.config_data.get("atc_port") or atc_net.DEFAULT_ATC_PORT)
                )
                lan_txt = atc_net.format_listen_summary(self._atc_server.port)
                fw = getattr(self._atc_server, "firewall_status", "") or ""
                bits = [
                    lan_txt,
                    "off-LAN pilots use their DCS/SRS address + TCP 8766 forwarded here",
                ]
                if fw:
                    bits.append(fw)
                self._set_net_status("  ·  ".join(bits))
            except OSError as exc:
                self._atc_server = None
                self._set_net_status(f"HOST failed: {exc}")
        elif role == "client":
            self._atc_client = atc_client.AtcClient(self.config_data)
            warn = self._atc_client.start()
            if warn:
                self._set_net_status(warn)
            else:
                self._set_net_status(
                    f"CLIENT  {self._atc_client.base_url}  ·  {self._atc_client.callsign or 'connected'}"
                )
                self._ensure_local_http(atc_client.ClientEngineProxy(self._atc_client))
        else:
            self._set_net_status("")
        if hasattr(self, "_refresh_traffic"):
            self._refresh_traffic()

    def _ensure_local_http(self, engine: Any) -> None:
        port = int(self.config_data.get("flow_http_port") or 8765)
        if self.http is not None:
            try:
                self.http.shutdown()
            except Exception:
                pass
            self.http = None
        try:
            self.http = flow_engine.start_http_server(engine, port)
        except OSError:
            pass

    def _set_net_status(self, text: str) -> None:
        if hasattr(self, "fly_net"):
            self.fly_net.set(text)
        if hasattr(self, "var_atc_net_status"):
            self.var_atc_net_status.set(text or "Solo — this PC talks to ATC by itself")

    def _live_engine(self) -> Any:
        """
        The in-memory flow engine — boom chat and voice must share this object.

        Constructing a fresh FlowEngine() from disk left tanker_chat on a fork
        until the UI thread swapped it in, so PTT during Texaco's TX was scored
        as ATC and ignored.
        """
        engine = self.engine
        if isinstance(getattr(self, "config_data", None), dict):
            engine.config = self.config_data
        if getattr(self, "airports", None) is not None:
            engine.airports = self.airports
        if getattr(self, "mission", None) is not None:
            engine.mission = self.mission
        return engine

    def _kick_tanker_boom_watch(self) -> None:
        if getattr(self, "_boom_busy", False):
            return
        self._boom_busy = True
        threading.Thread(target=self._tanker_boom_work, daemon=True).start()

    def _set_boom_status(self, text: str, *, fire: bool = False) -> None:
        if hasattr(self, "fly_boom"):
            self.fly_boom.set(text)
        if hasattr(self, "_fly_boom_lbl"):
            self._fly_boom_lbl.configure(fg=C_GREEN if fire else (C_AMBER if text else C_MUTED))

    def _boom_caption(self, engine: Any, extra: str = "") -> str:
        try:
            import tanker_chat as tanker_chat_mod

            return tanker_chat_mod.fly_boom_caption(
                getattr(engine, "state", None), extra
            )
        except Exception:
            return extra

    def _log_texaco(self, text: str, note: str = "") -> None:
        line = " ".join(str(text or "").split()).strip()
        if line:
            self._voice_log(f"TEXACO  {line}")
        if note:
            self._voice_log(f"BOOM  {note}")

    def _tanker_boom_work(self) -> None:
        """Texaco starts / continues boom chat once joined and in range."""
        try:
            import tanker as tanker_mod
            import tanker_chat as tanker_chat_mod

            engine = self._live_engine()
            if tanker_chat_mod.current_choices(engine.state):
                msg = self._boom_caption(engine, "waiting for your answer")
                self._ui_call(lambda m=msg: self._set_boom_status(m))
                return
            if tanker_chat_mod.is_awaiting_react(engine.state):
                if tanker_chat_mod.continuation_due(engine.state):
                    result = voice_engine.resolve_tanker_chat(
                        engine, continue_session=True
                    )
                    self._ui_call(lambda: self._finish_boom_tx(engine, result))
                    return
                delay = tanker_chat_mod.continuation_delay_s(engine.state)
                extra = (
                    f"say anything, or next bit in {delay:.0f}s"
                    if delay is not None
                    else "say anything"
                )
                msg = self._boom_caption(engine, extra)
                self._ui_call(lambda m=msg: self._set_boom_status(m))
                return
            if tanker_chat_mod.is_session_active(engine.state):
                if tanker_chat_mod.continuation_due(engine.state):
                    result = voice_engine.resolve_tanker_chat(
                        engine, continue_session=True
                    )
                    self._ui_call(lambda: self._finish_boom_tx(engine, result))
                    return
                delay = tanker_chat_mod.continuation_delay_s(engine.state)
                extra = (
                    f"next bit in {delay:.0f}s" if delay is not None else "chatting"
                )
                msg = self._boom_caption(engine, extra)
                self._ui_call(lambda m=msg: self._set_boom_status(m))
                return
            if not tanker_mod.has_rejoined(engine.state):
                self._ui_call(lambda: self._set_boom_status(""))
                return
            voice = getattr(self, "_voice", None)
            if voice is not None and getattr(voice, "ptt_held", False):
                self._ui_call(
                    lambda: self._set_boom_status(
                        "BOOM: waiting — you are transmitting"
                    )
                )
                return
            result = voice_engine.resolve_tanker_chat(engine)
            self._ui_call(lambda: self._finish_boom_tx(engine, result, opening=True))
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            self._ui_call(lambda: self._set_boom_status(f"BOOM: {err}"))
        finally:
            self._boom_busy = False

    def _finish_boom_tx(
        self,
        engine: Any,
        result: dict[str, Any] | None,
        *,
        opening: bool = False,
    ) -> None:
        import tanker_chat as tanker_chat_mod

        boom = result.get("boom") if isinstance(result, dict) else None
        reason = ""
        if isinstance(boom, dict):
            reason = str(boom.get("reason") or "")
        if not reason:
            reason = str((result or {}).get("detail") or "")
        note = tanker_chat_mod.llm_note(engine.state)
        fired = bool(result and result.get("action") == "transmit" and result.get("text"))
        line = self._boom_caption(engine, reason if not fired else "say anything")
        self._set_boom_status(line, fire=fired)
        if fired:
            self._log_texaco(str(result.get("text") or ""), note)
            self._refresh_fly_status()
        elif opening and result and result.get("action") == "blocked":
            self._note_no_tx(
                str(result.get("detail") or "Blocked off frequency"),
                action="auto",
            )
        again = result.get("deferred") if isinstance(result, dict) else None
        if isinstance(again, dict) and again.get("kind") == "tanker_chat_continue":
            self._schedule_tanker_chat(again)

    def _host_position_work(self) -> None:
        try:
            server = self._atc_server
            if server is None:
                return
            for sess in server.unique_flow_sessions():
                try:
                    self._position_work(
                        engine=sess.engine,
                        tracker=self._position_tracker_for(sess.session_id),
                        bind_live=False,
                        session=sess,
                        busy_attr="",
                    )
                except Exception:
                    continue
        finally:
            self._host_pos_busy = False

    def _position_work(
        self,
        engine: Any | None = None,
        tracker: runway_position.PositionTracker | None = None,
        *,
        bind_live: bool = True,
        session: Any = None,
        busy_attr: str = "_pos_busy",
    ) -> None:
        if tracker is None:
            tracker = self._position_tracker()
        try:
            if engine is None:
                engine = flow_engine.FlowEngine()
            step = engine.current_step() or {}
            template = str(step.get("template") or "")
            airport = engine.airport()
            trigger = runway_position.resolve_step_trigger(
                step, mission=engine.mission, state=engine.state
            )
            opus, weather = atc_phrase.resolve_opus_and_metar(
                engine.config, airport["icao"]
            )
            if not opus:
                opus = atc_phrase.synthetic_flight_context(
                    atc_phrase.callsign_override(engine.config) or "CALLSIGN"
                )
            callsign = (
                atc_phrase.cached_radio_callsign(engine.config)
                or (opus.radio_callsign if opus else "")
                or ""
            )
            runway = atc_phrase.pick_departure_runway(
                airport,
                weather,
                opus,
                engine.config,
                step=step,
                mission=engine.mission,
                state=engine.state,
                template=template or None,
            )
            watch_zones: list[dict[str, Any]] = []
            if trigger is not None and trigger.zone:
                assigned_eor = ""
                if str(trigger.zone).strip().lower() == "eor" and runway:
                    try:
                        assigned_eor = str(
                            (
                                atc_phrase.resolve_taxi_route(
                                    airport, runway, opus=opus
                                )
                                or {}
                            ).get("eor")
                            or ""
                        ).strip()
                    except Exception:
                        assigned_eor = ""
                watch_zones = runway_position.zones_by_ref(
                    airport, trigger.zone, runway, place=assigned_eor or None
                )
            status = tracker.evaluate(
                engine.config,
                airport,
                runway,
                callsign=callsign,
                opus=opus,
                watch=watch_zones or None,
            )
            dist_nm = None
            need_dist = (
                (trigger is not None and trigger.within_nm is not None)
                or engine.state.get("rearm_tower_outside_nm") is not None
            )
            if need_dist:
                dist_nm = runway_position.ownship_distance_nm(
                    airport,
                    config=engine.config,
                    callsign=callsign,
                    opus=opus,
                    state=engine.state,
                )
            # Side-effect: clear missed-approach rearm once outside the bubble.
            had_rearm = engine.state.get("rearm_tower_outside_nm") is not None
            atc_phrase.tower_land_gates_allowed(engine.state, dist_nm)
            if had_rearm and engine.state.get("rearm_tower_outside_nm") is None:
                try:
                    engine.save_state()
                except Exception:
                    pass
            fire, waiting = self._position_decide(
                step,
                trigger,
                watch_zones,
                status,
                engine.state,
                distance_nm=dist_nm,
                opus=opus,
                callsign=callsign,
                mission=engine.mission,
                airport=airport,
                config=engine.config,
                tracker=tracker,
            )
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            if bind_live:
                self._ui_call(lambda: self.fly_position.set(f"Position: {err}"))
            if busy_attr:
                setattr(self, busy_attr, False)
            return

        self._ui_call(
            lambda s=status, f=fire, w=waiting, st=step, e=engine, bl=bind_live, se=session, tr=tracker: self._position_apply(
                s,
                f,
                w,
                st,
                e,
                bind_live=bl,
                session=se,
                tracker=tr,
            )
        )
        if busy_attr:
            setattr(self, busy_attr, False)
        if bind_live:
            self._kick_tanker_boom_watch()

    def _position_decide(
        self,
        step: dict[str, Any],
        trigger: runway_position.StepTrigger | None,
        zones: list[dict[str, Any]] | None,
        status: runway_position.FlightStatus,
        state: dict[str, Any] | None = None,
        *,
        distance_nm: float | None = None,
        opus: Any = None,
        callsign: str = "",
        mission: dict[str, Any] | None = None,
        airport: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        tracker: runway_position.PositionTracker | None = None,
    ) -> tuple[str, str]:
        """
        (step id to fire, what it is waiting on). Either may be ''.

        Only the step at the cursor can fire, so a flow never jumps around, and a
        step that has fired stays fired until Reset.
        """
        cfg = config if isinstance(config, dict) else self.config_data
        hold = atc_phrase.auto_tx_hold_reason(state)
        if hold:
            return "", hold
        voice = getattr(self, "_voice", None)
        if voice is not None and getattr(voice, "ptt_held", False):
            return "", "waiting — you are transmitting"
        if tracker is None:
            tracker = self._position_tracker()
        step_id = str(step.get("id") or step.get("template") or "step")
        tmpl = str(step.get("template") or "")

        # Approach clearance: auto after check-in (short radio gap), while still
        # inbound to the IAF / VFR exit — not a field-NM or drawn-zone gate.
        if tmpl in ("approach_procedure", "approach_iaf"):
            ready, waiting = atc_phrase.approach_clearance_auto_ready(
                airport=airport,
                state=state,
                config=cfg,
                callsign=callsign or None,
                opus=opus,
            )
            if not ready:
                return "", waiting
            key = f"{step_id}:approach_clearance"
            latch = f"fire:{key}"
            if not tracker.armed(latch):
                return "", waiting
            tracker.pending_latch = latch
            return step_id, waiting

        if trigger is None:
            return "", ""
        if not trigger.enabled(cfg):
            return "", ""
        dwell = trigger.dwell(cfg)
        key = f"{step_id}:{status.runway or 'field'}"
        # Straight-in / instrument: one ship per TX — allow re-fire per seat.
        if tmpl == "clear_land" and atc_phrase.landing_already_cleared(
            state, opus=opus, step=step, mission=mission
        ):
            return "", "already cleared to land"
        if tmpl == "clear_land":
            seat = atc_phrase.peek_next_landing_clear_seat(
                opus,
                callsign,
                step=step,
                mission=mission,
                state=state,
            )
            if seat is not None:
                key = f"{key}:s{seat}"
        zone_list = [z for z in (zones or []) if isinstance(z, dict)]

        # Instrument missed → Approach rewind: do not auto contact-tower / land
        # while still inside the rearm bubble (near-field misfire guard).
        if tmpl in ("cleared_approach", "contact_tower", "right_break", "clear_land"):
            ok_rearm, wait_rearm = atc_phrase.tower_land_gates_allowed(state, distance_nm)
            if not ok_rearm:
                return "", wait_rearm

        held, waiting = runway_position.condition_held(
            trigger,
            status,
            zones=zone_list,
            distance_nm=distance_nm,
            config=cfg,
            tracker=tracker,
            leave_key=key,
        )

        # After a go-around: land only on base / short final, and only after
        # they have left the final they waved off from.
        pattern_nm = atc_phrase.pattern_land_within_nm(state) if tmpl == "clear_land" else None
        if pattern_nm is not None:
            on_final, wait_final = runway_position.on_base_or_short_final(
                status, within_nm=float(pattern_nm)
            )
            if state is not None and state.get("pattern_land_needs_leave"):
                if on_final:
                    return "", "go-around — leave final, then land on base / short final"
                state["pattern_land_needs_leave"] = False
            if not on_final:
                return "", wait_final
            waiting = wait_final

        if not tracker.held_for(f"cond:{key}", held, dwell):
            if held and dwell:
                left = dwell - tracker.held_since(f"cond:{key}")
                waiting += f"  ·  settling, {max(0.0, left):.0f}s to go"
            return "", waiting

        # Pacing against the radio rather than the aircraft: a clearance that
        # steps on the transmission before it is worse than a late one.
        gap_left = runway_position.gap_remaining(trigger, state)
        if gap_left > 0:
            return "", f"{waiting}  ·  holding {gap_left:.0f}s for radio gap"

        latch = f"fire:{key}"
        if not tracker.armed(latch):
            return "", waiting
        tracker.pending_latch = latch
        return step_id, waiting

    def _position_apply(
        self,
        status: runway_position.FlightStatus,
        fire: str,
        waiting: str,
        step: dict[str, Any],
        engine: Any,
        *,
        bind_live: bool = True,
        session: Any = None,
        tracker: runway_position.PositionTracker | None = None,
    ) -> None:
        need_full = bool(self.config_data.get("auto_clearance_require_full_flight", True))
        summary = waiting or status.summary(need_full=need_full)
        label = str(step.get("label") or step.get("template") or "")
        tmpl = str(step.get("template") or "")
        approach_auto = tmpl in ("approach_procedure", "approach_iaf")
        prefix = status.runway or "RWY"
        if waiting and label:
            prefix = f"{prefix} · {label}"
        if bind_live:
            self.fly_position.set(f"{prefix}: {summary}")
            if hasattr(self, "_fly_position_lbl"):
                if fire:
                    color = C_GREEN
                elif waiting or approach_auto:
                    color = C_AMBER
                elif not status.ok:
                    color = C_MUTED
                elif status.all_in_position(need_full=need_full) or status.all_at_eor(
                    need_full=need_full
                ):
                    color = C_GREEN
                else:
                    color = C_AMBER
                self._fly_position_lbl.configure(fg=color)
        if not fire:
            return
        if tracker is None:
            tracker = self._position_tracker()
        latch = tracker.pending_latch or f"fire:{fire}:{status.runway or 'field'}"
        try:
            if session is not None and self._atc_server is not None:
                result = self._atc_server.run_action(
                    session,
                    lambda e: e.play_id(fire)
                    if step.get("id")
                    else e.play_template(fire),
                )
            else:
                result = engine.play_id(fire) if step.get("id") else engine.play_template(fire)
        except Exception as exc:  # noqa: BLE001
            tracker.clear_fired(latch)
            tracker.pending_latch = ""
            self._note_no_tx(str(exc), action="auto", step=step if isinstance(step, dict) else None)
            return
        tracker.fire_once(latch)
        tracker.pending_latch = ""
        spoken = (result or {}).get("label") or label or fire
        who = ""
        if session is not None:
            who = f"{getattr(session, 'callsign', '')}  "
        self._voice_log(f"AUTO  {who}{spoken} — {summary}")
        self._on_trigger_received(f"AUTO {spoken} (position)")
        if bind_live:
            self.engine = engine
            self._refresh_fly_status()

    def _sync_eam_strip_from_srs(self) -> None:
        """When SRS UDP is live, mirror the selected radio onto the EAM strip."""
        if not bool(self.config_data.get("freq_gate_eam_enabled")):
            return
        if not hasattr(self, "_eam_freq_vars") or not self._eam_freq_vars:
            return
        st = srs_radio.srs_udp_status()
        if not st.get("fresh") or st.get("selected_mhz") is None:
            if hasattr(self, "_fly_eam_hint"):
                err = st.get("error") or "waiting for SRS client…"
                port = st.get("port")
                self._fly_eam_hint.set(
                    f"SRS live tune: {err}"
                    + (f" (UDP {port})" if port else " — radio buttons are fallback")
                )
            return
        mhz = float(st["selected_mhz"])
        if hasattr(self, "_fly_eam_hint"):
            idx = int(st.get("selected_index") or 0)
            name = st.get("name") or "SRS"
            self._fly_eam_hint.set(
                f"SRS live: {name} selected R{idx} · {mhz:.3f} MHz (common PTT)"
            )
        # Match an existing strip row by frequency; otherwise update active row.
        tol = float(self.config_data.get("freq_gate_tolerance_mhz") or 0.05)
        matched = None
        for i, var in enumerate(self._eam_freq_vars):
            try:
                row_mhz = float(str(var.get()).strip())
            except (TypeError, ValueError):
                continue
            if abs(row_mhz - mhz) <= tol:
                matched = i
                break
        display = f"{mhz:.3f}".rstrip("0").rstrip(".")
        if matched is None:
            try:
                active = int(self.var_eam_active.get()) if hasattr(self, "var_eam_active") else 0
            except (TypeError, ValueError, tk.TclError):
                active = 0
            active = max(0, min(active, len(self._eam_freq_vars) - 1))
            if str(self._eam_freq_vars[active].get()).strip() != display:
                self._eam_freq_vars[active].set(display)
            if hasattr(self, "var_eam_active") and int(self.var_eam_active.get()) != active:
                self.var_eam_active.set(active)
                freqs = []
                for var in self._eam_freq_vars:
                    try:
                        freqs.append(float(str(var.get()).strip()))
                    except (TypeError, ValueError):
                        pass
                if freqs:
                    self._rebuild_eam_freq_rows(freqs)
            self.config_data["freq_gate_eam_active"] = active
            srs_radio.set_eam_active_index(active)
            return
        if hasattr(self, "var_eam_active") and int(self.var_eam_active.get()) != matched:
            self.var_eam_active.set(matched)
            freqs = []
            for var in self._eam_freq_vars:
                try:
                    freqs.append(float(str(var.get()).strip()))
                except (TypeError, ValueError):
                    pass
            if freqs:
                self._rebuild_eam_freq_rows(freqs)
            self.config_data["freq_gate_eam_active"] = matched
            srs_radio.set_eam_active_index(matched)

    def _clear_tk_hotkeys(self) -> None:
        for seq in getattr(self, "_tk_hotkey_binds", []) or []:
            try:
                self.unbind_all(seq)
            except tk.TclError:
                pass
        self._tk_hotkey_binds = []

    def _apply_hotkeys(self) -> None:
        """Register global (Windows) + in-app binds for Next / Back / step cycle."""
        combos: dict[str, str] = {}
        for which, default in (
            ("next", hotkeys.DEFAULT_HOTKEY_NEXT),
            ("back", hotkeys.DEFAULT_HOTKEY_BACK),
            ("seek_next", hotkeys.DEFAULT_HOTKEY_SEEK_NEXT),
            ("seek_prev", hotkeys.DEFAULT_HOTKEY_SEEK_PREV),
        ):
            var = getattr(self, f"var_hotkey_{which}", None)
            if var is not None:
                combo = hotkeys.normalize_hotkey(var.get(), default=default)
                var.set(combo)
            else:
                combo = hotkeys.hotkey_from_config(self.config_data, which)
            combos[which] = combo
            self.config_data[f"hotkey_{which}"] = combo

        seen_combos: dict[str, str] = {}
        dup_warnings: list[str] = []
        for which, combo in combos.items():
            if not combo:
                continue
            other = seen_combos.get(combo)
            if other:
                dup_warnings.append(
                    f"{which.replace('_', ' ')} uses the same key as {other.replace('_', ' ')} ({combo})"
                )
            else:
                seen_combos[combo] = which

        self._clear_tk_hotkeys()

        callbacks = {
            "next": lambda: self._trigger("next"),
            "back": lambda: self._trigger("back"),
            "seek_next": lambda: self._trigger("seek_next"),
            "seek_prev": lambda: self._trigger("seek_prev"),
        }
        labels = {
            "next": "Next",
            "back": "Back",
            "seek_next": "Step ▶",
            "seek_prev": "Step ◀",
        }

        # In-app binds (when this window has focus)
        for which, cb in callbacks.items():
            parsed = hotkeys.parse_hotkey(combos[which])
            if not parsed:
                continue
            _mods, _vk, seq = parsed

            def _handler(_event: object, fn: Callable[[], None] = cb) -> str:
                fn()
                return "break"

            try:
                self.bind_all(seq, _handler)
                self._tk_hotkey_binds.append(seq)
            except tk.TclError:
                pass

        warnings = self._hotkey_listener.start(
            next_hotkey=combos["next"],
            back_hotkey=combos["back"],
            on_next=callbacks["next"],
            on_back=callbacks["back"],
            seek_next_hotkey=combos["seek_next"],
            seek_prev_hotkey=combos["seek_prev"],
            on_seek_next=callbacks["seek_next"],
            on_seek_prev=callbacks["seek_prev"],
        )
        warnings[0:0] = dup_warnings
        # Polled in parallel: RegisterHotKey dies in-game, this does not.
        # _trigger() collapses the two so a press still counts once.
        self._keys.clear_bindings()
        for which, cb in callbacks.items():
            combo = combos[which]
            if not combo:
                continue

            def fire(fn: Callable[[], None] = cb, name: str = labels[which]) -> None:
                self._on_trigger_received(f"Key {name}")
                fn()

            warning = self._keys.set_binding(which, combo, on_press=fire)
            if warning:
                warnings.append(warning)
        self._keys.start()

        registered = self._hotkey_listener.registered
        status = f"Keyboard: {', '.join(registered) if registered else 'none registered'}"
        warnings.extend(self._apply_joystick(callbacks))
        joy = [
            f"{labels[key]} {joystick.describe_binding(self._joy_bindings[key])}"
            for key in labels
            if self._joy_bindings.get(key)
        ]
        status += f"   ·   Controller: {', '.join(joy) if joy else 'none bound'}"
        if warnings:
            status += "\n" + "\n".join(f"! {w}" for w in warnings)
        if hasattr(self, "var_hotkey_status"):
            self.var_hotkey_status.set(status)
        self._update_fly_hotkey_hint()

    def _apply_joystick(
        self, callbacks: dict[str, Callable[[], None]]
    ) -> list[str]:
        """Bind HOTAS / mouse buttons. Unlike hotkeys these survive DCS having focus."""
        warnings: list[str] = []
        self._joystick.clear_bindings()
        if not self._joystick.supported:
            return warnings
        labels = {
            "next": "Next",
            "back": "Back",
            "seek_next": "Step ▶",
            "seek_prev": "Step ◀",
        }
        for key, cb in callbacks.items():
            binding = self._joy_bindings.get(key)
            var = getattr(self, f"var_joy_{key}", None)
            if var is not None:
                var.set(joystick.describe_binding(binding))
            if not binding:
                continue
            source = "Mouse" if joystick.is_mouse_binding(binding) else "HOTAS"

            def fire(
                fn: Callable[[], None] = cb,
                name: str = labels.get(key, key),
                src: str = source,
            ) -> None:
                self._on_trigger_received(f"{src} {name}")
                fn()

            warning = self._joystick.set_binding(key, binding, on_press=fire)
            if warning:
                warnings.append(f"{labels.get(key, key)}: {warning}")
        if any(self._joy_bindings.values()):
            self._joystick.start()
        else:
            self._joystick.stop()
        return warnings

    def _learn_joy_button(self, which: str) -> None:
        """Capture the next HOTAS or mouse press and bind it to a Fly action."""
        var = getattr(self, f"var_joy_{which}")
        previous = var.get()
        var.set("Press a HOTAS or mouse button…")

        def done(binding: dict[str, Any]) -> None:
            def apply() -> None:
                self._joy_bindings[which] = joystick.normalize_binding(binding)
                var.set(joystick.describe_binding(self._joy_bindings[which]))
                self._apply_hotkeys()

            self.after(0, apply)

        def cancel() -> None:
            if var.get().startswith("Press a HOTAS or mouse"):
                self._joystick.learn_next_press(None)
                var.set(previous)

        self._joystick.learn_next_press(done)
        self.after(10000, cancel)

    def _clear_joy_button(self, which: str) -> None:
        self._joy_bindings[which] = None
        getattr(self, f"var_joy_{which}").set("(none)")
        self._apply_hotkeys()

    def _suggest_srs_ptt(self) -> None:
        """Show which HOTAS buttons SRS already uses to transmit."""
        found = joystick.discover_srs_ptt()
        if not found:
            messagebox.showinfo(
                "SRS PTT",
                "No SRS transmit bindings matched a connected device.\n\n"
                "Checked the DCS-SimpleRadio-Standalone Client folder for .cfg profiles.",
            )
            return
        lines = "\n".join(
            f"  • {joystick.describe_binding(b)}   ({b['source']})" for b in found
        )
        messagebox.showinfo(
            "SRS PTT buttons",
            "SRS transmits on these HOTAS buttons:\n\n"
            f"{lines}\n\n"
            "Pick a different button for Next/Back so you do not advance the flow "
            "every time you key the radio.",
        )

    def _restart_as_admin(self) -> None:
        """
        Relaunch elevated. DCS and SRS run as administrator, and Windows UIPI
        refuses to deliver hotkeys to a lower-integrity process while they are focused.
        """
        if hotkeys.is_elevated():
            messagebox.showinfo("Administrator", "Already running as administrator.")
            return
        if not messagebox.askyesno(
            "Restart as administrator",
            "Restart the Flow Planner with administrator rights?\n\n"
            "This lets keyboard hotkeys work while DCS is focused. "
            "Unsaved plan changes will be lost.",
        ):
            return
        script = str(HERE / "flow_ui.py")
        try:
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, f'"{script}"', str(HERE), 1
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Restart as administrator", str(exc))
            return
        if int(rc) <= 32:
            messagebox.showerror(
                "Restart as administrator",
                "Windows declined the elevation request (UAC cancelled?).",
            )
            return
        self._on_close()

    # ---------- voice control ----------

    def _ui_call(self, fn: Callable[[], None]) -> None:
        """
        Schedule work on the Tk thread from an audio/model thread.

        Silently drops the callback once the window is gone, which happens
        routinely when the app closes mid-transcription.
        """
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _voice_context(self) -> dict[str, Any]:
        """
        Where the flight is right now, so the recognizer knows what to expect.

        Runs off-thread on every transmission, so everything here must be
        cached state — no network.
        """
        context: dict[str, Any] = {}
        self._sync_client_flow_cursor()
        try:
            airport = self.engine.airport()
            context["airport"] = airport
            # Both lists, so "request runway two one left" resolves even when
            # 21L is only ever used for instrument approaches.
            usable = atc_phrase.airport_ops_runways(airport) + (
                atc_phrase.airport_instrument_runways(airport)
            )
            context["runways"] = list(dict.fromkeys(usable))
        except Exception:  # noqa: BLE001
            pass
        cursor_channel = ""
        mission_phase = ""
        try:
            step = self.engine.current_step()
            if step:
                cursor_channel = str(step.get("channel") or "").strip().lower()
                mission_phase = voice_intent.normalize_mission_phase(
                    str(step.get("phase") or ""),
                    channel=cursor_channel,
                )
                live_step = step
                sid = str(step.get("id") or "").strip()
                live = self.mission.get("steps") if isinstance(self.mission.get("steps"), list) else None
                if sid and live:
                    for row in live:
                        if isinstance(row, dict) and str(row.get("id") or "").strip() == sid:
                            live_step = row
                            break
                context["expected"] = voice_intent.step_expected_template(live_step)
                context["current_step_id"] = sid
        except Exception:  # noqa: BLE001
            pass
        # Tips / voice scoring follow the live radio when it is an agency in
        # this mission phase (Blackjack vs Bandsaw during Flight, etc.).
        tuned = None
        try:
            tuned = srs_radio.channel_for_tuned_freq(
                context.get("airport") or self.engine.airport(),
                self.config_data,
            )
        except Exception:  # noqa: BLE001
            tuned = None
        context["tuned_channel"] = tuned or ""
        context["cursor_channel"] = cursor_channel
        context["phase"] = mission_phase
        context["channel"] = voice_intent.resolve_context_channel(
            mission_phase=mission_phase,
            cursor_channel=cursor_channel,
            tuned_channel=tuned,
        )
        context["callsign"] = atc_phrase.cached_radio_callsign(self.config_data)
        seat = atc_phrase.configured_opus_seat(self.config_data)
        if seat is not None:
            context["seat"] = int(seat)
        try:
            # Steps carry their own voice_phrases, so the grammar is per-mission.
            context["steps"] = list(self.engine.steps)
            # Prefer the live Plan mission so Keywords saved on a step work in Fly
            # before Save mission writes the JSON file.
            live = self.mission.get("steps") if isinstance(self.mission.get("steps"), list) else None
            if live:
                context["steps"] = [
                    s for s in live if isinstance(s, dict) and s.get("enabled", True)
                ]
        except Exception:  # noqa: BLE001
            context["steps"] = []
        state = getattr(self.engine, "state", None) or {}
        context["awaiting_readback"] = bool(state.get("awaiting_readback"))
        items = state.get("readback_items") or []
        context["readback_items"] = items if isinstance(items, list) else []
        context["last_tx_text"] = str(state.get("last_tx_text") or "")
        context["last_tx_channel"] = str(state.get("last_tx_channel") or "")
        context["last_tx_template"] = str(state.get("last_tx_template") or "")
        try:
            import tanker_chat as tanker_chat_mod

            context["tanker_chat_choices"] = tanker_chat_mod.current_choices(state)
            context["tanker_chat_session"] = tanker_chat_mod.is_session_active(state)
            context["tanker_chat_awaiting_react"] = tanker_chat_mod.is_awaiting_react(
                state
            )
            # Session itself is enough — don't hit Ollama /api/tags on PTT release.
            context["tanker_chat_freeform"] = bool(
                context["tanker_chat_awaiting_react"]
                or context["tanker_chat_session"]
                or tanker_chat_mod.is_open(state)
            )
            context["tanker_chat_last_spoke"] = tanker_chat_mod.last_spoke(state)
            try:
                context["tanker_chat_guard_until"] = float(
                    state.get("tanker_chat_guard_until") or 0
                )
            except (TypeError, ValueError):
                context["tanker_chat_guard_until"] = 0.0
        except Exception:
            context["tanker_chat_choices"] = []
            context["tanker_chat_session"] = False
            context["tanker_chat_awaiting_react"] = False
            context["tanker_chat_freeform"] = False
            context["tanker_chat_last_spoke"] = ""
            context["tanker_chat_guard_until"] = 0.0
        try:
            context["last_tx_at"] = float(state.get("last_tx_at") or 0.0)
        except (TypeError, ValueError):
            context["last_tx_at"] = 0.0
        # While a readback is open, the call due is that checklist — not the
        # next timeline step (e.g. stay on taxi readback, not monitor tower).
        if context["awaiting_readback"] and state.get("last_tx_template"):
            context["expected"] = str(state.get("last_tx_template") or "")
            if state.get("last_tx_channel"):
                last_ch = str(state.get("last_tx_channel") or "").strip().lower()
                context["channel"] = last_ch or context.get("channel") or ""
                # Keep the mission phase from the cursor; only the agency changes.
        return context

    def _sync_client_flow_cursor(self) -> None:
        """Copy the Host's shared flight cursor onto this Client's local engine."""
        if self._atc_role() != "client":
            return
        client = getattr(self, "_atc_client", None)
        if client is None:
            return
        st = client.last_status or {}
        fs = st.get("flow_state")
        if not isinstance(fs, dict):
            if st.get("index") is None:
                return
            fs = {"index": st.get("index")}
        state = getattr(self.engine, "state", None)
        if not isinstance(state, dict):
            return
        for key, value in fs.items():
            state[key] = value

    def _snap_voice_confidence(self, value: float | None = None) -> float:
        """Clamp to the slider range and snap to 5% steps (0.40, 0.45, … 0.95)."""
        try:
            raw = float(self.var_voice_confidence.get() if value is None else value)
        except (tk.TclError, TypeError, ValueError):
            raw = float(voice_engine.DEFAULT_MIN_CONFIDENCE)
        snapped = round(raw * 20.0) / 20.0
        return max(0.40, min(0.95, snapped))

    def _sync_voice_confidence_label(self, value: float | None = None) -> float:
        snapped = self._snap_voice_confidence(value)
        if hasattr(self, "var_voice_confidence"):
            try:
                if abs(float(self.var_voice_confidence.get()) - snapped) > 1e-9:
                    self.var_voice_confidence.set(snapped)
            except (tk.TclError, TypeError, ValueError):
                self.var_voice_confidence.set(snapped)
        if hasattr(self, "var_voice_confidence_label"):
            self.var_voice_confidence_label.set(f"{snapped:.0%}")
        return snapped

    def _on_voice_confidence_slide(self, value: str = "") -> None:
        """ttk.Scale callback — keep the readout on clean percentages."""
        try:
            raw = float(value) if str(value).strip() != "" else None
        except (TypeError, ValueError):
            raw = None
        self._sync_voice_confidence_label(raw)

    def _apply_voice(self) -> None:
        """(Re)start the recognizer from current config."""
        if hasattr(self, "var_voice_enabled"):
            self.config_data["voice_enabled"] = bool(self.var_voice_enabled.get())
            self.config_data["voice_model"] = self.var_voice_model.get().strip() or voice_engine.DEFAULT_MODEL
            self.config_data["voice_mic_device"] = self._selected_mic_index()
            self.config_data["voice_min_confidence"] = self._sync_voice_confidence_label()
            self.config_data["voice_require_address"] = bool(self.var_voice_require_address.get())
            if hasattr(self, "var_voice_nlu"):
                self.config_data["voice_nlu_enabled"] = bool(self.var_voice_nlu.get())

        if self._voice is None:
            self._voice = voice_engine.VoiceController(
                on_intent=self._on_voice_intent,
                on_status=self._on_voice_status,
                on_transcript=self._on_voice_transcript,
                context=self._voice_context,
            )
        warnings = self._voice.start(self.config_data)
        if not self.config_data.get("voice_enabled"):
            self._set_voice_status("Voice control off")
        elif warnings:
            self._set_voice_status("; ".join(warnings))
        self._refresh_voice_prompts()

    def _selected_mic_index(self) -> int:
        label = self.var_voice_mic.get().strip()
        for device in getattr(self, "_mic_devices", []):
            if str(device["name"]) == label:
                return int(device["index"])
        return -1

    def _set_voice_status(self, text: str) -> None:
        if hasattr(self, "var_voice_status"):
            self.var_voice_status.set(text)

    def _on_voice_status(self, text: str) -> None:
        self._ui_call(lambda: self._set_voice_status(text))

    def _on_voice_transcript(self, evaluation: voice_intent.Evaluation) -> None:
        text = evaluation.transcript

        def show() -> None:
            heard = text or "(nothing)"
            summary = f"MIC  {heard}  ->  {evaluation.describe()}"
            if hasattr(self, "var_voice_heard"):
                self.var_voice_heard.set(f"“{heard}”  →  {evaluation.describe()}")
            if text and hasattr(self, "fly_log"):
                self.fly_log.insert(tk.END, summary + "\n")
                self.fly_log.see(tk.END)
            # Always update the Fly glance panel (including "nothing heard").
            self._append_fly_voice_feed(summary)

        self._ui_call(show)

    def _on_voice_intent(self, match: voice_intent.Match) -> None:
        """Run the matched intent off the audio thread, then refresh Fly."""

        def work() -> None:
            try:
                if self._atc_role() == "client" and self._atc_client is not None:
                    result = self._atc_client.post_intent(match)
                else:
                    engine = self._live_engine()
                    result = voice_engine.execute_intent(match, engine)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)

                def fail() -> None:
                    if err.startswith("Blocked:"):
                        self._note_no_tx(err, action="voice")
                    else:
                        self._voice_log(f"VOICE  {match.intent} failed: {err}")

                self._ui_call(fail)
                return

            def done() -> None:
                action = result.get("action")
                detail = result.get("detail")
                if action == "blocked":
                    blocked = str(detail or "Blocked off frequency")
                    self._note_no_tx(
                        blocked,
                        action="voice",
                        channel=str(result.get("channel") or ""),
                    )
                elif action in ("hint",):
                    self._voice_log(f"DCS  {detail or result.get('text') or 'tanker radio'}")
                elif action in ("transmit", "queued"):
                    ch = str(result.get("channel") or "").strip().lower()
                    spoken = str(result.get("text") or result.get("label") or "")
                    queued = ""
                    if result.get("queued"):
                        queued = f"  (queue {result.get('queue_pos')})"
                    if ch == "tanker" and spoken and self._atc_role() != "client":
                        note = ""
                        try:
                            import tanker_chat as tanker_chat_mod

                            note = tanker_chat_mod.llm_note(engine.state)
                        except Exception:
                            note = ""
                        self._log_texaco(spoken, note)
                    else:
                        self._voice_log(
                            f"TX   {str(result.get('channel') or '').upper()}  {spoken}{queued}"
                        )
                elif isinstance(detail, dict):
                    self._voice_log(f"TX   {detail.get('label') or detail.get('step_id') or action}")
                else:
                    self._voice_log(f"VOICE  {action}: {detail}")
                if self._atc_role() != "client":
                    self._refresh_fly_status()
                else:
                    self._refresh_client_fly()
                deferred = result.get("deferred")
                if isinstance(deferred, dict) and deferred.get("kind") == "unrestricted_climb":
                    self._schedule_unrestricted_climb_resolve(deferred)
                elif isinstance(deferred, dict) and deferred.get("kind") in (
                    "tanker_chat",
                    "tanker_chat_continue",
                ):
                    self._schedule_tanker_chat(deferred)

            self._ui_call(done)

        threading.Thread(target=work, daemon=True).start()

    def _schedule_unrestricted_climb_resolve(self, deferred: dict[str, Any]) -> None:
        """After Tower's 'standby', come back with approve/unable then takeoff."""
        try:
            delay_s = float(deferred.get("delay_s") or 5.0)
        except (TypeError, ValueError):
            delay_s = 5.0
        delay_ms = max(1500, int(delay_s * 1000))
        try:
            chance = float(deferred.get("approve_chance"))
        except (TypeError, ValueError):
            chance = None

        def kick() -> None:
            def work() -> None:
                try:
                    engine = flow_engine.FlowEngine()
                    result = voice_engine.resolve_unrestricted_climb(
                        engine, approve_chance=chance
                    )
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)

                    def fail() -> None:
                        self._voice_log(f"VOICE  unrestricted climb failed: {err}")

                    self._ui_call(fail)
                    return

                def done() -> None:
                    # Unable path: short deny TX, then takeoff. Approve path: one
                    # combined takeoff clearance (climb unrestricted + winds).
                    if result.get("action") == "transmit" and result.get("text"):
                        self._voice_log(
                            f"TX   {str(result.get('channel') or 'tower').upper()}  "
                            f"{result.get('text', '')}"
                        )
                    takeoff = result.get("takeoff") if isinstance(result.get("takeoff"), dict) else None
                    if takeoff:
                        detail = takeoff.get("detail")
                        if isinstance(detail, dict):
                            spoken = str(detail.get("text") or "").strip()
                            label = detail.get("label") or detail.get("step_id") or "takeoff"
                            if spoken:
                                self._voice_log(
                                    f"TX   {str(detail.get('channel') or 'tower').upper()}  {spoken}"
                                )
                            else:
                                self._voice_log(f"TX   {label}")
                        elif detail:
                            self._voice_log(f"VOICE  takeoff: {detail}")
                    elif result.get("action") in ("play", "transmit") and result.get("text"):
                        # Approve path returned the combined clearance as text.
                        pass
                    self.engine = engine
                    self._refresh_fly_status()

                self._ui_call(done)

            threading.Thread(target=work, daemon=True).start()

        self.after(delay_ms, kick)

    def _schedule_tanker_chat(self, deferred: dict[str, Any]) -> None:
        """After rejoin (or between answers), Texaco comes back with boom small talk."""
        kind = str(deferred.get("kind") or "tanker_chat")
        try:
            delay_s = float(deferred.get("delay_s") or 12.0)
        except (TypeError, ValueError):
            delay_s = 12.0
        # Continuations are already a deliberate pause — allow shorter floors.
        floor_ms = 2500 if kind == "tanker_chat_continue" else 4000
        delay_ms = max(floor_ms, int(delay_s * 1000))
        continue_session = kind == "tanker_chat_continue"

        def kick() -> None:
            def work() -> None:
                try:
                    engine = self._live_engine()
                    result = voice_engine.resolve_tanker_chat(
                        engine, continue_session=continue_session
                    )
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)

                    def fail() -> None:
                        self._voice_log(f"VOICE  tanker chat failed: {err}")

                    self._ui_call(fail)
                    return

                def done() -> None:
                    self._finish_boom_tx(engine, result)

                self._ui_call(done)

            threading.Thread(target=work, daemon=True).start()

        self.after(delay_ms, kick)

    def _voice_log(self, line: str) -> None:
        text = line.rstrip() + "\n"
        if hasattr(self, "fly_log"):
            self.fly_log.insert(tk.END, text)
            self.fly_log.see(tk.END)
        self._append_fly_voice_feed(text)

    def _note_no_tx(
        self,
        reason: str,
        *,
        action: str = "",
        step: dict[str, Any] | None = None,
        channel: str = "",
    ) -> None:
        """LAST HEARD + log: a scripted TX was attempted but did not go out."""
        label = ""
        if isinstance(step, dict):
            label = str(
                step.get("label") or step.get("id") or step.get("channel") or ""
            ).strip()
        ch = (channel or "").strip().upper()
        if not ch and isinstance(step, dict):
            ch = str(step.get("channel") or "").strip().upper()
        act = (action or "").strip().upper()
        why = (reason or "did not transmit").strip()
        if why.lower().startswith("blocked:"):
            why = why.split(":", 1)[1].strip()
        parts = ["NO TX"]
        if act:
            parts.append(act)
        if label:
            parts.append(label)
        elif ch:
            parts.append(ch)
        line = "  ·  ".join(parts) + f"  —  {why}"
        self._voice_log(line)
        self._on_trigger_received(line)

    def _append_fly_voice_feed(self, line: str) -> None:
        """Push a short line into the Fly freq-box LAST HEARD panel."""
        if not hasattr(self, "fly_voice_feed"):
            return
        text = line if line.endswith("\n") else line + "\n"
        # Mashing Next off-freq should not flood the 8-line glance panel.
        if text.lstrip().upper().startswith("NO TX"):
            if getattr(self, "_last_no_tx_feed", "") == text:
                return
            self._last_no_tx_feed = text
        else:
            self._last_no_tx_feed = ""
        feed = self.fly_voice_feed
        feed.configure(state=tk.NORMAL)
        raw = text.lstrip()
        upper = raw.upper()
        tag = ""
        if upper.startswith("NO TX"):
            tag = "no_tx"
        elif upper.startswith("TEXACO") or upper.startswith("TX   TANKER"):
            tag = "texaco"
        elif upper.startswith("MIC") and "ignored" in raw.lower():
            tag = "mic_ignore"
        if tag:
            feed.insert(tk.END, text, tag)
        else:
            feed.insert(tk.END, text)
        # Keep the glanceable panel short (newest at bottom).
        try:
            end_line = int(float(feed.index("end-1c").split(".")[0]))
        except (TypeError, ValueError):
            end_line = 0
        max_lines = 8
        if end_line > max_lines:
            feed.delete("1.0", f"{end_line - max_lines + 1}.0")
        feed.see(tk.END)
        feed.configure(state=tk.DISABLED)

    def _capture_key(self, prompt: str, apply: Callable[[str], None]) -> None:
        """Modal: press a key combo, hand it to `apply`."""
        dlg = tk.Toplevel(self)
        dlg.title("Capture key")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        dlg.grab_set()
        self._place_dialog(dlg, 420, 160)
        tk.Label(
            dlg,
            text=prompt,
            bg=C_BG,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 11),
            wraplength=390,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(16, 6))
        var_live = tk.StringVar(value="Waiting…")
        tk.Label(dlg, textvariable=var_live, bg=C_BG, fg=C_GREEN, font=("Consolas", 14)).pack(
            anchor="w", padx=16, pady=4
        )
        tk.Label(
            dlg,
            text="Esc cancels  ·  modifiers alone are ignored",
            bg=C_BG,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=16, pady=(4, 8))

        def on_key(event: tk.Event) -> str:  # type: ignore[type-arg]
            if event.keysym in ("Escape",):
                dlg.destroy()
                return "break"
            combo = hotkeys.format_event(event)
            if not combo:
                return "break"
            var_live.set(combo)
            apply(combo)
            dlg.after(200, dlg.destroy)
            return "break"

        dlg.bind("<KeyPress>", on_key)
        dlg.focus_set()
        dlg.wait_window()

    def _capture_hotkey(self, which: str) -> None:
        """Press a key combo to set a Fly action."""
        labels = {
            "next": "Advance (Next)",
            "back": "Previous (Back)",
            "seek_next": "Step forward (no TX)",
            "seek_prev": "Step back (no TX)",
        }
        label = labels.get(which, which)
        var = getattr(self, f"var_hotkey_{which}")

        def apply(combo: str) -> None:
            var.set(combo)
            self.after(250, self._apply_hotkeys)

        self._capture_key(f"Press a key combo for {label}", apply)

    def _clear_hotkey(self, which: str) -> None:
        var = getattr(self, f"var_hotkey_{which}", None)
        if var is None:
            return
        var.set("")
        self.after(250, self._apply_hotkeys)

    def _capture_voice_ptt_key(self) -> None:
        """Press a key to use as push-to-talk. Held, not tapped."""

        def apply(combo: str) -> None:
            self.config_data["voice_ptt_key"] = combo
            self.var_voice_ptt_key.set(combo)
            self.after(250, self._apply_voice)

        self._capture_key(
            "Hold this key to talk. Pick the same key SRS transmits on, "
            "or a spare one if you would rather keep them separate.",
            apply,
        )

    def _clear_voice_ptt_key(self) -> None:
        self.config_data["voice_ptt_key"] = ""
        self.var_voice_ptt_key.set("(none)")
        self._apply_voice()

    def _build(self) -> None:
        top = tk.Frame(self, bg=C_BG)
        top.pack(fill=tk.X, padx=16, pady=(10, 4))
        ttk.Label(top, text="Mission Flow Planner", style="Title.TLabel").pack(side=tk.LEFT)
        self.mission_name_var = tk.StringVar(value=self.mission.get("name") or "Untitled")
        name_entry = ttk.Entry(top, textvariable=self.mission_name_var, width=22)
        name_entry.pack(side=tk.LEFT, padx=(16, 8))
        ttk.Button(top, text="Help", command=self._show_help_tab).pack(side=tk.RIGHT)
        self._ensure_identity_vars()
        # Same title row as Help — click the flight chip to choose / refresh / clear.
        self._build_opus_identity_bar(top, key="header").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 12)
        )

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 8))
        self.tab_plan = ttk.Frame(self.nb)
        self.tab_fly = ttk.Frame(self.nb)
        self.tab_traffic = ttk.Frame(self.nb)
        self.tab_setup = ttk.Frame(self.nb)
        self.tab_help = ttk.Frame(self.nb)
        self.nb.add(self.tab_plan, text="  Plan Flight  ")
        self.nb.add(self.tab_fly, text="  Fly  ")
        self.nb.add(self.tab_traffic, text="  Traffic  ")
        self.nb.add(self.tab_setup, text="  Setup  ")
        self.nb.add(self.tab_help, text="  Help  ")

        self._build_plan()
        self._build_fly()
        self._build_traffic()
        self._build_setup()
        self._build_help()
        self.nb.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

    def _on_notebook_tab_changed(self, _evt: object | None = None) -> None:
        try:
            idx = self.nb.index(self.nb.select())
            on_fly = idx == self.nb.index(self.tab_fly)
            on_plan = idx == self.nb.index(self.tab_plan)
        except tk.TclError:
            return
        if on_fly or on_plan:
            self._refresh_opus_identity_bar()
        if on_fly:
            self._refresh_fly_status()
            if hasattr(self, "_fly_wheel"):
                self._fly_canvas.bind_all("<MouseWheel>", self._fly_wheel)
            self.after_idle(self._fly_update_scrollregion)
        else:
            self._fly_maybe_unbind_wheel()
            if hasattr(self, "_setup_controls_canvas"):
                try:
                    self._setup_controls_canvas.unbind_all("<MouseWheel>")
                except tk.TclError:
                    pass
        try:
            on_traffic = idx == self.nb.index(self.tab_traffic)
        except tk.TclError:
            on_traffic = False
        if on_traffic:
            self._refresh_traffic()

    def _fly_maybe_unbind_wheel(self) -> None:
        """Drop the Fly wheel handler unless the pointer is still over Fly content."""
        try:
            if self.nb.index(self.nb.select()) != self.nb.index(self.tab_fly):
                self._fly_canvas.unbind_all("<MouseWheel>")
                return
            x, y = self.winfo_pointerxy()
            widget = self.winfo_containing(x, y)
            while widget is not None:
                if widget in (self._fly_canvas, getattr(self, "tab_fly", None)):
                    return
                widget = getattr(widget, "master", None)
            self._fly_canvas.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass

    def _wheel_owned_elsewhere(self, event: tk.Event) -> bool:
        """
        True when MouseWheel should scroll a Listbox / modal / Combobox popdown
        instead of the Plan/Fly canvas. bind_all + return "break" otherwise steals
        the wheel (lists then only scroll with middle-mouse drag).
        """
        w = event.widget
        try:
            if str(w.winfo_class()) == "Listbox":
                return True
        except tk.TclError:
            pass
        try:
            top = w.winfo_toplevel()
            if top is not None and top is not self:
                return True
        except tk.TclError:
            pass
        return False

    def _fly_update_scrollregion(self) -> None:
        if hasattr(self, "_fly_canvas"):
            self._fly_canvas.configure(scrollregion=self._fly_canvas.bbox("all"))

    # ---------- Plan tab ----------
    def _build_plan(self) -> None:
        root = self.tab_plan
        root.configure(style="TFrame")

        toolbar = tk.Frame(root, bg=C_BG)
        toolbar.pack(fill=tk.X, padx=8, pady=8)

        for text, cmd in [
            ("+ Add step", self.add_step),
            ("Duplicate", self.duplicate_step),
            ("Delete", self.delete_step),
            ("↑", lambda: self.move_step(-1)),
            ("↓", lambda: self.move_step(1)),
            ("New…", self.new_mission),
            ("Load mission", self.load_mission),
            ("Save mission", self.save_mission),
            ("Save mission as…", self.save_mission_as),
            ("Reset flight cache", self.reset_flight_cache),
        ]:
            ttk.Button(toolbar, text=text, command=cmd).pack(side=tk.LEFT, padx=3)

        body = tk.Frame(root, bg=C_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        left = tk.Frame(body, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))
        ttk.Label(left, text="Flight timeline", style="Header.TLabel").pack(anchor="w", padx=12, pady=(12, 6))

        list_frame = tk.Frame(left, bg=C_PANEL)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.timeline = tk.Listbox(
            list_frame,
            bg=C_CARD,
            fg=C_TEXT,
            selectbackground=C_ACCENT,
            selectforeground="#041018",
            activestyle="none",
            font=("Consolas", 11),
            borderwidth=0,
            highlightthickness=0,
            relief=tk.FLAT,
        )
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.timeline.yview)
        self.timeline.configure(yscrollcommand=scroll.set)
        self.timeline.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.timeline.bind("<<ListboxSelect>>", self._on_select)
        self.timeline.bind("<Double-Button-1>", lambda _e: self._open_step_editor())

        # Compact summary + preview; full editing is in a modal (less Plan scrolling)
        right = tk.Frame(body, bg=C_PANEL, width=440, highlightbackground=C_BORDER, highlightthickness=1)
        right.pack(side=tk.RIGHT, fill=tk.BOTH)
        right.pack_propagate(False)

        self.var_locked = tk.BooleanVar(value=False)
        self.var_step_summary_title = tk.StringVar(value="No step selected")
        self.var_step_summary_body = tk.StringVar(
            value="Select a step, then Edit step… (or double-click the timeline)."
        )

        step_hdr = tk.Frame(right, bg=C_PANEL)
        step_hdr.pack(fill=tk.X, padx=12, pady=(10, 4))
        ttk.Label(step_hdr, text="Step", style="Header.TLabel").pack(side=tk.LEFT)
        self._chk_locked = ttk.Checkbutton(
            step_hdr,
            text="Lock",
            variable=self.var_locked,
            command=self._on_lock_toggle,
            style="Panel.TCheckbutton",
        )
        self._chk_locked.pack(side=tk.RIGHT)
        self._lock_widgets = [step_hdr, self._chk_locked]

        # Preview + actions stay on the Plan pane (pack bottom-first)
        footer = tk.Frame(right, bg=C_PANEL)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(4, 10))
        prev_btns = tk.Frame(footer, bg=C_PANEL)
        prev_btns.pack(fill=tk.X)
        ttk.Button(prev_btns, text="Regenerate text", command=self.regenerate_step_text).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(prev_btns, text="Phrase helper…", command=self.phrase_helper).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0)
        )
        ttk.Button(prev_btns, text="Hear locally", command=self.hear_preview_local).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0)
        )
        ttk.Button(prev_btns, text="TX → SRS", command=self.hear_preview_srs).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0)
        )

        preview_wrap = tk.Frame(right, bg=C_PANEL)
        preview_wrap.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))
        prev_hdr = tk.Frame(preview_wrap, bg=C_PANEL)
        prev_hdr.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(prev_hdr, text="Preview", style="Header.TLabel").pack(side=tk.LEFT)
        tk.Label(
            prev_hdr,
            text="Edit step… for settings · Regenerate re-rolls wording",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=(8, 0))
        self.preview_box = tk.Text(
            preview_wrap,
            height=12,
            bg="#0a0e14",
            fg=C_GREEN,
            font=("Consolas", 10),
            relief=tk.FLAT,
            wrap=tk.WORD,
            padx=8,
            pady=8,
        )
        self.preview_box.pack(fill=tk.BOTH, expand=True)
        self.preview_box.insert(
            tk.END,
            "Select a step to preview the radio call here.\n"
            "Edit step… to change settings · Regenerate / Hear / TX below.",
        )

        summary = tk.Frame(right, bg=C_PANEL)
        summary.pack(fill=tk.X, padx=10, pady=(4, 0))
        tk.Label(
            summary,
            textvariable=self.var_step_summary_title,
            bg=C_PANEL,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 12),
            anchor="w",
            wraplength=400,
            justify=tk.LEFT,
        ).pack(fill=tk.X)
        tk.Label(
            summary,
            textvariable=self.var_step_summary_body,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 9),
            anchor="nw",
            justify=tk.LEFT,
            wraplength=400,
        ).pack(fill=tk.X, pady=(4, 8))
        self._btn_edit_step = ttk.Button(
            summary, text="Edit step…", style="Accent.TButton", command=self._open_step_editor
        )
        self._btn_edit_step.pack(fill=tk.X)

        self._build_step_editor()
        self._last_preview_phrase = ""
        self._preview_base_phrase = ""  # last auto-filled phrase (detect Preview edits)
        self._last_preview_channel = "other"
        self._last_preview_file: str | None = None
        self._update_freq_ui()
        self._refresh_step_speed_ui()
        self._mode_ui()
        self._refresh_step_summary()

    def _build_step_editor(self) -> None:
        """Persistent Edit step modal (withdrawn until opened)."""
        dlg = tk.Toplevel(self)
        dlg.title("Edit step")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        dlg.withdraw()
        dlg.protocol("WM_DELETE_WINDOW", self._cancel_step_editor)
        self._step_edit_dlg = dlg
        self._step_edit_open = False

        hdr = tk.Frame(dlg, bg=C_BG)
        hdr.pack(fill=tk.X, padx=12, pady=(12, 4))
        ttk.Label(hdr, text="Step settings", style="Header.TLabel").pack(side=tk.LEFT)
        tk.Label(
            hdr,
            text="Save writes to the mission · Cancel discards edits",
            bg=C_BG,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.RIGHT)

        foot = tk.Frame(dlg, bg=C_BG)
        foot.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(6, 12))
        self._btn_apply_step = ttk.Button(
            foot, text="Save", style="Accent.TButton", command=self._save_step_editor
        )
        self._btn_apply_step.pack(side=tk.RIGHT)
        ttk.Button(foot, text="Cancel", command=self._cancel_step_editor).pack(
            side=tk.RIGHT, padx=(0, 8)
        )

        scroll_host = tk.Frame(dlg, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        scroll_host.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))
        self._step_edit_canvas = tk.Canvas(scroll_host, bg=C_PANEL, highlightthickness=0, bd=0)
        edit_vsb = ttk.Scrollbar(scroll_host, orient=tk.VERTICAL, command=self._step_edit_canvas.yview)
        self._step_edit_canvas.configure(yscrollcommand=edit_vsb.set)
        edit_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._step_edit_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        form = tk.Frame(self._step_edit_canvas, bg=C_PANEL)
        self._step_edit_form_window = self._step_edit_canvas.create_window((0, 0), window=form, anchor="nw")
        self._step_form = form

        def _edit_form_cfg(_event: tk.Event | None = None) -> None:
            self._step_edit_canvas.configure(scrollregion=self._step_edit_canvas.bbox("all"))

        def _edit_canvas_cfg(event: tk.Event) -> None:
            self._step_edit_canvas.itemconfigure(self._step_edit_form_window, width=event.width)

        form.bind("<Configure>", _edit_form_cfg)
        self._step_edit_canvas.bind("<Configure>", _edit_canvas_cfg)

        def _edit_wheel(event: tk.Event) -> str | None:
            if not self._step_edit_open:
                return None
            if self._wheel_owned_elsewhere(event):
                return None
            delta = int(-1 * (event.delta / 120)) if event.delta else 0
            if delta:
                self._step_edit_canvas.yview_scroll(delta, "units")
            return "break"

        self._step_edit_canvas.bind(
            "<Enter>", lambda _e: self._step_edit_canvas.bind_all("<MouseWheel>", _edit_wheel)
        )
        self._step_edit_canvas.bind(
            "<Leave>", lambda _e: self._step_edit_canvas.unbind_all("<MouseWheel>")
        )

        self.var_label = tk.StringVar()
        self.var_channel = tk.StringVar()
        self.var_mode = tk.StringVar(value="tts")
        self.var_template = tk.StringVar()
        self.var_file = tk.StringVar()
        self.var_enabled = tk.BooleanVar(value=True)
        self.var_step_c2 = tk.BooleanVar(value=False)
        self.var_step_hold = tk.BooleanVar(value=False)
        self.var_freq_hint = tk.StringVar(value="")
        self.var_freq_mhz = tk.StringVar()
        self.var_step_voice = tk.StringVar()
        self.var_step_voice_display = tk.StringVar(value="(agency default)")
        self.var_step_runway = tk.StringVar()
        self.var_step_recovery = tk.StringVar(
            value=atc_phrase.recovery_label(atc_phrase.DEFAULT_RECOVERY)
        )
        self.var_step_speed_custom = tk.BooleanVar(value=False)
        self.var_step_speed = tk.DoubleVar(value=7)
        self.var_step_speed_lbl = tk.StringVar(value="(global)")

        def row(r: int, label: str, widget: tk.Widget, *, pady: int = 4) -> None:
            tk.Label(form, text=label, bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9)).grid(
                row=r, column=0, sticky="nw", pady=pady, padx=(10, 6)
            )
            widget.grid(row=r, column=1, sticky="we", pady=pady, padx=(0, 10))

        form.columnconfigure(1, weight=1)
        row(0, "Label", ttk.Entry(form, textvariable=self.var_label))

        ch_fr = tk.Frame(form, bg=C_PANEL)
        self._channel_buttons: dict[str, tk.Button] = {}
        for i, agency in enumerate(atc_phrase.CHANNELS):
            btn = tk.Button(
                ch_fr,
                text=agency,
                font=("Segoe UI", 9),
                relief=tk.FLAT,
                bd=0,
                padx=6,
                pady=3,
                cursor="hand2",
                command=lambda a=agency: self._set_channel(a),
            )
            btn.grid(row=i // 4, column=i % 4, padx=2, pady=2, sticky="we")
            self._channel_buttons[agency] = btn
        for c in range(4):
            ch_fr.columnconfigure(c, weight=1)
        row(1, "Channel", ch_fr)
        self._paint_channel_buttons()

        self.var_mission_phase = tk.StringVar(value="departure")
        phase_fr = tk.Frame(form, bg=C_PANEL)
        self._mission_phase_buttons: dict[str, tk.Button] = {}
        for i, key in enumerate(voice_intent.MISSION_PHASES):
            label = voice_intent.MISSION_PHASE_LABELS.get(key, key)
            btn = tk.Button(
                phase_fr,
                text=label,
                font=("Segoe UI", 9),
                relief=tk.FLAT,
                bd=0,
                padx=8,
                pady=3,
                cursor="hand2",
                command=lambda p=key: self._set_mission_phase(p),
            )
            btn.grid(row=0, column=i, padx=2, pady=2, sticky="we")
            self._mission_phase_buttons[key] = btn
            phase_fr.columnconfigure(i, weight=1)
        row(2, "Mission phase", phase_fr)
        self._paint_mission_phase_buttons()

        self.freq_fr = tk.Frame(form, bg=C_PANEL)
        self.freq_hint_lbl = tk.Label(
            self.freq_fr, textvariable=self.var_freq_hint, bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 9)
        )
        self.freq_edit_fr = tk.Frame(self.freq_fr, bg=C_PANEL)
        ttk.Entry(self.freq_edit_fr, textvariable=self.var_freq_mhz, width=10).pack(side=tk.LEFT)
        tk.Label(
            self.freq_edit_fr,
            text="MHz (this Other step)",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=(6, 0))
        row(3, "Freq", self.freq_fr)

        voice_fr = tk.Frame(form, bg=C_PANEL)
        tk.Label(
            voice_fr,
            textvariable=self.var_step_voice_display,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Segoe UI", 9),
            anchor="w",
            padx=8,
            pady=4,
            highlightbackground=C_BORDER,
            highlightthickness=1,
        ).pack(fill=tk.X)
        voice_btns = tk.Frame(voice_fr, bg=C_PANEL)
        voice_btns.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(voice_btns, text="Choose voice", command=self._choose_step_voice).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(voice_btns, text="Use agency default", command=self._clear_step_voice).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0)
        )
        row(4, "Voice", voice_fr)

        speed_fr = tk.Frame(form, bg=C_PANEL)
        spd_top = tk.Frame(speed_fr, bg=C_PANEL)
        spd_top.pack(fill=tk.X)
        self._chk_step_speed = ttk.Checkbutton(
            spd_top,
            text="Custom speed",
            variable=self.var_step_speed_custom,
            command=self._on_step_speed_custom_toggle,
            style="Panel.TCheckbutton",
        )
        self._chk_step_speed.pack(side=tk.LEFT)
        tk.Label(
            spd_top,
            textvariable=self.var_step_speed_lbl,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=(10, 0))
        self._step_speed_scale = ttk.Scale(
            speed_fr,
            from_=-5,
            to=10,
            variable=self.var_step_speed,
            orient=tk.HORIZONTAL,
            command=self._on_step_speed_slide,
        )
        self._step_speed_scale.pack(fill=tk.X, pady=(4, 0))
        tk.Label(
            speed_fr,
            text="Off = Setup default talk speed",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(2, 0))
        row(5, "Speed", speed_fr)

        self._row_runway_lbl = tk.Label(form, text="Runway", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9))
        rwy_fr = tk.Frame(form, bg=C_PANEL)
        self._runway_choices: list[str] = [""]
        self._lbl_step_runway = tk.Label(
            rwy_fr,
            textvariable=self.var_step_runway,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Segoe UI", 10),
            anchor="w",
            padx=8,
            pady=4,
            width=12,
            highlightbackground=C_BORDER,
            highlightthickness=1,
        )
        self._lbl_step_runway.pack(side=tk.LEFT)
        self._btn_step_runway = ttk.Button(rwy_fr, text="Choose…", command=self._choose_step_runway)
        self._btn_step_runway.pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(
            rwy_fr,
            text="Blank = Setup / flight plan / wind",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._rwy_fr = rwy_fr
        self._row_runway_lbl.grid(row=6, column=0, sticky="nw", pady=4, padx=(10, 6))
        rwy_fr.grid(row=6, column=1, sticky="we", pady=4, padx=(0, 10))

        self._row_recovery_lbl = tk.Label(form, text="Recovery", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9))
        rec_fr = tk.Frame(form, bg=C_PANEL)
        self._recovery_by_label = {lab: key for key, lab in atc_phrase.RECOVERY_CHOICES}
        self._label_by_recovery = {key: lab for key, lab in atc_phrase.RECOVERY_CHOICES}
        tk.Label(
            rec_fr,
            textvariable=self.var_step_recovery,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Segoe UI", 10),
            anchor="w",
            padx=8,
            pady=4,
            highlightbackground=C_BORDER,
            highlightthickness=1,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._btn_step_recovery = ttk.Button(rec_fr, text="Choose…", command=self._choose_step_recovery)
        self._btn_step_recovery.pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(
            rec_fr,
            text="Plan default — Fly can change mid-sortie",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._rec_fr = rec_fr
        self._row_recovery_lbl.grid(row=13, column=0, sticky="nw", pady=4, padx=(10, 6))
        rec_fr.grid(row=13, column=1, sticky="we", pady=4, padx=(0, 10))
        self._row_recovery_lbl.grid_remove()
        rec_fr.grid_remove()

        mode_fr = tk.Frame(form, bg=C_PANEL)
        ttk.Radiobutton(
            mode_fr,
            text="Template",
            variable=self.var_mode,
            value="tts",
            command=self._on_mode_change,
            style="Panel.TRadiobutton",
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_fr,
            text="Custom",
            variable=self.var_mode,
            value="custom",
            command=self._on_mode_change,
            style="Panel.TRadiobutton",
        ).pack(side=tk.LEFT, padx=8)
        ttk.Radiobutton(
            mode_fr,
            text="File",
            variable=self.var_mode,
            value="file",
            command=self._on_mode_change,
            style="Panel.TRadiobutton",
        ).pack(side=tk.LEFT)
        row(7, "Action", mode_fr)

        tmpl_labels = [lab for _, lab in atc_phrase.TEMPLATE_CHOICES]
        self._tmpl_by_label = {lab: key for key, lab in atc_phrase.TEMPLATE_CHOICES}
        self._label_by_tmpl = {key: lab for key, lab in atc_phrase.TEMPLATE_CHOICES}

        self._row_template_lbl = tk.Label(form, text="Template", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9))
        self._tmpl_labels = list(tmpl_labels)
        tmpl_fr = tk.Frame(form, bg=C_PANEL)
        self._lbl_template = tk.Label(
            tmpl_fr,
            textvariable=self.var_template,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Segoe UI", 10),
            anchor="w",
            padx=8,
            pady=4,
            highlightbackground=C_BORDER,
            highlightthickness=1,
        )
        self._lbl_template.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._btn_template = ttk.Button(tmpl_fr, text="Choose…", command=self._choose_step_template)
        self._btn_template.pack(side=tk.LEFT, padx=(6, 0))
        self._tmpl_fr = tmpl_fr
        self._row_template_lbl.grid(row=8, column=0, sticky="nw", pady=4, padx=(10, 6))
        tmpl_fr.grid(row=8, column=1, sticky="we", pady=4, padx=(0, 10))

        self._row_custom_lbl = tk.Label(form, text="Custom", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9))
        self.txt_custom = tk.Text(
            form,
            height=3,
            bg=C_CARD,
            fg=C_TEXT,
            insertbackground=C_TEXT,
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self._row_custom_hint = tk.Label(
            form,
            text="{callsign} {runway} {altimeter} {wind} {alpha_bullseye}",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        )
        self._row_custom_lbl.grid(row=9, column=0, sticky="nw", pady=4, padx=(10, 6))
        self.txt_custom.grid(row=9, column=1, sticky="we", pady=4, padx=(0, 10))
        self._row_custom_hint.grid(row=10, column=1, sticky="w", padx=(0, 10))

        self._row_file_lbl = tk.Label(form, text="Audio file", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9))
        file_fr = tk.Frame(form, bg=C_PANEL)
        ttk.Entry(file_fr, textvariable=self.var_file).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(file_fr, text="Browse…", command=self._browse_file).pack(side=tk.LEFT, padx=4)
        self._row_file_lbl.grid(row=11, column=0, sticky="nw", pady=4, padx=(10, 6))
        file_fr.grid(row=11, column=1, sticky="we", pady=4, padx=(0, 10))
        self._file_fr = file_fr

        self.var_enabled = tk.BooleanVar(value=True)
        step_flags = tk.Frame(form, bg=C_PANEL)
        ttk.Checkbutton(
            step_flags,
            text="Include in flight (enabled)",
            variable=self.var_enabled,
            style="Panel.TCheckbutton",
        ).pack(anchor="w")
        ttk.Checkbutton(
            step_flags,
            text="C2 services — picture, bogey dope, declare",
            variable=self.var_step_c2,
            style="Panel.TCheckbutton",
            command=self._refresh_step_summary,
        ).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(
            step_flags,
            text="Stay after play — transmit, leave cursor here",
            variable=self.var_step_hold,
            style="Panel.TCheckbutton",
            command=self._refresh_step_summary,
        ).pack(anchor="w")
        tk.Label(
            step_flags,
            text="C2 is Blackjack / Bandsaw-style control — off for Center and transit. "
            "Stay: Play does not advance; use Seek or the Say to advance phrases.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=360,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))
        tk.Label(form, text="Step", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9)).grid(
            row=12, column=0, sticky="nw", pady=4, padx=(10, 6)
        )
        step_flags.grid(row=12, column=1, sticky="we", pady=4, padx=(0, 10))
        self._step_flags_fr = step_flags

        self._step_phrases_draft: list[str] = []
        self.var_keywords_summary = tk.StringVar(value="")
        self._row_phrases_lbl = tk.Label(
            form, text="Keywords", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9)
        )
        phrases_fr = tk.Frame(form, bg=C_PANEL)
        ttk.Button(
            phrases_fr, text="Keywords…", command=self._open_step_keywords
        ).pack(side=tk.LEFT)
        tk.Label(
            phrases_fr,
            textvariable=self.var_keywords_summary,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            anchor="w",
            wraplength=320,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        self._row_phrases_hint = tk.Label(
            form,
            text="Built-in advance cues (read-only) plus mission phrases that fire this step.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        )
        self._row_phrases_lbl.grid(row=14, column=0, sticky="nw", pady=4, padx=(10, 6))
        phrases_fr.grid(row=14, column=1, sticky="we", pady=4, padx=(0, 10))
        self._row_phrases_hint.grid(row=15, column=1, sticky="w", padx=(0, 10), pady=(0, 6))
        self._phrases_fr = phrases_fr

        self._row_cues_lbl = tk.Label(
            form, text="Say to advance", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9)
        )
        self.txt_cues = tk.Text(
            form,
            height=3,
            bg=C_CARD,
            fg=C_TEXT,
            insertbackground=C_TEXT,
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self._row_cues_hint = tk.Label(
            form,
            text="Shown on Fly under TO ADVANCE. One phrase per line — any one fires this step.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=360,
            justify=tk.LEFT,
        )
        self._row_cues_lbl.grid_remove()
        self.txt_cues.grid_remove()
        self._row_cues_hint.grid_remove()

        self._build_trigger_group(form, row=16)
        dlg.bind("<Escape>", lambda _e: self._cancel_step_editor())

    def _step_edit_parent(self) -> tk.Misc:
        """Prefer the Edit step modal as parent for nested pickers while it is open."""
        dlg = getattr(self, "_step_edit_dlg", None)
        if dlg is not None and getattr(self, "_step_edit_open", False):
            try:
                if int(dlg.winfo_exists()):
                    return dlg
            except tk.TclError:
                pass
        return self

    def _refresh_step_edit_scroll(self) -> None:
        canvas = getattr(self, "_step_edit_canvas", None)
        if canvas is None:
            return
        self.after(30, lambda: canvas.configure(scrollregion=canvas.bbox("all")))

    def _refresh_step_summary(self) -> None:
        """Compact Plan-pane summary for the selected step."""
        if not hasattr(self, "var_step_summary_title"):
            return
        if self.selected_index is None or self.selected_index >= len(self._steps()):
            self.var_step_summary_title.set("No step selected")
            self.var_step_summary_body.set(
                "Select a step, then Edit step… (or double-click the timeline)."
            )
            return

        label = (self.var_label.get() or "Step").strip() or "Step"
        ch = (self.var_channel.get() or "other").strip()
        phase = ""
        if hasattr(self, "var_mission_phase"):
            phase_key = (self.var_mission_phase.get() or "").strip().lower()
            phase = voice_intent.MISSION_PHASE_LABELS.get(phase_key, phase_key)
        mode = self.var_mode.get()
        if mode == "file":
            action = f"File · {Path(self.var_file.get() or '').name or '(none)'}"
        elif mode == "custom":
            action = "Custom text"
        else:
            action = f"Template · {self.var_template.get() or '—'}"

        lines = [f"{ch.title()} · {phase}" if phase else ch.title(), action]

        freq = (self.var_freq_hint.get() or "").strip()
        if ch.lower() == "other":
            mhz = (self.var_freq_mhz.get() or "").strip()
            lines.append(f"Freq · {mhz} MHz" if mhz else "Freq · (set MHz)")
        elif freq:
            lines.append(f"Freq · {freq}")

        voice = (self.var_step_voice_display.get() or "").strip()
        speed = (self.var_step_speed_lbl.get() or "").strip()
        if voice or speed:
            bits = [b for b in (voice, f"speed {speed}" if speed else "") if b]
            lines.append(" · ".join(bits))

        if self._step_uses_runway():
            rwy = (self.var_step_runway.get() or "").strip() or "default"
            lines.append(f"Runway · {rwy}")

        lab = self.var_template.get()
        tmpl = self._tmpl_by_label.get(lab, "")
        if tmpl == "approach_check_in" and mode == "tts":
            lines.append(f"Recovery · {self.var_step_recovery.get() or '—'}")

        enabled = "Enabled" if self.var_enabled.get() else "Disabled"
        bits = [enabled]
        if bool(self.var_step_c2.get()) if hasattr(self, "var_step_c2") else False:
            bits.append("C2")
        if bool(self.var_step_hold.get()) if hasattr(self, "var_step_hold") else False:
            bits.append("stay after play")
        trig = (self.var_trig_zone_lbl.get() or "").strip() if hasattr(self, "var_trig_zone_lbl") else ""
        if trig and trig != TRIG_NONE:
            bits.append(f"fires: {trig}")
        lines.append(" · ".join(bits))

        if self.var_locked.get():
            label = f"{label}  (locked)"

        self.var_step_summary_title.set(label)
        self.var_step_summary_body.set("\n".join(lines))

    def _open_step_editor(self) -> None:
        if self.selected_index is None:
            messagebox.showinfo("Step", "Select a step in the timeline first.")
            return
        dlg = getattr(self, "_step_edit_dlg", None)
        if dlg is None:
            return
        self._place_dialog(dlg, 540, 680)
        self._step_edit_open = True
        dlg.deiconify()
        dlg.lift()
        try:
            dlg.grab_set()
        except tk.TclError:
            pass
        self._refresh_step_edit_scroll()
        dlg.focus_set()

    def _close_step_editor(self) -> None:
        dlg = getattr(self, "_step_edit_dlg", None)
        self._step_edit_open = False
        if dlg is None:
            return
        try:
            dlg.grab_release()
        except tk.TclError:
            pass
        dlg.withdraw()

    def _save_step_editor(self) -> None:
        if not self.apply_step():
            return
        self.after(40, lambda: self.preview_step(apply=False))
        if hasattr(self, "_refresh_voice_prompts"):
            self._refresh_voice_prompts()
        self._close_step_editor()

    def _cancel_step_editor(self) -> None:
        """Discard modal edits by reloading the selected step into the form."""
        if self.selected_index is not None:
            # Reload vars from the saved step (ignore in-modal edits)
            sel = self.timeline.curselection()
            if not sel:
                self.timeline.selection_set(self.selected_index)
            self._on_select()
        self._close_step_editor()

    # --- zone trigger editor --------------------------------------------

    def _build_trigger_group(self, form: tk.Frame, *, row: int) -> None:
        """
        "Fires automatically when" — the step names a drawn area and waits for the
        flight to be in it, instead of waiting to be asked.
        """
        self.var_trig_zone = tk.StringVar(value="")  # zone id or trigger tag
        self.var_trig_zone_lbl = tk.StringVar(value=TRIG_NONE)
        self.var_trig_when = tk.StringVar(value="inside")
        self.var_trig_flight = tk.StringVar(value="")
        self.var_trig_settled = tk.BooleanVar(value=True)
        self.var_trig_dwell = tk.StringVar(value="")
        self.var_trig_gap = tk.StringVar(value="")

        tk.Label(
            form, text="Fires when", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9)
        ).grid(row=row, column=0, sticky="nw", pady=(10, 4), padx=(10, 6))

        box = tk.Frame(form, bg=C_PANEL)
        box.grid(row=row, column=1, sticky="we", pady=(10, 4), padx=(0, 10))
        self._trig_fr = box

        zone_fr = tk.Frame(box, bg=C_PANEL)
        zone_fr.pack(fill=tk.X)
        tk.Label(
            zone_fr,
            textvariable=self.var_trig_zone_lbl,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Segoe UI", 10),
            anchor="w",
            padx=8,
            pady=4,
            highlightbackground=C_BORDER,
            highlightthickness=1,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(zone_fr, text="Choose…", command=self._choose_trigger_zone).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(zone_fr, text="Draw one…", command=self._open_zone_editor).pack(
            side=tk.LEFT, padx=(6, 0)
        )

        opts = tk.Frame(box, bg=C_PANEL)
        opts.pack(fill=tk.X, pady=(4, 0))
        for text, var, val in (
            ("inside it", self.var_trig_when, "inside"),
            ("leaving it", self.var_trig_when, "leaving"),
        ):
            ttk.Radiobutton(
                opts, text=text, variable=var, value=val, style="Panel.TRadiobutton"
            ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(
            opts,
            text="settled",
            variable=self.var_trig_settled,
            style="Panel.TCheckbutton",
        ).pack(side=tk.LEFT)

        who = tk.Frame(box, bg=C_PANEL)
        who.pack(fill=tk.X, pady=(2, 0))
        for text, val in (
            ("Setup default", ""),
            ("whole flight", "all"),
            ("just me", "me"),
        ):
            ttk.Radiobutton(
                who,
                text=text,
                variable=self.var_trig_flight,
                value=val,
                style="Panel.TRadiobutton",
            ).pack(side=tk.LEFT, padx=(0, 8))

        timers = tk.Frame(box, bg=C_PANEL)
        timers.pack(fill=tk.X, pady=(4, 0))
        default_dwell = f"{runway_position.rule(self.config_data, 'auto_clearance_dwell_s'):g}"
        for label, var, hint in (
            ("Hold for", self.var_trig_dwell, f"s  (blank = {default_dwell})"),
            ("Radio gap", self.var_trig_gap, "s since the last call"),
        ):
            tk.Label(timers, text=label, bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 8)).pack(
                side=tk.LEFT
            )
            ttk.Entry(timers, textvariable=var, width=5).pack(side=tk.LEFT, padx=(4, 2))
            tk.Label(timers, text=hint, bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 8)).pack(
                side=tk.LEFT, padx=(0, 10)
            )

        self._trig_hint = tk.Label(
            form,
            text=(
                "The eor tag uses the assigned EOR from the taxi clearance "
                "(NW EOR on 21R, Alpha South on 03L). A specific id pins one "
                "box only. Settled means stopped, on the deck and lined up — "
                "turn it off for an airborne area. Hold for is how long that "
                "has to stay true; radio gap keeps the call off the back of "
                "the previous one."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=480,
            justify=tk.LEFT,
        )
        self._trig_hint.grid(row=row + 1, column=1, sticky="w", padx=(0, 10), pady=(2, 8))

    def _zone_choices(self) -> list[tuple[str, str]]:
        """(display, value) for the zone picker. Value is a zone id or a trigger tag."""
        airport = self._airport()
        drawn = runway_position.zones(airport)
        out: list[tuple[str, str]] = [(TRIG_NONE, "")]

        tags: dict[str, int] = {}
        scoped: set[str] = set()
        for zone in drawn:
            tag = str(zone.get("trigger") or "").strip()
            if not tag or tag == "runway":
                continue
            tags[tag] = tags.get(tag, 0) + 1
            if zone.get("runway"):
                scoped.add(tag)
        # Tags first: one choice follows the active runway across every drawn area.
        for tag in list(ZONE_TAGS) + [t for t in tags if t not in ZONE_TAGS]:
            count = tags.get(tag, 0)
            if not count:
                note = "nothing drawn for this field yet"
            elif tag in scoped:
                if tag == "eor":
                    note = f"{count} drawn, assigned EOR for the active runway"
                else:
                    note = f"{count} drawn, follows active runway (all matching areas)"
            else:
                note = f"{count} drawn"
            out.append((f"any {tag} area — {note}", tag))
        # Specific ids last — they pin one end and ignore the other runway.
        for zone in drawn:
            zid = str(zone.get("id") or "").strip()
            if not zid or str(zone.get("trigger") or "") == "runway":
                continue
            bits = [str(zone.get("trigger") or ""), str(zone.get("runway") or "")]
            detail = ", ".join(b for b in bits if b)
            pinned = "  ·  pins this area only" if zone.get("runway") else ""
            out.append(
                (f"{runway_position.zone_label(zone)}  ({detail}){pinned}", zid)
            )
        return out

    def _choose_trigger_zone(self) -> None:
        if self._step_is_locked():
            messagebox.showinfo("Locked", "Unlock this step before editing.")
            return
        pairs = self._zone_choices()
        current = self.var_trig_zone.get().strip()
        display = [d for d, _ in pairs]
        cur_display = next((d for d, v in pairs if v == current), display[0])
        picked = self._pick_list_value(
            title="Trigger zone",
            heading=(
                "Fire this step when the flight is in…\n"
                "Use “any … area” so the active runway picks the right boxes."
            ),
            choices=display,
            current=cur_display,
            parent=self._step_edit_parent(),
        )
        if picked is None:
            return
        value = next((v for d, v in pairs if d == picked), "")
        self.var_trig_zone.set(value)
        self.var_trig_zone_lbl.set(picked)

    def _zone_editor_port(self) -> int:
        try:
            return int(self.config_data.get("zone_editor_port") or ZONE_EDITOR_PORT)
        except (TypeError, ValueError):
            return ZONE_EDITOR_PORT

    @staticmethod
    def _serving(port: int) -> bool:
        with socket.socket() as sock:
            sock.settimeout(0.25)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    @staticmethod
    def _python_exe() -> str:
        """The interpreter to run the editor with, windowless where that exists."""
        exe = sys.executable or ""
        if not exe:
            return "py"
        windowless = Path(exe).with_name("pythonw.exe")
        if os.name == "nt" and windowless.exists():
            return str(windowless)
        return exe

    @staticmethod
    def _zone_editor_supports_overlays(port: int) -> bool:
        """False when an old zone_server is up (Packs dropdown stays empty)."""
        try:
            import urllib.error
            import urllib.request

            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/overlays")
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                return int(getattr(resp, "status", 200) or 200) == 200
        except (OSError, TimeoutError):
            return False
        except Exception:  # noqa: BLE001 — urllib raises URLError/HTTPError variants
            return False

    def _open_zone_editor(self) -> None:
        """Open the map drawing tool in a browser, reusing a server already up."""
        port = self._zone_editor_port()
        url = f"http://127.0.0.1:{port}/?airport={self._airport_key()}"
        if self._serving(port) and self._zone_editor_supports_overlays(port):
            webbrowser.open(url)
            return
        if not ZONE_TOOL.exists():
            messagebox.showerror(
                "Zone editor",
                "The zone editor is missing. It lives next to this app in:\n\n"
                f"{ZONE_TOOL}",
            )
            return
        try:
            log = ZONE_EDITOR_LOG.open("w", encoding="utf-8", errors="replace")
            # --replace clears a stale editor left on the port after updates.
            self._zone_proc = subprocess.Popen(
                [
                    self._python_exe(),
                    str(ZONE_TOOL),
                    "--port",
                    str(port),
                    "--replace",
                    "--no-browser",
                ],
                cwd=str(ZONE_TOOL.parent),
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            messagebox.showerror(
                "Zone editor",
                f"Could not start the zone editor:\n{exc}\n\n"
                "It needs Python, the same as this app.",
            )
            return
        self._wait_for_zone_editor(url)

    def _wait_for_zone_editor(self, url: str, tries: int = 24) -> None:
        """Give the server a moment to bind before pointing a browser at it."""
        proc = self._zone_proc
        if proc is None:
            return
        if self._serving(self._zone_editor_port()):
            webbrowser.open(url)
            messagebox.showinfo(
                "Zone editor",
                "The zone editor is open in your browser.\n\n"
                "Pick the field, trace the area on the map, name what it fires, "
                "then press Save. This app picks the new area up on its own — "
                "it will be in the zone list a few seconds later.",
            )
            return
        if proc.poll() is not None or tries <= 0:
            self._zone_proc = None
            detail = ""
            try:
                detail = ZONE_EDITOR_LOG.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
            messagebox.showerror(
                "Zone editor",
                "The zone editor did not come up."
                + (f"\n\n{detail[-600:]}" if detail else f"\n\nLog: {ZONE_EDITOR_LOG}"),
            )
            return
        self.after(250, lambda: self._wait_for_zone_editor(url, tries - 1))

    def _trigger_from_form(self) -> dict[str, Any] | None:
        """The step's `trigger` block, or None when no zone is chosen."""
        zone = self.var_trig_zone.get().strip()
        if not zone:
            return None
        out: dict[str, Any] = {"zone": zone}
        if self.var_trig_when.get() == "leaving":
            out["when"] = "leaving"
        flight = self.var_trig_flight.get().strip()
        if flight in ("all", "me"):
            out["flight"] = flight
        if not self.var_trig_settled.get():
            out["settled"] = False
        for key, var in (("dwell_s", self.var_trig_dwell), ("gap_s", self.var_trig_gap)):
            raw = var.get().strip()
            if not raw:
                continue
            try:
                val = float(raw)
            except ValueError:
                continue
            if val > 0:
                out[key] = int(val) if val == int(val) else round(val, 1)
        return out

    def _trigger_to_form(self, step: dict[str, Any]) -> None:
        trig = step.get("trigger")
        trig = trig if isinstance(trig, dict) else {}
        zone = str(trig.get("zone") or "").strip()
        self.var_trig_zone.set(zone)
        self.var_trig_zone_lbl.set(self._trigger_zone_label(zone))
        when = str(trig.get("when") or "").casefold()
        self.var_trig_when.set("leaving" if when.startswith("leav") else "inside")
        flight = str(trig.get("flight") or "").casefold()
        self.var_trig_flight.set(flight if flight in ("all", "me") else "")
        self.var_trig_settled.set(bool(trig.get("settled", True)))
        for key, var in (("dwell_s", self.var_trig_dwell), ("gap_s", self.var_trig_gap)):
            val = trig.get(key)
            var.set("" if val in (None, "") else f"{float(val):g}")

    @staticmethod
    def _airports_mtime_now() -> float:
        try:
            return AIRPORTS_PATH.stat().st_mtime
        except OSError:
            return 0.0

    def _schedule_airports_poll(self) -> None:
        self.after(3000, self._airports_poll)

    def _airports_poll(self) -> None:
        """Pick up zones the editor wrote while we were open.

        Without this the drawn area is invisible until a restart, and worse, the
        next Setup save would write our older copy of airports.json over it.
        """
        try:
            mtime = self._airports_mtime_now()
            if mtime and mtime != self._airports_mtime:
                self._airports_mtime = mtime
                data = load_json(AIRPORTS_PATH)
                if isinstance(data, dict) and data:
                    self.airports = data
                    self.engine.airports = data
                    if hasattr(self, "var_trig_zone_lbl"):
                        zone = self.var_trig_zone.get().strip()
                        self.var_trig_zone_lbl.set(self._trigger_zone_label(zone))
                    self._update_zone_hint()
        except Exception:  # noqa: BLE001 — a half-written file just means try again
            pass
        finally:
            self._schedule_airports_poll()

    def _update_zone_hint(self) -> None:
        """How many areas this field has, shown under the Setup button."""
        if not hasattr(self, "var_zone_count"):
            return
        drawn = [
            z
            for z in runway_position.zones(self._airport())
            if str(z.get("trigger") or "") != "runway"
        ]
        name = str(self._airport().get("name") or self._airport_key())
        if not drawn:
            self.var_zone_count.set(f"{name} has no areas drawn yet.")
            return
        tags = sorted({str(z.get("trigger") or "?") for z in drawn})
        self.var_zone_count.set(
            f"{name} has {len(drawn)} area(s) drawn: {', '.join(tags)}."
        )

    def _trigger_zone_label(self, zone: str) -> str:
        if not zone:
            return TRIG_NONE
        for display, value in self._zone_choices():
            if value == zone:
                return display
        return f"{zone}  (not drawn for this field)"

    def _step_is_locked(self, step: dict[str, Any] | None = None) -> bool:
        if step is None:
            if self.selected_index is None:
                return False
            step = self._steps()[self.selected_index]
        return bool(step.get("locked"))

    def _on_lock_toggle(self) -> None:
        """Persist lock immediately so unlock works even when the editor is disabled."""
        if getattr(self, "_loading", False) or self.selected_index is None:
            return
        step = self._steps()[self.selected_index]
        step["locked"] = bool(self.var_locked.get())
        self._apply_lock_ui()
        self.refresh_timeline()
        if self.selected_index is not None:
            self.timeline.selection_set(self.selected_index)
        self._refresh_step_summary()

    def _apply_lock_ui(self) -> None:
        """Disable step editors when locked; lock checkbox stays usable."""
        locked = bool(self.var_locked.get())
        skip = set(self._lock_widgets)

        def _set_state(widget: tk.Widget) -> None:
            if widget in skip:
                return
            try:
                cls = widget.winfo_class()
            except tk.TclError:
                return
            try:
                if cls in ("TEntry", "TCombobox", "TButton", "TCheckbutton", "TRadiobutton", "TScale"):
                    widget.configure(state=("disabled" if locked else "normal"))
                elif cls in ("Entry", "Text", "Button", "Listbox", "Radiobutton"):
                    widget.configure(state=(tk.DISABLED if locked else tk.NORMAL))
            except tk.TclError:
                pass
            for child in widget.winfo_children():
                _set_state(child)

        if hasattr(self, "_step_form"):
            _set_state(self._step_form)
        for btn in getattr(self, "_channel_buttons", {}).values():
            try:
                btn.configure(state=(tk.DISABLED if locked else tk.NORMAL))
            except tk.TclError:
                pass
        if hasattr(self, "preview_box"):
            try:
                self.preview_box.configure(state=(tk.DISABLED if locked else tk.NORMAL))
            except tk.TclError:
                pass
        if getattr(self, "_btn_apply_step", None) is not None:
            try:
                self._btn_apply_step.configure(state=("disabled" if locked else "normal"))
            except tk.TclError:
                pass
        if getattr(self, "_btn_edit_step", None) is not None:
            try:
                # Still openable when locked (view-only); fields are disabled inside.
                self._btn_edit_step.configure(state="normal")
            except tk.TclError:
                pass
        # Restore template/custom field visibility and Combobox states after unlock
        if not locked:
            self._mode_ui()
            self._update_freq_ui()
            self._update_step_runway_ui()
            self._refresh_step_speed_ui()

    def _phrase_from_preview_box(self) -> str | None:
        """Editable radio phrase from the Preview box (strips Spoken/metadata footers)."""
        raw = self.preview_box.get("1.0", tk.END)
        if not raw.strip():
            return None
        for marker in (
            "\n\nSpoken / Hear / SRS:",
            "\n\nSpoken (Hear / SRS):",
            "\n\n→ ",
            "\n\n→",
        ):
            if marker in raw:
                raw = raw.split(marker, 1)[0]
                break
        phrase = raw.strip()
        if not phrase:
            return None
        if phrase.startswith("Select a step") or phrase.startswith("(preview unavailable)"):
            return None
        if phrase.startswith("[FILE]"):
            return None
        return phrase

    def _set_preview_display(self, body: str, *, base_phrase: str | None = None) -> None:
        """Write Preview box; track base phrase used to detect user edits."""
        was_locked = bool(self.var_locked.get()) if hasattr(self, "var_locked") else False
        if was_locked:
            self.preview_box.configure(state=tk.NORMAL)
        self.preview_box.delete("1.0", tk.END)
        self.preview_box.insert(tk.END, body)
        if was_locked:
            self.preview_box.configure(state=tk.DISABLED)
        if base_phrase is not None:
            self._preview_base_phrase = base_phrase
            self._last_preview_phrase = base_phrase

    def _step_uses_runway(self) -> bool:
        """True when this step's spoken phrase can include a runway."""
        mode = self.var_mode.get()
        if mode == "file":
            return False
        if mode == "custom":
            return True
        lab = self.var_template.get()
        tmpl = self._tmpl_by_label.get(lab, "")
        return tmpl in atc_phrase.TEMPLATES_USING_RUNWAY

    def _refresh_step_runway_choices(self) -> None:
        ap = self._airport()
        # Ops first, then instrument alts (21L) for explicit pilot/step requests.
        choices: list[str] = [""]
        for r in list(ap.get("runways") or []) + list(ap.get("instrument_runways") or []):
            n = atc_phrase.normalize_runway(r) or str(r).strip()
            if n and n not in choices:
                choices.append(n)
        current = self.var_step_runway.get().strip()
        if current and current not in choices:
            choices.append(current)
        self._runway_choices = choices

    def _update_step_runway_ui(self) -> None:
        show = self._step_uses_runway()
        if show:
            self._refresh_step_runway_choices()
            # Keep runway on its own row (6) — never reuse Speed's row 5.
            self._row_runway_lbl.grid(row=6, column=0, sticky="nw", pady=4, padx=(10, 6))
            self._rwy_fr.grid(row=6, column=1, sticky="we", pady=4, padx=(0, 10))
        else:
            self._row_runway_lbl.grid_remove()
            self._rwy_fr.grid_remove()
        self._update_recovery_ui()
        self._refresh_step_edit_scroll()

    def _update_recovery_ui(self) -> None:
        if not hasattr(self, "_row_recovery_lbl"):
            return
        lab = self.var_template.get()
        tmpl = self._tmpl_by_label.get(lab, "")
        show = tmpl == "approach_check_in" and self.var_mode.get() == "tts"
        if show:
            # Row 13 — do not reuse the enabled checkbox row (12).
            self._row_recovery_lbl.grid(row=13, column=0, sticky="nw", pady=4, padx=(10, 6))
            self._rec_fr.grid(row=13, column=1, sticky="we", pady=4, padx=(0, 10))
        else:
            self._row_recovery_lbl.grid_remove()
            self._rec_fr.grid_remove()

    def _on_mode_change(self) -> None:
        """Radio Template / Custom / File — Template clears locked custom text so Opus refreshes."""
        if getattr(self, "_loading", False):
            self._mode_ui()
            return
        if self._step_is_locked():
            return
        try:
            if getattr(self, "txt_cues", None) and self.txt_cues.winfo_ismapped():
                self._step_phrases_draft = self._read_cues_box()
        except tk.TclError:
            pass
        mode = self.var_mode.get()
        if mode == "tts" and self.selected_index is not None:
            step = self._steps()[self.selected_index]
            if step.get("text"):
                step["text"] = None
            self.txt_custom.configure(state=tk.NORMAL)
            self.txt_custom.delete("1.0", tk.END)
            # Fresh template preview (includes newly selected Opus FP / altitudes)
            self._preview_base_phrase = ""
            self.after(20, lambda: self.preview_step(apply=False))
        self._mode_ui()
        if mode in ("custom", "file"):
            self._fill_cues_box()

    def _mode_ui(self) -> None:
        mode = self.var_mode.get()
        # Show only the fields needed for the selected action (saves vertical space).
        # Row map is fixed — never relocate onto Action (7) / Enabled (12) / Speed (5).
        show_tmpl = mode == "tts"
        show_custom = mode == "custom"
        show_file = mode == "file"

        def _set(widget: tk.Widget, visible: bool, **grid_kw: Any) -> None:
            if visible:
                widget.grid(**grid_kw)
            else:
                widget.grid_remove()

        _set(self._row_template_lbl, show_tmpl, row=8, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self._tmpl_fr, show_tmpl, row=8, column=1, sticky="we", pady=4, padx=(0, 10))

        _set(self._row_custom_lbl, show_custom, row=9, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self.txt_custom, show_custom, row=9, column=1, sticky="we", pady=4, padx=(0, 10))
        _set(self._row_custom_hint, show_custom, row=10, column=1, sticky="w", padx=(0, 10))
        if show_custom:
            self.txt_custom.configure(state=tk.NORMAL)
        else:
            self.txt_custom.configure(state=tk.DISABLED)

        _set(self._row_file_lbl, show_file, row=11, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self._file_fr, show_file, row=11, column=1, sticky="we", pady=4, padx=(0, 10))

        show_cues = mode in ("custom", "file")
        show_keywords = mode == "tts"
        _set(self._row_cues_lbl, show_cues, row=14, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self.txt_cues, show_cues, row=14, column=1, sticky="we", pady=4, padx=(0, 10))
        _set(self._row_cues_hint, show_cues, row=15, column=1, sticky="w", padx=(0, 10), pady=(0, 6))
        _set(self._row_phrases_lbl, show_keywords, row=14, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self._phrases_fr, show_keywords, row=14, column=1, sticky="we", pady=4, padx=(0, 10))
        _set(self._row_phrases_hint, show_keywords, row=15, column=1, sticky="w", padx=(0, 10), pady=(0, 6))
        if show_cues:
            try:
                self.txt_cues.configure(state=tk.NORMAL)
            except tk.TclError:
                pass
        self._refresh_keywords_summary()

        self._update_step_runway_ui()
        self._update_recovery_ui()
        self._refresh_step_edit_scroll()

    def _steps(self) -> list:
        """Full mission timeline (single list)."""
        if not isinstance(self.mission.get("steps"), list):
            self.mission["steps"] = flow_engine.mission_steps(self.mission)
            self.mission.pop("outbound", None)
            self.mission.pop("inbound", None)
        return self.mission["steps"]

    def _airport_key(self) -> str:
        return str(
            self.mission.get("airport") or self.config_data.get("default_airport") or "nellis"
        )

    def _airport(self) -> dict:
        key = self._airport_key()
        return self.airports.get(key) or next(iter(self.airports.values()))

    def _set_channel(self, agency: str) -> None:
        if self._step_is_locked():
            return
        prev = (self.var_channel.get() or "").strip().lower()
        self.var_channel.set(agency)
        self._paint_channel_buttons()
        # Keep mission phase when the agency fits both Departure and Approach
        # (Tower/Ground); otherwise snap to the agency's default phase.
        current_phase = ""
        if hasattr(self, "var_mission_phase"):
            current_phase = (self.var_mission_phase.get() or "").strip().lower()
        if (
            agency in ("tower", "ground")
            and current_phase in voice_intent.MISSION_PHASES
            and voice_intent.channel_allowed_in_mission_phase(agency, current_phase)
        ):
            pass
        else:
            self._set_mission_phase(voice_intent.default_mission_phase_for_channel(agency))
        # Switching onto Other: seed freq from airport default if empty
        if agency == "other" and prev != "other" and not self.var_freq_mhz.get().strip():
            ap = self._airport()
            block = ap.get("other") or {}
            try:
                self.var_freq_mhz.set(f"{float(block.get('freq_mhz', 251.0)):g}")
            except (TypeError, ValueError):
                self.var_freq_mhz.set("251.0")
        self._update_freq_ui()
        self._refresh_step_voice_display()

    def _set_mission_phase(self, phase: str) -> None:
        if self._step_is_locked():
            return
        key = voice_intent.normalize_mission_phase(phase) or "departure"
        if hasattr(self, "var_mission_phase"):
            self.var_mission_phase.set(key)
        self._paint_mission_phase_buttons()

    def _paint_channel_buttons(self) -> None:
        if not hasattr(self, "_channel_buttons"):
            return
        selected = (self.var_channel.get() or "").strip().lower()
        for agency, btn in self._channel_buttons.items():
            on = agency == selected
            btn.configure(
                bg=C_ACCENT if on else C_CARD,
                fg="#061018" if on else C_TEXT,
                activebackground=C_ACCENT if on else C_BORDER,
                activeforeground="#061018" if on else C_TEXT,
            )

    def _paint_mission_phase_buttons(self) -> None:
        if not hasattr(self, "_mission_phase_buttons"):
            return
        selected = ""
        if hasattr(self, "var_mission_phase"):
            selected = (self.var_mission_phase.get() or "").strip().lower()
        for key, btn in self._mission_phase_buttons.items():
            on = key == selected
            btn.configure(
                bg=C_ACCENT if on else C_CARD,
                fg="#061018" if on else C_TEXT,
                activebackground=C_ACCENT if on else C_BORDER,
                activeforeground="#061018" if on else C_TEXT,
            )

    def _update_freq_hint(self) -> None:
        """Compatibility wrapper used after Setup save."""
        self._update_freq_ui()

    def _update_freq_ui(self) -> None:
        if not hasattr(self, "freq_fr"):
            return
        ap = self._airport()
        ch = (self.var_channel.get() or "other").strip().lower()
        block = ap.get(ch) or {}
        try:
            airport_mhz = float(block.get("freq_mhz", 0))
            mod = block.get("mod", "AM")
            self.var_freq_hint.set(f"{airport_mhz:g} {mod}  (from Setup)")
        except (TypeError, ValueError):
            self.var_freq_hint.set("(set freq in Setup → Airport)")
            airport_mhz = None

        self.freq_hint_lbl.pack_forget()
        self.freq_edit_fr.pack_forget()
        if ch == "other":
            if not self.var_freq_mhz.get().strip() and airport_mhz is not None and not self._loading:
                self.var_freq_mhz.set(f"{airport_mhz:g}")
            self.freq_edit_fr.pack(fill=tk.X)
        else:
            self.freq_hint_lbl.pack(anchor="w")
        self._paint_channel_buttons()

    def _refresh_step_voice_display(self) -> None:
        override = self.var_step_voice.get().strip()
        if override:
            self.var_step_voice_display.set(self._short_voice(override))
            return
        ch = self.var_channel.get() or "other"
        agency, _ = atc_phrase.voice_for_channel(self.config_data, ch)
        self.var_step_voice_display.set(f"(agency: {self._short_voice(agency)})")

    def _clear_step_voice(self) -> None:
        self.var_step_voice.set("")
        self._refresh_step_voice_display()

    def _global_talk_speed(self) -> int:
        if hasattr(self, "var_speed"):
            return atc_phrase.tts_speed(speed=self.var_speed.get())
        return atc_phrase.tts_speed(self.config_data)

    def _refresh_step_speed_ui(self) -> None:
        global_spd = self._global_talk_speed()
        custom = bool(self.var_step_speed_custom.get())
        if custom:
            n = atc_phrase.tts_speed(speed=self.var_step_speed.get())
            self.var_step_speed_lbl.set(str(n))
        else:
            self.var_step_speed_lbl.set(f"(global: {global_spd})")
        if hasattr(self, "_step_speed_scale"):
            try:
                self._step_speed_scale.configure(state=("normal" if custom else "disabled"))
            except tk.TclError:
                pass

    def _on_step_speed_slide(self, _value: str | None = None) -> None:
        if getattr(self, "_loading", False):
            return
        if not self.var_step_speed_custom.get():
            return
        self.var_step_speed_lbl.set(str(atc_phrase.tts_speed(speed=self.var_step_speed.get())))

    def _on_step_speed_custom_toggle(self) -> None:
        if getattr(self, "_loading", False):
            self._refresh_step_speed_ui()
            return
        if self.var_step_speed_custom.get():
            # Seed from global so the first drag isn't a surprise jump
            self.var_step_speed.set(float(self._global_talk_speed()))
        self._refresh_step_speed_ui()

    def _place_dialog(
        self,
        dlg: tk.Toplevel,
        width: int,
        height: int,
        *,
        relative_to: tk.Misc | None = None,
    ) -> None:
        """Size a modal and center it over the app (not the top-left of the screen)."""
        dlg.update_idletasks()
        host = relative_to or self
        try:
            hx = int(host.winfo_rootx())
            hy = int(host.winfo_rooty())
            hw = int(host.winfo_width())
            hh = int(host.winfo_height())
            if hw > 1 and hh > 1:
                x = hx + (hw - width) // 2
                y = hy + (hh - height) // 2
            else:
                raise tk.TclError("host not mapped")
        except tk.TclError:
            sw = int(dlg.winfo_screenwidth())
            sh = int(dlg.winfo_screenheight())
            x = (sw - width) // 2
            y = (sh - height) // 2
        try:
            sw = int(dlg.winfo_screenwidth())
            sh = int(dlg.winfo_screenheight())
        except tk.TclError:
            sw, sh = 1280, 720
        # Keep the whole modal on-screen (taskbar / short Fly windows).
        width = min(width, max(320, sw - 24))
        height = min(height, max(240, sh - 72))
        x = max(8, min(x, sw - width - 8))
        y = max(8, min(y, sh - height - 56))
        dlg.geometry(f"{width}x{height}+{x}+{y}")

    def _pick_list_value(
        self,
        *,
        title: str,
        choices: list[str],
        current: str = "",
        parent: tk.Misc | None = None,
        heading: str | None = None,
    ) -> str | None:
        """
        Modal Listbox picker — more reliable than ttk.Combobox popdowns
        (dark clam theme + scrolled Plan form often breaks click-to-open).
        """
        if not choices:
            return None
        owner = parent or self
        result: dict[str, str | None] = {"value": None}

        dlg = tk.Toplevel(owner)
        dlg.title(title)
        dlg.configure(bg=C_BG)
        dlg.transient(owner)
        dlg.grab_set()
        # Size from choice count; keep usable on kneeboard laptops
        height = min(480, 160 + min(len(choices), 14) * 22)
        self._place_dialog(dlg, 420, height, relative_to=owner)

        tk.Label(
            dlg,
            text=heading or title,
            bg=C_BG,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 11),
            wraplength=390,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 6))

        lb = tk.Listbox(
            dlg,
            bg=C_CARD,
            fg=C_TEXT,
            selectbackground=C_ACCENT,
            selectforeground="#061018",
            font=("Segoe UI", 11),
            activestyle="none",
            highlightthickness=1,
            highlightbackground=C_BORDER,
            relief=tk.FLAT,
            exportselection=False,
        )
        lb.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
        select_idx = 0
        for i, item in enumerate(choices):
            lb.insert(tk.END, item)
            if item == current:
                select_idx = i
        lb.selection_set(select_idx)
        lb.activate(select_idx)
        lb.see(select_idx)

        def accept(_evt: object | None = None) -> None:
            sel = lb.curselection()
            if not sel:
                return
            result["value"] = lb.get(int(sel[0]))
            dlg.destroy()

        def cancel(_evt: object | None = None) -> None:
            result["value"] = None
            dlg.destroy()

        foot = tk.Frame(dlg, bg=C_BG)
        foot.pack(fill=tk.X, padx=12, pady=(4, 12))
        ttk.Button(foot, text="Cancel", command=cancel).pack(side=tk.RIGHT)
        ttk.Button(foot, text="Select", command=accept).pack(side=tk.RIGHT, padx=(0, 8))
        def _lb_wheel(event: tk.Event) -> str:
            delta = int(-1 * (event.delta / 120)) if event.delta else 0
            if delta:
                lb.yview_scroll(delta, "units")
            return "break"

        lb.bind("<Double-Button-1>", accept)
        lb.bind("<Return>", accept)
        lb.bind("<MouseWheel>", _lb_wheel)
        dlg.bind("<Escape>", cancel)
        lb.focus_set()
        dlg.wait_window()
        return result["value"]

    def _choose_step_template(self) -> None:
        """Pick a phrase template (Listbox — Combobox popdown fails in Plan scroll)."""
        if self._step_is_locked():
            messagebox.showinfo("Locked", "Unlock this step before editing.")
            return
        labels = list(getattr(self, "_tmpl_labels", []) or [lab for _, lab in atc_phrase.TEMPLATE_CHOICES])
        cur = self.var_template.get().strip()
        picked = self._pick_list_value(
            title="Template",
            heading="Phrase template",
            choices=labels,
            current=cur if cur in labels else (labels[0] if labels else ""),
            parent=self._step_edit_parent(),
        )
        if picked:
            self.var_template.set(picked)
            self._update_step_runway_ui()
            self._update_recovery_ui()
            self._refresh_keywords_summary()

    def _choose_step_runway(self) -> None:
        """Pick a step runway override."""
        if self._step_is_locked():
            messagebox.showinfo("Locked", "Unlock this step before editing.")
            return
        self._refresh_step_runway_choices()
        choices = list(getattr(self, "_runway_choices", [""]))
        # Show a readable blank option in the picker
        display = ["(default / blank)" if c == "" else c for c in choices]
        cur = self.var_step_runway.get().strip()
        cur_display = "(default / blank)" if not cur else cur
        if cur_display not in display and cur:
            display.append(cur)
            choices.append(cur)
        picked = self._pick_list_value(
            title="Runway",
            heading="Step runway (blank = Setup / flight plan / wind)",
            choices=display,
            current=cur_display if cur_display in display else display[0],
            parent=self._step_edit_parent(),
        )
        if picked is None:
            return
        if picked == "(default / blank)":
            self.var_step_runway.set("")
        else:
            self.var_step_runway.set(picked)

    def _pick_caoc_track(
        self,
        *,
        query: str = "",
        current_id: str = "",
        parent: tk.Misc | None = None,
    ) -> dict[str, Any] | None:
        """Pick a live CAOC air track (bullseye) for Phrase helper."""
        self._sync_identity_to_config()
        try:
            rows = atc_phrase.list_caoc_air_bullseyes(
                self.config_data, query=query.strip() or None, max_age_s=0.0
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("CAOC tracks", str(exc), parent=parent or self)
            return None
        if not rows:
            messagebox.showinfo(
                "CAOC tracks",
                "No matching air tracks.\nTry a different filter or check Opus backend URL in Setup.",
                parent=parent or self,
            )
            return None
        labels = [
            f"{r.get('display') or '?'}   —   {r.get('label') or r.get('radio_callsign') or r.get('unit_name')}"
            for r in rows
        ]
        current_lab = ""
        for i, r in enumerate(rows):
            if current_id and str(r.get("unit_id") or "") == str(current_id):
                current_lab = labels[i]
                break
        picked = self._pick_list_value(
            title="CAOC unit",
            heading="Live server track (bullseye from CAOC)",
            choices=labels,
            current=current_lab or labels[0],
            parent=parent,
        )
        if not picked:
            return None
        try:
            idx = labels.index(picked)
        except ValueError:
            return None
        return rows[idx]

    def _choose_step_recovery(self) -> None:
        """Pick Approach recovery type (Listbox — Combobox popdown is unreliable here)."""
        if self._step_is_locked():
            messagebox.showinfo("Locked", "Unlock this step before editing.")
            return
        labels = [lab for _, lab in atc_phrase.RECOVERY_CHOICES]
        cur = self.var_step_recovery.get().strip()
        picked = self._pick_list_value(
            title="Recovery type",
            heading="Visual / Instrument recovery",
            choices=labels,
            current=cur if cur in labels else atc_phrase.recovery_label(atc_phrase.DEFAULT_RECOVERY),
            parent=self._step_edit_parent(),
        )
        if picked:
            self.var_step_recovery.set(picked)

    def _choose_jump_step(self) -> None:
        """Pick a Fly jump target (Listbox — Combobox popdown fails in Fly scroll)."""
        labels = list(getattr(self, "_jump_index_by_label", {}) or {})
        if not labels:
            messagebox.showinfo("Go to step", "No steps loaded yet.")
            return
        cur = self.var_jump.get().strip()
        picked = self._pick_list_value(
            title="Jump to step",
            heading="Jump to step (no transmit)",
            choices=labels,
            current=cur if cur in labels else labels[0],
        )
        if picked:
            self.var_jump.set(picked)

    def _choose_step_voice(self) -> None:
        """Pick a voice for this step only (does not change Setup agency defaults)."""
        voices = self._list_voices()
        ch = (self.var_channel.get() or "other").strip().lower()
        female_only = atc_phrase.channel_requires_female(ch)
        if female_only:
            voices = [v for v in voices if atc_phrase.voice_gender(v) == "female"]
        if not voices:
            messagebox.showwarning(
                "Voices",
                "No female voices available for tanker.\nUnlock OneCore or switch TTS provider."
                if female_only
                else "No voices available for the current TTS provider.",
            )
            return
        current = self.var_step_voice.get().strip()
        if not current:
            current = atc_phrase.voice_for_channel(self.config_data, ch)[0]
        elif female_only:
            current = atc_phrase.ensure_female_voice(self.config_data, current)

        parent = self._step_edit_parent()
        dlg = tk.Toplevel(parent)
        dlg.title("Step voice — female only" if female_only else "Step voice")
        dlg.configure(bg=C_BG)
        dlg.transient(parent)
        dlg.grab_set()
        self._place_dialog(dlg, 420, 380, relative_to=parent)
        heading = (
            "Voice for this tanker step (female only)"
            if female_only
            else "Voice for this step only"
        )
        tk.Label(dlg, text=heading, bg=C_BG, fg=C_TEXT, font=("Segoe UI Semibold", 11)).pack(
            anchor="w", padx=12, pady=(12, 6)
        )
        lb = tk.Listbox(
            dlg,
            bg=C_CARD,
            fg=C_TEXT,
            selectbackground=C_ACCENT,
            selectforeground="#061018",
            font=("Segoe UI", 10),
            activestyle="none",
            highlightthickness=1,
            highlightbackground=C_BORDER,
            relief=tk.FLAT,
        )
        lb.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
        self._fill_voice_listbox(lb, voices, current)

        custom_var = tk.StringVar(value=current if current not in voices else "")
        if atc_phrase.tts_provider(self.config_data) == "google":
            cf = tk.Frame(dlg, bg=C_BG)
            cf.pack(fill=tk.X, padx=12, pady=6)
            tk.Label(cf, text="Or type Google voice id:", bg=C_BG, fg=C_MUTED).pack(anchor="w")
            ttk.Entry(cf, textvariable=custom_var).pack(fill=tk.X, pady=4)

        def resolve_voice() -> str:
            typed = custom_var.get().strip()
            picked = self._selected_voice_from_listbox(lb)
            if typed and atc_phrase.tts_provider(self.config_data) == "google":
                return typed
            if picked:
                return picked
            return typed

        def coerce_pick(pick: str) -> str:
            pick = (pick or "").strip()
            if pick and female_only:
                return atc_phrase.ensure_female_voice(self.config_data, pick)
            return pick

        def on_ok() -> None:
            pick = coerce_pick(resolve_voice())
            if not pick:
                return
            self.var_step_voice.set(pick)
            self._refresh_step_voice_display()
            dlg.destroy()

        def on_preview() -> None:
            self._preview_voice_sample(coerce_pick(resolve_voice()))

        btns = tk.Frame(dlg, bg=C_BG)
        btns.pack(fill=tk.X, padx=12, pady=(4, 12))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Select", style="Accent.TButton", command=on_ok).pack(side=tk.RIGHT, padx=8)
        ttk.Button(btns, text="Preview", command=on_preview).pack(side=tk.LEFT)
        def _lb_wheel(event: tk.Event) -> str:
            delta = int(-1 * (event.delta / 120)) if event.delta else 0
            if delta:
                lb.yview_scroll(delta, "units")
            return "break"

        lb.bind("<Double-Button-1>", lambda _e: on_ok())
        lb.bind("<MouseWheel>", _lb_wheel)
        dlg.bind("<Return>", lambda _e: on_ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    def refresh_timeline(self) -> None:
        self.timeline.delete(0, tk.END)
        steps = self._steps()
        for i, step in enumerate(steps):
            en = "●" if step.get("enabled", True) else "○"
            lock = "L" if step.get("locked") else " "
            ch = (step.get("channel") or "?").upper()[:4]
            mode = step.get("mode") or "tts"
            if mode == "file":
                kind = "FILE"
            elif step.get("text"):
                kind = "TEXT"
            else:
                kind = "TTS"
            label = step.get("label") or step.get("id")
            extra = ""
            if (step.get("channel") or "").lower() == "other":
                mhz = atc_phrase._parse_mhz(step.get("freq_mhz"))
                if mhz is not None:
                    extra = f" @{mhz:g}"
            if step.get("voice"):
                extra += " [v]"
            if step.get("tts_speed") is not None and str(step.get("tts_speed")).strip() != "":
                try:
                    extra += f" [spd {atc_phrase.tts_speed(speed=step.get('tts_speed'))}]"
                except (TypeError, ValueError):
                    extra += " [spd]"
            step_rwy = atc_phrase.normalize_runway(step.get("runway"))
            if step_rwy:
                extra += f" [rwy {step_rwy}]"
            trig = step.get("trigger")
            if isinstance(trig, dict) and str(trig.get("zone") or "").strip():
                extra += f" [auto: {str(trig['zone']).strip()}]"
            line = f" {i + 1:>2}  {en}{lock} {ch:<4}  {kind:<4}  {label}{extra}"
            self.timeline.insert(tk.END, line)
        if self.selected_index is not None and self.selected_index < len(steps):
            self.timeline.selection_set(self.selected_index)

    def _on_select(self, _evt=None) -> None:
        sel = self.timeline.curselection()
        if not sel:
            return
        self.selected_index = sel[0]
        step = self._steps()[self.selected_index]
        self._loading = True
        self.var_label.set(step.get("label") or "")
        self.var_channel.set(step.get("channel") or "ground")
        self._paint_channel_buttons()
        if hasattr(self, "var_mission_phase"):
            self.var_mission_phase.set(
                voice_intent.normalize_mission_phase(
                    str(step.get("phase") or ""),
                    channel=str(step.get("channel") or ""),
                )
                or voice_intent.default_mission_phase_for_channel(
                    str(step.get("channel") or "ground")
                )
            )
            self._paint_mission_phase_buttons()
        mode = step.get("mode") or "tts"
        if mode == "file":
            self.var_mode.set("file")
        elif step.get("text"):
            self.var_mode.set("custom")
        else:
            self.var_mode.set("tts")
        tmpl = step.get("template") or "radio_check"
        self.var_template.set(self._label_by_tmpl.get(tmpl, self._label_by_tmpl["radio_check"]))
        self.var_file.set(step.get("file") or "")
        self.var_enabled.set(bool(step.get("enabled", True)))
        self.var_step_c2.set(voice_intent.step_offers_c2(step))
        self.var_step_hold.set(voice_intent.step_holds_after_play(step))
        self.var_locked.set(bool(step.get("locked")))
        self.var_step_voice.set(str(step.get("voice") or "").strip())
        self.var_step_runway.set(str(step.get("runway") or "").strip())
        rec_key = atc_phrase.normalize_recovery_key(
            step.get("recovery") or step.get("recovery_type")
        )
        self.var_step_recovery.set(self._label_by_recovery.get(rec_key, atc_phrase.recovery_label(rec_key)))
        if step.get("tts_speed") is not None and str(step.get("tts_speed")).strip() != "":
            self.var_step_speed_custom.set(True)
            self.var_step_speed.set(float(atc_phrase.tts_speed(speed=step.get("tts_speed"))))
        else:
            self.var_step_speed_custom.set(False)
            self.var_step_speed.set(float(self._global_talk_speed()))
        mhz = atc_phrase._parse_mhz(step.get("freq_mhz"))
        if mhz is not None:
            self.var_freq_mhz.set(f"{mhz:g}")
        elif (step.get("channel") or "").lower() == "other":
            ap = self._airport()
            block = ap.get("other") or {}
            try:
                self.var_freq_mhz.set(f"{float(block.get('freq_mhz', 251.0)):g}")
            except (TypeError, ValueError):
                self.var_freq_mhz.set("251.0")
        else:
            self.var_freq_mhz.set("")
        self.txt_custom.configure(state=tk.NORMAL)
        self.txt_custom.delete("1.0", tk.END)
        if step.get("text"):
            self.txt_custom.insert(tk.END, step.get("text"))
        self._step_phrases_draft = list(voice_intent.parse_phrases(step.get("voice_phrases")))
        self._fill_cues_box()
        self._refresh_keywords_summary()
        self._trigger_to_form(step)
        self._mode_ui()
        self._update_freq_ui()
        self._refresh_step_voice_display()
        self._refresh_step_speed_ui()
        self._update_step_runway_ui()
        self._update_recovery_ui()
        self._loading = False
        self._apply_lock_ui()
        self._refresh_step_summary()
        # Show preview immediately for the selected step
        self.after(60, lambda: self.preview_step(apply=False))

    def apply_step(self) -> bool:
        if self.selected_index is None:
            messagebox.showinfo("Step", "Select a step in the timeline first.")
            return False
        steps = self._steps()
        step = steps[self.selected_index]
        if step.get("locked"):
            messagebox.showinfo("Locked", "Unlock this step before editing.")
            return False
        step["label"] = self.var_label.get().strip() or "Step"
        step["channel"] = self.var_channel.get().strip() or "other"
        phase = "departure"
        if hasattr(self, "var_mission_phase"):
            phase = voice_intent.normalize_mission_phase(
                self.var_mission_phase.get(), channel=step["channel"]
            ) or voice_intent.default_mission_phase_for_channel(step["channel"])
        else:
            phase = voice_intent.default_mission_phase_for_channel(step["channel"])
        step["phase"] = phase
        mode = self.var_mode.get()
        step["enabled"] = bool(self.var_enabled.get())
        step["c2"] = bool(self.var_step_c2.get())
        step["hold"] = bool(self.var_step_hold.get())

        voice = self.var_step_voice.get().strip()
        if voice:
            step["voice"] = voice
        else:
            step.pop("voice", None)

        phrases = list(voice_intent.parse_phrases(self._phrases_for_apply()))
        if phrases:
            step["voice_phrases"] = phrases
        else:
            step.pop("voice_phrases", None)

        if self.var_step_speed_custom.get():
            step["tts_speed"] = atc_phrase.tts_speed(speed=self.var_step_speed.get())
        else:
            step.pop("tts_speed", None)

        trigger = self._trigger_from_form()
        if trigger:
            step["trigger"] = trigger
        else:
            step.pop("trigger", None)

        # Keep a stored runway even if the field is hidden for this template type,
        # unless the user cleared it while the field was visible.
        if self._step_uses_runway():
            rwy = atc_phrase.normalize_runway(self.var_step_runway.get())
            if rwy:
                step["runway"] = rwy
                self.var_step_runway.set(rwy)
            else:
                step.pop("runway", None)

        lab_tmpl = self.var_template.get()
        tmpl_key = self._tmpl_by_label.get(lab_tmpl, "radio_check")
        if tmpl_key == "approach_check_in" and self.var_mode.get() == "tts":
            rec_ui = self.var_step_recovery.get().strip()
            rec = atc_phrase.normalize_recovery_key(
                self._recovery_by_label.get(rec_ui, rec_ui)
            )
            step["recovery"] = rec
            # Mission default when no Fly override yet
            if not self.mission.get("active_recovery"):
                self.mission["active_recovery"] = rec
            self.var_step_recovery.set(atc_phrase.recovery_label(rec))
        elif tmpl_key != "approach_check_in":
            # Leave existing recovery on the step if user switches templates
            pass

        if step["channel"].lower() == "other":
            mhz = atc_phrase._parse_mhz(self.var_freq_mhz.get())
            if mhz is None:
                messagebox.showerror(
                    "Freq",
                    "Other channel needs a frequency in MHz (e.g. 255.4).",
                    parent=self._step_edit_parent(),
                )
                return False
            step["freq_mhz"] = mhz
            step["mod"] = "AM"
        else:
            step.pop("freq_mhz", None)
            step.pop("mod", None)

        preview_phrase = self._phrase_from_preview_box()
        custom_box = self.txt_custom.get("1.0", tk.END).strip()
        lab = self.var_template.get()
        tmpl = self._tmpl_by_label.get(lab, "radio_check")
        base_preview = (self._preview_base_phrase or "").strip()

        if mode == "file":
            step["mode"] = "file"
            step["file"] = self.var_file.get().strip() or None
            step["text"] = None
            step["template"] = tmpl
        elif mode == "tts":
            # Live template: regenerate from Opus/METAR each Preview/Hear/Fly.
            # Only lock to custom if the user edited the Preview box.
            edited = bool(
                preview_phrase
                and base_preview
                and preview_phrase.strip() != base_preview
            )
            step["mode"] = "tts"
            step["file"] = None
            step["template"] = tmpl
            if edited:
                custom = preview_phrase.strip()
                step["text"] = custom
                self._loading = True
                self.var_mode.set("custom")
                self.txt_custom.configure(state=tk.NORMAL)
                self.txt_custom.delete("1.0", tk.END)
                self.txt_custom.insert(tk.END, custom)
                self._mode_ui()
                self._loading = False
                self._preview_base_phrase = custom
                self._last_preview_phrase = custom
            else:
                step["text"] = None
                self.txt_custom.configure(state=tk.NORMAL)
                self.txt_custom.delete("1.0", tk.END)
        else:
            # Explicit Custom mode — Custom box, or Preview if the user edited it.
            custom = custom_box or None
            if preview_phrase:
                if base_preview and preview_phrase.strip() != base_preview:
                    custom = preview_phrase.strip()
                elif not custom:
                    custom = preview_phrase.strip()
            step["mode"] = "tts"
            step["file"] = None
            step["template"] = tmpl
            if custom:
                step["text"] = custom
                self._loading = True
                self.var_mode.set("custom")
                self.txt_custom.configure(state=tk.NORMAL)
                self.txt_custom.delete("1.0", tk.END)
                self.txt_custom.insert(tk.END, custom)
                self._mode_ui()
                self._loading = False
                self._preview_base_phrase = custom
                self._last_preview_phrase = custom
            else:
                step["text"] = None
        self.refresh_timeline()
        self.timeline.selection_set(self.selected_index)
        self._refresh_step_summary()
        # Do not regenerate phrase here — Apply only saves settings.
        # Use Regenerate text to re-roll template wording.
        return True

    def regenerate_step_text(self) -> None:
        """Rebuild the spoken phrase (re-rolls random climb / Local vs freq / etc.)."""
        if self.selected_index is None:
            messagebox.showinfo("Step", "Select a step in the timeline first.")
            return
        if not self._step_is_locked():
            self.apply_step()
        self.preview_step(apply=False)

    def _current_step_template_key(self) -> str:
        mode = (self.var_mode.get() or "").strip()
        if mode in ("file", "custom"):
            return ""
        lab = (self.var_template.get() or "").strip()
        return str(self._tmpl_by_label.get(lab, "") or "").strip()

    def _fill_cues_box(self) -> None:
        box = getattr(self, "txt_cues", None)
        if box is None:
            return
        try:
            locked = bool(self.var_locked.get()) if hasattr(self, "var_locked") else False
            box.configure(state=tk.NORMAL)
            box.delete("1.0", tk.END)
            box.insert(tk.END, "\n".join(self._step_phrases_draft or []))
            if locked:
                box.configure(state=tk.DISABLED)
        except tk.TclError:
            pass

    def _read_cues_box(self) -> list[str]:
        box = getattr(self, "txt_cues", None)
        if box is None:
            return list(getattr(self, "_step_phrases_draft", []) or [])
        try:
            return list(voice_intent.parse_phrases(box.get("1.0", tk.END)))
        except tk.TclError:
            return list(getattr(self, "_step_phrases_draft", []) or [])

    def _phrases_for_apply(self) -> list[str]:
        mode = (self.var_mode.get() or "").strip()
        editor_open = bool(getattr(self, "_step_edit_open", False))
        if mode in ("custom", "file") and editor_open:
            phrases = self._read_cues_box()
            self._step_phrases_draft = phrases
            return phrases
        return list(getattr(self, "_step_phrases_draft", []) or [])

    def _refresh_keywords_summary(self) -> None:
        """One-line Keywords… summary on the Edit step form."""
        if not hasattr(self, "var_keywords_summary"):
            return
        phrases = list(getattr(self, "_step_phrases_draft", []) or [])
        n = len(phrases)
        mission = f"{n} mission phrase" + ("" if n == 1 else "s")
        tmpl = self._current_step_template_key()
        builtins = voice_intent.intents_for_template(tmpl) if tmpl else []
        if builtins and builtins[0].example:
            builtin = f'built-in: "{builtins[0].example}"'
        elif tmpl:
            builtin = "no built-in advance keywords"
        else:
            builtin = "mission phrases only (custom/file)"
        self.var_keywords_summary.set(f"{mission} · {builtin}")

    def _open_step_keywords(self) -> None:
        """View built-in advance keywords; edit mission voice_phrases for this step."""
        if self.selected_index is None:
            messagebox.showinfo("Keywords", "Select a step in the timeline first.")
            return
        parent = self._step_edit_parent()
        locked = self._step_is_locked()
        if (self.var_mode.get() or "").strip() in ("custom", "file") and getattr(
            self, "_step_edit_open", False
        ):
            self._step_phrases_draft = self._read_cues_box()

        dlg = tk.Toplevel(parent)
        step_label = (self.var_label.get() or "Step").strip() or "Step"
        dlg.title(f"Keywords — {step_label}")
        dlg.configure(bg=C_BG)
        dlg.minsize(480, 360)
        dlg.resizable(True, True)
        dlg.transient(parent)
        dlg.grab_set()
        self._place_dialog(dlg, 560, 480)

        phrases = list(getattr(self, "_step_phrases_draft", []) or [])
        tmpl = self._current_step_template_key()
        builtins = voice_intent.intents_for_template(tmpl) if tmpl else []

        # Pin Save at the bottom first so expanding lists cannot cover it.
        foot = tk.Frame(dlg, bg=C_BG)
        foot.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(6, 12))

        try_fr = tk.Frame(dlg, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        try_fr.pack(side=tk.BOTTOM, fill=tk.X, padx=12)

        hdr = tk.Frame(dlg, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        hdr.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(12, 6))
        tk.Label(
            hdr,
            text=step_label,
            bg=C_PANEL,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", padx=12, pady=(8, 0))
        tk.Label(
            hdr,
            text="Any one mission phrase fires this step. Built-in grammar needs "
            "one word from every required line together.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=500,
            justify=tk.LEFT,
        ).pack(anchor="w", padx=12, pady=(0, 8))

        body = tk.Frame(dlg, bg=C_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))

        built_lines: list[str] = []
        if builtins:
            for intent in builtins:
                example = intent.example or intent.id
                does = intent.does or "run this step"
                need = voice_intent.format_intent_need_lines(intent)
                built_lines.append(f'"{example}"  →  {does}')
                for i, line in enumerate(need, start=1):
                    built_lines.append(f"  {i}. {line}")
        elif tmpl:
            built_lines.append(f'No built-in intent for "{tmpl}". Use mission phrases.')
        if built_lines:
            built_fr = tk.Frame(
                body, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1
            )
            built_fr.pack(fill=tk.X)
            tk.Label(
                built_fr,
                text="MUST SAY (built-in, read-only)",
                bg=C_PANEL,
                fg=C_ACCENT,
                font=("Segoe UI Semibold", 9),
            ).pack(anchor="w", padx=10, pady=(6, 2))
            built_box = tk.Text(
                built_fr,
                height=min(6, max(3, len(built_lines))),
                bg="#0a0e14",
                fg=C_TEXT,
                insertbackground=C_TEXT,
                font=("Consolas", 9),
                relief=tk.FLAT,
                wrap=tk.WORD,
                padx=6,
                pady=4,
            )
            built_box.pack(fill=tk.X, padx=10, pady=(0, 8))
            built_box.insert(tk.END, "\n".join(built_lines))
            built_box.configure(state=tk.DISABLED)

        miss_fr = tk.Frame(body, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        miss_fr.pack(fill=tk.BOTH, expand=True, pady=(8, 0) if built_lines else 0)
        tk.Label(
            miss_fr,
            text="MISSION PHRASES",
            bg=C_PANEL,
            fg=C_ACCENT,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", padx=10, pady=(8, 2))
        tk.Label(
            miss_fr,
            text="Type a phrase and Add (or Enter). Save writes it onto this step.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=10, pady=(0, 4))

        add_row = tk.Frame(miss_fr, bg=C_PANEL)
        add_row.pack(fill=tk.X, padx=10, pady=(0, 6))
        var_new = tk.StringVar()
        add_entry = ttk.Entry(add_row, textvariable=var_new)
        add_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        list_fr = tk.Frame(miss_fr, bg=C_PANEL)
        list_fr.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))
        lb = tk.Listbox(
            list_fr,
            height=5,
            bg=C_CARD,
            fg=C_TEXT,
            selectbackground=C_ACCENT,
            selectforeground="#061018",
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=C_BORDER,
            activestyle="none",
        )
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(list_fr, orient=tk.VERTICAL, command=lb.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        lb.configure(yscrollcommand=scroll.set)

        editing_idx: list[int | None] = [None]

        def refill() -> None:
            lb.delete(0, tk.END)
            for p in phrases:
                lb.insert(tk.END, p)

        refill()

        def selected_index() -> int | None:
            sel = lb.curselection()
            return int(sel[0]) if sel else None

        def add_phrase(_evt: object | None = None) -> str:
            if locked:
                messagebox.showinfo("Locked", "Unlock this step before editing.", parent=dlg)
                return "break"
            text = var_new.get().strip()
            if not text:
                return "break"
            idx = editing_idx[0]
            if idx is not None and 0 <= idx < len(phrases):
                phrases[idx] = text
                refill()
                lb.selection_set(idx)
                editing_idx[0] = None
            elif text not in phrases:
                phrases.append(text)
                refill()
                lb.selection_clear(0, tk.END)
                lb.selection_set(tk.END)
                lb.see(tk.END)
            var_new.set("")
            return "break"

        def edit_selected(_evt: object | None = None) -> None:
            if locked:
                messagebox.showinfo("Locked", "Unlock this step before editing.", parent=dlg)
                return
            idx = selected_index()
            if idx is None:
                return
            var_new.set(phrases[idx])
            editing_idx[0] = idx
            add_entry.focus_set()
            add_entry.selection_range(0, tk.END)

        def remove_selected() -> None:
            if locked:
                messagebox.showinfo("Locked", "Unlock this step before editing.", parent=dlg)
                return
            idx = selected_index()
            if idx is None:
                return
            del phrases[idx]
            editing_idx[0] = None
            refill()

        def move(delta: int) -> None:
            if locked:
                return
            idx = selected_index()
            if idx is None:
                return
            j = idx + delta
            if j < 0 or j >= len(phrases):
                return
            phrases[idx], phrases[j] = phrases[j], phrases[idx]
            editing_idx[0] = None
            refill()
            lb.selection_set(j)

        ttk.Button(add_row, text="Add", command=add_phrase).pack(side=tk.LEFT, padx=(6, 0))
        add_entry.bind("<Return>", add_phrase)
        if not locked:
            add_entry.focus_set()

        btn_row = tk.Frame(miss_fr, bg=C_PANEL)
        btn_row.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Button(btn_row, text="Edit", command=edit_selected).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="Remove", command=remove_selected).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(btn_row, text="Up", command=lambda: move(-1)).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(btn_row, text="Down", command=lambda: move(1)).pack(side=tk.LEFT, padx=(6, 0))
        lb.bind("<Double-Button-1>", edit_selected)

        tk.Label(
            try_fr,
            text="TRY MATCH",
            bg=C_PANEL,
            fg=C_ACCENT,
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w", padx=10, pady=(8, 2))
        try_row = tk.Frame(try_fr, bg=C_PANEL)
        try_row.pack(fill=tk.X, padx=10, pady=(0, 2))
        var_try = tk.StringVar()
        try_entry = ttk.Entry(try_row, textvariable=var_try)
        try_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        var_try_result = tk.StringVar(value="Type a call and Test.")
        tk.Label(
            try_fr,
            textvariable=var_try_result,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=500,
            justify=tk.LEFT,
            anchor="w",
        ).pack(fill=tk.X, padx=10, pady=(0, 8))

        def run_try(_evt: object | None = None) -> str:
            spoken = var_try.get().strip()
            if not spoken:
                var_try_result.set("Enter something to test.")
                return "break"
            channel = (self.var_channel.get() or "").strip().lower()
            phase = ""
            if hasattr(self, "var_mission_phase"):
                phase = voice_intent.normalize_mission_phase(
                    self.var_mission_phase.get(), channel=channel
                )
            step_id = ""
            if self.selected_index is not None:
                try:
                    step_id = str(self._steps()[self.selected_index].get("id") or "").strip()
                except (IndexError, TypeError, AttributeError):
                    step_id = ""
            draft_steps = [
                {
                    "id": step_id or "_keywords_try",
                    "label": step_label,
                    "channel": channel,
                    "phase": phase,
                    "template": tmpl,
                    "voice_phrases": list(phrases),
                    "enabled": True,
                }
            ]
            ap = self._airport()
            usable = atc_phrase.airport_ops_runways(ap) + atc_phrase.airport_instrument_runways(ap)
            callsign = atc_phrase.cached_radio_callsign(self.config_data)
            require = True
            if hasattr(self, "var_voice_require_address"):
                require = bool(self.var_voice_require_address.get())
            ev = voice_intent.evaluate(
                spoken,
                channel=channel,
                phase=phase,
                expected=tmpl,
                callsign=callsign,
                runways=list(dict.fromkeys(usable)),
                steps=draft_steps,
                current_step_id=step_id or "_keywords_try",
                require_address=require,
            )
            if ev.fired and ev.match:
                m = ev.match
                var_try_result.set(
                    f"Fired → {m.describe()}"
                    + (f" · step {m.step_id}" if m.step_id else "")
                )
            else:
                var_try_result.set(ev.describe())
            return "break"

        ttk.Button(try_row, text="Test", command=run_try).pack(side=tk.LEFT, padx=(6, 0))
        try_entry.bind("<Return>", run_try)

        def accept(_evt: object | None = None) -> None:
            if not locked:
                pending = var_new.get().strip()
                if pending and pending not in phrases:
                    phrases.append(pending)
                self._step_phrases_draft = list(voice_intent.parse_phrases(phrases))
                self._fill_cues_box()
                self._refresh_keywords_summary()
                self.apply_step()
            dlg.destroy()
            if hasattr(self, "_refresh_voice_prompts"):
                self._refresh_voice_prompts()

        def cancel(_evt: object | None = None) -> None:
            dlg.destroy()

        ttk.Button(foot, text="Save", style="Accent.TButton", command=accept).pack(
            side=tk.RIGHT
        )
        ttk.Button(foot, text="Cancel", command=cancel).pack(side=tk.RIGHT, padx=(0, 6))
        dlg.bind("<Escape>", cancel)
        dlg.bind("<Control-Return>", accept)

    def phrase_helper(self) -> None:
        """Modal: pick a situation + fields → auto-generate wording onto the step."""
        if self.selected_index is None:
            messagebox.showinfo("Phrase helper", "Select a step in the timeline first.")
            return
        if self._step_is_locked():
            messagebox.showinfo("Locked", "Unlock this step before using Phrase helper.")
            return

        step = self._steps()[self.selected_index]
        dlg = tk.Toplevel(self)
        dlg.title("Phrase helper")
        dlg.configure(bg=C_BG)
        dlg.minsize(560, 480)
        dlg.transient(self)
        dlg.grab_set()
        self._place_dialog(dlg, 640, 560)

        hdr = tk.Frame(dlg, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        hdr.pack(fill=tk.X, padx=12, pady=(12, 6))
        tk.Label(
            hdr,
            text="Generate wording for custom / uncommon situations",
            bg=C_PANEL,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", padx=12, pady=10)

        body = tk.Frame(dlg, bg=C_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        sit_labels = [lab for _, lab in atc_phrase.SITUATION_CHOICES]
        sit_by_label = {lab: key for key, lab in atc_phrase.SITUATION_CHOICES}
        # Prefer situation matching current template
        tmpl = step.get("template") or ""
        default_sit = "freeform"
        for key, lab in atc_phrase.SITUATION_CHOICES:
            hint = atc_phrase.situation_applies_to_step(key).get("template")
            if hint and hint == tmpl:
                default_sit = key
                break
        default_lab = next(lab for k, lab in atc_phrase.SITUATION_CHOICES if k == default_sit)

        top = tk.Frame(body, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        top.pack(fill=tk.X)
        top_inner = tk.Frame(top, bg=C_PANEL)
        top_inner.pack(fill=tk.X, padx=12, pady=10)
        tk.Label(top_inner, text="Situation", bg=C_PANEL, fg=C_LABEL).pack(side=tk.LEFT)
        var_sit = tk.StringVar(value=default_lab)
        sit_lbl = tk.Label(
            top_inner,
            textvariable=var_sit,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Segoe UI", 10),
            anchor="w",
            padx=8,
            pady=4,
            highlightbackground=C_BORDER,
            highlightthickness=1,
        )
        sit_lbl.pack(side=tk.LEFT, padx=(10, 0), fill=tk.X, expand=True)

        def pick_situation() -> None:
            picked = self._pick_list_value(
                title="Situation",
                heading="What kind of call?",
                choices=sit_labels,
                current=var_sit.get(),
                parent=dlg,
            )
            if picked:
                var_sit.set(picked)
                rebuild_fields()

        ttk.Button(top_inner, text="Choose…", command=pick_situation).pack(side=tk.LEFT, padx=(6, 0))

        fields_host = tk.Frame(body, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        fields_host.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        fields_inner = tk.Frame(fields_host, bg=C_PANEL)
        fields_inner.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

        field_vars: dict[str, tk.StringVar] = {}
        field_widgets: list[tk.Widget] = []

        def clear_fields() -> None:
            for w in field_widgets:
                w.destroy()
            field_widgets.clear()
            field_vars.clear()

        def rebuild_fields(*_a: object) -> None:
            clear_fields()
            sit = sit_by_label.get(var_sit.get(), "freeform")
            specs = atc_phrase.situation_fields(sit)
            # Seed from step when relevant
            seeds: dict[str, str] = {}
            if sit == "approach_recovery":
                seeds["recovery"] = atc_phrase.normalize_recovery_key(step.get("recovery"))
                if step.get("descend_ft"):
                    seeds["descend_ft"] = str(step.get("descend_ft"))
                if step.get("speed_kt"):
                    seeds["speed_kt"] = str(step.get("speed_kt"))
            elif sit == "bj_range_exit":
                seeds["handoff_channel"] = str(step.get("handoff_channel") or "approach")
            elif sit == "approach_clearance":
                seeds["pattern"] = atc_phrase.normalize_recovery_key(
                    step.get("approach_pattern") or step.get("pattern") or step.get("recovery")
                )
            elif sit == "bj_alpha_check":
                if step.get("alpha_bullseye") and isinstance(step["alpha_bullseye"], dict):
                    ab = step["alpha_bullseye"]
                    if ab.get("unit_id"):
                        seeds["track_id"] = str(ab["unit_id"])
                    if ab.get("display") or ab.get("unit_name"):
                        seeds["track_label"] = (
                            f"{ab.get('display') or '?'}   —   "
                            f"{ab.get('label') or ab.get('unit_name') or 'Unit'}"
                        )
                    if ab.get("radio_callsign"):
                        seeds["callsign"] = str(ab["radio_callsign"])

            for spec in specs:
                key = str(spec["key"])
                if key in field_vars:
                    # Already seeded (e.g. custom companion field)
                    default = field_vars[key].get()
                else:
                    default = seeds.get(key, str(spec.get("default") or ""))
                choices = list(spec.get("choices") or [])
                labs = [cl for _, cl in choices]
                key_to_lab = {ck: cl for ck, cl in choices}
                if spec.get("kind") == "choice":
                    if default in key_to_lab:
                        default = key_to_lab[default]
                    elif default not in labs and default and any(ck == "__custom__" for ck, _ in choices):
                        custom_key = {
                            "recovery": "recovery_custom",
                            "expect": "expect_custom",
                            "pattern": "pattern_custom",
                        }.get(key, f"{key}_custom")
                        field_vars[custom_key] = tk.StringVar(value=default)
                        default = key_to_lab.get("__custom__", "Custom…")
                var = field_vars.get(key) or tk.StringVar(value=default)
                field_vars[key] = var
                row_f = tk.Frame(fields_inner, bg=C_PANEL)
                row_f.pack(fill=tk.X, pady=4)
                field_widgets.append(row_f)
                tk.Label(
                    row_f, text=str(spec.get("label") or key), bg=C_PANEL, fg=C_LABEL, width=16, anchor="w"
                ).pack(side=tk.LEFT)
                if spec.get("kind") == "choice":
                    if var.get() not in labs and labs:
                        for ck, cl in choices:
                            if ck == var.get() or cl == var.get():
                                var.set(cl)
                                break
                    tk.Label(
                        row_f,
                        textvariable=var,
                        bg=C_CARD,
                        fg=C_TEXT,
                        font=("Segoe UI", 10),
                        anchor="w",
                        padx=8,
                        pady=4,
                        highlightbackground=C_BORDER,
                        highlightthickness=1,
                    ).pack(side=tk.LEFT, fill=tk.X, expand=True)

                    def make_picker(v: tk.StringVar = var, options: list[str] = labs, label: str = str(spec.get("label") or key)) -> Callable[[], None]:
                        def pick() -> None:
                            picked = self._pick_list_value(
                                title=label,
                                heading=label,
                                choices=options,
                                current=v.get(),
                                parent=dlg,
                            )
                            if picked:
                                v.set(picked)
                                update_preview()

                        return pick

                    ttk.Button(row_f, text="Choose…", command=make_picker()).pack(side=tk.LEFT, padx=(6, 0))
                elif spec.get("kind") == "caoc_track":
                    # Hidden id + visible label
                    id_var = field_vars.get("track_id") or tk.StringVar(value=seeds.get("track_id", ""))
                    field_vars["track_id"] = id_var
                    lab_var = field_vars.get("track_label") or tk.StringVar(
                        value=seeds.get("track_label") or "(choose a live unit…)"
                    )
                    field_vars["track_label"] = lab_var
                    # Keep `var` (track_id key) pointing at id for collect_params
                    field_vars[key] = id_var
                    tk.Label(
                        row_f,
                        textvariable=lab_var,
                        bg=C_CARD,
                        fg=C_TEXT,
                        font=("Segoe UI", 10),
                        anchor="w",
                        padx=8,
                        pady=4,
                        highlightbackground=C_BORDER,
                        highlightthickness=1,
                    ).pack(side=tk.LEFT, fill=tk.X, expand=True)

                    def pick_track(
                        id_v: tk.StringVar = id_var,
                        lab_v: tk.StringVar = lab_var,
                    ) -> None:
                        q = ""
                        if "track_query" in field_vars:
                            q = field_vars["track_query"].get().strip()
                        row = self._pick_caoc_track(
                            query=q,
                            current_id=id_v.get().strip(),
                            parent=dlg,
                        )
                        if not row:
                            return
                        id_v.set(str(row.get("unit_id") or ""))
                        lab_v.set(
                            f"{row.get('display') or '?'}   —   "
                            f"{row.get('label') or row.get('radio_callsign') or row.get('unit_name')}"
                        )
                        # Seed callsign from track unless user already typed one
                        if "callsign" in field_vars:
                            cur_cs = field_vars["callsign"].get().strip()
                            if not cur_cs:
                                field_vars["callsign"].set(
                                    str(row.get("radio_callsign") or row.get("unit_name") or "")
                                )
                        update_preview()

                    ttk.Button(row_f, text="Choose…", command=pick_track).pack(side=tk.LEFT, padx=(6, 0))
                else:
                    ent = ttk.Entry(row_f, textvariable=var, width=38)
                    ent.pack(side=tk.LEFT, fill=tk.X, expand=True)
                    ent.bind("<KeyRelease>", lambda _e: update_preview())
                hint = str(spec.get("hint") or "")
                if hint:
                    tk.Label(row_f, text=hint, bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 8)).pack(
                        side=tk.LEFT, padx=(8, 0)
                    )
            self.after(10, update_preview)

        preview_fr = tk.Frame(body, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        preview_fr.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        tk.Label(
            preview_fr, text="Generated phrase", bg=C_PANEL, fg=C_TEXT, font=("Segoe UI Semibold", 10)
        ).pack(anchor="w", padx=10, pady=(8, 4))
        preview = tk.Text(
            preview_fr,
            height=6,
            bg="#0a0e14",
            fg=C_GREEN,
            font=("Consolas", 10),
            relief=tk.FLAT,
            wrap=tk.WORD,
            padx=8,
            pady=8,
        )
        preview.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        def collect_params() -> dict[str, Any]:
            sit = sit_by_label.get(var_sit.get(), "freeform")
            specs = atc_phrase.situation_fields(sit)
            params: dict[str, Any] = {}
            for spec in specs:
                key = str(spec["key"])
                raw = field_vars[key].get().strip() if key in field_vars else ""
                if spec.get("kind") == "choice":
                    # map label → key
                    lab_to_key = {cl: ck for ck, cl in (spec.get("choices") or [])}
                    params[key] = lab_to_key.get(raw, raw)
                elif spec.get("kind") == "caoc_track":
                    params[key] = field_vars["track_id"].get().strip() if "track_id" in field_vars else raw
                else:
                    params[key] = raw
            return params

        def update_preview(*_a: object) -> None:
            sit = sit_by_label.get(var_sit.get(), "freeform")
            try:
                self._sync_identity_to_config()
                ap = self._airport()
                cs = "Flight"
                if hasattr(self, "var_callsign"):
                    raw_cs = self.var_callsign.get().strip()
                    if raw_cs and not raw_cs.startswith("("):
                        cs = raw_cs
                if hasattr(self, "var_callsign_override"):
                    ov = self.var_callsign_override.get().strip()
                    if ov:
                        cs = ov
                params = collect_params()
                # Alpha check uses selected track callsign when provided
                if sit == "bj_alpha_check" and params.get("callsign"):
                    cs = str(params["callsign"])
                wx = atc_phrase.Weather(270, 10, 29.92, "")
                rwy = atc_phrase.normalize_runway(step.get("runway")) or ""
                if not rwy:
                    try:
                        rwy = atc_phrase.pick_recovery_runway(ap, wx)
                    except Exception:
                        rwy = "21R"
                text = atc_phrase.generate_situation_phrase(
                    sit, ap, cs, wx, rwy, params, config=self.config_data
                )
            except Exception as exc:  # noqa: BLE001
                text = f"(could not generate: {exc})"
            preview.configure(state=tk.NORMAL)
            preview.delete("1.0", tk.END)
            preview.insert(tk.END, text)
            preview.configure(state=tk.NORMAL)

        foot = tk.Frame(dlg, bg=C_BG)
        foot.pack(fill=tk.X, padx=12, pady=(8, 12))

        def use_phrase() -> None:
            sit = sit_by_label.get(var_sit.get(), "freeform")
            params = collect_params()
            update_preview()
            text = preview.get("1.0", tk.END).strip()
            if not text or text.startswith("(could not"):
                messagebox.showerror("Phrase helper", "Generate a valid phrase first.", parent=dlg)
                return
            apply_meta = atc_phrase.situation_applies_to_step(sit)
            if apply_meta.get("channel"):
                step["channel"] = apply_meta["channel"]
                step["phase"] = voice_intent.default_mission_phase_for_channel(
                    str(apply_meta.get("channel") or "")
                )
            if apply_meta.get("template"):
                step["template"] = apply_meta["template"]
            if sit == "approach_recovery":
                rec = params.get("recovery") or atc_phrase.DEFAULT_RECOVERY
                if rec == "__custom__":
                    rec = params.get("recovery_custom") or atc_phrase.DEFAULT_RECOVERY
                rec = atc_phrase.normalize_recovery_key(rec)
                step["recovery"] = rec
                self.mission["active_recovery"] = rec
                for k in ("descend_ft", "speed_kt"):
                    if params.get(k):
                        try:
                            step[k] = int(str(params[k]).replace(",", ""))
                        except ValueError:
                            pass
            elif sit == "approach_clearance":
                pat = params.get("pattern") or atc_phrase.DEFAULT_RECOVERY
                if pat == "__custom__":
                    pat = params.get("pattern_custom") or pat
                step["approach_pattern"] = atc_phrase.normalize_recovery_key(pat)
            elif sit == "bj_range_exit":
                step["handoff_channel"] = params.get("handoff_channel") or "approach"
            elif sit == "bj_alpha_check":
                fix = atc_phrase.resolve_situation_alpha_fix(self.config_data, params)
                if fix:
                    step["alpha_bullseye"] = {
                        "name": fix.get("name"),
                        "bearing": fix.get("bearing"),
                        "range_nm": fix.get("range_nm"),
                        "display": fix.get("display"),
                        "spoken": fix.get("spoken"),
                        "unit_name": fix.get("unit_name"),
                        "unit_id": fix.get("unit_id"),
                        "radio_callsign": fix.get("radio_callsign"),
                        "label": fix.get("label"),
                    }
            elif sit == "agency_contact":
                step["handoff_channel"] = params.get("handoff_channel") or "approach"
                if params.get("from_channel"):
                    step["channel"] = params["from_channel"]
                    step["phase"] = voice_intent.default_mission_phase_for_channel(
                        str(params.get("from_channel") or "")
                    )

            # Lock generated wording as custom text (editable)
            step["mode"] = "tts"
            step["text"] = text
            step["file"] = None
            self._loading = True
            self.var_mode.set("custom")
            self.var_channel.set(step.get("channel") or "other")
            self._paint_channel_buttons()
            self.var_template.set(
                self._label_by_tmpl.get(step.get("template") or "radio_check", "Other — Radio check")
            )
            if step.get("recovery"):
                rk = str(step["recovery"])
                self.var_step_recovery.set(self._label_by_recovery.get(rk, rk))
            self.txt_custom.configure(state=tk.NORMAL)
            self.txt_custom.delete("1.0", tk.END)
            self.txt_custom.insert(tk.END, text)
            self._mode_ui()
            self._loading = False
            self._set_preview_display(text, base_phrase=text)
            self.refresh_timeline()
            self.timeline.selection_set(self.selected_index)
            dlg.destroy()

        ttk.Button(foot, text="Refresh preview", command=update_preview).pack(side=tk.LEFT)
        ttk.Button(foot, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(foot, text="Use on this step", command=use_phrase).pack(side=tk.RIGHT, padx=(0, 8))

        rebuild_fields()
        # Trace field changes — rebuild binds situation; also poll on focus-out via Refresh
        dlg.bind("<Return>", lambda _e: use_phrase())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    def add_step(self) -> None:
        steps = self._steps()
        ch = "ground"
        step = {
            "id": slug_id("new_step"),
            "phase": voice_intent.default_mission_phase_for_channel(ch),
            "label": "New step",
            "channel": ch,
            "mode": "tts",
            "template": "radio_check",
            "file": None,
            "text": None,
            "enabled": True,
            "locked": False,
        }
        steps.append(step)
        self.selected_index = len(steps) - 1
        self.refresh_timeline()
        self.timeline.selection_set(self.selected_index)
        self._on_select()
        self.after(80, self._open_step_editor)

    def duplicate_step(self) -> None:
        if self.selected_index is None:
            return
        steps = self._steps()
        import copy

        step = copy.deepcopy(steps[self.selected_index])
        step["id"] = slug_id(step.get("label") or "step")
        step["label"] = (step.get("label") or "Step") + " copy"
        step["locked"] = False  # copies start unlocked so you can edit them
        steps.insert(self.selected_index + 1, step)
        self.selected_index += 1
        self.refresh_timeline()
        self.timeline.selection_set(self.selected_index)
        self._on_select()
        self.after(80, self._open_step_editor)

    def delete_step(self) -> None:
        if self.selected_index is None:
            return
        step = self._steps()[self.selected_index]
        if step.get("locked"):
            messagebox.showinfo("Locked", "Unlock this step before deleting.")
            return
        if not messagebox.askyesno("Delete", "Delete this step?"):
            return
        steps = self._steps()
        steps.pop(self.selected_index)
        self.selected_index = min(self.selected_index, len(steps) - 1) if steps else None
        self.refresh_timeline()
        if self.selected_index is not None:
            self.timeline.selection_set(self.selected_index)
            self._on_select()
        else:
            self._refresh_step_summary()
            if hasattr(self, "preview_box"):
                self._set_preview_display(
                    "Select a step to preview the radio call here.\n"
                    "Edit step… to change settings · Regenerate / Hear / TX below."
                )

    def move_step(self, delta: int) -> None:
        if self.selected_index is None:
            return
        steps = self._steps()
        j = self.selected_index + delta
        if j < 0 or j >= len(steps):
            return
        steps[self.selected_index], steps[j] = steps[j], steps[self.selected_index]
        self.selected_index = j
        self.refresh_timeline()
        self.timeline.selection_set(j)

    def _browse_file(self) -> None:
        if self._step_is_locked():
            messagebox.showinfo(
                "Locked",
                "Unlock this step before changing the audio file.",
                parent=self._step_edit_parent(),
            )
            return
        path = filedialog.askopenfilename(
            parent=self._step_edit_parent(),
            title="Audio file for this step",
            filetypes=[
                ("Audio", "*.mp3 *.ogg *.wav *.flac"),
                ("MP3", "*.mp3"),
                ("OGG", "*.ogg"),
                ("WAV", "*.wav"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self.var_file.set(path)
        self.var_mode.set("file")
        self._mode_ui()
        # Persist onto the step immediately so TX / Fly do not need a separate Apply.
        if self.selected_index is not None:
            try:
                self.apply_step()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Audio file", str(exc))
                return
            self._set_preview_display(
                f"(file) {path}\n\nSaved on this step — TX → SRS or Fly will play it.",
                base_phrase="",
            )

    def _flows_dir(self) -> Path:
        d = HERE / "flows"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _mission_file_slug(self, name: str) -> str:
        return re.sub(r"[^\w\-]+", "_", (name or "").strip()).strip("_").lower() or "mission"

    def _is_base_flow_path(self, path: Path) -> bool:
        return flow_engine.is_base_flow_path(path)

    def _list_base_flows(self) -> list[tuple[str, Path | None]]:
        """Base templates (*_default.json) plus a blank timeline option."""
        choices: list[tuple[str, Path | None]] = []
        for path in sorted(self._flows_dir().glob("*_default.json")):
            try:
                data = load_json(path)
                label = str(data.get("name") or path.stem)
            except Exception:
                label = path.stem
            choices.append((label, path))
        if not choices:
            fallback = HERE / "flows" / "nellis_default.json"
            if fallback.is_file():
                choices.append(("Nellis Default", fallback))
        choices.append(("Blank timeline", None))
        return choices

    def _unique_mission_path(self, slug: str) -> Path:
        """flows/{slug}.json, or slug_2.json… — never overwrites *_default.json."""
        flows = self._flows_dir()
        candidate = flows / f"{slug}.json"
        if self._is_base_flow_path(candidate):
            slug = f"{slug}_plan"
            candidate = flows / f"{slug}.json"
        if not candidate.exists():
            return candidate
        n = 2
        while True:
            alt = flows / f"{slug}_{n}.json"
            if not alt.exists():
                return alt
            n += 1

    def new_mission(self) -> None:
        """Modal: name + airport + base flow + optional Opus import → new active plan."""
        base_choices = self._list_base_flows()
        label_to_path = {lab: path for lab, path in base_choices}
        airport_keys = sorted(self.airports.keys()) or ["nellis"]
        default_airport = (
            self.mission.get("airport")
            or self.config_data.get("default_airport")
            or airport_keys[0]
        )
        if default_airport not in airport_keys:
            default_airport = airport_keys[0]

        dlg = tk.Toplevel(self)
        dlg.title("New mission plan")
        dlg.configure(bg=C_BG)
        dlg.minsize(560, 460)
        dlg.transient(self)
        dlg.grab_set()
        self._place_dialog(dlg, 640, 520)

        hdr = tk.Frame(dlg, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        hdr.pack(fill=tk.X, padx=12, pady=(12, 6))
        tk.Label(
            hdr,
            text="Start a new plan from a base flow — optionally import callsign / FP from Opus",
            bg=C_PANEL,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", padx=12, pady=10)

        body = tk.Frame(dlg, bg=C_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        form = tk.Frame(body, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        form.pack(fill=tk.X)
        inner = tk.Frame(form, bg=C_PANEL)
        inner.pack(fill=tk.X, padx=14, pady=14)

        var_name = tk.StringVar(value="Untitled")
        var_airport = tk.StringVar(value=str(default_airport))
        var_base = tk.StringVar(value=base_choices[0][0])
        var_file = tk.StringVar(value=self._unique_mission_path("untitled").name)

        def row_label(r: int, text: str) -> None:
            tk.Label(inner, text=text, bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).grid(
                row=r, column=0, sticky="w", pady=6, padx=(0, 12)
            )

        row_label(0, "Mission name")
        name_entry = ttk.Entry(inner, textvariable=var_name, width=40)
        name_entry.grid(row=0, column=1, sticky="we", pady=6)
        base_labels = [lab for lab, _ in base_choices]

        def _choose_row(var: tk.StringVar, choices: list[str], title: str, heading: str) -> None:
            picked = self._pick_list_value(
                title=title,
                heading=heading,
                choices=choices,
                current=var.get() if var.get() in choices else (choices[0] if choices else ""),
                parent=dlg,
            )
            if picked:
                var.set(picked)

        def _picker_row(r: int, var: tk.StringVar, choices: list[str], title: str, heading: str) -> None:
            fr = tk.Frame(inner, bg=C_PANEL)
            fr.grid(row=r, column=1, sticky="we", pady=6)
            tk.Label(
                fr,
                textvariable=var,
                bg=C_CARD,
                fg=C_TEXT,
                font=("Segoe UI", 10),
                anchor="w",
                padx=8,
                pady=4,
                highlightbackground=C_BORDER,
                highlightthickness=1,
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Button(
                fr,
                text="Choose…",
                command=lambda: _choose_row(var, choices, title, heading),
            ).pack(side=tk.LEFT, padx=(6, 0))

        row_label(1, "Airport")
        _picker_row(1, var_airport, list(airport_keys), "Airport", "Mission airport")
        row_label(2, "Base flow")
        _picker_row(2, var_base, base_labels, "Base flow", "Starting flow template")
        tk.Label(
            inner,
            text="Copies the selected template into a new mission file (base templates are never overwritten).",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=420,
            justify="left",
        ).grid(row=3, column=1, sticky="w", pady=(0, 4))

        row_label(4, "Save as")
        file_row = tk.Frame(inner, bg=C_PANEL)
        file_row.grid(row=4, column=1, sticky="we", pady=6)
        ttk.Entry(file_row, textvariable=var_file, width=32).pack(side=tk.LEFT, fill=tk.X, expand=True)

        def browse_save() -> None:
            slug = self._mission_file_slug(var_name.get())
            initial = (var_file.get() or f"{slug}.json").strip()
            initial_name = Path(initial).name
            path_str = filedialog.asksaveasfilename(
                parent=dlg,
                title="Save new mission as",
                initialdir=str(self._flows_dir()),
                initialfile=initial_name,
                defaultextension=".json",
                filetypes=[("Mission JSON", "*.json"), ("All files", "*.*")],
            )
            if path_str:
                var_file.set(path_str)

        ttk.Button(file_row, text="Browse…", command=browse_save).pack(side=tk.LEFT, padx=(8, 0))
        inner.columnconfigure(1, weight=1)

        def sync_filename_from_name(*_args: object) -> None:
            # Only auto-update while the file still looks like a derived slug
            cur = (var_file.get() or "").strip()
            stem = Path(cur).stem if cur else ""
            prev_slug = self._mission_file_slug(getattr(sync_filename_from_name, "_last_name", "Untitled"))
            if not cur or stem == prev_slug or stem.startswith(prev_slug + "_"):
                slug = self._mission_file_slug(var_name.get())
                var_file.set(f"{slug}.json")
            sync_filename_from_name._last_name = var_name.get()  # type: ignore[attr-defined]

        sync_filename_from_name._last_name = var_name.get()  # type: ignore[attr-defined]
        var_name.trace_add("write", sync_filename_from_name)

        # Opus import panel
        opus = tk.Frame(body, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        opus.pack(fill=tk.X, pady=(10, 0))
        opus_inner = tk.Frame(opus, bg=C_PANEL)
        opus_inner.pack(fill=tk.X, padx=14, pady=12)
        tk.Label(
            opus_inner,
            text="Opus flight (optional)",
            bg=C_PANEL,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w")
        tk.Label(
            opus_inner,
            text="Imports callsign, seat, and flight plan into Setup for clearance / squawk / route.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))

        # Local label so New modal stays in sync without requiring Setup tab focus
        var_opus_local = tk.StringVar(value=self.var_opus_flight.get() if hasattr(self, "var_opus_flight") else "(choose an Opus flight)")

        def refresh_opus_label() -> None:
            if hasattr(self, "var_opus_flight"):
                var_opus_local.set(self.var_opus_flight.get())

        tk.Label(
            opus_inner,
            textvariable=var_opus_local,
            bg=C_PANEL,
            fg=C_GREEN,
            font=("Segoe UI Semibold", 10),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        opus_btns = tk.Frame(opus_inner, bg=C_PANEL)
        opus_btns.pack(anchor="w")

        def choose_opus() -> None:
            # Sync Setup identity fields if present
            if hasattr(self, "var_user"):
                self.config_data["opus_user_name"] = self.var_user.get().strip()
            if hasattr(self, "var_backend"):
                self.config_data["opus_backend_url"] = self.var_backend.get().strip()
            self._choose_opus_flight(parent=dlg, on_done=refresh_opus_label)

        def clear_opus() -> None:
            self._clear_opus_flight()
            refresh_opus_label()

        ttk.Button(opus_btns, text="Choose Opus flight…", command=choose_opus).pack(side=tk.LEFT)
        ttk.Button(opus_btns, text="Clear", command=clear_opus).pack(side=tk.LEFT, padx=(8, 0))

        foot = tk.Frame(dlg, bg=C_BG)
        foot.pack(fill=tk.X, padx=12, pady=(8, 12))

        def create_plan() -> None:
            name = var_name.get().strip() or "Untitled"
            airport = var_airport.get().strip() or "nellis"
            base_label = var_base.get()
            base_path = label_to_path.get(base_label)

            file_name = (var_file.get() or "").strip()
            if not file_name:
                file_name = f"{self._mission_file_slug(name)}.json"
            save_path = Path(file_name)
            if not save_path.is_absolute():
                if not file_name.lower().endswith(".json"):
                    file_name += ".json"
                    save_path = Path(file_name)
                save_path = self._flows_dir() / save_path.name
            elif save_path.suffix.lower() != ".json":
                save_path = save_path.with_suffix(".json")

            if self._is_base_flow_path(save_path):
                messagebox.showerror(
                    "New plan",
                    f"Cannot overwrite base template:\n{save_path.name}\n\n"
                    "Pick a different file name.",
                    parent=dlg,
                )
                return

            if save_path.exists():
                if not messagebox.askyesno(
                    "New plan",
                    f"{save_path.name} already exists.\nOverwrite it?",
                    parent=dlg,
                ):
                    return

            try:
                if base_path is not None:
                    data = load_json(base_path)
                    if not isinstance(data, dict):
                        raise ValueError("Base flow is not a mission object.")
                    mission = flow_engine.normalize_mission_to_steps(copy.deepcopy(data))
                else:
                    mission = {"name": name, "airport": airport, "steps": []}
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("New plan", f"Could not load base flow:\n{exc}", parent=dlg)
                return

            mission["name"] = name
            mission["airport"] = airport
            # New plans always start on wind/ops runways — never inherit a sticky request.
            mission.pop("requested_runway", None)

            try:
                save_json(save_path, mission)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("New plan", f"Could not save mission:\n{exc}", parent=dlg)
                return

            self.mission = mission
            self.engine.mission = mission
            self._set_active_flow_path(save_path)
            self.mission_name_var.set(name)
            self.selected_index = None
            try:
                self.engine.reset()
            except Exception:
                pass
            self.refresh_timeline()
            self._update_freq_hint()
            try:
                self._refresh_fly_status()
            except Exception:
                pass
            # Refresh callsign if Opus was linked
            if atc_phrase.configured_opus_flight_id(self.config_data) is not None:
                try:
                    self._refresh_callsign()
                except Exception:
                    pass

            dlg.destroy()
            steps_n = len(self._steps())
            opus_note = ""
            if atc_phrase.configured_opus_flight_id(self.config_data) is not None:
                opus_note = "\nOpus flight linked in Setup."
            messagebox.showinfo(
                "New plan",
                f"Created “{name}” with {steps_n} step(s).\n{save_path}{opus_note}\n\n"
                "This is now the active mission for Fly.",
            )

        ttk.Button(foot, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(foot, text="Create plan", command=create_plan).pack(side=tk.RIGHT, padx=(0, 8))
        name_entry.focus_set()
        name_entry.select_range(0, tk.END)
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.bind("<Return>", lambda _e: create_plan())

    def _set_active_flow_path(self, path: Path) -> None:
        """Point config at this mission file (prefer path relative to atc/)."""
        path = path.resolve()
        try:
            rel = path.relative_to(HERE)
            self.config_data["flow_file"] = rel.as_posix()
        except ValueError:
            self.config_data["flow_file"] = str(path)
        save_json(CONFIG_PATH, self.config_data)
        self.engine.config = self.config_data

    def load_mission(self) -> None:
        path_str = filedialog.askopenfilename(
            title="Load mission",
            initialdir=str(self._flows_dir()),
            filetypes=[("Mission JSON", "*.json"), ("All files", "*.*")],
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            data = load_json(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Load mission", f"Could not read file:\n{exc}")
            return
        if not isinstance(data, dict):
            messagebox.showerror("Load mission", "File is not a mission object.")
            return
        self.mission = flow_engine.normalize_mission_to_steps(data)
        if not self.mission.get("airport"):
            self.mission["airport"] = self.config_data.get("default_airport") or "nellis"
        self.engine.mission = self.mission
        self._set_active_flow_path(path)
        # Saved non-default plans keep their requested runway; base templates do not.
        self.engine.sync_requested_runway_from_mission()
        try:
            self.engine.save_state()
        except Exception:
            pass
        self.mission_name_var.set(self.mission.get("name") or path.stem)
        self.selected_index = None
        self.refresh_timeline()
        self._update_freq_hint()
        try:
            self._refresh_fly_status()
        except Exception:
            pass
        messagebox.showinfo(
            "Loaded",
            f"Loaded {len(self._steps())} steps.\n{path}\n\nThis is now the active mission for Fly.",
        )

    def save_mission(self) -> None:
        self.mission["name"] = self.mission_name_var.get().strip() or "Untitled"
        flow_engine.normalize_mission_to_steps(self.mission)
        path = flow_engine.resolve_flow_path(self.config_data)
        save_json(path, self.mission)
        self.engine.mission = self.mission
        messagebox.showinfo("Saved", f"Mission saved.\n{path}")

    def save_mission_as(self) -> None:
        self.mission["name"] = self.mission_name_var.get().strip() or "Untitled"
        flow_engine.normalize_mission_to_steps(self.mission)
        default_name = re.sub(r"[^\w\-]+", "_", self.mission["name"]).strip("_").lower() or "mission"
        path_str = filedialog.asksaveasfilename(
            title="Save mission as",
            initialdir=str(self._flows_dir()),
            initialfile=f"{default_name}.json",
            defaultextension=".json",
            filetypes=[("Mission JSON", "*.json"), ("All files", "*.*")],
        )
        if not path_str:
            return
        path = Path(path_str)
        save_json(path, self.mission)
        self.engine.mission = self.mission
        self._set_active_flow_path(path)
        messagebox.showinfo("Saved", f"Mission saved.\n{path}\n\nThis is now the active mission for Fly.")

    def reset_flight_cache(self) -> None:
        """
        Clear sticky sortie cache: unrestricted climb, shared initial climb,
        approach assignment, pilot runway, takeoff offer, Opus/METAR lookups.
        Re-picks runway from winds and seeks the timeline to the start.
        """
        if not messagebox.askyesno(
            "Reset flight cache",
            "Clear sticky flight data and start fresh?\n\n"
            "This drops:\n"
            "• unrestricted climb / shared initial climb\n"
            "• assigned approach / recovery\n"
            "• requested runway (back to winds)\n"
            "• takeoff rolling offer state\n"
            "• cached Opus flight + METAR\n\n"
            "The timeline cursor returns to step 1.",
        ):
            return
        try:
            self._sync_identity_to_config()
            self.engine.config = self.config_data
            self.engine.mission = self.mission
            status = self.engine.clear_flight_cache()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Reset flight cache", str(exc))
            return
        self.mission = self.engine.mission
        self._position_tracker().reset()
        if hasattr(self, "var_runway_override"):
            self.var_runway_override.set("")
        # Persist climb/runway stickiness clear on saved missions only.
        try:
            path = flow_engine.resolve_flow_path(self.config_data)
            if not flow_engine.is_base_flow_path(path):
                self.mission["name"] = (
                    self.mission_name_var.get().strip()
                    or self.mission.get("name")
                    or "Untitled"
                )
                flow_engine.normalize_mission_to_steps(self.mission)
                save_json(path, self.mission)
        except Exception:
            pass
        try:
            self.refresh_timeline()
        except Exception:
            pass
        try:
            self._refresh_fly_status()
            self._sync_fly_pilot_request_ui()
            self._sync_fly_recovery_ui()
        except Exception:
            pass
        if self.selected_index is not None:
            self.after(40, lambda: self.preview_step(apply=False))
        rwy = str(status.get("runway") or "").strip()
        msg = "Flight cache cleared. Cursor at step 1."
        if rwy:
            msg += f"\nRunway from winds: {rwy}"
        messagebox.showinfo("Reset flight cache", msg)

    def _sync_identity_to_config(self, *, allow_clear_opus: bool = False) -> None:
        """Push Setup identity / TTS fields into runtime config (even before Save).

        Never overwrite a saved Opus username/backend (or Google credentials path) with a
        blank UI value unless allow_clear_opus=True. Empty StringVars during early UI
        build used to wipe config.json on the next preview/TX save.
        """
        if hasattr(self, "var_user"):
            user = self.var_user.get().strip()
            if user or allow_clear_opus:
                self.config_data["opus_user_name"] = user
        if hasattr(self, "var_backend"):
            backend = self.var_backend.get().strip()
            if backend or allow_clear_opus:
                self.config_data["opus_backend_url"] = backend
        if hasattr(self, "var_callsign_override"):
            self.config_data["callsign_override"] = self.var_callsign_override.get().strip()
        # opus_flight_id / opus_seat live only in config_data (set by flight picker)
        if hasattr(self, "var_runway_override"):
            self.config_data["runway_override"] = self.var_runway_override.get().strip()
        if hasattr(self, "var_tts_provider"):
            self.config_data["tts_provider"] = atc_phrase.tts_provider(
                {"tts_provider": self.var_tts_provider.get()}
            )
        if hasattr(self, "var_google_credentials"):
            creds = self.var_google_credentials.get().strip()
            if creds or allow_clear_opus:
                self.config_data["google_credentials"] = creds
        if hasattr(self, "voice_vars"):
            voices = {ch: var.get().strip() for ch, var in self.voice_vars.items()}
            self.config_data["tts_voices"] = voices
            default_voice = voices.get("default") or next(iter(voices.values()), "")
            if default_voice:
                self.config_data["tts_voice"] = default_voice
                self.config_data["tts_gender"] = atc_phrase.voice_gender(default_voice)
        if hasattr(self, "var_volume"):
            self.config_data["tts_volume"] = round(float(self.var_volume.get()), 2)
        if hasattr(self, "var_speed"):
            provider = atc_phrase.tts_provider(self.config_data)
            if hasattr(self, "var_tts_provider"):
                provider = atc_phrase.tts_provider(
                    {"tts_provider": self.var_tts_provider.get()}
                )
            atc_phrase.set_tts_speed_for_provider(
                self.config_data, self.var_speed.get(), provider=provider
            )
        self.engine.config = self.config_data

    def preview_step(self, *, apply: bool = False) -> None:
        if self.selected_index is None:
            return
        # Locked steps: preview from saved data only (no Apply)
        if apply and not self._step_is_locked():
            self.apply_step()
        self._sync_identity_to_config()
        step = self._steps()[self.selected_index]
        req_id = getattr(self, "_preview_req_id", 0) + 1
        self._preview_req_id = req_id

        def work() -> None:
            try:
                ap = self._airport()
                opus, wx = atc_phrase.resolve_opus_and_metar(
                    self.config_data, ap["icao"]
                )
                cs = opus.radio_callsign if opus else "CALLSIGN"
                channel = step.get("channel") or "other"
                rwy = atc_phrase.pick_departure_runway(
                    ap,
                    wx,
                    opus,
                    self.config_data,
                    step=step,
                    mission=self.mission,
                    state=self.engine.state,
                    template=step.get("template"),
                )
                freq, mod, _ = atc_phrase.step_radio(ap, channel, step)
                voice, _ = atc_phrase.voice_for_step(self.config_data, channel, step)
                phrase = ""
                file_path = None
                if step.get("mode") == "file":
                    file_path = step.get("file")
                    text = f"[FILE] {file_path}\n\n→ {freq} {mod}"
                else:
                    phrase, _, _, _ = atc_phrase.build_flow_step_phrase(
                        ap,
                        channel,
                        step.get("template") or "radio_check",
                        cs,
                        wx,
                        rwy,
                        custom_text=step.get("text"),
                        opus=opus,
                        step=step,
                        mission=self.mission,
                        state=self.engine.state,
                        config=self.config_data,
                    )
                    spoken_footer = atc_phrase.spoken_radio_footer(phrase, voice=voice)
                    fp = ""
                    if opus and opus.fp_route_string:
                        fp = f" · FP {opus.fp_route_string}"
                    text = (
                        f"{phrase}\n\n"
                        f"{spoken_footer}\n\n"
                        f"→ {freq} {mod} · {cs} · rwy {rwy} · voice {voice}{fp}"
                    )

                def done() -> None:
                    if getattr(self, "_preview_req_id", 0) != req_id:
                        return
                    self._last_preview_channel = channel
                    self._last_preview_file = str(file_path) if file_path else None
                    self._set_preview_display(text, base_phrase=phrase or None)

                self.after(0, done)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)

                def fail() -> None:
                    if getattr(self, "_preview_req_id", 0) != req_id:
                        return
                    if apply:
                        messagebox.showerror("Preview", err)
                    else:
                        self._set_preview_display(
                            f"(preview unavailable)\n{err}", base_phrase=""
                        )

                self.after(0, fail)

        threading.Thread(target=work, daemon=True).start()

    def hear_preview_local(self) -> None:
        """Play current step on local speakers (no SRS)."""
        if self.selected_index is None:
            messagebox.showinfo("Hear", "Select a step first.")
            return
        if not self._step_is_locked():
            self.apply_step()
        self._sync_identity_to_config()
        step = self._steps()[self.selected_index]
        # Prefer the phrase currently shown so Hear matches Regenerate (no re-roll).
        shown_phrase = self._phrase_from_preview_box()

        def work() -> None:
            try:
                ap = self._airport()
                channel = step.get("channel") or "other"
                if step.get("mode") == "file":
                    path = step.get("file") or self._last_preview_file
                    if not path:
                        raise RuntimeError("No audio file set on this step")
                    atc_phrase.preview_file_local(str(path))
                    return

                opus, wx = atc_phrase.resolve_opus_and_metar(
                    self.config_data, ap["icao"]
                )
                cs = opus.radio_callsign if opus else "CALLSIGN"
                rwy = atc_phrase.pick_departure_runway(
                    ap,
                    wx,
                    opus,
                    self.config_data,
                    step=step,
                    mission=self.mission,
                    state=self.engine.state,
                    template=step.get("template"),
                )
                phrase, _, _, _ = atc_phrase.build_flow_step_phrase(
                    ap,
                    channel,
                    step.get("template") or "radio_check",
                    cs,
                    wx,
                    rwy,
                    custom_text=shown_phrase or step.get("text"),
                    opus=opus,
                    step=step,
                    mission=self.mission,
                    state=self.engine.state,
                    config=self.config_data,
                )
                voice, _ = atc_phrase.voice_for_step(self.config_data, channel, step)
                freq, mod, _ = atc_phrase.step_radio(ap, channel, step)
                vol = float(self.config_data.get("tts_volume", 0.8))
                speed = atc_phrase.tts_speed_for_step(self.config_data, step=step)
                provider = atc_phrase.tts_provider(self.config_data)
                google_creds = atc_phrase.google_credentials_path(self.config_data)
                use_google = (
                    self._atc_role() != "client"
                    and provider == "google"
                    and google_creds is not None
                )

                def show() -> None:
                    self._last_preview_channel = channel
                    eng = "Google" if use_google else "Windows"
                    spoken_footer = atc_phrase.spoken_radio_footer(phrase, voice=voice)
                    note = (
                        f"{phrase}\n\n"
                        f"{spoken_footer}\n\n"
                        f"→ local speakers · {eng} · {freq} {mod} · {voice} · speed {speed}"
                    )
                    self._set_preview_display(note, base_phrase=phrase)

                self._ui_call(show)
                atc_phrase.preview_voice_local(
                    voice,
                    phrase,
                    vol,
                    speed=speed,
                    google_credentials=google_creds if use_google else None,
                )
                if use_google:
                    self.after(0, self._refresh_tts_usage)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: messagebox.showerror("Hear locally", e))

        threading.Thread(target=work, daemon=True).start()

    def hear_preview_srs(self) -> None:
        """Transmit current step to SRS (same as a live Fly play of this step)."""
        if self.selected_index is None:
            messagebox.showinfo("TX preview", "Select a step first.")
            return
        if not self._step_is_locked():
            self.apply_step()
        self._sync_identity_to_config()
        save_json(CONFIG_PATH, self.config_data)
        step = dict(self._steps()[self.selected_index])
        # Match on-screen phrase (Regenerate) without forcing Custom mode save.
        # Do not paste Preview text onto a File step — that would ignore the audio.
        if (step.get("mode") or "").lower() != "file":
            shown = self._phrase_from_preview_box()
            if shown and not step.get("text"):
                step["text"] = shown
        if (step.get("mode") or "").lower() == "file":
            path = str(step.get("file") or "").strip()
            if not path:
                messagebox.showerror(
                    "TX preview",
                    "This step is set to File but no audio path is saved.\n"
                    "Browse for a file (and Apply if the step was locked).",
                )
                return
            if not Path(path).is_file():
                messagebox.showerror("TX preview", f"Audio file not found:\n{path}")
                return

        def work() -> None:
            try:
                # Reuse live engine (unsaved mission + caches) — avoid cold FlowEngine()
                self.engine.config = self.config_data
                self.engine.airports = self.airports
                self.engine.mission = self.mission
                detail = self.engine.play_step(step)
                code = int(detail.get("exit_code") or 0)
                if code != 0:
                    raise RuntimeError(
                        f"Transmit failed (exit {code}). "
                        "Check ExternalAudio path, SRS host, and the audio file."
                    )

                def done() -> None:
                    phrase = str(detail.get("text") or detail.get("file") or "")
                    voice = str(detail.get("voice") or "")
                    body = f"{phrase}\n\n"
                    if detail.get("text"):
                        body += (
                            atc_phrase.spoken_radio_footer(str(detail["text"]), voice=voice)
                            + "\n\n"
                        )
                    body += (
                        f"→ TX {detail.get('freq')} · {detail.get('callsign')} · "
                        f"voice {voice}"
                    )
                    if detail.get("tts_speed") is not None:
                        body += f" · speed {detail.get('tts_speed')}"
                    if detail.get("file"):
                        body += f"\n\n(file) {detail.get('file')}"
                    base = str(detail["text"]) if detail.get("text") else ""
                    self._set_preview_display(body, base_phrase=base)
                    self._last_preview_channel = step.get("channel") or "other"

                self.after(0, done)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: messagebox.showerror("TX preview", e))

        threading.Thread(target=work, daemon=True).start()

    # ---------- Fly tab ----------
    def _build_fly(self) -> None:
        """Kneeboard-friendly Fly view: large step name + next freq, big Play controls."""
        f = self.tab_fly
        self.fly_mission = tk.StringVar(value="")
        self.fly_step_num = tk.StringVar(value="")
        self.fly_step_name = tk.StringVar(value="…")
        self.fly_channel = tk.StringVar(value="")
        self.fly_freq = tk.StringVar(value="—")
        self.fly_mod = tk.StringVar(value="")
        self.fly_tx_name = tk.StringVar(value="")
        self.fly_say = tk.StringVar(value="")
        self.fly_hint = tk.StringVar(value="")
        self.fly_recovery = tk.StringVar(
            value=atc_phrase.recovery_label(atc_phrase.DEFAULT_RECOVERY)
        )
        self.fly_approach_detail = tk.StringVar(value="")
        self.always_on_top = tk.BooleanVar(value=False)
        self._fly_phrase_req_id = 0
        self._fly_recovery_loading = False

        # Scrollable shell — YOU CAN SAY + frequency + play controls no longer
        # fit a single screen, so the whole Fly page scrolls like Plan.
        scroll_host = tk.Frame(f, bg=C_BG)
        scroll_host.pack(fill=tk.BOTH, expand=True)
        self._fly_canvas = tk.Canvas(scroll_host, bg=C_BG, highlightthickness=0, bd=0)
        fly_vsb = ttk.Scrollbar(scroll_host, orient=tk.VERTICAL, command=self._fly_canvas.yview)
        self._fly_canvas.configure(yscrollcommand=fly_vsb.set)
        fly_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._fly_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        shell_host = tk.Frame(self._fly_canvas, bg=C_BG)
        self._fly_shell_window = self._fly_canvas.create_window(
            (0, 0), window=shell_host, anchor="nw"
        )
        shell = tk.Frame(shell_host, bg=C_BG)
        shell.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)
        self.fly_net = tk.StringVar(value="")
        self._fly_net_lbl = tk.Label(
            shell,
            textvariable=self.fly_net,
            bg=C_BG,
            fg=C_AMBER,
            font=("Segoe UI Semibold", 10),
            anchor="w",
        )
        self._fly_net_lbl.pack(fill=tk.X, pady=(0, 8))

        def _fly_shell_cfg(_event: tk.Event | None = None) -> None:
            self._fly_canvas.configure(scrollregion=self._fly_canvas.bbox("all"))

        def _fly_canvas_cfg(event: tk.Event) -> None:
            self._fly_canvas.itemconfigure(self._fly_shell_window, width=event.width)

        shell_host.bind("<Configure>", _fly_shell_cfg)
        self._fly_canvas.bind("<Configure>", _fly_canvas_cfg)

        def _fly_wheel(event: tk.Event) -> str | None:
            try:
                if self.nb.index(self.nb.select()) != self.nb.index(self.tab_fly):
                    return None
            except tk.TclError:
                return None
            if self._wheel_owned_elsewhere(event):
                return None
            delta = int(-1 * (event.delta / 120)) if event.delta else 0
            if delta:
                self._fly_canvas.yview_scroll(delta, "units")
            return "break"

        self._fly_wheel = _fly_wheel

        # Bind wheel on the embedded shell too — canvas Leave fires when the
        # pointer moves onto the windowed child, which would otherwise kill scroll.
        for widget in (self._fly_canvas, shell_host, shell):
            widget.bind("<Enter>", lambda _e: self._fly_canvas.bind_all("<MouseWheel>", _fly_wheel))
            widget.bind("<Leave>", lambda _e: self._fly_maybe_unbind_wheel())

        tk.Label(
            shell,
            textvariable=self.fly_mission,
            bg=C_BG,
            fg=C_MUTED,
            font=("Segoe UI", 12),
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 6))

        # Upcoming step card — step name is primary for in-cockpit glance
        card = tk.Frame(shell, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        card.pack(fill=tk.X, pady=(0, 12))

        hdr = tk.Frame(card, bg=C_PANEL)
        hdr.pack(fill=tk.X, padx=20, pady=(12, 0))
        tk.Label(
            hdr,
            text="NEXT TRANSMIT",
            bg=C_PANEL,
            fg=C_AMBER,
            font=("Segoe UI Semibold", 12),
        ).pack(side=tk.LEFT)
        ttk.Button(
            hdr,
            text="PLAY AND ADVANCE",
            style="FlyPlay.TButton",
            command=lambda: self._fly("next", bypass_freq_gate=False),
        ).pack(side=tk.LEFT, padx=(14, 0))
        ttk.Button(
            hdr,
            text="PLAY PREVIOUS",
            style="FlyBack.TButton",
            command=lambda: self._fly("back", bypass_freq_gate=False),
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(
            hdr, text="Keywords…", command=self._open_fly_keywords
        ).pack(side=tk.LEFT, padx=(6, 0))
        step_nav = tk.Frame(hdr, bg=C_PANEL)
        step_nav.pack(side=tk.RIGHT)
        ttk.Button(
            step_nav,
            text="◀",
            width=3,
            command=lambda: self._fly("seek_prev"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(
            step_nav,
            textvariable=self.fly_step_num,
            bg=C_PANEL,
            fg=C_AMBER,
            font=("Segoe UI Semibold", 16),
        ).pack(side=tk.LEFT)
        ttk.Button(
            step_nav,
            text="▶",
            width=3,
            command=lambda: self._fly("seek_next"),
        ).pack(side=tk.LEFT, padx=(6, 0))

        self._fly_step_name_lbl = tk.Label(
            card,
            textvariable=self.fly_step_name,
            bg=C_PANEL,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 26),
            wraplength=960,
            justify=tk.LEFT,
            anchor="w",
        )
        self._fly_step_name_lbl.pack(fill=tk.X, padx=20, pady=(4, 8))

        # Frequency hero + glanceable voice feed (left = tune, right = last heard)
        freq_box = tk.Frame(card, bg="#0a0e14", highlightbackground=C_GREEN, highlightthickness=2)
        freq_box.pack(fill=tk.X, padx=20, pady=(0, 8))
        self._fly_freq_box = freq_box
        freq_split = tk.Frame(freq_box, bg="#0a0e14")
        freq_split.pack(fill=tk.BOTH, expand=True)
        freq_split.columnconfigure(0, weight=3, minsize=280)
        freq_split.columnconfigure(1, weight=2, minsize=220)
        freq_left = tk.Frame(freq_split, bg="#0a0e14")
        freq_left.grid(row=0, column=0, sticky="nsew")
        freq_right = tk.Frame(freq_split, bg="#0a0e14")
        freq_right.grid(row=0, column=1, sticky="nsew", padx=(8, 10), pady=(8, 10))

        tk.Label(
            freq_left,
            text="NEXT TX FREQUENCY",
            bg="#0a0e14",
            fg=C_MUTED,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", padx=14, pady=(8, 0))
        freq_row = tk.Frame(freq_left, bg="#0a0e14")
        freq_row.pack(fill=tk.X, padx=14, pady=(0, 2))
        self._fly_freq_lbl = tk.Label(
            freq_row,
            textvariable=self.fly_freq,
            bg="#0a0e14",
            fg=C_GREEN,
            font=("Consolas", 34, "bold"),
            anchor="w",
        )
        self._fly_freq_lbl.pack(side=tk.LEFT)
        self._fly_mod_lbl = tk.Label(
            freq_row,
            textvariable=self.fly_mod,
            bg="#0a0e14",
            fg=C_TEXT,
            font=("Segoe UI Semibold", 16),
            anchor="w",
        )
        self._fly_mod_lbl.pack(side=tk.LEFT, padx=(14, 0), pady=(8, 0))

        self._fly_channel_lbl = tk.Label(
            freq_left,
            textvariable=self.fly_channel,
            bg="#0a0e14",
            fg=C_ACCENT,
            font=("Segoe UI Semibold", 16),
            anchor="w",
        )
        self._fly_channel_lbl.pack(fill=tk.X, padx=14, pady=(0, 2))
        tk.Label(
            freq_left,
            textvariable=self.fly_tx_name,
            bg="#0a0e14",
            fg=C_MUTED,
            font=("Segoe UI", 11),
            anchor="w",
        ).pack(fill=tk.X, padx=14, pady=(0, 4))
        self.fly_tuned_now = tk.StringVar(value="YOU ARE ON  ·  radio tune unknown")
        self._fly_tuned_now_lbl = tk.Label(
            freq_left,
            textvariable=self.fly_tuned_now,
            bg="#0a0e14",
            fg=C_GREEN,
            font=("Segoe UI Semibold", 12),
            anchor="w",
            wraplength=520,
            justify=tk.LEFT,
        )
        self._fly_tuned_now_lbl.pack(fill=tk.X, padx=14, pady=(0, 2))
        self.fly_next_radio = tk.StringVar(value="NEXT STEP  ·  —")
        self._fly_next_radio_lbl = tk.Label(
            freq_left,
            textvariable=self.fly_next_radio,
            bg="#0a0e14",
            fg=C_AMBER,
            font=("Segoe UI", 11),
            anchor="w",
            wraplength=520,
            justify=tk.LEFT,
        )
        self._fly_next_radio_lbl.pack(fill=tk.X, padx=14, pady=(0, 2))
        self.fly_freq_gate = tk.StringVar(value="Radio tune unknown — gate open")
        self._fly_freq_gate_lbl = tk.Label(
            freq_left,
            textvariable=self.fly_freq_gate,
            bg="#0a0e14",
            fg=C_MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        )
        self._fly_freq_gate_lbl.pack(fill=tk.X, padx=14, pady=(0, 2))
        self.fly_position = tk.StringVar(value="")
        self._fly_position_lbl = tk.Label(
            freq_left,
            textvariable=self.fly_position,
            bg="#0a0e14",
            fg=C_MUTED,
            font=("Segoe UI", 10),
            anchor="w",
        )
        self._fly_position_lbl.pack(fill=tk.X, padx=14, pady=(0, 2))
        self.fly_boom = tk.StringVar(value="")
        self._fly_boom_lbl = tk.Label(
            freq_left,
            textvariable=self.fly_boom,
            bg="#0a0e14",
            fg=C_MUTED,
            font=("Segoe UI", 10),
            anchor="w",
            wraplength=420,
            justify=tk.LEFT,
        )
        self._fly_boom_lbl.pack(fill=tk.X, padx=14, pady=(0, 10))

        tk.Label(
            freq_right,
            text="LAST HEARD",
            bg="#0a0e14",
            fg=C_MUTED,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w")
        self.fly_voice_feed = tk.Text(
            freq_right,
            height=7,
            bg="#070a0f",
            fg=C_TEXT,
            insertbackground=C_TEXT,
            font=("Consolas", 9),
            relief=tk.FLAT,
            wrap=tk.WORD,
            highlightthickness=1,
            highlightbackground=C_BORDER,
            padx=6,
            pady=4,
        )
        self.fly_voice_feed.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.fly_voice_feed.tag_configure("no_tx", foreground=C_AMBER)
        self.fly_voice_feed.tag_configure("texaco", foreground=C_GREEN)
        self.fly_voice_feed.tag_configure("mic_ignore", foreground=C_MUTED)
        self.fly_voice_feed.insert(
            tk.END,
            "Release PTT to see what Whisper heard. Script Next/Back shows TX or why it did not fire.\n",
        )
        self.fly_voice_feed.configure(state=tk.DISABLED)

        # EAM test radios — shown when Setup → External AWACS radio source is on
        self._fly_eam_box = tk.Frame(
            card, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1
        )
        eam_hdr = tk.Frame(self._fly_eam_box, bg=C_PANEL)
        eam_hdr.pack(fill=tk.X, padx=10, pady=(8, 4))
        tk.Label(
            eam_hdr,
            text="EAM RADIOS (testing)",
            bg=C_PANEL,
            fg=C_AMBER,
            font=("Segoe UI Semibold", 11),
        ).pack(side=tk.LEFT)
        ttk.Button(eam_hdr, text="Tune to step", command=self._tune_eam_to_step).pack(
            side=tk.RIGHT, padx=(4, 0)
        )
        ttk.Button(eam_hdr, text="Reseed airport", command=self._reseed_eam_freqs).pack(
            side=tk.RIGHT
        )
        self._fly_eam_hint = tk.StringVar(
            value="SRS live tune: waiting for SR-ClientRadio… (radio buttons are fallback)"
        )
        tk.Label(
            self._fly_eam_box,
            textvariable=self._fly_eam_hint,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill=tk.X, padx=10, pady=(0, 2))
        self._fly_eam_rows = tk.Frame(self._fly_eam_box, bg=C_PANEL)
        self._fly_eam_rows.pack(fill=tk.X, padx=10, pady=(0, 8))
        self._eam_freq_vars: list[tk.StringVar] = []
        try:
            eam_active = int(self.config_data.get("freq_gate_eam_active", 0) or 0)
        except (TypeError, ValueError):
            eam_active = 0
        self.var_eam_active = tk.IntVar(value=eam_active)

        # After ATC transmits, show the items the pilot must read back — same
        # top slot as YOU CAN SAY (mutually exclusive; readback wins when open).
        self.fly_readback_title = tk.StringVar(value="READ BACK")
        self.fly_readback_frame = tk.Frame(
            card, bg="#14100a", highlightbackground=C_AMBER, highlightthickness=2
        )
        tk.Label(
            self.fly_readback_frame,
            textvariable=self.fly_readback_title,
            bg="#14100a",
            fg=C_AMBER,
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=14, pady=(10, 2))
        tk.Label(
            self.fly_readback_frame,
            text="Required — say the highlighted items. Agency name optional. "
            "You can still ask for winds or a runway change.",
            bg="#14100a",
            fg=C_MUTED,
            font=("Segoe UI", 10),
            anchor="w",
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor="w", padx=14, pady=(0, 6))
        self.fly_readback_body = tk.Frame(self.fly_readback_frame, bg="#14100a")
        self.fly_readback_body.pack(fill=tk.X, padx=14, pady=(0, 12))

        # Voice prompts first (what the pilot says), then ATC expected response.
        self.fly_say_title = tk.StringVar(value="VOICE CUES")
        self.fly_say_frame = tk.Frame(
            card, bg="#0a0e14", highlightbackground=C_ACCENT, highlightthickness=2
        )
        tk.Label(
            self.fly_say_frame,
            textvariable=self.fly_say_title,
            bg="#0a0e14",
            fg=C_ACCENT,
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=16, pady=(12, 2))
        self.fly_say_subtitle = tk.StringVar(
            value="You must address the agency for a call to be recognized — except readbacks."
        )
        tk.Label(
            self.fly_say_frame,
            textvariable=self.fly_say_subtitle,
            bg="#0a0e14",
            fg=C_MUTED,
            font=("Segoe UI", 10),
            anchor="w",
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor="w", padx=16, pady=(0, 6))
        self.fly_say_body = tk.Frame(self.fly_say_frame, bg="#0a0e14")
        self.fly_say_body.pack(fill=tk.X, padx=14, pady=(0, 10))

        # What ATC will transmit on the next Play / advance
        self._fly_will_say_box = tk.Frame(
            card, bg=C_CARD, highlightbackground=C_BORDER, highlightthickness=1
        )
        self._fly_will_say_box.pack(fill=tk.X, padx=20, pady=(0, 8))
        tk.Label(
            self._fly_will_say_box,
            text="EXPECTED RESPONSE",
            bg=C_CARD,
            fg=C_MUTED,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", padx=12, pady=(8, 0))
        tk.Label(
            self._fly_will_say_box,
            textvariable=self.fly_say,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Segoe UI", 13),
            wraplength=920,
            justify=tk.LEFT,
            anchor="nw",
        ).pack(fill=tk.X, padx=12, pady=(2, 10))

        # Recovery — only shown on Approach (button strip, not modal)
        self.fly_takeoff_mode = tk.StringVar(value=atc_phrase.takeoff_mode_label("lineup"))
        self._fly_rec_box = tk.Frame(card, bg=C_PANEL)
        tk.Label(
            self._fly_rec_box,
            text="RECOVERY",
            bg=C_PANEL,
            fg=C_AMBER,
            font=("Segoe UI Semibold", 12),
        ).pack(side=tk.LEFT, padx=(0, 10))
        self._fly_rec_btns = tk.Frame(self._fly_rec_box, bg=C_PANEL)
        self._fly_rec_btns.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            self._fly_rec_box,
            textvariable=self.fly_recovery,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 10),
        ).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(
            self._fly_rec_box,
            textvariable=self.fly_approach_detail,
            bg=C_PANEL,
            fg=C_GREEN,
            font=("Consolas", 9),
        ).pack(side=tk.LEFT, padx=(10, 0))
        # Built later via _sync_fly_recovery_ui; hidden until Approach

        # Tower rolling offer — Accept / Deny buttons when pending
        self._fly_offer_fr = tk.Frame(card, bg=C_PANEL)
        tk.Label(
            self._fly_offer_fr,
            text="TOWER OFFER",
            bg=C_PANEL,
            fg=C_AMBER,
            font=("Segoe UI Semibold", 12),
        ).pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(
            self._fly_offer_fr,
            text="Will you accept rolling?   Next = yes  ·  Prev = no",
            bg=C_PANEL,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 12),
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Button(
            self._fly_offer_fr,
            text="Accept rolling",
            style="Accent.TButton",
            command=lambda: self._apply_fly_pilot_request("accept_rolling"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            self._fly_offer_fr,
            text="Deny",
            command=lambda: self._apply_fly_pilot_request("deny_rolling"),
        ).pack(side=tk.LEFT)

        # Pilot requests — frequency-scoped button strip (hidden when empty)
        self._fly_req_box = tk.Frame(card, bg=C_PANEL)
        tk.Label(
            self._fly_req_box,
            text="PILOT REQUEST",
            bg=C_PANEL,
            fg=C_AMBER,
            font=("Segoe UI Semibold", 12),
        ).pack(side=tk.LEFT, padx=(0, 10))
        self._fly_req_btns = tk.Frame(self._fly_req_box, bg=C_PANEL)
        self._fly_req_btns.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            self._fly_req_box,
            textvariable=self.fly_takeoff_mode,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 10),
        ).pack(side=tk.LEFT, padx=(8, 0))

        tk.Label(
            card,
            textvariable=self.fly_hint,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 11),
            wraplength=900,
            justify=tk.LEFT,
            anchor="w",
        ).pack(fill=tk.X, padx=20, pady=(0, 14))

        nav = tk.Frame(shell, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        nav.pack(fill=tk.X, pady=(0, 10))
        inner = tk.Frame(nav, bg=C_PANEL)
        inner.pack(fill=tk.X, padx=12, pady=10)
        tk.Label(
            inner,
            text="Jump to step (no transmit)",
            bg=C_PANEL,
            fg=C_LABEL,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", pady=(0, 6))
        row = tk.Frame(inner, bg=C_PANEL)
        row.pack(fill=tk.X)
        self.var_jump = tk.StringVar()
        self._lbl_jump = tk.Label(
            row,
            textvariable=self.var_jump,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Segoe UI", 10),
            anchor="w",
            padx=8,
            pady=4,
            highlightbackground=C_BORDER,
            highlightthickness=1,
        )
        self._lbl_jump.pack(side=tk.LEFT, padx=(0, 6), fill=tk.X, expand=True)
        ttk.Button(row, text="Choose…", command=self._choose_jump_step).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(row, text="Go", command=self._jump_to_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(row, text="RESET", command=lambda: self._fly("reset")).pack(side=tk.LEFT)
        self._jump_index_by_label: dict[str, int] = {}

        foot = tk.Frame(shell, bg=C_BG)
        foot.pack(fill=tk.X, pady=(0, 6))
        ttk.Checkbutton(
            foot,
            text="Keep window on top (kneeboard)",
            variable=self.always_on_top,
            command=lambda: self.attributes("-topmost", self.always_on_top.get()),
        ).pack(side=tk.LEFT)
        self.fly_hotkey_hint = tk.StringVar(value="")
        tk.Label(
            foot,
            textvariable=self.fly_hotkey_hint,
            bg=C_BG,
            fg=C_MUTED,
            font=("Segoe UI", 9),
        ).pack(side=tk.RIGHT)

        # Compact last-action log (fixed height — shell scrolls, log must not expand)
        tk.Label(shell, text="Last actions", bg=C_BG, fg=C_MUTED, font=("Segoe UI", 10)).pack(
            anchor="w", pady=(4, 2)
        )
        self.fly_log = tk.Text(
            shell,
            height=4,
            bg=C_CARD,
            fg=C_MUTED,
            font=("Consolas", 10),
            relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self.fly_log.pack(fill=tk.X)
        self._sync_fly_eam_ui()
        self._refresh_fly_status()
        self.after_idle(self._fly_update_scrollregion)

    def _fly_upcoming_radio(self, step: dict[str, Any] | None) -> tuple[str, str, str, str]:
        """Return (channel_label, freq_display, mod, tx_name) for the next step."""
        if not step:
            return ("—", "—", "", "")
        channel = str(step.get("channel") or step.get("phase") or "other").strip().lower()
        try:
            ap = self.engine.airport()
        except Exception:
            key = self.mission.get("airport") or self.config_data.get("default_airport") or "nellis"
            ap = self.airports.get(key) or next(iter(self.airports.values()))
        freq, mod, tx_name = atc_phrase.step_radio(
            ap,
            channel,
            step,
            state=getattr(self.engine, "state", None),
            config=self.config_data,
        )
        # UHF/VFR style: always three decimals for glanceable kneeboard read
        freq_disp = f"{float(freq):.3f}"
        ch_label = channel.upper()
        return ch_label, freq_disp, str(mod or "AM").upper(), str(tx_name or "")

    def _queue_fly_phrase_preview(self, step: dict[str, Any] | None) -> None:
        """Fill EXPECTED RESPONSE with the upcoming radio phrase (async; may re-roll)."""
        req_id = getattr(self, "_fly_phrase_req_id", 0) + 1
        self._fly_phrase_req_id = req_id
        if not step:
            self.fly_say.set("")
            return
        if (step.get("mode") or "tts").lower() == "file":
            path = step.get("file") or ""
            self.fly_say.set(f"[FILE] {path}" if path else "[FILE] (no file set)")
            return
        self.fly_say.set("Loading phrase…")
        step_snapshot = dict(step)
        cfg = dict(self.config_data)

        def work() -> None:
            try:
                try:
                    ap = self.engine.airport()
                except Exception:
                    key = self.mission.get("airport") or cfg.get("default_airport") or "nellis"
                    ap = self.airports.get(key) or next(iter(self.airports.values()))
                opus, wx = atc_phrase.resolve_opus_and_metar(cfg, ap["icao"])
                cs = opus.radio_callsign if opus else "CALLSIGN"
                channel = step_snapshot.get("channel") or "other"
                rwy = atc_phrase.pick_departure_runway(
                    ap,
                    wx,
                    opus,
                    cfg,
                    step=step_snapshot,
                    mission=self.mission,
                    state=self.engine.state,
                    template=step_snapshot.get("template"),
                )
                phrase, _, _, _ = atc_phrase.build_flow_step_phrase(
                    ap,
                    channel,
                    step_snapshot.get("template") or "radio_check",
                    cs,
                    wx,
                    rwy,
                    custom_text=step_snapshot.get("text"),
                    opus=opus,
                    step=step_snapshot,
                    mission=self.mission,
                    state=self.engine.state,
                    config=self.config_data,
                )
                text = (phrase or "").strip() or "(empty phrase)"
            except Exception as exc:  # noqa: BLE001
                text = f"(phrase unavailable: {exc})"

            def done() -> None:
                if getattr(self, "_fly_phrase_req_id", 0) != req_id:
                    return
                self.fly_say.set(text)

            self.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _fly_recovery_options(self) -> list[tuple[str, str]]:
        """Recovery choices for Fly — mission.recovery_options or full set."""
        allowed = self.mission.get("recovery_options")
        if isinstance(allowed, list) and allowed:
            keys = [atc_phrase.normalize_recovery_key(x) for x in allowed]
            out = [(k, lab) for k, lab in atc_phrase.RECOVERY_CHOICES if k in keys]
            return out or list(atc_phrase.RECOVERY_CHOICES)
        return list(atc_phrase.RECOVERY_CHOICES)

    _FLY_RECOVERY_BTN_LABELS: dict[str, str] = {
        "visual_overhead": "Overhead",
        "tactical_overhead": "Tactical",
        "straight_in": "Straight-in",
        "instrument": "Instrument",
    }
    _FLY_REQUEST_BTN_LABELS: dict[str, str] = {
        "accept_rolling": "Accept rolling",
        "deny_rolling": "Deny rolling",
        "request_rolling": "Request rolling",
        "request_lineup": "Request LUAW",
        "request_landing": "Gear down full stop",
        "request_low_approach": "The option",
        "request_go_around": "On the go",
        "request_handoff": "Request handoff",
        "request_tanker": "Request tanker",
        "tanker_return": "Back from tanker",
        "tanker_check_in": "Request rejoin",
        "tanker_tacan": "Say TACAN",
        "tanker_freq": "Say frequency",
        "tanker_bullseye": "Say bullseye",
        "tanker_dcs_precontact": "DCS: Ready pre-contact",
        "tanker_dcs_abort": "DCS: Abort / disconnect",
        "tanker_chat_start": "Texaco starts chat",
        "tanker_chat_stop": "Stop chat",
        "clear_runway_request": "Reset runway to winds",
    }

    def _sync_fly_recovery_ui(self, channel: str | None = None) -> None:
        """Show recovery buttons on Approach, and VFR patterns on approach Tower."""
        if not hasattr(self, "_fly_rec_box"):
            return
        ch, phase, _tmpl = self._fly_request_context(channel)
        active = atc_phrase.resolve_active_recovery(
            None, self.mission, state=self.engine.state
        )
        plan = atc_phrase.approach_plan_from_state(
            self.engine.state, airport=self.engine.airport()
        )
        detail_bits: list[str] = []
        if plan.get("vfr_recovery"):
            detail_bits.append(str(plan.get("vfr_recovery_say") or plan["vfr_recovery"]))
        if plan.get("iaf"):
            detail_bits.append(f"IAF {plan.get('iaf_say') or plan['iaf']}")
        if plan.get("runway"):
            detail_bits.append(f"RWY {plan['runway']}")
        if plan.get("descend_ft"):
            detail_bits.append(f"{plan['descend_ft']}ft")
        if plan.get("speed_restrict") and plan.get("speed_kt"):
            detail_bits.append(f"{plan['speed_kt']}kt")
        if plan.get("source"):
            detail_bits.append(str(plan["source"]))
        if hasattr(self, "fly_approach_detail"):
            self.fly_approach_detail.set(" · ".join(detail_bits))
        self._fly_recovery_loading = True
        self.fly_recovery.set(atc_phrase.recovery_label(active))
        self._fly_recovery_loading = False

        for child in self._fly_rec_btns.winfo_children():
            child.destroy()

        if ch == "approach":
            opts = self._fly_recovery_options()
        elif ch == "tower" and phase == "approach":
            opts = [
                (k, lab)
                for k, lab in self._fly_recovery_options()
                if k != "instrument"
            ]
        else:
            self._fly_rec_box.pack_forget()
            return

        if not opts:
            self._fly_rec_box.pack_forget()
            return
        for key, lab in opts:
            short = self._FLY_RECOVERY_BTN_LABELS.get(key, lab)
            style = "Accent.TButton" if key == active else "TButton"
            ttk.Button(
                self._fly_rec_btns,
                text=short,
                style=style,
                command=lambda k=key: self._set_fly_recovery(k),
            ).pack(side=tk.LEFT, padx=(0, 6))

        self._fly_rec_box.pack(fill=tk.X, padx=20, pady=(0, 8))

    def _set_fly_recovery(self, key: str) -> None:
        key = atc_phrase.normalize_recovery_key(key)
        self.fly_recovery.set(atc_phrase.recovery_label(key))
        self.engine.state["active_recovery"] = key
        self.mission["active_recovery"] = key
        try:
            airport = self.engine.airport()
            opus, weather = atc_phrase.resolve_opus_and_metar(
                self.config_data, airport["icao"]
            )
            atc_phrase.assign_approach_plan(
                airport,
                weather,
                mission=self.mission,
                state=self.engine.state,
                opus=opus,
                force=True,
                recovery=key,
                position=atc_phrase.ownship_latlon(
                    self.config_data, opus=opus, state=self.engine.state
                ),
            )
            self.engine.save_state()
        except Exception:
            try:
                self.engine.save_state()
            except Exception:
                pass
        for step in self._steps():
            if (step.get("template") or "") == "approach_check_in" and not step.get("text"):
                step["recovery"] = key
            if (step.get("template") or "") == "cleared_approach" and not step.get("text"):
                step["approach_pattern"] = key
        self._sync_fly_recovery_ui()
        self._queue_fly_phrase_preview(self.engine.current_step())

    def _choose_fly_recovery(self) -> None:
        """Legacy entry — recovery is button-driven on Approach now."""
        opts = self._fly_recovery_options()
        if not opts:
            return
        # Prefer currently active; otherwise first option
        active = atc_phrase.resolve_active_recovery(None, self.mission, state=self.engine.state)
        self._set_fly_recovery(active or opts[0][0])

    def _on_fly_recovery_change(self, _evt: object | None = None) -> None:
        if getattr(self, "_fly_recovery_loading", False):
            return
        lab = self.fly_recovery.get().strip()
        key = next((k for k, l in atc_phrase.RECOVERY_CHOICES if l == lab), None)
        self._set_fly_recovery(key or lab)

    def _fly_current_channel(self, st: dict[str, Any] | None = None) -> str:
        st = st or {}
        if st.get("at_end"):
            return ""
        step = st.get("step") if st else None
        if not step:
            step = self.engine.current_step() or {}
        try:
            import tanker as tanker_mod

            eng_st = getattr(self.engine, "state", None)
            if tanker_mod.tanker_overlay_active(eng_st) and tanker_mod.is_tanker_step(
                step if isinstance(step, dict) else None
            ):
                return "tanker"
        except Exception:
            pass
        return str(step.get("channel") or step.get("phase") or "other").strip().lower() or "other"

    def _fly_request_radio_channel(self, st: dict[str, Any] | None = None) -> str:
        """Agency for PILOT REQUEST buttons — live tune, else the flow cursor."""
        try:
            ap = self.engine.airport()
        except Exception:
            ap = self._airport()
        try:
            tuned = srs_radio.channel_for_tuned_freq(ap, self.config_data) or ""
        except Exception:
            tuned = ""
        if str(tuned).strip():
            return str(tuned).strip().lower()
        return self._fly_current_channel(st)

    def _fly_request_context(self, channel: str | None = None) -> tuple[str, str, str]:
        """(channel, mission_phase, template) for the Fly cursor step."""
        step = self.engine.current_step() or {}
        ch = (
            channel
            if channel is not None
            else self._fly_request_radio_channel()
        ).strip().lower()
        phase = atc_phrase.resolve_pilot_request_phase(
            phase=str(step.get("phase") or ""),
            channel=ch or str(step.get("channel") or ""),
            template=str(step.get("template") or ""),
        )
        tmpl = str(step.get("template") or "")
        return ch, phase, tmpl

    def _sync_fly_pilot_request_ui(self, channel: str | None = None) -> None:
        """Show Tower offer + pilot-request buttons for the current freq + phase."""
        if not hasattr(self, "_fly_req_box"):
            return
        mode = atc_phrase.resolve_active_takeoff_mode(self.mission, self.engine.state)
        ch, phase, tmpl = self._fly_request_context(channel)
        if phase == "departure":
            self.fly_takeoff_mode.set(f"Takeoff: {atc_phrase.takeoff_mode_label(mode)}")
        elif phase == "approach":
            rec = atc_phrase.resolve_active_recovery(
                None, self.mission, state=self.engine.state
            )
            self.fly_takeoff_mode.set(f"Landing: {atc_phrase.recovery_label(rec)}")
        else:
            self.fly_takeoff_mode.set("")

        # Offer bar — rolling takeoff only on departure Tower
        if atc_phrase.takeoff_offer_visible(
            self.engine.state, channel=ch, phase=phase, template=tmpl
        ):
            self._fly_offer_fr.pack(fill=tk.X, padx=20, pady=(0, 6))
        else:
            self._fly_offer_fr.pack_forget()

        # Request buttons (skip Accept/Deny — those are on the offer bar)
        for child in self._fly_req_btns.winfo_children():
            child.destroy()
        reqs = [
            (k, lab)
            for k, lab in atc_phrase.pilot_requests_for_channel(
                ch,
                self.engine.state,
                airport=self._airport(),
                phase=phase,
                template=tmpl,
            )
            if k not in ("accept_rolling", "deny_rolling")
        ]
        if not reqs:
            self._fly_req_box.pack_forget()
            return

        awaiting_option = atc_phrase.awaiting_option_on_the_go(self.engine.state)
        for key, lab in reqs:
            short = self._FLY_REQUEST_BTN_LABELS.get(key, lab)
            if key == "request_landing" and awaiting_option:
                short = "Full stop"
            accent = False
            if key == "request_rolling" and mode == "rolling":
                accent = True
            if key == "request_lineup" and mode == "lineup":
                accent = True
            if key == "request_landing" and awaiting_option:
                accent = True
            if key == "request_go_around" and awaiting_option:
                accent = True
            if key == "tanker_chat_start":
                accent = True
            if key == "tanker_chat_stop":
                accent = False
                short = "Stop chat"
            ttk.Button(
                self._fly_req_btns,
                text=short,
                style="Accent.TButton" if accent else "TButton",
                command=lambda k=key: self._apply_fly_pilot_request(k),
            ).pack(side=tk.LEFT, padx=(0, 6))
        self._fly_req_box.pack(fill=tk.X, padx=20, pady=(0, 8))

    def _apply_fly_pilot_request(self, request_key: str, *, tx_ack: bool = True) -> None:
        """Apply Accept/Deny/Request — update takeoff mode and optionally TX Tower ack."""
        try:
            ap = self._airport()
            opus, weather = atc_phrase.resolve_opus_and_metar(
                self.config_data, ap["icao"]
            )
            result = atc_phrase.apply_pilot_request(
                request_key,
                mission=self.mission,
                state=self.engine.state,
                airport=ap,
                weather=weather,
                opus=opus,
                config=self.config_data,
            )
        except ValueError as exc:
            messagebox.showerror("Pilot request", str(exc))
            return
        self.engine.mission = self.mission
        # Keep Setup Manual runway field in sync when resetting to winds.
        if request_key == "clear_runway_request" and hasattr(self, "var_runway_override"):
            self.var_runway_override.set("")
        try:
            self.engine.save_state()
        except Exception:
            pass
        # Persist takeoff mode / runway request on saved missions only — never bake
        # session overrides into a *_default.json template.
        try:
            path = flow_engine.resolve_flow_path(self.config_data)
            if not flow_engine.is_base_flow_path(path):
                self.mission["name"] = self.mission_name_var.get().strip() or self.mission.get("name") or "Untitled"
                flow_engine.normalize_mission_to_steps(self.mission)
                save_json(path, self.mission)
        except Exception:
            pass
        self._sync_fly_pilot_request_ui()
        self._sync_fly_recovery_ui()
        self.engine.prepare_takeoff_cursor()
        try:
            self.engine.save_state()
        except Exception:
            pass
        answering_rolling = request_key in (
            "accept_rolling",
            "deny_rolling",
            "request_lineup",
            "request_rolling",
        ) and str(self.engine.state.get("last_tx_template") or "") == "rolling_accept"
        # Preview would race the unique accept/deny wording — skip until after TX.
        if not answering_rolling:
            self._queue_fly_phrase_preview(self.engine.current_step())
        if result.get("execute_go_around"):
            def ga_work() -> None:
                try:
                    played = self.engine.execute_go_around(bypass_freq_gate=True)
                    self.after(0, lambda: self._on_go_around_done(played))
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    self.after(
                        0,
                        lambda m=err: messagebox.showerror("Go around", m),
                    )

            threading.Thread(target=ga_work, daemon=True).start()
            return
        if result.get("execute_departure_handoff"):
            def ho_work() -> None:
                try:
                    step = self.engine.current_step() or {}
                    tmpl = str(step.get("template") or "")
                    sid = str(step.get("id") or "")
                    if tmpl in ("departure_handoff", "center_handoff") and sid:
                        played = self.engine.play_id(sid, bypass_freq_gate=True)
                    else:
                        played = self.engine.play_template(
                            "departure_handoff", bypass_freq_gate=True
                        )
                    self.after(0, lambda p=played: self._on_handoff_request_done(p))
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    self.after(
                        0,
                        lambda m=err: messagebox.showerror("Handoff", m),
                    )

            threading.Thread(target=ho_work, daemon=True).start()
            return
        if result.get("execute_tanker_action"):
            action = str(result.get("execute_tanker_action") or "")

            def tk_work() -> None:
                try:
                    played = voice_engine.execute_tanker_action(
                        self.engine, action
                    )
                    self.after(0, lambda p=played: self._on_tanker_request_done(p))
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    self.after(
                        0,
                        lambda m=err: messagebox.showerror("Tanker", m),
                    )

            threading.Thread(target=tk_work, daemon=True).start()
            return
        if result.get("execute_option_full_stop"):
            def fs_work() -> None:
                try:
                    played = self.engine.accept_option_full_stop(
                        bypass_freq_gate=True
                    )
                    self.after(0, lambda: self._on_option_full_stop_done(played))
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    self.after(
                        0,
                        lambda m=err: messagebox.showerror("Full stop", m),
                    )

            threading.Thread(target=fs_work, daemon=True).start()
            return
        if not tx_ack:
            return
        # After apply_pilot_request the offer is cleared; last_tx_template still
        # says rolling_accept until the next TX. Play clearance / LUAW instead
        # of a short "rolling approved" ack.
        if answering_rolling:
            def offer_work() -> None:
                try:
                    played = voice_engine.play_rolling_offer_reply(
                        self.engine, request_key
                    )
                    if isinstance(played, dict) and played.get("action") == "none":
                        raise RuntimeError(played.get("detail") or "no takeoff step")
                    self.after(0, lambda p=played: self._on_rolling_offer_done(p))
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    self.after(
                        0,
                        lambda m=err: messagebox.showerror("Pilot request", m),
                    )

            threading.Thread(target=offer_work, daemon=True).start()
            return
        ack_kind = str(result.get("ack_kind") or "")
        if not ack_kind:
            return

        def work() -> None:
            try:
                self._sync_identity_to_config()
                ap = self._airport()
                opus, _wx = atc_phrase.resolve_opus_and_metar(self.config_data, ap["icao"])
                cs = opus.radio_callsign if opus else (
                    self.var_callsign_override.get().strip()
                    if hasattr(self, "var_callsign_override")
                    else "CALLSIGN"
                ) or "CALLSIGN"
                # Runway requests may be made on Ground / Approach too.
                req_ch = "tower"
                try:
                    cur = self.engine.current_step() or {}
                    ch_cur = str(cur.get("channel") or "").strip().lower()
                    if ch_cur in ("ground", "approach", "tower"):
                        req_ch = ch_cur
                except Exception:
                    pass
                phrase = atc_phrase.build_pilot_request_ack(
                    ack_kind,
                    ap,
                    cs,
                    runway=result.get("runway"),
                    channel=req_ch,
                )
                freq, mod, tx_name = atc_phrase.step_radio(ap, req_ch, None)
                code = atc_phrase.transmit(
                    self.config_data,
                    ap,
                    phrase,
                    tx_name,
                    freq,
                    mod,
                    channel="tower",
                )
                if code != 0:
                    raise RuntimeError(f"Transmit failed (exit {code})")

                def done() -> None:
                    self.fly_log.insert(tk.END, f"TX  Pilot request ack  ·  {freq}  ·  TOWER\n")
                    self.fly_log.see(tk.END)
                    self._refresh_fly_status()

                self.after(0, done)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: messagebox.showerror("Pilot request", e))

        threading.Thread(target=work, daemon=True).start()

    def _consume_position_fire_clears(self) -> None:
        """Drop auto-fire latches after go-around / missed so the next cycle can arm."""
        state = getattr(self.engine, "state", None)
        if not isinstance(state, dict):
            return
        raw = state.pop("clear_position_fire_substrings", None)
        if not raw:
            return
        try:
            tracker = self._position_tracker()
        except Exception:
            return
        for needle in raw if isinstance(raw, list) else [raw]:
            text = str(needle or "").strip()
            if text:
                tracker.clear_fired(text)
        try:
            self.engine.save_state()
        except Exception:
            pass

    def _on_rolling_offer_done(self, played: dict[str, Any] | None) -> None:
        played = played or {}
        detail = played.get("detail") if isinstance(played.get("detail"), dict) else played
        freq = ""
        label = "rolling reply"
        ch = "TOWER"
        if isinstance(detail, dict):
            freq = str(detail.get("freq") or "")
            label = str(detail.get("label") or detail.get("text") or label)
            ch = str(detail.get("channel") or "tower").upper()
        self.fly_log.insert(tk.END, f"TX  {label}  ·  {freq}  ·  {ch}\n")
        self.fly_log.see(tk.END)
        self._refresh_fly_status()

    def _on_go_around_done(self, played: dict[str, Any] | None) -> None:
        played = played or {}
        if played.get("acknowledged"):
            self._refresh_fly_status()
            return
        freq = played.get("freq") or ""
        plan = played.get("go_around_plan") or {}
        kind = str(plan.get("kind") or "go_around")
        self.fly_log.insert(
            tk.END, f"TX  Go around ({kind})  ·  {freq}  ·  TOWER\n"
        )
        self.fly_log.see(tk.END)
        self._consume_position_fire_clears()
        self._refresh_fly_status()

    def _on_tanker_request_done(self, played: dict[str, Any] | None) -> None:
        played = played or {}
        if played.get("action") == "blocked":
            self._note_no_tx(
                str(played.get("detail") or "Blocked"),
                action="tanker",
                channel=str(played.get("channel") or ""),
            )
            self._refresh_fly_status()
            return
        if played.get("action") == "hint":
            hint = str(played.get("detail") or played.get("text") or "DCS tanker radio")
            self.fly_log.insert(tk.END, f"DCS  {hint}\n")
            self.fly_log.see(tk.END)
            self._refresh_fly_status()
            return
        freq = played.get("freq") or ""
        label = played.get("text") or played.get("label") or "Tanker"
        ch = str(played.get("channel") or "tanker").upper()
        self.fly_log.insert(tk.END, f"TX  {label}  ·  {freq}  ·  {ch}\n")
        self.fly_log.see(tk.END)
        self._refresh_fly_status()
        deferred = played.get("deferred")
        if isinstance(deferred, dict) and deferred.get("kind") in (
            "tanker_chat",
            "tanker_chat_continue",
        ):
            self._schedule_tanker_chat(deferred)

    def _on_handoff_request_done(self, played: dict[str, Any] | None) -> None:
        played = played or {}
        freq = played.get("freq") or ""
        label = played.get("label") or "Departure handoff"
        ch = str(played.get("channel") or "departure").upper()
        self.fly_log.insert(tk.END, f"TX  {label}  ·  {freq}  ·  {ch}\n")
        self.fly_log.see(tk.END)
        self._refresh_fly_status()

    def _on_option_full_stop_done(self, played: dict[str, Any] | None) -> None:
        played = played or {}
        freq = played.get("freq") or ""
        label = played.get("label") or played.get("sought_template") or "exit"
        ch = str(played.get("channel") or "tower").upper()
        self.fly_log.insert(
            tk.END, f"TX  Option full stop → {label}  ·  {freq}  ·  {ch}\n"
        )
        self.fly_log.see(tk.END)
        self._refresh_fly_status()

    def _refresh_readback_panel(self) -> None:
        """Show the checklist of items ATC expects the pilot to read back."""
        if not hasattr(self, "fly_readback_frame"):
            return
        state = getattr(self.engine, "state", None) or {}
        items = state.get("readback_items") or []
        awaiting = bool(state.get("awaiting_readback"))
        if not awaiting or not isinstance(items, list) or not items:
            self.fly_readback_frame.pack_forget()
            return

        for child in self.fly_readback_body.winfo_children():
            child.destroy()

        hinges = atc_phrase.readback_hinge_items(
            [i for i in items if isinstance(i, dict)]
        )
        colour = [
            i
            for i in items
            if isinstance(i, dict)
            and i not in hinges
            and str(i.get("value") or "").strip()
        ]
        hinge_keys = {str(i.get("key") or "") for i in hinges}
        if hinge_keys == {"runway", "eor"}:
            self.fly_readback_title.set(
                "READ BACK  ·  runway or EOR  ·  agency optional"
            )
        elif hinge_keys == {"runway", "clearance"}:
            self.fly_readback_title.set(
                "READ BACK  ·  runway or clearance  ·  agency optional"
            )
        elif hinge_keys == {"accept", "deny"}:
            self.fly_readback_title.set(
                "ANSWER  ·  Next = accept rolling  ·  Prev = decline"
            )
        elif hinge_keys == {"squawk"}:
            self.fly_readback_title.set(
                "READ BACK  ·  “squawk” + code, code alone, or roger  ·  agency optional"
            )
        elif hinge_keys == {"climb"}:
            self.fly_readback_title.set(
                "READ BACK  ·  climb altitude  ·  agency optional"
            )
        elif hinge_keys == {"callsign"}:
            self.fly_readback_title.set(
                "READ BACK  ·  your callsign  ·  agency optional"
            )
        elif "instruction" in hinge_keys:
            self.fly_readback_title.set(
                "READ BACK  ·  closed traffic / Flex / missed  ·  agency optional"
            )
        elif len(hinges) > 1:
            self.fly_readback_title.set(
                "READ BACK  ·  either one  ·  agency optional"
            )
        else:
            self.fly_readback_title.set("READ BACK  ·  agency optional")

        def _row(item: dict[str, Any], *, hinge: bool) -> None:
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            spoken = str(item.get("spoken") or "").strip()
            if not value:
                return
            row = tk.Frame(self.fly_readback_body, bg="#14100a")
            row.pack(fill=tk.X, pady=3)
            tk.Label(
                row,
                text=f"{label}",
                bg="#14100a",
                fg=C_MUTED,
                font=("Segoe UI Semibold", 11),
                width=14,
                anchor="w",
            ).pack(side=tk.LEFT)
            tk.Label(
                row,
                text=value,
                bg="#14100a",
                fg=C_AMBER if hinge else C_MUTED,
                font=("Consolas", 22, "bold") if hinge else ("Segoe UI", 13),
                anchor="w",
            ).pack(side=tk.LEFT, padx=(4, 10))
            if spoken and spoken.casefold() != value.casefold():
                tk.Label(
                    row,
                    text=f'“{spoken}”',
                    bg="#14100a",
                    fg=C_MUTED,
                    font=("Segoe UI", 12 if hinge else 10),
                    anchor="w",
                ).pack(side=tk.LEFT)

        for idx, item in enumerate(hinges):
            if idx:
                tk.Label(
                    self.fly_readback_body,
                    text="— or —",
                    bg="#14100a",
                    fg=C_MUTED,
                    font=("Segoe UI Semibold", 11),
                    anchor="w",
                ).pack(fill=tk.X, pady=(2, 0))
            _row(item, hinge=True)
        if colour:
            tk.Label(
                self.fly_readback_body,
                text="optional colour",
                bg="#14100a",
                fg=C_MUTED,
                font=("Segoe UI", 9),
                anchor="w",
            ).pack(fill=tk.X, pady=(8, 0))
            for item in colour:
                _row(item, hinge=False)

        # Same top slot as YOU CAN SAY — above EXPECTED RESPONSE.
        try:
            self.fly_readback_frame.pack_forget()
        except tk.TclError:
            pass
        self.fly_readback_frame.pack(
            fill=tk.X, padx=20, pady=(0, 8), before=self._fly_will_say_box
        )

    def _refresh_voice_prompts(self) -> None:
        """
        Top kneeboard slot: READ BACK when a clearance is outstanding, otherwise
        TO ADVANCE / ALSO AVAILABLE cues. Same place on the card either way.
        """
        if not hasattr(self, "fly_say_frame"):
            return

        context = self._voice_context()
        awaiting = bool(context.get("awaiting_readback"))
        items = context.get("readback_items") if isinstance(context.get("readback_items"), list) else []

        # Readback replaces voice cues — required items, not optional examples.
        if awaiting and items:
            try:
                self.fly_say_frame.pack_forget()
            except tk.TclError:
                pass
            self._refresh_readback_panel()
            self._sync_fly_eam_ui()
            self.after_idle(self._fly_update_scrollregion)
            return

        airport = context.get("airport") or {}
        cue_ch = voice_intent.cue_channel(
            mission_phase=str(context.get("phase") or ""),
            cursor_channel=str(context.get("cursor_channel") or ""),
            tuned_channel=str(context.get("tuned_channel") or "") or None,
        ) or str(context.get("channel") or "")
        lines = voice_intent.suggestions(
            phase=str(context.get("phase") or ""),
            channel=cue_ch,
            expected=str(context.get("expected") or ""),
            callsign=str(context.get("callsign") or ""),
            airport_name=str(airport.get("name") or ""),
            limit=5,
            advance_limit=2,
            optional_limit=3,
            awaiting_readback=False,
            readback_items=None,
            steps=context.get("steps") if isinstance(context.get("steps"), list) else None,
            current_step_id=str(context.get("current_step_id") or ""),
            tanker_chat_choices=context.get("tanker_chat_choices")
            if isinstance(context.get("tanker_chat_choices"), list)
            else None,
            tanker_chat_session=bool(context.get("tanker_chat_session")),
            tanker_chat_last_spoke=str(context.get("tanker_chat_last_spoke") or ""),
        )
        if not lines:
            self.fly_say_frame.pack_forget()
            self._refresh_readback_panel()
            self._sync_fly_eam_ui()
            self.after_idle(self._fly_update_scrollregion)
            return

        agency = voice_intent.agency_spoken(cue_ch, str(airport.get("name") or "")) if cue_ch else ""
        callsign = str(context.get("callsign") or "").strip()
        self._paint_fly_voice_cues(lines, agency=agency, callsign=callsign)
        voice_on = bool(self.config_data.get("voice_enabled"))
        phase = voice_intent.normalize_mission_phase(
            str(context.get("phase") or ""), channel=cue_ch
        )
        phase_lbl = voice_intent.MISSION_PHASE_LABELS.get(phase, "")
        where = " ·  ".join(p for p in (phase_lbl, agency) if p)
        if voice_on:
            base = "VOICE CUES  ·  hold PTT"
        else:
            base = "VOICE CUES  ·  enable Voice in Setup → Controls to speak these"
        self.fly_say_title.set(f"{base}  ·  {where}" if where else base)
        if context.get("tanker_chat_session"):
            self.fly_say_subtitle.set(
                "Boom chat — talk back in your own words, no agency needed. "
                "Official tanker calls still work."
            )
        else:
            self.fly_say_subtitle.set(
                "You must address the agency for a call to be recognized — except readbacks."
            )
        # Above EXPECTED RESPONSE — pilot call first, then ATC reply.
        try:
            self.fly_say_frame.pack_forget()
        except tk.TclError:
            pass
        self.fly_say_frame.pack(
            fill=tk.X, padx=20, pady=(0, 8), before=self._fly_will_say_box
        )
        self._refresh_readback_panel()
        self._sync_fly_eam_ui()
        self.after_idle(self._fly_update_scrollregion)

    def _paint_fly_voice_cues(
        self,
        lines: list[tuple[Any, ...]],
        *,
        agency: str,
        callsign: str,
    ) -> None:
        """Complete radio tips: muted agency opener + amber must-say payload."""
        body = self.fly_say_body
        for child in body.winfo_children():
            child.destroy()
        prefix_parts = [p for p in (agency.strip(), callsign.strip()) if p]
        opener = ", ".join(prefix_parts)
        advance = [row for row in lines if len(row) >= 3 and row[2] == "advance"]
        optional = [row for row in lines if len(row) >= 3 and row[2] == "optional"]

        def _section(title: str, *, must: bool) -> None:
            tk.Label(
                body,
                text=title,
                bg="#0a0e14",
                fg=C_AMBER if must else C_MUTED,
                font=("Segoe UI Semibold", 11) if must else ("Segoe UI", 10),
                anchor="w",
            ).pack(fill=tk.X, pady=(8, 2) if must else (10, 1))

        def _cue(row: tuple[Any, ...], *, must: bool) -> None:
            say = str(row[0] or "").strip()
            does = str(row[1] or "").strip()
            needs = bool(row[3]) if len(row) > 3 else False
            if not say:
                return
            block = tk.Frame(body, bg="#0a0e14")
            block.pack(fill=tk.X, pady=(0, 8 if must else 4))
            call_row = tk.Frame(block, bg="#0a0e14")
            call_row.pack(fill=tk.X)
            if needs and opener:
                tk.Label(
                    call_row,
                    text=f"{opener}, ",
                    bg="#0a0e14",
                    fg=C_MUTED,
                    font=("Segoe UI", 14) if must else ("Segoe UI", 10),
                    anchor="w",
                ).pack(side=tk.LEFT)
            tk.Label(
                call_row,
                text=say,
                bg="#0a0e14",
                fg=C_AMBER if must else C_LABEL,
                font=("Consolas", 20, "bold") if must else ("Segoe UI", 11),
                anchor="w",
                wraplength=720,
                justify=tk.LEFT,
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)
            note = does
            if not needs:
                note = f"{does}  ·  agency optional" if does else "agency optional"
            if note:
                tk.Label(
                    block,
                    text=f"  →  {note}",
                    bg="#0a0e14",
                    fg=C_MUTED,
                    font=("Segoe UI", 10) if must else ("Segoe UI", 9),
                    anchor="w",
                    wraplength=900,
                    justify=tk.LEFT,
                ).pack(fill=tk.X, pady=(1, 0))

        if advance:
            _section("TO ADVANCE", must=True)
            for row in advance:
                _cue(row, must=True)
        if optional:
            _section("ALSO AVAILABLE", must=False)
            for row in optional:
                _cue(row, must=False)

    def _fly_live_step(self) -> dict[str, Any] | None:
        """Current Fly step, preferring the live Plan copy (voice_phrases included)."""
        step = None
        try:
            step = self.engine.current_step()
        except Exception:
            step = None
        if not isinstance(step, dict):
            return None
        sid = str(step.get("id") or "").strip()
        live = self.mission.get("steps") if isinstance(self.mission.get("steps"), list) else []
        if sid:
            for row in live:
                if isinstance(row, dict) and str(row.get("id") or "").strip() == sid:
                    return row
        return step

    def _open_fly_keywords(self) -> None:
        """Same Keywords editor as Plan, opened for the current Fly step."""
        step = self._fly_live_step()
        if not isinstance(step, dict):
            messagebox.showinfo("Keywords", "No current step.")
            return
        sid = str(step.get("id") or "").strip()
        idx = None
        for i, row in enumerate(self._steps()):
            if isinstance(row, dict) and str(row.get("id") or "").strip() == sid:
                idx = i
                break
        if idx is None:
            messagebox.showinfo("Keywords", "This step is not in the Plan timeline.")
            return
        self.timeline.selection_clear(0, tk.END)
        self.timeline.selection_set(idx)
        self._on_select()
        self._open_step_keywords()

    def _refresh_fly_status(self) -> None:
        # Keep the live Plan mission (Keywords / voice_phrases included). reload()
        # re-reads the flow file and would drop unsaved step edits.
        self.engine.config = self.config_data
        self.engine.airports = self.airports
        self.engine.mission = self.mission
        self._consume_position_fire_clears()
        st = self.engine.status()
        mission = st.get("mission") or self.mission.get("name") or "Mission"
        self.fly_mission.set(str(mission))

        if st.get("at_end"):
            total = st.get("total", 0)
            self.fly_step_num.set(f"STEP — / {total}")
            self.fly_step_name.set("No next step")
            self.fly_freq.set("—")
            self.fly_mod.set("")
            self.fly_channel.set("NO NEXT TRANSMIT")
            self.fly_tx_name.set("Seek prev or Reset to continue")
            self._fly_phrase_req_id = getattr(self, "_fly_phrase_req_id", 0) + 1
            self.fly_say.set("")
            self.fly_hint.set("Cursor is past the last enabled step.")
            if hasattr(self, "_fly_channel_lbl"):
                self._fly_channel_lbl.configure(fg=C_MUTED)
            if hasattr(self, "_fly_step_name_lbl"):
                self._fly_step_name_lbl.configure(fg=C_MUTED)
        else:
            num = st.get("step_number") or 0
            total = st.get("total") or 0
            step = st.get("step") or {}
            label = str(step.get("label") or step.get("id") or "—").strip() or "—"
            self.fly_step_num.set(f"STEP {num} / {total}")
            self.fly_step_name.set(label)
            ch, freq, mod, tx = self._fly_upcoming_radio(step)
            self.fly_freq.set(freq)
            self.fly_mod.set(mod)
            self.fly_channel.set(ch)
            self.fly_tx_name.set(f"SRS name: {tx}" if tx else "")
            mode = step.get("mode") or "tts"
            tmpl = step.get("template") or step.get("file") or ""
            eff = atc_phrase.effective_takeoff_template(
                str(tmpl), self.mission, self.engine.state
            )
            takeoff = atc_phrase.takeoff_mode_label(
                atc_phrase.resolve_active_takeoff_mode(self.mission, self.engine.state)
            )
            hint = f"{mode.upper()}  ·  {tmpl}"
            step_phase = atc_phrase.resolve_pilot_request_phase(
                phase=str(step.get("phase") or ""),
                channel=str(ch).strip().lower(),
                template=str(tmpl),
            )
            if str(ch).strip().lower() == "approach" or step_phase == "approach":
                rec = atc_phrase.resolve_active_recovery(step, self.mission, state=self.engine.state)
                hint += f"  ·  recovery {atc_phrase.recovery_label(rec)}"
            if step_phase == "departure" and (
                str(ch).strip().lower() == "tower"
                or atc_phrase.is_takeoff_related_template(str(tmpl))
            ):
                hint += f"  ·  takeoff {takeoff}"
            if eff != tmpl:
                hint += f"  ·  says {eff}"
            if atc_phrase.takeoff_offer_visible(
                self.engine.state,
                channel=str(ch).strip().lower(),
                phase=step_phase,
                template=str(tmpl),
            ):
                if atc_phrase.rolling_offer_awaiting_reply(self.engine.state):
                    hint += "  ·  rolling offer — Next accept / Prev decline"
                else:
                    hint += "  ·  rolling offer pending"
            if atc_phrase.awaiting_option_on_the_go(self.engine.state):
                hint += "  ·  option — On the go or Full stop / Next"
            self.fly_hint.set(hint)
            color = CHANNEL_COLORS.get(ch.lower(), C_ACCENT)
            if hasattr(self, "_fly_channel_lbl"):
                self._fly_channel_lbl.configure(fg=color)
            if hasattr(self, "_fly_step_name_lbl"):
                self._fly_step_name_lbl.configure(fg=C_TEXT)
            self._queue_fly_phrase_preview(step)
        ch_now = "" if st.get("at_end") else self._fly_request_radio_channel(st)
        self._sync_fly_recovery_ui(ch_now)
        self._sync_fly_pilot_request_ui(ch_now)
        self._refresh_jump_list(st)
        self._refresh_voice_prompts()
        self._sync_fly_eam_ui()
        self._update_fly_freq_gate_status()
        try:
            cap = self._boom_caption(self.engine)
            self._set_boom_status(cap)
        except Exception:
            pass

    def _refresh_jump_list(self, st: dict | None = None) -> None:
        if not hasattr(self, "var_jump"):
            return
        st = st or self.engine.status()
        labels: list[str] = []
        mapping: dict[str, int] = {}
        for item in st.get("steps") or []:
            label = f"{item['number']}. {item['label']}"
            labels.append(label)
            mapping[label] = int(item["index"])
        self._jump_index_by_label = mapping
        cur = st.get("step_number")
        if cur and labels:
            # select matching 1-based entry
            for lab in labels:
                if lab.startswith(f"{cur}."):
                    self.var_jump.set(lab)
                    break
        elif labels:
            self.var_jump.set(labels[0])
        else:
            self.var_jump.set("")

    def _jump_to_selected(self) -> None:
        label = self.var_jump.get().strip()
        if not label or label not in self._jump_index_by_label:
            messagebox.showinfo("Go to step", "Pick a step from the list.")
            return
        idx = self._jump_index_by_label[label]
        self._fly("seek", seek_index=idx)

    def _fly(
        self,
        action: str,
        seek_index: int | None = None,
        *,
        bypass_freq_gate: bool = False,
    ) -> None:
        # Auto-save so fly uses latest plan + identity overrides
        self.mission["name"] = self.mission_name_var.get().strip() or "Untitled"
        flow_engine.normalize_mission_to_steps(self.mission)
        path = flow_engine.resolve_flow_path(self.config_data)
        save_json(path, self.mission)
        self._sync_identity_to_config()
        self._sync_eam_freqs_to_config()
        save_json(CONFIG_PATH, self.config_data)

        if self._atc_role() == "client" and self._atc_client is not None:
            self._fly_via_host(action, seek_index=seek_index)
            return

        def work() -> None:
            eng = None
            try:
                eng = flow_engine.FlowEngine()
                if action == "next":
                    r = eng.next(bypass_freq_gate=bypass_freq_gate)
                elif action == "back":
                    r = eng.back(bypass_freq_gate=bypass_freq_gate)
                elif action == "reset":
                    r = eng.reset()
                    # New sortie: let position-fired clearances arm again.
                    self._position_tracker().reset()
                    self.mission = eng.mission
                    if hasattr(self, "var_runway_override"):
                        self.var_runway_override.set("")
                elif action == "seek_prev":
                    r = eng.seek_relative(-1)
                elif action == "seek_next":
                    r = eng.seek_relative(1)
                elif action == "seek":
                    r = eng.seek(0 if seek_index is None else seek_index)
                else:
                    raise RuntimeError(f"Unknown fly action: {action}")

                def done() -> None:
                    if isinstance(r, dict) and r.get("acknowledged"):
                        line = "READ BACK  noted\n"
                    elif isinstance(r, dict) and r.get("freq") is not None:
                        try:
                            freq_s = f"{float(r['freq']):.3f}"
                        except (TypeError, ValueError):
                            freq_s = str(r.get("freq"))
                        reply = str(r.get("rolling_offer_reply") or "")
                        if reply == "accept":
                            who = "accept rolling"
                        elif reply == "deny":
                            who = "decline rolling"
                        else:
                            who = r.get("label") or r.get("step_id") or "step"
                        line = (
                            f"TX  {who}  ·  "
                            f"{freq_s}  ·  {str(r.get('channel') or '').upper()}\n"
                        )
                    elif isinstance(r, dict) and (r.get("seeked") or action.startswith("seek") or action == "reset"):
                        n = r.get("step_number")
                        line = f"{action.upper()}  →  {'END' if r.get('at_end') else f'step {n}'}\n"
                    else:
                        line = f"{action.upper()}\n"
                    self.fly_log.insert(tk.END, line)
                    self.fly_log.see(tk.END)
                    if action in ("next", "back"):
                        self._append_fly_voice_feed(line)
                    self.engine = eng
                    self._refresh_fly_status()

                self.after(0, done)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                step = None
                try:
                    step = eng.current_step()
                except Exception:  # noqa: BLE001
                    step = None

                def show_err() -> None:
                    if action in ("next", "back") or err.startswith("Blocked:"):
                        self._note_no_tx(err, action=action, step=step)
                        if err.startswith("Blocked:"):
                            return
                    messagebox.showerror("Fly", err)

                self.after(0, show_err)

        threading.Thread(target=work, daemon=True).start()

    def _fly_via_host(self, action: str, seek_index: int | None = None) -> None:
        client = self._atc_client
        if client is None:
            messagebox.showerror("Fly", "Not connected to an ATC host.")
            return

        def work() -> None:
            try:
                if action == "next":
                    r = client.action("next")
                elif action == "back":
                    r = client.action("back")
                elif action == "reset":
                    r = client.action("reset")
                elif action == "seek_prev":
                    r = client.action("seek_relative", delta=-1)
                elif action == "seek_next":
                    r = client.action("seek_relative", delta=1)
                elif action == "seek":
                    r = client.action("seek", index=0 if seek_index is None else seek_index)
                else:
                    raise RuntimeError(f"Unknown fly action: {action}")

                def done() -> None:
                    queued = ""
                    if isinstance(r, dict) and r.get("queued"):
                        queued = f"  queue {r.get('queue_pos')}"
                    label = ""
                    if isinstance(r, dict):
                        label = str(r.get("label") or r.get("text") or r.get("action") or action)
                    self.fly_log.insert(tk.END, f"{action.upper()}  {label}{queued}\n")
                    self.fly_log.see(tk.END)
                    self._refresh_client_fly()

                self.after(0, done)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)

                def show_err() -> None:
                    self._set_net_status(f"CLIENT  {err}")
                    messagebox.showerror("Fly", err)

                self.after(0, show_err)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_client_fly(self) -> None:
        client = self._atc_client
        if client is None:
            return
        st = client.last_status or {}
        if client.last_error:
            self._set_net_status(f"CLIENT  {client.last_error}")
        else:
            q = st.get("queue") or {}
            wait = ""
            if q.get("speaking"):
                wait = f"  ·  {st.get('channel') or ''} speaking"
            elif q.get("queued"):
                wait = f"  ·  waiting ({q.get('queued')} ahead)"
            self._set_net_status(
                f"CLIENT  {client.callsign or st.get('callsign') or ''}  ·  "
                f"{st.get('label') or 'connected'}{wait}"
            )
        if st.get("label"):
            self.fly_step_name.set(str(st.get("label") or "…"))
        if st.get("step_number") is not None:
            self.fly_step_num.set(f"{st.get('step_number')} / {st.get('total') or '?'}")
        if st.get("channel"):
            self.fly_channel.set(str(st.get("channel") or "").upper())
        if st.get("last_tx_text") and hasattr(self, "fly_say"):
            self.fly_say.set(str(st["last_tx_text"]))
        self._sync_client_flow_cursor()

    def _build_traffic(self) -> None:
        f = self.tab_traffic
        panel = tk.Frame(f, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        panel.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        hdr = tk.Frame(panel, bg=C_PANEL)
        hdr.pack(fill=tk.X, padx=14, pady=(12, 6))
        ttk.Label(hdr, text="Multi-pilot traffic", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(hdr, text="Refresh", command=self._refresh_traffic).pack(side=tk.RIGHT)
        self.var_traffic = tk.StringVar(
            value="Role is Solo. Setup → Squadron → Host to accept other pilots."
        )
        tk.Label(
            panel,
            textvariable=self.var_traffic,
            bg=C_PANEL,
            fg=C_TEXT,
            font=("Consolas", 10),
            justify="left",
            anchor="nw",
        ).pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 14))
        self.after(1000, self._schedule_traffic_poll)

    def _schedule_traffic_poll(self) -> None:
        try:
            if self._atc_role() == "host":
                self._refresh_traffic()
            elif self._atc_role() == "client":
                self._refresh_client_fly()
        except Exception:
            pass
        self.after(1000, self._schedule_traffic_poll)

    def _refresh_traffic(self) -> None:
        if not hasattr(self, "var_traffic"):
            return
        role = self._atc_role()
        if role != "host" or self._atc_server is None:
            if role == "client":
                self.var_traffic.set(
                    "This PC is a client. The host's Traffic tab shows everyone."
                )
            else:
                self.var_traffic.set(
                    "Role is Solo. Setup → Squadron → Host to accept other pilots."
                )
            return
        data = self._atc_server.traffic()
        rows = atc_net.lan_ipv4_interfaces()
        ip_lines = [
            f"  {row.get('ip')}:{data.get('port')}  {row.get('name')}"
            for row in rows
        ] or ["  (no LAN IPv4 — check the Host NIC)"]
        advertised = str(self.config_data.get("atc_host") or "").strip()
        if advertised in {"", "127.0.0.1", "localhost"}:
            advertised = "(set Setup → Squadron → Address pilots type to your DCS/SRS host)"
        lines = [
            "This box listens on:",
            *ip_lines,
            "",
            f"Pilots type:  {advertised}  port {data.get('port')}",
            "Off-LAN: same IP/hostname as DCS/SRS. Forward TCP 8766 to this PC",
            "(like SRS 5002). 127.0.0.1 only works sitting at this server.",
            f"{len(data.get('sessions') or [])} pilots",
            "",
        ]
        queues = data.get("queues") or {}
        busy = [
            f"  {ch.upper()}: speaking {(info.get('speaking') or {}).get('callsign') or '—'}  "
            f"queued {info.get('queued') or 0}"
            for ch, info in queues.items()
            if (info.get("queued") or info.get("speaking"))
        ]
        lines.append("Channels")
        lines.extend(busy or ["  (idle)"])
        lines.append("")
        lines.append("Pilots")
        for sess in data.get("sessions") or []:
            lines.append(
                f"  {sess.get('callsign') or sess.get('session_id')}  "
                f"seat {sess.get('opus_seat') or '?'}  "
                f"step {sess.get('step_number')}/{sess.get('total')}  "
                f"{sess.get('channel') or '—'}  "
                f"{sess.get('label') or ''}  "
                f"radios {sess.get('tuned_freqs_mhz') or 'unknown'}"
            )
        if not (data.get("sessions") or []):
            lines.append("  (none yet — clients POST /v1/hello)")
        self.var_traffic.set("\n".join(lines))

    # ---------- Help ----------
    def _show_help_tab(self, topic: str | None = None) -> None:
        self.nb.select(self.tab_help)
        if topic and hasattr(self, "_help_topics"):
            titles = [t for t, _ in self._help_topics]
            if topic in titles:
                idx = titles.index(topic)
                self.help_topics.selection_clear(0, tk.END)
                self.help_topics.selection_set(idx)
                self.help_topics.see(idx)
                self._on_help_topic()

    def _build_help(self) -> None:
        root = self.tab_help
        panel = tk.Frame(root, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        panel.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        hdr = tk.Frame(panel, bg=C_PANEL)
        hdr.pack(fill=tk.X, padx=12, pady=(12, 8))
        ttk.Label(hdr, text="How-to & Help", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(
            hdr,
            text="Open Google Cloud Console",
            command=lambda: webbrowser.open("https://console.cloud.google.com/"),
        ).pack(side=tk.RIGHT)
        ttk.Button(
            hdr,
            text="Google voice list",
            command=lambda: webbrowser.open("https://cloud.google.com/text-to-speech/docs/voices"),
        ).pack(side=tk.RIGHT, padx=8)

        body = tk.Frame(panel, bg=C_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        left = tk.Frame(body, bg=C_PANEL)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        tk.Label(left, text="Topics", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        self.help_topics = tk.Listbox(
            left,
            width=28,
            height=18,
            bg=C_CARD,
            fg=C_TEXT,
            selectbackground=C_ACCENT,
            selectforeground="#061018",
            font=("Segoe UI", 10),
            activestyle="none",
            highlightthickness=1,
            highlightbackground=C_BORDER,
            relief=tk.FLAT,
            exportselection=False,
        )
        self.help_topics.pack(fill=tk.Y, expand=True, pady=(6, 0))
        self.help_topics.bind("<<ListboxSelect>>", lambda _e: self._on_help_topic())

        right = tk.Frame(body, bg=C_PANEL)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.help_text = tk.Text(
            right,
            wrap=tk.WORD,
            bg="#0a0e14",
            fg=C_TEXT,
            insertbackground=C_TEXT,
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            padx=14,
            pady=12,
            state=tk.DISABLED,
        )
        vsb = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.help_text.yview)
        self.help_text.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.help_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.help_text.tag_configure("title", font=("Segoe UI Semibold", 14), foreground=C_ACCENT, spacing3=10)
        self.help_text.tag_configure("heading", font=("Segoe UI Semibold", 11), foreground=C_GREEN, spacing1=12, spacing3=4)
        self.help_text.tag_configure("body", font=("Segoe UI", 10), foreground=C_TEXT, spacing3=2)
        self.help_text.tag_configure("bullet", font=("Segoe UI", 10), foreground=C_TEXT, lmargin1=18, lmargin2=32)
        self.help_text.tag_configure("muted", font=("Segoe UI", 9), foreground=C_MUTED, spacing1=8)

        self._help_topics = self._help_content()
        for title, _body in self._help_topics:
            self.help_topics.insert(tk.END, title)
        self.help_topics.selection_set(0)
        self._on_help_topic()

    def _on_help_topic(self) -> None:
        sel = self.help_topics.curselection()
        if not sel:
            return
        title, blocks = self._help_topics[sel[0]]
        self.help_text.configure(state=tk.NORMAL)
        self.help_text.delete("1.0", tk.END)
        self.help_text.insert(tk.END, title + "\n", "title")
        for kind, line in blocks:
            self.help_text.insert(tk.END, line + "\n", kind)
        self.help_text.configure(state=tk.DISABLED)
        self.help_text.see("1.0")

    def _help_content(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """Return (topic title, [(tag, line), ...]) for the Help tab."""
        secrets = str(HERE / "secrets")
        return [
            (
                "Getting started",
                [
                    ("heading", "Typical first flight"),
                    ("bullet", "1. Setup → Identity & TTS — set your Opus username (e.g. Turtle)."),
                    ("bullet", "2. Title bar — type your CAOC name and click the green flight chip to pick the Opus flight. That saves immediately (no Setup Save needed)."),
                    ("bullet", "3. Setup → Airport & radios — confirm SRS host and freqs (or Pull from Opus)."),
                    ("bullet", "   Optional: Manual runway overrides flight-plan / wind selection."),
                    ("bullet", "4. Plan Flight — New… (base flow + optional Opus), or Load / Save mission."),
                    ("bullet", "5. Fly — use Play and Advance through the sortie (or Stream Deck later)."),
                    ("heading", "Voice quality"),
                    ("body", "Windows voices work with zero setup (robotic)."),
                    ("body", "Google Cloud TTS is optional and sounds much more natural — see the Google topic."),
                    ("muted", "Server ATIS is separate and stays on the server. This app only does your local ATC phrases → SRS."),
                ],
            ),
            (
                "Google Cloud TTS setup",
                [
                    ("heading", "Do you need this?"),
                    ("body", "Only if you want Neural2 / WaveNet voices. Windows mode needs no API key."),
                    ("body", "Each person uses their own Google JSON. Never share your key file."),
                    ("body", "Squadron Host: the JSON lives only on the ATC box. Clients share that quota; they never receive the file. Share the squadron token only."),
                    ("heading", "1. Google Cloud project"),
                    ("bullet", "• Open console.cloud.google.com (button above)."),
                    ("bullet", "• Create a project (or select an existing one)."),
                    ("bullet", "• Billing → link a billing account (required even for free monthly TTS quota)."),
                    ("heading", "2. Enable the API"),
                    ("bullet", "• APIs & Services → Library."),
                    ("bullet", "• Search “Cloud Text-to-Speech API” → Enable."),
                    ("heading", "Important: API key ≠ JSON key"),
                    ("body", "A short key starting with AIza… is an “API key”. That will NOT work here."),
                    ("body", "You need a Service account key downloaded as a .json file (has private_key inside)."),
                    ("heading", "3. Create a service-account JSON key"),
                    ("bullet", "• APIs & Services → Credentials → Create credentials → Service account."),
                    ("bullet", "• Name it e.g. srs-atc-tts → Create → Done (roles can be skipped)."),
                    ("bullet", "• Click the service account email → Keys tab."),
                    ("bullet", "• Add key → Create new key → JSON → Create."),
                    ("bullet", "• Your browser downloads a .json file automatically."),
                    ("bullet", "• If you only see the text: Setup → Paste JSON… and we save the file for you."),
                    ("bullet", f"• Default save location: {secrets}\\google-tts.json"),
                    ("heading", "4. Wire it in this app"),
                    ("bullet", "• Setup → Identity & TTS → choose Google Cloud TTS."),
                    ("bullet", "• Browse… to the .json file, OR Paste JSON… if you have the text."),
                    ("bullet", "• Save setup → Voices tab → pick Neural2 / Chirp 3: HD voices."),
                    ("bullet", "• Plan Flight → Hear locally to audition on speakers (no SRS)."),
                    ("bullet", "• Voices picker → Preview for a short sample before assigning."),
                    ("bullet", "• TX → SRS / Fly to hear it on the radio."),
                    ("heading", "Free tier / usage"),
                    ("body", "Setup → Voices shows a local monthly character counter (resets each calendar month)."),
                    ("body", "Chirp / Neural2: 1M free chars/mo. WaveNet: 4M. ATC use is usually tiny."),
                    ("body", "At 90% of a free tier: red warning + auto-fallback Chirp→Neural2→WaveNet→Windows."),
                    ("body", "Host also caps each pilot (~80k chars/month). That jet falls back to Windows; others keep Google."),
                    ("muted", "Pricing: cloud.google.com/text-to-speech/pricing — Cloud Billing is authoritative."),
                ],
            ),
            (
                "Windows vs Google voices",
                [
                    ("heading", "Windows (default)"),
                    ("bullet", "• No API key."),
                    ("bullet", "• Uses voices installed on this PC (David, Zira, …)."),
                    ("bullet", "• Extra OneCore voices (Linda, Mark, Richard, …): Voices → Unlock OneCore… once (admin)."),
                    ("bullet", "• Hear locally and Voices → Preview play on speakers."),
                    ("heading", "Google (optional)"),
                    ("bullet", "• Needs your own service-account JSON on the Host/Solo PC — never on clients."),
                    ("bullet", "• Hundreds of Neural2 / WaveNet voices."),
                    ("bullet", "• Hear locally / Voices → Preview play Google audio on speakers (uses API quota)."),
                    ("bullet", "• TX → SRS / Fly still used for radio transmit."),
                    ("heading", "Setup → Voices"),
                    ("body", "Assign a default voice per agency (delivery, ground, tower, …)."),
                    (
                        "body",
                        "Randomize ▾ fills all agencies uniquely when possible — "
                        "Windows: all / male / female; Google: all / Chirp / Neural2 / WaveNet.",
                    ),
                ],
            ),
            (
                "Plan Flight tips",
                [
                    ("heading", "Building a mission"),
                    ("bullet", "• New… — pick a base flow, optional Opus flight, save as a new mission file."),
                    ("bullet", "• Add / reorder steps in the timeline."),
                    ("bullet", "• Each step: label, radio channel, TTS template or custom text or audio file."),
                    ("bullet", "• Approach recovery: Visual Overhead / Tactical overhead / Straight-in / Instrument."),
                    ("bullet", "• Fly tab Recovery picker changes the type mid-sortie (template steps only)."),
                    ("bullet", "• Phrase helper… — generate wording for custom situations onto a step."),
                    ("bullet", "• Phrase helper → Blackjack — Alpha check: pick a live CAOC track (e.g. Damn), verify ELVIS bullseye, apply as custom text; no Opus FP needed."),
                    ("bullet", "• Template mode regenerates from Opus (altitude, route, squawk) each Preview."),
                    ("bullet", "• Custom locks the phrase — switch back to Template to unlock / refresh."),
                    ("bullet", "• Apply changes to step, then Save mission."),
                    ("bullet", "• Reset flight cache — clears unrestricted climb, approach, runway request, and Opus/METAR stickiness; runway returns to winds."),
                    ("heading", "Other frequencies (per step)"),
                    ("body", "Select channel Other — Freq becomes editable (MHz)."),
                    ("body", "Each Other step can have its own frequency (unique per step)."),
                    ("body", "Timeline shows it like: OTH … @255.4"),
                    ("heading", "Voice per step"),
                    ("body", "Choose sets a voice for that step only."),
                    ("body", "Agency default clears the override and uses Setup → Voices for that channel."),
                    ("body", "Timeline marks a custom step voice with [v]."),
                    ("heading", "Runway per step"),
                    ("body", "Shown on taxi / tower / approach templates (and custom text)."),
                    ("body", "Blank = Setup Manual runway, else flight plan / wind."),
                    ("body", "Timeline marks a step runway like [rwy 21R]."),
                ],
            ),
            (
                "Fly & Stream Deck",
                [
                    ("heading", "Fly tab"),
                    ("bullet", "• Play and Advance transmits the next enabled step to SRS. If a readback card is showing, Play closes it instead. Watch will not send the next call until you read back."),
                    ("bullet", "• Back / Reset / seek jump around the timeline."),
                    ("bullet", "• On Approach: recovery buttons (Overhead / Tactical / Straight-in / Instrument)."),
                    ("heading", "Takeoff — rolling vs line up and wait"),
                    ("bullet", "• Tower may offer rolling (~35% by default; config takeoff_offer_chance)."),
                    ("bullet", "• After Tower asks, Next / HOTAS Advance accepts; Previous declines. Accept / Deny buttons still work on the Fly offer bar."),
                    ("bullet", "• Rolling skips Line up and wait; clearance is still “cleared for takeoff” (no “rolling” in the call)."),
                    ("bullet", "• Ready for departure → Line up and wait only. Cleared takeoff fires from the runway in-position zone, Play, or “in position” / “lined up”."),
                    ("bullet", "• Departure: climb to cruise auto beyond 10 NM (filed altitude), then handoff beyond 18 NM — or Request handoff / “request handoff” to switch now."),
                    ("bullet", "• Pilot request buttons only appear on frequencies that support them."),
                    ("heading", "Triggers — HOTAS, hotkeys, Stream Deck"),
                    ("bullet", "• Setup → Controls → HOTAS: Learn… a spare stick button for Advance / Previous, and optionally Step forward / back (cursor only, no TX)."),
                    ("bullet", "• HOTAS is the reliable option in-game — it keeps working while DCS is focused."),
                    ("bullet", "• Show SRS PTT buttons… lists what SRS already uses so you avoid a clash."),
                    ("bullet", "• Setup → Controls → Keyboard: Capture… any free combo (F13 / F14 only suit Stream Deck Advance / Previous)."),
                    ("bullet", "• Step forward / Step back hotkeys only move the timeline cursor — they never transmit."),
                    ("bullet", "• Keys are also polled, so they keep working in-game without administrator rights."),
                    ("bullet", "• Polling does not swallow the key, so pick a combo DCS itself does not use."),
                    ("bullet", "• Use a key, a HOTAS button, or both — one press only ever advances one step."),
                    ("bullet", "• No-keystroke option: http://127.0.0.1:8765/next and /back from a Stream Deck Website action; /seek_next and /seek_prev step the cursor without TX."),
                    ("bullet", "• Last trigger on the Controls tab confirms a press actually reached the app."),
                    ("heading", "Voice control"),
                    ("bullet", "• Setup → Controls → Voice: tick Enable, then hold your normal SRS PTT and talk. Optional: Understand messy radio (LLM) uses the boom-chat Ollama/API setting — keyword grammar still wins; the model only maps a missed call onto an allowed intent and never writes a new clearance."),
                    ("bullet", "• Mic and PTT are auto-detected from the SRS client config; override either if needed."),
                    ("bullet", "• PTT can be a HOTAS button or a key — whichever you already transmit with."),
                    ("bullet", "• Open with the agency — \u201cNellis Ground, Fleece 1, ready to taxi\u201d — or it stays silent."),
                    ("bullet", "• Not sure what to say? The Fly tab lists the calls that fit right now."),
                    ("bullet", "• After a clearance, READ BACK highlights your code — "
                     "\u201csquawk XXXX\u201d, \u201csquawking XXXX\u201d, or just the digits "
                     "(\u201czero four one one\u201d). Roger also works."),
                    ("bullet", "• Extra words in the readback are fine; you do not need to say \u201cin sequence\u201d."),
                    ("bullet", "• Wording is loose: \u201cready for taxi\u201d, \u201crequest taxi\u201d and a misheard \u201ctaxy\u201d all work."),
                    ("bullet", "• For the call that is due, the short version is enough — \u201cGround, Fleece 1, taxi\u201d."),
                    ("bullet", "• Right after ATC speaks, a plain \u201croger\u201d also clears the readback with no agency name."),
                    ("bullet", "• Try: request runway · say winds · request picture · bogey dope · declare · request tanker · say again."),
                    ("bullet", "• Tanker is a side trip, not the next C2 step: Blackjack/Bandsaw request tanker for track and braw (frequency change approved). The cursor jumps to Tanker; when you retune Blackjack or Bandsaw (or check back in / back from the tanker) it returns to that agency. On tanker freq: request rejoin — Texaco clears rejoin left, sometimes left observation (no “identified”). After rejoin, Texaco starts boom small talk once you have been 0.1–0.5 NM from the tanker for 30–60 seconds (or press Fly Texaco starts chat). DCS tanker radio still owns Intent to refuel, Ready pre-contact (cleared contact), and Abort."),
                    ("bullet", "• A call that fires a step advances Fly on its own — no need to press Play."),
                    ("bullet", "• Fly splits TO ADVANCE (plays the step) from ALSO AVAILABLE (winds, picture, …). "
                     "Cues are a full call when the agency opener is required; amber is the wording that must be said."),
                    ("bullet", "• Want your own wording? Fly or Plan → Keywords… — add a whole phrase, OK, then Save mission. Built-in lists are AND-groups (one word from each line together), not each word on its own."),
                    ("bullet", "• Mission phrases add to the built-in calls and show first under TO ADVANCE."),
                    ("bullet", "• Flight calls (\u201cTwo, go button five\u201d) and crew talk never move the timeline."),
                    ("bullet", "• The Fly log shows every transmission, why it did or did not fire, and what to fix."),
                    ("bullet", "• Runs locally on the CPU (~350ms with base.en) and leaves the GPU to DCS."),
                    ("body", "Save your mission first so Fly and Stream Deck use the same active flow file."),
                    ("muted", "Kneeboard PDF can be regenerated from Setup when freqs change."),
                ],
            ),
            (
                "Troubleshooting",
                [
                    ("heading", "No audio on SRS"),
                    ("bullet", "• Confirm SRS client is on the same server (Setup → Airport → SRS host/port)."),
                    ("bullet", "• Check the step frequency matches a radio you’re monitoring."),
                    ("bullet", "• Use TX preview → SRS from Plan Flight to isolate one step."),
                    ("heading", "Google TTS errors"),
                    ("bullet", "• JSON path missing/invalid → Browse again and Save setup."),
                    ("bullet", "• API not enabled, or billing not linked on the Google project."),
                    ("bullet", "• Wrong voice id — use Neural2 names from the Voices tab or Google’s voice list."),
                    ("heading", "Callsign wrong / blank"),
                    ("bullet", "• Opus username must match your Opus signup."),
                    ("bullet", "• Or set Manual callsign on Identity & TTS."),
                    ("heading", "Windows voice list empty / robotic"),
                    ("body", "Install more voices via Windows Settings → Time & language → Speech → Manage voices."),
                    ("muted", "Still need better audio? Switch to Google BYOK (Help topic above)."),
                ],
            ),
        ]

    # ---------- Setup ----------
    def _build_setup(self) -> None:
        """Compact Setup: nested tabs so nothing needs scrolling."""
        f = self.tab_setup

        bottom = tk.Frame(f, bg=C_BG)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 12))
        ttk.Button(bottom, text="Save setup", style="Accent.TButton", command=self.save_setup).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Pull freqs from Opus", command=self._pull_opus_freqs).pack(side=tk.LEFT, padx=8)
        ttk.Button(bottom, text="Compare to Opus", command=self._compare_opus_freqs).pack(side=tk.LEFT, padx=4)
        ttk.Button(bottom, text="Regenerate kneeboard PDF", command=self._regen_pdf).pack(side=tk.LEFT, padx=8)

        self.setup_nb = ttk.Notebook(f)
        self.setup_nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=(8, 8))
        self.setup_tab_squadron = ttk.Frame(self.setup_nb)
        self.setup_tab_identity = ttk.Frame(self.setup_nb)
        self.setup_tab_voices = ttk.Frame(self.setup_nb)
        self.setup_tab_airport = ttk.Frame(self.setup_nb)
        self.setup_tab_controls = ttk.Frame(self.setup_nb)
        self.setup_nb.add(self.setup_tab_squadron, text="  Squadron  ")
        self.setup_nb.add(self.setup_tab_identity, text="  Identity & TTS  ")
        self.setup_nb.add(self.setup_tab_voices, text="  Voices  ")
        self.setup_nb.add(self.setup_tab_airport, text="  Airport & radios  ")
        self.setup_nb.add(self.setup_tab_controls, text="  Controls  ")

        self._ensure_identity_vars()
        self.var_volume = tk.DoubleVar(value=0.8)
        self.var_speed = tk.DoubleVar(value=float(atc_phrase.DEFAULT_TTS_SPEED_WINDOWS))
        self.var_volume_lbl = tk.StringVar(value="0.80")
        self.var_speed_lbl = tk.StringVar(value=str(atc_phrase.DEFAULT_TTS_SPEED_WINDOWS))
        self.var_freq_status = tk.StringVar(value="")
        self.var_tts_provider = tk.StringVar(value="windows")
        self.var_google_credentials = tk.StringVar()
        self.var_tts_status = tk.StringVar(value="")
        self.var_voice_status = tk.StringVar(value="")

        self.var_volume.trace_add("write", lambda *_: self.var_volume_lbl.set(f"{float(self.var_volume.get()):.2f}"))
        self.var_speed.trace_add(
            "write", lambda *_: self.var_speed_lbl.set(str(atc_phrase.tts_speed(speed=self.var_speed.get())))
        )

        self._build_setup_squadron()
        self._build_setup_identity()
        self._build_setup_voices()
        self._build_setup_airport()
        self._build_setup_controls()

    def _build_setup_squadron(self) -> None:
        root = self.setup_tab_squadron
        panel = tk.Frame(root, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        panel.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        inner = tk.Frame(panel, bg=C_PANEL)
        inner.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)
        ttk.Label(inner, text="Multi-pilot ATC", style="Header.TLabel").pack(anchor="w")
        tk.Label(
            inner,
            text=(
                "Solo is today's one-PC app. Host is the dedicated ATC box that "
                "speaks on SRS. Client is a pilot PC: voice recognition stays local, "
                "the host answers on the radio. Same token on every machine — never "
                "share the Google JSON key. Default is Solo so this branch does not "
                "change how you already fly."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 9),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(4, 12))
        self.var_atc_role = tk.StringVar(value=self._atc_role())
        role_row = tk.Frame(inner, bg=C_PANEL)
        role_row.pack(anchor="w", pady=(0, 10))
        for label, value in (
            ("Solo (this PC only)", "solo"),
            ("Host (ATC for the squadron)", "host"),
            ("Client (pilot → host)", "client"),
        ):
            ttk.Radiobutton(
                role_row,
                text=label,
                variable=self.var_atc_role,
                value=value,
                command=self._on_atc_role_change,
                style="Panel.TRadiobutton",
            ).pack(side=tk.LEFT, padx=(0, 16))
        lf = tk.Frame(inner, bg=C_PANEL)
        lf.pack(fill=tk.X, pady=(8, 0))
        self.var_atc_host = tk.StringVar(
            value=str(self.config_data.get("atc_host") or "127.0.0.1")
        )
        self.var_atc_port = tk.StringVar(
            value=str(self.config_data.get("atc_port") or atc_net.DEFAULT_ATC_PORT)
        )
        self.var_atc_token = tk.StringVar(
            value=str(self.config_data.get("atc_token") or "")
        )
        self.var_atc_net_status = tk.StringVar(value="")
        self._setup_field(lf, 0, "Address pilots type", self.var_atc_host, width=28)
        self._setup_field(lf, 1, "ATC port", self.var_atc_port, width=8)
        token_row = tk.Frame(lf, bg=C_PANEL)
        token_row.grid(row=2, column=1, sticky="we", pady=3, padx=(8, 0))
        tk.Label(lf, text="Shared token", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).grid(
            row=2, column=0, sticky="w", pady=3
        )
        ttk.Entry(token_row, textvariable=self.var_atc_token, width=28).pack(side=tk.LEFT)
        ttk.Button(token_row, text="Generate", command=self._fill_atc_token).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(
            token_row, text="Test connection", command=self._test_atc_host_connection
        ).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(
            lf,
            text=(
                "Pilots: type the same hostname you already use for SRS "
                f"(for example showtime.455aew.com), ATC port {atc_net.DEFAULT_ATC_PORT} "
                "(not SRS 5002). Off-LAN is fine — the Showtime router must forward "
                "TCP 8766 to the DCS server, same as 5002. "
                "127.0.0.1 is only for tests on the Host PC. Same token on every machine."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=640,
            justify="left",
        ).grid(row=3, column=1, sticky="w", pady=(4, 0))
        tk.Label(
            inner,
            textvariable=self.var_atc_net_status,
            bg=C_PANEL,
            fg=C_GREEN,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill=tk.X, pady=(16, 0))

    def _new_atc_token(self) -> str:
        import secrets

        return secrets.token_urlsafe(12)

    def _fill_atc_token(self) -> None:
        self.var_atc_token.set(self._new_atc_token())

    def _test_atc_host_connection(self) -> None:
        """Ping the configured Host URL. Use this on the client PC."""
        host = (
            self.var_atc_host.get().strip()
            if hasattr(self, "var_atc_host")
            else str(self.config_data.get("atc_host") or "")
        )
        try:
            port = int(
                self.var_atc_port.get()
                if hasattr(self, "var_atc_port")
                else self.config_data.get("atc_port")
                or atc_net.DEFAULT_ATC_PORT
            )
        except (TypeError, ValueError):
            port = atc_net.DEFAULT_ATC_PORT
        token = (
            self.var_atc_token.get().strip()
            if hasattr(self, "var_atc_token")
            else atc_net.token_of(self.config_data)
        )
        cfg = {
            "atc_host": host or "127.0.0.1",
            "atc_port": port,
            "atc_token": token,
        }
        client = atc_client.AtcClient(cfg)
        try:
            health_url = client.probe_health()
        except Exception as exc:  # noqa: BLE001
            extra = ""
            target = str(cfg.get("atc_host") or "")
            local_ips = atc_net.lan_ipv4_addresses()
            if (
                target
                and target not in {"127.0.0.1", "localhost"}
                and local_ips
                and not atc_net.ipv4_same_lan(target, local_ips)
            ):
                extra = (
                    f"\n\nThis PC is {', '.join(local_ips)}; Host is {target}. "
                    "Different subnets are OK.\n"
                    "On this PC use the same address you already use for DCS/SRS, "
                    "and on the DCS server's router forward TCP 8766 to that server "
                    "(same idea as SRS 5002). Direct 192.168.50.20 only works if "
                    "this PC can already reach that IP."
                )
            messagebox.showerror(
                "ATC Host",
                f"Cannot reach the Host.\n\n{exc}{extra}\n\n"
                "Check:\n"
                "• DCS server: Host role saved, firewall allows inbound TCP 8766\n"
                f"• This PC: DCS/SRS address, port {atc_net.DEFAULT_ATC_PORT} "
                "(not SRS 5002)\n"
                "• Router: TCP 8766 forwarded/routed to the DCS server",
            )
            self._set_net_status(str(exc))
            return
        try:
            hello = client.hello()
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning(
                "ATC Host",
                f"Reached {health_url} but login failed.\n\n{exc}\n\n"
                "The port is open. Copy the Host's shared token exactly.",
            )
            self._set_net_status(str(exc))
            return
        cs = hello.get("callsign") or "connected"
        messagebox.showinfo(
            "ATC Host",
            f"Connected.\n\n{health_url}\n{cs}\n\n"
            "Save setup on this PC so voice/hotkeys keep using this Host.",
        )
        self._set_net_status(f"CLIENT  {health_url}  ·  {cs}")

    def _on_atc_role_change(self) -> None:
        if hasattr(self, "var_tts_provider"):
            self._update_tts_status()

    def _build_setup_identity(self) -> None:
        root = self.setup_tab_identity
        panel = tk.Frame(root, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        panel.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        # Two columns: identity | TTS
        cols = tk.Frame(panel, bg=C_PANEL)
        cols.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)
        left = tk.Frame(cols, bg=C_PANEL)
        right = tk.Frame(cols, bg=C_PANEL)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 16))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ttk.Label(left, text="Identity & Opus", style="Header.TLabel").pack(anchor="w", pady=(0, 8))
        lf = tk.Frame(left, bg=C_PANEL)
        lf.pack(fill=tk.X)
        user_setup = self._setup_field(lf, 0, "Opus username", self.var_user)
        user_setup.bind("<Return>", self._commit_identity_user)
        user_setup.bind("<FocusOut>", self._commit_identity_user)
        user_setup.bind("<KeyRelease>", self._schedule_identity_user_commit)
        tk.Label(
            lf,
            text="Saves as you type. Matches your seat on the selected flight when signed up.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).grid(row=1, column=1, sticky="w")
        self._setup_field(lf, 2, "Opus backend URL", self.var_backend)
        tk.Label(lf, text="Selected flight", bg=C_PANEL, fg=C_LABEL).grid(row=3, column=0, sticky="w", pady=6)
        flight_row = tk.Frame(lf, bg=C_PANEL)
        flight_row.grid(row=3, column=1, sticky="we", pady=6)
        tk.Label(
            flight_row,
            textvariable=self.var_opus_flight,
            bg=C_PANEL,
            fg=C_GREEN,
            font=("Segoe UI Semibold", 10),
            wraplength=320,
            justify="left",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(flight_row, text="Choose flight…", command=self._choose_opus_flight).pack(
            side=tk.RIGHT, padx=(8, 0)
        )
        override_ent = self._setup_field(lf, 4, "Manual callsign", self.var_callsign_override)
        override_ent.bind("<Return>", self._commit_identity_override)
        override_ent.bind("<FocusOut>", self._commit_identity_override)
        tk.Label(
            lf,
            text="Optional. Use alone for offline TTS (no Opus). Blank = selected flight name. Saves on Enter or leaving the field.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).grid(row=5, column=1, sticky="w")
        tk.Label(lf, text="Active callsign", bg=C_PANEL, fg=C_LABEL).grid(row=6, column=0, sticky="w", pady=6)
        tk.Label(lf, textvariable=self.var_callsign, bg=C_PANEL, fg=C_GREEN, font=("Segoe UI Semibold", 11)).grid(
            row=6, column=1, sticky="w", pady=6
        )
        btns = tk.Frame(lf, bg=C_PANEL)
        btns.grid(row=7, column=1, sticky="w", pady=6)
        ttk.Button(btns, text="Refresh from Opus", command=self._persist_identity).pack(side=tk.LEFT)
        ttk.Button(btns, text="Clear flight", command=self._clear_opus_flight).pack(side=tk.LEFT, padx=(8, 0))
        lf.columnconfigure(1, weight=1)

        tts_hdr = tk.Frame(right, bg=C_PANEL)
        tts_hdr.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(tts_hdr, text="Text-to-speech engine", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Button(
            tts_hdr,
            text="How to get Google JSON…",
            command=lambda: self._show_help_tab("Google Cloud TTS setup"),
        ).pack(side=tk.RIGHT)
        ttk.Radiobutton(
            right,
            text="Windows (default — free, no API)",
            variable=self.var_tts_provider,
            value="windows",
            command=self._on_tts_provider_change,
            style="Panel.TRadiobutton",
        ).pack(anchor="w")
        ttk.Radiobutton(
            right,
            text="Google Cloud TTS (Host/Solo JSON — clients never get it)",
            variable=self.var_tts_provider,
            value="google",
            command=self._on_tts_provider_change,
            style="Panel.TRadiobutton",
        ).pack(anchor="w", pady=(4, 8))

        self.google_cred_frame = tk.Frame(right, bg=C_PANEL)
        cred_row = tk.Frame(self.google_cred_frame, bg=C_PANEL)
        cred_row.pack(fill=tk.X)
        tk.Label(cred_row, text="JSON key", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).pack(side=tk.LEFT)
        ttk.Entry(cred_row, textvariable=self.var_google_credentials).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 4)
        )
        ttk.Button(cred_row, text="Browse…", command=self._browse_google_credentials).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(cred_row, text="Paste JSON…", command=self._paste_google_credentials).pack(side=tk.LEFT)
        tk.Label(
            self.google_cred_frame,
            textvariable=self.var_tts_status,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))
        tk.Label(
            self.google_cred_frame,
            text="Need a service-account JSON on this Host PC only (not an AIza… API key). Paste JSON… copies it into atc\\secrets\\ (gitignored). Never put this file on pilot PCs or Discord.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        self._tts_status_windows = tk.Label(
            right,
            text=(
                "Windows voices — no API key. Assign per agency on Voices. "
                "Extra installed voices (Linda / Mark / Richard) need Unlock OneCore… once (admin)."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=420,
            justify="left",
        )

        # Volume + speed on one row
        sliders = tk.Frame(right, bg=C_PANEL)
        sliders.pack(fill=tk.X, pady=(16, 0))
        vol_col = tk.Frame(sliders, bg=C_PANEL)
        spd_col = tk.Frame(sliders, bg=C_PANEL)
        vol_col.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))
        spd_col.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(vol_col, text="Volume", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).pack(anchor="w")
        ttk.Scale(vol_col, from_=0.2, to=1.0, variable=self.var_volume, orient=tk.HORIZONTAL).pack(fill=tk.X)
        tk.Label(vol_col, textvariable=self.var_volume_lbl, bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 8)).pack(
            anchor="e"
        )
        tk.Label(spd_col, text="Default talk speed (−10…10)", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).pack(
            anchor="w"
        )
        ttk.Scale(spd_col, from_=-5, to=10, variable=self.var_speed, orient=tk.HORIZONTAL).pack(fill=tk.X)
        tk.Label(
            spd_col,
            text=(
                f"Defaults: Windows {atc_phrase.DEFAULT_TTS_SPEED_WINDOWS} · "
                f"Google {atc_phrase.DEFAULT_TTS_SPEED_GOOGLE}"
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w")
        tk.Label(spd_col, textvariable=self.var_speed_lbl, bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 8)).pack(
            anchor="e"
        )

    def _build_setup_voices(self) -> None:
        root = self.setup_tab_voices
        panel = tk.Frame(root, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        panel.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        hdr = tk.Frame(panel, bg=C_PANEL)
        hdr.pack(fill=tk.X, padx=14, pady=(12, 6))
        ttk.Label(hdr, text="Voice per agency", style="Header.TLabel").pack(side=tk.LEFT)
        self._rand_menu_btn = ttk.Menubutton(hdr, text="Randomize ▾")
        self._rand_menu = tk.Menu(self._rand_menu_btn, tearoff=0)
        self._rand_menu_btn["menu"] = self._rand_menu
        self._rand_menu_btn.pack(side=tk.RIGHT)
        ttk.Button(hdr, text="Unlock OneCore…", command=self._unlock_onecore_voices).pack(
            side=tk.RIGHT, padx=(0, 4)
        )
        ttk.Button(hdr, text="Refresh list", command=self._refresh_voice_list).pack(side=tk.RIGHT, padx=8)
        tk.Label(
            panel,
            textvariable=self.var_voice_status,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=720,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 6))
        self._rebuild_randomize_menu()

        usage_fr = tk.Frame(panel, bg=C_CARD, highlightbackground=C_BORDER, highlightthickness=1)
        usage_fr.pack(fill=tk.X, padx=14, pady=(0, 10))
        usage_hdr = tk.Frame(usage_fr, bg=C_CARD)
        usage_hdr.pack(fill=tk.X, padx=10, pady=(8, 4))
        tk.Label(
            usage_hdr,
            text="Google TTS usage (this PC)",
            bg=C_CARD,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 10),
        ).pack(side=tk.LEFT)
        ttk.Button(usage_hdr, text="Refresh", command=self._refresh_tts_usage).pack(side=tk.RIGHT)
        self.var_tts_usage = tk.StringVar(value="")
        tk.Label(
            usage_fr,
            textvariable=self.var_tts_usage,
            bg=C_CARD,
            fg=C_MUTED,
            font=("Consolas", 9),
            justify="left",
            anchor="w",
        ).pack(fill=tk.X, padx=10, pady=(0, 4))
        self.var_tts_usage_warn = tk.StringVar(value="")
        self.lbl_tts_usage_warn = tk.Label(
            usage_fr,
            textvariable=self.var_tts_usage_warn,
            bg=C_CARD,
            fg="#ff6b6b",
            font=("Segoe UI Semibold", 9),
            justify="left",
            anchor="w",
            wraplength=640,
        )
        self.lbl_tts_usage_warn.pack(fill=tk.X, padx=10, pady=(0, 8))
        self._refresh_tts_usage()

        grid = tk.Frame(panel, bg=C_PANEL)
        grid.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 14))
        self.voice_vars: dict[str, tk.StringVar] = {}
        self.voice_display_vars: dict[str, tk.StringVar] = {}
        agencies = ["default"] + list(atc_phrase.CHANNELS)
        # Two columns — fits without scrolling
        for i, ch in enumerate(agencies):
            r, c = divmod(i, 2)
            cell = tk.Frame(grid, bg=C_PANEL)
            cell.grid(row=r, column=c, sticky="we", padx=(0, 16) if c == 0 else (0, 0), pady=4)
            var = tk.StringVar(value="")
            dvar = tk.StringVar(value="")
            self.voice_vars[ch] = var
            self.voice_display_vars[ch] = dvar
            label = "tanker · female" if ch == "tanker" else ch
            tk.Label(cell, text=label, bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10), width=12, anchor="w").pack(
                side=tk.LEFT
            )
            tk.Label(
                cell,
                textvariable=dvar,
                bg=C_CARD,
                fg=C_TEXT,
                font=("Segoe UI", 9),
                anchor="w",
                padx=8,
                pady=5,
                width=22,
                highlightbackground=C_BORDER,
                highlightthickness=1,
            ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
            ttk.Button(cell, text="Choose", width=8, command=lambda chn=ch: self._choose_voice(chn)).pack(
                side=tk.LEFT
            )
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        self.voice_labels = []

    def _build_setup_airport(self) -> None:
        root = self.setup_tab_airport
        panel = tk.Frame(root, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        panel.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        ttk.Label(panel, text="Airport & radios", style="Header.TLabel").pack(anchor="w", padx=14, pady=(12, 8))

        self.var_ap_name = tk.StringVar()
        self.var_icao = tk.StringVar()
        self.var_host = tk.StringVar()
        self.var_port = tk.StringVar()
        self.var_coalition = tk.StringVar()
        self.var_runways = tk.StringVar()
        self.var_runway_override = tk.StringVar()
        self.var_expect_minutes = tk.StringVar()
        self.var_known_sids = tk.StringVar()
        self.freq_vars = {ch: tk.StringVar() for ch in atc_phrase.CHANNELS}

        body = tk.Frame(panel, bg=C_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))
        left = tk.Frame(body, bg=C_PANEL)
        right = tk.Frame(body, bg=C_PANEL)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 16))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        lf = tk.Frame(left, bg=C_PANEL)
        lf.pack(fill=tk.X)
        self._setup_field(lf, 0, "Airport name", self.var_ap_name, width=28)
        self._setup_field(lf, 1, "ICAO", self.var_icao, width=28)
        self._setup_field(lf, 2, "SRS host", self.var_host, width=28)
        self._setup_field(lf, 3, "SRS port", self.var_port, width=28)
        self._setup_field(lf, 4, "Coalition (2=blue)", self.var_coalition, width=28)
        self._setup_field(lf, 5, "Runways", self.var_runways, width=28)
        self._setup_field(lf, 6, "Manual runway", self.var_runway_override, width=28)
        rwy_hint = tk.Frame(lf, bg=C_PANEL)
        rwy_hint.grid(row=7, column=1, sticky="w")
        tk.Label(
            rwy_hint,
            text="Blank = flight plan / wind  (e.g. 21R or 03L)",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT)
        ttk.Button(
            rwy_hint,
            text="Use winds",
            width=10,
            command=self._clear_setup_runway_override,
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._setup_field(lf, 8, "Expect FL (min)", self.var_expect_minutes, width=28)
        self._setup_field(lf, 9, "Known SIDs", self.var_known_sids, width=28)
        lf.columnconfigure(1, weight=1)

        tk.Label(right, text="Frequencies (MHz)", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI Semibold", 10)).pack(
            anchor="w", pady=(0, 6)
        )
        ff = tk.Frame(right, bg=C_PANEL)
        ff.pack(fill=tk.X)
        freq_channels = [ch for ch in atc_phrase.CHANNELS if ch != "tanker"]
        for i, ch in enumerate(freq_channels):
            r, c = divmod(i, 2)
            tk.Label(ff, text=ch, bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9), width=10, anchor="w").grid(
                row=r, column=c * 2, sticky="w", pady=3, padx=(0, 4)
            )
            ttk.Entry(ff, textvariable=self.freq_vars[ch], width=12).grid(
                row=r, column=c * 2 + 1, sticky="w", pady=3, padx=(0, 16)
            )

        tk.Label(
            panel,
            textvariable=self.var_freq_status,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=900,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(4, 8))

    def _build_setup_controls(self) -> None:
        root = self.setup_tab_controls
        # Freq-gate + HOTAS + keys + voice no longer fit one viewport — scroll.
        scroll_host = tk.Frame(root, bg=C_BG)
        scroll_host.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        canvas = tk.Canvas(scroll_host, bg=C_PANEL, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(scroll_host, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        panel = tk.Frame(canvas, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        panel_win = canvas.create_window((0, 0), window=panel, anchor="nw")

        def _panel_cfg(_event: tk.Event | None = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _canvas_cfg(event: tk.Event) -> None:
            canvas.itemconfigure(panel_win, width=event.width)

        panel.bind("<Configure>", _panel_cfg)
        canvas.bind("<Configure>", _canvas_cfg)

        def _controls_wheel(event: tk.Event) -> str | None:
            try:
                if self.nb.index(self.nb.select()) != self.nb.index(self.tab_setup):
                    return None
                if self.setup_nb.index(self.setup_nb.select()) != self.setup_nb.index(
                    self.setup_tab_controls
                ):
                    return None
            except tk.TclError:
                return None
            if self._wheel_owned_elsewhere(event):
                return None
            delta = int(-1 * (event.delta / 120)) if event.delta else 0
            if delta:
                canvas.yview_scroll(delta, "units")
            return "break"

        self._setup_controls_canvas = canvas
        self._setup_controls_wheel = _controls_wheel

        def _maybe_unbind(_event: object | None = None) -> None:
            try:
                if self.nb.index(self.nb.select()) != self.nb.index(self.tab_setup):
                    canvas.unbind_all("<MouseWheel>")
                    return
                if self.setup_nb.index(self.setup_nb.select()) != self.setup_nb.index(
                    self.setup_tab_controls
                ):
                    canvas.unbind_all("<MouseWheel>")
                    return
                x, y = self.winfo_pointerxy()
                widget = self.winfo_containing(x, y)
                while widget is not None:
                    if widget in (canvas, panel, scroll_host, self.setup_tab_controls):
                        return
                    widget = getattr(widget, "master", None)
                canvas.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass

        for widget in (canvas, panel, scroll_host):
            widget.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _controls_wheel))
            widget.bind("<Leave>", _maybe_unbind)

        self.var_hotkey_next = tk.StringVar(value=hotkeys.DEFAULT_HOTKEY_NEXT)
        self.var_hotkey_back = tk.StringVar(value=hotkeys.DEFAULT_HOTKEY_BACK)
        self.var_hotkey_seek_next = tk.StringVar(value="")
        self.var_hotkey_seek_prev = tk.StringVar(value="")
        self.var_hotkey_status = tk.StringVar(value="")
        self.var_joy_next = tk.StringVar(value="(none)")
        self.var_joy_back = tk.StringVar(value="(none)")
        self.var_joy_seek_next = tk.StringVar(value="(none)")
        self.var_joy_seek_prev = tk.StringVar(value="(none)")
        self.var_trigger_seen = tk.StringVar(value="Last trigger: (none yet)")

        # --- Keyboard + HOTAS / mouse ---
        ctrl = tk.Frame(panel, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        ctrl.pack(fill=tk.X, padx=14, pady=(14, 8))
        ctrl_inner = tk.Frame(ctrl, bg=C_PANEL)
        ctrl_inner.pack(fill=tk.X, padx=12, pady=10)
        ttk.Label(
            ctrl_inner, text="Keyboard & HOTAS / mouse", style="Header.TLabel"
        ).pack(anchor="w")
        tk.Label(
            ctrl_inner,
            text=(
                "Bind a key, a HOTAS or mouse button, or both. Capture… / Learn… take "
                "whatever you press. Keys are polled so they work while DCS is focused, "
                "but they are not swallowed — avoid a combo DCS itself uses. "
                "Step forward / back only move the cursor; they do not transmit. "
                "Side mouse buttons (4 / 5) work well; avoid your SRS PTT."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))

        hdr = tk.Frame(ctrl_inner, bg=C_PANEL)
        hdr.pack(fill=tk.X, pady=(0, 2))
        tk.Label(hdr, text="", bg=C_PANEL, width=18).pack(side=tk.LEFT)
        tk.Label(
            hdr, text="Keyboard", bg=C_PANEL, fg=C_MUTED, font=("Segoe UI Semibold", 8),
            width=22, anchor="w",
        ).pack(side=tk.LEFT, padx=(8, 6))
        tk.Label(hdr, text="", bg=C_PANEL, width=18).pack(side=tk.LEFT)
        tk.Label(
            hdr, text="HOTAS / mouse", bg=C_PANEL, fg=C_MUTED, font=("Segoe UI Semibold", 8),
            anchor="w",
        ).pack(side=tk.LEFT, padx=(16, 0))

        for which, label, key_var, joy_var, can_clear_key in (
            ("next", "Advance (Next)", self.var_hotkey_next, self.var_joy_next, False),
            ("back", "Previous (Back)", self.var_hotkey_back, self.var_joy_back, False),
            ("seek_next", "Step forward", self.var_hotkey_seek_next, self.var_joy_seek_next, True),
            ("seek_prev", "Step back", self.var_hotkey_seek_prev, self.var_joy_seek_prev, True),
        ):
            row = tk.Frame(ctrl_inner, bg=C_PANEL)
            row.pack(fill=tk.X, pady=3)
            tk.Label(
                row, text=label, bg=C_PANEL, fg=C_LABEL, width=18, anchor="w"
            ).pack(side=tk.LEFT)
            ttk.Entry(row, textvariable=key_var, width=22).pack(side=tk.LEFT, padx=(8, 6))
            ttk.Button(
                row, text="Capture…", command=lambda w=which: self._capture_hotkey(w)
            ).pack(side=tk.LEFT)
            if can_clear_key:
                ttk.Button(
                    row, text="Clear", command=lambda w=which: self._clear_hotkey(w)
                ).pack(side=tk.LEFT, padx=(4, 0))
            else:
                tk.Label(row, text="", bg=C_PANEL, width=6).pack(side=tk.LEFT, padx=(4, 0))
            tk.Label(
                row,
                textvariable=joy_var,
                bg=C_CARD,
                fg=C_TEXT,
                font=("Consolas", 9),
                width=28,
                anchor="w",
                padx=6,
            ).pack(side=tk.LEFT, padx=(16, 6), ipady=3)
            ttk.Button(
                row, text="Learn…", command=lambda w=which: self._learn_joy_button(w)
            ).pack(side=tk.LEFT)
            ttk.Button(
                row, text="Clear", command=lambda w=which: self._clear_joy_button(w)
            ).pack(side=tk.LEFT, padx=4)

        actions = tk.Frame(ctrl_inner, bg=C_PANEL)
        actions.pack(fill=tk.X, pady=(8, 2))
        ttk.Button(
            actions, text="Show SRS PTT buttons…", command=self._suggest_srs_ptt
        ).pack(side=tk.LEFT)
        ttk.Button(actions, text="Apply controls", command=self._apply_hotkeys).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        self.btn_admin = ttk.Button(
            actions, text="Restart as administrator…", command=self._restart_as_admin
        )
        self.btn_admin.pack(side=tk.LEFT, padx=8)

        tk.Label(
            ctrl_inner,
            textvariable=self.var_hotkey_status,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))
        tk.Label(
            ctrl_inner,
            textvariable=self.var_trigger_seen,
            bg=C_PANEL,
            fg=C_GREEN,
            font=("Consolas", 10),
        ).pack(anchor="w", pady=(4, 0))

        # Voice after Fly controls so keys/HOTAS stay at the top of the tab.
        self._build_setup_voice(panel)

        # --- Frequency gate ---
        fg = tk.Frame(panel, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        fg.pack(fill=tk.X, padx=14, pady=(0, 8))
        fg_inner = tk.Frame(fg, bg=C_PANEL)
        fg_inner.pack(fill=tk.X, padx=12, pady=10)
        ttk.Label(fg_inner, text="Frequency gate", style="Header.TLabel").pack(anchor="w")
        tk.Label(
            fg_inner,
            text=(
                "External Advance / Previous / voice / Stream Deck URL only fire when you are "
                "tuned to the step frequency on any radio — not only the one you are keying. "
                "Talking on intra-flight VHF will not block UHF ATC / C2 triggers. "
                "On-screen Play is never blocked. "
                "Tune is read from the DCS radio export (in-jet) and/or the live SRS radio bank "
                "(common PTT is TX only). External AWACS below is only for the manual Fly EAM strip "
                "when testing without a jet / without SRS UDP."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))
        self.var_freq_gate_enabled = tk.BooleanVar(
            value=bool(self.config_data.get("freq_gate_enabled", True))
        )
        self.var_freq_gate_eam = tk.BooleanVar(
            value=bool(self.config_data.get("freq_gate_eam_enabled"))
        )
        ttk.Checkbutton(
            fg_inner,
            text="Require correct frequency for external triggers",
            variable=self.var_freq_gate_enabled,
            command=self._on_freq_gate_options_changed,
        ).pack(anchor="w")
        ttk.Checkbutton(
            fg_inner,
            text="External AWACS radio source (testing) — show Fly EAM strip / manual freqs",
            variable=self.var_freq_gate_eam,
            command=self._on_freq_gate_options_changed,
        ).pack(anchor="w", pady=(4, 0))
        fg_btns = tk.Frame(fg_inner, bg=C_PANEL)
        fg_btns.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(
            fg_btns,
            text="Install DCS radio export…",
            command=self._install_dcs_radio_export,
        ).pack(side=tk.LEFT)
        ttk.Button(
            fg_btns,
            text="Check status",
            command=self._status_dcs_radio_export,
        ).pack(side=tk.LEFT, padx=(6, 0))

        # --- Picture call range ---
        pic = tk.Frame(panel, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        pic.pack(fill=tk.X, padx=14, pady=(0, 8))
        pic_inner = tk.Frame(pic, bg=C_PANEL)
        pic_inner.pack(fill=tk.X, padx=12, pady=10)
        ttk.Label(pic_inner, text="Picture call", style="Header.TLabel").pack(anchor="w")
        tk.Label(
            pic_inner,
            text=(
                "Blackjack / Bandsaw picture, bogey dope, and declare only use contacts "
                "inside this range of your jet (AWACS and tankers are always skipped). "
                "A group's bogey / bandit / spades / hostile call sticks until Bandsaw "
                "or you declare it hostile. Raise the range for testing."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))
        try:
            pic_default = float(
                self.config_data.get("picture_max_range_nm")
                or voice_actions.PICTURE_MAX_RANGE_NM
            )
        except (TypeError, ValueError):
            pic_default = float(voice_actions.PICTURE_MAX_RANGE_NM)
        self.var_picture_max_range = tk.StringVar(value=f"{pic_default:g}")
        pic_row = tk.Frame(pic_inner, bg=C_PANEL)
        pic_row.pack(anchor="w")
        tk.Label(
            pic_row,
            text="Max range (NM)",
            bg=C_PANEL,
            fg=C_LABEL,
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT)
        ttk.Entry(pic_row, textvariable=self.var_picture_max_range, width=8).pack(
            side=tk.LEFT, padx=(8, 6)
        )
        tk.Label(
            pic_row,
            text="default 150",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT)

        # --- Tanker boom small talk ---
        boom = tk.Frame(panel, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        boom.pack(fill=tk.X, padx=14, pady=(0, 8))
        boom_inner = tk.Frame(boom, bg=C_PANEL)
        boom_inner.pack(fill=tk.X, padx=12, pady=10)
        ttk.Label(boom_inner, text="Tanker boom chat", style="Header.TLabel").pack(
            anchor="w"
        )
        tk.Label(
            boom_inner,
            text=(
                "After you request rejoin, Texaco starts boom chat — you do not ask "
                "first. Auto-fires after 30–60 seconds at 0.1–0.5 NM from the tanker "
                "with a jet in that envelope (you count). First call is a hello "
                "(Good morning / afternoon / evening, sir); later bits mix A/B polls, "
                "open questions, and random small-talk riffs — answer in a word, say "
                "anything, or just listen; Texaco keeps going with short breaks until "
                "you Stop chat / say “talk later”, or leave the tanker. With Ollama / "
                "Gemini / OpenAI on, you can freestyle past the A/B buttons and Texaco "
                "riffs live on what you said for a few turns, then rotates. Ollama needs "
                "a pulled model (e.g. ollama pull llama3.2) — Fly shows when it falls "
                "back to the library. Fly Texaco starts chat only on tanker frequency."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))
        self.var_tanker_chat_llm = tk.StringVar(
            value=str(self.config_data.get("tanker_chat_llm") or "off").strip().lower()
            or "off"
        )
        self.var_tanker_chat_llm_key = tk.StringVar(
            value=str(self.config_data.get("tanker_chat_llm_key") or "")
        )
        llm_row = tk.Frame(boom_inner, bg=C_PANEL)
        llm_row.pack(anchor="w")
        for label, value in (
            ("Off (library only)", "off"),
            ("Ollama (local, free)", "ollama"),
            ("Auto (from key)", "auto"),
            ("Gemini", "gemini"),
            ("OpenAI", "openai"),
        ):
            ttk.Radiobutton(
                llm_row,
                text=label,
                variable=self.var_tanker_chat_llm,
                value=value,
                style="Panel.TRadiobutton",
            ).pack(side=tk.LEFT, padx=(0, 12))
        key_row = tk.Frame(boom_inner, bg=C_PANEL)
        key_row.pack(anchor="w", pady=(8, 0))
        tk.Label(
            key_row,
            text="API key",
            bg=C_PANEL,
            fg=C_LABEL,
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT)
        ttk.Entry(
            key_row,
            textvariable=self.var_tanker_chat_llm_key,
            width=44,
            show="*",
        ).pack(side=tk.LEFT, padx=(8, 6))
        tk.Label(
            key_row,
            text="Gemini (AIza…) · OpenAI (sk-…) · Ollama needs none",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT)

        # --- Automatic clearances from live position ---
        ac = tk.Frame(panel, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        ac.pack(fill=tk.X, padx=14, pady=(0, 8))
        ac_inner = tk.Frame(ac, bg=C_PANEL)
        ac_inner.pack(fill=tk.X, padx=12, pady=10)
        ttk.Label(
            ac_inner, text="Automatic clearances (live position)", style="Header.TLabel"
        ).pack(anchor="w")
        tk.Label(
            ac_inner,
            text=(
                "Must be on for anything to auto-fire. Watches the Opus CAOC radar feed "
                "and fires when the flight is where it needs to be — monitor tower at the "
                "assigned EOR, takeoff when lined up, range exit at 40 NM from Nellis or "
                "inside the approach circle, contact tower at 12 NM. Overhead / TAC "
                "landing clearance is gear-down / Play (not the 2 NM field gate). The Fly tab line "
                "shows the live wait (NM remaining, in-zone count, or why the feed "
                "cannot see you). Distance gates work far from the field; EOR / lineup "
                "only fire in those drawn boxes. Each fires once per sortie and re-arms "
                "on Reset."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))
        self.var_auto_clearance = tk.BooleanVar(
            value=bool(self.config_data.get("auto_clearance_enabled"))
        )
        self.var_auto_takeoff = tk.BooleanVar(
            value=bool(self.config_data.get("auto_takeoff_clearance", True))
        )
        self.var_auto_monitor = tk.BooleanVar(
            value=bool(self.config_data.get("auto_monitor_tower", True))
        )
        self.var_auto_full_flight = tk.BooleanVar(
            value=bool(self.config_data.get("auto_clearance_require_full_flight", True))
        )
        ttk.Checkbutton(
            ac_inner,
            text="Watch live position and fire clearances automatically",
            variable=self.var_auto_clearance,
            command=self._on_auto_clearance_changed,
        ).pack(anchor="w")
        ttk.Checkbutton(
            ac_inner,
            text="Line up and wait / cleared for takeoff when in position on the runway",
            variable=self.var_auto_takeoff,
            command=self._on_auto_clearance_changed,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(
            ac_inner,
            text="Monitor tower when the flight reaches the EOR",
            variable=self.var_auto_monitor,
            command=self._on_auto_clearance_changed,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(
            ac_inner,
            text="Require every flight member (off = your jet alone is enough)",
            variable=self.var_auto_full_flight,
            command=self._on_auto_clearance_changed,
        ).pack(anchor="w", pady=(4, 0))

        # The areas these watch have to be traced once per field, so the way to do
        # that belongs next to the switches that depend on it.
        zone_row = tk.Frame(ac_inner, bg=C_PANEL)
        zone_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(
            zone_row, text="Draw zones on a map…", command=self._open_zone_editor
        ).pack(side=tk.LEFT)
        self.var_zone_count = tk.StringVar(value="")
        tk.Label(
            zone_row,
            textvariable=self.var_zone_count,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(
            ac_inner,
            text=(
                "Opens a satellite map in your browser: trace the runway, the EOR and "
                "any area of your own, say what each one fires, press Save. Saved areas "
                "appear here and in Fires when on Plan Flight within a few seconds. "
                "Open-Zone-Editor.cmd next to this app does the same thing on its own."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))
        self._update_zone_hint()

        if hasattr(self, "_setup_controls_canvas"):
            self.after_idle(
                lambda: self._setup_controls_canvas.configure(
                    scrollregion=self._setup_controls_canvas.bbox("all")
                )
            )

    def _build_setup_voice(self, panel: tk.Frame) -> None:
        self.var_voice_enabled = tk.BooleanVar(value=False)
        self.var_voice_model = tk.StringVar(value=voice_engine.DEFAULT_MODEL)
        self.var_voice_mic = tk.StringVar(value="(Windows default)")
        self.var_voice_confidence = tk.DoubleVar(value=voice_engine.DEFAULT_MIN_CONFIDENCE)
        self.var_voice_require_address = tk.BooleanVar(value=True)
        self.var_voice_nlu = tk.BooleanVar(
            value=bool(self.config_data.get("voice_nlu_enabled", True))
        )
        self.var_voice_status = tk.StringVar(value="Voice control off")
        self.var_voice_heard = tk.StringVar(value="")
        self.var_voice_ptt = tk.StringVar(value="(auto: SRS PTT)")
        self.var_voice_ptt_key = tk.StringVar(value="(none)")

        box = tk.Frame(panel, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        box.pack(fill=tk.X, padx=14, pady=(0, 8))
        inner = tk.Frame(box, bg=C_PANEL)
        inner.pack(fill=tk.X, padx=12, pady=10)

        ttk.Label(inner, text="Voice control (Whisper)", style="Header.TLabel").pack(anchor="w")
        tk.Label(
            inner,
            text=(
                "Hold your SRS PTT and talk; ATC answers calls made to it. You do not have to "
                "get the wording right — close counts. The Fly tab lists the calls that fit "
                "where you are. Runs on the CPU so it does not compete with DCS for the GPU. "
                "Flight chatter on the same frequency is ignored, and the Fly log shows why "
                "anything was passed over."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))

        row1 = tk.Frame(inner, bg=C_PANEL)
        row1.pack(fill=tk.X, pady=3)
        ttk.Checkbutton(
            row1, text="Enable voice control", variable=self.var_voice_enabled
        ).pack(side=tk.LEFT)
        tk.Label(row1, text="Model", bg=C_PANEL, fg=C_LABEL).pack(side=tk.LEFT, padx=(20, 6))
        tk.Label(
            row1,
            textvariable=self.var_voice_model,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Consolas", 9),
            width=12,
            anchor="w",
            padx=6,
        ).pack(side=tk.LEFT, ipady=3)
        ttk.Button(row1, text="Choose…", command=self._choose_voice_model).pack(side=tk.LEFT, padx=(6, 0))
        tk.Label(
            row1,
            text="tiny ≈200ms · base ≈350ms",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=8)

        row2 = tk.Frame(inner, bg=C_PANEL)
        row2.pack(fill=tk.X, pady=3)
        tk.Label(row2, text="Microphone", bg=C_PANEL, fg=C_LABEL, width=16, anchor="w").pack(side=tk.LEFT)
        # Device names come from winmm; numpy is only needed to capture/transcribe.
        self._mic_devices = mic_capture.list_input_devices()
        tk.Label(
            row2,
            textvariable=self.var_voice_mic,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Consolas", 9),
            width=42,
            anchor="w",
            padx=6,
        ).pack(side=tk.LEFT, padx=(8, 6), ipady=3)
        ttk.Button(row2, text="Choose…", command=self._choose_voice_mic).pack(side=tk.LEFT)

        row3 = tk.Frame(inner, bg=C_PANEL)
        row3.pack(fill=tk.X, pady=3)
        tk.Label(row3, text="PTT button", bg=C_PANEL, fg=C_LABEL, width=16, anchor="w").pack(side=tk.LEFT)
        tk.Label(
            row3,
            textvariable=self.var_voice_ptt,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Consolas", 9),
            width=46,
            anchor="w",
            padx=6,
        ).pack(side=tk.LEFT, padx=(8, 6), ipady=3)
        ttk.Button(row3, text="Learn…", command=self._learn_voice_ptt).pack(side=tk.LEFT)
        ttk.Button(row3, text="Use SRS", command=self._use_srs_ptt_for_voice).pack(side=tk.LEFT, padx=4)

        row3b = tk.Frame(inner, bg=C_PANEL)
        row3b.pack(fill=tk.X, pady=3)
        tk.Label(row3b, text="or PTT key", bg=C_PANEL, fg=C_LABEL, width=16, anchor="w").pack(side=tk.LEFT)
        tk.Label(
            row3b,
            textvariable=self.var_voice_ptt_key,
            bg=C_CARD,
            fg=C_TEXT,
            font=("Consolas", 9),
            width=46,
            anchor="w",
            padx=6,
        ).pack(side=tk.LEFT, padx=(8, 6), ipady=3)
        ttk.Button(row3b, text="Capture…", command=self._capture_voice_ptt_key).pack(side=tk.LEFT)
        ttk.Button(row3b, text="Clear", command=self._clear_voice_ptt_key).pack(side=tk.LEFT, padx=4)

        row4 = tk.Frame(inner, bg=C_PANEL)
        row4.pack(fill=tk.X, pady=3)
        tk.Label(row4, text="Min confidence", bg=C_PANEL, fg=C_LABEL, width=16, anchor="w").pack(side=tk.LEFT)
        self.var_voice_confidence_label = tk.StringVar(value="")
        ttk.Scale(
            row4,
            from_=0.40,
            to=0.95,
            variable=self.var_voice_confidence,
            length=220,
            command=self._on_voice_confidence_slide,
        ).pack(side=tk.LEFT, padx=(8, 6))
        tk.Label(
            row4,
            textvariable=self.var_voice_confidence_label,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Consolas", 8),
            width=5,
            anchor="w",
        ).pack(side=tk.LEFT)
        self._sync_voice_confidence_label()

        row5 = tk.Frame(inner, bg=C_PANEL)
        row5.pack(fill=tk.X, pady=(6, 0))
        ttk.Checkbutton(
            row5,
            text="Only act on calls addressed to ATC",
            variable=self.var_voice_require_address,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(
            row5,
            text="Understand messy radio (LLM)",
            variable=self.var_voice_nlu,
        ).pack(side=tk.LEFT, padx=(18, 0))
        tk.Label(
            inner,
            text=(
                "The flight shares this frequency. With this on, a transmission only counts "
                "when you open with the agency (\u201cNellis Tower, \u2026\u201d) or your own callsign — "
                "so \u201cTwo, go button five\u201d and general chatter never move the timeline. "
                "Messy radio uses the same Ollama/Gemini/OpenAI setting as boom chat: if the "
                "keyword matcher misses, the model may pick an allowed call for this freq "
                "(taxi, picture, \u2026). It never writes a new clearance. Needs boom-chat LLM on."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        ttk.Button(inner, text="Apply voice", command=self._apply_voice).pack(anchor="w", pady=(8, 2))
        tk.Label(
            inner,
            textvariable=self.var_voice_status,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w")
        tk.Label(
            inner,
            textvariable=self.var_voice_heard,
            bg=C_PANEL,
            fg=C_GREEN,
            font=("Consolas", 9),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

    def _learn_voice_ptt(self) -> None:
        previous = self.var_voice_ptt.get()
        self.var_voice_ptt.set("Press your PTT button…")

        def done(binding: dict[str, Any]) -> None:
            def apply() -> None:
                normalized = joystick.normalize_binding(binding)
                self.config_data["voice_ptt"] = [normalized] if normalized else []
                self.var_voice_ptt.set(joystick.describe_binding(normalized))
                self._apply_voice()

            self.after(0, apply)

        def cancel() -> None:
            if self.var_voice_ptt.get().startswith("Press your PTT"):
                self._joystick.learn_next_press(None)
                self.var_voice_ptt.set(previous)

        self._joystick.learn_next_press(done)
        self.after(10000, cancel)

    def _use_srs_ptt_for_voice(self) -> None:
        found = joystick.discover_srs_ptt()
        if not found:
            messagebox.showinfo("SRS PTT", "No SRS transmit binding matched a connected device.")
            return
        self.config_data["voice_ptt"] = [joystick.normalize_binding(b) for b in found]
        self.var_voice_ptt.set(", ".join(joystick.describe_binding(b) for b in found))
        self._apply_voice()

    def _setup_field(
        self,
        parent: tk.Frame,
        row: int,
        label: str,
        var: tk.StringVar,
        *,
        width: int = 36,
    ) -> ttk.Entry:
        tk.Label(parent, text=label, bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).grid(
            row=row, column=0, sticky="w", pady=3
        )
        ent = ttk.Entry(parent, textvariable=var, width=width)
        ent.grid(row=row, column=1, sticky="we", pady=3, padx=(8, 0))
        return ent

    def _clear_setup_runway_override(self) -> None:
        """Blank Manual runway so wind / flight-plan selection takes over."""
        if hasattr(self, "var_runway_override"):
            self.var_runway_override.set("")
        self.config_data["runway_override"] = ""

    @staticmethod
    def _short_voice(name: str) -> str:
        s = (name or "").replace("Microsoft ", "")
        s = re.sub(r"\s*-\s*English\s*\([^)]*\)\s*$", "", s, flags=re.IGNORECASE)
        s = s.replace(" Desktop", "").strip()
        if not s:
            return ""
        gender = atc_phrase.voice_gender(name)
        # Keep agency grid readable: short name + gender
        base = s if len(s) <= 22 else s[:19] + "…"
        return f"{base} · {gender}"

    def _set_agency_voice(self, channel: str, voice: str) -> None:
        voice = (voice or "").strip()
        if atc_phrase.channel_requires_female(channel):
            cfg = dict(self.config_data)
            if hasattr(self, "var_tts_provider"):
                cfg["tts_provider"] = atc_phrase.tts_provider(
                    {"tts_provider": self.var_tts_provider.get()}
                )
            voice = atc_phrase.ensure_female_voice(cfg, voice)
        self.voice_vars[channel].set(voice)
        if channel in self.voice_display_vars:
            self.voice_display_vars[channel].set(self._short_voice(voice))

    def _list_windows_voices(self) -> list[str]:
        return atc_phrase.list_windows_voices()

    def _list_voices(self) -> list[str]:
        if atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()}) == "google":
            return atc_phrase.google_voice_choices()
        return self._list_windows_voices()

    def _windows_voice_status_extra(self) -> str:
        if atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()}) == "google":
            return ""
        pending = atc_phrase.windows_onecore_voices_pending()
        if not pending:
            return ""
        short = []
        for n in pending:
            label = n.replace("Microsoft ", "")
            label = re.sub(r"\s*-\s*English\s*\([^)]*\)\s*$", "", label, flags=re.IGNORECASE)
            short.append(label.strip() or n)
        shown = ", ".join(short[:5])
        more = f" +{len(short) - 5}" if len(short) > 5 else ""
        return f" · OneCore locked: {shown}{more} — Unlock OneCore…"

    def _unlock_onecore_voices(self) -> None:
        if os.name != "nt":
            messagebox.showinfo("OneCore", "OneCore unlock is Windows-only.")
            return
        pending = atc_phrase.windows_onecore_voices_pending()
        if not pending:
            messagebox.showinfo(
                "OneCore",
                "No locked OneCore voices found — System.Speech already sees the installed set.",
            )
            self._refresh_voice_list()
            return
        names = "\n".join(f"• {n}" for n in pending[:12])
        if len(pending) > 12:
            names += f"\n• …and {len(pending) - 12} more"
        if not messagebox.askokcancel(
            "Unlock OneCore voices",
            "Windows will ask for Administrator permission.\n\n"
            "This copies OneCore TTS registry tokens into classic Speech so "
            "Preview / SRS / ExternalAudio can use:\n\n"
            f"{names}\n\n"
            "After unlock, click Refresh list (restart ExternalAudio if it was open).",
        ):
            return
        ok, msg = atc_phrase.unlock_windows_onecore_voices(elevate=True)
        self._refresh_voice_list()
        if ok:
            messagebox.showinfo("OneCore", msg)
        else:
            messagebox.showerror("OneCore", msg)

    def _fill_voice_listbox(self, lb: tk.Listbox, voices: list[str], current: str = "") -> None:
        """Populate picker with locale headers and voice · gender lines."""
        lb.delete(0, tk.END)
        self._voice_listbox_values: list[str | None] = []
        voices = atc_phrase.sort_voices(voices)
        last_locale = object()
        select_idx: int | None = None
        for name in voices:
            loc = atc_phrase.voice_locale(name)
            if loc != last_locale:
                header = f"── {atc_phrase.locale_label(loc)} ──"
                lb.insert(tk.END, header)
                lb.itemconfig(tk.END, foreground=C_MUTED)
                self._voice_listbox_values.append(None)
                last_locale = loc
            lb.insert(tk.END, f"  {atc_phrase.voice_label(name)}")
            self._voice_listbox_values.append(name)
            if name == current:
                select_idx = len(self._voice_listbox_values) - 1
        if select_idx is not None:
            lb.selection_set(select_idx)
            lb.see(select_idx)

    def _selected_voice_from_listbox(self, lb: tk.Listbox) -> str | None:
        sel = lb.curselection()
        if not sel:
            return None
        values = getattr(self, "_voice_listbox_values", [])
        idx = int(sel[0])
        if idx < 0 or idx >= len(values):
            return None
        return values[idx]

    def _browse_google_credentials(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Google Cloud service-account JSON",
            filetypes=[("JSON credentials", "*.json"), ("All files", "*.*")],
            initialdir=str(HERE / "secrets"),
        )
        if path:
            try:
                pinned = atc_phrase.pin_google_credentials_to_secrets(path)
                self.var_google_credentials.set(str(pinned))
            except OSError as exc:
                self.var_google_credentials.set(path)
                messagebox.showwarning(
                    "Google TTS",
                    f"Could not copy the JSON into atc\\secrets\\:\n{exc}\n\n"
                    "Using the original path. Prefer keeping the key only on this Host.",
                )
            self._update_tts_status()

    def _paste_google_credentials(self) -> None:
        """
        Save pasted service-account JSON into atc/secrets/google-tts.json.
        A plain Google 'API key' (AIza...) will not work — this explains and rejects it.
        """
        dlg = tk.Toplevel(self)
        dlg.title("Paste Google service-account JSON")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        dlg.grab_set()
        self._place_dialog(dlg, 560, 420)

        tk.Label(
            dlg,
            text="Paste the full service-account JSON below",
            bg=C_BG,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", padx=12, pady=(12, 4))
        tk.Label(
            dlg,
            text=(
                "This must look like a downloaded key file (includes \"type\": \"service_account\" "
                "and a private_key). A short API key starting with AIza will NOT work."
            ),
            bg=C_BG,
            fg=C_MUTED,
            font=("Segoe UI", 9),
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        txt = tk.Text(
            dlg,
            wrap=tk.NONE,
            bg=C_CARD,
            fg=C_TEXT,
            insertbackground=C_TEXT,
            font=("Consolas", 9),
            relief=tk.FLAT,
            padx=8,
            pady=8,
        )
        txt.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        def save() -> None:
            raw = txt.get("1.0", tk.END).strip()
            if not raw:
                messagebox.showerror("Paste JSON", "Nothing pasted.", parent=dlg)
                return
            # Common mistake: API key string only
            if raw.startswith("AIza") or (len(raw) < 80 and "{" not in raw):
                messagebox.showerror(
                    "Wrong key type",
                    "That looks like a Google API key string (AIza…), not a service-account JSON file.\n\n"
                    "This app needs a JSON key file:\n"
                    "Google Cloud Console → APIs & Services → Credentials →\n"
                    "your Service account → Keys → Add key → Create new key → JSON.\n\n"
                    "Then paste that whole JSON here, or Browse to the downloaded .json file.\n\n"
                    "See Help → Google Cloud TTS setup.",
                    parent=dlg,
                )
                return
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                messagebox.showerror(
                    "Paste JSON",
                    f"Not valid JSON.\n\n{exc}\n\n"
                    "Copy the entire downloaded key file contents, including { and }.",
                    parent=dlg,
                )
                return
            if not isinstance(data, dict) or data.get("type") != "service_account":
                messagebox.showerror(
                    "Wrong key type",
                    "JSON must be a service account key (\"type\": \"service_account\").\n\n"
                    "An API key or OAuth client JSON will not work with ExternalAudio.\n"
                    "Create a Service account key (JSON) in Google Cloud Console.",
                    parent=dlg,
                )
                return
            if not data.get("private_key") or not data.get("client_email"):
                messagebox.showerror(
                    "Paste JSON",
                    "This JSON is missing private_key / client_email.\n"
                    "Download a new key: Service account → Keys → Create new key → JSON.",
                    parent=dlg,
                )
                return

            secrets_dir = HERE / "secrets"
            secrets_dir.mkdir(parents=True, exist_ok=True)
            out = secrets_dir / "google-tts.json"
            out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            self.var_google_credentials.set(str(out))
            self.var_tts_provider.set("google")
            self._on_tts_provider_change()
            self._update_tts_status()
            dlg.destroy()
            messagebox.showinfo(
                "Saved",
                f"Credentials saved to:\n{out}\n\n"
                "Google mode is selected. Click Save setup, then pick voices on the Voices tab.",
            )

        btns = tk.Frame(dlg, bg=C_BG)
        btns.pack(fill=tk.X, padx=12, pady=(4, 12))
        ttk.Button(
            btns,
            text="Open Help",
            command=lambda: (dlg.destroy(), self._show_help_tab("Google Cloud TTS setup")),
        ).pack(side=tk.LEFT)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Save JSON file", style="Accent.TButton", command=save).pack(
            side=tk.RIGHT, padx=8
        )

    def _update_tts_status(self) -> None:
        provider = atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()})
        role = (
            str(self.var_atc_role.get() or "solo").strip().lower()
            if hasattr(self, "var_atc_role")
            else atc_net.role_of(self.config_data)
        )
        if role == "client":
            self.google_cred_frame.pack_forget()
            if not self._tts_status_windows.winfo_ismapped():
                self._tts_status_windows.pack(anchor="w", pady=(0, 4))
            self._tts_status_windows.configure(
                text=(
                    "Client PCs do not load the squadron Google JSON. The Host "
                    "speaks Neural2 on SRS. Local Hear uses Windows voices. Share "
                    "only the squadron token — never the key file."
                )
            )
        elif provider == "google":
            self._tts_status_windows.pack_forget()
            if not self.google_cred_frame.winfo_ismapped():
                self.google_cred_frame.pack(fill=tk.X, pady=(0, 4))
            raw = self.var_google_credentials.get().strip()
            if not raw:
                self.var_tts_status.set("Select your service-account JSON, then assign voices on Voices.")
            else:
                path = Path(os.path.expandvars(os.path.expanduser(raw)))
                if path.is_file():
                    self.var_tts_status.set(
                        f"OK · {path.name} · stays on this Host · preview with Hear locally"
                    )
                else:
                    self.var_tts_status.set(f"File not found: {path}")
        else:
            self.google_cred_frame.pack_forget()
            if not self._tts_status_windows.winfo_ismapped():
                self._tts_status_windows.pack(anchor="w", pady=(0, 4))
            self._tts_status_windows.configure(
                text=(
                    "Windows voices — no API key. Assign per agency on Voices. "
                    "Extra installed voices (Linda / Mark / Richard) need Unlock OneCore… once (admin)."
                )
            )
        n = len(self.voice_labels or self._list_voices())
        self.var_voice_status.set(
            f"{provider.title()} mode · {n} voice(s) available · Choose per agency or Randomize ▾"
            + self._windows_voice_status_extra()
        )

    def _refresh_voice_list(self) -> None:
        self.voice_labels = self._list_voices()
        self.var_voice_status.set(
            f"Refreshed · {len(self.voice_labels)} voice(s) for "
            f"{atc_phrase.tts_provider({'tts_provider': self.var_tts_provider.get()})} mode"
            + self._windows_voice_status_extra()
        )
        self._refresh_tts_usage()

    def _refresh_tts_usage(self) -> None:
        """Update local monthly Google TTS usage / free-tier estimator."""
        if not hasattr(self, "var_tts_usage"):
            return
        try:
            # Only sync UI → config after Setup fields are loaded; early refresh during
            # panel build would otherwise push empty Opus StringVars into config_data.
            if getattr(self, "_setup_fields_loaded", False):
                self._sync_identity_to_config()
            before_provider = atc_phrase.tts_provider(self.config_data)
            before_voices = dict(self.config_data.get("tts_voices") or {})
            retire_msgs = atc_phrase.migrate_retired_google_voices(self.config_data)
            guard_msgs = list(retire_msgs) + atc_phrase.apply_free_tier_guard_to_config(
                self.config_data
            )
            after_provider = atc_phrase.tts_provider(self.config_data)
            after_voices = dict(self.config_data.get("tts_voices") or {})
            if guard_msgs and (
                before_provider != after_provider or before_voices != after_voices
            ):
                self.var_tts_provider.set(after_provider)
                if hasattr(self, "voice_vars") and isinstance(after_voices, dict):
                    for ch in self.voice_vars:
                        if ch in after_voices and after_voices[ch]:
                            self._set_agency_voice(ch, str(after_voices[ch]))
                self._on_tts_provider_change()

            summary = atc_phrase.tts_usage_summary()
            self.var_tts_usage.set("\n".join(atc_phrase.format_tts_usage_lines(summary)))
            warn_lines = atc_phrase.free_tier_warning_lines(summary) + list(guard_msgs or [])
            seen: set[str] = set()
            uniq: list[str] = []
            for line in warn_lines:
                if line not in seen:
                    seen.add(line)
                    uniq.append(line)
            if hasattr(self, "var_tts_usage_warn"):
                self.var_tts_usage_warn.set("\n".join(uniq))
        except Exception as exc:  # noqa: BLE001
            self.var_tts_usage.set(f"Usage unavailable: {exc}")
            if hasattr(self, "var_tts_usage_warn"):
                self.var_tts_usage_warn.set("")

    def _choose_voice_model(self) -> None:
        choices = list(voice_engine.MODEL_CHOICES)
        cur = self.var_voice_model.get().strip()
        picked = self._pick_list_value(
            title="Whisper model",
            heading="Voice recognition model",
            choices=choices,
            current=cur if cur in choices else voice_engine.DEFAULT_MODEL,
        )
        if picked:
            self.var_voice_model.set(picked)

    def _choose_voice_mic(self) -> None:
        self._mic_devices = mic_capture.list_input_devices()
        names = [str(d["name"]) for d in self._mic_devices]
        if not names:
            if mic_capture.np is None:
                messagebox.showinfo(
                    "Microphone",
                    "Voice capture needs numpy.\n\n"
                    "Run:  py -3 -m pip install numpy\n"
                    "Then restart the app.",
                )
            else:
                messagebox.showinfo(
                    "Microphone",
                    "No Windows waveIn input devices found.\n"
                    "Check that a microphone is enabled in Sound settings.",
                )
            return
        cur = self.var_voice_mic.get().strip()
        picked = self._pick_list_value(
            title="Microphone",
            heading="Input device for voice control",
            choices=names,
            current=cur if cur in names else names[0],
        )
        if picked:
            self.var_voice_mic.set(picked)

    def _choose_voice(self, channel: str) -> None:
        """Listbox picker — more reliable than ttk.Combobox popdowns on this dark UI."""
        voices = self._list_voices()
        if atc_phrase.channel_requires_female(channel):
            voices = [v for v in voices if atc_phrase.voice_gender(v) == "female"]
        self.voice_labels = voices
        if not voices:
            messagebox.showwarning(
                "Voices",
                "No female voices available for tanker.\n"
                "Unlock OneCore or switch TTS provider."
                if atc_phrase.channel_requires_female(channel)
                else "No voices available for the current TTS provider.",
            )
            return

        current = self.voice_vars[channel].get().strip()
        dlg = tk.Toplevel(self)
        dlg.title(f"Choose voice — {channel}")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        dlg.grab_set()
        self._place_dialog(dlg, 420, 380)

        tk.Label(
            dlg,
            text=(
                f"Agency: {channel} — female voices only"
                if atc_phrase.channel_requires_female(channel)
                else f"Agency: {channel}"
            ),
            bg=C_BG,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", padx=12, pady=(12, 6))

        lb = tk.Listbox(
            dlg,
            bg=C_CARD,
            fg=C_TEXT,
            selectbackground=C_ACCENT,
            selectforeground="#061018",
            font=("Segoe UI", 10),
            activestyle="none",
            highlightthickness=1,
            highlightbackground=C_BORDER,
            relief=tk.FLAT,
        )
        lb.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
        self._fill_voice_listbox(lb, voices, current)

        custom_fr = tk.Frame(dlg, bg=C_BG)
        custom_fr.pack(fill=tk.X, padx=12, pady=6)
        custom_var = tk.StringVar(value=current if current and current not in voices else "")
        if atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()}) == "google":
            tk.Label(custom_fr, text="Or type Google voice id:", bg=C_BG, fg=C_MUTED).pack(anchor="w")
            ttk.Entry(custom_fr, textvariable=custom_var).pack(fill=tk.X, pady=4)

        def resolve_voice() -> str:
            typed = custom_var.get().strip()
            picked = self._selected_voice_from_listbox(lb)
            if typed and atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()}) == "google":
                return typed
            if picked:
                return picked
            return typed

        def apply_choice(voice: str) -> None:
            voice = voice.strip()
            if not voice:
                return
            self._set_agency_voice(channel, voice)
            dlg.destroy()

        def on_ok() -> None:
            pick = resolve_voice()
            if pick:
                apply_choice(pick)

        def on_preview() -> None:
            pick = resolve_voice()
            if atc_phrase.channel_requires_female(channel):
                cfg = dict(self.config_data)
                cfg["tts_provider"] = atc_phrase.tts_provider(
                    {"tts_provider": self.var_tts_provider.get()}
                )
                pick = atc_phrase.ensure_female_voice(cfg, pick)
            self._preview_voice_sample(pick)

        btns = tk.Frame(dlg, bg=C_BG)
        btns.pack(fill=tk.X, padx=12, pady=(4, 12))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Select", style="Accent.TButton", command=on_ok).pack(side=tk.RIGHT, padx=8)
        ttk.Button(btns, text="Preview", command=on_preview).pack(side=tk.LEFT)

        def _lb_wheel(event: tk.Event) -> str:
            delta = int(-1 * (event.delta / 120)) if event.delta else 0
            if delta:
                lb.yview_scroll(delta, "units")
            return "break"

        lb.bind("<Double-Button-1>", lambda _e: on_ok())
        lb.bind("<MouseWheel>", _lb_wheel)
        dlg.bind("<Return>", lambda _e: on_ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    def _preview_voice_sample(self, voice: str) -> None:
        """Play a short local sample for a voice id (Windows or Google)."""
        voice = (voice or "").strip()
        if not voice:
            messagebox.showinfo("Preview", "Select or type a voice first.")
            return
        self._sync_identity_to_config()
        vol = float(self.config_data.get("tts_volume", 0.8))
        speed = atc_phrase.tts_speed(self.config_data)
        provider = atc_phrase.tts_provider(self.config_data)
        google_creds = atc_phrase.google_credentials_path(self.config_data)
        use_google = (
            self._atc_role() != "client"
            and (
                provider == "google" or atc_phrase.is_google_voice_name(voice)
            )
            and google_creds is not None
        )
        sample = atc_phrase.VOICE_PREVIEW_SAMPLE

        def work() -> None:
            try:
                atc_phrase.preview_voice_local(
                    voice,
                    sample,
                    vol,
                    speed=speed,
                    google_credentials=google_creds if use_google else None,
                )
                if use_google:
                    self.after(0, self._refresh_tts_usage)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: messagebox.showerror("Preview", e))

        threading.Thread(target=work, daemon=True).start()

    def _rebuild_randomize_menu(self) -> None:
        """Provider-aware Randomize ▾ entries (Windows vs Google families)."""
        menu = getattr(self, "_rand_menu", None)
        if menu is None:
            return
        menu.delete(0, tk.END)
        provider = atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()})
        menu.add_command(
            label="All voices (unique)",
            command=lambda: self._randomize_agency_voices(family=None),
        )
        menu.add_separator()
        if provider == "google":
            menu.add_command(
                label="Chirp 3: HD only",
                command=lambda: self._randomize_agency_voices(family="chirp"),
            )
            menu.add_command(
                label="Neural2 only",
                command=lambda: self._randomize_agency_voices(family="neural2"),
            )
            menu.add_command(
                label="WaveNet only",
                command=lambda: self._randomize_agency_voices(family="wavenet"),
            )
        else:
            menu.add_command(
                label="Male only",
                command=lambda: self._randomize_agency_voices(family="male"),
            )
            menu.add_command(
                label="Female only",
                command=lambda: self._randomize_agency_voices(family="female"),
            )

    def _randomize_agency_voices(self, family: str | None = None) -> None:
        """
        Assign voices randomly; prefer unique voices when the catalog is large enough.
        family: None = full catalog;
          Google: 'chirp' / 'neural2' / 'wavenet';
          Windows: 'male' / 'female'.
        """
        voices = self._list_voices()
        self.voice_labels = voices
        if not voices:
            messagebox.showwarning("Voices", "No voices available for the current TTS provider.")
            return

        provider = atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()})
        google_families = {"chirp", "neural2", "wavenet"}
        gender_families = {"male", "female"}

        if family in google_families and provider != "google":
            messagebox.showinfo(
                "Randomize",
                "Chirp / Neural2 / WaveNet filters need Google Cloud TTS mode.",
            )
            return
        if family in gender_families and provider == "google":
            # Still allow gender filter on Google catalog — useful and not Windows-only.
            pass

        if family in google_families:
            pool = [v for v in voices if atc_phrase.voice_billing_family(v) == family]
            label = {"chirp": "Chirp 3: HD", "neural2": "Neural2", "wavenet": "WaveNet"}.get(
                family, family
            )
            if not pool:
                messagebox.showwarning("Randomize", f"No {label} voices in the current list.")
                return
        elif family in gender_families:
            pool = [v for v in voices if atc_phrase.voice_gender(v) == family]
            label = family
            if not pool:
                messagebox.showwarning(
                    "Randomize",
                    f"No {family} voices available. Unlock OneCore… if Linda/Mark/Richard "
                    "are installed but missing from the list.",
                )
                return
        else:
            pool = list(voices)
            label = "all"

        channels = list(self.voice_vars.keys())
        order = list(channels)
        random.shuffle(order)
        pool = list(pool)
        random.shuffle(pool)

        assigned: dict[str, str] = {}
        used: set[str] = set()
        female_pool = [v for v in pool if atc_phrase.voice_gender(v) == "female"]
        for ch in order:
            ch_pool = female_pool if atc_phrase.channel_requires_female(ch) and female_pool else pool
            unused = [v for v in ch_pool if v not in used]
            if unused:
                pick = unused[0]
                assigned[ch] = pick
                used.add(pick)
            else:
                assigned[ch] = random.choice(ch_pool)

        for ch, voice in assigned.items():
            self._set_agency_voice(ch, voice)

        unique_n = len(set(assigned.values()))
        if unique_n < len(channels):
            self.var_voice_status.set(
                f"Randomized ({label}) · {unique_n}/{len(channels)} unique "
                f"(pool {len(pool)}) · Save setup to keep"
            )
        else:
            self.var_voice_status.set(
                f"Randomized ({label}) · {unique_n} unique · Save setup to keep"
            )

    def _on_tts_provider_change(self) -> None:
        provider = atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()})
        voices = (
            atc_phrase.google_voice_choices()
            if provider == "google"
            else self._list_windows_voices()
        )
        self.voice_labels = voices
        for ch, var in self.voice_vars.items():
            current = var.get().strip()
            if provider == "google":
                if not atc_phrase.is_google_voice_name(current):
                    self._set_agency_voice(
                        ch,
                        atc_phrase.DEFAULT_GOOGLE_VOICES.get(ch)
                        or atc_phrase.DEFAULT_GOOGLE_VOICES["default"],
                    )
                else:
                    self._set_agency_voice(ch, current)
            else:
                if atc_phrase.is_google_voice_name(current) or not current:
                    fallback = voices[0] if voices else "Microsoft Zira Desktop"
                    self._set_agency_voice(ch, fallback)
                else:
                    self._set_agency_voice(ch, current)
        # Apply provider default talk speed (Windows 4 / Google 7) and keep it saved.
        speed = atc_phrase.tts_speed_for_provider(self.config_data, provider=provider)
        # If this provider has never been tuned, seed the provider default.
        by = self.config_data.get("tts_speed_by_provider")
        if not isinstance(by, dict) or provider not in by:
            speed = atc_phrase.default_tts_speed(provider)
        atc_phrase.set_tts_speed_for_provider(self.config_data, speed, provider=provider)
        if hasattr(self, "var_speed"):
            self.var_speed.set(float(speed))
        self._rebuild_randomize_menu()
        self._update_tts_status()

    def _load_setup_fields(self) -> None:
        c = self.config_data
        if hasattr(self, "var_atc_role"):
            self.var_atc_role.set(atc_net.role_of(c))
            self.var_atc_host.set(str(c.get("atc_host") or "127.0.0.1"))
            self.var_atc_port.set(str(c.get("atc_port") or atc_net.DEFAULT_ATC_PORT))
            self.var_atc_token.set(str(c.get("atc_token") or ""))
        self.var_user.set(c.get("opus_user_name", ""))
        self.var_backend.set(c.get("opus_backend_url", ""))
        self.var_callsign_override.set(c.get("callsign_override", "") or "")
        self._update_opus_flight_label()
        self._refresh_opus_identity_bar()
        self.var_runway_override.set(c.get("runway_override", "") or "")
        self.var_volume.set(float(c.get("tts_volume", 0.8)))
        provider = atc_phrase.tts_provider(c)
        self.var_tts_provider.set(provider)
        speed = atc_phrase.tts_speed_for_provider(c, provider=provider)
        atc_phrase.set_tts_speed_for_provider(c, speed, provider=provider)
        self.var_speed.set(float(speed))
        self.var_google_credentials.set(c.get("google_credentials", "") or "")
        voices_cfg = c.get("tts_voices") or {}
        if provider == "google":
            default_voice = c.get("tts_voice") or atc_phrase.DEFAULT_GOOGLE_VOICES["default"]
        else:
            default_voice = c.get("tts_voice", "Microsoft Zira Desktop")
        if not isinstance(voices_cfg, dict):
            voices_cfg = {}
        for ch in self.voice_vars:
            self._set_agency_voice(ch, str(voices_cfg.get(ch) or default_voice))
        self._setup_fields_loaded = True
        self._on_tts_provider_change()
        ap = self._airport()
        self.var_ap_name.set(ap.get("name", ""))
        self.var_icao.set(ap.get("icao", ""))
        self.var_host.set(ap.get("srs_host", ""))
        self.var_port.set(str(ap.get("srs_port", 5002)))
        self.var_coalition.set(str(ap.get("coalition", 2)))
        self.var_runways.set(",".join(ap.get("runways") or []))
        self.var_expect_minutes.set(str(ap.get("expect_minutes") or 10))
        self.var_known_sids.set(",".join(ap.get("known_sids") or []))
        for ch in atc_phrase.CHANNELS:
            block = ap.get(ch) or {}
            self.freq_vars[ch].set(str(block.get("freq_mhz", "")))
        if hasattr(self, "var_hotkey_next"):
            self.var_hotkey_next.set(hotkeys.hotkey_from_config(c, "next"))
            self.var_hotkey_back.set(hotkeys.hotkey_from_config(c, "back"))
            self.var_hotkey_seek_next.set(hotkeys.hotkey_from_config(c, "seek_next"))
            self.var_hotkey_seek_prev.set(hotkeys.hotkey_from_config(c, "seek_prev"))
        if hasattr(self, "var_freq_gate_enabled"):
            self.var_freq_gate_enabled.set(bool(c.get("freq_gate_enabled", True)))
            self.var_freq_gate_eam.set(bool(c.get("freq_gate_eam_enabled")))
            srs_radio.apply_config(c)
            if bool(c.get("freq_gate_eam_enabled")):
                self._rebuild_eam_freq_rows(self._eam_seed_freqs())
            self._sync_fly_eam_ui()
        if hasattr(self, "var_picture_max_range"):
            try:
                pic_nm = float(
                    c.get("picture_max_range_nm") or voice_actions.PICTURE_MAX_RANGE_NM
                )
            except (TypeError, ValueError):
                pic_nm = float(voice_actions.PICTURE_MAX_RANGE_NM)
            self.var_picture_max_range.set(f"{pic_nm:g}")
        if hasattr(self, "var_tanker_chat_llm"):
            mode = str(c.get("tanker_chat_llm") or "off").strip().lower() or "off"
            if mode not in {"off", "auto", "gemini", "openai", "ollama"}:
                mode = "off"
            self.var_tanker_chat_llm.set(mode)
            self.var_tanker_chat_llm_key.set(str(c.get("tanker_chat_llm_key") or ""))
        if hasattr(self, "var_auto_clearance"):
            self.var_auto_clearance.set(bool(c.get("auto_clearance_enabled")))
            self.var_auto_takeoff.set(bool(c.get("auto_takeoff_clearance", True)))
            self.var_auto_monitor.set(bool(c.get("auto_monitor_tower", True)))
            self.var_auto_full_flight.set(
                bool(c.get("auto_clearance_require_full_flight", True))
            )
        for which, var in (
            ("next", "var_joy_next"),
            ("back", "var_joy_back"),
            ("seek_next", "var_joy_seek_next"),
            ("seek_prev", "var_joy_seek_prev"),
        ):
            binding = joystick.binding_from_config(c, which)
            self._joy_bindings[which] = binding
            if hasattr(self, var):
                getattr(self, var).set(joystick.describe_binding(binding))
        if hasattr(self, "btn_admin") and hotkeys.is_elevated():
            self.btn_admin.state(["disabled"])
            self.btn_admin.configure(text="Running as administrator")
        if hasattr(self, "var_voice_enabled"):
            self.var_voice_enabled.set(bool(c.get("voice_enabled")))
            self.var_voice_model.set(str(c.get("voice_model") or voice_engine.DEFAULT_MODEL))
            self.var_voice_confidence.set(
                float(c.get("voice_min_confidence") or voice_engine.DEFAULT_MIN_CONFIDENCE)
            )
            self._sync_voice_confidence_label()
            self.var_voice_require_address.set(bool(c.get("voice_require_address", True)))
            if hasattr(self, "var_voice_nlu"):
                self.var_voice_nlu.set(bool(c.get("voice_nlu_enabled", True)))
            index = c.get("voice_mic_device")
            index = int(index) if isinstance(index, int) else -1
            for device in getattr(self, "_mic_devices", []):
                if int(device["index"]) == index:
                    self.var_voice_mic.set(str(device["name"]))
                    break
            ptt = [joystick.normalize_binding(b) for b in (c.get("voice_ptt") or [])]
            ptt = [b for b in ptt if b]
            self.var_voice_ptt.set(
                ", ".join(joystick.describe_binding(b) for b in ptt) if ptt else "(auto: SRS PTT)"
            )
            ptt_key = hotkeys.normalize_hotkey(c.get("voice_ptt_key"))
            self.var_voice_ptt_key.set(ptt_key or "(none)")
        self._refresh_tts_usage()
        self._update_fly_hotkey_hint()

    def save_setup(self) -> None:
        role = atc_net.role_of(self.config_data)
        if hasattr(self, "var_atc_role"):
            role = str(self.var_atc_role.get() or "solo").strip().lower()
            if role not in atc_net.ROLES:
                role = "solo"
            self.config_data["atc_role"] = role
            self.config_data["atc_host"] = self.var_atc_host.get().strip() or "127.0.0.1"
            try:
                self.config_data["atc_port"] = int(self.var_atc_port.get() or atc_net.DEFAULT_ATC_PORT)
            except ValueError:
                self.config_data["atc_port"] = atc_net.DEFAULT_ATC_PORT
            self.config_data["atc_token"] = self.var_atc_token.get().strip()
            if role == "host" and not self.config_data["atc_token"]:
                self.config_data["atc_token"] = self._new_atc_token()
                self.var_atc_token.set(self.config_data["atc_token"])
            if role == "client" and not self.config_data["atc_token"]:
                messagebox.showerror(
                    "Squadron",
                    "Client needs the host's shared token. Copy it from the Host PC.",
                )
                return
        self.config_data["opus_user_name"] = self.var_user.get().strip()
        self.config_data["opus_backend_url"] = self.var_backend.get().strip()
        self.config_data["callsign_override"] = self.var_callsign_override.get().strip()
        self.config_data["runway_override"] = self.var_runway_override.get().strip()
        provider = atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()})
        self.config_data["tts_provider"] = provider
        creds_path = self.var_google_credentials.get().strip()
        if provider == "google" and creds_path and role != "client":
            try:
                pinned = atc_phrase.pin_google_credentials_to_secrets(creds_path)
                creds_path = str(pinned)
                self.var_google_credentials.set(creds_path)
            except (OSError, FileNotFoundError):
                pass
        if role != "client":
            self.config_data["google_credentials"] = creds_path
        if provider == "google" and role != "client":
            creds = atc_phrase.google_credentials_path(self.config_data)
            if creds is None or not creds.is_file():
                messagebox.showerror(
                    "Google TTS",
                    "tts_provider is Google but the credentials JSON path is missing or invalid.\n\n"
                    "Create a Google Cloud service account key and Browse to the .json file.\n"
                    "It is copied into atc\\secrets\\ and stays on this Host — never on client PCs.",
                )
                return
        voices: dict[str, str] = {}
        for ch, var in self.voice_vars.items():
            voices[ch] = var.get().strip()
        self.config_data["tts_voices"] = voices
        if provider == "google":
            default_voice = (
                voices.get("default")
                or next(iter(voices.values()), atc_phrase.DEFAULT_GOOGLE_VOICES["default"])
            )
        else:
            default_voice = voices.get("default") or next(
                iter(voices.values()), "Microsoft Zira Desktop"
            )
        self.config_data["tts_voice"] = default_voice
        self.config_data["tts_gender"] = atc_phrase.voice_gender(default_voice)
        self.config_data["tts_volume"] = round(float(self.var_volume.get()), 2)
        atc_phrase.set_tts_speed_for_provider(
            self.config_data, self.var_speed.get(), provider=provider
        )
        if hasattr(self, "var_hotkey_next"):
            self.config_data["hotkey_next"] = hotkeys.normalize_hotkey(
                self.var_hotkey_next.get(), default=hotkeys.DEFAULT_HOTKEY_NEXT
            )
            self.config_data["hotkey_back"] = hotkeys.normalize_hotkey(
                self.var_hotkey_back.get(), default=hotkeys.DEFAULT_HOTKEY_BACK
            )
            self.config_data["hotkey_seek_next"] = hotkeys.normalize_hotkey(
                self.var_hotkey_seek_next.get(), default=""
            )
            self.config_data["hotkey_seek_prev"] = hotkeys.normalize_hotkey(
                self.var_hotkey_seek_prev.get(), default=""
            )
            self.var_hotkey_next.set(str(self.config_data["hotkey_next"]))
            self.var_hotkey_back.set(str(self.config_data["hotkey_back"]))
            self.var_hotkey_seek_next.set(str(self.config_data["hotkey_seek_next"]))
            self.var_hotkey_seek_prev.set(str(self.config_data["hotkey_seek_prev"]))
        for which in ("next", "back", "seek_next", "seek_prev"):
            self.config_data[f"joy_{which}"] = self._joy_bindings.get(which)
        self._sync_eam_freqs_to_config()
        if hasattr(self, "var_picture_max_range"):
            try:
                pic_nm = float(self.var_picture_max_range.get().strip() or "0")
            except (TypeError, ValueError):
                pic_nm = float(voice_actions.PICTURE_MAX_RANGE_NM)
            if pic_nm <= 0:
                pic_nm = float(voice_actions.PICTURE_MAX_RANGE_NM)
            self.config_data["picture_max_range_nm"] = pic_nm
            self.var_picture_max_range.set(f"{pic_nm:g}")
        if hasattr(self, "var_tanker_chat_llm"):
            mode = str(self.var_tanker_chat_llm.get() or "off").strip().lower() or "off"
            if mode not in {"off", "auto", "gemini", "openai", "ollama"}:
                mode = "off"
            self.config_data["tanker_chat_llm"] = mode
            self.config_data["tanker_chat_llm_key"] = (
                self.var_tanker_chat_llm_key.get().strip()
            )
        if hasattr(self, "var_voice_enabled"):
            self.config_data["voice_enabled"] = bool(self.var_voice_enabled.get())
            self.config_data["voice_model"] = (
                self.var_voice_model.get().strip() or voice_engine.DEFAULT_MODEL
            )
            self.config_data["voice_mic_device"] = self._selected_mic_index()
            self.config_data["voice_min_confidence"] = self._sync_voice_confidence_label()
            self.config_data["voice_require_address"] = bool(
                self.var_voice_require_address.get()
            )
            if hasattr(self, "var_voice_nlu"):
                self.config_data["voice_nlu_enabled"] = bool(self.var_voice_nlu.get())
        save_json(CONFIG_PATH, self.config_data)
        self._apply_hotkeys()
        self._apply_voice()
        self._sync_atc_runtime()

        key = self.mission.get("airport") or "nellis"
        ap = self.airports.get(key, {})
        ap["name"] = self.var_ap_name.get().strip() or key.title()
        ap["icao"] = self.var_icao.get().strip().upper()
        ap["srs_host"] = self.var_host.get().strip()
        ap["srs_port"] = int(self.var_port.get() or 5002)
        ap["coalition"] = int(self.var_coalition.get() or 2)
        ap["runways"] = [x.strip() for x in self.var_runways.get().split(",") if x.strip()]
        try:
            ap["expect_minutes"] = int(re.sub(r"\D", "", self.var_expect_minutes.get()) or "10")
        except ValueError:
            ap["expect_minutes"] = 10
        ap.pop("initial_climb", None)
        ap.pop("expect_after", None)
        ap["known_sids"] = [x.strip().upper() for x in self.var_known_sids.get().split(",") if x.strip()]
        # Delivery is a separate freq at Nellis — speak "Delivery", not "Ground"
        ap["clearance_consolidated_with_ground"] = False
        for ch in atc_phrase.CHANNELS:
            if ch == "tanker":
                continue
            try:
                mhz = float(self.freq_vars[ch].get())
            except ValueError:
                continue
            prev = ap.get(ch) or {}
            block: dict[str, Any] = {
                "freq_mhz": mhz,
                "mod": prev.get("mod") or "AM",
                "source": prev.get("source") or "local",
            }
            # Keep Opus UHF Local N so clearance says "departure Local five"
            if prev.get("local_preset") is not None:
                try:
                    block["local_preset"] = int(prev["local_preset"])
                except (TypeError, ValueError):
                    pass
            ap[ch] = block
        self.airports[key] = ap
        save_json(AIRPORTS_PATH, self.airports)
        self.engine.config = self.config_data
        self.engine.airports = self.airports
        messagebox.showinfo("Setup", "Setup saved.")
        self._update_freq_hint()
        self._refresh_opus_identity_bar()

    def _ensure_identity_vars(self) -> None:
        """Shared Opus / CAOC identity vars (Plan, Fly, and Setup)."""
        if hasattr(self, "var_user"):
            return
        self.var_user = tk.StringVar()
        self.var_backend = tk.StringVar()
        self.var_callsign_override = tk.StringVar()
        self.var_opus_flight = tk.StringVar(value="(choose an Opus flight)")
        self.var_callsign = tk.StringVar(value="(refresh to resolve)")
        self.var_identity_match = tk.StringVar(
            value="CAOC matches your Opus username + selected flight callsign"
        )
        self._opus_flight_rows: list[dict[str, Any]] = []
        self._identity_bars: dict[str, tk.Frame] = {}

    def _build_opus_identity_bar(
        self,
        parent: tk.Misc,
        *,
        key: str,
        prominent: bool = False,
    ) -> tk.Frame:
        """
        Title-row Opus chip: username + one flight summary. Click the summary
        (or the ▾) for Choose / Refresh / Clear. Setup still has the full editor.
        """
        _ = prominent  # kept for call-site compatibility
        self._ensure_identity_vars()
        bar = tk.Frame(parent, bg=C_BG)
        tk.Label(
            bar,
            text="CAOC",
            bg=C_BG,
            fg=C_AMBER,
            font=("Segoe UI Semibold", 9),
        ).pack(side=tk.LEFT)
        user_ent = ttk.Entry(bar, textvariable=self.var_user, width=10)
        user_ent.pack(side=tk.LEFT, padx=(6, 8))
        user_ent.bind("<Return>", self._commit_identity_user)
        user_ent.bind("<FocusOut>", self._commit_identity_user)
        user_ent.bind("<KeyRelease>", self._schedule_identity_user_commit)

        chip = tk.Frame(bar, bg=C_BG, cursor="hand2")
        chip.pack(side=tk.LEFT, fill=tk.X, expand=True)
        flight_lbl = tk.Label(
            chip,
            textvariable=self.var_opus_flight,
            bg=C_BG,
            fg=C_GREEN,
            font=("Segoe UI Semibold", 9),
            anchor="w",
            cursor="hand2",
        )
        flight_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        caret = tk.Label(
            chip,
            text="▾",
            bg=C_BG,
            fg=C_MUTED,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        caret.pack(side=tk.LEFT, padx=(4, 0))
        for w in (chip, flight_lbl, caret):
            w.bind("<Button-1>", self._opus_header_menu)
        flight_lbl.bind("<Enter>", lambda _e: flight_lbl.configure(fg="#8fd4a8"))
        flight_lbl.bind("<Leave>", lambda _e: flight_lbl.configure(fg=C_GREEN))

        self._identity_bars[key] = bar
        return bar

    def _opus_header_menu(self, event: tk.Event) -> str:
        """Popup: choose / refresh / clear the active Opus flight."""
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Choose flight…", command=self._choose_opus_flight)
        menu.add_command(label="Refresh from Opus", command=self._persist_identity)
        menu.add_command(label="Clear flight", command=self._clear_opus_flight)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _persist_identity(self, *, refresh_opus: bool = True) -> None:
        """
        Write CAOC username / Opus flight to disk and resolve the callsign.

        Header changes used to stay in memory until Setup → Save, so Play / voice
        / a new FlowEngine() still loaded the old flight from config.json.
        """
        if hasattr(self, "var_user"):
            self.config_data["opus_user_name"] = self.var_user.get().strip()
        if hasattr(self, "var_backend"):
            backend = self.var_backend.get().strip()
            if backend:
                self.config_data["opus_backend_url"] = backend
        if hasattr(self, "var_callsign_override"):
            self.config_data["callsign_override"] = self.var_callsign_override.get().strip()
        atc_phrase.invalidate_opus_cache()
        try:
            save_json(CONFIG_PATH, self.config_data)
        except OSError:
            pass
        if getattr(self, "engine", None) is not None:
            self.engine.config = self.config_data
        self._refresh_opus_identity_bar()
        if refresh_opus:
            self._refresh_callsign()

    def _commit_identity_user(self, _evt: object | None = None) -> None:
        """Push the visible CAOC username into config and re-resolve Opus."""
        pending = getattr(self, "_identity_user_after", None)
        if pending is not None:
            try:
                self.after_cancel(pending)
            except (tk.TclError, ValueError):
                pass
            self._identity_user_after = None
        if not hasattr(self, "var_user"):
            return
        user = self.var_user.get().strip()
        prev = str(self.config_data.get("opus_user_name") or "").strip()
        if user == prev:
            self._refresh_opus_identity_bar()
            return
        self._persist_identity(refresh_opus=True)

    def _commit_identity_override(self, _evt: object | None = None) -> None:
        """Persist a manual callsign override without waiting for Save setup."""
        if not hasattr(self, "var_callsign_override"):
            return
        value = self.var_callsign_override.get().strip()
        prev = str(self.config_data.get("callsign_override") or "").strip()
        if value == prev:
            return
        self._persist_identity(refresh_opus=True)

    def _schedule_identity_user_commit(self, _evt: object | None = None) -> None:
        """Debounce CAOC name typing so we save without waiting for Setup."""
        pending = getattr(self, "_identity_user_after", None)
        if pending is not None:
            try:
                self.after_cancel(pending)
            except (tk.TclError, ValueError):
                pass
        self._identity_user_after = self.after(700, self._commit_identity_user)

    def _refresh_opus_identity_bar(self) -> None:
        """Refresh CAOC match hint from current username + flight selection."""
        self._ensure_identity_vars()
        user = self.var_user.get().strip() or str(
            self.config_data.get("opus_user_name") or ""
        ).strip()
        if not self.var_user.get().strip() and user:
            self.var_user.set(user)
        fid = atc_phrase.configured_opus_flight_id(self.config_data)
        # Short status only — flight details already sit in var_opus_flight.
        if not user and fid is None:
            self.var_identity_match.set("set user + flight")
        elif not user:
            self.var_identity_match.set("no user · callsign-only match")
        elif fid is None:
            self.var_identity_match.set("choose a flight")
        else:
            self.var_identity_match.set("")

    def _opus_flight_summary_bits(self, row: dict[str, Any]) -> list[str]:
        """Callsign · filed route · seat — crew/date stay on the picker."""
        fid = int(row.get("id") or 0)
        cs = str(row.get("callsign") or (f"#{fid}" if fid else "flight"))
        bits = [cs]
        route = str(row.get("fp_route_string") or "").strip()
        if route:
            bits.append(route)
        seat = atc_phrase.configured_opus_seat(self.config_data)
        if seat:
            bits.append(f"seat {seat}")
        return bits

    def _update_opus_flight_label(self, row: dict[str, Any] | None = None) -> None:
        """Refresh the header flight chip from config and optional picker row."""
        fid = atc_phrase.configured_opus_flight_id(self.config_data)
        if fid is None:
            self.var_opus_flight.set("choose Opus flight…")
            return
        if row and int(row.get("id") or 0) == fid:
            self.var_opus_flight.set(" · ".join(self._opus_flight_summary_bits(row)))
            return
        cached = str(self.config_data.get("opus_flight_label") or "").strip()
        self.var_opus_flight.set(self._compact_cached_flight_label(cached) or f"Flight #{fid}")

    @staticmethod
    def _compact_cached_flight_label(label: str) -> str:
        """Keep callsign + route; drop date / crew / filed-FP filler from older strings."""
        bits = [b.strip() for b in (label or "").split("·") if b.strip()]
        kept: list[str] = []
        for bit in bits:
            low = bit.lower()
            if low.startswith("filed fp") or low == "no flight plan":
                continue
            if low.startswith("crew "):
                continue
            if re.match(r"^\d{4}-\d{2}-\d{2}$", bit):
                continue
            if low.startswith("using seat"):
                bit = "seat " + bit[len("using seat") :].strip()
            kept.append(bit)
        return " · ".join(kept)

    def _clear_opus_flight(self) -> None:
        self.config_data.pop("opus_flight_id", None)
        self.config_data.pop("opus_seat", None)
        self.config_data.pop("opus_flight_label", None)
        self._update_opus_flight_label()
        self.var_callsign.set("(refresh to resolve)")
        self._persist_identity(refresh_opus=True)

    def _choose_opus_flight(
        self,
        *,
        parent: tk.Misc | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """Modal list of Opus flights — select one for callsign + flight plan."""
        owner = parent or self
        if hasattr(self, "var_user"):
            self.config_data["opus_user_name"] = self.var_user.get().strip()
        if hasattr(self, "var_backend"):
            self.config_data["opus_backend_url"] = self.var_backend.get().strip()
        if not (self.config_data.get("opus_backend_url") or "").strip():
            messagebox.showinfo("Opus flights", "Set the Opus backend URL first (Setup tab).", parent=owner)
            return

        dlg = tk.Toplevel(owner)
        dlg.title("Choose Opus flight")
        dlg.configure(bg=C_BG)
        dlg.transient(owner)
        dlg.grab_set()
        self._place_dialog(dlg, 980, 560, relative_to=owner)

        hdr = tk.Frame(dlg, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        hdr.pack(fill=tk.X, padx=12, pady=(12, 6))
        tk.Label(
            hdr,
            text="Flights from Opus — select a flight, then pick a seat / pilot from the crew list",
            bg=C_PANEL,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(side=tk.LEFT, padx=10, pady=8)
        status = tk.StringVar(value="Loading…")
        tk.Label(hdr, textvariable=status, bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 9)).pack(
            side=tk.RIGHT, padx=10
        )

        body = tk.Frame(dlg, bg=C_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        # Left: flight list
        left = tk.Frame(body, bg=C_BG)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cols = ("callsign", "date", "vul", "mission", "ac", "qty", "fp")
        tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse", height=12)
        headings = {
            "callsign": ("Callsign", 110),
            "date": ("Date", 90),
            "vul": ("VUL", 90),
            "mission": ("Mission", 110),
            "ac": ("A/C", 70),
            "qty": ("Qty", 40),
            "fp": ("Flight plan", 280),
        }
        for key, (title, width) in headings.items():
            tree.heading(key, text=title)
            tree.column(key, width=width, stretch=(key == "fp"))
        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Right: full crew / seat list for selected flight
        right = tk.Frame(body, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1, width=280)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))
        right.pack_propagate(False)
        tk.Label(
            right,
            text="Crew / seats",
            bg=C_PANEL,
            fg=C_TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", padx=10, pady=(10, 4))
        crew_hint = tk.StringVar(value="Select a flight to see all pilots.")
        tk.Label(
            right,
            textvariable=crew_hint,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=250,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 6))
        crew_list = tk.Listbox(
            right,
            bg=C_CARD,
            fg=C_TEXT,
            selectbackground=C_ACCENT,
            selectforeground=C_BG,
            activestyle="none",
            font=("Segoe UI", 10),
            highlightthickness=0,
            borderwidth=0,
        )
        crew_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        foot = tk.Frame(dlg, bg=C_BG)
        foot.pack(fill=tk.X, padx=12, pady=(0, 12))

        rows_by_iid: dict[str, dict[str, Any]] = {}
        crew_slots_by_index: list[dict[str, Any]] = []

        def fill_crew(row: dict[str, Any] | None) -> None:
            nonlocal crew_slots_by_index
            crew_list.delete(0, tk.END)
            crew_slots_by_index = []
            if not row:
                crew_hint.set("Select a flight to see all pilots.")
                return
            slots = row.get("crew_slots")
            if not slots:
                slots = atc_phrase.opus_crew_slots(row.get("signups") or [], qty=row.get("qty"))
            crew_slots_by_index = list(slots)
            user = self.var_user.get().strip().casefold()
            select_idx = 0
            filled = 0
            for i, slot in enumerate(crew_slots_by_index):
                label = atc_phrase.format_crew_slot(slot)
                crew_list.insert(tk.END, label)
                if slot.get("user_name"):
                    filled += 1
                if user and str(slot.get("user_name") or "").casefold() == user:
                    select_idx = i
            # Prefer previously chosen seat for this flight
            prev_seat = atc_phrase.configured_opus_seat(self.config_data)
            prev_fid = atc_phrase.configured_opus_flight_id(self.config_data)
            if prev_fid == int(row.get("id") or 0) and prev_seat:
                for i, slot in enumerate(crew_slots_by_index):
                    if int(slot.get("seat") or 0) == prev_seat:
                        select_idx = i
                        break
            if crew_slots_by_index:
                crew_list.selection_set(select_idx)
                crew_list.activate(select_idx)
                crew_list.see(select_idx)
            qty = row.get("qty")
            crew_hint.set(
                f"{filled} signed up"
                + (f" · qty {qty}" if qty is not None else "")
                + " — click a seat, then Use selected flight"
            )

        def on_select(_evt: object | None = None) -> None:
            sel = tree.selection()
            if not sel:
                return
            fill_crew(rows_by_iid.get(sel[0]))

        tree.bind("<<TreeviewSelect>>", on_select)

        def apply_rows(rows: list[dict[str, Any]]) -> None:
            tree.delete(*tree.get_children())
            rows_by_iid.clear()
            self._opus_flight_rows = rows
            selected_id = atc_phrase.configured_opus_flight_id(self.config_data)
            focus_iid = None
            for row in rows:
                fid = int(row["id"])
                vul = ""
                if row.get("vul_start") or row.get("vul_end"):
                    vul = f"{row.get('vul_start') or '?'}–{row.get('vul_end') or '?'}"
                mission = " ".join(
                    x for x in [str(row.get("mission_number") or ""), str(row.get("mission") or "")] if x
                ).strip()
                fp = str(row.get("fp_route_string") or ("(no FP)" if not row.get("has_filed_plan") else ""))
                iid = str(fid)
                tree.insert(
                    "",
                    tk.END,
                    iid=iid,
                    values=(
                        row.get("callsign") or f"#{fid}",
                        row.get("event_date") or "",
                        vul,
                        mission,
                        row.get("aircraft") or "",
                        row.get("qty") if row.get("qty") is not None else "",
                        fp,
                    ),
                )
                rows_by_iid[iid] = row
                if selected_id == fid:
                    focus_iid = iid
            status.set(f"{len(rows)} flight(s)")
            if focus_iid:
                tree.selection_set(focus_iid)
                tree.focus(focus_iid)
                tree.see(focus_iid)
                fill_crew(rows_by_iid.get(focus_iid))
            elif rows:
                first = str(rows[0]["id"])
                tree.selection_set(first)
                tree.focus(first)
                fill_crew(rows_by_iid.get(first))

        def load() -> None:
            status.set("Loading…")

            def work() -> None:
                try:
                    rows = atc_phrase.list_opus_flights(self.config_data, include_detail=True)
                    self.after(0, lambda: apply_rows(rows))
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    self.after(0, lambda e=err: status.set(f"Failed: {e}"))
                    self.after(
                        0,
                        lambda e=err: messagebox.showerror("Opus flights", e, parent=dlg),
                    )

            threading.Thread(target=work, daemon=True).start()

        def use_selected() -> None:
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Opus flights", "Select a flight first.", parent=dlg)
                return
            row = rows_by_iid.get(sel[0])
            if not row:
                return
            crew_sel = crew_list.curselection()
            seat_n = 1
            if crew_sel and crew_slots_by_index:
                slot = crew_slots_by_index[int(crew_sel[0])]
                try:
                    seat_n = int(slot.get("seat") or 1)
                except (TypeError, ValueError):
                    seat_n = 1
            self.config_data["opus_flight_id"] = int(row["id"])
            self.config_data["opus_seat"] = seat_n
            self.config_data["opus_flight_label"] = " · ".join(
                self._opus_flight_summary_bits(row)
            )
            self._update_opus_flight_label(row)
            dlg.destroy()
            self._persist_identity(refresh_opus=True)
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    pass
            # Regenerate live template previews with the new FP / altitude / squawk
            elif self.selected_index is not None:
                step = self._steps()[self.selected_index]
                if not step.get("text") and (step.get("mode") or "tts") != "file":
                    self.after(80, lambda: self.preview_step(apply=False))

        ttk.Button(foot, text="Refresh list", command=load).pack(side=tk.LEFT)
        ttk.Button(foot, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(foot, text="Use selected flight", command=use_selected).pack(side=tk.RIGHT, padx=(0, 8))
        tree.bind("<Double-1>", lambda _e: use_selected())

        load()

    def _sync_opus_presets_into_airport(self) -> dict[str, int]:
        """Pull Opus Local N presets into the active airport (departure Local 5, etc.)."""
        key = self.mission.get("airport") or "nellis"
        ap = self.airports.get(key) or {}
        presets = atc_phrase.sync_opus_local_presets(
            self.config_data, ap, icao=str(ap.get("icao") or self.var_icao.get() if hasattr(self, "var_icao") else "")
        )
        self.airports[key] = ap
        self.engine.airports = self.airports
        save_json(AIRPORTS_PATH, self.airports)
        return presets

    def _refresh_callsign(self) -> None:
        self.config_data["opus_user_name"] = self.var_user.get().strip()
        self.config_data["opus_backend_url"] = self.var_backend.get().strip()
        self.config_data["callsign_override"] = self.var_callsign_override.get().strip()
        atc_phrase.invalidate_opus_cache()

        def work() -> None:
            backend = (self.config_data.get("opus_backend_url") or "").strip()
            selected = atc_phrase.configured_opus_flight_id(self.config_data)
            opus = atc_phrase.resolve_active_opus_flight(self.config_data)
            dep_local = None
            if backend:
                try:
                    presets = self._sync_opus_presets_into_airport()
                    dep_local = presets.get("departure")
                except Exception as exc:  # noqa: BLE001
                    print(f"WARNING: Opus preset sync failed ({exc})", file=sys.stderr)
            if not opus:
                def _missing() -> None:
                    self.var_callsign.set(
                        "(not found — choose an Opus flight or set manual callsign)"
                    )
                    self._refresh_opus_identity_bar()

                self.after(0, _missing)
                return
            if not backend or (not selected and not (self.config_data.get("opus_user_name") or "").strip()):
                mode = "manual" if atc_phrase.callsign_override(self.config_data) else "offline"
                label = f"{opus.radio_callsign} · {mode}"

                def _manual() -> None:
                    self.var_callsign.set(label)
                    self._refresh_opus_identity_bar()

                self.after(0, _manual)
                return
            ov = " · manual" if atc_phrase.callsign_override(self.config_data) else ""
            local_bit = f" · dep Local {dep_local}" if dep_local else ""
            # Radio callsign only — route/crew already live on the header chip.
            label = f"{opus.radio_callsign}{ov} · seat {opus.seat}{local_bit}"

            def _ok() -> None:
                self.var_callsign.set(label)
                header = [opus.radio_callsign]
                if opus.fp_route_string:
                    header.append(opus.fp_route_string)
                if opus.seat:
                    header.append(f"seat {opus.seat}")
                self.config_data["opus_flight_label"] = " · ".join(header)
                self._update_opus_flight_label()
                self._refresh_opus_identity_bar()

            self.after(0, _ok)

        threading.Thread(target=work, daemon=True).start()

    def _airport_from_form(self) -> dict[str, Any]:
        """Snapshot of airport fields currently shown in Setup (unsaved OK)."""
        key = self.mission.get("airport") or "nellis"
        ap = dict(self.airports.get(key, {}))
        ap["name"] = self.var_ap_name.get().strip() or key.title()
        ap["icao"] = self.var_icao.get().strip().upper()
        for ch in atc_phrase.CHANNELS:
            if ch == "tanker":
                continue
            try:
                mhz = float(self.freq_vars[ch].get())
            except ValueError:
                continue
            prev = ap.get(ch) or {}
            ap[ch] = {"freq_mhz": mhz, "mod": prev.get("mod") or "AM"}
        return ap

    def _pull_opus_freqs(self) -> None:
        self.config_data["opus_user_name"] = self.var_user.get().strip()
        self.config_data["opus_backend_url"] = self.var_backend.get().strip()
        icao = self.var_icao.get().strip().upper()

        def work() -> None:
            try:
                result = atc_phrase.fetch_opus_theater_freqs(
                    self.config_data, icao=icao, band="uhf"
                )
                freqs = result.get("freqs") or {}

                def apply() -> None:
                    for ch, mhz in freqs.items():
                        if ch in self.freq_vars:
                            self.freq_vars[ch].set(str(mhz))
                    presets = result.get("presets") or {}
                    lines = [
                        f"{m['channel']}: Local {m.get('preset')} · {m['name']} = {m['freq_mhz']}"
                        for m in result.get("matched") or []
                    ]
                    self.var_freq_status.set(
                        f"Pulled Opus theater {result.get('theater_id')} UHF defaults "
                        f"({len(freqs)} channels). Save setup to keep.\n" + "\n".join(lines)
                    )
                    key = self.mission.get("airport") or "nellis"
                    ap = self.airports.get(key, {})
                    atc_phrase.apply_opus_freqs_to_airport(ap, freqs, presets=presets)
                    self.airports[key] = ap
                    self.engine.airports = self.airports
                    save_json(AIRPORTS_PATH, self.airports)
                    dep = presets.get("departure")
                    msg = (
                        f"Loaded {len(freqs)} UHF presets from Opus theater "
                        f"{result.get('theater_id')}."
                    )
                    if dep is not None:
                        msg += f"\n\nDeparture will speak as Local {dep}."
                    messagebox.showinfo("Opus freqs", msg)

                self.after(0, apply)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: messagebox.showerror("Opus freqs", e))

        threading.Thread(target=work, daemon=True).start()

    def _compare_opus_freqs(self) -> None:
        self.config_data["opus_user_name"] = self.var_user.get().strip()
        self.config_data["opus_backend_url"] = self.var_backend.get().strip()
        icao = self.var_icao.get().strip().upper()
        local = self._airport_from_form()

        def work() -> None:
            try:
                result = atc_phrase.fetch_opus_theater_freqs(
                    self.config_data, icao=icao, band="uhf"
                )
                rows = atc_phrase.compare_airport_freqs(local, result.get("freqs") or {})
                mismatches = [r for r in rows if not r["match"]]
                if not mismatches:
                    msg = f"All {len(rows)} channels match Opus theater {result.get('theater_id')} UHF."
                else:
                    bits = [
                        f"{r['channel']}: local {r['local']} vs Opus {r['opus']}"
                        for r in mismatches
                    ]
                    msg = f"{len(mismatches)} mismatch(es):\n" + "\n".join(bits)
                self.after(0, lambda: self.var_freq_status.set(msg))
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.after(0, lambda e=err: messagebox.showerror("Compare freqs", e))

        threading.Thread(target=work, daemon=True).start()

    def _regen_pdf(self) -> None:
        self.save_mission()
        try:
            path = kneeboard_pdf.generate()
            messagebox.showinfo("Kneeboard PDF", f"Created:\n{path}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("PDF", str(exc))


def main() -> int:
    try:
        app = MissionPlanner()
    except Exception as exc:  # noqa: BLE001
        # start "" hides the console — surface the crash so it is not a silent blink.
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Mission Flow Planner", f"Could not start:\n\n{exc}")
            root.destroy()
        except Exception:
            print(f"Could not start: {exc}", file=sys.stderr)
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
