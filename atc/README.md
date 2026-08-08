# 455 Mission Flow Planner

## Open the app
Double-click either:
- `Open-ATC-Setup.cmd`
- `Open-Flight-Flow.cmd`

Both open the same polished UI.

## Tabs
1. **Plan Flight** — build the full sortie timeline (TTS template, custom text, or MP3/OGG). No JSON editing.
2. **Fly** — big Next / Back / Reset / Flip for mid-flight.
3. **Setup** — Opus username, voice, airport freqs, SRS host, TTS provider, optional runway override.
4. **Help** — in-app how-tos (Getting started, Google JSON setup, Plan Flight tips, troubleshooting). Also the top-right **Help** button.

## Typical workflow
1. Setup → set Opus user (e.g. Turtle) → Refresh callsign → Save setup  
2. Plan Flight → add/reorder steps → Preview → Save mission  
3. Fly → Play Next through the full timeline (seek/jump to skip)  

Stream Deck / kneeboard PDF can wait until the plan feels right.

---

## Voice options (pick one in Setup)

| Choice | API key? | Sound quality |
|--------|----------|----------------|
| **Windows voices** (default) | **No** — works out of the box | Robotic SAPI (David/Zira, etc.) |
| **Google Cloud TTS** (optional) | Yes — your own JSON credentials | Neural2 / WaveNet (much more natural) |

Most users can stay on **Windows voices** forever. Google is only if someone wants better audio and is willing to create a free-tier Google Cloud key.

---

## Google Cloud TTS (BYOK) — optional

Only needed if you select Google in Setup. Each user brings their **own** credentials. Nothing is shared on the squadron server.
Server ATIS is unchanged (still server-side).

### 1. Create a Google Cloud project + enable TTS
1. Open [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or pick an existing one)
3. Enable billing (required even for the free monthly character quota)
4. APIs & Services → Library → enable **Cloud Text-to-Speech API**

### 2. Create a service account key
1. APIs & Services → Credentials → **Create credentials** → **Service account**
2. Name it e.g. `srs-atc-tts` → Create → Skip optional roles (TTS needs no special IAM role for the API call itself)
3. Open the service account → Keys → Add key → **Create new key** → **JSON**
4. Save the downloaded file into:
   `SRS-ExternalAudio-Remote\atc\secrets\google-tts.json`  
   (or any private path you prefer)

### 3. Wire it in Setup
1. Open the Flow app → **Setup**
2. Under **Text-to-speech**, choose **Google Cloud TTS (BYOK)**
3. **Browse…** to your JSON file
4. Pick Neural2 voices per agency (or type any valid Google voice id)
5. **Save setup**

### 4. Test
- Use **Hear locally** on Plan Flight to audition a step on speakers (same Google voice, no SRS).
- On Setup → Voices, open a voice picker and click **Preview** for a short sample.
- Use **TX → SRS** / Fly when you want to hear it on the radio frequency.

### Free tier notes
- Google gives a monthly free character allowance for WaveNet / Neural2 (see current Google TTS pricing).
- Phrase-board usage is tiny; you will rarely leave the free tier.
- If credentials are missing/invalid, transmits fail with a clear error instead of falling back silently.

### Config keys (`config.json`)
| Key | Example | Meaning |
|-----|---------|---------|
| `tts_provider` | `"google"` or `"windows"` | Which engine ExternalAudio uses |
| `google_credentials` | `C:\\Users\\you\\...\\google-tts.json` | Path to service-account JSON |
| `tts_voices` | `"en-US-Neural2-D"` etc. | Per-agency Google voice ids |

Voice catalog: https://cloud.google.com/text-to-speech/docs/voices
