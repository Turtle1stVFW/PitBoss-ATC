#!/usr/bin/env python3
"""
Simple setup / test UI for local Ground-Tower ATC (Stream Deck phase B).

- Edit Opus username, SRS/airport settings
- Pick free Windows TTS voices
- Preview phrase + optional live SRS transmit
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
AIRPORTS_PATH = HERE / "airports.json"
STREAMDECK_DIR = HERE / "streamdeck"
PHRASE_SCRIPT = HERE / "atc_phrase.py"

# Ensure sibling imports work when launched as script
sys.path.insert(0, str(HERE))
import atc_phrase  # noqa: E402


PHRASES = [
    ("ground", "taxi", "Ground: Taxi"),
    ("ground", "hold_short", "Ground: Hold short"),
    ("ground", "contact_tower", "Ground: Contact tower"),
    ("tower", "lineup", "Tower: Line up / wait"),
    ("tower", "clear_takeoff", "Tower: Clear takeoff"),
    ("tower", "clear_land", "Tower: Clear land"),
    ("tower", "go_around", "Tower: Go around"),
]


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def list_windows_voices() -> list[tuple[str, str, str]]:
    """Return [(name, gender, culture), ...] via System.Speech (free)."""
    # Prefer shared SAPI list so OneCore-unlocked voices appear after Unlock.
    names = atc_phrase.list_windows_voices()
    out: list[tuple[str, str, str]] = []
    for name in names:
        gender = atc_phrase.voice_gender(name)
        culture = atc_phrase.voice_locale(name)
        out.append((name, gender.title(), culture))
    return out or [("Microsoft Zira Desktop", "Female", "en-US")]

def preview_voice_local(voice: str, text: str) -> None:
    safe_voice = voice.replace("'", "''")
    safe_text = text.replace("'", "''")
    ps = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('{safe_voice}')
$s.Speak('{safe_text}')
"""
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", ps],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )


class AtcUi(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PitBoss ATC — Setup")
        self.geometry("720x620")
        self.minsize(640, 560)

        self.config_data = load_json(CONFIG_PATH)
        self.airports = load_json(AIRPORTS_PATH)
        self.voices = list_windows_voices()
        self.voice_by_label = {
            f"{n} ({g}, {c})": (n, g.lower()) for n, g, c in self.voices
        }

        self._build()
        self._load_fields()
        self.after(100, self.refresh_callsign)

    def _build(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.tab_setup = ttk.Frame(nb)
        self.tab_airport = ttk.Frame(nb)
        self.tab_voice = ttk.Frame(nb)
        self.tab_test = ttk.Frame(nb)
        nb.add(self.tab_setup, text="Setup")
        nb.add(self.tab_airport, text="Airport / SRS")
        nb.add(self.tab_voice, text="Voice (free)")
        nb.add(self.tab_test, text="Test / Transmit")

        # --- Setup ---
        f = self.tab_setup
        self.var_user = tk.StringVar()
        self.var_backend = tk.StringVar()
        self.var_metar = tk.StringVar()
        self.var_exe = tk.StringVar()
        self.var_airport_key = tk.StringVar()
        self.var_callsign = tk.StringVar(value="(not resolved yet)")

        r = 0
        ttk.Label(f, text="Opus username (e.g. Turtle)").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(f, textvariable=self.var_user, width=40).grid(row=r, column=1, sticky="we", padx=8, pady=4)
        r += 1
        ttk.Label(f, text="Opus backend URL").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(f, textvariable=self.var_backend, width=50).grid(row=r, column=1, sticky="we", padx=8, pady=4)
        r += 1
        ttk.Label(f, text="METAR URL template").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(f, textvariable=self.var_metar, width=50).grid(row=r, column=1, sticky="we", padx=8, pady=4)
        r += 1
        ttk.Label(f, text="ExternalAudio.exe").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        exe_row = ttk.Frame(f)
        exe_row.grid(row=r, column=1, sticky="we", padx=8, pady=4)
        ttk.Entry(exe_row, textvariable=self.var_exe, width=44).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(exe_row, text="Browse…", command=self._browse_exe).pack(side=tk.LEFT, padx=4)
        r += 1
        ttk.Label(f, text="Default airport key").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(
            f,
            textvariable=self.var_airport_key,
            values=list(self.airports.keys()),
            state="readonly",
            width=20,
        ).grid(row=r, column=1, sticky="w", padx=8, pady=4)
        r += 1
        ttk.Label(f, text="Resolved flight callsign").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        ttk.Label(f, textvariable=self.var_callsign, font=("", 11, "bold")).grid(
            row=r, column=1, sticky="w", padx=8, pady=4
        )
        r += 1
        btns = ttk.Frame(f)
        btns.grid(row=r, column=0, columnspan=2, sticky="w", padx=8, pady=10)
        ttk.Button(btns, text="Refresh callsign from Opus", command=self.refresh_callsign).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(btns, text="Open Stream Deck folder", command=self._open_streamdeck).pack(
            side=tk.LEFT, padx=4
        )
        f.columnconfigure(1, weight=1)

        # --- Airport ---
        a = self.tab_airport
        self.var_name = tk.StringVar()
        self.var_icao = tk.StringVar()
        self.var_host = tk.StringVar()
        self.var_port = tk.StringVar()
        self.var_coalition = tk.StringVar()
        self.var_gfreq = tk.StringVar()
        self.var_tfreq = tk.StringVar()
        self.var_runways = tk.StringVar()
        self.var_taxi = tk.StringVar()

        fields = [
            ("Display name", self.var_name),
            ("ICAO (Opus METAR)", self.var_icao),
            ("SRS host", self.var_host),
            ("SRS port", self.var_port),
            ("Coalition (1 red / 2 blue)", self.var_coalition),
            ("Ground freq MHz", self.var_gfreq),
            ("Tower freq MHz", self.var_tfreq),
            ("Runways (comma)", self.var_runways),
            ("Taxi via", self.var_taxi),
        ]
        for i, (label, var) in enumerate(fields):
            ttk.Label(a, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(a, textvariable=var, width=40).grid(row=i, column=1, sticky="we", padx=8, pady=4)
        a.columnconfigure(1, weight=1)
        ttk.Label(
            a,
            text="Tip: change Default airport on Setup tab, then Save — edits apply to that airport key.",
            wraplength=520,
        ).grid(row=len(fields), column=0, columnspan=2, sticky="w", padx=8, pady=8)

        # --- Voice ---
        v = self.tab_voice
        self.var_voice_label = tk.StringVar()
        self.var_volume = tk.DoubleVar(value=0.8)
        ttk.Label(v, text="Free Windows voices (installed on this PC)").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=8
        )
        ttk.Combobox(
            v,
            textvariable=self.var_voice_label,
            values=list(self.voice_by_label.keys()),
            state="readonly",
            width=50,
        ).grid(row=1, column=0, columnspan=2, sticky="we", padx=8, pady=4)
        ttk.Label(v, text="Volume (0–1)").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Scale(v, from_=0.2, to=1.0, variable=self.var_volume, orient=tk.HORIZONTAL).grid(
            row=2, column=1, sticky="we", padx=8, pady=4
        )
        ttk.Button(v, text="Preview voice on speakers", command=self._preview_voice).grid(
            row=3, column=0, sticky="w", padx=8, pady=8
        )
        ttk.Label(
            v,
            text="These are free System.Speech voices. Azure/Google neural voices need paid/cloud keys later.",
            wraplength=520,
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        v.columnconfigure(1, weight=1)

        # --- Test ---
        t = self.tab_test
        self.preview_box = tk.Text(t, height=8, wrap=tk.WORD)
        self.preview_box.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.log_box = tk.Text(t, height=8, wrap=tk.WORD, state=tk.DISABLED)
        self.log_box.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        row = ttk.Frame(t)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Phrase").pack(side=tk.LEFT)
        self.var_phrase = tk.StringVar(value=PHRASES[0][2])
        ttk.Combobox(
            row,
            textvariable=self.var_phrase,
            values=[p[2] for p in PHRASES],
            state="readonly",
            width=28,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(row, text="Build preview", command=self.build_preview).pack(side=tk.LEFT, padx=4)
        ttk.Button(row, text="Transmit to SRS", command=lambda: self.run_phrase(False)).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(row, text="Dry-run only", command=lambda: self.run_phrase(True)).pack(
            side=tk.LEFT, padx=4
        )

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(bottom, text="Save settings", command=self.save).pack(side=tk.LEFT, padx=4)
        ttk.Button(bottom, text="Quit", command=self.destroy).pack(side=tk.RIGHT, padx=4)

    def _log(self, msg: str) -> None:
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def _browse_exe(self) -> None:
        path = filedialog.askopenfilename(
            title="Select DCS-SR-ExternalAudio.exe",
            filetypes=[("Executable", "*.exe"), ("All", "*.*")],
        )
        if path:
            self.var_exe.set(path)

    def _open_streamdeck(self) -> None:
        STREAMDECK_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(STREAMDECK_DIR))  # type: ignore[attr-defined]

    def _selected_voice(self) -> tuple[str, str]:
        label = self.var_voice_label.get()
        if label in self.voice_by_label:
            return self.voice_by_label[label]
        return ("Microsoft Zira Desktop", "female")

    def _load_fields(self) -> None:
        c = self.config_data
        self.var_user.set(c.get("opus_user_name", ""))
        self.var_backend.set(c.get("opus_backend_url", ""))
        self.var_metar.set(c.get("opus_metar_url", ""))
        self.var_exe.set(c.get("external_audio_exe", ""))
        key = c.get("default_airport") or next(iter(self.airports))
        self.var_airport_key.set(key)
        self.var_volume.set(float(c.get("tts_volume", 0.8)))

        voice = c.get("tts_voice", "Microsoft Zira Desktop")
        match = next((lab for lab, (n, _) in self.voice_by_label.items() if n == voice), None)
        if match:
            self.var_voice_label.set(match)
        elif self.voice_by_label:
            self.var_voice_label.set(next(iter(self.voice_by_label)))

        self._load_airport_fields(key)

    def _load_airport_fields(self, key: str) -> None:
        ap = self.airports.get(key) or next(iter(self.airports.values()))
        self.var_name.set(ap.get("name", ""))
        self.var_icao.set(ap.get("icao", ""))
        self.var_host.set(ap.get("srs_host", ""))
        self.var_port.set(str(ap.get("srs_port", 5002)))
        self.var_coalition.set(str(ap.get("coalition", 2)))
        self.var_gfreq.set(str(ap.get("ground", {}).get("freq_mhz", "")))
        self.var_tfreq.set(str(ap.get("tower", {}).get("freq_mhz", "")))
        self.var_runways.set(",".join(ap.get("runways") or []))
        self.var_taxi.set(ap.get("taxi_via", "Alpha"))

    def _collect_to_memory(self) -> None:
        voice_name, gender = self._selected_voice()
        self.config_data.update(
            {
                "opus_user_name": self.var_user.get().strip(),
                "opus_backend_url": self.var_backend.get().strip(),
                "opus_metar_url": self.var_metar.get().strip(),
                "external_audio_exe": self.var_exe.get().strip(),
                "default_airport": self.var_airport_key.get().strip(),
                "tts_voice": voice_name,
                "tts_gender": gender,
                "tts_volume": round(float(self.var_volume.get()), 2),
            }
        )
        key = self.var_airport_key.get().strip() or "nellis"
        runways = [x.strip() for x in self.var_runways.get().split(",") if x.strip()]
        self.airports[key] = {
            "name": self.var_name.get().strip() or key.title(),
            "icao": self.var_icao.get().strip().upper(),
            "coalition": int(self.var_coalition.get() or 2),
            "srs_host": self.var_host.get().strip(),
            "srs_port": int(self.var_port.get() or 5002),
            "ground": {
                "freq_mhz": float(self.var_gfreq.get() or 275.8),
                "mod": "AM",
            },
            "tower": {
                "freq_mhz": float(self.var_tfreq.get() or 327.0),
                "mod": "AM",
            },
            "runways": runways or ["21", "03"],
            "taxi_via": self.var_taxi.get().strip() or "Alpha",
        }

    def save(self) -> None:
        try:
            self._collect_to_memory()
            save_json(CONFIG_PATH, self.config_data)
            save_json(AIRPORTS_PATH, self.airports)
            self._log("Saved config.json and airports.json")
            messagebox.showinfo("Saved", "Settings saved.")
            self.refresh_callsign()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save failed", str(exc))

    def refresh_callsign(self) -> None:
        def work() -> None:
            try:
                self._collect_to_memory()
                cs = atc_phrase.resolve_callsign_from_opus(self.config_data)
                self.after(0, lambda: self.var_callsign.set(cs or "(not found — check Opus signup)"))
                self.after(0, lambda: self._log(f"Callsign: {cs or 'not found'}"))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self._log(f"Callsign error: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _preview_voice(self) -> None:
        voice, _ = self._selected_voice()
        text = self.preview_box.get("1.0", tk.END).strip() or (
            f"This is {voice}, Nellis Ground, radio check."
        )
        preview_voice_local(voice, text)
        self._log(f"Previewing: {voice}")

    def _phrase_ids(self) -> tuple[str, str]:
        label = self.var_phrase.get()
        for role, phrase, lab in PHRASES:
            if lab == label:
                return role, phrase
        return "ground", "taxi"

    def build_preview(self) -> None:
        def work() -> None:
            try:
                self._collect_to_memory()
                key = self.config_data.get("default_airport", "nellis")
                airport = self.airports[key]
                callsign = atc_phrase.resolve_callsign_from_opus(self.config_data)
                if not callsign:
                    raise RuntimeError("No Opus callsign for this user")
                weather = atc_phrase.fetch_metar(self.config_data, airport["icao"])
                opus = atc_phrase.resolve_active_opus_flight(self.config_data)
                runway = atc_phrase.pick_departure_runway(
                    airport, weather, opus, self.config_data
                )
                role, phrase = self._phrase_ids()
                text, tx_name, freq, mod = atc_phrase.build_phrase(
                    airport, role, phrase, callsign, weather, runway
                )
                summary = (
                    f"{text}\n\n"
                    f"— callsign {callsign} | rwy {runway} | {tx_name} @ {freq} {mod}\n"
                    f"— METAR: {weather.raw or '(none)'}"
                )

                def apply() -> None:
                    self.preview_box.delete("1.0", tk.END)
                    self.preview_box.insert(tk.END, summary)
                    self.var_callsign.set(callsign)
                    self._log("Preview built")

                self.after(0, apply)
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("Preview failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def run_phrase(self, dry_run: bool) -> None:
        self._collect_to_memory()
        save_json(CONFIG_PATH, self.config_data)
        save_json(AIRPORTS_PATH, self.airports)
        role, phrase = self._phrase_ids()
        key = self.config_data.get("default_airport", "nellis")
        cmd = [
            sys.executable,
            str(PHRASE_SCRIPT),
            "--airport",
            key,
            "--role",
            role,
            "--phrase",
            phrase,
        ]
        if dry_run:
            cmd.append("--dry-run")

        def work() -> None:
            self.after(0, lambda: self._log("Running: " + " ".join(cmd)))
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(HERE))
                out = (proc.stdout or "") + (proc.stderr or "")
                self.after(0, lambda: self._log(out.strip() or f"exit {proc.returncode}"))
                if proc.returncode != 0:
                    self.after(
                        0,
                        lambda: messagebox.showerror("Transmit failed", out or f"exit {proc.returncode}"),
                    )
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("Transmit failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()


def main() -> int:
    app = AtcUi()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
