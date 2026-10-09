# Agency radio calls

Reference of pilot calls, match keywords, and agency replies used by PitBoss. Placeholders use angle brackets (e.g. `<Callsign>`, `<Runway>`). Spoken values are filled at TX time from Opus, METAR, airport config, and live state.

## Data sources

| Source | Role |
|--------|------|
| `atc/agencies.py` (`_CORE`) | Agency ids, spoken names, default handoffs, services |
| `atc/airports.json` | Per-airport freqs / mods / Local presets / zone triggers |
| `atc/voice_intent.py` (`INTENTS`, `_AGENCY_TERMS`) | Pilot call examples + keyword groups + channel gates |
| `atc/atc_phrase.py` (`TEMPLATE_CHOICES`, `build_template_text`) | Scripted agency replies for flow templates |
| `atc/voice_actions.py` | Ad-hoc replies (winds, altimeter, picture, bogey dope, declare) |
| `atc/tanker.py` | C2 tanker vectors / TACAN / freq / bullseye / AAR join-depart |
| `atc/tanker_chat_library.py` (`THREADS`) | Boom small-talk openers, pilot choices, boom replies |
| `atc/flows/*.json` | Mission sequence (`channel` + `template`); optional `voice_phrases` |

Pilot **keywords** do not live inside the reply builders. Linking is by `Intent.template` / `voice_engine.execute_intent` / flow step `channel`+`template`.

## Conventions

- **Pilot call** — typical Fly tip / `Intent.example`, opened with agency + callsign unless noted.
- **Keywords** — AND-of-OR groups: every group needs at least one hit. `_ASKING` = common request verbs listed in `voice_intent.py`.
- **Response** — representative agency line with `<placeholders>`; conditional branches summarized with `|`.
- **Nellis freqs** below are from `airports.json` (`nellis`); other airports override.
- C2 picture / bogey dope / declare / alpha check are offered on Blackjack, Bandsaw, and Ops by default (`step_offers_c2`); Joshua / Center / `other` only if the step sets `c2: true`.
- Wrong field agency (e.g. calling Tower while still on Ground) may get a redirect: <Callsign>, <CalledAgency>, contact <CorrectFieldAgency>.

## Address aliases

Opening tokens in the first few words select the agency (`extract_channel`):

| Channel | Address terms |
|---------|---------------|
| `delivery` | clearance delivery, delivery |
| `ground` | ground |
| `tower` | tower |
| `departure` | departure, dep |
| `approach` | approach, arrival |
| `blackjack` | blackjack, black jack, magic, darkstar, awacs |
| `bandsaw` | bandsaw, + Whisper variants in `_BANDSAW_TERMS` |
| `joshua` | joshua control, joshua, josh, jashua, joshwa, joshua approach |
| `control_east` | sally, nellis control, control east, natcf, control |
| `control_west` | lee, control west, nellis control west |
| `center` | los angeles center, la center, center, centre |
| `ops` | ops, operations, base ops |
| `tanker` | texaco, shell, arco, esso, tanker, boom |
| `other` | *(no address terms — mission-authored)* |

## Shared ad-hoc (any / multi-agency)

These are not pinned to a single agency (or are available broadly).

### `request_winds`

- **Pilot call:** `<Agency>, <Callsign>, say winds`
- **Does:** current winds
- **Keywords:** `_ASKING` + `how` **AND** `wind` | `winds`
- **Response:** <Callsign>, <AgencyOrAirport>, <Winds>.

### `request_altimeter`

- **Pilot call:** `<Agency>, <Callsign>, say altimeter`
- **Does:** current altimeter
- **Keywords:** `_ASKING` **AND** `altimeter` | `qnh`
- **Response:** <Callsign>, <AgencyOrAirport>, altimeter <Altimeter>.

### `say_again`

- **Pilot call:** `<Agency>, <Callsign>, say again`
- **Does:** replay the last transmission
- **Keywords:** `say again` | `repeat` | `come again` | `one more time` | `didn t copy` | `did not copy`
- **Response:** *(replays last transmission)*

### `request_runway`

- **Pilot call:** `<Agency>, <Callsign>, request runway two one left`
- **Does:** change the active runway
- **Keywords:** `_ASKING` **AND** `runway`
- **Veto:** `cleared for`, `cleared to land`, `cleared takeoff`
- **Response:** *(changes active runway; then subsequent templates use it)*

## Clearance Delivery (`delivery`)

- **Spoken:** <Airport> Delivery
- **Nellis freq:** 289.4 AM *(typical)*
- **Address terms:** clearance delivery, delivery

### Timeline / step calls

#### `acknowledge_readback` → template `clearance_readback`

- **Pilot call:** <Airport> Delivery, <Callsign>, squawk zero five five one
- **Does:** confirm your clearance readback
- **Keywords:** `roger` | `wilco` | `copy` | `readback` | `read back` | `as filed` | `cleared to` | `cleared via` | `climb and maintain` | `climb via` | `climb as published` | `expect flight level` | `expect`
- **Veto:** `ready to copy`, `request clearance`, `requesting clearance`
- **Response:** <Callsign>, <Airport> Delivery, readback correct.

#### `ready_clearance` → template `clearance`

- **Pilot call:** <Airport> Delivery, <Callsign>, request clearance
- **Does:** IFR clearance
- **Keywords:** `clearance` | `ifr` **AND** `request` | `requesting` | `ready` | `like` | `copy`
- **Veto:** `readback`, `squawk`, `as filed`
- **Response:** <Callsign>, <Airport> Delivery, cleared to <Dest> via the <SID|Flex>, then as filed, climb via the SID | climb as published | climb and maintain <Climb>, expect <FiledAlt> <Minutes> minutes after departure, departure frequency <DepFreq>, squawk <Squawk>.
- **No flight plan:** <Callsign>, <Airport> Delivery, I show no flight plan on file, remain this frequency.
- **Note:** `acknowledge_readback` also fires for taxi / takeoff / landing readback windows (not Delivery-only).

## Ground (`ground`)

- **Spoken:** <Airport> Ground
- **Nellis freq:** 275.8 AM *(typical)*
- **Address terms:** ground

### Timeline / step calls

#### `at_eor` → template `monitor_tower`

- **Pilot call:** <Airport> Ground, <Callsign>, at EOR
- **Does:** monitor tower
- **Keywords:** `eor` | `at eor` | `at the eor` | `ready at eor` | `parked eor` | `parked at eor` | `holding eor` | `holding at eor` | `holding short` | `number 1 holding short` | `number one holding short` | `at the end` | `at the end of the runway` | `end of runway` | `end of the runway` | `the end of runway` | `at end of runway` | `we re at eor` | `we are at eor` | `holding short of the runway` | `number one at eor` | `alpha south` | `at alpha south` | `northwest eor` | `at northwest eor` | `nw eor`
- **Veto:** `taxi`, `request taxi`, `ready to taxi`
- **Response:** <Callsign>, <Airport> Ground, monitor tower, <TowerLocal|TowerFreq>.

#### `clear_of_runway` → template `taxi_in`

- **Pilot call:** <Airport> Ground, <Callsign>, clear of the runway
- **Does:** taxi back to parking
- **Keywords:** `clear of the runway` | `clear of runway` | `off the active` | `taxi in` | `clear the active`
- **Response:** <Callsign>, <Airport> Ground, taxi to <Parking> via <TaxiVia>.

#### `ready_taxi` → template `taxi`

- **Pilot call:** <Airport> Ground, <Callsign>, request taxi
- **Does:** taxi clearance
- **Keywords:** `taxi` **AND** `request` | `requesting` | `ready` | `we d like` | `i d like` | `would like`
- **Veto:** `taxi in`, `clear of the`, `off the active`
- **Response:** <Callsign>, <Airport> Ground, runway <Runway>, taxi <EOR> via <TaxiVia>, <Airport> altimeter <Altimeter>.

### Additional templates (Play / flow — no dedicated stock intent)

- **`hold_short`:** <Callsign>, <Airport> Ground, hold short runway <Runway>.
- **`contact_tower`:** <Callsign>, <Airport> Ground, contact tower on <TowerFreq>.

## Tower (`tower`)

- **Spoken:** <Airport> Tower
- **Nellis freq:** 327.0 AM *(typical)*
- **Address terms:** tower

### Timeline / step calls

#### `accept_rolling`

- **Pilot call:** <Airport> Tower, <Callsign>, we'll take the rolling
- **Does:** accept a rolling takeoff
- **Keywords:** `rolling` | `roll` **AND** `take` | `accept` | `affirm` | `affirmative` | `roger` | `we ll` | `will` | `ready`
- **Veto:** `unable`, `negative`, `full length`
- **Response:** *(sets rolling accept → next clear_takeoff wording)*

#### `clear_of_runway` → template `taxi_in`

- **Pilot call:** <Airport> Tower, <Callsign>, clear of the runway
- **Does:** taxi back to parking
- **Keywords:** `clear of the runway` | `clear of runway` | `off the active` | `taxi in` | `clear the active`
- **Response:** <Callsign>, <Airport> Ground, taxi to <Parking> via <TaxiVia>.

#### `deny_rolling`

- **Pilot call:** <Airport> Tower, <Callsign>, unable rolling
- **Does:** decline the rolling takeoff
- **Keywords:** `rolling` | `roll` **AND** `unable` | `negative` | `rather not` | `full length`
- **Response:** *(sets deny → LUAW wording)*

#### `going_around`

- **Pilot call:** <Airport> Tower, <Callsign>, going around
- **Does:** go-around / missed approach / pattern reentry
- **Keywords:** `going around` | `go around` | `waving off` | `wave off` | `on the go` | `we're on the go` | `we are on the go` | `on the go tower` | `missed approach` | `going missed` | `we're going missed` | `we are going missed` | `executing missed` | `going missed approach` | `going round` | `go ahead and missed` | `ahead and missed` | `going a missed` | `we're missed` | `we are missed` | `executing the missed`
- **Response:** <Callsign>, <Airport> Tower, go around, <ClosedTraffic|Reentry> approved runway <Runway>. | missed approach as published, contact <Airport> Approach, expect <IAF>.

#### `in_position` → template `clear_takeoff`

- **Pilot call:** <Airport> Tower, <Callsign>, in position
- **Does:** cleared for takeoff
- **Keywords:** `in position` | `in-position` | `we're in position` | `we are in position` | `number one in position` | `number 1 in position` | `lined up` | `we're lined up` | `we are lined up` | `on the numbers`
- **Veto:** `remain`, `holding short`, `rolling`, `line up and wait`, `and wait`
- **Response:** <Callsign>, <Airport> Tower, [<UnrestrictedClimb>] [<VFR Flex west>,] <Winds>, runway <Runway>, cleared for takeoff, contact <Airport> Departure <DepFreq>.

#### `ready_departure` → template `lineup`

- **Pilot call:** <Airport> Tower, <Callsign>, ready for departure
- **Does:** line up and wait
- **Keywords:** `ready` | `number 1` | `holding short` | `request` | `requesting` **AND** `departure` | `takeoff` | `take off` | `the active` | `to go` | `for the go`
- **Veto:** `contact departure`, `with departure`, `rolling`, `in position`, `lined up`
- **Response:** <Callsign>, <Airport> Tower, runway <Runway>, line up-and wait.

#### `request_landing` → template `clear_land`

- **Pilot call:** <Airport> Tower, <Callsign>, gear down full stop
- **Does:** landing clearance
- **Keywords:** `gear down` | `full stop` | `landing` **AND** `request` | `requesting` | `for` | `with` | `gear` | `full`
- **Veto:** `low approach`, `the option`, `low pass`, `initial`, `with you`
- **Response:** <Callsign>, <Airport> Tower, <Winds>, runway <Runway>, cleared to land, check gear down.

#### `request_lineup`

- **Pilot call:** <Airport> Tower, <Callsign>, ready for line up
- **Does:** line up and wait
- **Keywords:** `line up and wait` | `lineup and wait` | `position and hold` | `request line up` | `request lineup` | `ready for line up` | `ready for lineup`
- **Response:** *(plays lineup template)*

#### `request_low_approach`

- **Pilot call:** <Airport> Tower, <Callsign>, request low approach
- **Does:** expect the option (then on the go)
- **Keywords:** `low approach` | `request low approach` | `requesting low approach` | `the option` | `low pass`
- **Veto:** `cleared to land`, `full stop`
- **Response:** <Callsign>, <Airport> Tower, … cleared the option / low approach …

#### `request_rolling`

- **Pilot call:** <Airport> Tower, <Callsign>, request rolling
- **Does:** request a rolling takeoff
- **Keywords:** `rolling` | `roll` | `rolling takeoff` **AND** `request` | `requesting` | `like` | `want` | `prefer` | `can we` | `can i` | `able`
- **Veto:** `unable`, `negative`, `full length`, `line up`, `lineup`
- **Response:** *(Tower may offer rolling / clear rolling path)*

#### `request_unrestricted_climb`

- **Pilot call:** *(no stock Fly tip / hidden)*
- **Does:** —
- **Keywords:** `_ASKING` **AND** `unrestricted climb` | `unrestricted` | `unrestricted climb please`
- **Veto:** `unable unrestricted`, `unable the unrestricted`
- **Response:** *(Tower standby / unable / grant unrestricted climb prefix)*

#### `tower_check_in` → template `right_break`

- **Pilot call:** <Airport> Tower, <Callsign>, with you
- **Does:** tower check-in
- **Keywords:** `with you` | `with tower`
- **Veto:** `initial`
- **Response:** <Callsign>, <Airport> Tower, right break approved runway <Runway>. | continue straight-in runway <Runway>. | roger, continue <Instrument> runway <Runway>.

#### `tower_initial` → template `right_break`

- **Pilot call:** <Airport> Tower, <Callsign>, initial
- **Does:** tower check-in
- **Keywords:** `initial` | `at initial` | `we're initial` | `we are initial` | `on initial`
- **Veto:** `with you`
- **Response:** <Callsign>, <Airport> Tower, right break approved runway <Runway>. | continue straight-in runway <Runway>. | roger, continue <Instrument> runway <Runway>.

### Additional templates (Play / flow — no dedicated stock intent)

- **`remain_position`:** <Callsign>, <Airport> Tower, remain in position.
- **`rolling_accept`:** <Callsign>, will you accept rolling?
- **`clear_takeoff_rolling`:** <Callsign>, <Airport> Tower, thanks, <Winds>, runway <Runway>, cleared for takeoff, contact <Airport> Departure <DepFreq>. *(after rolling accept)*
- **`clear_takeoff_intersection`:** <Callsign>, <Airport> Tower, <Winds>, runway <Runway> at <Intersection>, cleared for takeoff, contact <Airport> Departure <DepFreq>.
- **`exit_runway`:** <Callsign>, <Airport> Tower, exit <left|right>, [cross <Runway>,] contact <Airport> Ground <GroundFreq>.
- **`contact_departure`:** <Callsign>, <Airport> Tower, contact <Airport> Departure <DepFreq>.

## Departure (`departure`)

- **Spoken:** <Airport> Departure
- **Nellis freq:** 350.225 AM *(typical)*
- **Address terms:** departure, dep

### Timeline / step calls

#### `departure_check_in` → template `radar_contact`

- **Pilot call:** <Airport> Departure, <Callsign>, with you
- **Does:** departure radar contact
- **Keywords:** `with you` | `airborne` | `we re airborne` | `we are airborne` | `checking in` | `check in` | `on frequency` | `radar contact` | `with departure`
- **Veto:** `say wind`, `say winds`, `altimeter`, `request wind`, `request winds`
- **Response:** <Callsign>, <Airport> Departure, radar contact, climb and maintain <Climb>.

#### `inbound_recovery` → template `approach_check_in`

- **Pilot call:** <Airport> Departure, <Callsign>, checking in
- **Does:** Approach check-in / recovery assignment
- **Keywords:** `inbound` | `recovery` | `recover` | `rtb` | `checking in` | `with you` | `check in` **AND** `request` | `requesting` | `for` | `with you` | `checking in` | `inbound` | `nellis`
- **Veto:** `request hold`, `holding`, `vectors`, `cancel hold`
- **Response:** <Callsign>, <Airport> Approach, <LandingFlow>, expect <Recovery> [for the <Pattern> runway <Runway>], … altimeter <Altimeter>.

#### `request_handoff` → template `departure_handoff`

- **Pilot call:** <Airport> Departure, <Callsign>, request handoff
- **Does:** handoff to Blackjack
- **Keywords:** `request handoff` | `requesting handoff` | `request the handoff` | `request blackjack` | `switch to blackjack` | `contact blackjack` | `push blackjack` | `handoff please`
- **Response:** <Callsign>, <Airport> Departure, contact <NextAgency> <NextFreq>.

### Additional templates (Play / flow — no dedicated stock intent)

- **`climb_cruise`:** <Callsign>, <Airport> Departure, climb and maintain <CruiseAlt>. | climb as filed.

## Approach (`approach`)

- **Spoken:** <Airport> Approach
- **Nellis freq:** 273.55 AM *(typical)*
- **Address terms:** approach, arrival

### Timeline / step calls

#### `approach_continue` → template `cleared_approach`

- **Pilot call:** <Airport> Approach, <Callsign>, request handoff
- **Does:** contact tower
- **Keywords:** `request handoff` | `requesting handoff` | `request the handoff` | `request tower` | `requesting tower` | `contact tower` | `airport in sight` | `field in sight` | `for the overhead` | `request the break` | `ready for the overhead` | `proceeding`
- **Veto:** `checking in`, `with you`, `established`
- **Response:** <Callsign>, <Airport> Approach, contact <Airport> Tower <TowerFreq>.

#### `approach_established` → template `cleared_approach`

- **Pilot call:** <Airport> Approach, <Callsign>, established
- **Does:** contact tower
- **Keywords:** `established` | `we're established` | `we are established` | `established on the approach` | `established inbound`
- **Veto:** `checking in`, `with you`, `handoff`
- **Response:** <Callsign>, <Airport> Approach, contact <Airport> Tower <TowerFreq>.

#### `cancel_hold`

- **Pilot call:** <Airport> Approach, <Callsign>, cancel hold
- **Does:** leave hold / continue recovery
- **Keywords:** `cancel hold` | `leave hold` | `leaving hold` | `done holding` | `outbound`
- **Response:** <Callsign>, <Airport> Approach, … *(leave hold / continue recovery)*

#### `inbound_recovery` → template `approach_check_in`

- **Pilot call:** <Airport> Approach, <Callsign>, checking in
- **Does:** Approach check-in / recovery assignment
- **Keywords:** `inbound` | `recovery` | `recover` | `rtb` | `checking in` | `with you` | `check in` **AND** `request` | `requesting` | `for` | `with you` | `checking in` | `inbound` | `nellis`
- **Veto:** `request hold`, `holding`, `vectors`, `cancel hold`
- **Response:** <Callsign>, <Airport> Approach, <LandingFlow>, expect <Recovery> [for the <Pattern> runway <Runway>], … altimeter <Altimeter>.

#### `request_approach`

- **Pilot call:** <Airport> Approach, <Callsign>, request overhead
- **Does:** request different recovery / approach
- **Keywords:** `request` | `requesting` | `want` | `prefer` | `change to` | `switch to` | `able` **AND** `overhead` | `tactical` | `straight in` | `straight-in` | `instrument` | `ils` | `stryk` | `torye` | `acton` | `arcoe` | `mintt` | `dudbe` | `recovery`
- **Veto:** `checking in`, `with you`, `bogey dope`, `picture`, `hold`, `holding`, `vector`, `vectors`, `altimeter`, `winds`
- **Response:** <Callsign>, <Airport> Approach, … *(recovery / procedure change)*

#### `request_hold`

- **Pilot call:** <Airport> Approach, <Callsign>, request hold
- **Does:** hold clearance
- **Keywords:** `request hold` | `need to hold` | `holding at` | `hold at` **AND** `hold` | `holding`
- **Veto:** `hold short`, `cancel hold`, `leave hold`, `holding short`
- **Response:** <Callsign>, <Airport> Approach, hold at <Fix> … *(hold clearance builder)*

#### `request_vectors`

- **Pilot call:** <Airport> Approach, <Callsign>, request vectors
- **Does:** radar vectors
- **Keywords:** `request vectors` | `need vectors` | `vectors to` | `vector to` **AND** `vectors` | `vector`
- **Response:** <Callsign>, <Airport> Approach, … *(radar vectors)*

### Additional templates (Play / flow — no dedicated stock intent)

- **`approach_procedure`:** <Callsign>, <Airport> Approach, cleared <VFRRecovery|IAF procedure> …

## Blackjack (`blackjack`)

- **Spoken:** Blackjack
- **Nellis freq:** 377.8 AM *(typical)*
- **Address terms:** blackjack, black jack, magic, darkstar, awacs

### Timeline / step calls

#### `range_entry` → template `bj_check_in`

- **Pilot call:** Blackjack, <Callsign>, checking in
- **Does:** blackjack check-in
- **Keywords:** `checking in` | `check in` | `checkin` | `with you` | `on frequency` | `with blackjack` | `on station` | `range entry` | `entering the range` | `enter the range` | `for the range` | `for range entry`
- **Veto:** `off station`, `range exit`, `range complete`, `exiting`
- **Response:** <Callsign>, Blackjack. Radar contact [<AlphaBullseye>]. [Scheduled airspace, areas <Areas>. Cleared entry until <VulEnd>, time now <ZuluNow>.] Current altimeter <Altimeter>. Cleared tactical. Frequency change approved, check out this frequency when range work complete.

#### `range_exit` → template `bj_range_exit`

- **Pilot call:** Blackjack, <Callsign>, off station, range complete
- **Does:** range exit
- **Keywords:** `range` | `station` **AND** `exit` | `exiting` | `departing` | `off` | `complete` | `detached`
- **Response:** <Callsign>, Blackjack, range exit approved, contact <NellisControlLocal> <ControlFreq>.

### Ad-hoc / C2 / tanker info

#### `request_alpha_check`

- **Pilot call:** Blackjack, <Callsign>, alpha check bullseye
- **Does:** your position off bullseye
- **Keywords:** `alpha check` | `alfa check` | `position check`
- **Response:** <Callsign>, <Agency>, alpha check <BullseyeBearing> <RangeNm>.

#### `request_bandsaw`

- **Pilot call:** *(no stock Fly tip / hidden)*
- **Does:** —
- **Keywords:** `_ASKING` + `push` | `go` **AND** `_BANDSAW_TERMS` (bandsaw / Whisper variants)
- **Response:** *(Blackjack contact Bandsaw handoff)*

#### `request_bogey_dope`

- **Pilot call:** Blackjack, <Callsign>, bogey dope
- **Does:** BRAA to the closest hostile
- **Keywords:** `bogey dope` | `bogie dope` | `braa` | `snaplock`
- **Response:** <Callsign>, <Agency>, clean. | <Callsign>, <Agency>, group <BRAA>, [<Altitude>,] [<Aspect>,] <Declaration> [, <N> contacts].
- **Note:** If C2 is Bandsaw-only for the step, Blackjack may redirect: <Callsign>, Blackjack, <picture|bogey dope|declare> is with Bandsaw. Contact Bandsaw <BandsawFreq>.

#### `request_control`

- **Pilot call:** *(no stock Fly tip / hidden)*
- **Does:** —
- **Keywords:** `_ASKING` + `push` | `go` **AND** `sally` | `lee` | `nellis control` | `control east` | `control west` | `natcf`
- **Veto:** `picture`, `pitcher`, `bogey`, `declare`, `tanker`, `texaco`, `joshua`
- **Response:** *(Blackjack contact Nellis Control handoff)*

#### `request_declare`

- **Pilot call:** Blackjack, <Callsign>, declare bullseye 056 67
- **Does:** ask C2 what that group is (query only — never PATCH)
- **Keywords:** `declare`
- **Veto:** `declare as`, `vid`, `visual id`
- **Response:** <Callsign>, <Agency>, <hostile|bandit|bogey|friendly|clean|unable>.
- **Note:** If C2 is Bandsaw-only for the step, Blackjack may redirect: <Callsign>, Blackjack, <picture|bogey dope|declare> is with Bandsaw. Contact Bandsaw <BandsawFreq>.

#### `report_vid`

- **Pilot call:** Bandsaw, <Callsign>, VID hostile
- **Does:** set CAOC affiliation after visual ID (PATCH)
- **Keywords:** `vid` | `id group` | `upgrade` | `declare as` | `group is` | `that's a` **AND** `bandit` | `hostile` | `hostel` | `friendly`
- **Response:** <Callsign>, <Agency>, <bandit|hostile|friendly>.
- **Note:** `declare Elvis xxx/xxx` stays `request_declare` even if "hostile" is in the transcript. Affiliation writes require a bullseye cue so the wrong group is not upgraded. Unknown picture/dope ends with *recommend intercept for visual ID*.

#### `request_joshua`

- **Pilot call:** *(no stock Fly tip / hidden)*
- **Does:** —
- **Keywords:** `_ASKING` + `push` | `go` **AND** `_JOSHUA_TERMS` (joshua / josh / …)
- **Veto:** `picture`, `pitcher`, `bogey`, `declare`, `tanker`, `texaco`
- **Response:** *(Blackjack contact Joshua handoff)*

#### `request_picture`

- **Pilot call:** Blackjack, <Callsign>, request picture
- **Does:** hostile groups off the live radar
- **Keywords:** `picture` | `pitcher`
- **Response:** <Callsign>, <Agency>, picture clean. | <Callsign>, <Agency>, <PictureLabel> … <GroupClauses>.
- **Note:** If C2 is Bandsaw-only for the step, Blackjack may redirect: <Callsign>, Blackjack, <picture|bogey dope|declare> is with Bandsaw. Contact Bandsaw <BandsawFreq>.

#### `request_tanker`

- **Pilot call:** Blackjack, <Callsign>, request tanker
- **Does:** track and BRAA to the Opus KC-135
- **Keywords:** `_ASKING` + `push` | `go` | `going` **AND** `tanker` | `texaco` | `air refuel` | `air refueling` | `aar`
- **Veto:** `tacan`, `frequency`, `freq`, `bullseye`, `back from`, `off the tanker`, `off tanker`, `returning from`, `done with`
- **Response:** <Callsign>, <Agency>, tanker <TankerCallsign>, [<Aircraft>,] [track <ARTrack>,] <BRAA> [, altitude <Alt>]. Frequency change approved.

#### `tanker_bullseye`

- **Pilot call:** Blackjack, <Callsign>, say tanker bullseye
- **Does:** live tanker bullseye
- **Keywords:** `_ASKING` **AND** `tanker bullseye` | `bullseye`
- **Veto:** `alpha check`, `alfa check`, `declare`
- **Response:** <Callsign>, <Agency>, <TankerCallsign> is <BullseyeFix>.

#### `tanker_freq`

- **Pilot call:** Blackjack, <Callsign>, say tanker frequency
- **Does:** published tanker UHF
- **Keywords:** `_ASKING` **AND** `tanker frequency` | `tanker freq` | `frequency`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, tanker frequency <UHF>.

#### `tanker_return`

- **Pilot call:** Blackjack, <Callsign>, back from the tanker
- **Does:** check back in after AAR
- **Keywords:** `back from the tanker` | `off the tanker` | `done with the tanker` | `returning from the tanker` | `check back in` | `checking back in` | `back from tanker` | `off tanker`
- **Response:** <Callsign>, Blackjack, radar contact [<AlphaBullseye>]. Continue. *(or Bandsaw/other agency equivalent)*

#### `tanker_tacan`

- **Pilot call:** Blackjack, <Callsign>, say TACAN
- **Does:** published tanker TACAN
- **Keywords:** `_ASKING` **AND** `tacan` | `tanker tacan` | `tanker channel`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, TACAN <Tacan>.

### Additional templates (Play / flow — no dedicated stock intent)

- **`bj_alpha_check`:** <Callsign>, Blackjack, alpha check <AlphaBullseye>.
- **`bj_range_entry`:** <Callsign>, Blackjack, Alpha approved, cleared hot.
- **`contact_bandsaw`:** <Callsign>, Blackjack, contact Bandsaw <BandsawFreq>.
- **`contact_joshua`:** <Callsign>, Blackjack, contact Joshua <JoshuaFreq>.

## Bandsaw (`bandsaw`)

- **Spoken:** Bandsaw
- **Nellis freq:** 378.225 AM *(typical)*
- **Address terms:** bandsaw, + Whisper variants in `_BANDSAW_TERMS`

### Timeline / step calls

#### `bandsaw_check_in` → template `bandsaw_check_in`

- **Pilot call:** Bandsaw, <Callsign>, checking in
- **Does:** bandsaw check-in
- **Keywords:** `checking in` | `check in` | `checkin` | `with you` | `on frequency` | `with bandsaw` | `with ansa` | `with and saw`
- **Veto:** `checking out`, `check out`, `checked out`, `off frequency`, `switching`, `switch blackjack`, `push blackjack`, `contact blackjack`
- **Response:** <Callsign>, Bandsaw, radar contact. Alpha check [<AlphaBullseye>].

#### `bandsaw_check_out` → template `bandsaw_check_out`

- **Pilot call:** Bandsaw, <Callsign>, checking out, switch Blackjack
- **Does:** bandsaw check-out → Blackjack
- **Keywords:** `checking out` | `check out` | `checked out` | `off frequency` | `switching to blackjack` | `switch blackjack` | `push blackjack` | `contact blackjack` | `done with bandsaw` | `bandsaw complete`
- **Veto:** `checking in`, `check in`, `checkin`, `with you`, `on station`
- **Response:** <Callsign>, Bandsaw, copy, contact Blackjack <BlackjackFreq>.

### Ad-hoc / C2 / tanker info

#### `request_alpha_check`

- **Pilot call:** Bandsaw, <Callsign>, alpha check bullseye
- **Does:** your position off bullseye
- **Keywords:** `alpha check` | `alfa check` | `position check`
- **Response:** <Callsign>, <Agency>, alpha check <BullseyeBearing> <RangeNm>.

#### `request_bogey_dope`

- **Pilot call:** Bandsaw, <Callsign>, bogey dope
- **Does:** BRAA to the closest hostile
- **Keywords:** `bogey dope` | `bogie dope` | `braa` | `snaplock`
- **Response:** <Callsign>, <Agency>, clean. | <Callsign>, <Agency>, group <BRAA>, [<Altitude>,] [<Aspect>,] <Declaration> [, <N> contacts].

#### `request_declare`

- **Pilot call:** Bandsaw, <Callsign>, declare bullseye 056 67
- **Does:** ask C2 what that group is (query only — never PATCH)
- **Keywords:** `declare`
- **Veto:** `declare as`, `vid`, `visual id`
- **Response:** <Callsign>, <Agency>, <hostile|bandit|bogey|friendly|clean|unable>.

#### `report_vid`

- **Pilot call:** Bandsaw, <Callsign>, VID hostile / ID group Elvis 090 17 bandit / declare as bandit / upgrade … hostel
- **Does:** set CAOC affiliation after visual ID (PATCH)
- **Keywords:** `vid` | `id group` | `upgrade` | `declare as` | `group is` | `that's a` **AND** `bandit` | `hostile` | `hostel` | `friendly`
- **Response:** <Callsign>, <Agency>, <bandit|hostile|friendly>.
- **Note:** DECLARE [Elvis] is a query. ID / upgrade / "declare as" writes the label and needs bullseye digits. Unknown calls end with *recommend intercept for visual ID*.

#### `request_picture`

- **Pilot call:** Bandsaw, <Callsign>, request picture
- **Does:** hostile groups off the live radar
- **Keywords:** `picture` | `pitcher`
- **Response:** <Callsign>, <Agency>, picture clean. | <Callsign>, <Agency>, <PictureLabel> … <GroupClauses>.

#### `request_tanker`

- **Pilot call:** Bandsaw, <Callsign>, request tanker
- **Does:** track and BRAA to the Opus KC-135
- **Keywords:** `_ASKING` + `push` | `go` | `going` **AND** `tanker` | `texaco` | `air refuel` | `air refueling` | `aar`
- **Veto:** `tacan`, `frequency`, `freq`, `bullseye`, `back from`, `off the tanker`, `off tanker`, `returning from`, `done with`
- **Response:** <Callsign>, <Agency>, tanker <TankerCallsign>, [<Aircraft>,] [track <ARTrack>,] <BRAA> [, altitude <Alt>]. Frequency change approved.

#### `tanker_bullseye`

- **Pilot call:** Bandsaw, <Callsign>, say tanker bullseye
- **Does:** live tanker bullseye
- **Keywords:** `_ASKING` **AND** `tanker bullseye` | `bullseye`
- **Veto:** `alpha check`, `alfa check`, `declare`
- **Response:** <Callsign>, <Agency>, <TankerCallsign> is <BullseyeFix>.

#### `tanker_freq`

- **Pilot call:** Bandsaw, <Callsign>, say tanker frequency
- **Does:** published tanker UHF
- **Keywords:** `_ASKING` **AND** `tanker frequency` | `tanker freq` | `frequency`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, tanker frequency <UHF>.

#### `tanker_return`

- **Pilot call:** Bandsaw, <Callsign>, back from the tanker
- **Does:** check back in after AAR
- **Keywords:** `back from the tanker` | `off the tanker` | `done with the tanker` | `returning from the tanker` | `check back in` | `checking back in` | `back from tanker` | `off tanker`
- **Response:** <Callsign>, Blackjack, radar contact [<AlphaBullseye>]. Continue. *(or Bandsaw/other agency equivalent)*

#### `tanker_tacan`

- **Pilot call:** Bandsaw, <Callsign>, say TACAN
- **Does:** published tanker TACAN
- **Keywords:** `_ASKING` **AND** `tacan` | `tanker tacan` | `tanker channel`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, TACAN <Tacan>.

## Joshua (`joshua`)

- **Spoken:** Joshua
- **Nellis freq:** 348.7 AM *(typical)*
- **Address terms:** joshua control, joshua, josh, jashua, joshwa, joshua approach

### Timeline / step calls

#### `joshua_check_in` → template `joshua_check_in`

- **Pilot call:** Joshua, <Callsign>, checking in
- **Does:** joshua check-in
- **Keywords:** `checking in` | `check in` | `checkin` | `with you` | `on frequency` | `with joshua` | `with josh`
- **Veto:** `checking out`, `check out`, `checked out`, `off frequency`, `switching`, `switch blackjack`, `push blackjack`, `contact blackjack`
- **Response:** <Callsign>, Joshua, radar contact. Remain this frequency.

#### `joshua_check_out` → template `joshua_check_out`

- **Pilot call:** Joshua, <Callsign>, checking out, switch Blackjack
- **Does:** joshua check-out → Blackjack
- **Keywords:** `checking out` | `check out` | `checked out` | `off frequency` | `switching to blackjack` | `switch blackjack` | `push blackjack` | `contact blackjack` | `done with joshua` | `joshua complete`
- **Veto:** `checking in`, `check in`, `checkin`, `with you`, `on station`
- **Response:** <Callsign>, Joshua, copy, contact Blackjack <BlackjackFreq>.

### Ad-hoc / C2 / tanker info

#### `request_alpha_check`

- **Pilot call:** Joshua, <Callsign>, alpha check bullseye
- **Does:** your position off bullseye
- **Keywords:** `alpha check` | `alfa check` | `position check`
- **Response:** <Callsign>, <Agency>, alpha check <BullseyeBearing> <RangeNm>.

#### `request_bogey_dope`

- **Pilot call:** Joshua, <Callsign>, bogey dope
- **Does:** BRAA to the closest hostile
- **Keywords:** `bogey dope` | `bogie dope` | `braa` | `snaplock`
- **Response:** <Callsign>, <Agency>, clean. | <Callsign>, <Agency>, group <BRAA>, [<Altitude>,] [<Aspect>,] <Declaration> [, <N> contacts].

#### `request_declare`

- **Pilot call:** Joshua, <Callsign>, declare bullseye 056 67
- **Does:** declaration at that bullseye (ELVIS); bare declare = nearest
- **Keywords:** `declare`
- **Response:** <Callsign>, <Agency>, <hostile|bogey|friendly|clean|unable>.

#### `request_picture`

- **Pilot call:** Joshua, <Callsign>, request picture
- **Does:** hostile groups off the live radar
- **Keywords:** `picture` | `pitcher`
- **Response:** <Callsign>, <Agency>, picture clean. | <Callsign>, <Agency>, <PictureLabel> … <GroupClauses>.

#### `request_tanker`

- **Pilot call:** Joshua, <Callsign>, request tanker
- **Does:** track and BRAA to the Opus KC-135
- **Keywords:** `_ASKING` + `push` | `go` | `going` **AND** `tanker` | `texaco` | `air refuel` | `air refueling` | `aar`
- **Veto:** `tacan`, `frequency`, `freq`, `bullseye`, `back from`, `off the tanker`, `off tanker`, `returning from`, `done with`
- **Response:** <Callsign>, <Agency>, tanker <TankerCallsign>, [<Aircraft>,] [track <ARTrack>,] <BRAA> [, altitude <Alt>]. Frequency change approved.

#### `tanker_bullseye`

- **Pilot call:** Joshua, <Callsign>, say tanker bullseye
- **Does:** live tanker bullseye
- **Keywords:** `_ASKING` **AND** `tanker bullseye` | `bullseye`
- **Veto:** `alpha check`, `alfa check`, `declare`
- **Response:** <Callsign>, <Agency>, <TankerCallsign> is <BullseyeFix>.

#### `tanker_freq`

- **Pilot call:** Joshua, <Callsign>, say tanker frequency
- **Does:** published tanker UHF
- **Keywords:** `_ASKING` **AND** `tanker frequency` | `tanker freq` | `frequency`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, tanker frequency <UHF>.

#### `tanker_return`

- **Pilot call:** Joshua, <Callsign>, back from the tanker
- **Does:** check back in after AAR
- **Keywords:** `back from the tanker` | `off the tanker` | `done with the tanker` | `returning from the tanker` | `check back in` | `checking back in` | `back from tanker` | `off tanker`
- **Response:** <Callsign>, Blackjack, radar contact [<AlphaBullseye>]. Continue. *(or Bandsaw/other agency equivalent)*

#### `tanker_tacan`

- **Pilot call:** Joshua, <Callsign>, say TACAN
- **Does:** published tanker TACAN
- **Keywords:** `_ASKING` **AND** `tacan` | `tanker tacan` | `tanker channel`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, TACAN <Tacan>.

## Nellis Control (East / Sally) (`control_east`)

- **Spoken:** Nellis Control
- **Nellis freq:** 317.525 AM *(typical)*
- **Address terms:** sally, nellis control, control east, natcf, control

### Timeline / step calls

#### `control_check_in` → template `control_check_in`

- **Pilot call:** Nellis Control, <Callsign>, checking in
- **Does:** Nellis Control check-in
- **Keywords:** `checking in` | `check in` | `checkin` | `with you` | `on frequency` | `for pickup` | `natcf`
- **Veto:** `checking out`, `check out`, `contact approach`, `request approach`, `switch approach`
- **Response:** <Callsign>, Nellis Control, radar contact, [proceed direct <Fix>,] [descend … <Altitude>,] [expect <Recovery> …].

#### `control_handoff` → template `control_handoff`

- **Pilot call:** Nellis Control, <Callsign>, contact Approach
- **Does:** Nellis Control → Approach
- **Keywords:** `checking out` | `check out` | `contact approach` | `request approach` | `switch approach` | `push approach` | `for approach`
- **Veto:** `checking in`, `check in`, `checkin`, `with you`
- **Response:** <Callsign>, Nellis Control, contact <Airport> Approach <ApproachFreq>.

### Ad-hoc / C2 / tanker info

#### `request_alpha_check`

- **Pilot call:** Nellis Control, <Callsign>, alpha check bullseye
- **Does:** your position off bullseye
- **Keywords:** `alpha check` | `alfa check` | `position check`
- **Response:** <Callsign>, <Agency>, alpha check <BullseyeBearing> <RangeNm>.

#### `request_bogey_dope`

- **Pilot call:** Nellis Control, <Callsign>, bogey dope
- **Does:** BRAA to the closest hostile
- **Keywords:** `bogey dope` | `bogie dope` | `braa` | `snaplock`
- **Response:** <Callsign>, <Agency>, clean. | <Callsign>, <Agency>, group <BRAA>, [<Altitude>,] [<Aspect>,] <Declaration> [, <N> contacts].

#### `request_declare`

- **Pilot call:** Nellis Control, <Callsign>, declare bullseye 056 67
- **Does:** declaration at that bullseye (ELVIS); bare declare = nearest
- **Keywords:** `declare`
- **Response:** <Callsign>, <Agency>, <hostile|bogey|friendly|clean|unable>.

#### `request_picture`

- **Pilot call:** Nellis Control, <Callsign>, request picture
- **Does:** hostile groups off the live radar
- **Keywords:** `picture` | `pitcher`
- **Response:** <Callsign>, <Agency>, picture clean. | <Callsign>, <Agency>, <PictureLabel> … <GroupClauses>.

#### `request_tanker`

- **Pilot call:** Nellis Control, <Callsign>, request tanker
- **Does:** track and BRAA to the Opus KC-135
- **Keywords:** `_ASKING` + `push` | `go` | `going` **AND** `tanker` | `texaco` | `air refuel` | `air refueling` | `aar`
- **Veto:** `tacan`, `frequency`, `freq`, `bullseye`, `back from`, `off the tanker`, `off tanker`, `returning from`, `done with`
- **Response:** <Callsign>, <Agency>, tanker <TankerCallsign>, [<Aircraft>,] [track <ARTrack>,] <BRAA> [, altitude <Alt>]. Frequency change approved.

#### `tanker_bullseye`

- **Pilot call:** Nellis Control, <Callsign>, say tanker bullseye
- **Does:** live tanker bullseye
- **Keywords:** `_ASKING` **AND** `tanker bullseye` | `bullseye`
- **Veto:** `alpha check`, `alfa check`, `declare`
- **Response:** <Callsign>, <Agency>, <TankerCallsign> is <BullseyeFix>.

#### `tanker_freq`

- **Pilot call:** Nellis Control, <Callsign>, say tanker frequency
- **Does:** published tanker UHF
- **Keywords:** `_ASKING` **AND** `tanker frequency` | `tanker freq` | `frequency`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, tanker frequency <UHF>.

#### `tanker_return`

- **Pilot call:** Nellis Control, <Callsign>, back from the tanker
- **Does:** check back in after AAR
- **Keywords:** `back from the tanker` | `off the tanker` | `done with the tanker` | `returning from the tanker` | `check back in` | `checking back in` | `back from tanker` | `off tanker`
- **Response:** <Callsign>, Blackjack, radar contact [<AlphaBullseye>]. Continue. *(or Bandsaw/other agency equivalent)*

#### `tanker_tacan`

- **Pilot call:** Nellis Control, <Callsign>, say TACAN
- **Does:** published tanker TACAN
- **Keywords:** `_ASKING` **AND** `tacan` | `tanker tacan` | `tanker channel`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, TACAN <Tacan>.

## Nellis Control (West / Lee) (`control_west`)

- **Spoken:** Nellis Control
- **Nellis freq:** 254.4 AM *(typical)*
- **Address terms:** lee, control west, nellis control west

### Timeline / step calls

#### `control_check_in` → template `control_check_in`

- **Pilot call:** Nellis Control, <Callsign>, checking in
- **Does:** Nellis Control check-in
- **Keywords:** `checking in` | `check in` | `checkin` | `with you` | `on frequency` | `for pickup` | `natcf`
- **Veto:** `checking out`, `check out`, `contact approach`, `request approach`, `switch approach`
- **Response:** <Callsign>, Nellis Control, radar contact, [proceed direct <Fix>,] [descend … <Altitude>,] [expect <Recovery> …].

#### `control_handoff` → template `control_handoff`

- **Pilot call:** Nellis Control, <Callsign>, contact Approach
- **Does:** Nellis Control → Approach
- **Keywords:** `checking out` | `check out` | `contact approach` | `request approach` | `switch approach` | `push approach` | `for approach`
- **Veto:** `checking in`, `check in`, `checkin`, `with you`
- **Response:** <Callsign>, Nellis Control, contact <Airport> Approach <ApproachFreq>.

### Ad-hoc / C2 / tanker info

#### `request_alpha_check`

- **Pilot call:** Nellis Control, <Callsign>, alpha check bullseye
- **Does:** your position off bullseye
- **Keywords:** `alpha check` | `alfa check` | `position check`
- **Response:** <Callsign>, <Agency>, alpha check <BullseyeBearing> <RangeNm>.

#### `request_bogey_dope`

- **Pilot call:** Nellis Control, <Callsign>, bogey dope
- **Does:** BRAA to the closest hostile
- **Keywords:** `bogey dope` | `bogie dope` | `braa` | `snaplock`
- **Response:** <Callsign>, <Agency>, clean. | <Callsign>, <Agency>, group <BRAA>, [<Altitude>,] [<Aspect>,] <Declaration> [, <N> contacts].

#### `request_declare`

- **Pilot call:** Nellis Control, <Callsign>, declare bullseye 056 67
- **Does:** declaration at that bullseye (ELVIS); bare declare = nearest
- **Keywords:** `declare`
- **Response:** <Callsign>, <Agency>, <hostile|bogey|friendly|clean|unable>.

#### `request_picture`

- **Pilot call:** Nellis Control, <Callsign>, request picture
- **Does:** hostile groups off the live radar
- **Keywords:** `picture` | `pitcher`
- **Response:** <Callsign>, <Agency>, picture clean. | <Callsign>, <Agency>, <PictureLabel> … <GroupClauses>.

#### `request_tanker`

- **Pilot call:** Nellis Control, <Callsign>, request tanker
- **Does:** track and BRAA to the Opus KC-135
- **Keywords:** `_ASKING` + `push` | `go` | `going` **AND** `tanker` | `texaco` | `air refuel` | `air refueling` | `aar`
- **Veto:** `tacan`, `frequency`, `freq`, `bullseye`, `back from`, `off the tanker`, `off tanker`, `returning from`, `done with`
- **Response:** <Callsign>, <Agency>, tanker <TankerCallsign>, [<Aircraft>,] [track <ARTrack>,] <BRAA> [, altitude <Alt>]. Frequency change approved.

#### `tanker_bullseye`

- **Pilot call:** Nellis Control, <Callsign>, say tanker bullseye
- **Does:** live tanker bullseye
- **Keywords:** `_ASKING` **AND** `tanker bullseye` | `bullseye`
- **Veto:** `alpha check`, `alfa check`, `declare`
- **Response:** <Callsign>, <Agency>, <TankerCallsign> is <BullseyeFix>.

#### `tanker_freq`

- **Pilot call:** Nellis Control, <Callsign>, say tanker frequency
- **Does:** published tanker UHF
- **Keywords:** `_ASKING` **AND** `tanker frequency` | `tanker freq` | `frequency`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, tanker frequency <UHF>.

#### `tanker_return`

- **Pilot call:** Nellis Control, <Callsign>, back from the tanker
- **Does:** check back in after AAR
- **Keywords:** `back from the tanker` | `off the tanker` | `done with the tanker` | `returning from the tanker` | `check back in` | `checking back in` | `back from tanker` | `off tanker`
- **Response:** <Callsign>, Blackjack, radar contact [<AlphaBullseye>]. Continue. *(or Bandsaw/other agency equivalent)*

#### `tanker_tacan`

- **Pilot call:** Nellis Control, <Callsign>, say TACAN
- **Does:** published tanker TACAN
- **Keywords:** `_ASKING` **AND** `tacan` | `tanker tacan` | `tanker channel`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, TACAN <Tacan>.

## Los Angeles Center (`center`)

- **Spoken:** Los Angeles Center
- **Nellis freq:** 353.6 AM *(typical)*
- **Address terms:** los angeles center, la center, center, centre

### Timeline / step calls

#### `center_check_in` → template `center_check_in`

- **Pilot call:** Los Angeles Center, <Callsign>, checking in
- **Does:** LA Center check-in
- **Keywords:** `checking in` | `check in` | `checkin` | `with you` | `on frequency` | `radar contact`
- **Veto:** `checking out`, `check out`, `contact blackjack`, `switch blackjack`
- **Response:** <Callsign>, Los Angeles Center, radar contact. Remain this frequency.

### Ad-hoc / C2 / tanker info

#### `request_alpha_check`

- **Pilot call:** Los Angeles Center, <Callsign>, alpha check bullseye
- **Does:** your position off bullseye
- **Keywords:** `alpha check` | `alfa check` | `position check`
- **Response:** <Callsign>, <Agency>, alpha check <BullseyeBearing> <RangeNm>.

#### `request_bogey_dope`

- **Pilot call:** Los Angeles Center, <Callsign>, bogey dope
- **Does:** BRAA to the closest hostile
- **Keywords:** `bogey dope` | `bogie dope` | `braa` | `snaplock`
- **Response:** <Callsign>, <Agency>, clean. | <Callsign>, <Agency>, group <BRAA>, [<Altitude>,] [<Aspect>,] <Declaration> [, <N> contacts].

#### `request_declare`

- **Pilot call:** Los Angeles Center, <Callsign>, declare bullseye 056 67
- **Does:** declaration at that bullseye (ELVIS); bare declare = nearest
- **Keywords:** `declare`
- **Response:** <Callsign>, <Agency>, <hostile|bogey|friendly|clean|unable>.

#### `request_picture`

- **Pilot call:** Los Angeles Center, <Callsign>, request picture
- **Does:** hostile groups off the live radar
- **Keywords:** `picture` | `pitcher`
- **Response:** <Callsign>, <Agency>, picture clean. | <Callsign>, <Agency>, <PictureLabel> … <GroupClauses>.

#### `request_tanker`

- **Pilot call:** Los Angeles Center, <Callsign>, request tanker
- **Does:** track and BRAA to the Opus KC-135
- **Keywords:** `_ASKING` + `push` | `go` | `going` **AND** `tanker` | `texaco` | `air refuel` | `air refueling` | `aar`
- **Veto:** `tacan`, `frequency`, `freq`, `bullseye`, `back from`, `off the tanker`, `off tanker`, `returning from`, `done with`
- **Response:** <Callsign>, <Agency>, tanker <TankerCallsign>, [<Aircraft>,] [track <ARTrack>,] <BRAA> [, altitude <Alt>]. Frequency change approved.

#### `tanker_bullseye`

- **Pilot call:** Los Angeles Center, <Callsign>, say tanker bullseye
- **Does:** live tanker bullseye
- **Keywords:** `_ASKING` **AND** `tanker bullseye` | `bullseye`
- **Veto:** `alpha check`, `alfa check`, `declare`
- **Response:** <Callsign>, <Agency>, <TankerCallsign> is <BullseyeFix>.

#### `tanker_freq`

- **Pilot call:** Los Angeles Center, <Callsign>, say tanker frequency
- **Does:** published tanker UHF
- **Keywords:** `_ASKING` **AND** `tanker frequency` | `tanker freq` | `frequency`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, tanker frequency <UHF>.

#### `tanker_tacan`

- **Pilot call:** Los Angeles Center, <Callsign>, say TACAN
- **Does:** published tanker TACAN
- **Keywords:** `_ASKING` **AND** `tacan` | `tanker tacan` | `tanker channel`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, TACAN <Tacan>.

### Additional templates (Play / flow — no dedicated stock intent)

- **`center_radar`:** <Callsign>, Los Angeles Center, radar contact. Remain this frequency.
- **`center_handoff`:** <Callsign>, Los Angeles Center, contact <NextAgency> <NextFreq>.

## Ops (`ops`)

- **Spoken:** <Airport> Ops
- **Nellis freq:** 255.0 AM *(typical)*
- **Address terms:** ops, operations, base ops

### Timeline / step calls

*(No dedicated step intents — see ad-hoc / extras below.)*

### Ad-hoc / C2 / tanker info

#### `request_alpha_check`

- **Pilot call:** <Airport> Ops, <Callsign>, alpha check bullseye
- **Does:** your position off bullseye
- **Keywords:** `alpha check` | `alfa check` | `position check`
- **Response:** <Callsign>, <Agency>, alpha check <BullseyeBearing> <RangeNm>.

#### `request_bogey_dope`

- **Pilot call:** <Airport> Ops, <Callsign>, bogey dope
- **Does:** BRAA to the closest hostile
- **Keywords:** `bogey dope` | `bogie dope` | `braa` | `snaplock`
- **Response:** <Callsign>, <Agency>, clean. | <Callsign>, <Agency>, group <BRAA>, [<Altitude>,] [<Aspect>,] <Declaration> [, <N> contacts].

#### `request_declare`

- **Pilot call:** <Airport> Ops, <Callsign>, declare bullseye 056 67
- **Does:** declaration at that bullseye (ELVIS); bare declare = nearest
- **Keywords:** `declare`
- **Response:** <Callsign>, <Agency>, <hostile|bogey|friendly|clean|unable>.

#### `request_picture`

- **Pilot call:** <Airport> Ops, <Callsign>, request picture
- **Does:** hostile groups off the live radar
- **Keywords:** `picture` | `pitcher`
- **Response:** <Callsign>, <Agency>, picture clean. | <Callsign>, <Agency>, <PictureLabel> … <GroupClauses>.

#### `request_tanker`

- **Pilot call:** <Airport> Ops, <Callsign>, request tanker
- **Does:** track and BRAA to the Opus KC-135
- **Keywords:** `_ASKING` + `push` | `go` | `going` **AND** `tanker` | `texaco` | `air refuel` | `air refueling` | `aar`
- **Veto:** `tacan`, `frequency`, `freq`, `bullseye`, `back from`, `off the tanker`, `off tanker`, `returning from`, `done with`
- **Response:** <Callsign>, <Agency>, tanker <TankerCallsign>, [<Aircraft>,] [track <ARTrack>,] <BRAA> [, altitude <Alt>]. Frequency change approved.

#### `tanker_bullseye`

- **Pilot call:** <Airport> Ops, <Callsign>, say tanker bullseye
- **Does:** live tanker bullseye
- **Keywords:** `_ASKING` **AND** `tanker bullseye` | `bullseye`
- **Veto:** `alpha check`, `alfa check`, `declare`
- **Response:** <Callsign>, <Agency>, <TankerCallsign> is <BullseyeFix>.

#### `tanker_freq`

- **Pilot call:** <Airport> Ops, <Callsign>, say tanker frequency
- **Does:** published tanker UHF
- **Keywords:** `_ASKING` **AND** `tanker frequency` | `tanker freq` | `frequency`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, tanker frequency <UHF>.

#### `tanker_return`

- **Pilot call:** <Airport> Ops, <Callsign>, back from the tanker
- **Does:** check back in after AAR
- **Keywords:** `back from the tanker` | `off the tanker` | `done with the tanker` | `returning from the tanker` | `check back in` | `checking back in` | `back from tanker` | `off tanker`
- **Response:** <Callsign>, Blackjack, radar contact [<AlphaBullseye>]. Continue. *(or Bandsaw/other agency equivalent)*

#### `tanker_tacan`

- **Pilot call:** <Airport> Ops, <Callsign>, say TACAN
- **Does:** published tanker TACAN
- **Keywords:** `_ASKING` **AND** `tacan` | `tanker tacan` | `tanker channel`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, TACAN <Tacan>.

### Additional templates (Play / flow — no dedicated stock intent)

- **`ops_check_in`:** <Callsign>, Ops, go ahead.

## Tanker (boom) (`tanker`)

- **Spoken:** <TankerCallsign>
- **Nellis freq:** live Opus UHF AM *(typical)*
- **Address terms:** texaco, shell, arco, esso, tanker, boom

### Timeline / step calls

#### `tanker_astern`

- **Pilot call:** <TankerCallsign>, <Callsign>, astern
- **Does:** use DCS Ready pre-contact (cleared contact)
- **Keywords:** `astern` | `pre contact` | `precontact` | `pre-contact` | `left observation` | `observation` | `on the left` | `echelon left`
- **Response:** *(no SRS reply — use DCS tanker radio: Ready pre-contact)*

#### `tanker_chat_start`

- **Pilot call:** <TankerCallsign>, <Callsign>, how's it going
- **Does:** boom / reform small talk
- **Keywords:** `how's it going` | `hows it going` | `how you doing` | `how are you` | `hey boom` | `hey texaco` | `hi boom` | `you busy` | `pretty quiet` | `what's for lunch` | `whats for lunch` | `shoot the breeze` | `small talk` | `got time to talk` | `wanna chat` | `want to chat` | `start chatting`
- **Response:** *(boom opener from `THREADS`; see Boom small-talk)*

#### `tanker_chat_stop`

- **Pilot call:** <TankerCallsign>, <Callsign>, stop talking
- **Does:** end boom small talk
- **Keywords:** `stop talking` | `stop chatting` | `stop the chat` | `quit talking` | `that's enough` | `thats enough` | `talk later`
- **Response:** *(ends boom small talk)*

#### `tanker_check_in`

- **Pilot call:** <TankerCallsign>, <Callsign>, request rejoin
- **Does:** cleared rejoin left (sometimes left observation)
- **Keywords:** `request rejoin` | `request reform` | `request the rejoin` | `request the reform` | `cleared rejoin` | `rejoin left` | `reform left` | `checking in` | `with you`
- **Response:** <Callsign>, <TankerCallsign>, cleared rejoin left [| left observation].

#### `tanker_contact`

- **Pilot call:** <TankerCallsign>, <Callsign>, cleared contact
- **Does:** use DCS tanker radio for cleared contact
- **Keywords:** `contact` | `in contact` | `boom contact` | `cleared contact`
- **Veto:** `contact tower`, `contact approach`, `contact blackjack`, `contact ground`
- **Response:** *(no SRS reply — use DCS tanker radio: cleared contact)*

#### `tanker_depart`

- **Pilot call:** <TankerCallsign>, <Callsign>, going exit high, thanks for the fuel
- **Does:** boom goodbye + DCS disconnect
- **Keywords:** `request departure` | `cleared to depart` | `done with the tanker` | `exit high` | `exit low` | `going high` | `going low` | `thanks for the fuel` | `thanks for the gas` | `appreciate the fuel` | `appreciate the gas`
- **Response:** <Callsign>, <TankerCallsign>, copy, you're cleared off. Thanks for flying with us. *(variant pool)*

#### `tanker_disconnect`

- **Pilot call:** <TankerCallsign>, <Callsign>, disconnect
- **Does:** use DCS Abort refueling
- **Keywords:** `disconnect` | `request disconnect` | `coming off` | `abort refueling`
- **Response:** *(no SRS reply — use DCS tanker radio: Abort refueling)*

### Official AAR notes

- Astern / contact / disconnect are **DCS tanker radio** actions; SRS does not speak those clearances.
- After `tanker_chat_start`, boom openers and A/B choices come from `tanker_chat_library.THREADS` (see [Boom small-talk](#boom-small-talk-tanker_chat_library)).

## Other / custom (`other`)

- **Spoken:** Control
- **Nellis freq:** 251.0 AM *(typical)*
- **Address terms:** *(no address terms — mission-authored)*

### Timeline / step calls

#### `center_check_in` → template `center_check_in`

- **Pilot call:** Control, <Callsign>, checking in
- **Does:** LA Center check-in
- **Keywords:** `checking in` | `check in` | `checkin` | `with you` | `on frequency` | `radar contact`
- **Veto:** `checking out`, `check out`, `contact blackjack`, `switch blackjack`
- **Response:** <Callsign>, Los Angeles Center, radar contact. Remain this frequency.

### Ad-hoc / C2 / tanker info

#### `request_alpha_check`

- **Pilot call:** Control, <Callsign>, alpha check bullseye
- **Does:** your position off bullseye
- **Keywords:** `alpha check` | `alfa check` | `position check`
- **Response:** <Callsign>, <Agency>, alpha check <BullseyeBearing> <RangeNm>.

#### `request_bogey_dope`

- **Pilot call:** Control, <Callsign>, bogey dope
- **Does:** BRAA to the closest hostile
- **Keywords:** `bogey dope` | `bogie dope` | `braa` | `snaplock`
- **Response:** <Callsign>, <Agency>, clean. | <Callsign>, <Agency>, group <BRAA>, [<Altitude>,] [<Aspect>,] <Declaration> [, <N> contacts].

#### `request_declare`

- **Pilot call:** Control, <Callsign>, declare bullseye 056 67
- **Does:** declaration at that bullseye (ELVIS); bare declare = nearest
- **Keywords:** `declare`
- **Response:** <Callsign>, <Agency>, <hostile|bogey|friendly|clean|unable>.

#### `request_picture`

- **Pilot call:** Control, <Callsign>, request picture
- **Does:** hostile groups off the live radar
- **Keywords:** `picture` | `pitcher`
- **Response:** <Callsign>, <Agency>, picture clean. | <Callsign>, <Agency>, <PictureLabel> … <GroupClauses>.

#### `request_tanker`

- **Pilot call:** Control, <Callsign>, request tanker
- **Does:** track and BRAA to the Opus KC-135
- **Keywords:** `_ASKING` + `push` | `go` | `going` **AND** `tanker` | `texaco` | `air refuel` | `air refueling` | `aar`
- **Veto:** `tacan`, `frequency`, `freq`, `bullseye`, `back from`, `off the tanker`, `off tanker`, `returning from`, `done with`
- **Response:** <Callsign>, <Agency>, tanker <TankerCallsign>, [<Aircraft>,] [track <ARTrack>,] <BRAA> [, altitude <Alt>]. Frequency change approved.

#### `tanker_bullseye`

- **Pilot call:** Control, <Callsign>, say tanker bullseye
- **Does:** live tanker bullseye
- **Keywords:** `_ASKING` **AND** `tanker bullseye` | `bullseye`
- **Veto:** `alpha check`, `alfa check`, `declare`
- **Response:** <Callsign>, <Agency>, <TankerCallsign> is <BullseyeFix>.

#### `tanker_freq`

- **Pilot call:** Control, <Callsign>, say tanker frequency
- **Does:** published tanker UHF
- **Keywords:** `_ASKING` **AND** `tanker frequency` | `tanker freq` | `frequency`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, tanker frequency <UHF>.

#### `tanker_tacan`

- **Pilot call:** Control, <Callsign>, say TACAN
- **Does:** published tanker TACAN
- **Keywords:** `_ASKING` **AND** `tacan` | `tanker tacan` | `tanker channel`
- **Response:** <Callsign>, <Agency>, <TankerCallsign>, TACAN <Tacan>.

### Additional templates (Play / flow — no dedicated stock intent)

- **`radio_check`:** <Callsign>, <Airport>, loud and clear.
- **`center_handoff`:** <Callsign>, Los Angeles Center, contact <NextAgency> <NextFreq>.

## Mission overrides — `f2c_swll.json`

`nellis_default.json` uses built-in intents only (no `voice_phrases`).
The F2C SWLL mission adds **mission phrases** that fire the named step when due (any one phrase). Authored audio may be `mode: file` or custom `text` instead of a live template.

| Step id | Label | Channel | Voice phrases (pilot) | Agency audio |
|---------|-------|---------|----------------------|--------------|
| `la_center_check_in_682c3c` | LA Center Radar Contact Direct Jenid | `other` | `With you.` | file `11.mp3` |
| `la_center_radar_contact_direct_jenid_fbd5e9` | request | `other` | `With you.` | file `12.mp3` |
| `go_with_request_df5eb1` | Go with request copy | `other` | `With you.` | file `13.mp3` |
| `la_center_check_in_copy_d7f067` | Contct Joshua | `other` | `With you.` | file `14.mp3` |
| `bs_checkin` | Joshua Control Check In | `other` @ 348.7 | `Point Charlie` | file `15.mp3` |
| `contct_joshua_05740a` | LA Center return | `other` | `With you.` | TTS: `Bruiser Five, Los Angeles Center, Welcome Back` |
| `la_center_return_9da1ef` | LA Center descend handoff 50 miles | `other` | `With you.` | TTS: `Bruiser Five, descend Fifteen thousand feet, Contact Nellis approach two seven three decimal fife fife, Gooday!` |

Other SWLL steps on `other` / Joshua freqs use file audio without `voice_phrases` (advance via Play / HOTAS).

## Boom small-talk (`tanker_chat_library`)

After the pilot starts chat on tanker (`tanker_chat_start`), the boom speaks an **opener**; the pilot answers with a **say** line (matched by **hits**); the boom returns **reply**. Threads may nest `follow` A/B questions. Placeholders in openers: `{cs}` → `<Callsign>`, `{tcs}` → `<TankerCallsign>`.

Library size: **100** threads.

### `dunkin_starbucks` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you guys drink coffee down there all the time? Tough to keep it warm though. Dunkin or Starbucks?
- **Pilot:** Dunkin
  - **Hits:** `dunkin`, `dunkins`, `dunkin donuts`
  - **Boom reply:** Copy Dunkin. That's the real stuff. We got a Keurig up here, little shots of coffee, something nice and dark.
- **Boom opener:** Morning coffee, or you still pounding it in the afternoon?
- **Pilot:** Morning
  - **Hits:** `morning`, `am`, `breakfast`
  - **Boom reply:** Morning, copy. That's for the morning. Cappuccino after noon would blast me.
- **Pilot:** Afternoon
  - **Hits:** `afternoon`, `pm`, `later`, `evening`
  - **Boom reply:** Afternoon, copy. We're not British — you can drink it whenever you want up here.
- **Pilot:** Starbucks
  - **Hits:** `starbucks`, `star bucks`, `sbux`
  - **Boom reply:** Starbucks, copy. Fancy. We had a French press up here one time till somebody broke it. Now it's just the Keurig.
- **Boom opener:** Morning coffee, or you still pounding it in the afternoon?
- **Pilot:** Morning
  - **Hits:** `morning`, `am`, `breakfast`
  - **Boom reply:** Morning, copy. Dark in the morning, lighter later if you can get it.
- **Pilot:** Afternoon
  - **Hits:** `afternoon`, `pm`, `later`, `evening`
  - **Boom reply:** Afternoon, copy. That's a late-day cappuccino — respect.

### `keep_warm` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you able to keep a cup of coffee warm down there, or does the jet just cook it?
- **Pilot:** By the radar
  - **Hits:** `radar`, `fireball`, `up front`, `cockpit`
  - **Boom reply:** Copy, by the radar. Giant fireball, that tracks. We stash ours up front by this big radar of our own.
- **Pilot:** It's cold
  - **Hits:** `cold`, `ice`, `never warm`
  - **Boom reply:** Copy, ice coffee by accident. That's a Viper problem. We'd freeze too if this thing ever shut up.

### `lunch` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, what are you serving down there today — Pop-Tarts or peanut butter and jelly?
- **Pilot:** Pop-Tarts
  - **Hits:** `pop tart`, `poptart`, `pop-tarts`, `toaster`
  - **Boom reply:** Pop-Tarts, copy. Don't crush 'em in the bag — then it's just crumbs. Pretty tasty crumbs, though.
- **Pilot:** PB and J
  - **Hits:** `pbj`, `p b and j`, `peanut butter`, `jelly`, `sandwich`
  - **Boom reply:** PB and J, copy. That's a classic. We had chicken nuggets one time and the whole boom crew got jealous.

### `quiet` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, how you doing out there? Kind of boring from up here. Quiet, or you guys busy?
- **Pilot:** Quiet
  - **Hits:** `quiet`, `boring`, `slow`, `all quiet`, `dead`
  - **Boom reply:** Copy, all quiet on the western front. Good day for gas.
- **Pilot:** Busy
  - **Hits:** `busy`, `working`, `spiked`, `interesting`
  - **Boom reply:** Busy, copy. We'll keep you on the boom and stay out of the way.

### `keurig` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, we got a Keurig up here today. You guys running a French press, or just the gas station stuff?
- **Pilot:** Keurig
  - **Hits:** `keurig`, `k cup`, `pods`, `pod`
  - **Boom reply:** Keurig, copy. Fancy coffee. Little espresso shot if you can get it.
- **Pilot:** French press
  - **Hits:** `french press`, `press`, `cold brew`, `cold press`
  - **Boom reply:** French press, copy. Ours broke. No French press, no cold press — just hot breath and the Keurig.

### `boom_feel` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, how you feeling on the boom? Looking stable from here.
- **Pilot:** Looks good
  - **Hits:** `good`, `stable`, `fine`, `great`, `easy`, `smooth`
  - **Boom reply:** Copy, looking good. Sit there and drink your coffee.
- **Pilot:** A little work
  - **Hits:** `work`, `rough`, `bumpy`, `fighting`, `heavy`
  - **Boom reply:** Copy, a little work. We'll hold her steady. You're doing fine.

### `nuggets` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, random question while you're hanging out. Chicken nuggets or a protein bar?
- **Pilot:** Nuggets
  - **Hits:** `nugget`, `nuggets`, `chicken`
  - **Boom reply:** Nuggets, copy. That's a boom-crew meal. Don't tell the dietitian.
- **Pilot:** Protein bar
  - **Hits:** `protein`, `bar`, `clif`, `cliff`, `healthy`
  - **Boom reply:** Protein bar, copy. Responsible. We respect that and then eat the nuggets anyway.

### `tacos_burgers` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, serious boom poll. After this, tacos or a burger?
- **Pilot:** Tacos
  - **Hits:** `taco`, `tacos`
  - **Boom reply:** Tacos, copy. Correct answer. We'll be jealous from twenty-six thousand.
- **Pilot:** Burger
  - **Hits:** `burger`, `burgers`, `cheeseburger`
  - **Boom reply:** Burger, copy. That's a recovery meal. Get two. Boom operator's orders.

### `dogs_cats` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, important question. Dogs or cats?
- **Pilot:** Dogs
  - **Hits:** `dog`, `dogs`, `puppy`
  - **Boom reply:** Dogs, copy. They'd love this view. Also they'd steal the left seat.
- **Pilot:** Cats
  - **Hits:** `cat`, `cats`, `kitten`
  - **Boom reply:** Cats, copy. They'd ignore the whole sortie and sleep on the HUD.

### `pizza` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, pizza topping hill to die on. Pepperoni or pineapple?
- **Pilot:** Pepperoni
  - **Hits:** `pepperoni`, `pep`
  - **Boom reply:** Pepperoni, copy. That's the approved loadout. No notes.
- **Pilot:** Pineapple
  - **Hits:** `pineapple`, `hawaiian`, `ham`
  - **Boom reply:** Pineapple, copy. Bold. The boom crew is divided. I'm not taking sides on open mic.

### `energy` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, what are you running on down there — energy drink or just spite?
- **Pilot:** Energy drink
  - **Hits:** `energy`, `monster`, `red bull`, `redbull`, `bang`, `celsius`
  - **Boom reply:** Energy drink, copy. Don't shake it. We do not want a foam show on the boom.
- **Pilot:** Spite
  - **Hits:** `spite`, `hate`, `anger`, `vibes`, `nothing`
  - **Boom reply:** Spite, copy. That's sustainable fuel. Zero calories, unlimited range.

### `naps` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, honest question. Could you nap in that cockpit, or is it all elbows?
- **Pilot:** Could nap
  - **Hits:** `nap`, `sleep`, `could`, `yes`
  - **Boom reply:** Could nap, copy. Respect. Autopilot and a dream. We'll keep the boom quiet.
- **Pilot:** No chance
  - **Hits:** `no`, `can't`, `cannot`, `elbows`, `awake`
  - **Boom reply:** No chance, copy. We have a bunk. Not bragging. Okay a little bragging.

### `view` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, who has the better view right now — you looking up at us, or us looking down at the whole desert?
- **Pilot:** You do
  - **Hits:** `you`, `yours`, `up there`
  - **Boom reply:** We do, copy. Big windows, bad coffee, better scenery. Fair trade.
- **Pilot:** We do
  - **Hits:** `we`, `ours`, `viper`, `down here`
  - **Boom reply:** You do, copy. Looking up at a flying gas station is a vibe. We'll allow it.

### `bathroom` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, awkward boom question. You thinking about the bathroom yet, or are we still professional?
- **Pilot:** Still professional
  - **Hits:** `professional`, `fine`, `no`
  - **Boom reply:** Still professional, copy. That's a lie and we both know it. Hang in there.
- **Pilot:** Thinking about it
  - **Hits:** `thinking`, `bathroom`, `yes`, `hurry`
  - **Boom reply:** Thinking about it, copy. We have a toilet. Not bragging. Definitely bragging.

### `music` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, playlist check. Country or rock while you hang on the boom?
- **Pilot:** Country
  - **Hits:** `country`, `nashville`, `brooks`
  - **Boom reply:** Country, copy. We'll hum something slow so you don't start dancing on the boom.
- **Pilot:** Rock
  - **Hits:** `rock`, `metal`, `guitar`
  - **Boom reply:** Rock, copy. Keep it in your head. If you start headbanging we have to call a breakaway.

### `monday_friday` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, does this feel like a Monday sortie or a Friday sortie?
- **Pilot:** Monday
  - **Hits:** `monday`, `monday's`
  - **Boom reply:** Monday, copy. That tracks. Coffee's weaker and the boom feels longer.
- **Pilot:** Friday
  - **Hits:** `friday`, `weekend`
  - **Boom reply:** Friday, copy. Get your gas and go home. Boom crew is already mentally at the grill.

### `beach_mountains` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, after you land, beach or mountains?
- **Pilot:** Beach
  - **Hits:** `beach`, `ocean`, `sand`
  - **Boom reply:** Beach, copy. Sand in everything. Still better than a G-suit.
- **Pilot:** Mountains
  - **Hits:** `mountain`, `mountains`, `hike`
  - **Boom reply:** Mountains, copy. Thin air, you already trained for that. Smart.

### `wings` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, wings heat check. Mild or you actually want to suffer?
- **Pilot:** Mild
  - **Hits:** `mild`, `ranch`
  - **Boom reply:** Mild, copy. That's the adult choice. We still respect the buffalo.
- **Pilot:** Hot
  - **Hits:** `hot`, `spicy`, `suicide`, `extra`
  - **Boom reply:** Hot, copy. Write that down. If you break away later we're blaming the sauce.

### `ice_cream` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, ice cream diplomacy. Chocolate or vanilla?
- **Pilot:** Chocolate
  - **Hits:** `chocolate`, `choc`
  - **Boom reply:** Chocolate, copy. Correct. Vanilla people can still sit with us.
- **Pilot:** Vanilla
  - **Hits:** `vanilla`
  - **Boom reply:** Vanilla, copy. Don't let them tell you it's boring. It's a classic like a Viper.

### `burrito` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, breakfast of champions. Breakfast burrito or just coffee and bad decisions?
- **Pilot:** Burrito
  - **Hits:** `burrito`, `breakfast`
  - **Boom reply:** Burrito, copy. That's a preflight. Hope you didn't drop it in the map case.
- **Pilot:** Coffee
  - **Hits:** `coffee`, `decisions`, `just coffee`
  - **Boom reply:** Coffee, copy. Bad decisions, copy. That's a full fuel load.

### `gatorade` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, hydration check. Gatorade or water like a responsible adult?
- **Pilot:** Gatorade
  - **Hits:** `gatorade`, `gator`, `electrolyte`
  - **Boom reply:** Gatorade, copy. Pick a color that matches the jet. We're watching.
- **Pilot:** Water
  - **Hits:** `water`, `h2o`
  - **Boom reply:** Water, copy. Boring and correct. Boom crew is drinking something that glows.

### `truck_car` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, what do you drive to the squadron — a truck, or something that fits in a hangar?
- **Pilot:** Truck
  - **Hits:** `truck`, `pickup`, `f150`, `f-150`
  - **Boom reply:** Truck, copy. Compensating for the tiny cockpit. We get it.
- **Pilot:** Car
  - **Hits:** `car`, `sedan`, `civic`, `small`
  - **Boom reply:** Car, copy. Fast on the ground too. Don't get a ticket after a tanker hop, that's embarrassing.

### `early_late` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, are you an early bird or did we just ruin your morning?
- **Pilot:** Early bird
  - **Hits:** `early`, `morning person`, `bird`
  - **Boom reply:** Early bird, copy. That's why you're already on the boom and we're still yawning.
- **Pilot:** Night owl
  - **Hits:** `night`, `owl`, `late`, `ruined`
  - **Boom reply:** Night owl, copy. Sorry about the sunrise. We'll try to be interesting.

### `books_podcasts` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, if you could listen to something on this boom besides us. Book or podcast?
- **Pilot:** Book
  - **Hits:** `book`, `audiobook`, `novel`
  - **Boom reply:** Book, copy. Audiobook. Don't turn pages in the cockpit, that's how you lose a checklist.
- **Pilot:** Podcast
  - **Hits:** `podcast`, `show`
  - **Boom reply:** Podcast, copy. Two guys talking about nothing. Wait — that's us.

### `grill_smoker` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, weekend cooking. Grill or smoker?
- **Pilot:** Grill
  - **Hits:** `grill`, `grilling`, `propane`
  - **Boom reply:** Grill, copy. Fast and honest. Like a Viper takeoff.
- **Pilot:** Smoker
  - **Hits:** `smoker`, `brisket`, `smoke`
  - **Boom reply:** Smoker, copy. That's a six-hour tanker track. Respect the low and slow.

### `fishing_hunting` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, days off. Fishing or hunting?
- **Pilot:** Fishing
  - **Hits:** `fishing`, `fish`, `bass`
  - **Boom reply:** Fishing, copy. Sitting, waiting, hoping something takes the bait. That's boom work too.
- **Pilot:** Hunting
  - **Hits:** `hunting`, `hunt`, `deer`
  - **Boom reply:** Hunting, copy. We'd make a joke about being the prey up here but you're on the boom so we'll behave.

### `socks` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, fashion report. Matching socks today, or fighter-pilot chaos?
- **Pilot:** Matching
  - **Hits:** `matching`, `match`, `same`
  - **Boom reply:** Matching, copy. Overachiever. The boom crew is impressed and a little scared.
- **Pilot:** Chaos
  - **Hits:** `chaos`, `mismatch`, `different`, `random`
  - **Boom reply:** Chaos, copy. One olive drab, one mystery. That's a combat loadout.

### `sunglasses` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, those sunglasses — high speed, or did you steal them from a gas station?
- **Pilot:** High speed
  - **Hits:** `high speed`, `cool`, `aviator`, `oakley`
  - **Boom reply:** High speed, copy. Don't drop them in the seat. That's a hundred-dollar breakaway.
- **Pilot:** Gas station
  - **Hits:** `gas station`, `cheap`, `stole`, `walmart`
  - **Boom reply:** Gas station, copy. Honest. They probably work better than ours.

### `autopilot` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you hand-flying this whole time or is George doing the work?
- **Pilot:** Hand flying
  - **Hits:** `hand`, `hands`, `manual`, `flying`
  - **Boom reply:** Hand flying, copy. That's why you look so smooth. Or so busy. One of those.
- **Pilot:** George
  - **Hits:** `george`, `autopilot`, `auto`, `ap`
  - **Boom reply:** George, copy. Let the computer suffer. We'll still give you the credit.

### `jp8` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, favorite smell. Jet fuel, or the coffee we spilled in the galley?
- **Pilot:** Jet fuel
  - **Hits:** `jet fuel`, `jp8`, `jp-8`, `gas`, `fuel`
  - **Boom reply:** Jet fuel, copy. That's a lifestyle. Don't sniff too hard, we need you conscious.
- **Pilot:** Coffee
  - **Hits:** `coffee`, `galley`, `k cup`
  - **Boom reply:** Coffee, copy. Ours smells like regret and a burned K-cup. Still better than the latrine.

### `maps` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you still carry a paper map for luck, or is it all glass now?
- **Pilot:** Paper
  - **Hits:** `paper`, `map`, `chart`
  - **Boom reply:** Paper, copy. Old school. If the glass dies you can still find Nevada. Probably.
- **Pilot:** Glass
  - **Hits:** `glass`, `mfd`, `digital`, `ipad`
  - **Boom reply:** Glass, copy. High speed. Don't draw on it with a grease pencil. We've seen that.

### `sunset` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, that sunset out there — you seeing it, or are you staring at our belly?
- **Pilot:** Sunset
  - **Hits:** `sunset`, `pretty`, `seeing it`
  - **Boom reply:** Sunset, copy. Pretty. Don't fly into it. That's how ballads start.
- **Pilot:** Your belly
  - **Hits:** `belly`, `gray`, `grey`
  - **Boom reply:** Our belly, copy. That's a lot of gray. We'll try to be interesting.

### `lottery` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, if we all won the lottery tomorrow, would you still show up for this, or buy an island?
- **Pilot:** Still show
  - **Hits:** `show`, `still`, `fly`
  - **Boom reply:** Still show, copy. That's the sickness. We'd buy a nicer tanker and do this for fun.
- **Pilot:** Island
  - **Hits:** `island`, `beach`, `quit`
  - **Boom reply:** Island, copy. Send coordinates. Boom crew is requesting a TDY.

### `video_games` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, after you shut down. Video games, or are you too high-speed for that?
- **Pilot:** Games
  - **Hits:** `games`, `game`, `xbox`, `playstation`, `pc`
  - **Boom reply:** Games, copy. Don't fly the Viper in the game tonight. Give it a rest.
- **Pilot:** Too high speed
  - **Hits:** `high speed`, `sleep`, `no`, `adult`
  - **Boom reply:** Too high speed, copy. You'll be asleep in the truck. We believe you.

### `rain` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you hoping it rains when you recover, or do you want a dry runway and a cold one?
- **Pilot:** Rain
  - **Hits:** `rain`, `wet`
  - **Boom reply:** Rain, copy. Dramatic. Don't hydroplane. We're not coming back for you.
- **Pilot:** Dry
  - **Hits:** `dry`, `cold one`, `beer`
  - **Boom reply:** Dry, copy. Professional. Cold one after, also professional. Boom approved.

### `bacon` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, breakfast ethics. Bacon on everything, or are you one of those yogurt people?
- **Pilot:** Bacon
  - **Hits:** `bacon`
  - **Boom reply:** Bacon, copy. That's a valid religion. Amen from the boom.
- **Pilot:** Yogurt
  - **Hits:** `yogurt`, `yoghurt`, `healthy`, `granola`
  - **Boom reply:** Yogurt, copy. We don't trust it, but we admire the discipline.

### `hot_sauce` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you keep hot sauce in the jet, or is that a myth?
- **Pilot:** Keep it
  - **Hits:** `keep`, `yes`, `tabasco`, `sauce`
  - **Boom reply:** Keep it, copy. Legend. If you drop the bottle we're writing a mishap report.
- **Pilot:** Myth
  - **Hits:** `myth`, `no`, `don't`
  - **Boom reply:** Myth, copy. Disappointing. Boom crew believed in you.

### `left_right` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, in the Viper, you ever wish you had a copilot to complain to, or is solo the whole point?
- **Pilot:** Want a copilot
  - **Hits:** `copilot`, `want`, `someone`
  - **Boom reply:** Want a copilot, copy. We have like eight. They all talk. It's not better.
- **Pilot:** Solo
  - **Hits:** `solo`, `alone`, `point`
  - **Boom reply:** Solo, copy. That's the point. We'll stop talking in a minute. Probably.

### `stick_shift` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, cars. Stick shift, or you let the car think for you too?
- **Pilot:** Stick
  - **Hits:** `stick`, `manual`, `clutch`
  - **Boom reply:** Stick, copy. Old school. Don't stall it in the parking lot after a tanker hop.
- **Pilot:** Automatic
  - **Hits:** `automatic`, `auto`, `slushbox`
  - **Boom reply:** Automatic, copy. Save the skills for the jet. We support this.

### `camping` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, time off. Tent camping, or a hotel with actual water pressure?
- **Pilot:** Camping
  - **Hits:** `camping`, `tent`, `camp`
  - **Boom reply:** Camping, copy. You already sit in a tiny seat for hours. Glutton for punishment.
- **Pilot:** Hotel
  - **Hits:** `hotel`, `motel`, `water`
  - **Boom reply:** Hotel, copy. Water pressure. Boom crew is jealous. Our shower is a rumor.

### `ufo` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you seeing anything weird out there besides us, or is the sky behaving?
- **Pilot:** Sky's fine
  - **Hits:** `fine`, `behaving`, `nothing`, `quiet`
  - **Boom reply:** Sky's fine, copy. Boring. We'll be your unidentified flying object for today.
- **Pilot:** Something weird
  - **Hits:** `weird`, `ufo`, `lights`, `something`
  - **Boom reply:** Something weird, copy. If it's not us, don't chase it. That's how documentaries start.

### `superstition` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, preflight superstition. Lucky patch, or you fly dirty?
- **Pilot:** Lucky patch
  - **Hits:** `lucky`, `patch`, `ritual`, `charm`
  - **Boom reply:** Lucky patch, copy. Don't tell the ops officer it's load-bearing.
- **Pilot:** Fly dirty
  - **Hits:** `dirty`, `nothing`, `nope`, `forgot`
  - **Boom reply:** Fly dirty, copy. Confidence. Or you forgot the patch in the truck. We'll allow either.

### `callsign` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you like your callsign, or are you still mad about how you got it?
- **Pilot:** I like it
  - **Hits:** `like`, `love`, `good`
  - **Boom reply:** You like it, copy. That's rare. Don't tell the naming committee or they'll change it.
- **Pilot:** Still mad
  - **Hits:** `mad`, `hate`, `stupid`, `story`
  - **Boom reply:** Still mad, copy. That's how you know it's a real callsign. Ours is Texaco. We lost that fight.

### `holding_hands` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you realize we're basically holding hands at two hundred knots, right?
- **Pilot:** Don't say that
  - **Hits:** `don't`, `dont`, `stop`, `no`
  - **Boom reply:** Don't say that, copy. Too late. It's on the tape. Stay stable, sweetheart.
- **Pilot:** Copy
  - **Hits:** `copy`, `yeah`, `true`, `roger`
  - **Boom reply:** Copy, copy. Look at us. Two professionals. One hose. Beautiful.

### `altitude` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you happier down in the weeds, or is the high thirties growing on you?
- **Pilot:** Weeds
  - **Hits:** `weeds`, `low`, `down`, `nap`
  - **Boom reply:** Weeds, copy. Fast and low. We'll stay up here with the coffee and the lawsuits.
- **Pilot:** High
  - **Hits:** `high`, `thirties`, `up`, `cruise`
  - **Boom reply:** High, copy. Thin air, fat gas. That's the life. Don't get used to our speed.

### `snacks_share` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, if we could lower a bag of chips down the boom, would you take it, or is that a FOD hazard?
- **Pilot:** I'd take it
  - **Hits:** `take`, `chips`, `yes`
  - **Boom reply:** You'd take it, copy. Noted. Legal says no. Morale says maybe. Legal wins.
- **Pilot:** FOD hazard
  - **Hits:** `fod`, `hazard`, `no`
  - **Boom reply:** FOD hazard, copy. Responsible. That's why you're on the boom and not in the snack pile.

### `sports` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you following any sports, or is that a peacetime hobby?
- **Pilot:** Following
  - **Hits:** `following`, `sports`, `football`, `yes`
  - **Boom reply:** Following, copy. Don't tell us the score. We have a guy who gets violent.
- **Pilot:** Peacetime
  - **Hits:** `peacetime`, `no`, `flying`
  - **Boom reply:** Peacetime, copy. Flying is the sport. We're the bench. Get your gas.

### `movies` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, tanker movie night. Top Gun, or something where the tanker actually lands the girl?
- **Pilot:** Top Gun
  - **Hits:** `top gun`, `maverick`, `gun`
  - **Boom reply:** Top Gun, copy. They never show the boom. That's how you know it's fiction.
- **Pilot:** Tanker movie
  - **Hits:** `hollywood`, `other`, `real`
  - **Boom reply:** Tanker movie, copy. Hasn't been made. Hollywood's scared of how cool we are.

### `donuts` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, donut diplomacy. Glazed, or are you a filled-donut person?
- **Pilot:** Glazed
  - **Hits:** `glazed`, `glaze`
  - **Boom reply:** Glazed, copy. Simple. Effective. Like a good rejoin.
- **Pilot:** Filled
  - **Hits:** `filled`, `cream`, `jelly`
  - **Boom reply:** Filled, copy. High risk, high reward. Don't wear it. We can see your visor.

### `water_pressure` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, after you land, first stop. Shower, or the snack bar?
- **Pilot:** Shower
  - **Hits:** `shower`, `bath`
  - **Boom reply:** Shower, copy. You've earned it. We smell like JP-8 and burnt coffee. Same plan.
- **Pilot:** Snack bar
  - **Hits:** `snack`, `food`, `bar`
  - **Boom reply:** Snack bar, copy. Priorities. Gas the jet, gas the pilot. We support this doctrine.

### `window_aisle` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, if this tanker was an airliner, window or aisle?
- **Pilot:** Window
  - **Hits:** `window`
  - **Boom reply:** Window, copy. That's us. Big windows, no pretzels, occasional fighter hanging off the wing.
- **Pilot:** Aisle
  - **Hits:** `aisle`, `isle`
  - **Boom reply:** Aisle, copy. Bathroom access. That's the real luxury up here.

### `coffee_black` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, coffee. Black, or you putting enough cream in it to make it a dessert?
- **Pilot:** Black
  - **Hits:** `black`, `straight`
  - **Boom reply:** Black, copy. That's a briefing. No notes. Carry on.
- **Pilot:** Cream
  - **Hits:** `cream`, `sugar`, `sweet`, `dessert`
  - **Boom reply:** Cream, copy. Dessert coffee. We won't tell the other Vipers.

### `last_name` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you want us to keep using the callsign, or you got a first name we can ruin?
- **Pilot:** Callsign
  - **Hits:** `callsign`, `keep`
  - **Boom reply:** Callsign, copy. Professional. We'll still invent a worse one in the debrief.
- **Pilot:** First name
  - **Hits:** `first`, `name`, `steve`
  - **Boom reply:** First name, copy. Dangerous. We'll forget it immediately and go back to Fleece.

### `boredom` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, rank this. More boring — waiting on the boom, or waiting in the hold?
- **Pilot:** The boom
  - **Hits:** `boom`, `this`
  - **Boom reply:** The boom, copy. Ouch. We'll try a dance. Don't join us.
- **Pilot:** The hold
  - **Hits:** `hold`, `holding`, `orbit`
  - **Boom reply:** The hold, copy. Correct. At least here you get free gas and a conversation.

### `selfie` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, if GoPros were allowed, would you film this, or is it too embarrassing?
- **Pilot:** I'd film it
  - **Hits:** `film`, `gopro`, `yes`
  - **Boom reply:** You'd film it, copy. Tag us. Boom crew wants residuals.
- **Pilot:** Embarrassing
  - **Hits:** `embarrassing`, `no`, `don't`
  - **Boom reply:** Embarrassing, copy. Fair. You look like a little fish on a big hook. It's cute.

### `lucky_gas` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you treat this gas as lucky, or is it all just numbers on a tape?
- **Pilot:** Lucky
  - **Hits:** `lucky`, `luck`
  - **Boom reply:** Lucky, copy. We'll put extra wishes in the hose. That's not a real procedure. Or is it.
- **Pilot:** Numbers
  - **Hits:** `numbers`, `tape`, `math`
  - **Boom reply:** Numbers, copy. Cold. Accurate. We'll still say good luck because we're nice.

### `complain` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you allowed to complain on this freq, or is that a debrief item?
- **Pilot:** I'll complain
  - **Hits:** `complain`, `yes`, `allowed`
  - **Boom reply:** You'll complain, copy. Hit us. The boom can take it. The tape cannot. Wait.
- **Pilot:** Debrief item
  - **Hits:** `debrief`, `no`, `professional`
  - **Boom reply:** Debrief item, copy. Professional. We'll complain for you. The coffee's cold.

### `cookies` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, cookie crisis. Chocolate chip, or you one of those oatmeal people?
- **Pilot:** Chocolate chip
  - **Hits:** `chocolate`, `chip`, `chips`
  - **Boom reply:** Chocolate chip, copy. That's a valid life. Don't crumble them in the G-suit.
- **Pilot:** Oatmeal
  - **Hits:** `oatmeal`, `raisin`
  - **Boom reply:** Oatmeal, copy. We respect the fiber. Boom crew is still stealing the chocolate ones.

### `left_seat` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, in a two-seat jet, you grabbing left seat, or you like the maps?
- **Pilot:** Left seat
  - **Hits:** `left`, `front`, `pilot`
  - **Boom reply:** Left seat, copy. Front office. Don't make us ride in the back, we get airsick in fighters.
- **Pilot:** Maps
  - **Hits:** `maps`, `wso`, `back`, `right`
  - **Boom reply:** Maps, copy. That's the smart seat. Somebody's got to know where Nevada went.

### `ketchup_mustard` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, burger toppings. Ketchup, or mustard like a chaotic good?
- **Pilot:** Ketchup
  - **Hits:** `ketchup`, `catsup`
  - **Boom reply:** Ketchup, copy. Classic. Don't get it on the mask. That's a visor emergency.
- **Pilot:** Mustard
  - **Hits:** `mustard`, `yellow`
  - **Boom reply:** Mustard, copy. Bold yellow energy. We can work with that.

### `winter_summer` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you a winter flyer or a summer flyer? Be honest.
- **Pilot:** Winter
  - **Hits:** `winter`, `cold`, `snow`
  - **Boom reply:** Winter, copy. Long johns and a dream. The boom likes the smooth air though.
- **Pilot:** Summer
  - **Hits:** `summer`, `hot`, `heat`
  - **Boom reply:** Summer, copy. Cook yourself in the cockpit, then come ask us for gas. Fair.

### `alarm` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, this morning. Did the alarm win, or did you bargain with snooze?
- **Pilot:** Alarm won
  - **Hits:** `alarm`, `won`, `up`
  - **Boom reply:** Alarm won, copy. That's why you're already on the boom. Overachiever.
- **Pilot:** Snooze
  - **Hits:** `snooze`, `slept`, `late`
  - **Boom reply:** Snooze, copy. Same. We bargained. The snooze lost on round four.

### `pickle` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, pickle on the burger — yes, or are you a coward?
- **Pilot:** Pickle
  - **Hits:** `pickle`, `pickles`, `yes`
  - **Boom reply:** Pickle, copy. Correct. That's a complete weapon system.
- **Pilot:** No pickle
  - **Hits:** `no`, `without`, `coward`
  - **Boom reply:** No pickle, copy. We'll allow it. Quietly judging from the boom pod.

### `radio` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, you actually like talking on the radio, or is this painful?
- **Pilot:** I like it
  - **Hits:** `like`, `love`, `yes`
  - **Boom reply:** You like it, copy. Dangerous. We'll keep feeding you questions.
- **Pilot:** Painful
  - **Hits:** `painful`, `hate`, `no`
  - **Boom reply:** Painful, copy. Same. We'll shut up after this. No we won't.

### `halloween` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, Halloween. You dressing up, or is the flight suit already a costume?
- **Pilot:** Dressing up
  - **Hits:** `dressing`, `costume`, `yes`
  - **Boom reply:** Dressing up, copy. Send photos. Boom crew votes. We are not kind.
- **Pilot:** Flight suit
  - **Hits:** `flight suit`, `already`, `suit`
  - **Boom reply:** Flight suit, copy. That's a year-round costume. High speed, low effort.

### `coffee_size` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, coffee size. Normal cup, or one of those buckets they sell now?
- **Pilot:** Normal
  - **Hits:** `normal`, `small`, `cup`
  - **Boom reply:** Normal, copy. That's a professional. We respect the twelve-ounce life.
- **Pilot:** Bucket
  - **Hits:** `bucket`, `venti`, `large`, `huge`
  - **Boom reply:** Bucket, copy. That's a tanker of coffee. We see you. Kindred spirits.

### `left_or_right` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, random. You hang the mask on the left, or the right, or is it chaos?
- **Pilot:** Left
  - **Hits:** `left`
  - **Boom reply:** Left, copy. We'll log that. For science. And gossip.
- **Pilot:** Right
  - **Hits:** `right`
  - **Boom reply:** Right, copy. The other half of the squadron just lost a bet.

### `smooth_air` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, air feel. Smooth, or are we all riding a dirt road up here?
- **Pilot:** Smooth
  - **Hits:** `smooth`, `fine`, `good`
  - **Boom reply:** Smooth, copy. Don't jinx it. We just jinxed it. Sorry.
- **Pilot:** Dirt road
  - **Hits:** `dirt`, `rough`, `bumpy`, `chop`
  - **Boom reply:** Dirt road, copy. We'll hold her as still as this old jet allows.

### `mascot` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, if this tanker needed a mascot. Bear, or a slightly judgmental eagle?
- **Pilot:** Bear
  - **Hits:** `bear`
  - **Boom reply:** Bear, copy. Big, slow, full of gas. That's on the nose and we accept it.
- **Pilot:** Eagle
  - **Hits:** `eagle`, `bird`
  - **Boom reply:** Eagle, copy. Judgmental. That's the boom operator. Hi.

### `leftovers` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, dinner last night. Leftovers, or you cooked like a civilian?
- **Pilot:** Leftovers
  - **Hits:** `leftovers`, `left overs`, `leftover`
  - **Boom reply:** Leftovers, copy. That's a fighter-pilot meal plan. Efficient and mysterious.
- **Pilot:** I cooked
  - **Hits:** `cooked`, `cook`, `civilian`
  - **Boom reply:** You cooked, copy. Show-off. What was it, eggs and optimism?

### `window_scratch` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, your canopy. Crystal, or you flying around with a bug cemetery up front?
- **Pilot:** Crystal
  - **Hits:** `crystal`, `clean`, `clear`
  - **Boom reply:** Crystal, copy. Overachiever. Don't let us sneeze on it.
- **Pilot:** Bugs
  - **Hits:** `bugs`, `bug`, `dirty`, `cemetery`
  - **Boom reply:** Bugs, copy. That's a war record. Leave it. It's character.

### `call_mom` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, after you land, you calling home, or going straight to the snack bar?
- **Pilot:** Calling home
  - **Hits:** `home`, `mom`, `calling`, `call`
  - **Boom reply:** Calling home, copy. That's a good human. Tell them the boom says hi.
- **Pilot:** Snack bar
  - **Hits:** `snack`, `food`, `bar`
  - **Boom reply:** Snack bar, copy. Honest. Fuel the pilot first. We support this doctrine.

### `desert_glow` (riff)

- **Boom opener:** Man, the desert looks like a glowing parking lot from up here. Whole valley just sitting there like it paid rent.
- **Pilot:** Pretty
  - **Hits:** `pretty`, `beautiful`, `nice`, `wow`
  - **Boom reply:** Pretty, copy. Big windows, bad coffee, better scenery. Fair trade.
- **Pilot:** Boring
  - **Hits:** `boring`, `meh`, `whatever`, `same`
  - **Boom reply:** Boring, copy. Yeah, after the tenth orbit it's just brown. Still beats the paperwork.

### `boom_coffee_story` (riff)

- **Boom opener:** Random boom fact while you're hanging out — somebody brought a French press up here once. Lasted three sorties. Then gravity won. Now it's Keurig and denial.
- **Pilot:** Classic
  - **Hits:** `classic`, `lol`, `haha`, `ha`, `funny`
  - **Boom reply:** Classic, copy. We still talk about that press like it was a fallen hero.

### `quiet_night` (riff)

- **Boom opener:** Kinda quiet on the frequency tonight. Just us, the boom, and whatever song the navigator is humming that is definitely not in key.
- **Pilot:** I hear it
  - **Hits:** `hear`, `hearing`, `song`, `music`
  - **Boom reply:** You hear it, copy. Tell him we said stop. He will not stop.
- **Pilot:** Peaceful
  - **Hits:** `peaceful`, `quiet`, `nice`, `calm`
  - **Boom reply:** Peaceful, copy. Good day for gas. Don't jinx it.

### `stable_looking` (riff)

- **Boom opener:** You're looking real stable from here. Sit there, drink whatever counts as coffee in a Viper, and let us do the boring part.

### `snack_jealousy` (riff)

- **Boom opener:** Not gonna lie — if you've got a Pop-Tart down there, the boom crew is jealous. We can smell crumbs through the radio. Science.
- **Pilot:** No crumbs
  - **Hits:** `no`, `none`, `empty`, `out`
  - **Boom reply:** No crumbs, copy. Responsible. We respect that and then eat yours in our heads.
- **Pilot:** Got one
  - **Hits:** `got`, `have`, `yes`, `pop`, `tart`
  - **Boom reply:** Got one, copy. Protect it. Do not crush it in the bag. That's a war crime.

### `orbit_thoughts` (riff)

- **Boom opener:** We've been drawing circles up here so long the autopilot filed for overtime. You're the entertainment. Try not to make it too interesting.

### `window_seat` (riff)

- **Boom opener:** Best seat on the jet is the boom — terrible coffee, excellent gossip, and a free show every time somebody joins. You're doing great, by the way.
- **Pilot:** Thanks
  - **Hits:** `thanks`, `thank`, `appreciate`, `cheers`
  - **Boom reply:** You bet. Keep it easy. We'll keep her steady.

### `weirdest_cockpit` (open)

- **Boom opener:** Open question while the gas is flowing — weirdest thing currently in your cockpit. Go.
- **Pilot:** Snacks
  - **Hits:** `snack`, `snacks`, `food`, `bar`, `tart`, `nugget`
  - **Boom reply:** Snacks, copy. That's not weird, that's survival. Approved loadout.
- **Pilot:** Nothing
  - **Hits:** `nothing`, `empty`, `clean`, `none`
  - **Boom reply:** Nothing, copy. Clean cockpit energy. Respect. Also suspicious.
- **Pilot:** Phone
  - **Hits:** `phone`, `cell`, `iphone`
  - **Boom reply:** Phone, copy. Don't drop it. We are not diving for a phone.

### `after_this` (open)

- **Boom opener:** After this, what's the move — food, sleep, or pretending you still have hobbies?
- **Pilot:** Food
  - **Hits:** `food`, `eat`, `hungry`, `taco`, `burger`, `pizza`
  - **Boom reply:** Food, copy. Correct priority. Tell the snack bar the boom sent you.
- **Pilot:** Sleep
  - **Hits:** `sleep`, `nap`, `bed`, `rack`, `tired`
  - **Boom reply:** Sleep, copy. Autopilot and a dream. We'll keep the boom quiet.
- **Pilot:** Hobbies
  - **Hits:** `hobby`, `hobbies`, `pretend`, `gaming`, `gym`
  - **Boom reply:** Hobbies, copy. Bold claim at this hour. We believe in you for about ten minutes.

### `song_stuck` (open)

- **Boom opener:** What's the song stuck in your head right now? Be honest. The boom already knows if it's bad.
- **Pilot:** Nothing
  - **Hits:** `nothing`, `none`, `blank`, `empty`
  - **Boom reply:** Nothing, copy. Lucky. Ours is the navigator's off-key humming on loop.
- **Pilot:** Country
  - **Hits:** `country`, `nashville`
  - **Boom reply:** Country, copy. That's a long orbit song. Approved.
- **Pilot:** Metal
  - **Hits:** `metal`, `rock`, `heavy`
  - **Boom reply:** Metal, copy. Keep the volume where we can't hear the headbanging on the boom.

### `best_gas_station` (open)

- **Boom opener:** Serious research question — best gas station snack of all time. We need data.
- **Pilot:** Jerky
  - **Hits:** `jerky`, `beef`
  - **Boom reply:** Jerky, copy. Classic boom fuel. Chewy. Dependable. Judgmental.
- **Pilot:** Candy
  - **Hits:** `candy`, `chocolate`, `skittles`, `sour`
  - **Boom reply:** Candy, copy. Sugar and spite. Sustainable.
- **Pilot:** Chips
  - **Hits:** `chips`, `chip`, `doritos`, `lays`
  - **Boom reply:** Chips, copy. Loud bag, zero stealth. We still support this.

### `weather_report` (riff)

- **Boom opener:** Unofficial weather from twenty-six thousand — clear, a little bumpy on the edges, and one hundred percent chance of somebody asking about coffee again.

### `almost_done` (riff)

- **Boom opener:** You're almost topped off from what we can see. Hang out, stay boring, and we'll get you back to collecting stories for the debrief.
- **Pilot:** Copy
  - **Hits:** `copy`, `roger`, `wilco`
  - **Boom reply:** Copy. Looking good. Almost there.

### `navy_or_air_force` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, serious boom poll. Who's harder to gas — Navy guys, or you lot?
- **Pilot:** Navy
  - **Hits:** `navy`, `naval`, `boat`, `carrier`, `hornet`, `super hornet`
  - **Boom reply:** Navy, copy. Correct. They show up like it's a boat and we're a floating pier. You're doing fine. Don't tell them we said that.
- **Pilot:** Us
  - **Hits:** `us`, `vipers`, `viper`, `air force`, `af`, `me`
  - **Boom reply:** Us, copy. Honesty. Rare on this freq. Still easier than a Mudhen who brought the whole jet and half the squadron's feelings.

### `mudhen_or_eagle` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, boom crew argument. Who complains more on the radio — Mudhens or Eagles?
- **Pilot:** Mudhens
  - **Hits:** `mudhen`, `mudhens`, `strike eagle`, `f fifteen e`, `fifteen e`
  - **Boom reply:** Mudhens, copy. Two seats, twice the opinions. We measured. Science is settled.
- **Pilot:** Eagles
  - **Hits:** `eagle`, `eagles`, `f fifteen`, `fifteen c`, `fast eagle`
  - **Boom reply:** Eagles, copy. One seat, infinite superiority. They gas like the boom owes them rent.

### `boat_vs_boom` (ab)

- **Boom opener:** <Callsign>, <TankerCallsign>, if you had to pick — trap on a boat in the weather, or hang on our boom with this coffee?
- **Pilot:** Boat
  - **Hits:** `boat`, `trap`, `carrier`, `boat trap`, `deck`
  - **Boom reply:** Boat, copy. Bold. Tell the Navy we said hi and that their wiring diagrams still look like spaghetti.
- **Pilot:** Boom
  - **Hits:** `boom`, `here`, `yours`, `tanker`, `this`
  - **Boom reply:** Boom, copy. Smart. Warm gas, bad jokes, no salt water. You're among friends.

### `navy_join` (riff)

- **Boom opener:** Had a Navy guy on the boom last week. Asked if we had a 'ready deck.' Buddy, this is a KC-135. The deck is wherever the coffee spills.
- **Pilot:** Classic
  - **Hits:** `classic`, `lol`, `haha`, `ha`, `funny`
  - **Boom reply:** Classic, copy. We still tell that story. They still don't get it.
- **Pilot:** Heard worse
  - **Hits:** `worse`, `heard`, `hornet`, `navy`
  - **Boom reply:** Heard worse, copy. Hornet drivers invent new radio procedures mid-join. Keeps us young.

### `mudhen_opinions` (riff)

- **Boom opener:** Unpopular boom opinion — Mudhens don't need two radios. They need one radio and a mute switch for the back seat. Don't quote me. Quote me.
- **Pilot:** Facts
  - **Hits:** `facts`, `true`, `fact`, `yes`
  - **Boom reply:** Facts, copy. WSO's got the map, the jet, and the lecture. Pilot's just driving the gas station.
- **Pilot:** Harsh
  - **Hits:** `harsh`, `mean`, `ouch`, `rude`
  - **Boom reply:** Harsh, copy. Fair. We'll still gas 'em. Slowly. With commentary.

### `eagle_superiority` (riff)

- **Boom opener:** Eagle pilots gas like they're doing us a favor. Big jet energy. Tiny patience. We smile, push gas, and save the jokes for after they're gone.
- **Pilot:** Accurate
  - **Hits:** `accurate`, `true`, `yes`, `facts`
  - **Boom reply:** Accurate, copy. Fast jet, faster ego. Still family. Distant family.
- **Pilot:** Be nice
  - **Hits:** `nice`, `be nice`, `kind`, `easy`
  - **Boom reply:** Be nice, copy. Fine. They're pretty. The jet, I mean. Mostly the jet.

### `viper_favorite` (riff)

- **Boom opener:** Between us — Vipers are our favorite. Quiet-ish, don't call the boom a 'probe,' and you actually say thanks sometimes. Navy files that under optional.
- **Pilot:** Thanks
  - **Hits:** `thanks`, `thank`, `appreciate`, `cheers`
  - **Boom reply:** See? There it is. Written up. Boom crew morale just went up one percent.
- **Pilot:** Don't tell Navy
  - **Hits:** `navy`, `don't`, `secret`, `quiet`
  - **Boom reply:** Don't tell Navy, copy. Too late. They're already writing a NATOPS change.

### `worst_join_story` (open)

- **Boom opener:** Open mic — worst community join you've seen. Navy, Mudhen, Eagle, or somebody who shall remain nameless. Boom is taking notes.
- **Pilot:** Navy
  - **Hits:** `navy`, `boat`, `hornet`, `carrier`
  - **Boom reply:** Navy, copy. Filed under 'boat procedures, land edition.' We still laugh about it on the long orbits.
- **Pilot:** Mudhen
  - **Hits:** `mudhen`, `mudhens`, `strike`, `wso`
  - **Boom reply:** Mudhen, copy. Two voices, three directions, one confused boom operator. Classic Strike Eagle theater.
- **Pilot:** Eagle
  - **Hits:** `eagle`, `eagles`, `fifteen`
  - **Boom reply:** Eagle, copy. Showed up already superior. Left still superior. Gas was the only thing that changed.

### `who_youd_gas_last` (open)

- **Boom opener:** Honest boom question — if fuel was short, who are you gassing last: Navy, Mudhen, or Eagle?
- **Pilot:** Navy
  - **Hits:** `navy`, `boat`, `hornet`
  - **Boom reply:** Navy last, copy. They can hold. Or find a boat. Not our problem. Mostly kidding. Mostly.
- **Pilot:** Mudhen
  - **Hits:** `mudhen`, `mudhens`, `strike`
  - **Boom reply:** Mudhen last, copy. They'll discuss it as a crew for twenty minutes anyway. Buys us time.
- **Pilot:** Eagle
  - **Hits:** `eagle`, `eagles`
  - **Boom reply:** Eagle last, copy. They'll claim they didn't need it. Then ask for more. Then claim they didn't need it.

### `navy_callsigns` (riff)

- **Boom opener:** Navy callsigns sound like a bar fight and a boat manual had a baby. Meanwhile you're over here just trying to drink cold coffee and not hit us. Respect.

### `eagle_paint` (riff)

- **Boom opener:** Saw an Eagle yesterday with paint so clean it looked offended to be on our boom. We gave him gas anyway. Charity work.
- **Pilot:** Ha
  - **Hits:** `ha`, `haha`, `lol`, `funny`
  - **Boom reply:** Ha, copy. Pretty jet, fragile ego, full tanks. Everybody wins.

### `mudhen_map` (riff)

- **Boom opener:** Mudhen back-seater once asked if we could 'hold the boom a second' while they folded a map. Sir. This is aviation. The map lost.
- **Pilot:** WSO
  - **Hits:** `wso`, `back seat`, `backseater`, `guy in back`
  - **Boom reply:** WSO, copy. Loves the jet, loves the fight, mildly confused by gas stations that fly. We get it.

### `riddle_offer` (open)

- **Boom opener:** Got a short riddle while the gas flows — want it, or you too busy flying?
- **Pilot:** want
  - **Hits:** `want`, `sure`, `hit me`, `riddle`, `yes`
  - **Boom reply:** Alright. I have cities but no houses, forests but no trees, water but no fish — what am I?
- **Pilot:** busy
  - **Hits:** `busy`, `later`, `no`, `flying`
  - **Boom reply:** Fair. I'll just sit here and look professional. Boring.

### `food_from_home` (ab)

- **Boom opener:** First stop when you get home — Chick-fil-A, Whataburger, or you pretending salad?
- **Pilot:** Chick-fil-A
  - **Hits:** `chick`, `fil-a`, `fila`, `cfa`
  - **Boom reply:** Copy. That's the one. We talk about it up here like it's a religion.
- **Pilot:** Whataburger
  - **Hits:** `whataburger`, `whata`
  - **Boom reply:** Whataburger, copy. That's a Texas problem and I respect it.

### `worse_seat` (open)

- **Boom opener:** Be honest — Viper seat for an hour, or the boom pad staring at your intake. Who's more uncomfortable?
- **Pilot:** viper
  - **Hits:** `viper`, `fighter`, `jet`, `ejection`
  - **Boom reply:** Viper, copy. At least you can see where you're going. I get a close-up of your paint.
- **Pilot:** boom
  - **Hits:** `boom`, `pad`, `pod`, `tanker`
  - **Boom reply:** Boom pad, copy. We lie on our stomachs and call it a career. Respect.

---

*Generated from `voice_intent.INTENTS`, `atc_phrase` template builders, `voice_actions`, `tanker`, and `tanker_chat_library.THREADS`. Re-check those sources if phraseology drifts.*
