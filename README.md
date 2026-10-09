# PitBoss ATC

Patched `DCS-SR-ExternalAudio` plus the **PitBoss ATC** Python app. **Testing build** — expect rough edges.

## Share this with a new pilot

**You (Host operator)**

1. Run `atc\Build-Beta-Installer.cmd`. It writes `dist\PitBossATC-Setup-<version>.exe` with Python, numpy, faster-whisper, and the speech model already inside. Your `config.json` and keys are not in it.
2. Send that Setup exe, the ATC hostname, and the shared token. Walkthrough: [PILOT-SETUP.md](PILOT-SETUP.md).
3. Keep the Host running (`atc\Start-ATC-Host.cmd`). Your checklist: [HOST-SETUP.md](HOST-SETUP.md).

Testers are not updated on every commit. `atc\Publish-Release.cmd --publish` publishes the Setup exe. Running copies then offer that download. A draft release does not.

`atc\Pack-Share-Zip.cmd` is the source zip for someone who will install Python themselves. Open-beta testers should get the Setup exe.

**Them (pilot PC)** — [PILOT-SETUP.md](PILOT-SETUP.md) is the walkthrough. Short version:

1. Run `PitBossATC-Setup.exe`. Python is included. Leave the default folder.
2. Launch **PitBoss ATC**. First-run window: Opus username, **Client**, hostname + token, Test connection, Save.
3. Restart DCS once. Title bar: pick the Opus flight. Fly: hold SRS PTT and talk.

## Requirements

- Windows 10/11 x64
- [Python 3.10+](https://www.python.org/downloads/windows/) on every PC that runs the UI  
  Enable **Add python.exe to PATH** and **tcl/tk and IDLE**. The Microsoft Store stub is not enough.
- DCS + SRS already installed on flying PCs
- .NET desktop runtime only if you **rebuild** ExternalAudio (prebuilt exe is in this folder)

## Launchers (`atc\`)

| File | Who |
|------|-----|
| `Setup-Pilot.cmd` | First time on a PC (deps + radio export + open the app) |
| `Open-Flight-Flow.cmd` | Everyday launch |
| `Start-ATC-Host.cmd` | Dedicated-server Host box |
| `Pack-Share-Zip.cmd` | Host operator: zip a tester copy with secrets stripped |
| `Build-Beta-Installer.cmd` | Host operator: build `PitBossATC-Setup.exe` (Python + voice bundled) |
| `Publish-Release.cmd` | Host operator: upload that exe as a GitHub release (`--publish` offers it to testers) |
| `Allow-ATC-Host-Firewall.cmd` | Host: inbound TCP 8766 (Run as administrator) |

5. Optional Google Neural2 voices: see [docs/ATC.md](docs/ATC.md). Put your service-account JSON in `atc\secrets\` (gitignored).
First launch copies `config.example.json` → `config.json` if needed. Do not commit or zip `config.json`.

## Layout

| Path | Purpose |
|------|---------|
| `DCS-SR-ExternalAudio.exe` + DLLs | Slim Windows build with `--ip` / hostname support |
| `runtimes\win-x64\` | Native Speech / gRPC libs |
| `atc\` | Flow planner, phrase board, Stream Deck scripts |
| `atc\Install-DCS-Radio-Export.cmd` | Auto-install DCS Export hook for the freq gate (installer-safe) |
| `docs\` | Maintainer architecture / flow overview ([docs/README.md](docs/README.md)) |
| `patches\` | Source files + rebuild instructions for the `--ip` patch |

## Rebuild ExternalAudio

See [docs/PATCHES.md](docs/PATCHES.md). Upstream project: [ciribob/DCS-SimpleRadioStandalone](https://github.com/ciribob/DCS-SimpleRadioStandalone) (GPL-3.0).
| `DCS-SR-ExternalAudio.exe` | Slim Windows build with `--ip` / hostname support |
| `atc\` | Flow planner, Fly tab, voice, Host/Client |
| `atc\dcs\` | DCS radio-export hook (frequency gate) |
| `tools\` | Zone editor / map-jet tester |
| `patches\` | Source + rebuild notes for the `--ip` patch |

In-app **Help** covers voices, Plan Flight, and troubleshooting. The long reference is [atc/README.md](atc/README.md).

## Rebuild ExternalAudio

See [patches/README.md](patches/README.md). Upstream: [ciribob/DCS-SimpleRadioStandalone](https://github.com/ciribob/DCS-SimpleRadioStandalone) (GPL-3.0).

## License

- `DCS-SR-ExternalAudio` binaries and `patches\` are derived from Ciribob’s SRS (GPL-3.0). See [LICENSE](LICENSE).
- The `atc\` Python tools are for squadron use; keep GPL obligations if you redistribute the ExternalAudio binaries.
