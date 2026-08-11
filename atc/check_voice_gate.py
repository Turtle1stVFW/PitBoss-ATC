"""
Regression check for voice gating: run `py -3 check_voice_gate.py`.

The flight shares the PTT, so the grammar is a long list of judgment calls about
what counts as ATC business. Add a case here whenever a phrase fires that should
not, or stays silent when it should not. Needs no microphone and no model —
voice_intent alone, so it runs anywhere.
"""

import voice_intent

CALLSIGN = "FLEECE 1"
RUNWAYS = ["21L", "21R", "03L", "03R"]
READBACK_ITEMS = [
    {
        "key": "squawk",
        "label": "Squawk",
        "value": "0551",
        "spoken": "squawk zero five five one",
        "highlight": True,
    }
]

# (transcript, channel, mission_phase, should_fire, expected_intent_or_None)
CASES = [
    # ---- real ATC calls: must fire ----------------------------------------
    ("Nellis Ground, Fleece 1, ready to taxi", "ground", "departure", True, "ready_taxi"),
    ("Nellis Tower, Fleece 1, ready for departure", "tower", "departure", True, "ready_departure"),
    ("Ground, Fleece 1, request runway two one left", "ground", "departure", True, "request_runway"),
    ("Nellis Approach, Fleece 1, say winds", "approach", "approach", True, "request_winds"),
    ("Approach, Fleece 1, request altimeter", "approach", "approach", True, "request_altimeter"),
    ("Blackjack, Fleece 1, request picture", "blackjack", "flight", True, "request_picture"),
    ("Blackjack, Fleece 1, alpha check bullseye", "blackjack", "flight", True, "request_alpha_check"),
    # Blackjack check-in — callsign + mission colour; "checking in" is enough.
    ("Blackjack, Fleece 1, checking in", "blackjack", "flight", True, "range_entry"),
    ("Blackjack, Fleece 1, checking in, mission zero eight zero seven zero two", "blackjack", "flight", True, "range_entry"),
    # Post-check-in range window: optional Bandsaw, then Blackjack checkout.
    ("Bandsaw, Fleece 1, checking in", "bandsaw", "flight", True, "bandsaw_check_in"),
    ("Bandsaw, Fleece 1, with you", "bandsaw", "flight", True, "bandsaw_check_in"),
    ("Bandsaw, Fleece 1, request picture", "bandsaw", "flight", True, "request_picture"),
    ("Blackjack, Fleece 1, request Bandsaw", "blackjack", "flight", True, "request_bandsaw"),
    ("Blackjack, Fleece 1, push Bandsaw", "blackjack", "flight", True, "request_bandsaw"),
    ("Blackjack, Fleece 1, off station, range complete", "blackjack", "flight", True, "range_exit"),
    ("Blackjack, Fleece 1, range exit", "blackjack", "flight", True, "range_exit"),
    ("Delivery, Fleece 1, request IFR clearance", "delivery", "departure", True, "ready_clearance"),
    ("Nellis Tower, Fleece 1, gear down full stop", "tower", "approach", True, "request_landing"),
    ("Tower, Fleece 1, going around", "tower", "approach", True, "going_around"),
    ("Ground, Fleece 1, clear of the runway", "ground", "approach", True, "clear_of_runway"),
    # own callsign, no agency named — still us talking to ATC
    ("Fleece 1, ready to taxi", "ground", "departure", True, "ready_taxi"),
    ("Fleece 1, request the current winds", "tower", "departure", True, "request_winds"),
    # Departure radar contact — airborne check-in
    ("Departure, Fleece 1, with you", "departure", "departure", True, "departure_check_in"),
    ("Nellis Departure, Fleece 1, airborne", "departure", "departure", True, "departure_check_in"),
    ("Fleece 1, airborne", "departure", "departure", True, "departure_check_in"),
    # Unrestricted climb — Tower only, hidden from kneeboard cues
    ("Tower, Fleece 1, request unrestricted climb", "tower", "departure", True, "request_unrestricted_climb"),
    ("Fleece 1, request unrestricted", "tower", "departure", True, "request_unrestricted_climb"),
    # we are Fleece 1, so "Fleece 1" is never a wingman
    ("Nellis Tower, Fleece 1 flight, ready for departure", "tower", "departure", True, "ready_departure"),

    # ---- intra-flight chatter: must stay silent ---------------------------
    ("Two, go button five", "ground", "departure", False, None),
    ("Fleece 2, ready to taxi", "ground", "departure", False, None),
    ("Two, fence in", "departure", "departure", False, None),
    ("Tally two, visual", "blackjack", "flight", False, None),
    ("Fox two", "blackjack", "flight", False, None),
    ("Knock it off, knock it off", "blackjack", "flight", False, None),
    ("Bingo fuel", "blackjack", "flight", False, None),
    ("Lead, you're streaming fuel", "departure", "departure", False, None),
    ("Fleece 2, push button three for tower", "ground", "departure", False, None),
    ("Two, master arm safe, fence out", "approach", "approach", False, None),
    ("Dash two, combat spread", "departure", "departure", False, None),
    # thinking out loud about a call is not making the call
    ("We should request runway two one left", "ground", "departure", False, None),
    ("Should we ask tower for the rolling?", "tower", "departure", False, None),
    ("I'm gonna ask for a picture", "blackjack", "flight", False, None),
    # no addressee at all
    ("Ready to taxi", "ground", "departure", False, None),
    ("Say again", "tower", "departure", False, None),
    ("Request runway zero three left", "ground", "departure", False, None),
    # off-topic
    ("uh yeah so we were thinking about lunch", "ground", "departure", False, None),

    # ---- wrong mission phase: addressed correctly but implausible ---------
    ("Nellis Ground, Fleece 1, ready to taxi", "ground", "approach", False, None),
    ("Tower, Fleece 1, gear down full stop", "tower", "departure", False, None),

    # ---- loose wording and Whisper mis-hearings: must still fire ----------
    ("Nellis Ground, Fleece 1, ready to taxy", "ground", "departure", True, "ready_taxi"),
    ("Nellis Ground, Fleece 1, we're ready for taxi", "ground", "departure", True, "ready_taxi"),
    ("Nellis Tower, Fleece 1, we are readdy for departure", "tower", "departure", True, "ready_departure"),
    ("Nellis Tower, Fleece 1, number one holding short, ready to go", "tower", "departure", True, "ready_departure"),
    ("Approach, Fleece 1, could we get the altimiter", "approach", "approach", True, "request_altimeter"),
    ("Approach, Fleece 1, say the winds please", "approach", "approach", True, "request_winds"),
    ("Blackjack, Fleece 1, request a pitcher", "blackjack", "flight", True, "request_picture"),
    ("Ground, Fleece 1, how about runway two one left", "ground", "departure", True, "request_runway"),
    ("Tower, Fleece 1, we'd like runway 21 left", "tower", "departure", True, "request_runway"),
    # ...but a near-miss must not become a different word
    ("Nellis Ground, Fleece 1, send the text", "ground", "departure", False, None),
]


def extras() -> int:
    """Expectation boost, seat-aware callsigns, and the discipline toggle."""
    bad = 0

    # The call ATC is waiting for should outrank the same call unprompted.
    plain = voice_intent.evaluate(
        "Nellis Ground, Fleece 1, ready to taxi",
        channel="ground", phase="departure", callsign=CALLSIGN, runways=RUNWAYS,
    )
    primed = voice_intent.evaluate(
        "Nellis Ground, Fleece 1, ready to taxi",
        channel="ground", phase="departure", expected="taxi",
        callsign=CALLSIGN, runways=RUNWAYS,
    )
    print(f"expectation boost: {plain.match.confidence:.0%} -> {primed.match.confidence:.0%}")
    if primed.match.confidence <= plain.match.confidence:
        print("  FAIL expected the awaited call to score higher")
        bad += 1

    # If we are Fleece 2, then Fleece 2 is us and Fleece 1 is someone else.
    as_two = voice_intent.evaluate(
        "Fleece 2, ready to taxi", channel="ground", phase="departure",
        callsign="FLEECE 2", runways=RUNWAYS,
    )
    lead = voice_intent.evaluate(
        "Fleece 1, ready to taxi", channel="ground", phase="departure",
        callsign="FLEECE 2", runways=RUNWAYS,
    )
    print(f"as Fleece 2 — own call fires: {as_two.fired}, call to lead fires: {lead.fired}")
    if not as_two.fired or lead.fired:
        print("  FAIL seat-aware callsign gate")
        bad += 1

    # Discipline off: bare request without agency still fires.
    loose = voice_intent.evaluate(
        "Ready to taxi", channel="ground", phase="departure",
        callsign=CALLSIGN, runways=RUNWAYS, require_address=False,
    )
    print(f"discipline off — bare 'ready to taxi' fires: {loose.fired}")
    if not loose.fired:
        print("  FAIL require_address=False should allow bare calls")
        bad += 1

    # ...but never lets flight chatter through.
    still = voice_intent.evaluate(
        "Two, go button five", channel="ground", phase="departure",
        callsign=CALLSIGN, runways=RUNWAYS, require_address=False,
    )
    print(f"discipline off — flight chatter still silent: {not still.fired} ({still.reason})")
    if still.fired:
        print("  FAIL chatter must be suppressed regardless of the toggle")
        bad += 1

    # The prompts on the Fly tab have to be calls that actually fire, or the
    # card teaches the pilot phrasing the gate then rejects. Kneeboard cues are
    # short ("request clearance"); wrap with agency + callsign for the address gate.
    for channel, mission_phase, expected in (
        ("delivery", "departure", "clearance"),
        ("ground", "departure", "taxi"),
        ("tower", "departure", "lineup"),
        ("blackjack", "flight", "bj_check_in"),
        ("bandsaw", "flight", "bandsaw_check_in"),
        ("approach", "approach", "approach_check_in"),
        ("departure", "departure", "radar_contact"),
    ):
        prompts = voice_intent.suggestions(
            phase=mission_phase, channel=channel, expected=expected,
            callsign=CALLSIGN, airport_name="Nellis", limit=4,
        )
        if not prompts:
            print(f"  FAIL no prompts offered for {channel}/{mission_phase}")
            bad += 1
            continue
        if expected == "radar_contact":
            sayings = [s.lower() for s, _ in prompts]
            if any("wind" in s or "altimeter" in s for s in sayings):
                print(f"  FAIL radar contact cues must not offer winds/altimeter: {prompts}")
                bad += 1
            if any("inbound" in s or "overhead" in s for s in sayings):
                print(f"  FAIL radar contact cues must not offer inbound/overhead: {prompts}")
                bad += 1
            if not any(s == "with you" for s, _ in prompts):
                print(f"  FAIL radar contact should lead with check-in: {prompts}")
                bad += 1
        agency = voice_intent.agency_spoken(channel, "Nellis")
        for say, _does in prompts:
            # Short kneeboard cue → full call the address gate expects.
            spoken = f"{agency}, {CALLSIGN}, {say}"
            ev = voice_intent.evaluate(
                spoken, channel=channel, phase=mission_phase, expected=expected,
                callsign=CALLSIGN, runways=RUNWAYS,
            )
            if not ev.fired:
                print(
                    f"  FAIL [{channel}/{mission_phase}] prompt does not fire: "
                    f"{say!r} as {spoken!r} — {ev.describe()}"
                )
                bad += 1
    print("every suggested call round-trips through the gate")

    # Tuned-agency tip helper: Flight + Bandsaw tune → Bandsaw tips.
    tip_ch = voice_intent.resolve_context_channel(
        mission_phase="flight",
        cursor_channel="blackjack",
        tuned_channel="bandsaw",
    )
    if tip_ch != "bandsaw":
        print(f"  FAIL tip channel should follow Bandsaw tune, got {tip_ch!r}")
        bad += 1
    tip_ch_bj = voice_intent.resolve_context_channel(
        mission_phase="flight",
        cursor_channel="bandsaw",
        tuned_channel="blackjack",
    )
    if tip_ch_bj != "blackjack":
        print(f"  FAIL tip channel should follow Blackjack tune, got {tip_ch_bj!r}")
        bad += 1
    # Departure-phase tip must not jump to Blackjack just because radios are there.
    tip_dep = voice_intent.resolve_context_channel(
        mission_phase="departure",
        cursor_channel="tower",
        tuned_channel="blackjack",
    )
    if tip_dep != "tower":
        print(f"  FAIL departure tips must ignore flight freqs, got {tip_dep!r}")
        bad += 1
    print("tuned-frequency tip channel — ok" if tip_ch == "bandsaw" else "tuned-frequency tip channel — FAIL")

    # Departure radar contact: bare "with you" / "airborne" while awaiting;
    # winds/altimeter stay silent on that step.
    for text, want in (
        ("with you", "departure_check_in"),
        ("airborne", "departure_check_in"),
        ("Fleece 1, checking in", "departure_check_in"),
        ("Departure, Fleece 1, we are airborne", "departure_check_in"),
    ):
        ev = voice_intent.evaluate(
            text,
            channel="departure",
            phase="departure",
            expected="radar_contact",
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        if not ev.fired or ev.match.intent != want:
            print(f"  FAIL departure check-in should fire: {text!r} — {ev.describe()}")
            bad += 1
    for text in (
        "Departure, Fleece 1, say winds",
        "Departure, Fleece 1, say altimeter",
    ):
        ev = voice_intent.evaluate(
            text,
            channel="departure",
            phase="departure",
            expected="radar_contact",
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        if ev.fired and ev.match.intent in ("request_winds", "request_altimeter"):
            print(f"  FAIL winds/altimeter must stay silent on radar contact: {text!r}")
            bad += 1
    print("departure radar contact — with you / airborne; no winds/altimeter")

    # Unrestricted climb must fire on Tower but never appear in YOU CAN SAY.
    for channel, mission_phase, expected in (
        ("tower", "departure", "lineup"),
        ("tower", "departure", "clear_takeoff"),
        ("ground", "departure", "taxi"),
    ):
        prompts = voice_intent.suggestions(
            phase=mission_phase, channel=channel, expected=expected,
            callsign=CALLSIGN, airport_name="Nellis", limit=6,
        )
        if any("unrestricted" in s.lower() for s, _ in prompts):
            print(f"  FAIL unrestricted climb must stay off kneeboard: {prompts}")
            bad += 1
    print("unrestricted climb — available by voice, never hinted")

    # ---- readback windows -------------------------------------------------
    # Squawk hinge for IFR clearance.
    for text in (
        "squawk zero five five one",
        "squawking 0551",
        "Delivery, Fleece 1, squawk zero five five one",
        "roger",
        "copy",
    ):
        result = voice_intent.evaluate(
            text,
            channel="delivery",
            phase="departure",
            expected="clearance",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=True,
            readback_items=READBACK_ITEMS,
            require_address=True,
        )
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(f"  FAIL clearance readback should fire: {text!r} — {result.describe()}")
            bad += 1
    print(f"awaiting readback — bare squawk fires: True (acknowledge_readback)")

    taxi_items = [
        {
            "key": "runway",
            "label": "Runway",
            "value": "21L",
            "spoken": "runway two one left",
            "hinge": True,
            "highlight": True,
        },
        {
            "key": "eor",
            "label": "EOR",
            "value": "EOR",
            "spoken": "taxi to EOR",
            "hinge": True,
        },
    ]

    def taxi_rb(text: str, *, awaiting: bool = True):
        return voice_intent.evaluate(
            text,
            channel="ground",
            phase="departure",
            expected="taxi",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=awaiting,
            readback_items=taxi_items if awaiting else None,
            require_address=True,
        )

    for text in (
        "runway two one left",
        "taxi to EOR",
        "Ground, Fleece 1, runway 21 left",
        "two one left",
    ):
        result = taxi_rb(text)
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(f"  FAIL taxi readback should fire: {text!r} — {result.describe()}")
            bad += 1
    print("awaiting taxi readback — either runway or EOR, no agency required")

    takeoff_items = [
        {
            "key": "runway",
            "label": "Runway",
            "value": "21L",
            "spoken": "runway two one left",
            "hinge": True,
            "highlight": True,
        },
        {
            "key": "cleared",
            "label": "Cleared",
            "value": "takeoff",
            "spoken": "cleared for takeoff",
            "hinge": True,
        },
    ]
    for text in (
        "runway two one left, cleared for takeoff",
        "cleared for takeoff",
        "Tower, Fleece 1, runway 21 left cleared for takeoff",
    ):
        result = voice_intent.evaluate(
            text,
            channel="tower",
            phase="departure",
            expected="clear_takeoff",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=True,
            readback_items=takeoff_items,
            require_address=True,
        )
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(f"  FAIL takeoff readback should fire: {text!r} — {result.describe()}")
            bad += 1
    for text in ("roger", "Tower, Fleece 1, roger"):
        result = voice_intent.evaluate(
            text,
            channel="tower",
            phase="departure",
            expected="clear_takeoff",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=True,
            readback_items=takeoff_items,
            require_address=True,
        )
        if result.fired:
            print(f"  FAIL takeoff bare roger must stay silent: {text!r}")
            bad += 1
    print("awaiting takeoff readback — runway or cleared for takeoff, agency optional")

    # Departure radar contact: climb altitude hinge (flexible forms).
    climb_items = [
        {
            "key": "climb",
            "label": "Climb / maintain",
            "value": "15,000 ft",
            "spoken": "climb and maintain one five thousand",
            "hinge": True,
            "highlight": True,
        },
    ]

    def climb_rb(text: str, *, awaiting: bool = True):
        return voice_intent.evaluate(
            text,
            channel="departure",
            phase="departure",
            expected="radar_contact",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=awaiting,
            readback_items=climb_items if awaiting else None,
            require_address=True,
        )

    for text in (
        "climb and maintain fifteen thousand",
        "climb and maintain one five thousand",
        "maintain fifteen thousand",
        "fifteen thousand",
        "15000",
        "Fleece 1, climb and maintain 15 thousand",
        # No agency — exchange already open.
        "one five thousand",
        # Informal / wrong-agency habits — still accept the altitude.
        "angels 15",
        "angels fifteen",
        "flight level 150",
        "FL 150",
        "fl150",
        "flight level one five zero",
    ):
        result = climb_rb(text)
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(f"  FAIL climb readback should fire: {text!r} — {result.describe()}")
            bad += 1

    for text in (
        "climb and maintain twelve thousand",
        "maintain 12000",
        "twelve thousand",
        "Fleece 1, climb and maintain one two thousand",
        "angels 12",
        "FL 250",
        "flight level two five zero",
        "eighteen thousand",
    ):
        result = climb_rb(text)
        if not result.fired or result.match.intent != "correct_climb_readback":
            print(f"  FAIL wrong climb should correct: {text!r} — {result.describe()}")
            bad += 1
        elif result.match.slots.get("climb_ft") != 15000:
            print(f"  FAIL correction should carry assigned 15000: {text!r} — {result.match.slots}")
            bad += 1

    # Bare roger is not enough — need the altitude (training point).
    for text in ("roger", "copy", "Departure, Fleece 1, roger"):
        result = climb_rb(text)
        if result.fired and result.match.intent == "acknowledge_readback":
            print(f"  FAIL climb bare roger must stay silent: {text!r}")
            bad += 1
    print("awaiting climb readback — flexible altitude; wrong gets correction")

    # Blackjack check-in: bare callsign closes the readback.
    bj_items = [
        {
            "key": "callsign",
            "label": "Callsign",
            "value": "FLEECE 1",
            "spoken": "fleece one",
            "hinge": True,
            "highlight": True,
        },
    ]

    def bj_rb(text: str, *, awaiting: bool = True):
        return voice_intent.evaluate(
            text,
            channel="blackjack",
            phase="flight",
            expected="bj_check_in",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=awaiting,
            readback_items=bj_items if awaiting else None,
            require_address=True,
        )

    for text in ("Fleece 1", "fleece one", "Fleece one"):
        result = bj_rb(text)
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(f"  FAIL blackjack callsign readback should fire: {text!r} — {result.describe()}")
            bad += 1
    for text in ("roger", "copy", "Blackjack, roger"):
        result = bj_rb(text)
        if result.fired and result.match.intent == "acknowledge_readback":
            print(f"  FAIL blackjack bare roger must stay silent: {text!r}")
            bad += 1
    print("awaiting blackjack check-in readback — callsign closes it")

    return bad


# Steps can carry their own trigger wording; these run alongside the built-ins.
PHRASE_STEPS: list[dict[str, object]] = [
    {
        "id": "del_clearance",
        "label": "Clearance delivery",
        "channel": "delivery",
        "phase": "departure",
        "template": "clearance",
        "voice_phrases": ["hit me with the clearance"],
    },
    {
        "id": "gnd_taxi",
        "label": "Taxi to EOR",
        "channel": "ground",
        "phase": "departure",
        "template": "taxi",
        "voice_phrases": ["lets roll to the end"],
    },
]


def step_phrases() -> int:
    bad = 0

    def run(text: str, *, phase: str, channel: str):
        return voice_intent.evaluate(
            text, channel=channel, phase=phase, callsign=CALLSIGN, steps=PHRASE_STEPS
        )

    authored = run(
        "Delivery, Fleece 1, hit me with the clearance",
        phase="departure",
        channel="delivery",
    )
    if not authored.fired or not str(authored.match.intent).startswith("step:"):
        print(f"  FAIL authored phrase should fire: {authored.describe()}")
        bad += 1

    stock = run(
        "Ground, Fleece 1, request taxi",
        phase="departure",
        channel="ground",
    )
    if not stock.fired or stock.match.intent != "ready_taxi":
        print(f"  FAIL stock grammar still works with steps loaded: {stock.describe()}")
        bad += 1

    print(f"step phrases — {'ok' if not bad else f'{bad} problem(s)'}")
    return bad


def expecting() -> int:
    """Short-form answers while ATC is waiting on a specific call."""
    bad = 0

    # Clearance readback: squawk code is enough with no agency opener.
    result = voice_intent.evaluate(
        "squawk zero five five one",
        channel="delivery",
        phase="departure",
        expected="clearance",
        callsign=CALLSIGN,
        runways=RUNWAYS,
        awaiting_readback=True,
        readback_items=READBACK_ITEMS,
    )
    if not result.fired or result.match.intent != "acknowledge_readback":
        print(f"  FAIL expected-answer flexibility: {result.describe()}")
        bad += 1

    print(f"expected-answer flexibility — {'ok' if not bad else f'{bad} problem(s)'}")
    return bad


def main() -> int:
    failures = 0
    for transcript, channel, mission_phase, should_fire, want in CASES:
        ev = voice_intent.evaluate(
            transcript,
            channel=channel,
            phase=mission_phase,
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        fired = ev.fired
        ok = fired == should_fire and (not should_fire or ev.match.intent == want)
        if not ok:
            failures += 1
        flag = "ok  " if ok else "FAIL"
        verdict = ev.match.describe() if ev.match else f"silent — {ev.reason}"
        print(f"{flag} [{mission_phase:9}/{channel:9}] {transcript!r}\n         {verdict}")
    print()
    print(f"{len(CASES) - failures}/{len(CASES)} cases behaved as intended")
    print()
    failures += extras()
    failures += step_phrases()
    failures += expecting()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
