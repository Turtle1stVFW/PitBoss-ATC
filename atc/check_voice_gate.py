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
    ("Nellis Tower, Fleece 1, in position", "tower", "departure", True, "in_position"),
    ("Tower, Fleece 1, we're in position", "tower", "departure", True, "in_position"),
    ("Tower, Fleece 1, lined up", "tower", "departure", True, "in_position"),
    ("Ground, Fleece 1, request runway two one left", "ground", "departure", True, "request_runway"),
    ("Nellis Approach, Fleece 1, say winds", "approach", "approach", True, "request_winds"),
    ("Approach, Fleece 1, request altimeter", "approach", "approach", True, "request_altimeter"),
    ("Blackjack, Fleece 1, request picture", "blackjack", "flight", True, "request_picture"),
    ("Blackjack, Fleece 1, bogey dope", "blackjack", "flight", True, "request_bogey_dope"),
    ("Blackjack, Fleece 1, declare", "blackjack", "flight", True, "request_declare"),
    ("Blackjack, Fleece 1, alpha check bullseye", "blackjack", "flight", True, "request_alpha_check"),
    # Blackjack check-in — callsign + mission colour; "checking in" is enough.
    ("Blackjack, Fleece 1, checking in", "blackjack", "flight", True, "range_entry"),
    ("Blackjack, Fleece 1, checking in, mission zero eight zero seven zero two", "blackjack", "flight", True, "range_entry"),
    ("Blackjack, Fleece 1, with you", "blackjack", "flight", True, "range_entry"),
    ("Blackjack, FLEECE 1 with you, 15,000", "blackjack", "flight", True, "range_entry"),
    ("Blackjack, Fleece 1, with you at 15,000", "blackjack", "flight", True, "range_entry"),
    # Post-check-in range window: optional Bandsaw, then Blackjack checkout.
    ("Bandsaw, Fleece 1, checking in", "bandsaw", "flight", True, "bandsaw_check_in"),
    ("Bandsaw, Fleece 1, with you", "bandsaw", "flight", True, "bandsaw_check_in"),
    ("Bandsaw, Fleece 1, checking out", "bandsaw", "flight", True, "bandsaw_check_out"),
    ("Bandsaw, Fleece 1, switch Blackjack", "bandsaw", "flight", True, "bandsaw_check_out"),
    ("Bandsaw, Fleece 1, checking out, contact Blackjack", "bandsaw", "flight", True, "bandsaw_check_out"),
    # Whisper often hears Bandsaw as ANSA / and saw / bansaw
    ("ANSA, Fleece 1, checking in", "bandsaw", "flight", True, "bandsaw_check_in"),
    ("ANSA, Fleece 1, checking out", "bandsaw", "flight", True, "bandsaw_check_out"),
    ("And saw, Fleece 1, declare", "bandsaw", "flight", True, "request_declare"),
    ("Bansaw, Fleece 1, request picture", "bandsaw", "flight", True, "request_picture"),
    ("BANSAH, Fleece 1, request picture", "bandsaw", "flight", True, "request_picture"),
    ("Bands off, lease one, request picture", "bandsaw", "flight", True, "request_picture"),
    ("Bandsaw, Fleece 1, request picture", "bandsaw", "flight", True, "request_picture"),
    ("Bandsaw, Fleece 1, bogey dope", "bandsaw", "flight", True, "request_bogey_dope"),
    ("Bandsaw, Fleece 1, declare", "bandsaw", "flight", True, "request_declare"),
    ("Blackjack, Fleece 1, request Bandsaw", "blackjack", "flight", True, "request_bandsaw"),
    ("Blackjack, Fleece 1, push Bandsaw", "blackjack", "flight", True, "request_bandsaw"),
    ("Blackjack, Fleece 1, request ANSA", "blackjack", "flight", True, "request_bandsaw"),
    ("Blackjack, Fleece 1, off station, range complete", "blackjack", "flight", True, "range_exit"),
    ("Blackjack, Fleece 1, range exit", "blackjack", "flight", True, "range_exit"),
    ("Blackjack, Fleece 1, say again", "blackjack", "flight", True, "say_again"),
    # Approach / recovery
    ("Nellis Approach, Fleece 1, checking in", "approach", "approach", True, "inbound_recovery"),
    ("Approach, Fleece 1, with you", "approach", "approach", True, "inbound_recovery"),
    ("Approach, Fleece 1, inbound for recovery", "approach", "approach", True, "inbound_recovery"),
    ("Approach, Fleece 1, request ARCOE recovery", "approach", "approach", True, "request_approach"),
    ("Approach, Fleece 1, request tactical overhead", "approach", "approach", True, "request_approach"),
    ("Approach, Fleece 1, request overhead", "approach", "approach", True, "request_approach"),
    ("Approach, Fleece 1, request straight in", "approach", "approach", True, "request_approach"),
    ("Approach, Fleece 1, request instrument", "approach", "approach", True, "request_approach"),
    ("Tower, Fleece 1, request tactical overhead", "tower", "approach", True, "request_approach"),
    ("Tower, Fleece 1, request overhead", "tower", "approach", True, "request_approach"),
    ("Tower, Fleece 1, request straight in", "tower", "approach", True, "request_approach"),
    ("Tower, Fleece 1, request instrument", "tower", "approach", False, None),
    ("Approach, Fleece 1, request hold", "approach", "approach", True, "request_hold"),
    ("Approach, Fleece 1, cancel hold", "approach", "approach", True, "cancel_hold"),
    ("Approach, Fleece 1, request vectors", "approach", "approach", True, "request_vectors"),
    ("Approach, Fleece 1, airport in sight, request tower", "approach", "approach", True, "approach_continue"),
    ("Approach, Fleece 1, request handoff", "approach", "approach", True, "approach_continue"),
    ("Approach, Fleece 1, established", "approach", "approach", True, "approach_established"),
    ("Delivery, Fleece 1, request IFR clearance", "delivery", "departure", True, "ready_clearance"),
    ("Nellis Tower, Fleece 1, gear down full stop", "tower", "approach", True, "request_landing"),
    ("Nellis Tower, Fleece 1, with you", "tower", "approach", True, "tower_check_in"),
    ("Tower, Fleece 1, initial", "tower", "approach", True, "tower_initial"),
    ("Nellis tower, FLEECE 1.1, initial, 2.1, right", "tower", "approach", True, "tower_initial"),
    ("Nellis Tower, Fleece 1-1, initial", "tower", "approach", True, "tower_initial"),
    ("Nellis Tower, Fleece 1.2, initial", "tower", "approach", False, None),
    ("Nellis tower, FLEECE 1.1, going missed", "tower", "approach", True, "going_around"),
    ("Nellis tower, FLEECE 1.1 on the go", "tower", "approach", True, "going_around"),
    ("Nellis tower, FLEECE 1.1, missed approach", "tower", "approach", True, "going_around"),
    ("Call to tower FLEECE 1. Go ahead and missed", "tower", "approach", True, "going_around"),
    ("Tower, Fleece 1, going around", "tower", "approach", True, "going_around"),
    ("Tower, Fleece 1, missed approach", "tower", "approach", True, "going_around"),
    ("Tower, Fleece 1, going missed", "tower", "approach", True, "going_around"),
    ("Tower, Fleece 1, on the go", "tower", "approach", True, "going_around"),
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
    ("Tower, Fleece 1, request rolling", "tower", "departure", True, "request_rolling"),
    ("Departure, Fleece 1, request handoff", "departure", "departure", True, "request_handoff"),
    ("Nellis Departure, Fleece 1, request blackjack", "departure", "departure", True, "request_handoff"),
    ("Tower, Fleece 1, request a rolling takeoff", "tower", "departure", True, "request_rolling"),
    # we are Fleece 1, so "Fleece 1" is never a wingman
    ("Nellis Tower, Fleece 1 flight, ready for departure", "tower", "departure", True, "ready_departure"),
    # EOR — Whisper writes the letters as "E or" / "e o r", not "eor"
    ("Ground, Fleece 1, at EOR", "ground", "departure", True, "at_eor"),
    ("Ground, Fleece 1, at E or", "ground", "departure", True, "at_eor"),
    ("Ground, Fleece 1, at e o r", "ground", "departure", True, "at_eor"),
    ("Ground, Fleece 1, at ee or", "ground", "departure", True, "at_eor"),
    ("Ground, Fleece 1, end of the runway", "ground", "departure", True, "at_eor"),

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
    ("Blackjack on 14, please push, 14", "departure", "departure", False, None),
    ("Blackjack on 14, please push, 14", "blackjack", "flight", False, None),
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

    # Approach assigner: VMC → VFR recovery on 21; IFR → IAF.
    import atc_phrase

    airports = atc_phrase.load_json(atc_phrase.AIRPORTS_PATH)
    nellis = airports["nellis"]
    vmc = atc_phrase.Weather(210, 8, 29.92, "KLSV 010000Z 21008KT 10SM FEW100 20/05 A2992")
    plan_vmc = atc_phrase.assign_approach_plan(nellis, vmc, state={}, force=True)
    if plan_vmc.get("pattern") == "instrument":
        print(f"  FAIL VMC plan should not be instrument: {plan_vmc}")
        bad += 1
    elif not plan_vmc.get("vfr_recovery"):
        print(f"  FAIL VMC plan missing VFR recovery: {plan_vmc}")
        bad += 1
    elif not str(plan_vmc.get("runway") or "").startswith("21"):
        print(f"  FAIL VMC plan should prefer 21s: {plan_vmc}")
        bad += 1
    elif int(plan_vmc.get("descend_ft") or 0) != 12000:
        # Default ARCOE on 21 uses catalog descend_ft 12000 (not global 10000).
        print(f"  FAIL ARCOE should use 12000 ft: {plan_vmc}")
        bad += 1
    else:
        print(
            f"approach assign VMC — {plan_vmc.get('vfr_recovery')} "
            f"{plan_vmc.get('pattern')} RWY {plan_vmc.get('runway')} "
            f"desc {plan_vmc.get('descend_ft')} / {plan_vmc.get('speed_kt')}kt"
        )
    bj_exit = atc_phrase.build_blackjack_range_exit(nellis, "Fleece 1", plan=plan_vmc)
    if "expect" in bj_exit.lower():
        print(f"  FAIL Blackjack range exit must not say expect recovery: {bj_exit}")
        bad += 1
    elif "proceed direct arcoe" not in bj_exit.lower():
        print(f"  FAIL Blackjack range exit should proceed direct the fix: {bj_exit}")
        bad += 1
    elif "cleared" in bj_exit.lower():
        print(f"  FAIL Blackjack range exit should not say cleared: {bj_exit}")
        bad += 1
    elif "descend pilot discretion" not in bj_exit.lower():
        print(f"  FAIL Blackjack range exit should start the IAF descent: {bj_exit}")
        bad += 1
    else:
        print(f"blackjack range exit — {bj_exit}")
    bj_hold = atc_phrase.build_blackjack_range_exit(
        nellis, "Fleece 1", plan=plan_vmc, include_handoff=False
    )
    if "proceed direct arcoe" not in bj_hold.lower():
        print(f"  FAIL far-out range exit should proceed direct the fix: {bj_hold}")
        bad += 1
    elif "contact" in bj_hold.lower():
        print(f"  FAIL far-out range exit must not hand to Approach: {bj_hold}")
        bad += 1
    elif "remain this frequency" not in bj_hold.lower():
        print(f"  FAIL far-out range exit should remain this frequency: {bj_hold}")
        bad += 1
    elif "descend pilot discretion" not in bj_hold.lower():
        print(f"  FAIL far-out range exit should start the IAF descent: {bj_hold}")
        bad += 1
    else:
        print(f"blackjack range exit (far) — {bj_hold}")
    bj_app = atc_phrase.build_blackjack_approach_handoff(nellis, "Fleece 1")
    bj_after = atc_phrase.build_template_text(
        nellis,
        "bj_range_exit",
        "Fleece 1",
        vmc,
        str(plan_vmc.get("runway") or "21R"),
        state={"range_exit_approved": True},
    )
    if "contact" not in bj_app.lower() or "proceed direct" in bj_app.lower():
        print(f"  FAIL Approach handoff should be contact only: {bj_app}")
        bad += 1
    elif "descend" in bj_app.lower() or "descend" in bj_after.lower():
        print(f"  FAIL later Approach handoff must not re-issue descent: {bj_app} / {bj_after}")
        bad += 1
    elif "contact" not in bj_after.lower() or "proceed direct" in bj_after.lower():
        print(f"  FAIL already-released range exit is Approach only: {bj_after}")
        bad += 1
    else:
        print(f"blackjack Approach handoff — {bj_app}")

    phrase_vmc = atc_phrase.build_approach_recovery(
        nellis, "Fleece 1", vmc, str(plan_vmc.get("runway") or "21R"), plan=plan_vmc
    )
    need_bits = (
        "landing south",
        "expect arcoe recovery for the overhead",
        "descend pilot discretion",
    )
    if any(bit.lower() not in phrase_vmc.lower() for bit in need_bits):
        print(f"  FAIL Approach check-in phrase: {phrase_vmc}")
        bad += 1
    elif "radar contact" in phrase_vmc.lower():
        print(f"  FAIL Approach check-in should not lead with radar contact: {phrase_vmc}")
        bad += 1
    else:
        print(f"approach check-in phrase — {phrase_vmc}")
    # TORYE is a separate VFR recovery (§4.13.5.2), not an ARCOE gate.
    plan_torye = atc_phrase.assign_approach_plan(
        nellis, vmc, state={}, force=True, vfr_recovery="TORYE"
    )
    phrase_torye = atc_phrase.build_approach_recovery(
        nellis, "Fleece 1", vmc, str(plan_torye.get("runway") or "21R"), plan=plan_torye
    )
    if plan_torye.get("vfr_recovery") != "TORYE":
        print(f"  FAIL TORYE is its own recovery: {plan_torye} / {phrase_torye}")
        bad += 1
    elif "expect torye recovery for the overhead" not in phrase_torye.lower():
        print(f"  FAIL TORYE Approach expect: {phrase_torye}")
        bad += 1
    elif "tac overhead" in phrase_torye.lower():
        print(f"  FAIL default VFR is overhead, not TAC: {phrase_torye}")
        bad += 1
    else:
        print(f"approach TORYE recovery — {phrase_torye}")

    ifr = atc_phrase.Weather(
        210,
        8,
        29.92,
        "KLSV 010000Z 21008KT 1SM FG BKN008 10/10 A2992",
        ceiling_ft=800,
        visibility_sm=1.0,
    )
    plan_ifr = atc_phrase.assign_approach_plan(nellis, ifr, state={}, force=True)
    if plan_ifr.get("pattern") != "instrument" or not plan_ifr.get("iaf"):
        print(f"  FAIL IFR plan needs instrument + IAF: {plan_ifr}")
        bad += 1
    elif plan_ifr.get("instrument_id") != "ILS_Z_21L" or plan_ifr.get("iaf") != "ARCOE":
        print(f"  FAIL default 21 instrument is ILS Z / ARCOE: {plan_ifr}")
        bad += 1
    elif int(plan_ifr.get("descend_ft") or 0) != 15000:
        # ARCOE IAF crossing altitude on ILS Z RWY 21L (CIFP 15000+)
        print(f"  FAIL ARCOE on ILS Z should be 15000 ft: {plan_ifr}")
        bad += 1
    elif plan_ifr.get("speed_kt") is not None:
        print(f"  FAIL routine speed should be omitted: {plan_ifr}")
        bad += 1
    else:
        print(
            f"approach assign IFR — {plan_ifr.get('instrument_id')} "
            f"IAF {plan_ifr.get('iaf')} RWY {plan_ifr.get('runway')} "
            f"desc {plan_ifr.get('descend_ft')} (no speed)"
        )
    bj_ifr = atc_phrase.build_blackjack_range_exit(
        nellis, "Fleece 1", plan=plan_ifr, include_handoff=False
    )
    if "descend pilot discretion" not in bj_ifr.lower():
        print(f"  FAIL IFR range exit should start the IAF descent: {bj_ifr}")
        bad += 1
    elif "fifteen" not in bj_ifr.lower() and "one five" not in bj_ifr.lower() and "one fife" not in bj_ifr.lower():
        print(f"  FAIL IFR range exit should use the 15000 IAF altitude: {bj_ifr}")
        bad += 1
    else:
        print(f"blackjack range exit (IFR IAF) — {bj_ifr}")
    expect_ifr = atc_phrase.build_approach_recovery(
        nellis, "Fleece 1", ifr, str(plan_ifr.get("runway") or "21L"), plan=plan_ifr
    )
    clear_ifr = atc_phrase.build_iaf_clearance(nellis, "Fleece 1", plan=plan_ifr)
    if "or localizer" in (plan_ifr.get("instrument_say") or "").lower():
        print(f"  FAIL instrument_say must be one procedure: {plan_ifr.get('instrument_say')}")
        bad += 1
    elif "expect ils zulu" not in expect_ifr.lower():
        print(f"  FAIL IFR check-in should expect ILS Z only: {expect_ifr}")
        bad += 1
    elif "cleared" in expect_ifr.lower() or "cross " in expect_ifr.lower():
        print(f"  FAIL IFR check-in is expect-only: {expect_ifr}")
        bad += 1
    elif "cross arcoe at or above" not in clear_ifr.lower() or (
        "fifteen" not in clear_ifr.lower()
        and "one fife" not in clear_ifr.lower()
        and "one five" not in clear_ifr.lower()
    ):
        print(f"  FAIL IFR clearance needs cross ARCOE at/above 15000: {clear_ifr}")
        bad += 1
    elif "cleared ils zulu" not in clear_ifr.lower():
        print(f"  FAIL IFR clearance should clear ILS Z (not ILS or LOC): {clear_ifr}")
        bad += 1
    else:
        print(f"approach IFR expect — {expect_ifr}")
        print(f"approach IFR clearance — {clear_ifr}")

    # Each IAF belongs to its own plate (CIFP): DUDBE = HI-TACAN Y 21L,
    # ARCOE = ILS Z / HI-TACAN Z 21L, LUCIL = TACAN 03R.
    for iaf_name, want_inst, want_alt in (
        ("DUDBE", "HI_TACAN_Y_21L", 15000),
        ("ARCOE", "ILS_Z_21L", 15000),
        ("KRYSS", "ILS_Z_21L", 8800),
        ("ZAPVO", "LOC_Y_21L", 7000),
        ("HULPU", "HI_TACAN_Y_21L", 5500),
        ("LUCIL", "TACAN_03R", 10300),
    ):
        p_iaf = atc_phrase.assign_approach_plan(
            nellis, ifr, state={}, force=True, recovery="instrument", iaf=iaf_name
        )
        if p_iaf.get("iaf") != iaf_name or p_iaf.get("instrument_id") != want_inst:
            print(
                f"  FAIL {iaf_name} should be on {want_inst}: "
                f"{p_iaf.get('iaf')} / {p_iaf.get('instrument_id')}"
            )
            bad += 1
        elif int(p_iaf.get("descend_ft") or 0) != want_alt:
            print(f"  FAIL {iaf_name} plate altitude {want_alt}: {p_iaf.get('descend_ft')}")
            bad += 1
    print("approach IAF/plate pairing — DUDBE/TACAN Y, ARCOE/ILS Z, LUCIL/TACAN 03R")

    # An unrelated fix must not attach itself to the first plate.
    if atc_phrase.find_instrument_by_iaf(
        atc_phrase.load_approach_catalog(nellis), "JUNNO"
    ):
        print("  FAIL unknown fix must not match an IAF")
        bad += 1

    # Sticky pre-catalog plan (DUDBE on HI-ILS Z) must be rejected and rebuilt.
    st_stale_plate = {
        "approach_assigned": True,
        "approach_plan": {
            "pattern": "instrument",
            "runway": "21L",
            "instrument_id": "HI_ILS_OR_LOC_Z_21L",
            "instrument_say": "HI ILS or localizer Zulu runway two one left",
            "iaf": "DUDBE",
            "iaf_say": "Dudbe",
            "descend_ft": 16000,
            "source": "override",
        },
        "active_recovery": "instrument",
        "active_instrument": "HI_ILS_OR_LOC_Z_21L",
        "active_iaf": "DUDBE",
    }
    if atc_phrase.approach_plan_is_valid(
        st_stale_plate["approach_plan"], atc_phrase.load_approach_catalog(nellis)
    ):
        print("  FAIL old HI-ILS Z + DUDBE plan must be invalid")
        bad += 1
    plan_fixed = atc_phrase.assign_approach_plan(
        nellis, ifr, state=st_stale_plate, force=False
    )
    if plan_fixed.get("iaf") == "DUDBE" and "ILS" in str(plan_fixed.get("instrument_id") or ""):
        print(f"  FAIL rebuilt plan still pairs DUDBE with ILS: {plan_fixed}")
        bad += 1
    elif plan_fixed.get("instrument_id") == "HI_ILS_OR_LOC_Z_21L":
        print(f"  FAIL rebuilt plan kept retired plate id: {plan_fixed}")
        bad += 1
    elif not atc_phrase.approach_plan_from_state(st_stale_plate, airport=nellis):
        # State should have been rewritten with a valid plan.
        if not atc_phrase.approach_plan_is_valid(
            plan_fixed, atc_phrase.load_approach_catalog(nellis)
        ):
            print(f"  FAIL rebuild did not produce a valid plan: {plan_fixed}")
            bad += 1
        else:
            print(
                f"approach stale plate rebuild — {plan_fixed.get('instrument_id')} "
                f"IAF {plan_fixed.get('iaf')}"
            )
    else:
        print(
            f"approach stale plate rebuild — {plan_fixed.get('instrument_id')} "
            f"IAF {plan_fixed.get('iaf')}"
        )

    # Filed ARCOE in IMC must recover via ARCOE, not the catalog's first IAF.
    class _ArcoeFP:
        fp_route_string = "KLSV DREAM COYOT ARCOE KLSV"
        fp_altitude = "FL240"

    plan_arcoe_ifr = atc_phrase.assign_approach_plan(
        nellis, ifr, state={}, force=True, opus=_ArcoeFP()
    )
    if plan_arcoe_ifr.get("pattern") != "instrument":
        print(f"  FAIL filed ARCOE in IMC stays instrument: {plan_arcoe_ifr}")
        bad += 1
    elif plan_arcoe_ifr.get("iaf") != "ARCOE" or plan_arcoe_ifr.get("source") != "route":
        print(f"  FAIL filed ARCOE should drive the IAF: {plan_arcoe_ifr}")
        bad += 1
    else:
        print(
            f"approach filed ARCOE (IMC) — {plan_arcoe_ifr.get('instrument_id')} "
            f"IAF {plan_arcoe_ifr.get('iaf')} source={plan_arcoe_ifr.get('source')}"
        )

    # Same fix filed in VMC gives the VFR recovery, not an instrument approach.
    plan_arcoe_vmc = atc_phrase.assign_approach_plan(
        nellis, vmc, state={}, force=True, opus=_ArcoeFP()
    )
    if plan_arcoe_vmc.get("vfr_recovery") != "ARCOE" or plan_arcoe_vmc.get("pattern") == "instrument":
        print(f"  FAIL filed ARCOE in VMC is a VFR recovery: {plan_arcoe_vmc}")
        bad += 1

    # A recovery fix filed mid-route (not in the tail) is still found.
    plan_deep = atc_phrase.assign_approach_plan(
        nellis,
        vmc,
        state={},
        force=True,
        opus=type(
            "O",
            (),
            {
                "fp_route_string": "KLSV STRYK BTY BAM MLF DTA PUC HVE MTU FFU OGD KLSV",
                "fp_altitude": None,
            },
        )(),
    )
    if plan_deep.get("vfr_recovery") != "STRYK":
        print(f"  FAIL mid-route STRYK should still match: {plan_deep}")
        bad += 1

    # Position decides the plate when nothing is filed or requested.
    for label, pos, want_inst, want_iaf in (
        ("north (Arcoe side)", (36.90, -114.90), "ILS_Z_21L", "ARCOE"),
        ("west (Dudbe side)", (36.40, -115.90), "HI_TACAN_Y_21L", "DUDBE"),
    ):
        p_pos = atc_phrase.assign_approach_plan(
            nellis, ifr, state={}, force=True, position=pos
        )
        if p_pos.get("instrument_id") != want_inst or p_pos.get("iaf") != want_iaf:
            print(
                f"  FAIL from {label} expect {want_inst}/{want_iaf}: "
                f"{p_pos.get('instrument_id')}/{p_pos.get('iaf')}"
            )
            bad += 1
        elif p_pos.get("iaf_source") != "position":
            print(f"  FAIL position-picked IAF should be labelled: {p_pos}")
            bad += 1
    print("approach position pick — north gets ARCOE, west gets DUDBE")

    # VMC: the VFR recovery follows the range you are coming home from.
    vmc_03 = atc_phrase.Weather(30, 12, 29.92, "KLSV 03012KT 10SM", visibility_sm=10)
    for label, wx, pos, want in (
        ("western ranges", vmc, (36.45, -116.00), "STRYK"),
        ("Elgin / northeast", vmc, (36.95, -114.45), "TORYE"),
        ("due north", vmc, (36.90, -114.95), "ARCOE"),
        ("north with 03 active", vmc_03, (36.90, -115.05), "MINTT"),
    ):
        p_vfr = atc_phrase.assign_approach_plan(
            nellis, wx, state={}, force=True, position=pos
        )
        if p_vfr.get("vfr_recovery") != want:
            print(
                f"  FAIL from {label} expect {want}: "
                f"{p_vfr.get('vfr_recovery')} RWY {p_vfr.get('runway')}"
            )
            bad += 1
    print("approach VFR position pick — STRYK west, TORYE northeast, ARCOE north, MINTT on 03")

    # Filed route still outranks position.
    p_conflict = atc_phrase.assign_approach_plan(
        nellis,
        ifr,
        state={},
        force=True,
        position=(36.90, -114.90),
        opus=type("O", (), {"fp_route_string": "BTY DUDBE KLSV", "fp_altitude": None})(),
    )
    if p_conflict.get("iaf") != "DUDBE" or p_conflict.get("iaf_source") != "route":
        print(f"  FAIL filed route must outrank position: {p_conflict}")
        bad += 1

    # Sticky position plan must refresh when the flight plan names another fix.
    st_pos = {
        "approach_assigned": True,
        "approach_plan": {
            "pattern": "tactical_overhead",
            "runway": "21R",
            "vfr_recovery": "STRYK",
            "vfr_recovery_say": "Stryk",
            "descend_ft": 10000,
            "source": "position",
            "fp_route": None,
        },
        "active_recovery": "tactical_overhead",
        "active_vfr_recovery": "STRYK",
    }
    plan_fp_refresh = atc_phrase.assign_approach_plan(
        nellis,
        vmc,
        state=st_pos,
        force=False,
        opus=type(
            "O",
            (),
            {"fp_route_string": "KLSV DREAM COYOT ARCOE KLSV", "fp_altitude": "FL240"},
        )(),
    )
    if plan_fp_refresh.get("vfr_recovery") != "ARCOE" or plan_fp_refresh.get("source") != "route":
        print(f"  FAIL sticky position plan must yield to filed ARCOE: {plan_fp_refresh}")
        bad += 1
    else:
        print(
            f"approach FP refresh — sticky STRYK -> {plan_fp_refresh.get('vfr_recovery')} "
            f"source={plan_fp_refresh.get('source')}"
        )

    # Pattern-only rebuild still reads the filed route (does not suppress FP scan).
    plan_pattern = atc_phrase.assign_approach_plan(
        nellis,
        vmc,
        state={},
        force=True,
        recovery="tactical_overhead",
        position=(36.45, -116.00),  # would pick STRYK if FP ignored
        opus=type(
            "O",
            (),
            {"fp_route_string": "JUNNO TORYE KLSV", "fp_altitude": None},
        )(),
    )
    if plan_pattern.get("vfr_recovery") != "TORYE" or plan_pattern.get("source") != "route":
        print(f"  FAIL pattern rebuild must still honour filed TORYE: {plan_pattern}")
        bad += 1
    else:
        print("approach pattern+FP — TORYE from route, not position STRYK")

    # 03 only with >= 11 kt headwind on 03.
    light_north = atc_phrase.Weather(30, 8, 29.92, "", visibility_sm=10)
    plan_light = atc_phrase.assign_approach_plan(
        nellis, light_north, state={}, force=True
    )
    if not str(plan_light.get("runway") or "").startswith("21"):
        print(f"  FAIL light north wind must stay on 21: {plan_light}")
        bad += 1
    else:
        print(f"approach wind gate light 030/08 — RWY {plan_light.get('runway')}")

    # 081/07 favors 03 by heading but fails the 11 kt gate — stay on 21.
    east_light = atc_phrase.Weather(81, 7, 29.92, "KLSV 08107KT 10SM", visibility_sm=10)
    st_stale = {
        "approach_assigned": True,
        "approach_plan": {
            "pattern": "tactical_overhead",
            "runway": "03L",
            "vfr_recovery": "MINTT",
            "vfr_recovery_say": "Mintt",
        },
        "active_recovery": "tactical_overhead",
    }
    plan_081 = atc_phrase.assign_approach_plan(
        nellis, east_light, state=st_stale, force=False
    )
    if not str(plan_081.get("runway") or "").startswith("21"):
        print(f"  FAIL 081/07 must leave stale 03 plan: {plan_081}")
        bad += 1
    else:
        print(
            f"approach wind gate 081/07 stale-03 refresh — RWY {plan_081.get('runway')} "
            f"{plan_081.get('vfr_recovery')}"
        )
    rwy_dep = atc_phrase.pick_departure_runway(
        nellis,
        east_light,
        None,
        state=st_stale,
        template="approach_check_in",
    )
    if not str(rwy_dep).startswith("21"):
        print(f"  FAIL pick_departure_runway 081/07 should be 21: {rwy_dep}")
        bad += 1
    strong_north = atc_phrase.Weather(30, 12, 29.92, "", visibility_sm=10)
    plan_strong = atc_phrase.assign_approach_plan(
        nellis, strong_north, state={}, force=True
    )
    if not str(plan_strong.get("runway") or "").startswith("03"):
        print(f"  FAIL 030/12 should open 03: {plan_strong}")
        bad += 1
    else:
        print(f"approach wind gate 030/12 — RWY {plan_strong.get('runway')}")

    # Reset runway to winds clears sticky request + refreshes Approach plan.
    st_reset: dict = {}
    atc_phrase.assign_approach_plan(nellis, strong_north, state=st_reset, force=True)
    atc_phrase.set_requested_runway("21R", state=st_reset)
    atc_phrase.assign_approach_plan(
        nellis, strong_north, state=st_reset, force=True, recovery="tactical_overhead"
    )
    if not str((st_reset.get("approach_plan") or {}).get("runway") or "").startswith("21"):
        print(f"  FAIL sticky request should force 21: {st_reset.get('approach_plan')}")
        bad += 1
    cfg_reset = {"runway_override": "21L"}
    rwy_reset = atc_phrase.reset_runway_to_winds(
        nellis, strong_north, state=st_reset, config=cfg_reset
    )
    if not str(rwy_reset or "").startswith("03"):
        print(f"  FAIL reset to winds should reopen 03: {rwy_reset} / {st_reset}")
        bad += 1
    elif atc_phrase.requested_runway(state=st_reset) or cfg_reset.get("runway_override"):
        print(f"  FAIL reset should clear overrides: req={atc_phrase.requested_runway(state=st_reset)} cfg={cfg_reset}")
        bad += 1
    else:
        print(f"approach reset to winds — RWY {rwy_reset}")

    tips_app = voice_intent.suggestions(
        phase="approach",
        channel="approach",
        expected="approach_check_in",
        callsign=CALLSIGN,
        airport_name="Nellis",
        limit=4,
    )
    if not any("wind" in say.lower() for say, _does, _role, *_ in tips_app):
        print(f"  FAIL approach kneeboard should tip winds: {tips_app}")
        bad += 1
    elif not any(
        "wind" in say.lower() and role == "optional" for say, _does, role, *_ in tips_app
    ):
        print(f"  FAIL approach winds tip must be optional, not advance: {tips_app}")
        bad += 1
    else:
        print(f"approach kneeboard winds tip — {[s for s, _, r, *_ in tips_app]}")

    # Filed route tail names the recovery (STRYK near arrival).
    class _Opus:
        fp_route_string = "KLSV FLEX21R DREAM JUNNO STRYK KLSV"
        fp_altitude = "FL250"

    plan_route = atc_phrase.assign_approach_plan(
        nellis, vmc, state={}, force=True, opus=_Opus()
    )
    if plan_route.get("vfr_recovery") != "STRYK" or plan_route.get("source") != "route":
        print(f"  FAIL route should pick STRYK: {plan_route}")
        bad += 1
    elif int(plan_route.get("descend_ft") or 0) != 10000:
        print(f"  FAIL STRYK descend should be 10000: {plan_route}")
        bad += 1
    else:
        print(
            f"approach assign route — {plan_route.get('vfr_recovery')} "
            f"source={plan_route.get('source')} desc {plan_route.get('descend_ft')}"
        )

    plan_iaf_route = atc_phrase.assign_approach_plan(
        nellis,
        vmc,
        state={},
        force=True,
        opus=type("O", (), {"fp_route_string": "BTY FLUSH DUDBE KLSV", "fp_altitude": None})(),
    )
    if plan_iaf_route.get("iaf") != "DUDBE" or plan_iaf_route.get("pattern") != "instrument":
        print(f"  FAIL route IAF should force instrument/DUDBE: {plan_iaf_route}")
        bad += 1
    elif plan_iaf_route.get("instrument_id") != "HI_TACAN_Y_21L":
        print(f"  FAIL route DUDBE belongs to HI-TACAN Y 21L: {plan_iaf_route}")
        bad += 1
    elif int(plan_iaf_route.get("descend_ft") or 0) != 15000:
        print(f"  FAIL route DUDBE altitude should be 15000: {plan_iaf_route}")
        bad += 1
    else:
        print(
            f"approach assign route IAF — {plan_iaf_route.get('iaf')} "
            f"{plan_iaf_route.get('instrument_id')} desc {plan_iaf_route.get('descend_ft')}"
        )

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

    # Whisper writes element callsigns as 1.1 / 1-1 → normalize merges to 11.
    # That is Fleece 1 ship 1 (us), not a wingman.
    elem = voice_intent.evaluate(
        "Nellis tower, FLEECE 1.1, initial, 2.1, right",
        channel="tower",
        phase="approach",
        expected="right_break",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    wing = voice_intent.evaluate(
        "Nellis Tower, Fleece 1.2, initial",
        channel="tower",
        phase="approach",
        expected="right_break",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if not elem.fired or elem.match.intent != "tower_initial":
        print(f"  FAIL Fleece 1.1 initial is us, not the flight: {elem.describe()}")
        bad += 1
    elif wing.fired:
        print(f"  FAIL Fleece 1.2 initial is the wingman: {wing.describe()}")
        bad += 1
    else:
        print("element callsign — 1.1 is lead; 1.2 stays flight talk")

    ga_ok = True
    for text, want in (
        ("Nellis tower, FLEECE 1.1, going missed", "going_around"),
        ("Nellis tower, FLEECE 1.1 on the go", "going_around"),
        ("Nellis tower, FLEECE 1.1, missed approach", "going_around"),
        ("Call to tower FLEECE 1. Go ahead and missed", "going_around"),
    ):
        for cs in (CALLSIGN, "FLEECE 1-1", "FLEECE 1.1"):
            got = voice_intent.evaluate(
                text,
                channel="tower",
                phase="approach",
                expected="exit_runway",
                callsign=cs,
                runways=RUNWAYS,
            )
            if not got.fired or got.match.intent != want:
                print(
                    f"  FAIL go-around {text!r} as {cs}: {got.describe()}"
                )
                bad += 1
                ga_ok = False
    if ga_ok:
        print("go-around — 1.1 / on the go / missed / Whisper 'go ahead and missed'")

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
        ("approach", "approach", "cleared_approach"),
        ("departure", "departure", "radar_contact"),
        ("tower", "approach", "right_break"),
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
            sayings = [s.lower() for s, _d, _r, *_ in prompts]
            if any("wind" in s or "altimeter" in s for s in sayings):
                print(f"  FAIL radar contact cues must not offer winds/altimeter: {prompts}")
                bad += 1
            if any("inbound" in s or "overhead" in s for s in sayings):
                print(f"  FAIL radar contact cues must not offer inbound/overhead: {prompts}")
                bad += 1
            if not any(s == "with you" for s, _d, _r, *_ in prompts):
                print(f"  FAIL radar contact should lead with check-in: {prompts}")
                bad += 1
        if expected == "cleared_approach":
            sayings = [s.lower() for s, _d, r, *_ in prompts if r == "advance"]
            if "request handoff" not in sayings or "established" not in sayings:
                print(f"  FAIL contact tower cues should be request handoff / established: {prompts}")
                bad += 1
            if any("checking in" in s for s in sayings):
                print(f"  FAIL contact tower must not tip checking in: {prompts}")
                bad += 1
        if expected == "right_break":
            sayings = [s.lower() for s, _d, r, *_ in prompts if r == "advance"]
            if "with you" not in sayings or "initial" not in sayings:
                print(f"  FAIL tower check-in cues should be with you / initial: {prompts}")
                bad += 1
            if any("gear" in s or "full stop" in s for s in sayings):
                print(f"  FAIL tower check-in must not tip gear down: {prompts}")
                bad += 1
        agency = voice_intent.agency_spoken(channel, "Nellis")
        for say, _does, _role, *_ in prompts:
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

    # Ground taxi: advance = taxi request; winds stay optional (not advance).
    taxi_tips = voice_intent.suggestions(
        phase="departure",
        channel="ground",
        expected="taxi",
        callsign=CALLSIGN,
        airport_name="Nellis",
        limit=6,
        advance_limit=2,
        optional_limit=3,
    )
    taxi_adv = [s for s, _d, r, *_ in taxi_tips if r == "advance"]
    taxi_opt = [s for s, _d, r, *_ in taxi_tips if r == "optional"]
    if not any("taxi" in s.lower() for s in taxi_adv):
        print(f"  FAIL taxi TO ADVANCE should include taxi cue: {taxi_tips}")
        bad += 1
    if any("wind" in s.lower() for s in taxi_adv):
        print(f"  FAIL winds must not be TO ADVANCE on taxi: {taxi_tips}")
        bad += 1
    if not any("wind" in s.lower() for s in taxi_opt):
        print(f"  FAIL taxi ALSO AVAILABLE should include winds: {taxi_tips}")
        bad += 1
    else:
        print(f"taxi advance/optional split — adv={taxi_adv} opt={taxi_opt}")

    # Fly card: agency opener required on clearance/taxi; optional on check-in.
    clr_tips = voice_intent.suggestions(
        phase="departure",
        channel="delivery",
        expected="clearance",
        callsign=CALLSIGN,
        airport_name="Nellis",
        limit=4,
    )
    if not any(
        "clearance" in s.lower() and needs
        for s, _d, _r, needs in clr_tips
    ):
        print(f"  FAIL clearance cue should require the agency opener: {clr_tips}")
        bad += 1
    rc_tips = voice_intent.suggestions(
        phase="departure",
        channel="departure",
        expected="radar_contact",
        callsign=CALLSIGN,
        airport_name="Nellis",
        limit=4,
    )
    if not any(s == "with you" and not needs for s, _d, _r, needs in rc_tips):
        print(f"  FAIL radar-contact 'with you' should be agency-optional: {rc_tips}")
        bad += 1
    else:
        print("kneeboard agency-required flag — clearance yes, with-you no")

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
    # Next step is Ground taxi — opener is Ground even if still on Delivery.
    cue_gnd = voice_intent.cue_channel(
        mission_phase="departure",
        cursor_channel="ground",
        tuned_channel="delivery",
    )
    if cue_gnd != "ground":
        print(f"  FAIL taxi cue must address Ground, not leftover Delivery: {cue_gnd!r}")
        bad += 1
    cue_flight = voice_intent.cue_channel(
        mission_phase="flight",
        cursor_channel="blackjack",
        tuned_channel="bandsaw",
    )
    if cue_flight != "bandsaw":
        print(f"  FAIL flight cues should follow Bandsaw tune, got {cue_flight!r}")
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

    for text, want in (
        ("Approach, Fleece 1, request handoff", "approach_continue"),
        ("Fleece 1, established", "approach_established"),
        ("established", "approach_established"),
    ):
        ev = voice_intent.evaluate(
            text,
            channel="approach",
            phase="approach",
            expected="cleared_approach",
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        if not ev.fired or ev.match.intent != want:
            print(f"  FAIL contact tower should fire: {text!r} — {ev.describe()}")
            bad += 1
    checkin_on_tower = voice_intent.evaluate(
        "Approach, Fleece 1, checking in",
        channel="approach",
        phase="approach",
        expected="cleared_approach",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if checkin_on_tower.fired and checkin_on_tower.match.intent == "inbound_recovery":
        print(f"  FAIL checking in must not steal contact tower: {checkin_on_tower.describe()}")
        bad += 1
    else:
        print("contact tower — request handoff / established; not checking in")

    for text, want in (
        ("with you", "tower_check_in"),
        ("initial", "tower_initial"),
        ("Tower, Fleece 1, with you", "tower_check_in"),
        ("Nellis Tower, Fleece 1, at initial", "tower_initial"),
    ):
        ev = voice_intent.evaluate(
            text,
            channel="tower",
            phase="approach",
            expected="right_break",
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        if not ev.fired or ev.match.intent != want:
            print(f"  FAIL tower check-in should fire: {text!r} — {ev.describe()}")
            bad += 1
    land_on_checkin = voice_intent.evaluate(
        "Tower, Fleece 1, gear down full stop",
        channel="tower",
        phase="approach",
        expected="right_break",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if land_on_checkin.fired and land_on_checkin.match.intent == "request_landing":
        print(f"  FAIL gear down must not steal tower check-in: {land_on_checkin.describe()}")
        bad += 1
    else:
        print("tower check-in — with you / initial; not gear down")

    # Unrestricted climb must fire on Tower but never appear in voice cues.
    for channel, mission_phase, expected in (
        ("tower", "departure", "lineup"),
        ("tower", "departure", "clear_takeoff"),
        ("ground", "departure", "taxi"),
    ):
        prompts = voice_intent.suggestions(
            phase=mission_phase, channel=channel, expected=expected,
            callsign=CALLSIGN, airport_name="Nellis", limit=6,
        )
        if any("unrestricted" in s.lower() for s, _d, _r, *_ in prompts):
            print(f"  FAIL unrestricted climb must stay off kneeboard: {prompts}")
            bad += 1
    print("unrestricted climb — available by voice, never hinted")

    # Built-in keyword helpers for the Keywords editor.
    clearance_intents = voice_intent.intents_for_template("clearance")
    if not clearance_intents or clearance_intents[0].id != "ready_clearance":
        print(f"  FAIL intents_for_template(clearance): {clearance_intents}")
        bad += 1
    elif "request" not in voice_intent.format_intent_keywords(clearance_intents[0]):
        print(
            f"  FAIL format_intent_keywords missing groups: "
            f"{voice_intent.format_intent_keywords(clearance_intents[0])}"
        )
        bad += 1
    takeoff_intents = voice_intent.intents_for_template("clear_takeoff")
    if not takeoff_intents or takeoff_intents[0].id != "in_position":
        print(f"  FAIL clear_takeoff should map to in_position: {takeoff_intents}")
        bad += 1
    elif any(i.id == "ready_departure" for i in takeoff_intents):
        print(f"  FAIL clear_takeoff must not list ready_departure: {takeoff_intents}")
        bad += 1
    lineup_intents = voice_intent.intents_for_template("lineup")
    if not lineup_intents or lineup_intents[0].id != "ready_departure":
        print(f"  FAIL lineup should map to ready_departure: {lineup_intents}")
        bad += 1
    else:
        print("intents_for_template / format_intent_keywords — ok")

    tips_to = voice_intent.suggestions(
        phase="departure",
        channel="tower",
        expected="clear_takeoff",
        callsign=CALLSIGN,
        airport_name="Nellis",
        limit=6,
    )
    if any(
        r == "advance" and "ready" in say.lower()
        for say, _d, r, *_ in tips_to
    ):
        print(f"  FAIL clear_takeoff must not tip ready for departure: {tips_to}")
        bad += 1
    elif not any(
        r == "advance" and "in position" in say.lower()
        for say, _d, r, *_ in tips_to
    ):
        print(f"  FAIL clear_takeoff should tip in position: {tips_to}")
        bad += 1
    tips_luaw = voice_intent.suggestions(
        phase="departure",
        channel="tower",
        expected="lineup",
        callsign=CALLSIGN,
        airport_name="Nellis",
        limit=6,
    )
    if not any(
        r == "advance" and "ready" in say.lower() for say, _d, r, *_ in tips_luaw
    ):
        print(f"  FAIL lineup should tip ready for departure: {tips_luaw}")
        bad += 1
    else:
        print("lineup tips ready; clear_takeoff tips in position")

    pos_on_takeoff = voice_intent.evaluate(
        "in position",
        channel="tower",
        phase="departure",
        expected="clear_takeoff",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if not pos_on_takeoff.fired or pos_on_takeoff.match.intent != "in_position":
        print(f"  FAIL in position should clear takeoff: {pos_on_takeoff.describe()}")
        bad += 1
    mixed = voice_intent.evaluate(
        "Nellis Tower, Fleece 1, in position ready for takeoff",
        channel="tower",
        phase="departure",
        expected="clear_takeoff",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if not mixed.fired or mixed.match.intent != "in_position":
        print(f"  FAIL in position + takeoff must not be ready_departure: {mixed.describe()}")
        bad += 1
    remain = voice_intent.evaluate(
        "Tower, Fleece 1, remain in position",
        channel="tower",
        phase="departure",
        expected="clear_takeoff",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if remain.fired and remain.match.intent == "in_position":
        print(f"  FAIL remain in position must not clear takeoff: {remain.describe()}")
        bad += 1
    pos_on_offer = voice_intent.evaluate(
        "Tower, Fleece 1, in position",
        channel="tower",
        phase="departure",
        expected="rolling_accept",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if pos_on_offer.fired and pos_on_offer.match.intent == "in_position":
        print(f"  FAIL in position must not answer rolling offer: {pos_on_offer.describe()}")
        bad += 1
    else:
        print("in position — takeoff fallback; not LUAW / rolling / remain")

    # ---- readback windows -------------------------------------------------
    # Squawk hinge for IFR clearance.
    for text in (
        "squawk zero five five one",
        "squawking 0551",
        "Delivery, Fleece 1, squawk zero five five one",
        "zero five five one",
        "0551",
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
    # Wrong code alone must stay silent (not a random altitude/freq lookalike win).
    wrong = voice_intent.evaluate(
        "zero four one one",
        channel="delivery",
        phase="departure",
        expected="clearance",
        callsign=CALLSIGN,
        runways=RUNWAYS,
        awaiting_readback=True,
        readback_items=READBACK_ITEMS,
        require_address=True,
    )
    if wrong.fired and wrong.match and wrong.match.intent == "acknowledge_readback":
        print(f"  FAIL wrong bare code must not close clearance readback: {wrong.describe()}")
        bad += 1
    print("awaiting readback — squawk word, bare code, or roger closes it")

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
        "taxi to E or",
        "taxi to e o r",
        "northwest E or",
        "Ground, Fleece 1, runway 21 left",
        "two one left",
    ):
        result = taxi_rb(text)
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(f"  FAIL taxi readback should fire: {text!r} — {result.describe()}")
            bad += 1
    print("awaiting taxi readback — either runway or EOR, no agency required")

    def eor_call(text: str):
        return voice_intent.evaluate(
            text,
            channel="ground",
            phase="departure",
            expected="monitor_tower",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            require_address=True,
        )

    for text in (
        "at EOR",
        "at E or",
        "E or",
        "e o r",
        "end of runway",
        "Ground, Fleece 1, at ee or",
    ):
        result = eor_call(text)
        if not result.fired or result.match.intent != "at_eor":
            print(f"  FAIL at-EOR should fire: {text!r} — {result.describe()}")
            bad += 1
    print("at EOR — letters, Whisper splits, or end of runway")

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

    land_items = [
        {
            "key": "runway",
            "label": "Runway",
            "value": "21L",
            "spoken": "runway two one left",
            "hinge": True,
            "highlight": True,
        },
        {
            "key": "clearance",
            "label": "Clearance",
            "value": "cleared to land",
            "spoken": "cleared to land",
            "hinge": True,
        },
    ]

    def land_ga(text: str, *, awaiting: bool = True, expected: str = "clear_land"):
        return voice_intent.evaluate(
            text,
            channel="tower",
            phase="approach",
            expected=expected,
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=awaiting,
            readback_items=land_items if awaiting else None,
            require_address=True,
        )

    for text, label in (
        ("going around", "going around"),
        ("missed approach", "missed approach"),
        ("going missed", "going missed"),
        ("on the go", "on the go"),
        ("cleared to land, going around", "cleared to land + going around"),
        ("runway two one left, going around", "runway + going around"),
        ("Fleece 1, going missed", "callsign + going missed"),
    ):
        result = land_ga(text)
        if not result.fired or result.match.intent != "going_around":
            print(
                f"  FAIL land readback {label!r} should go around: "
                f"{text!r} — {result.describe()}"
            )
            bad += 1
    after_rb = land_ga("going around", awaiting=False, expected="exit_runway")
    if not after_rb.fired or after_rb.match.intent != "going_around":
        print(
            f"  FAIL after land readback, bare going around should fire: "
            f"{after_rb.describe()}"
        )
        bad += 1
    land_ack_ok = True
    for text in (
        "cleared to land",
        "clear to land",
        "clear land",
        "cleared land",
    ):
        land_only = land_ga(text)
        if not land_only.fired or land_only.match.intent != "acknowledge_readback":
            print(
                f"  FAIL land readback without go-around should acknowledge: "
                f"{text!r} — {land_only.describe()}"
            )
            bad += 1
            land_ack_ok = False
    if land_ack_ok:
        print(
            "awaiting land readback — going around / missed / on the go "
            "(not stolen by cleared-to-land); clear/cleared to land both ack"
        )

    closed_items = [
        {
            "key": "instruction",
            "label": "Traffic",
            "value": "right closed traffic",
            "spoken": "right closed traffic",
            "hinge": True,
            "highlight": True,
        },
        {
            "key": "runway",
            "label": "Runway",
            "value": "21R",
            "spoken": "runway two one right",
            "hinge": True,
        },
    ]
    flex_items = [
        {
            "key": "instruction",
            "label": "Reentry",
            "value": "Flex reentry",
            "spoken": "flex reentry",
            "hinge": True,
            "highlight": True,
        },
    ]
    missed_items = [
        {
            "key": "instruction",
            "label": "Missed",
            "value": "as published",
            "spoken": "missed approach as published",
            "hinge": True,
            "highlight": True,
        },
    ]

    def ga_rb(text: str, items, *, expected: str = "go_around"):
        return voice_intent.evaluate(
            text,
            channel="tower",
            phase="approach",
            expected=expected,
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=True,
            readback_items=items,
            require_address=True,
        )

    for text, items, label in (
        ("going around", closed_items, "repeat going around"),
        ("on the go", closed_items, "repeat on the go"),
        ("right closed traffic", closed_items, "right closed traffic"),
        ("right closed", closed_items, "right closed"),
        ("right close traffic", closed_items, "right close traffic"),
        ("Right, close traffic, FLEECE 1.", closed_items, "STT close traffic"),
        ("close traffic", closed_items, "close traffic"),
        ("roger", closed_items, "roger closed"),
        ("flex", flex_items, "flex"),
        ("flex reentry", flex_items, "flex reentry"),
        ("missed approach as published", missed_items, "missed as published"),
        ("as published", missed_items, "as published"),
        ("going missed", missed_items, "repeat going missed"),
    ):
        result = ga_rb(text, items)
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(
                f"  FAIL go-around readback {label!r} should acknowledge: "
                f"{text!r} — {result.describe()}"
            )
            bad += 1
    still_waveoff = land_ga("going around")
    if not still_waveoff.fired or still_waveoff.match.intent != "going_around":
        print(
            f"  FAIL land readback still waveoff after go-around tests: "
            f"{still_waveoff.describe()}"
        )
        bad += 1
    else:
        print(
            "awaiting go-around readback — closed / Flex / missed "
            "(repeat going around does not re-issue)"
        )

    luaw_items = [
        {
            "key": "runway",
            "label": "Runway",
            "value": "21R",
            "spoken": "runway two one right",
            "hinge": True,
            "highlight": True,
        },
        {
            "key": "clearance",
            "label": "Clearance",
            "value": "line up and wait",
            "spoken": "line up and wait",
            "hinge": True,
        },
    ]
    for text in (
        "Tower, Fleece 1, request rolling",
        "request a rolling takeoff",
        "Tower, Fleece 1, runway two one right, request rolling",
    ):
        result = voice_intent.evaluate(
            text,
            channel="tower",
            phase="departure",
            expected="lineup",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=True,
            readback_items=luaw_items,
            require_address=True,
        )
        if not result.fired or result.match.intent != "request_rolling":
            print(f"  FAIL LUAW readback must still allow rolling request: {text!r} — {result.describe()}")
            bad += 1
    # A normal LUAW readback must still close the window.
    luaw_rb = voice_intent.evaluate(
        "runway two one right, line up and wait",
        channel="tower",
        phase="departure",
        expected="lineup",
        callsign=CALLSIGN,
        runways=RUNWAYS,
        awaiting_readback=True,
        readback_items=luaw_items,
        require_address=True,
    )
    if not luaw_rb.fired or luaw_rb.match.intent != "acknowledge_readback":
        print(f"  FAIL LUAW readback should still close: {luaw_rb.describe()}")
        bad += 1
    print("LUAW readback — rolling request fires; runway/LUAW still closes")

    # Random "will you accept rolling?" — accept / decline, not LUAW / runway.
    rolling_items = atc_phrase.build_readback_checklist(
        "rolling_accept", nellis, None, vmc, "21R"
    )
    rolling_keys = {str(i.get("key") or "") for i in rolling_items}
    rolling_spoken = " ".join(str(i.get("spoken") or "") for i in rolling_items).lower()
    if rolling_keys != {"accept", "deny"}:
        print(f"  FAIL rolling offer checklist keys: {rolling_items}")
        bad += 1
    elif "line up" in rolling_spoken or "21" in rolling_spoken:
        print(f"  FAIL rolling offer must not list LUAW/runway: {rolling_items}")
        bad += 1
    else:
        print("rolling offer checklist — accept / decline, no runway")

    for text, want in (
        ("Tower, Fleece 1, we'll take the rolling", "accept_rolling"),
        ("accept rolling", "accept_rolling"),
        ("unable rolling", "deny_rolling"),
        ("line up and wait", "request_lineup"),
    ):
        result = voice_intent.evaluate(
            text,
            channel="tower",
            phase="departure",
            expected="rolling_accept",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=True,
            readback_items=rolling_items,
            require_address=True,
        )
        if not result.fired or result.match.intent != want:
            print(
                f"  FAIL rolling offer {text!r} → {want}: {result.describe()}"
            )
            bad += 1
    ready_on_offer = voice_intent.evaluate(
        "Nellis Tower, Fleece 1, ready for departure",
        channel="tower",
        phase="departure",
        expected="rolling_accept",
        callsign=CALLSIGN,
        runways=RUNWAYS,
        awaiting_readback=True,
        readback_items=rolling_items,
        require_address=True,
    )
    if ready_on_offer.fired:
        print(
            f"  FAIL ready for departure must not answer rolling offer: "
            f"{ready_on_offer.describe()}"
        )
        bad += 1
    rwy_on_offer = voice_intent.evaluate(
        "runway two one right",
        channel="tower",
        phase="departure",
        expected="rolling_accept",
        callsign=CALLSIGN,
        runways=RUNWAYS,
        awaiting_readback=True,
        readback_items=rolling_items,
        require_address=True,
    )
    if rwy_on_offer.fired and rwy_on_offer.match.intent == "acknowledge_readback":
        print(f"  FAIL runway alone must not close rolling offer: {rwy_on_offer.describe()}")
        bad += 1
    tips_roll = voice_intent.suggestions(
        phase="departure",
        channel="tower",
        expected="rolling_accept",
        callsign=CALLSIGN,
        airport_name="Nellis",
        awaiting_readback=True,
        readback_items=rolling_items,
        limit=6,
    )
    tip_text = " ".join(s.lower() for s, _d, _r, *_ in tips_roll)
    if "line up and wait" in tip_text and "unable" not in tip_text:
        print(f"  FAIL rolling offer tips still look like LUAW: {tips_roll}")
        bad += 1
    elif "rolling" not in tip_text:
        print(f"  FAIL rolling offer tips should mention rolling: {tips_roll}")
        bad += 1
    else:
        print("rolling offer voice — accept / decline; no ready / runway readback")

    thanks = atc_phrase.build_template_text(
        nellis,
        "clear_takeoff_rolling",
        "Fleece 1",
        vmc,
        "21R",
        state={"rolling_offer_reply": "accept"},
    )
    luaw = atc_phrase.build_template_text(
        nellis,
        "lineup",
        "Fleece 1",
        vmc,
        "21R",
        state={"rolling_offer_reply": "deny"},
    )
    if "thanks" not in thanks.lower() or "cleared for takeoff" not in thanks.lower():
        print(f"  FAIL accept rolling should clear takeoff with thanks: {thanks}")
        bad += 1
    elif "runway two one right" not in thanks.lower():
        print(f"  FAIL takeoff clearance must include the runway: {thanks}")
        bad += 1
    elif "rolling approved" in thanks.lower():
        print(f"  FAIL accept rolling must not say rolling approved: {thanks}")
        bad += 1
    elif "no worries" not in luaw.lower() or "line up" not in luaw.lower():
        print(f"  FAIL deny rolling should be LUAW with no worries: {luaw}")
        bad += 1
    else:
        print("rolling offer reply phrases — thanks/clearance vs no worries/LUAW")

    takeoff_samples = [
        thanks,
        atc_phrase.build_template_text(nellis, "clear_takeoff", "Fleece 1", vmc, "21R", state={}),
        atc_phrase.build_template_text(
            nellis, "clear_takeoff_intersection", "Fleece 1", vmc, "21R", state={}
        ),
    ]
    takeoff_bad = False
    for sample in takeoff_samples:
        low = sample.lower()
        if "contact departure" in low:
            print(f"  FAIL takeoff must not say contact departure: {sample}")
            bad += 1
            takeoff_bad = True
            break
        if "switch to departure" not in low and "change to departure" not in low:
            print(f"  FAIL takeoff should switch/change to departure: {sample}")
            bad += 1
            takeoff_bad = True
            break
    if not takeoff_bad:
        print("takeoff clearance — switch/change to departure, not contact")

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
        # 'feet' must never be required (Whisper often omits or pluralizes).
        "fifteen thousands",
        "15 thousands",
        "15k",
        "FL150",
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

    # After Departure hands to Blackjack: "with you" is BJ check-in, not Bandsaw.
    bj_after_ho_ok = True
    for text, want in (
        ("Blackjack, FLEECE 1 with you, 15,000", "range_entry"),
        ("Blackjack, Fleece 1, with you at 15,000", "range_entry"),
    ):
        ev = voice_intent.evaluate(
            text,
            channel="blackjack",
            phase="flight",
            expected="bj_check_in",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            require_address=True,
        )
        if not ev.fired or ev.match.intent != want:
            print(
                f"  FAIL blackjack after handoff {text!r} should be {want}: "
                f"{ev.describe()}"
            )
            bad += 1
            bj_after_ho_ok = False
    push_wing = voice_intent.evaluate(
        "Blackjack on 14, please push, 14",
        channel="blackjack",
        phase="flight",
        expected="bj_check_in",
        callsign=CALLSIGN,
        runways=RUNWAYS,
        require_address=True,
    )
    if push_wing.fired:
        print(
            f"  FAIL wingman please-push must stay silent: {push_wing.describe()}"
        )
        bad += 1
        bj_after_ho_ok = False
    if bj_after_ho_ok:
        print("blackjack after handoff — with you (not Bandsaw); please push is wingman")

    # Say again must replay last_tx_text. Re-running bj_range_exit after a
    # far-out release would become "radar contact, remain this frequency".
    import voice_engine

    class _ReplayEngine:
        def __init__(self) -> None:
            self.config = {"dry_run": True}
            self.state = {
                "last_step_id": "bj_range_exit",
                "last_tx_text": (
                    "Fleece one, Blackjack, range exit approved, "
                    "proceed direct Arcoe, remain this frequency."
                ),
                "last_tx_template": "bj_range_exit",
                "last_tx_channel": "blackjack",
                "range_exit_approved": True,
            }
            self.played_id = None

        def airport(self) -> dict:
            return nellis

        def replay_last_tx(self, **_k: object) -> dict:
            return {
                "text": self.state["last_tx_text"],
                "channel": "blackjack",
                "replayed": True,
            }

        def play_id(self, step_id: str, **_k: object) -> dict:
            self.played_id = step_id
            return {"text": "Fleece one, Blackjack, radar contact. Continue, remain this frequency."}

    replay_eng = _ReplayEngine()
    replay_match = voice_intent.Match(
        intent="say_again", kind="request", template="", confidence=1.0
    )
    replayed = voice_engine.execute_intent(replay_match, replay_eng)
    if replay_eng.played_id is not None:
        print(f"  FAIL say again must not re-run play_id: {replay_eng.played_id}")
        bad += 1
    elif "range exit approved" not in str(replayed.get("text") or "").lower():
        print(f"  FAIL say again should repeat the exit: {replayed}")
        bad += 1
    elif "radar contact" in str(replayed.get("text") or "").lower():
        print(f"  FAIL say again must not become continue: {replayed}")
        bad += 1
    else:
        print("say again — repeats last TX, does not re-run range exit")

    # Tower handoff / check-in / clear-land (post-approach clearance).
    handoff = atc_phrase.build_approach_tower_handoff(nellis, "Fleece 1")
    if "cleared" in handoff.lower():
        print(f"  FAIL contact tower must not re-clear the approach: {handoff}")
        bad += 1
    elif "contact tower" not in handoff.lower():
        print(f"  FAIL contact tower handoff: {handoff}")
        bad += 1
    else:
        print(f"tower handoff — {handoff}")

    plan_ohb = {"pattern": "visual_overhead", "runway": "21R"}
    brk = atc_phrase.build_tower_check_in(nellis, "Fleece 1", "21R", plan=plan_ohb)
    if "right break approved" not in brk.lower():
        print(f"  FAIL OHB tower check-in should approve right break: {brk}")
        bad += 1
    cont = atc_phrase.build_tower_check_in(
        nellis, "Fleece 1", "21R", plan={"pattern": "straight_in", "runway": "21R"}
    )
    if "continue straight-in" not in cont.lower():
        print(f"  FAIL straight-in tower check-in: {cont}")
        bad += 1
    twr_oh = voice_intent.evaluate(
        "Tower, Fleece 1, request overhead",
        channel="tower",
        phase="approach",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    twr_inst = voice_intent.evaluate(
        "Tower, Fleece 1, request instrument",
        channel="tower",
        phase="approach",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    app_oh = voice_intent.evaluate(
        "Approach, Fleece 1, request overhead",
        channel="approach",
        phase="approach",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if (
        not twr_oh.fired
        or twr_oh.match.intent != "request_approach"
        or (twr_oh.match.slots or {}).get("recovery") != "visual_overhead"
    ):
        print(f"  FAIL Tower should take request overhead: {twr_oh.describe()}")
        bad += 1
    elif twr_inst.fired:
        print(f"  FAIL Tower must not take request instrument: {twr_inst.describe()}")
        bad += 1
    elif (
        not app_oh.fired
        or (app_oh.match.slots or {}).get("recovery") != "visual_overhead"
    ):
        print(f"  FAIL Approach should take request overhead: {app_oh.describe()}")
        bad += 1
    else:
        print("tower/approach recovery request — VFR on both; instrument Approach only")
    wx_land = atc_phrase.Weather(210, 5, 29.92, "", visibility_sm=10)
    land = atc_phrase.build_clear_land(nellis, "Fleece 1", wx_land, "21R")
    if "check gear down" not in land.lower() or "cleared to land" not in land.lower():
        print(f"  FAIL clear land needs gear down: {land}")
        bad += 1
    else:
        print(f"tower clear land — {land}")

    class _Multi:
        signup_count = 2
        flight_qty = 2
        flight_callsign = "Fleece 1"
        seat = 1

        @property
        def squawk_in_sequence(self) -> bool:
            return True

    st_ohb: dict = {"active_recovery": "visual_overhead"}
    land_ohb = atc_phrase.build_clear_land(
        nellis, "Fleece 1", wx_land, "21R", opus=_Multi(), state=st_ohb
    )
    if land_ohb.lower().count("cleared to land") != 1 or "one two" in land_ohb.lower():
        print(f"  FAIL overhead must clear the flight once: {land_ohb}")
        bad += 1
    else:
        atc_phrase.commit_landing_clearance(st_ohb)
        if not atc_phrase.landing_already_cleared(st_ohb, opus=_Multi()):
            print(f"  FAIL overhead should latch after one clearance: {st_ohb}")
            bad += 1
        else:
            print(f"tower overhead multi-ship clear land — {land_ohb}")

    st_si: dict = {"active_recovery": "straight_in"}
    land_si1 = atc_phrase.build_clear_land(
        nellis, "Fleece 1", wx_land, "21R", opus=_Multi(), state=st_si
    )
    atc_phrase.commit_landing_clearance(st_si)
    land_si2 = atc_phrase.build_clear_land(
        nellis, "Fleece 1", wx_land, "21R", opus=_Multi(), state=st_si
    )
    atc_phrase.commit_landing_clearance(st_si)
    if (
        "fleece one one" not in land_si1.lower()
        or "fleece one two" in land_si1.lower()
        or "fleece one two" not in land_si2.lower()
        or not atc_phrase.should_hold_for_landing_clearances(
            {"landing_cleared_seats": [1], "landing_ships_total": 2, "active_recovery": "straight_in"},
            opus=_Multi(),
        )
        or atc_phrase.should_hold_for_landing_clearances(st_si, opus=_Multi())
    ):
        print(
            f"  FAIL straight-in must clear element callsigns one ship per TX: "
            f"{land_si1!r} then {land_si2!r} state={st_si}"
        )
        bad += 1
    else:
        print(f"tower straight-in sequenced clear land — {land_si1} / {land_si2}")

    class _QtyOnly:
        signup_count = 1
        flight_qty = 2

        @property
        def squawk_in_sequence(self) -> bool:
            return True

    land_solo = atc_phrase.build_clear_land(
        nellis, "Fleece 1", wx_land, "21R", opus=_QtyOnly()
    )
    if land_solo.lower().count("cleared to land") != 1 or "fleece two" in land_solo.lower():
        print(f"  FAIL planned qty alone must not double clear land: {land_solo}")
        bad += 1
    else:
        print(f"tower solo-on-qty2 clear land — {land_solo}")

    closer_hits = 0
    for _ in range(40):
        h = atc_phrase.build_approach_tower_handoff(nellis, "Fleece 1")
        if any(x in h.lower() for x in ("good day", "see ya", "see you")):
            closer_hits += 1
    if closer_hits < 10:
        print(f"  FAIL handoff closers too rare ({closer_hits}/40): last={h}")
        bad += 1
    else:
        print(f"tower handoff closers — {closer_hits}/40 samples had good day/see ya/see you")

    import runway_position as rp

    field = rp.airport_field_latlon(nellis)
    if field is None:
        print("  FAIL airport_field_latlon for Nellis")
        bad += 1
    else:
        d_near = atc_phrase._haversine_nm(field[0], field[1], field[0] + 0.01, field[1])
        # ~0.6 NM north of field centre
        trig12 = rp.resolve_step_trigger(
            {"template": "cleared_approach", "trigger": {"within_nm": 12}}
        )
        held, _ = rp.within_nm_held(trig12, 11.0)
        if not held or trig12 is None or float(trig12.within_nm or 0) != 12:
            print(f"  FAIL contact tower 12 NM gate: {trig12} held={held}")
            bad += 1
        st_land = {
            "approach_plan": {"pattern": "visual_overhead", "runway": "21R"},
            "active_recovery": "visual_overhead",
        }
        trig2 = rp.resolve_step_trigger(
            {"template": "clear_land", "trigger": {"gap_s": 5}},
            state=st_land,
        )
        if trig2 is not None and trig2.within_nm is not None:
            print(f"  FAIL OHB clear_land must not auto on field distance: {trig2}")
            bad += 1
        st_si = {
            "approach_plan": {"pattern": "straight_in", "runway": "21R"},
            "active_recovery": "straight_in",
        }
        trig6 = rp.resolve_step_trigger(
            {"template": "clear_land", "trigger": {"gap_s": 5}},
            state=st_si,
        )
        gates_ok = True
        if trig6 is None or abs(float(trig6.within_nm or 0) - 6.0) > 0.01:
            print(f"  FAIL straight-in clear_land should be 6 NM: {trig6}")
            bad += 1
            gates_ok = False
        trig_brk_si = rp.resolve_step_trigger(
            {"template": "right_break"},
            state=st_si,
        )
        if trig_brk_si is None or abs(float(trig_brk_si.within_nm or 0) - 12.0) > 0.01:
            print(
                f"  FAIL straight-in tower check-in should be 12 NM: {trig_brk_si}"
            )
            bad += 1
            gates_ok = False
        trig_brk_ohb = rp.resolve_step_trigger(
            {"template": "right_break"},
            state=st_land,
        )
        if trig_brk_ohb is not None and trig_brk_ohb.within_nm is not None:
            print(
                f"  FAIL OHB tower check-in should have no NM gate: {trig_brk_ohb}"
            )
            bad += 1
            gates_ok = False
        if atc_phrase.should_skip_approach_step(
            {"template": "right_break"}, state=st_si
        ):
            print("  FAIL straight-in must not skip tower check-in")
            bad += 1
            gates_ok = False
        if gates_ok:
            print(
                f"tower distance gates — contact/check-in 12 NM, "
                f"straight-in land 6 NM (sample {d_near:.2f} NM offset)"
            )

        st_flex = {
            "active_recovery": "tactical_overhead",
            "approach_plan": {"pattern": "tactical_overhead", "runway": "21R"},
        }
        ga_flex = atc_phrase.assign_go_around_plan(
            nellis, runway="21R", state=st_flex, prefer="flex"
        )
        trig_flex = rp.resolve_step_trigger(
            {"template": "clear_land", "trigger": {"gap_s": 5}},
            state=st_flex,
        )
        if (
            str(ga_flex.get("kind") or "") != "reentry"
            or str(ga_flex.get("seek_template") or "") != "clear_land"
            or st_flex.get("active_recovery") != "straight_in"
            or (st_flex.get("approach_plan") or {}).get("pattern") != "straight_in"
            or not atc_phrase.reentry_go_around_pending(st_flex)
            or not st_flex.get("pattern_land_needs_leave")
            or trig_flex is None
            or abs(float(trig_flex.within_nm or 0) - 6.0) > 0.01
        ):
            print(
                f"  FAIL Flex reentry should seek land at 6 NM (short final): "
                f"ga={ga_flex} state={st_flex} trig={trig_flex}"
            )
            bad += 1
        else:
            print("flex/duck reentry — straight-in land on short final (6 NM)")

        st_closed = {
            "active_recovery": "tactical_overhead",
            "approach_plan": {"pattern": "tactical_overhead", "runway": "21R"},
        }
        next_after_flex = atc_phrase.pick_vfr_go_around_plan(
            "21R", nellis, last=ga_flex
        )
        if str(next_after_flex.get("kind") or "") != "closed_traffic":
            print(f"  FAIL after Flex, next VFR go-around should be closed traffic: {next_after_flex}")
            bad += 1
        kinds = {
            str(atc_phrase.pick_vfr_go_around_plan("21R", nellis).get("kind") or "")
            for _ in range(40)
        }
        if "closed_traffic" not in kinds or "reentry" not in kinds:
            print(f"  FAIL 21R go-around mix should include closed and Flex: {kinds}")
            bad += 1

        ga_closed = atc_phrase.assign_go_around_plan(
            nellis, runway="21R", state=st_closed, prefer="closed"
        )
        trig_closed_brk = rp.resolve_step_trigger(
            {"template": "right_break"},
            state=st_closed,
        )
        trig_closed_land = rp.resolve_step_trigger(
            {"template": "clear_land", "trigger": {"gap_s": 5}},
            state=st_closed,
        )
        if (
            str(ga_closed.get("kind") or "") != "closed_traffic"
            or str(ga_closed.get("seek_template") or "") != "clear_land"
            or st_closed.get("active_recovery") != "tactical_overhead"
            or trig_closed_brk is not None
            or trig_closed_land is None
            or abs(float(trig_closed_land.within_nm or 0) - 2.0) > 0.01
        ):
            print(
                f"  FAIL closed traffic should seek land at 2 NM, no check-in: "
                f"{ga_closed} {st_closed} brk={trig_closed_brk} land={trig_closed_land}"
            )
            bad += 1

        st_after_flex = {
            "active_recovery": "straight_in",
            "overhead_recovery": "tactical_overhead",
            "approach_plan": {"pattern": "straight_in", "runway": "21R"},
            "go_around_plan": dict(ga_flex),
        }
        ga_restore = atc_phrase.assign_go_around_plan(
            nellis, runway="21R", state=st_after_flex, prefer="closed"
        )
        trig_restore = rp.resolve_step_trigger(
            {"template": "right_break"},
            state=st_after_flex,
        )
        trig_land_closed = rp.resolve_step_trigger(
            {"template": "clear_land", "trigger": {"gap_s": 5}},
            state=st_after_flex,
        )
        if (
            st_after_flex.get("active_recovery") != "tactical_overhead"
            or str(ga_restore.get("kind") or "") != "closed_traffic"
            or str(ga_restore.get("seek_template") or "") != "clear_land"
            or trig_restore is not None
            or trig_land_closed is None
            or abs(float(trig_land_closed.within_nm or 0) - 2.0) > 0.01
        ):
            print(
                f"  FAIL closed after Flex must restore overhead and land at 2 NM: "
                f"{ga_restore} {st_after_flex} brk={trig_restore} land={trig_land_closed}"
            )
            bad += 1
        else:
            dep = rp.FlightStatus(
                fixes=[
                    rp.UnitFix(
                        "1", "me", 2500.0, 0.0, 10.0, None, None, None, own=True
                    )
                ]
            )
            fin = rp.FlightStatus(
                fixes=[
                    rp.UnitFix(
                        "1", "me", -1500.0, 200.0, 20.0, None, None, None, own=True
                    )
                ]
            )
            ok_dep, _ = rp.on_base_or_short_final(dep, within_nm=2.0)
            ok_fin, _ = rp.on_base_or_short_final(fin, within_nm=2.0)
            if ok_dep or not ok_fin:
                print(f"  FAIL short-final gate: dep={ok_dep} final={ok_fin}")
                bad += 1
            else:
                print("closed / reentry — already with Tower; land on base / short final")

        rb_closed = atc_phrase.build_readback_checklist(
            "go_around", nellis, None, None, "21R", state=st_closed
        )
        rb_flex = atc_phrase.build_readback_checklist(
            "go_around", nellis, None, None, "21R", state=st_flex
        )
        keys_closed = {str(i.get("key") or "") for i in rb_closed}
        spoken_closed = " ".join(str(i.get("spoken") or "") for i in rb_closed).lower()
        spoken_flex = " ".join(str(i.get("spoken") or "") for i in rb_flex).lower()
        if (
            "instruction" not in keys_closed
            or "closed traffic" not in spoken_closed
            or "flex" not in spoken_flex
        ):
            print(
                f"  FAIL go-around readback checklist: "
                f"closed={rb_closed} flex={rb_flex}"
            )
            bad += 1
        else:
            print("go-around readback — closed traffic / Flex hinges")

        trig18 = rp.resolve_step_trigger(
            {
                "template": "departure_handoff",
                "trigger": {"within_nm": 18, "when": "leaving"},
            }
        )
        held_in, _ = rp.within_nm_held(trig18, 17.0)
        held_out, _ = rp.within_nm_held(trig18, 18.5)
        if (
            trig18 is None
            or float(trig18.within_nm or 0) != 18
            or trig18.when != "leaving"
            or held_in
            or not held_out
        ):
            print(
                f"  FAIL departure handoff 18 NM leaving gate: {trig18} "
                f"in17={held_in} out18.5={held_out}"
            )
            bad += 1
        else:
            print(
                "departure handoff — auto beyond 18 NM "
                "(manual Play still works earlier)"
            )

        trig10 = rp.resolve_step_trigger(
            {
                "template": "climb_cruise",
                "trigger": {"within_nm": 10, "when": "leaving"},
            }
        )
        c_in, _ = rp.within_nm_held(trig10, 9.0)
        c_out, _ = rp.within_nm_held(trig10, 10.5)
        opus_hi = atc_phrase.synthetic_flight_context("Fleece 1")
        opus_hi.fp_altitude = "220"
        cruise_txt = atc_phrase.build_template_text(
            nellis,
            "climb_cruise",
            "Fleece 1",
            atc_phrase.Weather(210, 5, 29.92, ""),
            "21R",
            opus=opus_hi,
            state={"initial_climb_ft": 15000},
        )
        skip_same = atc_phrase.should_skip_cruise_climb_step(
            {"template": "climb_cruise"},
            state={"initial_climb_ft": 15000, "filed_altitude_ft": 15000},
        )
        skip_higher = atc_phrase.should_skip_cruise_climb_step(
            {"template": "climb_cruise"},
            state={"initial_climb_ft": 15000, "filed_altitude_ft": 22000},
            opus=opus_hi,
        )
        if (
            trig10 is None
            or float(trig10.within_nm or 0) != 10
            or trig10.when != "leaving"
            or c_in
            or not c_out
            or "climb and maintain" not in cruise_txt.lower()
            or "fifteen" in cruise_txt.lower()
            or "one five" in cruise_txt.lower()
            or not skip_same
            or skip_higher
        ):
            print(
                f"  FAIL climb to cruise: trig={trig10} in9={c_in} out10.5={c_out} "
                f"txt={cruise_txt!r} skip_same={skip_same} skip_higher={skip_higher}"
            )
            bad += 1
        else:
            print("climb to cruise — auto beyond 10 NM (filed, not the 15k interim)")

        hold_rb = atc_phrase.auto_tx_hold_reason(
            {"awaiting_readback": True, "readback_items": [{"key": "climb"}]}
        )
        hold_clear = atc_phrase.auto_tx_hold_reason({"awaiting_readback": False})
        if hold_rb != "waiting for readback" or hold_clear:
            print(f"  FAIL auto-TX must wait for readback: {hold_rb!r} {hold_clear!r}")
            bad += 1
        else:
            print("auto-TX — waits for readback before the next command")

        dep_reqs = {
            k
            for k, _ in atc_phrase.pilot_requests_for_channel(
                "tower", phase="departure", template="lineup"
            )
        }
        app_reqs = {
            k
            for k, _ in atc_phrase.pilot_requests_for_channel(
                "tower", phase="approach", template="clear_land"
            )
        }
        if "request_lineup" not in dep_reqs or "request_rolling" not in dep_reqs:
            print(f"  FAIL departure Tower should offer LUAW/rolling: {dep_reqs}")
            bad += 1
        elif "request_landing" in dep_reqs:
            print(f"  FAIL departure Tower should not offer landing requests: {dep_reqs}")
            bad += 1
        elif "request_lineup" in app_reqs or "request_rolling" in app_reqs:
            print(f"  FAIL approach Tower must not offer LUAW/rolling: {app_reqs}")
            bad += 1
        elif "request_landing" not in app_reqs or "request_go_around" not in app_reqs:
            print(f"  FAIL approach Tower should offer landing/go-around: {app_reqs}")
            bad += 1
        elif atc_phrase.takeoff_offer_visible(
            {"pending_takeoff_offer": "rolling"},
            channel="tower",
            phase="approach",
            template="clear_land",
        ):
            print("  FAIL rolling offer must not show on approach Tower")
            bad += 1
        else:
            print(
                "tower pilot requests — departure LUAW/rolling; "
                "approach gear-down / go-around (no takeoff offer)"
            )

        ho_on = {
            k
            for k, _ in atc_phrase.pilot_requests_for_channel(
                "departure", phase="departure", template="departure_handoff"
            )
        }
        ho_off = {
            k
            for k, _ in atc_phrase.pilot_requests_for_channel(
                "departure", phase="departure", template="radar_contact"
            )
        }
        if "request_handoff" not in ho_on:
            print(f"  FAIL departure handoff step should offer Request handoff: {ho_on}")
            bad += 1
        elif "request_handoff" in ho_off:
            print(f"  FAIL radar contact must not offer Request handoff: {ho_off}")
            bad += 1
        else:
            print("departure Request handoff — only on the handoff step")

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

    def run(text: str, *, phase: str, channel: str, expected: str = "", step_id: str = ""):
        return voice_intent.evaluate(
            text,
            channel=channel,
            phase=phase,
            expected=expected,
            callsign=CALLSIGN,
            steps=PHRASE_STEPS,
            current_step_id=step_id,
        )

    authored = run(
        "Delivery, Fleece 1, hit me with the clearance",
        phase="departure",
        channel="delivery",
        expected="clearance",
        step_id="del_clearance",
    )
    if not authored.fired or not str(authored.match.intent).startswith("step:"):
        print(f"  FAIL authored phrase should fire: {authored.describe()}")
        bad += 1

    bare = run(
        "hit me with the clearance",
        phase="departure",
        channel="delivery",
        expected="clearance",
        step_id="del_clearance",
    )
    if not bare.fired or not str(bare.match.intent).startswith("step:"):
        print(f"  FAIL authored phrase on the due step needs no agency: {bare.describe()}")
        bad += 1

    wrong_step = run(
        "hit me with the clearance",
        phase="departure",
        channel="ground",
        expected="taxi",
        step_id="gnd_taxi",
    )
    if wrong_step.fired and str(getattr(wrong_step.match, "intent", "")).startswith("step:"):
        print(f"  FAIL clearance phrase must not fire on taxi step: {wrong_step.describe()}")
        bad += 1

    stock = run(
        "Ground, Fleece 1, request taxi",
        phase="departure",
        channel="ground",
        expected="taxi",
        step_id="gnd_taxi",
    )
    if not stock.fired or stock.match.intent != "ready_taxi":
        print(f"  FAIL stock grammar still works with steps loaded: {stock.describe()}")
        bad += 1

    half = voice_intent.evaluate(
        "Delivery, Fleece 1, request",
        channel="delivery",
        phase="departure",
        expected="clearance",
        callsign=CALLSIGN,
    )
    if half.fired and half.match and half.match.intent == "ready_clearance":
        print(f"  FAIL lone 'request' must not advance clearance: {half.describe()}")
        bad += 1

    del_cues = voice_intent.suggestions(
        phase="departure",
        channel="delivery",
        expected="clearance",
        steps=PHRASE_STEPS,
        current_step_id="del_clearance",
        limit=5,
        advance_limit=2,
        optional_limit=3,
    )
    del_says = [str(s) for s, *_ in del_cues]
    taxi_cues = voice_intent.suggestions(
        phase="departure",
        channel="ground",
        expected="taxi",
        steps=PHRASE_STEPS,
        current_step_id="gnd_taxi",
        limit=5,
        advance_limit=2,
        optional_limit=3,
    )
    taxi_says = [str(s) for s, *_ in taxi_cues]
    if "hit me with the clearance" not in del_says:
        print(f"  FAIL clearance cues should show the mission phrase: {del_cues}")
        bad += 1
    elif "lets roll to the end" in del_says:
        print(f"  FAIL taxi mission phrase must not show on clearance: {del_cues}")
        bad += 1
    elif any("clearance" in s.lower() and s != "hit me with the clearance" for s in del_says[:2]):
        print(f"  FAIL stock clearance cue should yield to the mission phrase: {del_cues}")
        bad += 1
    elif "lets roll to the end" not in taxi_says:
        print(f"  FAIL taxi cues should show the mission phrase: {taxi_cues}")
        bad += 1
    elif "hit me with the clearance" in taxi_says:
        print(f"  FAIL clearance mission phrase must not show on taxi: {taxi_cues}")
        bad += 1
    else:
        print("custom step cues — current mission phrases only")

    print(f"step phrases — {'ok' if not bad else f'{bad} problem(s)'}")
    return bad


def expecting() -> int:
    """Short-form answers while ATC is waiting on a specific call."""
    bad = 0

    # Clearance readback: bare spoken digits are enough with no agency opener.
    result = voice_intent.evaluate(
        "zero five five one",
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
