# Packaging notes

**Open beta:** `atc\Build-Beta-Installer.cmd` writes
`dist\PitBossATC-Setup-<version>.exe`. The setup exe installs a private
CPython under `atc\runtime\`, with numpy, faster-whisper, and the `base.en`
weights already in place. Testers do not install Python or run pip.
`Setup-Pilot.cmd` and `Pack-Share-Zip.cmd` remain the from-source path.

The build machine needs network once (CPython, wheels, the speech model, and
the Inno Setup compiler). The compiler is downloaded into `dist\cache\` from
the NuGet `Tools.InnoSetup` package. Output and cache are gitignored.

## Voice control dependencies (required by default)

Ship voice capture/recognition with the app — do **not** leave these as optional
post-install `pip` steps for end users. Without them, Setup → Microphone shows
misleading “no input devices” / voice fails silently.

| Package | Why |
|---------|-----|
| `numpy` | Mic ring buffer + PCM → float for Whisper |
| `faster-whisper` | On-device speech recognition (pulls ctranslate2, etc.) |

`Build-Beta-Installer.cmd` uses a bundled runtime: a private Python under
`atc\runtime` with these packages installed, and launchers prefer
`atc\runtime\python.exe` over any system Python.

Dev / CI can install from `atc/requirements-voice.txt`.

### Whisper model weights

The beta installer copies `base.en` (~150 MB) to `atc\runtime\models\base.en\`
as real files (not Hugging Face symlinks). The app loads that folder when it
is present. A from-source checkout still downloads `base.en` on first voice
enable into `%USERPROFILE%\.cache\huggingface\`. Users do **not** need an
`HF_TOKEN`, and Windows symlink warnings are harmless (the app suppresses them).

To warm a source checkout so flight night is offline-safe:

```bat
py -3 -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8')"
```

Or copy a prebuilt Hub cache into the user's profile / a private
`HF_HOME` next to the app and set `HF_HOME` in the launcher.

---

## DCS radio export

The frequency gate needs `ATC-RadioExport.lua` in each DCS Saved Games profile
and a small hook in `Export.lua`. Do **not** ask users to edit those files.

## Post-install custom action

The beta setup already does this (`Setup-Installed.cmd` → `setup_pilot.py`).
For a hand-rolled installer, after files are laid down, run (elevated only if
your installer already elevates; Saved Games is per-user and does not need admin):

```bat
Install-DCS-Radio-Export.cmd
```

From the installed `atc` folder, or:

```bat
py -3 install_dcs_radio_export.py --source "{app}\atc\dcs\ATC-RadioExport.lua"
```

Exit codes:

| Code | Meaning |
|------|---------|
| 0 | Install / uninstall succeeded (or `--status` all ready) |
| 1 | Failure |
| 2 | `--status` and at least one profile is not ready |

---

## Zone editor

**Setup → Draw zones on a map…** and `atc\Open-Zone-Editor.cmd` both run
`tools\zone_server.py`, so `tools\` has to land beside `atc\` — same parent
folder, since the app looks for it at `..\tools\zone_server.py`. Include
`tools\vendor\` with it: Leaflet is served locally so the editor works without
internet beyond the map tiles.

It runs on `127.0.0.1:8777`. If an installer or corporate policy blocks the app
from binding a loopback port, that button is what breaks; `zone_editor_port` in
`config.json` moves it.

## Inno Setup

`PitBossATC.iss` is compiled by `build_beta.py` (per-user folder
`%LOCALAPPDATA%\PitBoss ATC`, no administrator). After files are copied it
runs `Setup-Installed.cmd`, which creates `config.json` if needed and installs
the DCS radio-export hook. Uninstall removes that hook.

The radio-export script is idempotent: re-running upgrades the Lua file and
will not duplicate the Export.lua hook or modify the SRS `pcall` line.
