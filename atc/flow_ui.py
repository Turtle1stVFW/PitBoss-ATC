#!/usr/bin/env python3
"""
455 Mission Flow Planner — one timeline, plan then fly with Play Next.
No JSON editing required.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import threading
import tkinter as tk
import uuid
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atc_phrase  # noqa: E402
import flow_engine  # noqa: E402
import kneeboard_pdf  # noqa: E402

CONFIG_PATH = HERE / "config.json"
AIRPORTS_PATH = HERE / "airports.json"

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
        self.airports = self.engine.airports
        self.selected_index: int | None = None
        self._loading = False

        self.http = None
        try:
            port = int(self.config_data.get("flow_http_port") or 8765)
            self.http = flow_engine.start_http_server(self.engine, port)
        except OSError:
            pass

        self._style()
        self._build()
        self._load_setup_fields()
        self.refresh_timeline()
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
            padding=(18, 14),
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

    def _on_close(self) -> None:
        if self.http:
            try:
                self.http.shutdown()
            except Exception:
                pass
        self.destroy()

    def _build(self) -> None:
        top = tk.Frame(self, bg=C_BG)
        top.pack(fill=tk.X, padx=16, pady=(14, 6))
        ttk.Label(top, text="Mission Flow Planner", style="Title.TLabel").pack(side=tk.LEFT)
        self.mission_name_var = tk.StringVar(value=self.mission.get("name") or "Untitled")
        name_entry = ttk.Entry(top, textvariable=self.mission_name_var, width=28)
        name_entry.pack(side=tk.LEFT, padx=16)
        ttk.Label(top, text="Plan the flight → Fly with Play Next", style="Muted.TLabel").pack(side=tk.LEFT)

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

        ttk.Label(right, text="Step settings", style="Header.TLabel").pack(anchor="w", padx=12, pady=(10, 4))

        # Always-visible bottom: actions + large preview (pack bottom-first)
        footer = tk.Frame(right, bg=C_PANEL)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(4, 10))
        ttk.Button(footer, text="Apply changes", style="Accent.TButton", command=self.apply_step).pack(
            fill=tk.X, pady=(0, 6)
        )
        prev_btns = tk.Frame(footer, bg=C_PANEL)
        prev_btns.pack(fill=tk.X)
        ttk.Button(prev_btns, text="Preview text", command=self.preview_step).pack(
            side=tk.LEFT, fill=tk.X, expand=True
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
            text="updates when you select a step",
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
            "Use Preview text / Hear locally / TX → SRS below.",
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

        mode_fr = tk.Frame(form, bg=C_PANEL)
        ttk.Radiobutton(
            mode_fr,
            text="Template",
            variable=self.var_mode,
            value="tts",
            command=self._mode_ui,
            style="Panel.TRadiobutton",
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_fr,
            text="Custom",
            variable=self.var_mode,
            value="custom",
            command=self._mode_ui,
            style="Panel.TRadiobutton",
        ).pack(side=tk.LEFT, padx=8)
        ttk.Radiobutton(
            mode_fr,
            text="File",
            variable=self.var_mode,
            value="file",
            command=self._mode_ui,
            style="Panel.TRadiobutton",
        ).pack(side=tk.LEFT)
        row(4, "Action", mode_fr)

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
        self._row_template_lbl.grid(row=5, column=0, sticky="nw", pady=4, padx=(10, 6))
        self.cmb_template.grid(row=5, column=1, sticky="we", pady=4, padx=(0, 10))

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
            text="{callsign} {runway} {altimeter} {wind}",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        )
        self._row_custom_lbl.grid(row=6, column=0, sticky="nw", pady=4, padx=(10, 6))
        self.txt_custom.grid(row=6, column=1, sticky="we", pady=4, padx=(0, 10))
        self._row_custom_hint.grid(row=7, column=1, sticky="w", padx=(0, 10))

        self._row_file_lbl = tk.Label(form, text="Audio file", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 9))
        file_fr = tk.Frame(form, bg=C_PANEL)
        ttk.Entry(file_fr, textvariable=self.var_file).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(file_fr, text="Browse…", command=self._browse_file).pack(side=tk.LEFT, padx=4)
        self._row_file_lbl.grid(row=8, column=0, sticky="nw", pady=4, padx=(10, 6))
        file_fr.grid(row=8, column=1, sticky="we", pady=4, padx=(0, 10))
        self._file_fr = file_fr

        ttk.Checkbutton(
            form,
            text="Include in flight (enabled)",
            variable=self.var_enabled,
            style="Panel.TCheckbutton",
        ).grid(row=9, column=1, sticky="w", pady=6, padx=(0, 10))

        self._last_preview_phrase = ""
        self._last_preview_channel = "other"
        self._last_preview_file: str | None = None
        self._update_freq_ui()
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

        _set(self._row_template_lbl, show_tmpl, row=5, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self.cmb_template, show_tmpl, row=5, column=1, sticky="we", pady=4, padx=(0, 10))
        if show_tmpl:
            self.cmb_template.configure(state="readonly")

        _set(self._row_custom_lbl, show_custom, row=6, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self.txt_custom, show_custom, row=6, column=1, sticky="we", pady=4, padx=(0, 10))
        _set(self._row_custom_hint, show_custom, row=7, column=1, sticky="w", padx=(0, 10))
        if show_custom:
            self.txt_custom.configure(state=tk.NORMAL)
        else:
            self.txt_custom.configure(state=tk.DISABLED)

        _set(self._row_file_lbl, show_file, row=8, column=0, sticky="nw", pady=4, padx=(10, 6))
        _set(self._file_fr, show_file, row=8, column=1, sticky="we", pady=4, padx=(0, 10))

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

        def on_ok() -> None:
            typed = custom_var.get().strip()
            picked = self._selected_voice_from_listbox(lb)
            if typed and atc_phrase.tts_provider(self.config_data) == "google":
                pick = typed
            elif picked:
                pick = picked
            elif typed:
                pick = typed
            else:
                return
            self.var_step_voice.set(pick)
            self._refresh_step_voice_display()
            dlg.destroy()

        btns = tk.Frame(dlg, bg=C_BG)
        btns.pack(fill=tk.X, padx=12, pady=(4, 12))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Select", style="Accent.TButton", command=on_ok).pack(side=tk.RIGHT, padx=8)
        lb.bind("<Double-Button-1>", lambda _e: on_ok())
        dlg.bind("<Return>", lambda _e: on_ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    def refresh_timeline(self) -> None:
        self.timeline.delete(0, tk.END)
        steps = self._steps()
        for i, step in enumerate(steps):
            en = "●" if step.get("enabled", True) else "○"
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
            line = f" {i + 1:>2}  {en}  {ch:<4}  {kind:<4}  {label}{extra}"
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
        self.var_step_voice.set(str(step.get("voice") or "").strip())
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
        self._mode_ui()
        self._update_freq_ui()
        self._refresh_step_voice_display()
        self._loading = False
        # Show preview immediately for the selected step
        self.after(60, lambda: self.preview_step(apply=False))

    def apply_step(self) -> None:
        if self.selected_index is None:
            messagebox.showinfo("Step", "Select a step in the timeline first.")
            return
        steps = self._steps()
        step = steps[self.selected_index]
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

        if mode == "file":
            step["mode"] = "file"
            step["file"] = self.var_file.get().strip() or None
            step["text"] = None
        elif mode == "custom":
            step["mode"] = "tts"
            step["text"] = self.txt_custom.get("1.0", tk.END).strip() or None
            step["file"] = None
            step["template"] = "radio_check"
        else:
            step["mode"] = "tts"
            step["text"] = None
            step["file"] = None
            lab = self.var_template.get()
            step["template"] = self._tmpl_by_label.get(lab, "radio_check")
        self.refresh_timeline()
        self.timeline.selection_set(self.selected_index)
        self.after(40, lambda: self.preview_step(apply=False))

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
        steps.insert(self.selected_index + 1, step)
        self.selected_index += 1
        self.refresh_timeline()
        self.timeline.selection_set(self.selected_index)

    def delete_step(self) -> None:
        if self.selected_index is None:
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

    def _sync_identity_to_config(self) -> None:
        """Push Setup identity / TTS fields into runtime config (even before Save)."""
        if hasattr(self, "var_user"):
            self.config_data["opus_user_name"] = self.var_user.get().strip()
        if hasattr(self, "var_backend"):
            self.config_data["opus_backend_url"] = self.var_backend.get().strip()
        if hasattr(self, "var_callsign_override"):
            self.config_data["callsign_override"] = self.var_callsign_override.get().strip()
        if hasattr(self, "var_tts_provider"):
            self.config_data["tts_provider"] = atc_phrase.tts_provider(
                {"tts_provider": self.var_tts_provider.get()}
            )
        if hasattr(self, "var_google_credentials"):
            self.config_data["google_credentials"] = self.var_google_credentials.get().strip()
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

    def preview_step(self, *, apply: bool = True) -> None:
        if self.selected_index is None:
            return
        if apply:
            self.apply_step()
        self._sync_identity_to_config()
        step = self._steps()[self.selected_index]
        req_id = getattr(self, "_preview_req_id", 0) + 1
        self._preview_req_id = req_id

        def work() -> None:
            try:
                ap = self._airport()
                opus = atc_phrase.resolve_active_opus_flight(self.config_data)
                cs = opus.radio_callsign if opus else "CALLSIGN"
                wx = atc_phrase.fetch_metar(self.config_data, ap["icao"])
                rwy = atc_phrase.pick_departure_runway(ap, wx, opus)
                channel = step.get("channel") or "other"
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
                    )
                    fp = ""
                    if opus and opus.fp_route_string:
                        fp = f" · FP {opus.fp_route_string}"
                    text = (
                        f"{phrase}\n\n"
                        f"→ {freq} {mod} · {cs} · rwy {rwy} · voice {voice}{fp}"
                    )

                def done() -> None:
                    if getattr(self, "_preview_req_id", 0) != req_id:
                        return
                    self._last_preview_phrase = phrase
                    self._last_preview_channel = channel
                    self._last_preview_file = str(file_path) if file_path else None
                    self.preview_box.delete("1.0", tk.END)
                    self.preview_box.insert(tk.END, text)

                self.after(0, done)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)

                def fail() -> None:
                    if getattr(self, "_preview_req_id", 0) != req_id:
                        return
                    if apply:
                        messagebox.showerror("Preview", err)
                    else:
                        self.preview_box.delete("1.0", tk.END)
                        self.preview_box.insert(tk.END, f"(preview unavailable)\n{err}")

                self.after(0, fail)

        threading.Thread(target=work, daemon=True).start()

    def hear_preview_local(self) -> None:
        """Play current step on local speakers (no SRS)."""
        if self.selected_index is None:
            messagebox.showinfo("Hear", "Select a step first.")
            return
        self.apply_step()
        self._sync_identity_to_config()
        step = self._steps()[self.selected_index]

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

                opus = atc_phrase.resolve_active_opus_flight(self.config_data)
                cs = opus.radio_callsign if opus else "CALLSIGN"
                wx = atc_phrase.fetch_metar(self.config_data, ap["icao"])
                rwy = atc_phrase.pick_departure_runway(ap, wx, opus)
                phrase, _, _, _ = atc_phrase.build_flow_step_phrase(
                    ap,
                    channel,
                    step.get("template") or "radio_check",
                    cs,
                    wx,
                    rwy,
                    custom_text=step.get("text"),
                    opus=opus,
                )
                voice, _ = atc_phrase.voice_for_step(self.config_data, channel, step)
                freq, mod, _ = atc_phrase.step_radio(ap, channel, step)
                vol = float(self.config_data.get("tts_volume", 0.8))
                speed = atc_phrase.tts_speed(self.config_data)
                provider = atc_phrase.tts_provider(self.config_data)

                def show() -> None:
                    self._last_preview_phrase = phrase
                    self._last_preview_channel = channel
                    self.preview_box.delete("1.0", tk.END)
                    if provider == "google":
                        note = (
                            f"{phrase}\n\n→ Google TTS cannot preview on local speakers here.\n"
                            f"Use “Hear on SRS” to test {freq} {mod} · voice {voice}."
                        )
                    else:
                        note = f"{phrase}\n\n→ local speakers · {freq} {mod} · {voice} · speed {speed}"
                    self.preview_box.insert(tk.END, note)

                self.after(0, show)
                if provider == "google":
                    self.after(
                        0,
                        lambda: messagebox.showinfo(
                            "Google TTS",
                            "Local speaker preview is Windows-only.\n\n"
                            "Use “Hear on SRS” to hear your Google voice on the radio.",
                        ),
                    )
                    return
                atc_phrase.preview_voice_local(voice, phrase, vol, speed=speed)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("Hear locally", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def hear_preview_srs(self) -> None:
        """Transmit current step to SRS (same as a live Fly play of this step)."""
        if self.selected_index is None:
            messagebox.showinfo("TX preview", "Select a step first.")
            return
        self.apply_step()
        self._sync_identity_to_config()
        save_json(CONFIG_PATH, self.config_data)
        step = self._steps()[self.selected_index]

        def work() -> None:
            try:
                eng = flow_engine.FlowEngine()
                detail = eng.play_step(step)

                def done() -> None:
                    phrase = detail.get("text") or detail.get("file") or ""
                    self.preview_box.delete("1.0", tk.END)
                    self.preview_box.insert(
                        tk.END,
                        f"{phrase}\n\n→ TX {detail.get('freq')} · {detail.get('callsign')} · "
                        f"voice {detail.get('voice') or ''}",
                    )
                    if detail.get("text"):
                        self._last_preview_phrase = str(detail["text"])
                    self._last_preview_channel = step.get("channel") or "other"

                self.after(0, done)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("TX preview", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # ---------- Fly tab ----------
    def _build_fly(self) -> None:
        f = self.tab_fly
        self.fly_status = tk.StringVar(value="…")
        self.fly_detail = tk.StringVar(value="")
        self.always_on_top = tk.BooleanVar(value=False)

        card = tk.Frame(f, bg=C_PANEL, highlightbackground=C_BORDER, highlightthickness=1)
        card.pack(fill=tk.BOTH, expand=True, padx=24, pady=24)
        ttk.Label(card, text="In-flight controls", style="Header.TLabel").pack(pady=(20, 8))
        tk.Label(card, textvariable=self.fly_status, bg=C_PANEL, fg=C_TEXT, font=("Segoe UI Semibold", 20)).pack(
            pady=8
        )
        tk.Label(card, textvariable=self.fly_detail, bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 11), wraplength=700).pack(
            pady=4
        )

        btns = tk.Frame(card, bg=C_PANEL)
        btns.pack(pady=16)
        ttk.Button(btns, text="PLAY NEXT", style="Big.TButton", command=lambda: self._fly("next")).grid(
            row=0, column=0, padx=10, pady=8
        )
        ttk.Button(btns, text="PLAY PREVIOUS", style="Big.TButton", command=lambda: self._fly("back")).grid(
            row=0, column=1, padx=10, pady=8
        )

        nav = tk.Frame(card, bg=C_PANEL)
        nav.pack(pady=(4, 12))
        tk.Label(nav, text="Navigate (no TX)", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6)
        )
        ttk.Button(nav, text="◀ Seek prev", command=lambda: self._fly("seek_prev")).grid(
            row=1, column=0, padx=6, pady=4
        )
        ttk.Button(nav, text="Seek next ▶", command=lambda: self._fly("seek_next")).grid(
            row=1, column=1, padx=6, pady=4
        )
        self.var_jump = tk.StringVar()
        self.cmb_jump = ttk.Combobox(nav, textvariable=self.var_jump, state="readonly", width=42)
        self.cmb_jump.grid(row=1, column=2, padx=6, pady=4)
        ttk.Button(nav, text="Go to step", command=self._jump_to_selected).grid(row=1, column=3, padx=6, pady=4)
        self._jump_index_by_label: dict[str, int] = {}

        ttk.Button(btns, text="RESET", command=lambda: self._fly("reset")).grid(
            row=1, column=0, columnspan=2, padx=10, pady=8
        )

        ttk.Checkbutton(
            card,
            text="Keep window on top while flying",
            variable=self.always_on_top,
            command=lambda: self.attributes("-topmost", self.always_on_top.get()),
        ).pack(pady=8)

        self.fly_log = tk.Text(card, height=10, bg=C_CARD, fg=C_MUTED, font=("Consolas", 9), relief=tk.FLAT)
        self.fly_log.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)
        self._refresh_fly_status()

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
        if st.get("at_end"):
            self.fly_status.set(f"END / {st.get('total', 0)}\n(end — seek prev or reset)")
            self.fly_detail.set("Cursor past last step — Seek prev or Reset")
        else:
            num = st.get("step_number") or 0
            total = st.get("total") or 0
            self.fly_status.set(f"STEP {num} / {total}\n{st.get('label') or '—'}")
            step = st.get("step") or {}
            self.fly_detail.set(
                f"Next to play · {step.get('channel', '')} · {step.get('mode', '')} · "
                f"{step.get('template') or step.get('file') or ''}"
            )
        self._refresh_jump_list(st)

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
                msg = json.dumps(r, indent=2)

                def done() -> None:
                    self.fly_log.insert(tk.END, msg + "\n\n")
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
                    ("bullet", "2. Click Refresh from Opus, confirm callsign, then Save setup."),
                    ("bullet", "3. Setup → Airport & radios — confirm SRS host and freqs (or Pull from Opus)."),
                    ("bullet", "4. Plan Flight — build or load a mission timeline, then Save mission."),
                    ("bullet", "5. Fly — use Play Next through the sortie (or Stream Deck later)."),
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
                    ("bullet", "• Save setup → Voices tab → pick Neural2 voices."),
                    ("bullet", "• Plan Flight → Hear on SRS to test (local speakers are Windows-only)."),
                    ("heading", "Free tier"),
                    ("body", "Google includes a monthly free character allowance for WaveNet / Neural2."),
                    ("body", "ATC phrase use is tiny — most people never leave the free tier."),
                    ("muted", "Pricing: cloud.google.com/text-to-speech/pricing"),
                ],
            ),
            (
                "Windows vs Google voices",
                [
                    ("heading", "Windows (default)"),
                    ("bullet", "• No API key."),
                    ("bullet", "• Uses voices installed on this PC (David, Zira, …)."),
                    ("bullet", "• Hear locally works on speakers."),
                    ("heading", "Google (optional)"),
                    ("bullet", "• Needs your own service-account JSON."),
                    ("bullet", "• Hundreds of Neural2 / WaveNet voices."),
                    ("bullet", "• Test with Hear on SRS / Fly — not local speaker preview."),
                    ("heading", "Setup → Voices"),
                    ("body", "Assign a default voice per agency (delivery, ground, tower, …)."),
                    ("body", "Randomize unique fills agencies with different voices when the catalog is large enough."),
                ],
            ),
            (
                "Plan Flight tips",
                [
                    ("heading", "Building a mission"),
                    ("bullet", "• Add / reorder steps in the timeline."),
                    ("bullet", "• Each step: label, radio channel, TTS template or custom text or audio file."),
                    ("bullet", "• Apply changes to step, then Save mission."),
                    ("heading", "Other frequencies (per step)"),
                    ("body", "Select channel Other — Freq becomes editable (MHz)."),
                    ("body", "Each Other step can have its own frequency (unique per step)."),
                    ("body", "Timeline shows it like: OTH … @255.4"),
                    ("heading", "Voice per step"),
                    ("body", "Choose sets a voice for that step only."),
                    ("body", "Agency default clears the override and uses Setup → Voices for that channel."),
                    ("body", "Timeline marks a custom step voice with [v]."),
                ],
            ),
            (
                "Fly & Stream Deck",
                [
                    ("heading", "Fly tab"),
                    ("bullet", "• Play Next transmits the next enabled step to SRS."),
                    ("bullet", "• Back / Reset / seek jump around the timeline."),
                    ("heading", "Stream Deck"),
                    ("body", "streamdeck\\*.cmd files can call the flow engine for Next / Back / Reset."),
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
        self.setup_nb.add(self.setup_tab_identity, text="  Identity & TTS  ")
        self.setup_nb.add(self.setup_tab_voices, text="  Voices  ")
        self.setup_nb.add(self.setup_tab_airport, text="  Airport & radios  ")

        self.var_user = tk.StringVar()
        self.var_backend = tk.StringVar()
        self.var_callsign_override = tk.StringVar()
        self.var_callsign = tk.StringVar(value="(refresh to resolve)")
        self.var_volume = tk.DoubleVar(value=0.8)
        self.var_speed = tk.DoubleVar(value=3)
        self.var_volume_lbl = tk.StringVar(value="0.80")
        self.var_speed_lbl = tk.StringVar(value="3")
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
        self._setup_field(lf, 1, "Opus backend URL", self.var_backend)
        self._setup_field(lf, 2, "Manual callsign", self.var_callsign_override)
        tk.Label(
            lf,
            text="Blank = Opus flight name (e.g. BRUISER 5)",
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).grid(row=3, column=1, sticky="w")
        tk.Label(lf, text="Active callsign", bg=C_PANEL, fg=C_LABEL).grid(row=4, column=0, sticky="w", pady=6)
        tk.Label(lf, textvariable=self.var_callsign, bg=C_PANEL, fg=C_GREEN, font=("Segoe UI Semibold", 11)).grid(
            row=4, column=1, sticky="w", pady=6
        )
        ttk.Button(lf, text="Refresh from Opus", command=self._refresh_callsign).grid(
            row=5, column=1, sticky="w", pady=6
        )
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
        tk.Label(spd_col, text="Talk speed (−10…10)", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).pack(
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
        ttk.Button(hdr, text="Randomize unique", command=self._randomize_agency_voices).pack(side=tk.RIGHT)
        ttk.Button(hdr, text="Refresh list", command=self._refresh_voice_list).pack(side=tk.RIGHT, padx=8)
        tk.Label(
            panel,
            textvariable=self.var_voice_status,
            bg=C_PANEL,
            fg=C_MUTED,
            font=("Segoe UI", 8),
        ).pack(anchor="w", padx=14, pady=(0, 6))

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
        self._setup_field(lf, 6, "Expect FL (min)", self.var_expect_minutes, width=28)
        self._setup_field(lf, 7, "Known SIDs", self.var_known_sids, width=28)
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
        ).pack(anchor="w", padx=14, pady=(4, 12))

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
                    self.var_tts_status.set(f"OK · {path.name} · test with Hear on SRS")
                else:
                    self.var_tts_status.set(f"File not found: {path}")
        else:
            self.google_cred_frame.pack_forget()
            if not self._tts_status_windows.winfo_ismapped():
                self._tts_status_windows.pack(anchor="w", pady=(0, 4))
        n = len(self.voice_labels or self._list_voices())
        self.var_voice_status.set(
            f"{provider.title()} mode · {n} voice(s) available · Choose per agency or Randomize unique"
        )

    def _refresh_voice_list(self) -> None:
        self.voice_labels = self._list_voices()
        self.var_voice_status.set(
            f"Refreshed · {len(self.voice_labels)} voice(s) for "
            f"{atc_phrase.tts_provider({'tts_provider': self.var_tts_provider.get()})} mode"
        )

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

        def apply_choice(voice: str) -> None:
            voice = voice.strip()
            if not voice:
                return
            self._set_agency_voice(channel, voice)
            dlg.destroy()

        def on_ok() -> None:
            typed = custom_var.get().strip()
            picked = self._selected_voice_from_listbox(lb)
            if typed and atc_phrase.tts_provider({"tts_provider": self.var_tts_provider.get()}) == "google":
                apply_choice(typed)
            elif picked:
                apply_choice(picked)
            elif typed:
                apply_choice(typed)

        btns = tk.Frame(dlg, bg=C_BG)
        btns.pack(fill=tk.X, padx=12, pady=(4, 12))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Select", style="Accent.TButton", command=on_ok).pack(side=tk.RIGHT, padx=8)
        lb.bind("<Double-Button-1>", lambda _e: on_ok())
        dlg.bind("<Return>", lambda _e: on_ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    def _randomize_agency_voices(self) -> None:
        """Assign voices randomly; prefer unique voices when the catalog is large enough."""
        voices = self._list_voices()
        self.voice_labels = voices
        if not voices:
            messagebox.showwarning("Voices", "No voices available for the current TTS provider.")
            return

        channels = list(self.voice_vars.keys())
        order = list(channels)
        random.shuffle(order)
        pool = list(voices)
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
                f"Randomized · {unique_n}/{len(channels)} unique (only {len(voices)} voice(s) installed)"
            )
        else:
            self.var_voice_status.set(f"Randomized · {unique_n} unique voices · Save setup to keep")

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
        self.var_volume.set(float(c.get("tts_volume", 0.8)))
        self.var_speed.set(float(c.get("tts_speed", 3)))
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

    def save_setup(self) -> None:
        self.config_data["opus_user_name"] = self.var_user.get().strip()
        self.config_data["opus_backend_url"] = self.var_backend.get().strip()
        self.config_data["callsign_override"] = self.var_callsign_override.get().strip()
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
        save_json(CONFIG_PATH, self.config_data)

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
        for ch in atc_phrase.CHANNELS:
            try:
                mhz = float(self.freq_vars[ch].get())
            except ValueError:
                continue
            prev = ap.get(ch) or {}
            ap[ch] = {
                "freq_mhz": mhz,
                "mod": prev.get("mod") or "AM",
                "source": prev.get("source") or "local",
            }
        self.airports[key] = ap
        save_json(AIRPORTS_PATH, self.airports)
        self.engine.config = self.config_data
        self.engine.airports = self.airports
        messagebox.showinfo("Setup", "Setup saved.")
        self._update_freq_hint()

    def _refresh_callsign(self) -> None:
        self.config_data["opus_user_name"] = self.var_user.get().strip()
        self.config_data["opus_backend_url"] = self.var_backend.get().strip()
        self.config_data["callsign_override"] = self.var_callsign_override.get().strip()

        def work() -> None:
            opus = atc_phrase.resolve_active_opus_flight(self.config_data)
            if not opus:
                self.after(0, lambda: self.var_callsign.set("(not found — set Opus signup or manual callsign)"))
                return
            filed = "filed FP" if opus.has_filed_plan else "NO flight plan"
            ov = " · manual" if atc_phrase.callsign_override(self.config_data) else ""
            label = f"{opus.radio_callsign}{ov} · seat {opus.seat} · {filed}"
            self.after(0, lambda: self.var_callsign.set(label))

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
                    lines = [
                        f"{m['channel']}: {m['name']} = {m['freq_mhz']}"
                        for m in result.get("matched") or []
                    ]
                    self.var_freq_status.set(
                        f"Pulled Opus theater {result.get('theater_id')} UHF defaults "
                        f"({len(freqs)} channels). Save setup to keep.\n" + "\n".join(lines)
                    )
                    # Mark sources as opus in memory when saving next
                    key = self.mission.get("airport") or "nellis"
                    ap = self.airports.get(key, {})
                    atc_phrase.apply_opus_freqs_to_airport(
                        ap, freqs, presets=result.get("presets") or {}
                    )
                    self.airports[key] = ap
                    messagebox.showinfo(
                        "Opus freqs",
                        f"Loaded {len(freqs)} UHF presets from Opus theater "
                        f"{result.get('theater_id')}.\nReview values, then Save setup.",
                    )

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
