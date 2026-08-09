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

# (transcript, phase, should_fire, expected_intent_or_None)
CASES = [
    # ---- real ATC calls: must fire ----------------------------------------
    ("Nellis Ground, Fleece 1, ready to taxi", "ground", True, "ready_taxi"),
    ("Nellis Tower, Fleece 1, ready for departure", "tower", True, "ready_departure"),
    ("Ground, Fleece 1, request runway two one left", "ground", True, "request_runway"),
    ("Nellis Approach, Fleece 1, say winds", "approach", True, "request_winds"),
    ("Approach, Fleece 1, request altimeter", "approach", True, "request_altimeter"),
    ("Blackjack, Fleece 1, request picture", "blackjack", True, "request_picture"),
    ("Blackjack, Fleece 1, alpha check bullseye", "blackjack", True, "request_alpha_check"),
    ("Delivery, Fleece 1, request IFR clearance", "delivery", True, "ready_clearance"),
    ("Nellis Tower, Fleece 1, gear down full stop", "tower", True, "request_landing"),
    ("Tower, Fleece 1, going around", "tower", True, "going_around"),
    ("Ground, Fleece 1, clear of the runway", "ground", True, "clear_of_runway"),
    # own callsign, no agency named — still us talking to ATC
    ("Fleece 1, ready to taxi", "ground", True, "ready_taxi"),
    ("Fleece 1, request the current winds", "tower", True, "request_winds"),
    # we are Fleece 1, so "Fleece 1" is never a wingman
    ("Nellis Tower, Fleece 1 flight, ready for departure", "tower", True, "ready_departure"),

    # ---- intra-flight chatter: must stay silent ---------------------------
    ("Two, go button five", "ground", False, None),
    ("Fleece 2, ready to taxi", "ground", False, None),
    ("Two, fence in", "departure", False, None),
    ("Tally two, visual", "blackjack", False, None),
    ("Fox two", "blackjack", False, None),
    ("Knock it off, knock it off", "blackjack", False, None),
    ("Bingo fuel", "blackjack", False, None),
    ("Lead, you're streaming fuel", "departure", False, None),
    ("Fleece 2, push button three for tower", "ground", False, None),
    ("Two, master arm safe, fence out", "approach", False, None),
    ("Dash two, combat spread", "departure", False, None),
    # thinking out loud about a call is not making the call
    ("We should request runway two one left", "ground", False, None),
    ("Should we ask tower for the rolling?", "tower", False, None),
    ("I'm gonna ask for a picture", "blackjack", False, None),
    # no addressee at all
    ("Ready to taxi", "ground", False, None),
    ("Say again", "tower", False, None),
    ("Request runway zero three left", "ground", False, None),
    # off-topic
    ("uh yeah so we were thinking about lunch", "ground", False, None),

    # ---- wrong phase: addressed correctly but implausible ------------------
    ("Nellis Ground, Fleece 1, ready to taxi", "approach", False, None),
    ("Tower, Fleece 1, gear down full stop", "delivery", False, None),

    # ---- loose wording and Whisper mis-hearings: must still fire ----------
    ("Nellis Ground, Fleece 1, ready to taxy", "ground", True, "ready_taxi"),
    ("Nellis Ground, Fleece 1, we're ready for taxi", "ground", True, "ready_taxi"),
    ("Nellis Tower, Fleece 1, we are readdy for departure", "tower", True, "ready_departure"),
    ("Nellis Tower, Fleece 1, number one holding short, ready to go", "tower", True, "ready_departure"),
    ("Approach, Fleece 1, could we get the altimiter", "approach", True, "request_altimeter"),
    ("Approach, Fleece 1, say the winds please", "approach", True, "request_winds"),
    ("Blackjack, Fleece 1, request a pitcher", "blackjack", True, "request_picture"),
    ("Ground, Fleece 1, how about runway two one left", "ground", True, "request_runway"),
    ("Tower, Fleece 1, we'd like runway 21 left", "tower", True, "request_runway"),
    # ...but a near-miss must not become a different word
    ("Nellis Ground, Fleece 1, send the text", "ground", False, None),
]


def extras() -> int:
    """Expectation boost, seat-aware callsigns, and the discipline toggle."""
    bad = 0

    # The call ATC is waiting for should outrank the same call unprompted.
    plain = voice_intent.evaluate(
        "Nellis Ground, Fleece 1, ready to taxi",
        channel="ground", phase="ground", callsign=CALLSIGN, runways=RUNWAYS,
    )
    primed = voice_intent.evaluate(
        "Nellis Ground, Fleece 1, ready to taxi",
        channel="ground", phase="ground", expected="taxi",
        callsign=CALLSIGN, runways=RUNWAYS,
    )
    print(f"expectation boost: {plain.match.confidence:.0%} -> {primed.match.confidence:.0%}")
    if primed.match.confidence <= plain.match.confidence:
        print("  FAIL expected the awaited call to score higher")
        bad += 1

    # If we are Fleece 2, then Fleece 2 is us and Fleece 1 is someone else.
    as_two = voice_intent.evaluate(
        "Fleece 2, ready to taxi", channel="ground", phase="ground",
        callsign="FLEECE 2", runways=RUNWAYS,
    )
    lead = voice_intent.evaluate(
        "Fleece 1, ready to taxi", channel="ground", phase="ground",
        callsign="FLEECE 2", runways=RUNWAYS,
    )
    print(f"as Fleece 2 — own call fires: {as_two.fired}, call to lead fires: {lead.fired}")
    if not as_two.fired or lead.fired:
        print("  FAIL seat-aware callsign handling")
        bad += 1

    # Relaxing discipline lets an unaddressed call through.
    loose = voice_intent.evaluate(
        "Ready to taxi", channel="ground", phase="ground",
        callsign=CALLSIGN, runways=RUNWAYS, require_address=False,
    )
    print(f"discipline off — bare 'ready to taxi' fires: {loose.fired}")
    if not loose.fired:
        print("  FAIL toggle should allow unaddressed calls")
        bad += 1

    # ...but never lets flight chatter through.
    still = voice_intent.evaluate(
        "Two, go button five", channel="ground", phase="ground",
        callsign=CALLSIGN, runways=RUNWAYS, require_address=False,
    )
    print(f"discipline off — flight chatter still silent: {not still.fired} ({still.reason})")
    if still.fired:
        print("  FAIL chatter must be suppressed regardless of the toggle")
        bad += 1

    # The prompts on the Fly tab have to be calls that actually fire, or the
    # card teaches the pilot phrasing the gate then rejects.
    for phase, expected in (
        ("delivery", "clearance"), ("ground", "taxi"), ("tower", "clear_takeoff"),
        ("blackjack", "bj_check_in"), ("approach", "approach_check_in"),
    ):
        prompts = voice_intent.suggestions(
            phase=phase, channel=phase, expected=expected,
            callsign=CALLSIGN, airport_name="Nellis", limit=4,
        )
        if not prompts:
            print(f"  FAIL no prompts offered for {phase}")
            bad += 1
            continue
        for say, _does in prompts:
            ev = voice_intent.evaluate(
                say, channel=phase, phase=phase, expected=expected,
                callsign=CALLSIGN, runways=RUNWAYS,
            )
            if not ev.fired:
                print(f"  FAIL [{phase}] prompt does not fire: {say!r} — {ev.describe()}")
                bad += 1
    print("every suggested call round-trips through the gate")

    # After clearance, a bare readback (no agency) must fire; flight chatter must not.
    items = READBACK_ITEMS
    def rb(text: str, *, awaiting: bool = True):
        return voice_intent.evaluate(
            text,
            channel="delivery",
            phase="delivery",
            expected="clearance_readback",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=awaiting,
            readback_items=items if awaiting else None,
        )

    # Clearance hinge: squawk / squawking + the assigned code. Extra words OK;
    # "in sequence" is never required.
    for text in (
        "squawk zero five five one",
        "squawking zero five five one",
        "squawk 0551",
        "cleared to St Louis via Dream, climb and maintain five thousand, "
        "expect flight level two five zero, squawk zero five five one",
        "Fleece 1, squawking 0551, as filed, and we will call ground when ready",
    ):
        result = rb(text)
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(f"  FAIL clearance squawk should fire: {text!r} — {result.describe()}")
            bad += 1
    print(f"awaiting readback — bare squawk fires: True (acknowledge_readback)")

    # Wrong code, or the word alone without digits, must not confirm.
    for text in ("squawk one two three four", "squawk", "zero five five one"):
        result = rb(text)
        if result.fired:
            print(f"  FAIL bad/partial squawk must stay silent: {text!r} — {result.describe()}")
            bad += 1

    if rb("squawk zero five five one", awaiting=False).fired:
        print("  FAIL bare readback must not fire when not awaiting")
        bad += 1
    if rb("Two, go button five").fired:
        print("  FAIL flight chatter must stay silent during readback window")
        bad += 1

    return bad


# Steps can carry their own trigger wording; these run alongside the built-ins.
PHRASE_STEPS: list[dict[str, object]] = [
    {
        "id": "del_clearance",
        "label": "Clearance delivery",
        "channel": "delivery",
        "phase": "delivery",
        "template": "clearance",
        "voice_phrases": ["hit me with the clearance"],
    },
    {
        "id": "gnd_taxi",
        "label": "Taxi to EOR",
        "channel": "ground",
        "phase": "ground",
        "template": "taxi",
        "voice_phrases": "wheels turning, lets roll to the EOR",
    },
]


def step_phrases() -> int:
    """Mission-authored phrases fire their own step without loosening the gate."""
    bad = 0

    def ev(text: str, phase: str):
        return voice_intent.evaluate(
            text, channel=phase, phase=phase, callsign=CALLSIGN, steps=PHRASE_STEPS
        )

    fires = [
        ("Nellis Delivery, Fleece 1, hit me with the clearance", "delivery", "del_clearance"),
        ("Nellis Ground, Fleece 1, wheels turning", "ground", "gnd_taxi"),
        # Authored wording outranks the loose crew-talk filter ("lets")
        ("Nellis Ground, Fleece 1, lets roll to the EOR", "ground", "gnd_taxi"),
    ]
    for text, phase, want_step in fires:
        result = ev(text, phase)
        if not result.fired or result.match.step_id != want_step:
            print(f"  FAIL step phrase should fire {want_step}: {text!r} — {result.describe()}")
            bad += 1

    silent = [
        ("Two, go button five", "ground"),  # tactical chatter is still a hard stop
        ("Fleece 2, wheels turning", "ground"),  # addressed to the wingman
        ("we should hit me with the clearance", "delivery"),  # nobody addressed
    ]
    for text, phase in silent:
        result = ev(text, phase)
        if result.fired:
            print(f"  FAIL step phrase should stay silent: {text!r} — {result.describe()}")
            bad += 1

    # The authored phrase should be offered on the Fly card, ahead of stock lines
    lines = voice_intent.suggestions(
        phase="delivery",
        channel="delivery",
        expected="clearance",
        callsign=CALLSIGN,
        airport_name="Nellis",
        steps=PHRASE_STEPS,
        limit=3,
    )
    if not lines or "hit me with the clearance" not in lines[0][0].lower():
        print(f"  FAIL authored phrase should lead the suggestions: {lines}")
        bad += 1

    print(f"step phrases — {'ok' if not bad else f'{bad} problem(s)'}")
    return bad


def expecting() -> int:
    """When ATC is waiting on a specific call, a shortened answer still counts."""
    bad = 0

    def ev(text: str, phase: str, expected: str = "", awaiting: bool = False):
        return voice_intent.evaluate(
            text,
            channel=phase,
            phase=phase,
            expected=expected,
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=awaiting,
            readback_items=READBACK_ITEMS if awaiting else None,
        )

    # Summarised — one word instead of the full call
    for text, phase, expect_tmpl, want in [
        ("Nellis Ground, Fleece 1, taxi", "ground", "taxi", "ready_taxi"),
        ("Nellis Tower, Fleece 1, ready", "tower", "clear_takeoff", "ready_departure"),
        ("Tower, Fleece 1, takeoff", "tower", "clear_takeoff", "ready_departure"),
        ("Nellis Delivery, Fleece 1, clearance", "delivery", "clearance", "ready_clearance"),
    ]:
        result = ev(text, phase, expected=expect_tmpl)
        if not result.fired or result.match.intent != want:
            print(f"  FAIL summarised call should fire {want}: {text!r} — {result.describe()}")
            bad += 1

    # The same shorthand means nothing when that step is not the one due
    for text, phase, expect_tmpl in [
        ("Nellis Ground, Fleece 1, taxi", "ground", "clear_takeoff"),
        ("Nellis Tower, Fleece 1, ready", "tower", "clear_land"),
        ("Nellis Delivery, Fleece 1, clearance", "delivery", "taxi"),
    ]:
        result = ev(text, phase, expected=expect_tmpl)
        if result.fired:
            print(f"  FAIL shorthand should need the full call: {text!r} — {result.describe()}")
            bad += 1

    # Filler on its own is not a summary of anything
    for text, phase, expect_tmpl in [
        ("Nellis Ground, Fleece 1, request", "ground", "taxi"),
        ("Nellis Tower, Fleece 1, request", "tower", "clear_takeoff"),
    ]:
        result = ev(text, phase, expected=expect_tmpl)
        if result.fired:
            print(f"  FAIL filler alone should stay silent: {text!r} — {result.describe()}")
            bad += 1

    # Shorthand does not buy you past the addressing rules
    for text, phase, expect_tmpl in [
        ("Fleece 2, taxi behind me", "ground", "taxi"),
        ("Two, ready", "tower", "clear_takeoff"),
        ("we should taxi soon", "ground", "taxi"),
        ("taxi", "ground", "taxi"),  # nobody addressed, no readback open
    ]:
        result = ev(text, phase, expected=expect_tmpl)
        if result.fired:
            print(f"  FAIL shorthand must still be addressed: {text!r} — {result.describe()}")
            bad += 1

    # Bare acknowledgements only mean something mid-readback, and then on any
    # agency — taxi and takeoff clearances open a window too, not just delivery.
    for text, phase in [("roger", "delivery"), ("copy", "ground"), ("wilco", "tower")]:
        open_window = ev(text, phase, expected="clear_takeoff", awaiting=True)
        if not open_window.fired or open_window.match.intent != "acknowledge_readback":
            print(f"  FAIL bare ack should fire mid-readback: {text!r} — {open_window.describe()}")
            bad += 1
        closed = ev(f"Nellis Ground, Fleece 1, {text}", phase, expected="taxi")
        if closed.fired:
            print(f"  FAIL bare ack must be silent with nothing outstanding: {text!r} — {closed.describe()}")
            bad += 1

    print(f"expected-answer flexibility — {'ok' if not bad else f'{bad} problem(s)'}")
    return bad


def main() -> int:
    failures = 0
    for transcript, phase, should_fire, want in CASES:
        ev = voice_intent.evaluate(
            transcript,
            channel=phase,
            phase=phase,
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        fired = ev.fired
        ok = fired == should_fire and (not should_fire or ev.match.intent == want)
        if not ok:
            failures += 1
        flag = "ok  " if ok else "FAIL"
        verdict = ev.match.describe() if ev.match else f"silent — {ev.reason}"
        print(f"{flag} [{phase:9}] {transcript!r}\n         {verdict}")
    print()
    print(f"{len(CASES) - failures}/{len(CASES)} cases behaved as intended")
    print()
    failures += extras()
    failures += step_phrases()
    failures += expecting()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
