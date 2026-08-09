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
import subprocess
import sys
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
import flow_engine  # noqa: E402
import hotkeys  # noqa: E402
import joystick  # noqa: E402
import kneeboard_pdf  # noqa: E402
import mic_capture  # noqa: E402
import voice_engine  # noqa: E402
import voice_intent  # noqa: E402

CONFIG_PATH = HERE / "config.json"
AIRPORTS_PATH = HERE / "airports.json"

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
        for note in atc_phrase.migrate_retired_google_voices(self.config_data):
            print(note, file=sys.stderr)
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
        self._joy_bindings: dict[str, dict[str, Any] | None] = {"next": None, "back": None}
        self._trigger_lock = threading.Lock()
        self._trigger_last: dict[str, float] = {}
        self._voice: voice_engine.VoiceController | None = None

        self._style()
        self._build()
        self._load_setup_fields()
        self.refresh_timeline()
        self._apply_hotkeys()
        self._apply_voice()
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
            font=("Segoe UI Semibold", 15),
            padding=(18, 12),
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
        parts = [f"Hotkeys  Next {nxt}  ·  Back {bak}"]
        joy_next = self._joy_bindings.get("next")
        joy_back = self._joy_bindings.get("back")
        if joy_next or joy_back:
            parts.append(
                f"HOTAS  Next {joystick.describe_binding(joy_next)}"
                f"  ·  Back {joystick.describe_binding(joy_back)}"
            )
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
        self._ui_call(lambda: self._fly(action))

    def _clear_tk_hotkeys(self) -> None:
        for seq in getattr(self, "_tk_hotkey_binds", []) or []:
            try:
                self.unbind_all(seq)
            except tk.TclError:
                pass
        self._tk_hotkey_binds = []

    def _apply_hotkeys(self) -> None:
        """Register global (Windows) + in-app binds for Next / Back."""
        if hasattr(self, "var_hotkey_next"):
            nxt = hotkeys.normalize_hotkey(
                self.var_hotkey_next.get(), default=hotkeys.DEFAULT_HOTKEY_NEXT
            )
            bak = hotkeys.normalize_hotkey(
                self.var_hotkey_back.get(), default=hotkeys.DEFAULT_HOTKEY_BACK
            )
            self.var_hotkey_next.set(nxt)
            self.var_hotkey_back.set(bak)
        else:
            nxt = hotkeys.hotkey_from_config(self.config_data, "next")
            bak = hotkeys.hotkey_from_config(self.config_data, "back")
        self.config_data["hotkey_next"] = nxt
        self.config_data["hotkey_back"] = bak

        self._clear_tk_hotkeys()

        def on_next() -> None:
            self._trigger("next")

        def on_back() -> None:
            self._trigger("back")

        # In-app binds (when this window has focus)
        for raw, cb in ((nxt, on_next), (bak, on_back)):
            parsed = hotkeys.parse_hotkey(raw)
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
            next_hotkey=nxt,
            back_hotkey=bak,
            on_next=on_next,
            on_back=on_back,
        )
        # Polled in parallel: RegisterHotKey dies in-game, this does not.
        # _trigger() collapses the two so a press still counts once.
        self._keys.clear_bindings()
        for key, combo, cb, label in (
            ("next", nxt, on_next, "Next"),
            ("back", bak, on_back, "Back"),
        ):
            def fire(fn: Callable[[], None] = cb, name: str = label) -> None:
                self._on_trigger_received(f"Key {name}")
                fn()

            warning = self._keys.set_binding(key, combo, on_press=fire)
            if warning:
                warnings.append(warning)
        self._keys.start()

        registered = self._hotkey_listener.registered
        status = f"Keyboard: {', '.join(registered) if registered else 'none registered'}"
        warnings.extend(self._apply_joystick(on_next, on_back))
        joy = [
            f"{label} {joystick.describe_binding(self._joy_bindings[key])}"
            for key, label in (("next", "Next"), ("back", "Back"))
            if self._joy_bindings.get(key)
        ]
        status += f"   ·   HOTAS: {', '.join(joy) if joy else 'none bound'}"
        if warnings:
            status += "\n" + "\n".join(f"! {w}" for w in warnings)
        if hasattr(self, "var_hotkey_status"):
            self.var_hotkey_status.set(status)
        self._update_fly_hotkey_hint()

    def _apply_joystick(
        self, on_next: Callable[[], None], on_back: Callable[[], None]
    ) -> list[str]:
        """Bind HOTAS buttons. Unlike hotkeys these survive DCS having focus."""
        warnings: list[str] = []
        self._joystick.clear_bindings()
        if not self._joystick.supported:
            return warnings
        for key, cb, label in (("next", on_next, "Next"), ("back", on_back, "Back")):
            binding = self._joy_bindings.get(key)
            var = getattr(self, f"var_joy_{key}", None)
            if var is not None:
                var.set(joystick.describe_binding(binding))
            if not binding:
                continue

            def fire(fn: Callable[[], None] = cb, name: str = label) -> None:
                self._on_trigger_received(f"HOTAS {name}")
                fn()

            warning = self._joystick.set_binding(key, binding, on_press=fire)
            if warning:
                warnings.append(f"{label}: {warning}")
        if any(self._joy_bindings.values()):
            self._joystick.start()
        else:
            self._joystick.stop()
        return warnings

    def _learn_joy_button(self, which: str) -> None:
        """Capture the next HOTAS press and bind it to Next or Back."""
        var = self.var_joy_next if which == "next" else self.var_joy_back
        previous = var.get()
        var.set("Press a HOTAS button…")

        def done(binding: dict[str, Any]) -> None:
            def apply() -> None:
                self._joy_bindings[which] = joystick.normalize_binding(binding)
                var.set(joystick.describe_binding(self._joy_bindings[which]))
                self._apply_hotkeys()

            self.after(0, apply)

        def cancel() -> None:
            if var.get().startswith("Press a HOTAS"):
                self._joystick.learn_next_press(None)
                var.set(previous)

        self._joystick.learn_next_press(done)
        self.after(10000, cancel)

    def _clear_joy_button(self, which: str) -> None:
        self._joy_bindings[which] = None
        (self.var_joy_next if which == "next" else self.var_joy_back).set("(none)")
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
        try:
            step = self.engine.current_step()
            if step:
                context["channel"] = str(step.get("channel") or step.get("phase") or "")
                context["phase"] = str(step.get("phase") or step.get("channel") or "")
                context["expected"] = str(step.get("template") or "")
        except Exception:  # noqa: BLE001
            pass
        context["callsign"] = atc_phrase.cached_radio_callsign(self.config_data)
        try:
            # Steps carry their own voice_phrases, so the grammar is per-mission.
            context["steps"] = list(self.engine.steps)
        except Exception:  # noqa: BLE001
            context["steps"] = []
        state = getattr(self.engine, "state", None) or {}
        context["awaiting_readback"] = bool(state.get("awaiting_readback"))
        items = state.get("readback_items") or []
        context["readback_items"] = items if isinstance(items, list) else []
        return context

    def _apply_voice(self) -> None:
        """(Re)start the recognizer from current config."""
        if hasattr(self, "var_voice_enabled"):
            self.config_data["voice_enabled"] = bool(self.var_voice_enabled.get())
            self.config_data["voice_model"] = self.var_voice_model.get().strip() or voice_engine.DEFAULT_MODEL
            self.config_data["voice_mic_device"] = self._selected_mic_index()
            self.config_data["voice_min_confidence"] = round(float(self.var_voice_confidence.get()), 2)
            self.config_data["voice_require_address"] = bool(self.var_voice_require_address.get())

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
            if hasattr(self, "var_voice_heard"):
                heard = text or "(nothing)"
                self.var_voice_heard.set(f"“{heard}”  →  {evaluation.describe()}")
            if text and hasattr(self, "fly_log"):
                self.fly_log.insert(tk.END, f"MIC  {text}  ->  {evaluation.describe()}\n")
                self.fly_log.see(tk.END)

        self._ui_call(show)

    def _on_voice_intent(self, match: voice_intent.Match) -> None:
        """Run the matched intent off the audio thread, then refresh Fly."""

        def work() -> None:
            try:
                engine = flow_engine.FlowEngine()
                result = voice_engine.execute_intent(match, engine)
            except Exception as exc:  # noqa: BLE001
                self._ui_call(lambda: self._voice_log(f"VOICE  {match.intent} failed: {exc}"))
                return

            def done() -> None:
                action = result.get("action")
                detail = result.get("detail")
                if action == "transmit":
                    self._voice_log(f"TX   {result.get('channel', '').upper()}  {result.get('text', '')}")
                elif isinstance(detail, dict):
                    self._voice_log(f"TX   {detail.get('label') or detail.get('step_id') or action}")
                else:
                    self._voice_log(f"VOICE  {action}: {detail}")
                # Adopt the engine the intent ran on, the way _fly does, so the
                # Fly card follows the cursor a voice-fired step just moved.
                self.engine = engine
                self._refresh_fly_status()

            self._ui_call(done)

        threading.Thread(target=work, daemon=True).start()

    def _voice_log(self, line: str) -> None:
        if hasattr(self, "fly_log"):
            self.fly_log.insert(tk.END, line.rstrip() + "\n")
            self.fly_log.see(tk.END)

    def _capture_key(self, prompt: str, apply: Callable[[str], None]) -> None:
        """Modal: press a key combo, hand it to `apply`."""
        dlg = tk.Toplevel(self)
        dlg.title("Capture key")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("420x160")
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
        """Press a key combo to set Next or Back."""
        label = "Advance (Next)" if which == "next" else "Previous (Back)"

        def apply(combo: str) -> None:
            if which == "next":
                self.var_hotkey_next.set(combo)
            else:
                self.var_hotkey_back.set(combo)
            self.after(250, self._apply_hotkeys)

        self._capture_key(f"Press a key combo for {label}", apply)

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
        top.pack(fill=tk.X, padx=16, pady=(14, 6))
        ttk.Label(top, text="Mission Flow Planner", style="Title.TLabel").pack(side=tk.LEFT)
        self.mission_name_var = tk.StringVar(value=self.mission.get("name") or "Untitled")
        name_entry = ttk.Entry(top, textvariable=self.mission_name_var, width=28)
        name_entry.pack(side=tk.LEFT, padx=16)
        ttk.Label(top, text="Plan the flight → Fly with Play and Advance", style="Muted.TLabel").pack(side=tk.LEFT)

        ttk.Button(top, text="Help", command=self._show_help_tab).pack(side=tk.RIGHT, padx=(8, 0))

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.tab_plan = ttk.Frame(self.nb)
        self.tab_fly = ttk.Frame(self.nb)
        self.tab_setup = ttk.Frame(self.nb)
        self.tab_help = ttk.Frame(self.nb)
        self.nb.add(self.tab_plan, text="  Plan Flight  ")
        self.nb.add(self.tab_fly, text="  Fly  ")
        self.nb.add(self.tab_setup, text="  Setup  ")
        self.nb.add(self.tab_help, text="  Help  ")

        self._build_plan()
        self._build_fly()
        self._build_setup()
        self._build_help()
        self.nb.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

    def _on_notebook_tab_changed(self, _evt: object | None = None) -> None:
        try:
            on_fly = self.nb.index(self.nb.select()) == self.nb.index(self.tab_fly)
        except tk.TclError:
            return
        if on_fly:
            self._refresh_fly_status()
            if hasattr(self, "_fly_wheel"):
                self._fly_canvas.bind_all("<MouseWheel>", self._fly_wheel)
            self.after_idle(self._fly_update_scrollregion)
        else:
            self._fly_maybe_unbind_wheel()

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

        # Wider editor; form scrolls; action buttons stay pinned at bottom
        right = tk.Frame(body, bg=C_PANEL, width=480, highlightbackground=C_BORDER, highlightthickness=1)
        right.pack(side=tk.RIGHT, fill=tk.BOTH)
        right.pack_propagate(False)

        self.var_locked = tk.BooleanVar(value=False)
        step_hdr = tk.Frame(right, bg=C_PANEL)
        step_hdr.pack(fill=tk.X, padx=12, pady=(10, 4))
        ttk.Label(step_hdr, text="Step settings", style="Header.TLabel").pack(side=tk.LEFT)
        self._chk_locked = ttk.Checkbutton(
            step_hdr,
            text="Lock",
            variable=self.var_locked,
            command=self._on_lock_toggle,
            style="Panel.TCheckbutton",
        )
        self._chk_locked.pack(side=tk.RIGHT)
        self._lock_widgets = [step_hdr, self._chk_locked]

        # Always-visible bottom: actions + large preview (pack bottom-first)
        footer = tk.Frame(right, bg=C_PANEL)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(4, 10))
        self._btn_apply_step = ttk.Button(
            footer, text="Apply changes", style="Accent.TButton", command=self.apply_step
        )
        self._btn_apply_step.pack(fill=tk.X, pady=(0, 6))
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
        preview_wrap.pack(side=tk.BOTTOM, fill=tk.BOTH, padx=10, pady=(0, 4))
        prev_hdr = tk.Frame(preview_wrap, bg=C_PANEL)
        prev_hdr.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(prev_hdr, text="Preview", style="Header.TLabel").pack(side=tk.LEFT)
        tk.Label(
            prev_hdr,
            text="Apply saves settings · Regenerate re-rolls wording · Custom locks text",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=(8, 0))
        self.preview_box = tk.Text(
            preview_wrap,
            height=9,
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
            "Use Regenerate text / Hear locally / TX → SRS below.",
        )

        # Scrollable form above preview
        scroll_host = tk.Frame(right, bg=C_PANEL)
        scroll_host.pack(fill=tk.BOTH, expand=True)
        self._plan_canvas = tk.Canvas(scroll_host, bg=C_PANEL, highlightthickness=0, bd=0)
        plan_vsb = ttk.Scrollbar(scroll_host, orient=tk.VERTICAL, command=self._plan_canvas.yview)
        self._plan_canvas.configure(yscrollcommand=plan_vsb.set)
        plan_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._plan_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        form = tk.Frame(self._plan_canvas, bg=C_PANEL)
        self._plan_form_window = self._plan_canvas.create_window((0, 0), window=form, anchor="nw")

        def _plan_form_cfg(_event: tk.Event | None = None) -> None:
            self._plan_canvas.configure(scrollregion=self._plan_canvas.bbox("all"))

        def _plan_canvas_cfg(event: tk.Event) -> None:
            self._plan_canvas.itemconfigure(self._plan_form_window, width=event.width)

        form.bind("<Configure>", _plan_form_cfg)
        self._plan_canvas.bind("<Configure>", _plan_canvas_cfg)

        def _plan_wheel(event: tk.Event) -> str | None:
            if self.nb.index(self.nb.select()) != self.nb.index(self.tab_plan):
                return None
            delta = int(-1 * (event.delta / 120)) if event.delta else 0
            if delta:
                self._plan_canvas.yview_scroll(delta, "units")
            return "break"

        self._plan_canvas.bind("<Enter>", lambda _e: self._plan_canvas.bind_all("<MouseWheel>", _plan_wheel))
        self._plan_canvas.bind("<Leave>", lambda _e: self._plan_canvas.unbind_all("<MouseWheel>"))

        self.var_label = tk.StringVar()
        self.var_channel = tk.StringVar()
        self.var_mode = tk.StringVar(value="tts")
        self.var_template = tk.StringVar()
        self.var_file = tk.StringVar()
        self.var_enabled = tk.BooleanVar(value=True)
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
        self._step_form = form

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
        row(2, "Freq", self.freq_fr)

        # Voice: display on first line, buttons on second so nothing clips horizontally
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
        row(3, "Voice", voice_fr)

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
        row(4, "Speed", speed_fr)

        self._row_runway_lbl = tk.Label(form, text="Runway", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9))
        rwy_fr = tk.Frame(form, bg=C_PANEL)
        self.cmb_step_runway = ttk.Combobox(rwy_fr, textvariable=self.var_step_runway, width=12)
        self.cmb_step_runway.pack(side=tk.LEFT)
        tk.Label(
            rwy_fr,
            text="Blank = Setup / flight plan / wind",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._rwy_fr = rwy_fr
        self._row_runway_lbl.grid(row=5, column=0, sticky="nw", pady=4, padx=(10, 6))
        rwy_fr.grid(row=5, column=1, sticky="we", pady=4, padx=(0, 10))

        self._row_recovery_lbl = tk.Label(form, text="Recovery", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9))
        rec_fr = tk.Frame(form, bg=C_PANEL)
        self._recovery_by_label = {lab: key for key, lab in atc_phrase.RECOVERY_CHOICES}
        self._label_by_recovery = {key: lab for key, lab in atc_phrase.RECOVERY_CHOICES}
        # Listbox picker (ttk.Combobox popdown fails in this scrolled dark form)
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
        self._row_recovery_lbl.grid(row=12, column=0, sticky="nw", pady=4, padx=(10, 6))
        rec_fr.grid(row=12, column=1, sticky="we", pady=4, padx=(0, 10))
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
        row(6, "Action", mode_fr)

        tmpl_labels = [lab for _, lab in atc_phrase.TEMPLATE_CHOICES]
        self._tmpl_by_label = {lab: key for key, lab in atc_phrase.TEMPLATE_CHOICES}
        self._label_by_tmpl = {key: lab for key, lab in atc_phrase.TEMPLATE_CHOICES}

        self._row_template_lbl = tk.Label(form, text="Template", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9))
        self.cmb_template = ttk.Combobox(
            form,
            textvariable=self.var_template,
            values=tuple(tmpl_labels),
            state="readonly",
            height=min(16, len(tmpl_labels)),
        )
        self.cmb_template.configure(
            postcommand=lambda: self.cmb_template.configure(values=tuple(tmpl_labels))
        )
        self.cmb_template.bind(
            "<<ComboboxSelected>>",
            lambda _e: (self._update_step_runway_ui(), self._update_recovery_ui()),
        )
        self._row_template_lbl.grid(row=7, column=0, sticky="nw", pady=4, padx=(10, 6))
        self.cmb_template.grid(row=7, column=1, sticky="we", pady=4, padx=(0, 10))

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
        self._row_custom_lbl.grid(row=8, column=0, sticky="nw", pady=4, padx=(10, 6))
        self.txt_custom.grid(row=8, column=1, sticky="we", pady=4, padx=(0, 10))
        self._row_custom_hint.grid(row=9, column=1, sticky="w", padx=(0, 10))

        self._row_file_lbl = tk.Label(form, text="Audio file", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9))
        file_fr = tk.Frame(form, bg=C_PANEL)
        ttk.Entry(file_fr, textvariable=self.var_file).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(file_fr, text="Browse…", command=self._browse_file).pack(side=tk.LEFT, padx=4)
        self._row_file_lbl.grid(row=10, column=0, sticky="nw", pady=4, padx=(10, 6))
        file_fr.grid(row=10, column=1, sticky="we", pady=4, padx=(0, 10))
        self._file_fr = file_fr

        ttk.Checkbutton(
            form,
            text="Include in flight (enabled)",
            variable=self.var_enabled,
            style="Panel.TCheckbutton",
        ).grid(row=11, column=1, sticky="w", pady=6, padx=(0, 10))

        # Extra things the pilot can say to fire this step, on top of the
        # built-in grammar. Shown on the Fly tab's "You can say" card.
        self._row_phrases_lbl = tk.Label(
            form, text="Voice phrases", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9)
        )
        self.txt_step_phrases = tk.Text(
            form,
            height=3,
            bg=C_CARD,
            fg=C_TEXT,
            insertbackground=C_TEXT,
            font=("Segoe UI", 10),
            relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self._row_phrases_hint = tk.Label(
            form,
            text="One per line. Any one fires this step; near-misses count.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        )
        self._row_phrases_lbl.grid(row=13, column=0, sticky="nw", pady=4, padx=(10, 6))
        self.txt_step_phrases.grid(row=13, column=1, sticky="we", pady=4, padx=(0, 10))
        self._row_phrases_hint.grid(row=14, column=1, sticky="w", padx=(0, 10), pady=(0, 6))

        self._last_preview_phrase = ""
        self._preview_base_phrase = ""  # last auto-filled phrase (detect Preview edits)
        self._last_preview_channel = "other"
        self._last_preview_file: str | None = None
        self._update_freq_ui()
        self._refresh_step_speed_ui()
        self._mode_ui()

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
        self.cmb_step_runway.configure(values=tuple(choices))

    def _update_step_runway_ui(self) -> None:
        show = self._step_uses_runway()
        if show:
            self._refresh_step_runway_choices()
            self._row_runway_lbl.grid(row=5, column=0, sticky="nw", pady=4, padx=(10, 6))
            self._rwy_fr.grid(row=5, column=1, sticky="we", pady=4, padx=(0, 10))
        else:
            self._row_runway_lbl.grid_remove()
            self._rwy_fr.grid_remove()
        self._update_recovery_ui()
        if hasattr(self, "_plan_canvas"):
            self.after(30, lambda: self._plan_canvas.configure(scrollregion=self._plan_canvas.bbox("all")))

    def _update_recovery_ui(self) -> None:
        if not hasattr(self, "_row_recovery_lbl"):
            return
        lab = self.var_template.get()
        tmpl = self._tmpl_by_label.get(lab, "")
        show = tmpl == "approach_check_in" and self.var_mode.get() == "tts"
        if show:
            self._row_recovery_lbl.grid(row=12, column=0, sticky="nw", pady=4, padx=(10, 6))
            self._rec_fr.grid(row=12, column=1, sticky="we", pady=4, padx=(0, 10))
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

    def _mode_ui(self) -> None:
        mode = self.var_mode.get()
        # Show only the fields needed for the selected action (saves vertical space)
        show_tmpl = mode == "tts"
        show_custom = mode == "custom"
        show_file = mode == "file"

        def _set(widget: tk.Widget, visible: bool, **grid_kw: Any) -> None:
            if visible:
                widget.grid(**grid_kw)
            else:
                widget.grid_remove()

        _set(self._row_template_lbl, show_tmpl, row=7, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self.cmb_template, show_tmpl, row=7, column=1, sticky="we", pady=4, padx=(0, 10))
        if show_tmpl:
            self.cmb_template.configure(state="readonly")

        _set(self._row_custom_lbl, show_custom, row=8, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self.txt_custom, show_custom, row=8, column=1, sticky="we", pady=4, padx=(0, 10))
        _set(self._row_custom_hint, show_custom, row=9, column=1, sticky="w", padx=(0, 10))
        if show_custom:
            self.txt_custom.configure(state=tk.NORMAL)
        else:
            self.txt_custom.configure(state=tk.DISABLED)

        _set(self._row_file_lbl, show_file, row=10, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self._file_fr, show_file, row=10, column=1, sticky="we", pady=4, padx=(0, 10))

        self._update_step_runway_ui()
        self._update_recovery_ui()

        if hasattr(self, "_plan_canvas"):
            self.after(30, lambda: self._plan_canvas.configure(scrollregion=self._plan_canvas.bbox("all")))

    def _steps(self) -> list:
        """Full mission timeline (single list)."""
        if not isinstance(self.mission.get("steps"), list):
            self.mission["steps"] = flow_engine.mission_steps(self.mission)
            self.mission.pop("outbound", None)
            self.mission.pop("inbound", None)
        return self.mission["steps"]

    def _airport(self) -> dict:
        key = self.mission.get("airport") or self.config_data.get("default_airport") or "nellis"
        return self.airports.get(key) or next(iter(self.airports.values()))

    def _set_channel(self, agency: str) -> None:
        if self._step_is_locked():
            return
        prev = (self.var_channel.get() or "").strip().lower()
        self.var_channel.set(agency)
        self._paint_channel_buttons()
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
        dlg.geometry(f"420x{height}")

        tk.Label(
            dlg,
            text=heading or title,
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
        lb.bind("<Double-Button-1>", accept)
        lb.bind("<Return>", accept)
        dlg.bind("<Escape>", cancel)
        lb.focus_set()
        dlg.wait_window()
        return result["value"]

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
        )
        if picked:
            self.var_step_recovery.set(picked)

    def _choose_step_voice(self) -> None:
        """Pick a voice for this step only (does not change Setup agency defaults)."""
        voices = self._list_voices()
        if not voices:
            messagebox.showwarning("Voices", "No voices available for the current TTS provider.")
            return
        current = self.var_step_voice.get().strip()
        if not current:
            ch = self.var_channel.get() or "other"
            current = atc_phrase.voice_for_channel(self.config_data, ch)[0]

        dlg = tk.Toplevel(self)
        dlg.title("Step voice")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("420x380")
        tk.Label(dlg, text="Voice for this step only", bg=C_BG, fg=C_TEXT, font=("Segoe UI Semibold", 11)).pack(
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

        def on_ok() -> None:
            pick = resolve_voice()
            if not pick:
                return
            self.var_step_voice.set(pick)
            self._refresh_step_voice_display()
            dlg.destroy()

        def on_preview() -> None:
            self._preview_voice_sample(resolve_voice())

        btns = tk.Frame(dlg, bg=C_BG)
        btns.pack(fill=tk.X, padx=12, pady=(4, 12))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Select", style="Accent.TButton", command=on_ok).pack(side=tk.RIGHT, padx=8)
        ttk.Button(btns, text="Preview", command=on_preview).pack(side=tk.LEFT)
        lb.bind("<Double-Button-1>", lambda _e: on_ok())
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
        self.txt_step_phrases.configure(state=tk.NORMAL)
        self.txt_step_phrases.delete("1.0", tk.END)
        phrases = voice_intent.parse_phrases(step.get("voice_phrases"))
        if phrases:
            self.txt_step_phrases.insert(tk.END, "\n".join(phrases))
        self._mode_ui()
        self._update_freq_ui()
        self._refresh_step_voice_display()
        self._refresh_step_speed_ui()
        self._update_step_runway_ui()
        self._update_recovery_ui()
        self._loading = False
        self._apply_lock_ui()
        # Show preview immediately for the selected step
        self.after(60, lambda: self.preview_step(apply=False))

    def apply_step(self) -> None:
        if self.selected_index is None:
            messagebox.showinfo("Step", "Select a step in the timeline first.")
            return
        steps = self._steps()
        step = steps[self.selected_index]
        if step.get("locked"):
            messagebox.showinfo("Locked", "Unlock this step before editing.")
            return
        step["label"] = self.var_label.get().strip() or "Step"
        step["channel"] = self.var_channel.get().strip() or "other"
        step["phase"] = step["channel"]
        mode = self.var_mode.get()
        step["enabled"] = bool(self.var_enabled.get())

        voice = self.var_step_voice.get().strip()
        if voice:
            step["voice"] = voice
        else:
            step.pop("voice", None)

        phrases = voice_intent.parse_phrases(self.txt_step_phrases.get("1.0", tk.END))
        if phrases:
            step["voice_phrases"] = list(phrases)
        else:
            step.pop("voice_phrases", None)

        if self.var_step_speed_custom.get():
            step["tts_speed"] = atc_phrase.tts_speed(speed=self.var_step_speed.get())
        else:
            step.pop("tts_speed", None)

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
                messagebox.showerror("Freq", "Other channel needs a frequency in MHz (e.g. 255.4).")
                return
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
        # Do not regenerate phrase here — Apply only saves settings.
        # Use Regenerate text to re-roll template wording.

    def regenerate_step_text(self) -> None:
        """Rebuild the spoken phrase (re-rolls random climb / Local vs freq / etc.)."""
        if self.selected_index is None:
            messagebox.showinfo("Step", "Select a step in the timeline first.")
            return
        if not self._step_is_locked():
            self.apply_step()
        self.preview_step(apply=False)

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
        dlg.geometry("640x560")
        dlg.minsize(560, 480)
        dlg.transient(self)
        dlg.grab_set()

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
                        rwy = atc_phrase.active_runway(list(ap.get("runways") or ["21R"]), wx.wind_dir)
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
                step["phase"] = apply_meta["channel"]
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
                    step["phase"] = params["from_channel"]

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
            "phase": ch,
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
        path = filedialog.askopenfilename(filetypes=[("Audio", "*.mp3 *.ogg"), ("All", "*.*")])
        if path:
            self.var_file.set(path)
            self.var_mode.set("file")
            self._mode_ui()

    def _flows_dir(self) -> Path:
        d = HERE / "flows"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _mission_file_slug(self, name: str) -> str:
        return re.sub(r"[^\w\-]+", "_", (name or "").strip()).strip("_").lower() or "mission"

    def _is_base_flow_path(self, path: Path) -> bool:
        return path.name.endswith("_default.json")

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
        dlg.geometry("640x520")
        dlg.minsize(560, 460)
        dlg.transient(self)
        dlg.grab_set()

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
        row_label(1, "Airport")
        ttk.Combobox(
            inner,
            textvariable=var_airport,
            values=airport_keys,
            state="readonly",
            width=37,
        ).grid(row=1, column=1, sticky="we", pady=6)
        row_label(2, "Base flow")
        ttk.Combobox(
            inner,
            textvariable=var_base,
            values=[lab for lab, _ in base_choices],
            state="readonly",
            width=37,
        ).grid(row=2, column=1, sticky="we", pady=6)
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
            self.config_data["tts_speed"] = atc_phrase.tts_speed(speed=self.var_speed.get())
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

                def show() -> None:
                    self._last_preview_channel = channel
                    eng = "Google" if provider == "google" else "Windows"
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
                    google_credentials=google_creds if provider == "google" else None,
                )
                if provider == "google":
                    self.after(0, self._refresh_tts_usage)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("Hear locally", str(exc)))

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
        shown = self._phrase_from_preview_box()
        if shown and not step.get("text"):
            step["text"] = shown

        def work() -> None:
            try:
                # Reuse live engine (unsaved mission + caches) — avoid cold FlowEngine()
                self.engine.config = self.config_data
                self.engine.airports = self.airports
                self.engine.mission = self.mission
                detail = self.engine.play_step(step)

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
                    base = str(detail["text"]) if detail.get("text") else ""
                    self._set_preview_display(body, base_phrase=base)
                    self._last_preview_channel = step.get("channel") or "other"

                self.after(0, done)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("TX preview", str(exc)))

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
        hdr.pack(fill=tk.X, padx=20, pady=(14, 0))
        tk.Label(
            hdr,
            text="NEXT TRANSMIT",
            bg=C_PANEL,
            fg=C_AMBER,
            font=("Segoe UI Semibold", 13),
        ).pack(side=tk.LEFT)
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
            font=("Segoe UI Semibold", 18),
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
            font=("Segoe UI Semibold", 28),
            wraplength=960,
            justify=tk.LEFT,
            anchor="w",
        )
        self._fly_step_name_lbl.pack(fill=tk.X, padx=20, pady=(2, 4))

        # Play controls sit with the step name / seek arrows so you don't scroll
        # past frequency and phrase boxes to advance the flow.
        btns = tk.Frame(card, bg=C_PANEL)
        btns.pack(fill=tk.X, padx=20, pady=(0, 8))
        btns.columnconfigure(0, weight=3)
        btns.columnconfigure(1, weight=2)
        ttk.Button(
            btns,
            text="PLAY AND ADVANCE",
            style="FlyPlay.TButton",
            command=lambda: self._fly("next"),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(
            btns,
            text="PLAY PREVIOUS",
            style="Big.TButton",
            command=lambda: self._fly("back"),
        ).grid(row=0, column=1, sticky="ew", padx=(8, 0))

        # Frequency hero
        freq_box = tk.Frame(card, bg="#0a0e14", highlightbackground=C_GREEN, highlightthickness=2)
        freq_box.pack(fill=tk.X, padx=20, pady=(0, 8))
        tk.Label(
            freq_box,
            text="FREQUENCY",
            bg="#0a0e14",
            fg=C_MUTED,
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w", padx=14, pady=(8, 0))
        freq_row = tk.Frame(freq_box, bg="#0a0e14")
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
            freq_box,
            textvariable=self.fly_channel,
            bg="#0a0e14",
            fg=C_ACCENT,
            font=("Segoe UI Semibold", 16),
            anchor="w",
        )
        self._fly_channel_lbl.pack(fill=tk.X, padx=14, pady=(0, 2))
        tk.Label(
            freq_box,
            textvariable=self.fly_tx_name,
            bg="#0a0e14",
            fg=C_MUTED,
            font=("Segoe UI", 11),
            anchor="w",
        ).pack(fill=tk.X, padx=14, pady=(0, 10))

        # Compact phrase readout — what will go out on the radio
        self._fly_will_say_box = tk.Frame(
            card, bg=C_CARD, highlightbackground=C_BORDER, highlightthickness=1
        )
        self._fly_will_say_box.pack(fill=tk.X, padx=20, pady=(0, 8))
        tk.Label(
            self._fly_will_say_box,
            text="WILL SAY",
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

        # After ATC transmits, show the items the pilot should read back
        # (squawk, runway, …). Highlighted values are the ones that matter most.
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
        ).pack(anchor="w", padx=14, pady=(10, 4))
        self.fly_readback_body = tk.Frame(self.fly_readback_frame, bg="#14100a")
        self.fly_readback_body.pack(fill=tk.X, padx=14, pady=(0, 12))

        # Voice prompts live in this same card as FREQUENCY / WILL SAY so the
        # kneeboard glance is one panel, not a separate strip further down.
        self.fly_say_title = tk.StringVar(value="YOU CAN SAY")
        self.fly_say_frame = tk.Frame(
            card, bg="#0a0e14", highlightbackground=C_ACCENT, highlightthickness=2
        )
        tk.Label(
            self.fly_say_frame,
            textvariable=self.fly_say_title,
            bg="#0a0e14",
            fg=C_ACCENT,
            font=("Segoe UI Semibold", 12),
        ).pack(anchor="w", padx=16, pady=(12, 4))
        self.fly_say_body = tk.Label(
            self.fly_say_frame,
            text="",
            bg="#0a0e14",
            fg=C_TEXT,
            font=("Segoe UI Semibold", 14),
            justify=tk.LEFT,
            anchor="nw",
            wraplength=920,
        )
        self.fly_say_body.pack(fill=tk.X, padx=14, pady=(0, 10))

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
            text="Will you accept rolling?",
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
        self.cmb_jump = ttk.Combobox(row, textvariable=self.var_jump, state="readonly", width=36)
        self.cmb_jump.pack(side=tk.LEFT, padx=(0, 6), fill=tk.X, expand=True)
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
        freq, mod, tx_name = atc_phrase.step_radio(ap, channel, step)
        # UHF/VFR style: always three decimals for glanceable kneeboard read
        freq_disp = f"{float(freq):.3f}"
        ch_label = channel.upper()
        return ch_label, freq_disp, str(mod or "AM").upper(), str(tx_name or "")

    def _queue_fly_phrase_preview(self, step: dict[str, Any] | None) -> None:
        """Fill WILL SAY with the upcoming radio phrase (async; may re-roll templates)."""
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
    }

    def _sync_fly_recovery_ui(self, channel: str | None = None) -> None:
        """Show recovery buttons only on Approach; rebuild button strip."""
        if not hasattr(self, "_fly_rec_box"):
            return
        ch = (channel if channel is not None else self._fly_current_channel()).strip().lower()
        active = atc_phrase.resolve_active_recovery(
            None, self.mission, state=self.engine.state
        )
        self._fly_recovery_loading = True
        self.fly_recovery.set(atc_phrase.recovery_label(active))
        self._fly_recovery_loading = False

        for child in self._fly_rec_btns.winfo_children():
            child.destroy()

        if ch != "approach":
            self._fly_rec_box.pack_forget()
            return

        opts = self._fly_recovery_options()
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
        return str(step.get("channel") or step.get("phase") or "other").strip().lower() or "other"

    def _sync_fly_pilot_request_ui(self, channel: str | None = None) -> None:
        """Show Tower offer + pilot-request buttons only when relevant to current freq."""
        if not hasattr(self, "_fly_req_box"):
            return
        mode = atc_phrase.resolve_active_takeoff_mode(self.mission, self.engine.state)
        self.fly_takeoff_mode.set(f"Takeoff: {atc_phrase.takeoff_mode_label(mode)}")
        ch = (channel if channel is not None else self._fly_current_channel()).strip().lower()
        pending = atc_phrase.pending_takeoff_offer(self.engine.state)

        # Offer bar
        if pending == "rolling" and ch == "tower":
            self._fly_offer_fr.pack(fill=tk.X, padx=20, pady=(0, 6))
        else:
            self._fly_offer_fr.pack_forget()

        # Request buttons (skip Accept/Deny — those are on the offer bar)
        for child in self._fly_req_btns.winfo_children():
            child.destroy()
        reqs = [
            (k, lab)
            for k, lab in atc_phrase.pilot_requests_for_channel(
                ch, self.engine.state, airport=self._airport()
            )
            if k not in ("accept_rolling", "deny_rolling")
        ]
        if not reqs:
            self._fly_req_box.pack_forget()
            return

        for key, lab in reqs:
            short = self._FLY_REQUEST_BTN_LABELS.get(key, lab)
            accent = False
            if key == "request_rolling" and mode == "rolling":
                accent = True
            if key == "request_lineup" and mode == "lineup":
                accent = True
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
            result = atc_phrase.apply_pilot_request(
                request_key, mission=self.mission, state=self.engine.state
            )
        except ValueError as exc:
            messagebox.showerror("Pilot request", str(exc))
            return
        self.engine.mission = self.mission
        try:
            self.engine.save_state()
        except Exception:
            pass
        # Persist takeoff mode on the active mission file
        try:
            path = flow_engine.resolve_flow_path(self.config_data)
            self.mission["name"] = self.mission_name_var.get().strip() or self.mission.get("name") or "Untitled"
            flow_engine.normalize_mission_to_steps(self.mission)
            save_json(path, self.mission)
        except Exception:
            pass
        self._sync_fly_pilot_request_ui()
        self.engine.prepare_takeoff_cursor()
        try:
            self.engine.save_state()
        except Exception:
            pass
        self._queue_fly_phrase_preview(self.engine.current_step())
        if not tx_ack:
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
                self.after(0, lambda: messagebox.showerror("Pilot request", str(exc)))

        threading.Thread(target=work, daemon=True).start()

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

        self.fly_readback_title.set(
            "READ BACK  ·  agency name optional — say the highlighted items"
        )
        for item in items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            spoken = str(item.get("spoken") or "").strip()
            highlight = bool(item.get("highlight"))
            if not value:
                continue
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
                fg=C_AMBER if highlight else C_TEXT,
                font=("Consolas", 22, "bold") if highlight else ("Segoe UI Semibold", 16),
                anchor="w",
            ).pack(side=tk.LEFT, padx=(4, 10))
            if spoken and spoken.casefold() != value.casefold():
                tk.Label(
                    row,
                    text=f'“{spoken}”',
                    bg="#14100a",
                    fg=C_MUTED,
                    font=("Segoe UI", 12),
                    anchor="w",
                ).pack(side=tk.LEFT)

        # Sit above YOU CAN SAY when both are visible; otherwise under WILL SAY.
        after = (
            self.fly_say_frame
            if self.fly_say_frame.winfo_manager()
            else self._fly_will_say_box
        )
        # Pack before the say frame when present so readback stays the focus.
        if self.fly_say_frame.winfo_manager():
            if not self.fly_readback_frame.winfo_manager():
                self.fly_readback_frame.pack(
                    fill=tk.X, padx=20, pady=(0, 8), before=self.fly_say_frame
                )
        elif not self.fly_readback_frame.winfo_manager():
            self.fly_readback_frame.pack(
                fill=tk.X, padx=20, pady=(0, 8), after=after
            )

    def _refresh_voice_prompts(self) -> None:
        """
        Fill the 'You can say' block inside the NEXT TRANSMIT card.

        Written out in full, agency and callsign included, because that is what
        the gate needs to hear — reading a line off the card always works, and
        anything close to it works too. While a readback is outstanding the
        lead line is the readback itself (no agency opener).
        """
        if not hasattr(self, "fly_say_frame"):
            return
        if not self.config_data.get("voice_enabled"):
            self.fly_say_frame.pack_forget()
            self._refresh_readback_panel()
            self.after_idle(self._fly_update_scrollregion)
            return

        context = self._voice_context()
        airport = context.get("airport") or {}
        awaiting = bool(context.get("awaiting_readback"))
        items = context.get("readback_items") if isinstance(context.get("readback_items"), list) else []
        lines = voice_intent.suggestions(
            phase=str(context.get("phase") or ""),
            channel=str(context.get("channel") or ""),
            expected=str(context.get("expected") or ""),
            callsign=str(context.get("callsign") or ""),
            airport_name=str(airport.get("name") or ""),
            limit=4,
            awaiting_readback=awaiting,
            readback_items=items,
            steps=context.get("steps") if isinstance(context.get("steps"), list) else None,
        )
        if not lines:
            self.fly_say_frame.pack_forget()
            self._refresh_readback_panel()
            self.after_idle(self._fly_update_scrollregion)
            return

        # Phrase on its own line, what it does underneath — easier to scan
        # from the kneeboard than a monospace table.
        body = "\n\n".join(f'"{say}"\n  →  {does}' for say, does in lines)
        self.fly_say_body.configure(text=body)
        if awaiting:
            self.fly_say_title.set("YOU CAN SAY  ·  readback — agency name optional")
        else:
            self.fly_say_title.set("YOU CAN SAY  ·  hold PTT, the short version counts")
        if not self.fly_say_frame.winfo_manager():
            self.fly_say_frame.pack(
                fill=tk.X, padx=20, pady=(0, 8), after=self._fly_will_say_box
            )
        self._refresh_readback_panel()
        self.after_idle(self._fly_update_scrollregion)

    def _refresh_fly_status(self) -> None:
        self.engine.mission = self.mission
        self.engine.reload()
        self.engine.mission = self.mission  # keep unsaved plan if editing
        # Prefer disk for fly consistency after save
        try:
            self.engine.mission = load_json(flow_engine.resolve_flow_path(self.config_data))
        except Exception:
            self.engine.mission = self.mission
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
            if str(ch).strip().lower() == "approach":
                rec = atc_phrase.resolve_active_recovery(step, self.mission, state=self.engine.state)
                hint += f"  ·  recovery {atc_phrase.recovery_label(rec)}"
            if str(ch).strip().lower() == "tower" or atc_phrase.is_takeoff_related_template(str(tmpl)):
                hint += f"  ·  takeoff {takeoff}"
            if eff != tmpl:
                hint += f"  ·  says {eff}"
            if atc_phrase.pending_takeoff_offer(self.engine.state) == "rolling":
                hint += "  ·  rolling offer pending"
            self.fly_hint.set(hint)
            color = CHANNEL_COLORS.get(ch.lower(), C_ACCENT)
            if hasattr(self, "_fly_channel_lbl"):
                self._fly_channel_lbl.configure(fg=color)
            if hasattr(self, "_fly_step_name_lbl"):
                self._fly_step_name_lbl.configure(fg=C_TEXT)
            self._queue_fly_phrase_preview(step)
        ch_now = "" if st.get("at_end") else self._fly_current_channel(st)
        self._sync_fly_recovery_ui(ch_now)
        self._sync_fly_pilot_request_ui(ch_now)
        self._refresh_jump_list(st)
        self._refresh_voice_prompts()

    def _refresh_jump_list(self, st: dict | None = None) -> None:
        if not hasattr(self, "cmb_jump"):
            return
        st = st or self.engine.status()
        labels: list[str] = []
        mapping: dict[str, int] = {}
        for item in st.get("steps") or []:
            label = f"{item['number']}. {item['label']}"
            labels.append(label)
            mapping[label] = int(item["index"])
        self._jump_index_by_label = mapping
        self.cmb_jump.configure(values=labels)
        cur = st.get("step_number")
        if cur and labels:
            # select matching 1-based entry
            for lab in labels:
                if lab.startswith(f"{cur}."):
                    self.var_jump.set(lab)
                    break
        elif labels:
            self.var_jump.set(labels[0])

    def _jump_to_selected(self) -> None:
        label = self.var_jump.get().strip()
        if not label or label not in self._jump_index_by_label:
            messagebox.showinfo("Go to step", "Pick a step from the list.")
            return
        idx = self._jump_index_by_label[label]
        self._fly("seek", seek_index=idx)

    def _fly(self, action: str, seek_index: int | None = None) -> None:
        # Auto-save so fly uses latest plan + identity overrides
        self.mission["name"] = self.mission_name_var.get().strip() or "Untitled"
        flow_engine.normalize_mission_to_steps(self.mission)
        path = flow_engine.resolve_flow_path(self.config_data)
        save_json(path, self.mission)
        self._sync_identity_to_config()
        save_json(CONFIG_PATH, self.config_data)

        def work() -> None:
            try:
                eng = flow_engine.FlowEngine()
                if action == "next":
                    r = eng.next()
                elif action == "back":
                    r = eng.back()
                elif action == "reset":
                    r = eng.reset()
                elif action == "seek_prev":
                    r = eng.seek_relative(-1)
                elif action == "seek_next":
                    r = eng.seek_relative(1)
                elif action == "seek":
                    r = eng.seek(0 if seek_index is None else seek_index)
                else:
                    raise RuntimeError(f"Unknown fly action: {action}")

                def done() -> None:
                    if isinstance(r, dict) and r.get("freq") is not None:
                        try:
                            freq_s = f"{float(r['freq']):.3f}"
                        except (TypeError, ValueError):
                            freq_s = str(r.get("freq"))
                        line = (
                            f"TX  {r.get('label') or r.get('step_id') or 'step'}  ·  "
                            f"{freq_s}  ·  {str(r.get('channel') or '').upper()}\n"
                        )
                    elif isinstance(r, dict) and (r.get("seeked") or action.startswith("seek") or action == "reset"):
                        n = r.get("step_number")
                        line = f"{action.upper()}  →  {'END' if r.get('at_end') else f'step {n}'}\n"
                    else:
                        line = f"{action.upper()}\n"
                    self.fly_log.insert(tk.END, line)
                    self.fly_log.see(tk.END)
                    self.engine = eng
                    self._refresh_fly_status()

                self.after(0, done)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("Fly", str(exc)))

        threading.Thread(target=work, daemon=True).start()

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
                    ("bullet", "2. Choose flight… from the Opus list, confirm callsign/FP, then Save setup."),
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
                    ("muted", "Pricing: cloud.google.com/text-to-speech/pricing — Cloud Billing is authoritative."),
                ],
            ),
            (
                "Windows vs Google voices",
                [
                    ("heading", "Windows (default)"),
                    ("bullet", "• No API key."),
                    ("bullet", "• Uses voices installed on this PC (David, Zira, …)."),
                    ("bullet", "• Hear locally and Voices → Preview play on speakers."),
                    ("heading", "Google (optional)"),
                    ("bullet", "• Needs your own service-account JSON."),
                    ("bullet", "• Hundreds of Neural2 / WaveNet voices."),
                    ("bullet", "• Hear locally / Voices → Preview play Google audio on speakers (uses API quota)."),
                    ("bullet", "• TX → SRS / Fly still used for radio transmit."),
                    ("heading", "Setup → Voices"),
                    ("body", "Assign a default voice per agency (delivery, ground, tower, …)."),
                    ("body", "Randomize ▾ can fill all agencies from all voices, or Chirp / Neural2 / WaveNet only."),
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
                    ("bullet", "• Play and Advance transmits the next enabled step to SRS."),
                    ("bullet", "• Back / Reset / seek jump around the timeline."),
                    ("bullet", "• On Approach: recovery buttons (Overhead / Tactical / Straight-in / Instrument)."),
                    ("heading", "Takeoff — rolling vs line up and wait"),
                    ("bullet", "• Tower may offer rolling (~35% by default; config takeoff_offer_chance)."),
                    ("bullet", "• Accept / Deny on the Tower offer bar; Request rolling / LUAW buttons on Tower."),
                    ("bullet", "• Rolling skips Line up and wait; clearance becomes cleared for takeoff rolling."),
                    ("bullet", "• Pilot request buttons only appear on frequencies that support them."),
                    ("heading", "Triggers — HOTAS, hotkeys, Stream Deck"),
                    ("bullet", "• Setup → Controls → HOTAS: Learn… a spare stick button for Advance / Previous."),
                    ("bullet", "• HOTAS is the reliable option in-game — it keeps working while DCS is focused."),
                    ("bullet", "• Show SRS PTT buttons… lists what SRS already uses so you avoid a clash."),
                    ("bullet", "• Setup → Controls → Keyboard: Capture… any free combo (F13 / F14 only suit Stream Deck)."),
                    ("bullet", "• Keys are also polled, so they keep working in-game without administrator rights."),
                    ("bullet", "• Polling does not swallow the key, so pick a combo DCS itself does not use."),
                    ("bullet", "• Use a key, a HOTAS button, or both — one press only ever advances one step."),
                    ("bullet", "• No-keystroke option: http://127.0.0.1:8765/next and /back from a Stream Deck Website action."),
                    ("bullet", "• Last trigger on the Controls tab confirms a press actually reached the app."),
                    ("heading", "Voice control"),
                    ("bullet", "• Setup → Controls → Voice: tick Enable, then hold your normal SRS PTT and talk."),
                    ("bullet", "• Mic and PTT are auto-detected from the SRS client config; override either if needed."),
                    ("bullet", "• PTT can be a HOTAS button or a key — whichever you already transmit with."),
                    ("bullet", "• Open with the agency — \u201cNellis Ground, Fleece 1, ready to taxi\u201d — or it stays silent."),
                    ("bullet", "• Not sure what to say? The Fly tab lists the calls that fit right now."),
                    ("bullet", "• After a clearance, READ BACK highlights your squawk — say \u201csquawk XXXX\u201d or \u201csquawking XXXX\u201d."),
                    ("bullet", "• Extra words in the readback are fine; you do not need to say \u201cin sequence\u201d."),
                    ("bullet", "• Wording is loose: \u201cready for taxi\u201d, \u201crequest taxi\u201d and a misheard \u201ctaxy\u201d all work."),
                    ("bullet", "• For the call that is due, the short version is enough — \u201cGround, Fleece 1, taxi\u201d."),
                    ("bullet", "• Right after ATC speaks, a plain \u201croger\u201d also clears the readback with no agency name."),
                    ("bullet", "• Try: request runway two one left · say winds · request picture · say again."),
                    ("bullet", "• A call that fires a step advances Fly on its own — no need to press Play."),
                    ("bullet", "• Want your own wording? Plan → pick a step → Voice phrases, one per line."),
                    ("bullet", "• Your phrases add to the built-in calls and show first on the Fly card."),
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
        self.setup_tab_identity = ttk.Frame(self.setup_nb)
        self.setup_tab_voices = ttk.Frame(self.setup_nb)
        self.setup_tab_airport = ttk.Frame(self.setup_nb)
        self.setup_tab_controls = ttk.Frame(self.setup_nb)
        self.setup_nb.add(self.setup_tab_identity, text="  Identity & TTS  ")
        self.setup_nb.add(self.setup_tab_voices, text="  Voices  ")
        self.setup_nb.add(self.setup_tab_airport, text="  Airport & radios  ")
        self.setup_nb.add(self.setup_tab_controls, text="  Controls  ")

        self.var_user = tk.StringVar()
        self.var_backend = tk.StringVar()
        self.var_callsign_override = tk.StringVar()
        self.var_opus_flight = tk.StringVar(value="(choose an Opus flight)")
        self.var_callsign = tk.StringVar(value="(refresh to resolve)")
        self._opus_flight_rows: list[dict[str, Any]] = []
        self.var_volume = tk.DoubleVar(value=0.8)
        self.var_speed = tk.DoubleVar(value=7)
        self.var_volume_lbl = tk.StringVar(value="0.80")
        self.var_speed_lbl = tk.StringVar(value="7")
        self.var_freq_status = tk.StringVar(value="")
        self.var_tts_provider = tk.StringVar(value="windows")
        self.var_google_credentials = tk.StringVar()
        self.var_tts_status = tk.StringVar(value="")
        self.var_voice_status = tk.StringVar(value="")

        self.var_volume.trace_add("write", lambda *_: self.var_volume_lbl.set(f"{float(self.var_volume.get()):.2f}"))
        self.var_speed.trace_add(
            "write", lambda *_: self.var_speed_lbl.set(str(atc_phrase.tts_speed(speed=self.var_speed.get())))
        )

        self._build_setup_identity()
        self._build_setup_voices()
        self._build_setup_airport()
        self._build_setup_controls()

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
        self._setup_field(lf, 0, "Opus username", self.var_user)
        tk.Label(
            lf,
            text="Optional. Matches your seat on the selected flight when signed up.",
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
        self._setup_field(lf, 4, "Manual callsign", self.var_callsign_override)
        tk.Label(
            lf,
            text="Optional. Use alone for offline TTS (no Opus). Blank = selected flight name.",
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
        ttk.Button(btns, text="Refresh from Opus", command=self._refresh_callsign).pack(side=tk.LEFT)
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
            text="Google Cloud TTS (optional BYOK)",
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
            text="Need a service-account JSON file (not an AIza… API key). Paste JSON… will save it for you.",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(2, 0))

        self._tts_status_windows = tk.Label(
            right,
            text="Windows voices — no API key. Assign per agency on the Voices tab.",
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
        rand_menu_btn = ttk.Menubutton(hdr, text="Randomize ▾")
        rand_menu = tk.Menu(rand_menu_btn, tearoff=0)
        rand_menu.add_command(
            label="All voices (unique)",
            command=lambda: self._randomize_agency_voices(family=None),
        )
        rand_menu.add_separator()
        rand_menu.add_command(
            label="Chirp 3: HD only",
            command=lambda: self._randomize_agency_voices(family="chirp"),
        )
        rand_menu.add_command(
            label="Neural2 only",
            command=lambda: self._randomize_agency_voices(family="neural2"),
        )
        rand_menu.add_command(
            label="WaveNet only",
            command=lambda: self._randomize_agency_voices(family="wavenet"),
        )
        rand_menu_btn["menu"] = rand_menu
        rand_menu_btn.pack(side=tk.RIGHT)
        ttk.Button(hdr, text="Refresh list", command=self._refresh_voice_list).pack(side=tk.RIGHT, padx=8)
        tk.Label(
            panel,
            textvariable=self.var_voice_status,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=14, pady=(0, 6))

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
            tk.Label(cell, text=ch, bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10), width=10, anchor="w").pack(
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
        tk.Label(
            lf,
            text="Blank = flight plan / wind  (e.g. 21R or 03L)",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).grid(row=7, column=1, sticky="w")
        self._setup_field(lf, 8, "Expect FL (min)", self.var_expect_minutes, width=28)
        self._setup_field(lf, 9, "Known SIDs", self.var_known_sids, width=28)
        lf.columnconfigure(1, weight=1)

        tk.Label(right, text="Frequencies (MHz)", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI Semibold", 10)).pack(
            anchor="w", pady=(0, 6)
        )
        ff = tk.Frame(right, bg=C_PANEL)
        ff.pack(fill=tk.X)
        for i, ch in enumerate(atc_phrase.CHANNELS):
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
        panel = tk.Frame(root, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        panel.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        self.var_hotkey_next = tk.StringVar(value=hotkeys.DEFAULT_HOTKEY_NEXT)
        self.var_hotkey_back = tk.StringVar(value=hotkeys.DEFAULT_HOTKEY_BACK)
        self.var_hotkey_status = tk.StringVar(value="")
        self.var_joy_next = tk.StringVar(value="(none)")
        self.var_joy_back = tk.StringVar(value="(none)")
        self.var_trigger_seen = tk.StringVar(value="Last trigger: (none yet)")

        # --- HOTAS: the reliable path in-game ---
        joy = tk.Frame(panel, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        joy.pack(fill=tk.X, padx=14, pady=(14, 8))
        joy_inner = tk.Frame(joy, bg=C_PANEL)
        joy_inner.pack(fill=tk.X, padx=12, pady=10)
        ttk.Label(joy_inner, text="HOTAS buttons (recommended)", style="Header.TLabel").pack(anchor="w")
        tk.Label(
            joy_inner,
            text=(
                "Reads the stick directly, so it keeps working while DCS is focused and "
                "needs no administrator rights. Bind a spare button — avoid your SRS PTT."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))

        for which, label, var in (
            ("next", "Advance (Next)", self.var_joy_next),
            ("back", "Previous (Back)", self.var_joy_back),
        ):
            row = tk.Frame(joy_inner, bg=C_PANEL)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, bg=C_PANEL, fg=C_LABEL, width=16, anchor="w").pack(side=tk.LEFT)
            tk.Label(
                row,
                textvariable=var,
                bg=C_CARD,
                fg=C_TEXT,
                font=("Consolas", 9),
                width=46,
                anchor="w",
                padx=6,
            ).pack(side=tk.LEFT, padx=(8, 6), ipady=3)
            ttk.Button(
                row, text="Learn…", command=lambda w=which: self._learn_joy_button(w)
            ).pack(side=tk.LEFT)
            ttk.Button(
                row, text="Clear", command=lambda w=which: self._clear_joy_button(w)
            ).pack(side=tk.LEFT, padx=4)

        ttk.Button(
            joy_inner, text="Show SRS PTT buttons…", command=self._suggest_srs_ptt
        ).pack(anchor="w", pady=(8, 2))

        # --- Keyboard hotkeys ---
        hk = tk.Frame(panel, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        hk.pack(fill=tk.X, padx=14, pady=(0, 8))
        hk_inner = tk.Frame(hk, bg=C_PANEL)
        hk_inner.pack(fill=tk.X, padx=12, pady=10)
        ttk.Label(hk_inner, text="Keyboard hotkeys (Stream Deck)", style="Header.TLabel").pack(anchor="w")
        tk.Label(
            hk_inner,
            text=(
                "Capture… takes whatever you press. F13 / F14 are the defaults because a "
                "Stream Deck can send them without a keyboard having them; on a plain "
                "keyboard use any free combo. Keys are polled as well as registered, so "
                "they keep working while DCS is focused — but polling does not swallow "
                "the key, so avoid a combo DCS itself uses."
            ),
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))

        row_n = tk.Frame(hk_inner, bg=C_PANEL)
        row_n.pack(fill=tk.X, pady=3)
        tk.Label(row_n, text="Advance (Next)", bg=C_PANEL, fg=C_LABEL, width=16, anchor="w").pack(side=tk.LEFT)
        ttk.Entry(row_n, textvariable=self.var_hotkey_next, width=22).pack(side=tk.LEFT, padx=(8, 6))
        ttk.Button(row_n, text="Capture…", command=lambda: self._capture_hotkey("next")).pack(side=tk.LEFT)

        row_b = tk.Frame(hk_inner, bg=C_PANEL)
        row_b.pack(fill=tk.X, pady=3)
        tk.Label(row_b, text="Previous (Back)", bg=C_PANEL, fg=C_LABEL, width=16, anchor="w").pack(side=tk.LEFT)
        ttk.Entry(row_b, textvariable=self.var_hotkey_back, width=22).pack(side=tk.LEFT, padx=(8, 6))
        ttk.Button(row_b, text="Capture…", command=lambda: self._capture_hotkey("back")).pack(side=tk.LEFT)

        actions = tk.Frame(hk_inner, bg=C_PANEL)
        actions.pack(fill=tk.X, pady=(8, 2))
        ttk.Button(actions, text="Apply controls", command=self._apply_hotkeys).pack(side=tk.LEFT)
        self.btn_admin = ttk.Button(
            actions, text="Restart as administrator…", command=self._restart_as_admin
        )
        self.btn_admin.pack(side=tk.LEFT, padx=8)

        tk.Label(
            hk_inner,
            textvariable=self.var_hotkey_status,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
            wraplength=880,
            justify="left",
        ).pack(anchor="w")

        tk.Label(
            panel,
            textvariable=self.var_trigger_seen,
            bg=C_PANEL,
            fg=C_GREEN,
            font=("Consolas", 10),
        ).pack(anchor="w", padx=26, pady=(4, 10))

        self._build_setup_voice(panel)

    def _build_setup_voice(self, panel: tk.Frame) -> None:
        self.var_voice_enabled = tk.BooleanVar(value=False)
        self.var_voice_model = tk.StringVar(value=voice_engine.DEFAULT_MODEL)
        self.var_voice_mic = tk.StringVar(value="(Windows default)")
        self.var_voice_confidence = tk.DoubleVar(value=voice_engine.DEFAULT_MIN_CONFIDENCE)
        self.var_voice_require_address = tk.BooleanVar(value=True)
        self.var_voice_status = tk.StringVar(value="Voice control off")
        self.var_voice_heard = tk.StringVar(value="")
        self.var_voice_ptt = tk.StringVar(value="(auto: SRS PTT)")
        self.var_voice_ptt_key = tk.StringVar(value="(none)")

        box = tk.Frame(panel, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        box.pack(fill=tk.X, padx=14, pady=(0, 14))
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
        ttk.Combobox(
            row1,
            textvariable=self.var_voice_model,
            values=list(voice_engine.MODEL_CHOICES),
            width=16,
            state="readonly",
        ).pack(side=tk.LEFT)
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
        self._mic_devices = mic_capture.list_input_devices() if mic_capture.supported() else []
        ttk.Combobox(
            row2,
            textvariable=self.var_voice_mic,
            values=[str(d["name"]) for d in self._mic_devices],
            width=42,
            state="readonly",
        ).pack(side=tk.LEFT, padx=(8, 6))

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
        ttk.Scale(
            row4, from_=0.4, to=0.95, variable=self.var_voice_confidence, length=220
        ).pack(side=tk.LEFT, padx=(8, 6))
        tk.Label(
            row4,
            textvariable=self.var_voice_confidence,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Consolas", 8),
            width=6,
        ).pack(side=tk.LEFT)

        row5 = tk.Frame(inner, bg=C_PANEL)
        row5.pack(fill=tk.X, pady=(6, 0))
        ttk.Checkbutton(
            row5,
            text="Only act on calls addressed to ATC",
            variable=self.var_voice_require_address,
        ).pack(side=tk.LEFT)
        tk.Label(
            inner,
            text=(
                "The flight shares this frequency. With this on, a transmission only counts "
                "when you open with the agency (\u201cNellis Tower, \u2026\u201d) or your own callsign — "
                "so \u201cTwo, go button five\u201d and general chatter never move the timeline."
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
    ) -> None:
        tk.Label(parent, text=label, bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).grid(
            row=row, column=0, sticky="w", pady=3
        )
        ttk.Entry(parent, textvariable=var, width=width).grid(
            row=row, column=1, sticky="we", pady=3, padx=(8, 0)
        )

    @staticmethod
    def _short_voice(name: str) -> str:
        s = (name or "").replace("Microsoft ", "").replace(" Desktop", "")
        if not s:
            return ""
        gender = atc_phrase.voice_gender(s)
        # Keep agency grid readable: short name + gender
        base = s if len(s) <= 22 else s[:19] + "…"
        return f"{base} · {gender}"

    def _set_agency_voice(self, channel: str, voice: str) -> None:
        voice = (voice or "").strip()
        self.voice_vars[channel].set(voice)
        if channel in self.voice_display_vars:
            self.voice_display_vars[channel].set(self._short_voice(voice))

    def _list_windows_voices(self) -> list[str]:
        ps = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.GetInstalledVoices() | ForEach-Object { if ($_.Enabled) { $_.VoiceInfo.Name } }
"""
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            names = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
            return atc_phrase.sort_voices(names) or ["Microsoft Zira Desktop"]
        except Exception:
            return ["Microsoft Zira Desktop"]

    def _list_voices(self) -> list[str]:
        if atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()}) == "google":
            return atc_phrase.google_voice_choices()
        return self._list_windows_voices()

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
            self.var_google_credentials.set(path)
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
        dlg.geometry("560x420")

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
        if provider == "google":
            self._tts_status_windows.pack_forget()
            if not self.google_cred_frame.winfo_ismapped():
                self.google_cred_frame.pack(fill=tk.X, pady=(0, 4))
            raw = self.var_google_credentials.get().strip()
            if not raw:
                self.var_tts_status.set("Select your service-account JSON, then assign voices on Voices.")
            else:
                path = Path(os.path.expandvars(os.path.expanduser(raw)))
                if path.is_file():
                    self.var_tts_status.set(f"OK · {path.name} · preview with Hear locally")
                else:
                    self.var_tts_status.set(f"File not found: {path}")
        else:
            self.google_cred_frame.pack_forget()
            if not self._tts_status_windows.winfo_ismapped():
                self._tts_status_windows.pack(anchor="w", pady=(0, 4))
        n = len(self.voice_labels or self._list_voices())
        self.var_voice_status.set(
            f"{provider.title()} mode · {n} voice(s) available · Choose per agency or Randomize ▾"
        )

    def _refresh_voice_list(self) -> None:
        self.voice_labels = self._list_voices()
        self.var_voice_status.set(
            f"Refreshed · {len(self.voice_labels)} voice(s) for "
            f"{atc_phrase.tts_provider({'tts_provider': self.var_tts_provider.get()})} mode"
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

    def _choose_voice(self, channel: str) -> None:
        """Listbox picker — more reliable than ttk.Combobox popdowns on this dark UI."""
        voices = self._list_voices()
        self.voice_labels = voices
        if not voices:
            messagebox.showwarning("Voices", "No voices available for the current TTS provider.")
            return

        current = self.voice_vars[channel].get().strip()
        dlg = tk.Toplevel(self)
        dlg.title(f"Choose voice — {channel}")
        dlg.configure(bg=C_BG)
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("420x380")

        tk.Label(
            dlg,
            text=f"Agency: {channel}",
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
            self._preview_voice_sample(resolve_voice())

        btns = tk.Frame(dlg, bg=C_BG)
        btns.pack(fill=tk.X, padx=12, pady=(4, 12))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Select", style="Accent.TButton", command=on_ok).pack(side=tk.RIGHT, padx=8)
        ttk.Button(btns, text="Preview", command=on_preview).pack(side=tk.LEFT)
        lb.bind("<Double-Button-1>", lambda _e: on_ok())
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
        sample = atc_phrase.VOICE_PREVIEW_SAMPLE

        def work() -> None:
            try:
                atc_phrase.preview_voice_local(
                    voice,
                    sample,
                    vol,
                    speed=speed,
                    google_credentials=google_creds
                    if provider == "google" or atc_phrase.is_google_voice_name(voice)
                    else None,
                )
                self.after(0, self._refresh_tts_usage)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("Preview", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _randomize_agency_voices(self, family: str | None = None) -> None:
        """
        Assign voices randomly; prefer unique voices when the catalog is large enough.
        family: None = full catalog, or 'chirp' / 'neural2' / 'wavenet'.
        """
        voices = self._list_voices()
        self.voice_labels = voices
        if not voices:
            messagebox.showwarning("Voices", "No voices available for the current TTS provider.")
            return

        provider = atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()})
        if family and provider != "google":
            messagebox.showinfo(
                "Randomize",
                "Chirp / Neural2 / WaveNet filters need Google Cloud TTS mode.",
            )
            return

        if family:
            pool = [v for v in voices if atc_phrase.voice_billing_family(v) == family]
            label = {"chirp": "Chirp 3: HD", "neural2": "Neural2", "wavenet": "WaveNet"}.get(
                family, family
            )
            if not pool:
                messagebox.showwarning("Randomize", f"No {label} voices in the current list.")
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
        for ch in order:
            unused = [v for v in pool if v not in used]
            if unused:
                pick = unused[0]
                assigned[ch] = pick
                used.add(pick)
            else:
                assigned[ch] = random.choice(pool)

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
        self._update_tts_status()

    def _load_setup_fields(self) -> None:
        c = self.config_data
        self.var_user.set(c.get("opus_user_name", ""))
        self.var_backend.set(c.get("opus_backend_url", ""))
        self.var_callsign_override.set(c.get("callsign_override", "") or "")
        self._update_opus_flight_label()
        self.var_runway_override.set(c.get("runway_override", "") or "")
        self.var_volume.set(float(c.get("tts_volume", 0.8)))
        self.var_speed.set(float(c.get("tts_speed", 7)))
        provider = atc_phrase.tts_provider(c)
        self.var_tts_provider.set(provider)
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
        for which, var in (("next", "var_joy_next"), ("back", "var_joy_back")):
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
            self.var_voice_require_address.set(bool(c.get("voice_require_address", True)))
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
        self.config_data["opus_user_name"] = self.var_user.get().strip()
        self.config_data["opus_backend_url"] = self.var_backend.get().strip()
        self.config_data["callsign_override"] = self.var_callsign_override.get().strip()
        self.config_data["runway_override"] = self.var_runway_override.get().strip()
        provider = atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()})
        self.config_data["tts_provider"] = provider
        self.config_data["google_credentials"] = self.var_google_credentials.get().strip()
        if provider == "google":
            creds = atc_phrase.google_credentials_path(self.config_data)
            if creds is None or not creds.is_file():
                messagebox.showerror(
                    "Google TTS",
                    "tts_provider is Google but the credentials JSON path is missing or invalid.\n\n"
                    "Create a Google Cloud service account key and Browse to the .json file.",
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
        self.config_data["tts_speed"] = atc_phrase.tts_speed(speed=self.var_speed.get())
        if hasattr(self, "var_hotkey_next"):
            self.config_data["hotkey_next"] = hotkeys.normalize_hotkey(
                self.var_hotkey_next.get(), default=hotkeys.DEFAULT_HOTKEY_NEXT
            )
            self.config_data["hotkey_back"] = hotkeys.normalize_hotkey(
                self.var_hotkey_back.get(), default=hotkeys.DEFAULT_HOTKEY_BACK
            )
            self.var_hotkey_next.set(str(self.config_data["hotkey_next"]))
            self.var_hotkey_back.set(str(self.config_data["hotkey_back"]))
        for which in ("next", "back"):
            self.config_data[f"joy_{which}"] = self._joy_bindings.get(which)
        if hasattr(self, "var_voice_enabled"):
            self.config_data["voice_enabled"] = bool(self.var_voice_enabled.get())
            self.config_data["voice_model"] = (
                self.var_voice_model.get().strip() or voice_engine.DEFAULT_MODEL
            )
            self.config_data["voice_mic_device"] = self._selected_mic_index()
            self.config_data["voice_min_confidence"] = round(
                float(self.var_voice_confidence.get()), 2
            )
            self.config_data["voice_require_address"] = bool(
                self.var_voice_require_address.get()
            )
        save_json(CONFIG_PATH, self.config_data)
        self._apply_hotkeys()
        self._apply_voice()

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

    def _update_opus_flight_label(self, row: dict[str, Any] | None = None) -> None:
        """Refresh the Selected flight label from config and optional picker row."""
        fid = atc_phrase.configured_opus_flight_id(self.config_data)
        if fid is None:
            self.var_opus_flight.set("(choose an Opus flight)")
            return
        if row and int(row.get("id") or 0) == fid:
            cs = str(row.get("callsign") or f"#{fid}")
            route = str(row.get("fp_route_string") or "")
            route_short = route if len(route) <= 36 else route[:33] + "…"
            bits = [cs]
            if row.get("event_date"):
                bits.append(str(row["event_date"]))
            if row.get("vul_start"):
                bits.append(str(row["vul_start"]))
            if route_short:
                bits.append(route_short)
            slots = row.get("crew_slots") or atc_phrase.opus_crew_slots(
                row.get("signups"), qty=row.get("qty")
            )
            crew = [f"{c['seat']}:{c['user_name']}" for c in slots if c.get("user_name")]
            if crew:
                bits.append("crew " + ", ".join(crew))
            seat = atc_phrase.configured_opus_seat(self.config_data)
            if seat:
                bits.append(f"using seat {seat}")
            self.var_opus_flight.set(" · ".join(bits))
            return
        # Cached label from config or minimal id
        cached = str(self.config_data.get("opus_flight_label") or "").strip()
        self.var_opus_flight.set(cached or f"Flight #{fid}")

    def _clear_opus_flight(self) -> None:
        self.config_data.pop("opus_flight_id", None)
        self.config_data.pop("opus_seat", None)
        self.config_data.pop("opus_flight_label", None)
        atc_phrase.invalidate_opus_cache()
        self._update_opus_flight_label()
        self.var_callsign.set("(refresh to resolve)")

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
        dlg.geometry("980x560")
        dlg.transient(owner)
        dlg.grab_set()

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
                    self.after(0, lambda: status.set(f"Failed: {exc}"))
                    self.after(0, lambda: messagebox.showerror("Opus flights", str(exc), parent=dlg))

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
            cs = str(row.get("callsign") or f"#{row['id']}")
            route = str(row.get("fp_route_string") or "")
            route_short = route if len(route) <= 42 else route[:39] + "…"
            crew_names = [
                f"{c['seat']}:{c['user_name']}"
                for c in (row.get("crew_slots") or atc_phrase.opus_crew_slots(row.get("signups"), qty=row.get("qty")))
                if c.get("user_name")
            ]
            label_bits = [cs]
            if row.get("event_date"):
                label_bits.append(str(row["event_date"]))
            if route_short:
                label_bits.append(route_short)
            if crew_names:
                label_bits.append("crew " + ", ".join(crew_names))
            self.config_data["opus_flight_label"] = " · ".join(label_bits)
            atc_phrase.invalidate_opus_cache()
            self._update_opus_flight_label(row)
            dlg.destroy()
            self._refresh_callsign()
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
                self.after(
                    0,
                    lambda: self.var_callsign.set(
                        "(not found — choose an Opus flight or set manual callsign)"
                    ),
                )
                return
            if not backend or (not selected and not (self.config_data.get("opus_user_name") or "").strip()):
                mode = "manual" if atc_phrase.callsign_override(self.config_data) else "offline"
                label = f"{opus.radio_callsign} · {mode}"
                self.after(0, lambda: self.var_callsign.set(label))
                return
            filed = "filed FP" if opus.has_filed_plan else "NO flight plan"
            route = opus.fp_route_string or ""
            route_bit = f" · {route}" if route else ""
            ov = " · manual" if atc_phrase.callsign_override(self.config_data) else ""
            local_bit = f" · dep Local {dep_local}" if dep_local else ""
            label = (
                f"{opus.radio_callsign}{ov} · seat {opus.seat} · {filed}{route_bit}{local_bit}"
            )
            self.after(0, lambda: self.var_callsign.set(label))
            self.after(0, self._update_opus_flight_label)

        threading.Thread(target=work, daemon=True).start()

    def _airport_from_form(self) -> dict[str, Any]:
        """Snapshot of airport fields currently shown in Setup (unsaved OK)."""
        key = self.mission.get("airport") or "nellis"
        ap = dict(self.airports.get(key, {}))
        ap["name"] = self.var_ap_name.get().strip() or key.title()
        ap["icao"] = self.var_icao.get().strip().upper()
        for ch in atc_phrase.CHANNELS:
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
                self.after(0, lambda: messagebox.showerror("Opus freqs", str(exc)))

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
                self.after(0, lambda: messagebox.showerror("Compare freqs", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _regen_pdf(self) -> None:
        self.save_mission()
        try:
            path = kneeboard_pdf.generate()
            messagebox.showinfo("Kneeboard PDF", f"Created:\n{path}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("PDF", str(exc))


def main() -> int:
    app = MissionPlanner()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
