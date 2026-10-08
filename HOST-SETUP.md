# Host setup (one ATC box)

One machine runs **PitBoss ATC** as Host and speaks ATC on SRS for the whole hop. A Windows DCS dedicated server is fine. Flying PCs are **Clients** — they never get the Google JSON.

## On the Host PC

The same `PitBossATC-Setup.exe` you send to pilots also works on the Host box (Python, numpy, and Whisper are inside it). A from-source checkout still needs [Python 3.10+](https://www.python.org/downloads/windows/) with **Add python.exe to PATH** and **tcl/tk and IDLE**.

1. Install with the Setup exe, or put this repo on the box.
2. Launch **PitBoss ATC (Host)** from the Start menu, or double-click `atc\Start-ATC-Host.cmd`.
3. First time from a source checkout only: `atc\Setup-Pilot.cmd` (voice packages and the radio-export hook). A dedicated server that is not in a jet can skip the radio export.
4. Setup → Basics → **Host**. Click **Generate** for a token if the box is empty. **Save setup**.
5. Right-click `atc\Allow-ATC-Host-Firewall.cmd` → Run as administrator (inbound TCP **8766**).
6. On the DCS server’s router, forward **TCP 8766** to this box the same way SRS **5002** already is.
7. Setup → Airbases — SRS host / port must be the live squadron SRS (this PC is the one that transmits). Sync freqs from Opus.
8. Optional Google voices: Setup → Basics → Google Cloud TTS. Browse the service-account JSON. The app copies it into `atc\secrets\` (gitignored). **Never** send that file to pilots.

Give each tester:

- `dist\PitBossATC-Setup-<version>.exe` from `atc\Build-Beta-Installer.cmd`
- ATC address (the hostname they already use for SRS, not `192.168.50.20` unless they can already ping it)
- The shared token
- ATC port `8766`

[PILOT-SETUP.md](PILOT-SETUP.md) is the walkthrough if they get stuck. A source zip from `atc\Pack-Share-Zip.cmd` is only for someone who will install Python themselves.

Do **not** give them `atc\config.json`, `atc\secrets\`, or `tts_usage.json`.

## Traffic tab

After the first Client connects, **Traffic** lists who is on and the per-frequency TX queue. Same Opus flight = one timeline.

## Host vs Solo

| | Host | Solo |
|--|------|------|
| Who transmits ATC | This box | The flying PC |
| Google JSON | This box only | This box only |
| Pilots run | Client + token | The full app on their PC |

Until the Host is up, a tester can stay on **Solo** and still rehearse.

## Standalone installer (later)

`atc\Setup-Pilot.cmd` is the current “do the mechanical bits” step. A real installer would bundle Python, voice packages, the Whisper model, ExternalAudio, and the DCS export hook. Notes: [atc/installer/README.md](atc/installer/README.md).
