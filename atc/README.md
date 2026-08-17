# 455 Mission Flow Planner

## Open the app
Double-click either:
- `Open-ATC-Setup.cmd`
- `Open-Flight-Flow.cmd`

Both open the same polished UI. `Open-Zone-Editor.cmd` is the separate map for
drawing the areas that fire steps automatically — the app has a button for it too.

## Tabs
1. **Plan Flight** — build the full sortie timeline (TTS template, custom text, or MP3/OGG). No JSON editing.
2. **Fly** — big Next / Back / Reset / Flip for mid-flight.
3. **Setup** — Opus username, voice, airport freqs, SRS host, TTS provider, optional runway override, and **Controls** (HOTAS / hotkeys / voice).
4. **Help** — in-app how-tos (Getting started, Google JSON setup, Plan Flight tips, troubleshooting). Also the top-right **Help** button.

## Typical workflow
1. Title bar — type your CAOC name and click the green flight chip to pick the
   Opus flight (saves immediately; Setup → Save is not required for identity).
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
| **HOTAS / mouse button** | No | Reads the stick or mouse directly, unaffected by focus. Setup → Controls → **Learn…** (same control — press either) |
| **Keyboard hotkey** | No | Also polled, so it survives DCS focus. Does not swallow the key, so pick a combo DCS ignores |
| **Local URL** | No | `http://127.0.0.1:8765/next` and `/back` transmit; `/seek_next` and `/seek_prev` only move the cursor |
| **Restart as administrator** | Yes | Belt and braces: makes the registered hotkey work as well |

Use whichever you prefer, or all of them at once — a press is only acted on once no
matter how many routes it arrives by.

**Show SRS PTT buttons…** lists the HOTAS buttons SRS already transmits on, so you can
pick something else for Next/Back. Mouse side buttons (4 / 5) are a good spare option.
The **Last trigger** line confirms a press actually reached the app.

### Keyboard hotkeys

Defaults are F13/F14 because Stream Deck can *send* those even though no keyboard has
them, and they never collide with a DCS binding. On a plain keyboard pick anything free
— **Capture…** takes whatever you press, e.g. `Ctrl+Alt+Shift+N`. **Step forward** and
**Step back** are unbound until you assign them; they cycle the timeline without
transmitting. The Controls tab shows what actually registered, and says so when another
app already owns the combo.

Keys are handled two ways at once. `RegisterHotKey` swallows the keystroke so it never
reaches the game, but Windows will not deliver it while DCS has focus. Polling the key
state does survive that — the same reason reading the stick works — at the cost of the
key also reaching DCS. Running both means the hotkey works everywhere, and the duplicate
is discarded.

Other endpoints on the same local server: `/reset`, `/flip`, `/seek?n=7`, `/status`.

### Frequency gate

External Advance / Previous (hotkey, HOTAS, mouse, voice, Stream Deck URL) only
transmit when **any** of your radios is tuned to the step’s frequency. Keying
intra-flight VHF does not block UHF ATC / C2 triggers — selected / PTT is TX
only. On-screen **Play** is never blocked. If radio state is unknown or stale,
the gate **allows** and Fly shows `Radio tune unknown — gate open`. Fly lists
every tuned radio and marks the keyed one `TX`.

**In the jet:** Setup → Controls → **Install DCS radio export…** (or
`Install-DCS-Radio-Export.cmd`). That copies the Lua script and patches
`Export.lua` automatically — leave the SRS line alone. Writes
`Saved Games\DCS\ATC-ExternalAudio\radios.json` for the gate to read.

**External AWACS (testing):** enable **External AWACS radio source** on Setup →
Controls. Fly shows an EAM radio strip — mirror your SRS AWACS overlay freqs, or
use **Tune to step** while walking the flow.

### Automatic clearances from live position

Setup → Controls → **Automatic clearances (live position)** watches the Opus CAOC
radar feed and issues the clearance the flight has physically earned:

* **Cleared for takeoff** once every flight member is lined up on the runway.
* **Monitor tower** once every flight member has reached the EOR — so you don't
  have to say "at EOR" at all.

Each fires once per sortie and re-arms on **Reset**. A rolling offer is never
automatic, and line-up-and-wait still comes from your ready call, since nobody is
on the runway yet at that point. Fly shows a live line under the frequency gate:
`21R: in position 1/2 · at EOR 0/2 · need 2`.

Turn off **Require every flight member** if you fly with AI wingmen — AI tracks
often carry no flight label, so the flight can collapse to just your jet.

These two only work at fields whose runway and EOR areas have been traced. Fly
says `geometry not calibrated, nothing will fire` when they have not — the shipped
Nellis numbers are estimated from published field data, close but not tight
enough to tell the runway from the parallel taxiway, and nothing fires on a
guess. Trace them once with the zone editor below.

If a jet you know is tracking straight reports about 12° of heading error, the
feed is sending magnetic — set `position_heading_offset_deg` in `config.json`.

### Drawing the areas (zone editor)

**Setup → Automatic clearances → Draw zones on a map…** opens a satellite map in
your browser. Pick the field, trace an area, say what it fires, press Save. The
same button is on **Plan Flight** next to **Fires when** as **Draw one…**, and
`Open-Zone-Editor.cmd` next to this app opens it without the app running.

It is a separate little program — a local web server on `127.0.0.1:8777`
(`zone_editor_port` in `config.json` moves it) that serves the map and writes
`airports.json`. Nothing leaves your machine except the map tiles. Pressing the
button again just brings the tab back rather than starting a second copy, and
closing the app stops it.

Saved areas show up on their own: the app re-reads `airports.json` every few
seconds, so a new zone is in the picker without a restart. Live CAOC tracks are
drawn on the same map, which is how you check that a jet parked on the runway in
DCS also sits inside the area you traced. Details of the drawing tools, imports
from Google Earth KML and the `calibrated` flag are in `tools/README.md`.

### Any step can fire off a zone

The two above are the built-in cases. On **Plan Flight**, any step — including a
custom one on the `other` channel for a custom agency — can name a drawn area
under **Fires when**, and it goes off when the flight is in it. The timeline marks
those steps `[auto: <zone>]`. The Setup master switch still applies: with
**Watch live position** off, nothing fires off position at all.

| Control | What it does |
|---------|--------------|
| Zone | A trigger tag (`eor`, `tower`) or one specific area. A tag follows the active runway, so one flow works off either end; an id pins to the area you picked. The picker shows which tags have nothing drawn yet. |
| inside / leaving | `leaving` is for arrival steps — "clear of the runway" fires once the flight has been in the area and is out of it again. |
| settled | Stopped, on the deck and lined up with the runway, not merely inside. On by default; turn it off for an airborne area like a tower zone, where heading means nothing. |
| Hold for | Seconds the condition must hold *continuously*. Blank uses `auto_clearance_dwell_s`. A jet dropping out restarts the clock rather than pausing it. |
| Radio gap | Minimum seconds since the last transmission, so the call never treads on the one before it. |

For line-up-and-wait, where everyone should be lined up before the takeoff
clearance rather than merely on the runway: `settled` on, hold for 15 s, radio gap
8 s. In `atc/flows/*.json` that step reads:

```json
{
  "id": "twr_clear_takeoff",
  "template": "clear_takeoff",
  "trigger": {
    "zone": "in_position",
    "when": "inside",
    "flight": "all",
    "settled": true,
    "dwell_s": 15,
    "gap_s": 8
  }
}
```

`flight` is `all` or `me`; leave it out to follow the **Require every flight
member** setting. `me` means your jet specifically, not whichever wingman happens
to be parked in the right place. A step with no `trigger` waits to be asked, as
before.

Zones can carry an altitude band (`min_alt_ft` / `max_alt_ft`, feet AGL), so
traffic overhead does not count as being on your ramp. A floor of `0` means the
surface and includes a jet reading slightly below it — field elevation is one
number for a field that is not flat. Bands are set in the zone editor; the
shipped Nellis `tower` area is a 5 NM circle, surface to 5000 ft.

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
is ignored. Repeating the last instruction (for example “contact Blackjack” after a
handoff) is treated as a readback and does **not** fire the next step — check in with
“with you” / “checking in” when you are ready. Flight chatter on the same PTT stays
silent throughout.

Calls it understands:

| Say | Result |
|-----|--------|
| "Ground, ready to taxi" / "request clearance" / "ready for departure" | Fires the matching flow step |
| "Request runway two one left" | Sets the runway and reads back the approval |
| "Say winds" / "say altimeter" | Live METAR answer |
| "Blackjack, request picture" | AFTTP picture (RANGE/AZIMUTH/VIC/… from live CAOC hostiles within `picture_max_range_nm`) |
| "Blackjack, bogey dope" / "BRAA" | BRAA to the closest hostile relative to you |
| "Blackjack, declare bullseye 056 67" | Short declaration only (`Fleece 1, Blackjack, hostile.`); cue picks the contact |
| "Bandsaw, checking in" / picture / bogey dope / declare | Optional C2 on Bandsaw |
| "Bandsaw, checking out / switch Blackjack" | Leave Bandsaw → contact Blackjack (not check-in) |
| "Blackjack, request Bandsaw" | Push to Bandsaw (optional; you can also self-tune) |
| "Blackjack / Bandsaw, request tanker" | Vectors to the Opus KC-135 (boom — not MPRS). Live CAOC bullseye / steer when the track is up |
| "Texaco, request boom" | Missing official join: identified, cleared rejoin left (still use DCS **Intent to refuel**) |
| "Left observation" | Missing official call: cleared astern, then use DCS **Ready pre-contact** |
| DCS tanker radio | **Ready pre-contact** → DCS *cleared contact* (boom). **Abort refueling** to disconnect. Do not use SRS for those. |
| "Blackjack, off station / range complete" | Range checkout → Approach (required after Blackjack check-in) |
| "Approach, checking in" / "inbound" | Approach assigns recovery from METAR (VMC → VFR recovery + TAC overhead; IFR → instrument + IAF). Prefers RWY 21 |
| "Request ARCOE / TORYE / STRYK / MINTT / overhead / instrument" | Change the assigned recovery / approach |
| "Request hold" / "cancel hold" | Spoken hold / continue (simple state) |
| "Request vectors" | Radar vector clearance toward recovery / field |
| "Airport in sight, request tower" | Cleared approach / contact Tower |
| "Alpha check" | Bullseye position for your aircraft |
| "We'll take the rolling" / "unable rolling" | Accepts or declines the rolling departure. After Tower asks, **Next / Advance accepts** and **Previous declines** (HOTAS, hotkey, Stream Deck) — no voice or alt-tab needed. |
| "Gear down full stop" / "going around" / "clear of the runway" | Fires the matching step |
| "Say again" | Replays the last transmission |

The mission timeline uses three **mission phases** (separate from the radio agency):

| Phase | Agencies |
|-------|----------|
| **Departure** | Delivery, Ground, Tower, Departure |
| **Flight / airwork** | Blackjack, Bandsaw, Ops, Other (en-route / C2) |
| **Approach** | Approach, Tower, Ground |

After Blackjack check-in you are in Flight: stay on Blackjack, push or self-tune to
**Bandsaw** for picture/C2 work, or do other range tasks. **You can say** tips follow
the frequency you are actually tuned to within that phase (Blackjack tips on 377.8,
Bandsaw tips on 378.225). When finished with Bandsaw, **check out** on that net
(“checking out” / “switch Blackjack”) — that call advances past Bandsaw;
check-in alone does not. Then return to Blackjack for range exit before Approach.
Bandsaw is optional and does not block that handoff.

**Approach / recovery (NAFBI 11-250):** Blackjack range exit says **proceed direct**
to the exit / recovery fix (e.g. Arcoe / Torye / Dudbe) and hands you to Approach.
Approach **check-in** gives landing south/north and **expect** (e.g. Arcoe recovery
for the TAC Overhead, or ILS Zulu 21L). The next step is the **clearance** —
VFR: cleared … recovery; instrument: *cross Dudbe at or above 16000, cleared ILS
Zulu runway 21L* (one procedure only — ILS **or** LOC, not both). Assignment order:

1. Your spoken / Fly override  
2. A recovery or IAF named near the **end of the Opus filed route** (e.g. `… STRYK KLSV`)  
3. METAR: VMC → runway-side VFR default (ARCOE/TORYE/STRYK on 21, MINTT on 03); IFR → instrument + IAF  

Per NellisAFBI 11-250 §4.13.5 the four VFR recoveries are **STRYK, TORYE, ARCOE,
MINTT** — TORYE and ARCOE are separate initial fixes (Elgin pick-up is TORYE).
Check-in sounds like: *“Fleece 1, Nellis Approach, Nellis landing south, expect
TAC Overhead runway two one right, cleared direct Arcoe, …”* (or Torye / Stryk /
Mintt for that recovery; landing north when the 03s are active; instrument uses
the plate name + IAF). Descend altitude comes from the VFR recovery or the
**plate IAF altitude** in `approaches/nellis.json`. Speed is **not** cleared
unless traffic (or similar) sets a restriction. Recoveries prefer the **21s**;
the **03s** are used only when headwind on 03 is **11 kt or greater**. You can
request a different recovery, hold, or vectors. “Airport in sight / request tower”
clears you to Tower.

After an **instrument missed**, Approach sends you back to the IAF (e.g. Arcoe).
Watch re-arms the approach-clearance gate there (about 8 NM) and will not
auto-hand to Tower until you are outside the missed-approach bubble and then
inside 12 NM of the field.

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
- **Custom** and **file** steps do not inherit leftover template cues (so Fly will not
  show “with you” unless you type it). Edit step → **Say to advance** sets the Fly
  **TO ADVANCE** line for that step.
- They also do not inherit Bandsaw/Blackjack behavior from a leftover template.
  **C2 services** (picture / bogey dope / declare) and **Stay after play** are checkboxes
  on Edit step. Leave both off for Center / Joshua / other transit agencies. Check C2
  only if that agency should answer those tactical calls. Check Stay if Play should
  transmit and wait (Bandsaw-style); the Say to advance phrases still move the cursor.
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

---

## Automatic clearance tuning (`config.json`)

Only needed if the defaults misjudge your field. Distances are metres.

| Key | Default | Meaning |
|-----|---------|---------|
| `auto_clearance_enabled` | `false` | Master switch for position-driven clearances |
| `auto_takeoff_clearance` | `true` | Cleared for takeoff when in position |
| `auto_monitor_tower` | `true` | Monitor tower when at the EOR |
| `auto_clearance_require_full_flight` | `true` | Every member must qualify, not just you |
| `auto_clearance_dwell_s` | `3.0` | Default hold time, when a step does not set its own |
| `position_lateral_margin_m` | `20` | Slack either side of the runway edge (fallback box only) |
| `position_box_m` | `900` | How far past the threshold counts as in position (fallback box only) |
| `position_heading_tolerance_deg` | `30` | How far off runway heading is tolerated |
| `position_heading_offset_deg` | `0` | Correction if the feed reports magnetic heading |
| `position_alt_tolerance_m` | `60` | Height band around field elevation |
| `position_max_speed_mps` | `12` | Above this you are rolling, not in position |
| `position_settled_speed_mps` | `2` | Stopped, for a step that asks for **settled** — tighter than the taxi limit above |
| `eor_radius_m` | `250` | Size of the EOR circle (fallback only; a drawn area wins) |
| `own_max_distance_m` | `20000` | Ignore a matched track further out than this |
| `zone_editor_port` | `8777` | Loopback port the zone editor serves the map on |

`check_runway_position.py` exercises the geometry and the zone containment tests
against synthetic data — worth running after editing any of these.
