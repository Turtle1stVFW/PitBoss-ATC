# Host setup (one ATC box)

One machine runs **PitBoss ATC** as Host and speaks ATC on SRS for the whole hop. A Windows DCS dedicated server is fine. Flying PCs are **Clients** — they never get the Google JSON.

## On the Host PC

1. Install [Python 3.10+](https://www.python.org/downloads/windows/) with **Add python.exe to PATH** and **tcl/tk and IDLE**.
2. Put this repo on the box (or unzip the share pack).
3. Double-click `atc\Setup-Pilot.cmd` once (same bootstrap as pilots: Python check, voice packages if you want local listen, radio export is optional on a dedicated server that is not in a jet).
4. Or just `atc\Start-ATC-Host.cmd`.
5. Setup → Squadron → **Host**. Click **Generate** for a token if the box is empty. **Save setup**.
6. Right-click `atc\Allow-ATC-Host-Firewall.cmd` → Run as administrator (inbound TCP **8766**).
7. On the DCS server’s router, forward **TCP 8766** to this box the same way SRS **5002** already is.
8. Setup → Airport — SRS host / port / coalition must be the live squadron SRS (this PC is the one that transmits).
9. Optional Google voices: Setup → Identity & TTS → Google Cloud TTS. Browse the service-account JSON. The app copies it into `atc\secrets\` (gitignored). **Never** send that file to pilots.

Give each tester:

- The folder (or the zip from `atc\Pack-Share-Zip.cmd`)
- [PILOT-SETUP.md](PILOT-SETUP.md)
- ATC address (the hostname they already use for SRS, not `192.168.50.20` unless they can already ping it)
- The shared token
- ATC port `8766`

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
