# New pilot setup (testing)

This is a testing build of **PitBoss ATC**. You already have DCS, SRS, and an Opus / CAOC login. You do **not** need the Google TTS key — that stays on the Host.

Ask the person running ATC for two things only:

1. **ATC address** — usually the same hostname you already type for SRS (example: `showtime.455aew.com`)
2. **Shared token** — a short string. Not a JSON file.

## 1. Install Python (once per PC)

Download [Python 3.10+](https://www.python.org/downloads/windows/) (not the Microsoft Store stub).

In the installer, check:

- **Add python.exe to PATH**
- **tcl/tk and IDLE**

## 2. Get the folder onto this PC

Unzip the share pack (or clone the repo) anywhere. Do not copy someone else’s `atc\config.json` or `atc\secrets\` folder.

## 3. Run first-time setup

Double-click:

```
atc\Setup-Pilot.cmd
```

That finds Python, creates `config.json` if needed, installs voice recognition, installs the DCS radio-export hook, and opens the app.

The first launch shows a **First-run setup** window. Fill in:

| Field | What to put |
|--------|-------------|
| Opus username | Your CAOC / Opus name (same as the flight roster) |
| This PC is | **Client (pilot)** for a squadron hop |
| ATC address | The hostname they gave you (not `127.0.0.1`) |
| ATC port | `8766` (this is **not** SRS port 5002) |
| Shared token | Paste exactly |
| Voice control | Leave on |

Click **Test connection**, then **Save and continue**.

If the first-run window is gone: **Help → First-run setup…**

## 4. Before you fly

1. Title bar — confirm the username, then click the green flight chip and pick **your** Opus flight / seat.
2. Setup → Airport — SRS host should match the server you already use. Clients do not transmit ATC; the Host does.
3. DCS must be restarted once after the radio-export install (Setup-Pilot does the install; restart is on you).
4. Fly tab — hold your normal **SRS PTT** and talk. The Fly card lists what you can say right now.
5. Setup → Preferences → **Simplified UI** trims the Fly tab to the frequency, whether you are on it, what to say, and what ATC will answer. Turn it off again if you need the diagnostic lines while troubleshooting.

## What you do *not* do

- Do not pick **Host** on a flying PC.
- Do not install or copy `atc\secrets\google-tts.json`.
- Do not edit `Export.lua` by hand. Use Setup-Pilot or Setup → Controls → **Install DCS radio export…**
- Do not give anyone your `config.json` — it can hold tokens and local paths.

## Solo try-out (no squadron Host)

Setup → Squadron → **Solo**. This PC will speak on SRS itself (needs `DCS-SR-ExternalAudio.exe` in the folder above `atc\`). Windows voices work with no API key.

## If something fails

| Symptom | Check |
|---------|--------|
| Setup-Pilot says no Python | Reinstall from python.org with PATH + tcl/tk |
| Test connection fails | Host app is running; token matches; port **8766**; same address you use for DCS/SRS |
| Voice says install numpy / faster-whisper | Run `Setup-Pilot.cmd` again (needs internet once) |
| Fly says radio tune unknown | Restart DCS after the radio-export install; sit in the jet |
| ATC never answers | Tuned to the step frequency; PTT is the same one SRS uses; open with the agency (`Ground, Fleece 1, taxi`) |
| Callsign blank | Opus username must match the roster, then pick the flight chip |

In-app **Help** has the longer how-tos. The Host operator can walk you through the first-run window if anything is unclear.
