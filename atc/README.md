# 455 Mission Flow Planner

## Open the app
Double-click either:
- `Open-ATC-Setup.cmd`
- `Open-Flight-Flow.cmd`

Both open the same polished UI.

## Tabs
1. **Plan Flight** — build the full sortie timeline (TTS template, custom text, or MP3/OGG). No JSON editing.
2. **Fly** — big Next / Back / Reset / Flip for mid-flight.
3. **Setup** — Opus username, voice, airport freqs, SRS host, TTS provider, optional runway override, and **Controls** (HOTAS / hotkeys / voice).
4. **Help** — in-app how-tos (Getting started, Google JSON setup, Plan Flight tips, troubleshooting). Also the top-right **Help** button.

## Typical workflow
1. Setup → Choose flight… from Opus → confirm callsign/FP → Save setup  
   (Opus username is optional — used to auto-pick your seat on that flight.)
2. Plan Flight → add/reorder steps → Preview → Save mission  
3. Fly → Play Next through the full timeline (seek/jump to skip)  

Stream Deck / kneeboard PDF can wait until the plan feels right.

---

## Advancing the flow in-game (Setup → Controls)

DCS and SRS run as administrator (SRS ships `RequireAdmin=true`). Windows UIPI will not
deliver a keyboard hotkey to a normal-privilege app while an elevated window has focus,
so **every hotkey goes dead the moment you click into DCS** — the same reason Stream
Deck keystrokes get swallowed. Changing the combo does not help; the block is about
privilege, not which keys you picked. Three ways around it:

| Option | Needs admin? | Notes |
|--------|--------------|-------|
| **HOTAS button** | No | Reads the stick directly, unaffected by focus. Setup → Controls → **Learn…** |
| **Keyboard hotkey** | No | Also polled, so it survives DCS focus. Does not swallow the key, so pick a combo DCS ignores |
| **Local URL** | No | `http://127.0.0.1:8765/next` and `/back` — a Stream Deck *Website* action, no keystroke involved |
| **Restart as administrator** | Yes | Belt and braces: makes the registered hotkey work as well |

Use whichever you prefer, or all of them at once — a press is only acted on once no
matter how many routes it arrives by.

**Show SRS PTT buttons…** lists the HOTAS buttons SRS already transmits on, so you can
pick something else for Next/Back. The **Last trigger** line confirms a press actually
reached the app.

### Keyboard hotkeys

Defaults are F13/F14 because Stream Deck can *send* those even though no keyboard has
them, and they never collide with a DCS binding. On a plain keyboard pick anything free
— **Capture…** takes whatever you press, e.g. `Ctrl+Alt+Shift+N`. The Controls tab shows
what actually registered, and says so when another app already owns the combo.

Keys are handled two ways at once. `RegisterHotKey` swallows the keystroke so it never
reaches the game, but Windows will not deliver it while DCS has focus. Polling the key
state does survive that — the same reason reading the stick works — at the cost of the
key also reaching DCS. Running both means the hotkey works everywhere, and the duplicate
is discarded.

Other endpoints on the same local server: `/reset`, `/flip`, `/seek?n=7`, `/status`.

---

## Voice control (Setup → Controls → Voice)

Hold your normal SRS PTT, make the call, and ATC answers. Speech recognition runs
locally with `faster-whisper` on the CPU — roughly 350 ms for a typical radio call with
`base.en`, and it leaves the GPU entirely to DCS. Nothing is sent anywhere.

One-time install (everything else in the app works without it):

```
pip install faster-whisper numpy
```

The microphone defaults to whatever SRS is set to, and the PTT defaults to SRS's own
transmit buttons, both auto-detected from the SRS client config. If you transmit on a
key rather than a HOTAS button, set **or PTT key** instead — hold it exactly the same
way. Either works, and you can set both.

**It shares with SRS rather than taking over.** The stick is only ever *read* — no device
handle, no exclusive grab — so the same button still keys SRS while it starts a listen
here; only buttons you actually bind get polled, at about 0.005% of one core. The mic is
opened shared, which is the only mode `waveIn` has, so SRS records the same audio at the
same time. Verified by running two capture clients on one microphone and one stick at
once. The single way to break this is a mic with "Allow applications to take exclusive
control" enabled *and* another app holding it — that shows up as an open failure on the
Controls tab, not as silence.

### You don't have to get the words right

Nothing here is a fixed phrase. Each call is a couple of keywords in any order, with a
one-typo-per-word allowance on top, so both the way you happen to phrase it and the way
Whisper happens to mishear it still land. All of these fire the taxi step:

```
Nellis Ground, Fleece 1, ready to taxi
Nellis Ground, Fleece 1, we're ready for taxi
Nellis Ground, Fleece 1, ready to taxy        <- Whisper misheard it
Ground, Fleece 1, request taxi
```

Being loose about wording is safe because wording is not what decides whether to act —
the checks in the next section are. Getting a word wrong costs you nothing; talking to
your wingman is what stays silent.

### The call that's due can be shortened

For the one call the flow is actually waiting on, you can drop the polite half and just
name the thing. All of these work when taxi is the next step:

```
Nellis Ground, Fleece 1, taxi
Ground, Fleece 1, taxi please
Nellis Tower, Fleece 1, ready          <- when takeoff is the next step
```

This shortcut is deliberately narrow: it applies only to the step that is due, so
"Ground, Fleece 1, taxi" does nothing while the flow is waiting on your takeoff call —
say the full "ready for departure" instead. Filler on its own is never enough either
("Ground, Fleece 1, request" is ignored), and none of it gets past the addressing rules
below: a shorthand call to your wingman stays silent like any other.

### What can I say right now?

The Fly tab shows a **You can say** card with the handful of calls that make sense at
this point in the flight, written out in full — agency, callsign, request. The call ATC
is waiting for is listed first. Read a line off the card and it will always work, and so
will anything close to it. The card follows the flow cursor, and hides when voice control
is off.

### Readbacks (after ATC talks)

Clearances — IFR, taxi, takeoff, line up, landing — open a readback window. While one is
open the app is waiting on you, so it listens much more loosely: you do **not** need to
say “Delivery” again. Fly shows a **READ BACK** strip with the items that matter
(destination, via, climb, expect altitude, and the **squawk** highlighted large).

For the IFR clearance, the hinge is the Mode 3 code — say **squawk XXXX** or
**squawking XXXX** (spoken digits are fine) and it advances to “readback correct”. You
can bury that in a long readback; you do **not** need to say “in sequence”. A plain
“roger” / “copy” / “wilco” still works as a short ack. Saying “squawk” with no code, or
the wrong code, does nothing.

Taxi / takeoff / landing readbacks have no scripted reply, so your ack simply closes the
window and the strip clears. Outside a readback window a bare “roger” means nothing and
is ignored. Flight chatter on the same PTT stays silent throughout.

Calls it understands:

| Say | Result |
|-----|--------|
| "Ground, ready to taxi" / "request clearance" / "ready for departure" | Fires the matching flow step |
| "Request runway two one left" | Sets the runway and reads back the approval |
| "Say winds" / "say altimeter" | Live METAR answer |
| "Blackjack, request picture" | Group picture from the live CAOC radar feed |
| "Alpha check" | Bullseye position for your aircraft |
| "We'll take the rolling" / "unable rolling" | Accepts or declines the rolling departure |
| "Gear down full stop" / "going around" / "clear of the runway" | Fires the matching step |
| "Say again" | Replays the last transmission |

When a call fires a step, the Fly card moves to the next step on its own — same as if you
had pressed **Play and advance**. Answers that are not steps (winds, altimeter, picture,
alpha check) transmit without moving the cursor.

### Teaching a step your own wording

The table above is the built-in grammar. To add wording of your own, open **Plan**, pick a
step, and fill in **Voice phrases** — one phrase per line. Any single line fires that step:

```
lets get this show going
spin em up
we're rolling
```

Points worth knowing:

- Phrases are stored on the step in the flow file (`voice_phrases`), so they travel with
  the mission and are per-step, not global.
- They stack on top of the built-in calls rather than replacing them; "ready to taxi"
  keeps working either way.
- Your phrase is offered first on the **You can say** card, so you can see what you wrote.
- Near-misses still count, so you do not need to enumerate every variation.
- A phrase you wrote outranks the casual-speech filter — "let's roll" works as a trigger
  even though "let's" normally reads as crew talk. Addressing rules still apply: it has to
  be aimed at ATC, and calls to your wingman stay silent. Tactical brevity ("fox two",
  "go button five") is always ignored, whatever you type here.
- Clear the box to go back to the built-ins only.

### Knowing when *not* to listen

The flight shares this PTT, so most of what it hears is not ATC business. Matching
keywords alone would taxi the jet every time someone said "ready". Four things have to
line up before a transmission does anything:

| Check | Effect |
|-------|--------|
| **Who you called** | Open with the agency ("Nellis Tower, …") or at least your own callsign. Anything else is ignored. Turn off **Only act on calls addressed to ATC** to relax this. |
| **Who you *didn't* call** | "Two, …", "Dash three, …", "Lead, …" is flight business and never fires — even if the rest sounds like a request. |
| **Flight chatter** | "go button five", "fence in", "tally", "fox two", "bingo", "knock it off" and friends hard-stop the match. Ambiguous ones ("visual", "blind") only count against you when no agency was addressed. |
| **Where you are** | "Gear down full stop" while still at clearance delivery scores too low to fire. The call ATC is currently waiting for gets a boost, so the expected reply is the easiest thing to say. |

It also knows which jet is yours: if you are Fleece 2, then "Fleece 2" is you and
"Fleece 1" is someone you are talking to.

Thinking out loud is not a request either — "should we ask tower for the rolling?" is
recognised as crew talk and stays silent.

Every transmission is logged with the verdict, and anything ignored for a reason you can
fix says what to change:

```
MIC  Two, go button five            ->  ignored — flight chatter ("go button")
MIC  Fleece 2, ready to taxi        ->  ignored — talking to the flight
MIC  Ready to taxi                  ->  ignored — no agency addressed — open with the agency, e.g. "Tower, …"
MIC  ...how's the family            ->  ignored — no ATC call recognised — try one of the calls on the Fly tab
MIC  Nellis Ground, Fleece 1, ready to taxi  ->  ready_taxi channel=ground (100%)
```

Raise or lower **Min confidence** to taste. An out-of-context call is capped below the
default threshold, so lowering the bar is also how you loosen the phase and agency
checks.

If something fires when it shouldn't (or stays quiet when it shouldn't), add the phrase
to `check_voice_gate.py` and run `py -3 check_voice_gate.py` — it exercises the whole
grammar with no mic and no model.

Model weights (~150 MB for `base.en`) download once on first use and are cached.

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
