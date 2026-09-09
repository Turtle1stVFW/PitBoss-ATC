#!/usr/bin/env python3
"""First-run setup window for testers. Opened from flow_ui when identity is empty."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

import atc_net

C_PANEL = "#151c27"
C_TEXT = "#e8eef7"
C_MUTED = "#9aabc4"
C_LABEL = "#c8d4e6"
C_ACCENT = "#4c9afe"
C_GREEN = "#3dd68c"


def should_show(config: dict[str, Any] | None) -> bool:
    """True for a fresh tester copy. Existing identities are left alone."""
    cfg = config or {}
    if cfg.get("setup_complete"):
        return False
    return not str(cfg.get("opus_user_name") or "").strip()


def show(app: Any, *, force: bool = False) -> None:
    """
    Modal first-run sheet.

    `app` is the MissionPlanner instance (vars + save_setup + radio export).
    """
    cfg = getattr(app, "config_data", None) or {}
    if not force and not should_show(cfg):
        return

    win = tk.Toplevel(app)
    win.title("First-run setup  ·  testing")
    win.configure(bg=C_PANEL)
    win.transient(app)
    win.grab_set()
    win.geometry("640x620")
    win.minsize(560, 540)

    pad = tk.Frame(win, bg=C_PANEL)
    pad.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)

    ttk.Label(pad, text="455 ATC Flow — testing", style="Header.TLabel").pack(anchor="w")
    tk.Label(
        pad,
        text=(
            "One-time setup for this PC. Ask the Host operator for the ATC "
            "address and shared token. Do not copy anyone's Google JSON key."
        ),
        bg=C_PANEL,
        fg=C_MUTED,
        font=("Segoe UI", 9),
        wraplength=590,
        justify="left",
    ).pack(anchor="w", pady=(4, 14))

    user_var = tk.StringVar(value=str(cfg.get("opus_user_name") or ""))
    role_var = tk.StringVar(value=atc_net.role_of(cfg) or "client")
    if role_var.get() == "solo" and not str(cfg.get("opus_user_name") or "").strip():
        # Fresh example config is Solo; testers joining a hop want Client.
        role_var.set("client")
    host_var = tk.StringVar(value=str(cfg.get("atc_host") or ""))
    if host_var.get() in {"", "127.0.0.1", "localhost"}:
        host_var.set("")
    port_var = tk.StringVar(value=str(cfg.get("atc_port") or atc_net.DEFAULT_ATC_PORT))
    token_var = tk.StringVar(value=str(cfg.get("atc_token") or ""))
    voice_var = tk.BooleanVar(value=True)
    status_var = tk.StringVar(value="")

    form = tk.Frame(pad, bg=C_PANEL)
    form.pack(fill=tk.X)

    def row(r: int, label: str, widget: tk.Misc) -> None:
        tk.Label(form, text=label, bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).grid(
            row=r, column=0, sticky="w", pady=4
        )
        widget.grid(row=r, column=1, sticky="we", pady=4, padx=(10, 0))

    form.columnconfigure(1, weight=1)
    row(0, "Opus username", ttk.Entry(form, textvariable=user_var, width=28))

    roles = tk.Frame(form, bg=C_PANEL)
    for text, value in (
        ("Client (pilot → Host)", "client"),
        ("Solo (this PC speaks ATC)", "solo"),
        ("Host (ATC box only)", "host"),
    ):
        ttk.Radiobutton(
            roles,
            text=text,
            variable=role_var,
            value=value,
            command=lambda: _toggle_client(client_box, role_var),  # slot stays put
        ).pack(anchor="w")
    row(1, "This PC is", roles)

    client_slot = tk.Frame(pad, bg=C_PANEL)
    client_slot.pack(fill=tk.X, pady=(12, 8))
    client_box = tk.Frame(client_slot, bg=C_PANEL, highlightbackground="#2a3648", highlightthickness=1)
    inner = tk.Frame(client_box, bg=C_PANEL)
    inner.pack(fill=tk.X, padx=10, pady=8)
    tk.Label(
        inner,
        text="Squadron Host  (Clients only)",
        bg=C_PANEL,
        fg=C_GREEN,
        font=("Segoe UI Semibold", 10),
    ).pack(anchor="w")
    cf = tk.Frame(inner, bg=C_PANEL)
    cf.pack(fill=tk.X, pady=(6, 0))
    cf.columnconfigure(1, weight=1)
    tk.Label(cf, text="ATC address", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).grid(
        row=0, column=0, sticky="w", pady=3
    )
    ttk.Entry(cf, textvariable=host_var, width=32).grid(row=0, column=1, sticky="we", padx=(8, 0))
    tk.Label(cf, text="ATC port", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).grid(
        row=1, column=0, sticky="w", pady=3
    )
    ttk.Entry(cf, textvariable=port_var, width=8).grid(row=1, column=1, sticky="w", padx=(8, 0))
    tk.Label(cf, text="Shared token", bg=C_PANEL, fg=C_LABEL, font=("Segoe UI", 10)).grid(
        row=2, column=0, sticky="w", pady=3
    )
    ttk.Entry(cf, textvariable=token_var, width=32).grid(row=2, column=1, sticky="we", padx=(8, 0))
    tk.Label(
        inner,
        text="Same hostname you already use for SRS. Port 8766 — not SRS 5002.",
        bg=C_PANEL,
        fg=C_MUTED,
        font=("Segoe UI", 8),
    ).pack(anchor="w", pady=(4, 0))

    acts = tk.Frame(pad, bg=C_PANEL)
    acts.pack(fill=tk.X, pady=(4, 8))
    ttk.Checkbutton(acts, text="Enable voice control (hold SRS PTT and talk)", variable=voice_var).pack(
        anchor="w"
    )

    def do_test() -> None:
        _push_vars(app, user_var, role_var, host_var, port_var, token_var, voice_var)
        if hasattr(app, "_test_atc_host_connection"):
            app._test_atc_host_connection()
        status_var.set(getattr(app, "var_atc_net_status", tk.StringVar()).get())

    def do_export() -> None:
        if hasattr(app, "_install_dcs_radio_export"):
            app._install_dcs_radio_export()

    def do_voice_pkgs() -> None:
        import setup_pilot

        messagebox.showinfo(
            "Voice packages",
            "A console-style pip install may take a minute (internet once).",
            parent=win,
        )
        ok, msg = setup_pilot.install_voice_packages(warm_model=False)
        if ok:
            messagebox.showinfo("Voice packages", msg, parent=win)
        else:
            messagebox.showerror("Voice packages", msg, parent=win)

    btns = tk.Frame(pad, bg=C_PANEL)
    btns.pack(fill=tk.X, pady=(4, 8))
    ttk.Button(btns, text="Test connection", command=do_test).pack(side=tk.LEFT)
    ttk.Button(btns, text="Install DCS radio export…", command=do_export).pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(btns, text="Install voice packages…", command=do_voice_pkgs).pack(side=tk.LEFT, padx=(8, 0))

    tk.Label(
        pad,
        textvariable=status_var,
        bg=C_PANEL,
        fg=C_ACCENT,
        font=("Segoe UI", 9),
        wraplength=590,
        justify="left",
        anchor="w",
    ).pack(fill=tk.X, pady=(0, 8))

    def save_and_close() -> None:
        name = user_var.get().strip()
        if not name:
            messagebox.showerror("First-run setup", "Opus username is required.", parent=win)
            return
        role = role_var.get().strip().lower() or "client"
        if role == "client" and not token_var.get().strip():
            messagebox.showerror(
                "First-run setup",
                "Client needs the Host's shared token.",
                parent=win,
            )
            return
        if role == "client" and not host_var.get().strip():
            messagebox.showerror(
                "First-run setup",
                "Client needs the ATC address (the hostname you already use for SRS).",
                parent=win,
            )
            return
        _push_vars(app, user_var, role_var, host_var, port_var, token_var, voice_var)
        app.config_data["setup_complete"] = True
        if hasattr(app, "save_setup"):
            app.save_setup()
        win.destroy()

    def skip() -> None:
        win.destroy()

    foot = tk.Frame(pad, bg=C_PANEL)
    foot.pack(fill=tk.X, pady=(8, 0))
    ttk.Button(foot, text="Save and continue", style="Accent.TButton", command=save_and_close).pack(
        side=tk.LEFT
    )
    ttk.Button(foot, text="Skip for now", command=skip).pack(side=tk.LEFT, padx=(8, 0))
    tk.Label(
        foot,
        text="Help → First-run setup… opens this again.",
        bg=C_PANEL,
        fg=C_MUTED,
        font=("Segoe UI", 8),
    ).pack(side=tk.RIGHT)

    _toggle_client(client_box, role_var)
    win.wait_window()


def _toggle_client(box: tk.Misc, role_var: tk.StringVar) -> None:
    if role_var.get() == "client":
        box.pack(fill=tk.X)
    else:
        box.pack_forget()


def _push_vars(
    app: Any,
    user_var: tk.StringVar,
    role_var: tk.StringVar,
    host_var: tk.StringVar,
    port_var: tk.StringVar,
    token_var: tk.StringVar,
    voice_var: tk.BooleanVar,
) -> None:
    if hasattr(app, "var_user"):
        app.var_user.set(user_var.get().strip())
    if hasattr(app, "var_atc_role"):
        app.var_atc_role.set(role_var.get().strip().lower() or "client")
        app.var_atc_host.set(host_var.get().strip() or "127.0.0.1")
        app.var_atc_port.set(port_var.get().strip() or str(atc_net.DEFAULT_ATC_PORT))
        app.var_atc_token.set(token_var.get().strip())
    if hasattr(app, "var_voice_enabled"):
        app.var_voice_enabled.set(bool(voice_var.get()))
    app.config_data["opus_user_name"] = user_var.get().strip()
    app.config_data["atc_role"] = role_var.get().strip().lower() or "client"
    app.config_data["atc_host"] = host_var.get().strip() or "127.0.0.1"
    try:
        app.config_data["atc_port"] = int(port_var.get() or atc_net.DEFAULT_ATC_PORT)
    except ValueError:
        app.config_data["atc_port"] = atc_net.DEFAULT_ATC_PORT
    app.config_data["atc_token"] = token_var.get().strip()
    app.config_data["voice_enabled"] = bool(voice_var.get())
