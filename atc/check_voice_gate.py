"""
Regression check for voice gating: run `py -3 check_voice_gate.py`.

The flight shares the PTT, so the grammar is a long list of judgment calls about
what counts as ATC business. Add a case here whenever a phrase fires that should
not, or stays silent when it should not. Needs no microphone and no model —
voice_intent alone, so it runs anywhere.
"""

import voice_intent
import voice_nlu

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
    ("Blackjack, Fleece 1, request Joshua", "blackjack", "flight", True, "request_joshua"),
    ("Joshua, Fleece 1, checking in", "joshua", "flight", True, "joshua_check_in"),
    ("Joshua Control, Fleece 1, with you", "joshua", "flight", True, "joshua_check_in"),
    ("Joshua, Fleece 1, checking out", "joshua", "flight", True, "joshua_check_out"),
    ("Joshua, Fleece 1, switch Blackjack", "joshua", "flight", True, "joshua_check_out"),
    ("Joshua, Fleece 1, request picture", "joshua", "flight", False, None),
    ("Joshua, Fleece 1, request tanker", "joshua", "flight", True, "request_tanker"),
    # Agency sandbox: address + flight phase, even when the cursor is still Blackjack.
    ("Joshua, Fleece 1, checking in", "blackjack", "flight", True, "joshua_check_in"),
    ("Nellis Control, Fleece 1, checking in", "control_east", "flight", True, "control_check_in"),
    ("Nellis Control, Fleece 1, checking in", "blackjack", "flight", True, "control_check_in"),
    ("Control East, Fleece 1, with you", "control_east", "flight", True, "control_check_in"),
    ("Control West, Fleece 1, checking in", "control_west", "flight", True, "control_check_in"),
    ("Sally, Fleece 1, checking in", "control_east", "flight", True, "control_check_in"),
    ("Lee, Fleece 1, checking in", "control_west", "flight", True, "control_check_in"),
    ("Sally, Fleece 1, with you", "blackjack", "flight", True, "control_check_in"),
    ("Los Angeles Center, Fleece 1, checking in", "center", "flight", True, "center_check_in"),
    ("LA Center, Fleece 1, with you", "center", "flight", True, "center_check_in"),
    ("Nellis Control, Fleece 1, contact Approach", "control_east", "flight", True, "control_handoff"),
    ("Blackjack, Fleece 1, request tanker", "blackjack", "flight", True, "request_tanker"),
    ("Blackjack, Fleece 1, request another tanker", "blackjack", "flight", True, "request_tanker"),
    ("Blackjack, Fleece 1, request the closest tanker", "blackjack", "flight", True, "request_tanker"),
    ("Blackjack, Fleece 1, request Texaco 5", "blackjack", "flight", True, "request_tanker"),
    ("Blackjack, Fleece 1, request Texaco five", "blackjack", "flight", True, "request_tanker"),
    ("Blackjack, Fleece 1, request vectors to the tanker", "blackjack", "flight", True, "request_tanker"),
    ("Bandsaw, Fleece 1, going to the tanker", "bandsaw", "flight", True, "request_tanker"),
    ("Blackjack, Fleece 1, back from the tanker", "blackjack", "flight", True, "tanker_return"),
    ("Blackjack, Fleece 1, off the tanker", "blackjack", "flight", True, "tanker_return"),
    ("Blackjack, Fleece 1, check back in", "blackjack", "flight", True, "tanker_return"),
    ("Bandsaw, Fleece 1, returning from the tanker", "bandsaw", "flight", True, "tanker_return"),
    ("Blackjack, Fleece 1, say TACAN", "blackjack", "flight", True, "tanker_tacan"),
    ("Blackjack, Fleece 1, say tanker frequency", "blackjack", "flight", True, "tanker_freq"),
    # Blackjack check-in closer — not a tanker-freq request.
    ("Copy, check out this frequency, Dagger one", "blackjack", "flight", False, None),
    ("Blackjack, Dagger 1, check out this frequency", "blackjack", "flight", False, None),
    ("Blackjack, Fleece 1, say tanker bullseye", "blackjack", "flight", True, "tanker_bullseye"),
    ("Texaco, Fleece 1, request rejoin", "tanker", "flight", True, "tanker_check_in"),
    ("Texaco, Fleece 1, request reform", "tanker", "flight", True, "tanker_check_in"),
    ("Texaco 1, Fleece 1, left observation", "tanker", "flight", True, "tanker_astern"),
    ("Texaco, Fleece 1, astern", "tanker", "flight", True, "tanker_astern"),
    ("Texaco, Fleece 1, contact", "tanker", "flight", True, "tanker_contact"),
    ("Texaco, Fleece 1, disconnect", "tanker", "flight", True, "tanker_disconnect"),
    ("Texaco, Fleece 1, request departure", "tanker", "flight", True, "tanker_depart"),
    ("Texaco 5, going exit high, thanks for the fuel", "tanker", "flight", True, "tanker_depart"),
    ("Hey boom, how you doing?", "tanker", "flight", True, "tanker_chat_start"),
    ("Talk later", "tanker", "flight", True, "tanker_chat_stop"),
    ("Texaco, Fleece 1, how's it going", "tanker", "flight", True, "tanker_chat_start"),
    ("Texaco, Fleece 1, stop talking", "tanker", "flight", True, "tanker_chat_stop"),
    ("Blackjack, Fleece 1, push Bandsaw", "blackjack", "flight", True, "request_bandsaw"),
    ("Blackjack, Fleece 1, request ANSA", "blackjack", "flight", True, "request_bandsaw"),
    # Knight Ops — preflight WORDS / start, postflight codes. Not C2.
    ("Knight Ops, Fleece 1, request current WORDS", "ops", "departure", True, "ops_request_words"),
    ("Wool Ops, Fleece 1, request current WORDS", "ops", "departure", True, "ops_request_words"),
    ("Toro Ops, Fleece 1, request current WORDS", "ops", "departure", True, "ops_request_words"),
    ("Ops, Fleece 1, request current words", "ops", "departure", True, "ops_request_words"),
    ("Night Ops, Fleece 1, request current WORDS", "ops", "departure", True, "ops_request_words"),
    ("Fleece 1, request current WORDS", "ops", "departure", True, "ops_request_words"),
    ("Request current WORDS", "ops", "departure", True, "ops_request_words"),
    ("Knight Ops, Fleece 1, request start", "ops", "departure", True, "ops_request_start"),
    ("Knight Ops, Fleece 1, ready to start", "ops", "departure", True, "ops_request_start"),
    ("Knight Ops, Fleece 1, checking in", "ops", "departure", True, "ops_check_in"),
    ("Knight Ops, Fleece 1, with you", "ops", "departure", True, "ops_check_in"),
    ("Knight Ops, Fleece 1, request current WORDS", "delivery", "departure", True, "ops_request_words"),
    ("Knight Ops, Fleece 1, Fleece 1-1 Code 1, Fleece 1-2 Code 2", "ops", "approach", True, "ops_status"),
    ("Knight Ops, Fleece 1, code 1", "ops", "approach", True, "ops_status"),
    ("Knight Ops, Fleece 1, request picture", "ops", "flight", False, None),
    ("Ops check", "ops", "departure", False, None),
    ("Two, ops check", "ops", "departure", False, None),
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
    # Elevator: any radar agency will move you; Ground and Tower will not.
    ("Nellis Control, Fleece 1, request elevator one four thousand", "control_east", "flight", True, "request_altitude_change"),
    ("Blackjack, Fleece 1, request elevator angels four", "blackjack", "flight", True, "request_altitude_change"),
    ("Bandsaw, Fleece 1, request descent to angels one zero", "bandsaw", "flight", True, "request_altitude_change"),
    ("Departure, Fleece 1, request climb to flight level two two zero", "departure", "departure", True, "request_altitude_change"),
    ("Nellis Control, Fleece 1, request higher altitude", "control_east", "flight", True, "request_altitude_change"),
    ("Ground, Fleece 1, request elevator one four thousand", "ground", "departure", False, None),
    # Vectors to a named point, and to the nearest suitable field.
    ("Nellis Control, Fleece 1, request vectors to Stryk", "control_east", "approach", True, "request_point_vectors"),
    ("Blackjack, Fleece 1, request bearing to Mormon Mesa", "blackjack", "flight", True, "request_point_vectors"),
    ("Nellis Control, Fleece 1, how far to Beatty", "control_east", "flight", True, "request_point_vectors"),
    ("Approach, Fleece 1, request vectors to the nearest divert", "approach", "approach", True, "request_divert"),
    ("Blackjack, Fleece 1, request the closest suitable field", "blackjack", "flight", True, "request_divert"),
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
    # own callsign without the agency is not a radio call
    ("Fleece 1, ready to taxi", "ground", "departure", False, None),
    ("Fleece 1, request the current winds", "tower", "departure", False, None),
    # Departure radar contact — airborne check-in
    ("Departure, Fleece 1, with you", "departure", "departure", True, "departure_check_in"),
    ("Nellis Departure, Fleece 1, airborne", "departure", "departure", True, "departure_check_in"),
    ("Fleece 1, airborne", "departure", "departure", False, None),
    # Unrestricted climb — Tower only, hidden from kneeboard cues
    ("Tower, Fleece 1, request unrestricted climb", "tower", "departure", True, "request_unrestricted_climb"),
    ("Fleece 1, request unrestricted", "tower", "departure", False, None),
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
    elif "proceed direct" in bj_exit.lower():
        print(f"  FAIL Blackjack range exit should leave routing to Control: {bj_exit}")
        bad += 1
    elif "cleared" in bj_exit.lower():
        print(f"  FAIL Blackjack range exit should not say cleared: {bj_exit}")
        bad += 1
    elif "descend" in bj_exit.lower():
        print(f"  FAIL Blackjack range exit should leave descent to Control: {bj_exit}")
        bad += 1
    elif "nellis control" not in bj_exit.lower():
        print(f"  FAIL Blackjack range exit should hand to Nellis Control: {bj_exit}")
        bad += 1
    elif "contact approach" in bj_exit.lower():
        print(f"  FAIL Blackjack range exit should not skip Control: {bj_exit}")
        bad += 1
    else:
        print(f"blackjack range exit — {bj_exit}")
    ctrl_vmc = atc_phrase.build_control_check_in(
        "Fleece 1", airport=nellis, plan=plan_vmc
    )
    if "proceed direct arcoe" not in ctrl_vmc.lower():
        print(f"  FAIL Control check-in should proceed direct the fix: {ctrl_vmc}")
        bad += 1
    elif "descend pilot discretion" not in ctrl_vmc.lower():
        print(f"  FAIL Control check-in should start the IAF descent: {ctrl_vmc}")
        bad += 1
    elif "expect" not in ctrl_vmc.lower():
        print(f"  FAIL Control check-in should issue expect recovery: {ctrl_vmc}")
        bad += 1
    else:
        print(f"control check-in — {ctrl_vmc}")
    import time as _time

    old_miss = atc_phrase._ownship_miss_until
    atc_phrase._ownship_miss_until = _time.time() + 120
    try:
        west_txt = atc_phrase.build_template_text(
            nellis,
            "bj_range_exit",
            "Fleece 1",
            vmc,
            str(plan_vmc.get("runway") or "21R"),
            channel="blackjack",
            config={"_": 1},
            state={"ownship_ll": [37.5, -116.5]},
        )
    finally:
        atc_phrase._ownship_miss_until = old_miss
    if "nellis control" not in west_txt.lower() or "eight" not in west_txt.lower():
        print(f"  FAIL western range exit should hand to Control West (Local 8): {west_txt}")
        bad += 1
    else:
        print(f"blackjack range exit (west) — {west_txt}")
    bj_hold = atc_phrase.build_blackjack_range_exit(
        nellis, "Fleece 1", plan=plan_vmc, include_handoff=False
    )
    if "contact" in bj_hold.lower():
        print(f"  FAIL remain-freq range exit must not hand off: {bj_hold}")
        bad += 1
    elif "remain this frequency" not in bj_hold.lower():
        print(f"  FAIL remain-freq range exit wording: {bj_hold}")
        bad += 1
    else:
        print(f"blackjack range exit (remain) — {bj_hold}")
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
    elif "descend" in bj_app.lower():
        print(f"  FAIL Approach handoff must not re-issue descent: {bj_app}")
        bad += 1
    elif "nellis control" not in bj_after.lower() or "proceed direct" in bj_after.lower():
        print(f"  FAIL already-released range exit still hands to Control: {bj_after}")
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
    ctrl_ifr = atc_phrase.build_control_check_in(
        "Fleece 1", airport=nellis, plan=plan_ifr
    )
    if "descend pilot discretion" not in ctrl_ifr.lower():
        print(f"  FAIL IFR Control check-in should start the IAF descent: {ctrl_ifr}")
        bad += 1
    elif "fifteen" not in ctrl_ifr.lower() and "one five" not in ctrl_ifr.lower() and "one fife" not in ctrl_ifr.lower():
        print(f"  FAIL IFR Control check-in should use the 15000 IAF altitude: {ctrl_ifr}")
        bad += 1
    else:
        print(f"control check-in (IFR IAF) — {ctrl_ifr}")
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
    # ARCOE = ILS Z / HI-TACAN Z 21L, KRYSS = ILS X (not ILS Z at ARCOE/15k).
    for iaf_name, want_inst, want_alt in (
        ("DUDBE", "HI_TACAN_Y_21L", 15000),
        ("ARCOE", "ILS_Z_21L", 15000),
        ("KRYSS", "ILS_X_21L", 8800),
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

    # Filed KRYSS with no ARCOE is ILS X at 8800 — not ILS Zulu via ARCOE/15k.
    class _KryssFP:
        fp_route_string = "KLSV DREAM COYOT KRYSS KLSV"
        fp_altitude = "FL240"

    plan_kryss_ifr = atc_phrase.assign_approach_plan(
        nellis, ifr, state={}, force=True, opus=_KryssFP()
    )
    if plan_kryss_ifr.get("pattern") != "instrument":
        print(f"  FAIL filed KRYSS in IMC stays instrument: {plan_kryss_ifr}")
        bad += 1
    elif (
        plan_kryss_ifr.get("iaf") != "KRYSS"
        or plan_kryss_ifr.get("instrument_id") != "ILS_X_21L"
        or plan_kryss_ifr.get("source") != "route"
    ):
        print(f"  FAIL filed KRYSS should be ILS X: {plan_kryss_ifr}")
        bad += 1
    elif int(plan_kryss_ifr.get("descend_ft") or 0) != 8800:
        print(f"  FAIL filed KRYSS crossing should be 8800: {plan_kryss_ifr}")
        bad += 1
    elif "x-ray" not in str(plan_kryss_ifr.get("instrument_say") or "").lower():
        print(f"  FAIL filed KRYSS should say ILS X-ray: {plan_kryss_ifr}")
        bad += 1
    else:
        print(
            f"approach filed KRYSS (IMC) — {plan_kryss_ifr.get('instrument_id')} "
            f"IAF {plan_kryss_ifr.get('iaf')} desc {plan_kryss_ifr.get('descend_ft')}"
        )

    # ARCOE still in the tail keeps HI ILS Z even if KRYSS is the last fix.
    class _ArcoeKryssFP:
        fp_route_string = "KLSV DREAM COYOT ARCOE KRYSS KLSV"
        fp_altitude = "FL240"

    plan_both = atc_phrase.assign_approach_plan(
        nellis, ifr, state={}, force=True, opus=_ArcoeKryssFP()
    )
    if plan_both.get("iaf") != "ARCOE" or plan_both.get("instrument_id") != "ILS_Z_21L":
        print(f"  FAIL ARCOE+KRYSS should stay ILS Z / ARCOE: {plan_both}")
        bad += 1
    elif int(plan_both.get("descend_ft") or 0) != 15000:
        print(f"  FAIL ARCOE+KRYSS crossing should stay 15000: {plan_both}")
        bad += 1

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
    # Only outer range-gate IAFs (ARCOE / DUDBE) — never SHEET/5k on ILS X.
    for label, pos, want_inst, want_iaf in (
        ("north (Arcoe side)", (36.90, -114.90), "ILS_Z_21L", "ARCOE"),
        ("west (Dudbe side)", (36.40, -115.90), "HI_TACAN_Y_21L", "DUDBE"),
        ("near Sheet (still Arcoe gate)", (36.45, -114.85), "ILS_Z_21L", "ARCOE"),
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
        elif want_iaf == "ARCOE" and int(p_pos.get("descend_ft") or 0) != 15000:
            print(f"  FAIL Arcoe gate must be 15000: {p_pos}")
            bad += 1
    print("approach position pick — north/near-Sheet gets ARCOE, west gets DUDBE")

    # Blackjack IMC exit must match Approach (ARCOE/15k), not SHEET/5k.
    st_imc = {}
    plan_near_sheet = atc_phrase.assign_approach_plan(
        nellis, ifr, state=st_imc, force=True, position=(36.45, -114.85)
    )
    bj_imc = atc_phrase.build_blackjack_range_exit(
        nellis, "Fleece 1", plan=plan_near_sheet
    )
    ctrl_imc = atc_phrase.build_control_check_in(
        "Fleece 1", airport=nellis, plan=plan_near_sheet
    )
    clear_near = atc_phrase.build_iaf_clearance(
        nellis, "Fleece 1", plan=plan_near_sheet
    )
    if plan_near_sheet.get("iaf") != "ARCOE" or int(plan_near_sheet.get("descend_ft") or 0) != 15000:
        print(f"  FAIL IMC near Sheet must plan ARCOE/15k: {plan_near_sheet}")
        bad += 1
    elif "sheet" in bj_imc.lower() or "five thousand" in bj_imc.lower():
        print(f"  FAIL Blackjack must not clear SHEET/5k in IMC: {bj_imc}")
        bad += 1
    elif "proceed direct" in bj_imc.lower() or "nellis control" not in bj_imc.lower():
        print(f"  FAIL Blackjack IMC exit should only hand to Control: {bj_imc}")
        bad += 1
    elif "proceed direct arcoe" not in ctrl_imc.lower():
        print(f"  FAIL Control IMC check-in should proceed direct Arcoe: {ctrl_imc}")
        bad += 1
    elif "cross arcoe" not in clear_near.lower():
        print(f"  FAIL Approach clearance should match Control gate: {clear_near}")
        bad += 1
    elif "fifteen thousand" not in ctrl_imc.lower() and "one fife" not in ctrl_imc.lower():
        print(f"  FAIL Control IMC descend should be 15k: {ctrl_imc}")
        bad += 1
    else:
        print(f"blackjack/control IMC align — {bj_imc}")

    # Sticky SHEET leftover from an old position pick must rebuild to the gate.
    st_sheet = {
        "approach_assigned": True,
        "approach_plan": {
            "pattern": "instrument",
            "runway": "21L",
            "instrument_id": "ILS_X_21L",
            "instrument_say": "ILS X-ray runway two one left",
            "iaf": "SHEET",
            "iaf_say": "Sheet",
            "iaf_source": "position",
            "descend_ft": 5000,
            "iaf_altitude_type": "at_or_above",
            "vmc": False,
            "source": "position",
        },
        "active_recovery": "instrument",
        "ownship_ll": [36.45, -114.85],
        "ownship_ll_t": __import__("time").time(),
    }
    plan_fixed_sheet = atc_phrase.assign_approach_plan(
        nellis, ifr, state=st_sheet, force=False
    )
    if plan_fixed_sheet.get("iaf") != "ARCOE" or int(plan_fixed_sheet.get("descend_ft") or 0) != 15000:
        print(f"  FAIL sticky SHEET must rebuild to ARCOE/15k: {plan_fixed_sheet}")
        bad += 1
    else:
        print("approach sticky SHEET rebuild — ARCOE / 15000")

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

    # STRYK recovery: Blackjack clears direct the entry (Stryk / Nixon / Sarah).
    p_west_stryk = atc_phrase.assign_approach_plan(
        nellis, vmc, state={}, force=True, position=(36.45, -116.00)
    )
    if (
        p_west_stryk.get("vfr_recovery") != "STRYK"
        or str(p_west_stryk.get("direct_fix") or "") != "STRYK"
    ):
        print(f"  FAIL western ranges should still proceed direct Stryk: {p_west_stryk}")
        bad += 1
    p_sarah = atc_phrase.assign_approach_plan(
        nellis, vmc, state={}, force=True, position=(36.6075, -115.30055)
    )
    p_nixon = atc_phrase.assign_approach_plan(
        nellis, vmc, state={}, force=True, position=(36.533617, -115.5375)
    )
    p_sarah_rt = atc_phrase.assign_approach_plan(
        nellis,
        vmc,
        state={},
        force=True,
        opus=type("O", (), {"fp_route_string": "KLSV DREAM SARAH KLSV", "fp_altitude": None})(),
    )
    p_nixon_rt = atc_phrase.assign_approach_plan(
        nellis,
        vmc,
        state={},
        force=True,
        opus=type("O", (), {"fp_route_string": "KLSV JUNNO NIXON KLSV", "fp_altitude": None})(),
    )
    entries_ok = True
    for label, plan, want_fix in (
        ("SARAH position", p_sarah, "SARAH"),
        ("SARAH route", p_sarah_rt, "SARAH"),
        ("NIXON position", p_nixon, "NIXON"),
        ("NIXON route", p_nixon_rt, "NIXON"),
    ):
        if plan.get("vfr_recovery") != "STRYK":
            print(f"  FAIL {label} should stay STRYK recovery: {plan}")
            bad += 1
            entries_ok = False
        elif str(plan.get("direct_fix") or "") != want_fix:
            print(f"  FAIL {label} should proceed direct {want_fix}: {plan}")
            bad += 1
            entries_ok = False
    if entries_ok:
        bj_sarah = atc_phrase.build_control_check_in(
            "Fleece 1", airport=nellis, plan=p_sarah
        )
        bj_nixon = atc_phrase.build_control_check_in(
            "Fleece 1", airport=nellis, plan=p_nixon
        )
        app_sarah = atc_phrase.build_approach_recovery(
            nellis, "Fleece 1", vmc, str(p_sarah.get("runway") or "21R"), plan=p_sarah
        )
        if "direct sarah" not in bj_sarah.lower() or "gass peak" in bj_sarah.lower():
            print(f"  FAIL Control SARAH check-in should be direct Sarah: {bj_sarah}")
            bad += 1
        elif "direct nixon" not in bj_nixon.lower() or "gass peak" in bj_nixon.lower():
            print(f"  FAIL Control NIXON check-in should be direct Nixon: {bj_nixon}")
            bad += 1
        elif "stryk" not in app_sarah.lower() or "gass peak" in app_sarah.lower():
            print(f"  FAIL Approach should still expect Stryk recovery: {app_sarah}")
            bad += 1
        else:
            print("approach STRYK — Blackjack direct Sarah / Nixon, recovery Stryk")
    # Sticky western STRYK plan, then ownship at SARAH, must refresh the direct-to.
    st_sticky = {
        "approach_assigned": True,
        "approach_plan": dict(p_west_stryk),
        "active_vfr_recovery": "STRYK",
        "ownship_ll": [36.6075, -115.30055],
        "ownship_ll_t": __import__("time").time(),
    }
    p_sticky = atc_phrase.assign_approach_plan(nellis, vmc, state=st_sticky, force=False)
    if str(p_sticky.get("direct_fix") or "") != "SARAH" or p_sticky.get("vfr_recovery") != "STRYK":
        print(f"  FAIL sticky STRYK at SARAH must refresh direct Sarah: {p_sticky}")
        bad += 1
    else:
        print("approach sticky STRYK at SARAH — direct Sarah")
    p_ask_sarah = atc_phrase.assign_approach_plan(
        nellis, vmc, state={}, force=True, vfr_recovery="SARAH"
    )
    p_ask_nixon = atc_phrase.assign_approach_plan(
        nellis, vmc, state={}, force=True, vfr_recovery="NIXON"
    )
    if (
        p_ask_sarah.get("vfr_recovery") != "STRYK"
        or str(p_ask_sarah.get("direct_fix") or "") != "SARAH"
    ):
        print(f"  FAIL request Sarah should be STRYK / Sarah: {p_ask_sarah}")
        bad += 1
    elif (
        p_ask_nixon.get("vfr_recovery") != "STRYK"
        or str(p_ask_nixon.get("direct_fix") or "") != "NIXON"
    ):
        print(f"  FAIL request Nixon should be STRYK / Nixon: {p_ask_nixon}")
        bad += 1
    else:
        print("approach request Sarah / Nixon — STRYK recovery, direct the entry")

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

    # NAFBI 11-250 §1.12: RWY 21 is calm-wind. Flip only when the along-runway
    # headwind/tailwind *component* exceeds 10 kt — not merely closest heading.
    light_north = atc_phrase.Weather(30, 8, 29.92, "", visibility_sm=10)
    plan_light = atc_phrase.assign_approach_plan(
        nellis, light_north, state={}, force=True
    )
    if not str(plan_light.get("runway") or "").startswith("21"):
        print(f"  FAIL light north wind must stay on 21: {plan_light}")
        bad += 1
    else:
        print(f"approach wind gate light 030/08 — RWY {plan_light.get('runway')}")

    # 090/15 favors 03 by heading (60° vs 120°) but the component is only 7.5 kt.
    east_xwind = atc_phrase.Weather(90, 15, 29.92, "KLSV 09015KT 10SM", visibility_sm=10)
    plan_090 = atc_phrase.assign_approach_plan(nellis, east_xwind, state={}, force=True)
    if not str(plan_090.get("runway") or "").startswith("21"):
        print(f"  FAIL 090/15 must stay on 21 (component 7.5 kt): {plan_090}")
        bad += 1
    else:
        print(f"approach wind gate 090/15 closest-03 but component 7.5 — RWY {plan_090.get('runway')}")
    rwy_090 = atc_phrase.pick_departure_runway(nellis, east_xwind, None)
    if not str(rwy_090).startswith("21"):
        print(f"  FAIL pick_departure_runway 090/15 should be 21: {rwy_090}")
        bad += 1
    rwy_090_active = atc_phrase.active_runway(
        list(nellis.get("runways") or ["21R", "03L"]), 90, 15
    )
    if not str(rwy_090_active).startswith("21"):
        print(f"  FAIL active_runway 090/15 should be 21: {rwy_090_active}")
        bad += 1
    # 090/22: same heading as 090/15, but component 11 kt exceeds 10 → 03.
    east_strong = atc_phrase.Weather(90, 22, 29.92, "", visibility_sm=10)
    rwy_090_22 = atc_phrase.pick_recovery_runway(nellis, east_strong)
    if not str(rwy_090_22).startswith("03"):
        print(f"  FAIL 090/22 component 11 kt should open 03: {rwy_090_22}")
        bad += 1
    else:
        print(f"approach wind gate 090/22 component 11 — RWY {rwy_090_22}")

    # 030/10 is exactly 10 kt — does not *exceed* 10, stay on 21.
    at_limit = atc_phrase.pick_recovery_runway(
        nellis, atc_phrase.Weather(30, 10, 29.92, "")
    )
    if not str(at_limit).startswith("21"):
        print(f"  FAIL 030/10 must stay on 21 (does not exceed 10): {at_limit}")
        bad += 1
    else:
        print(f"approach wind gate 030/10 at-limit — RWY {at_limit}")
    just_over = atc_phrase.pick_recovery_runway(
        nellis, atc_phrase.Weather(30, 11, 29.92, "")
    )
    if not str(just_over).startswith("03"):
        print(f"  FAIL 030/11 should open 03: {just_over}")
        bad += 1
    else:
        print(f"approach wind gate 030/11 exceeds 10 — RWY {just_over}")

    # 081/07 favors 03 by heading but fails the 10 kt component gate — stay on 21.
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

    # §4.1.4 2200L–0800L: dep 03 / arr 21 unless the component gate overrides.
    light = atc_phrase.Weather(210, 8, 29.92, "", visibility_sm=10)
    night = 23 * 60
    day = 13 * 60
    if atc_phrase.parse_mission_local_minutes("13:00:06 Z") != day:
        print(
            f"  FAIL CAOC missionTimeZulu parse: "
            f"{atc_phrase.parse_mission_local_minutes('13:00:06 Z')}"
        )
        bad += 1
    if not atc_phrase.in_night_ops_window(22 * 60) or atc_phrase.in_night_ops_window(8 * 60):
        print("  FAIL night window should include 2200 and exclude 0800")
        bad += 1
    rwy_night_dep = atc_phrase.pick_departure_runway(
        nellis, light, None, local_minutes=night
    )
    if not str(rwy_night_dep).startswith("03"):
        print(f"  FAIL night light-wind departure should be 03: {rwy_night_dep}")
        bad += 1
    else:
        print(f"night §4.1.4 2300L dep — RWY {rwy_night_dep}")
    rwy_night_arr = atc_phrase.pick_recovery_runway(
        nellis, light, local_minutes=night, for_departure=False
    )
    if not str(rwy_night_arr).startswith("21"):
        print(f"  FAIL night light-wind arrival should be 21: {rwy_night_arr}")
        bad += 1
    else:
        print(f"night §4.1.4 2300L arr — RWY {rwy_night_arr}")
    rwy_night_wind = atc_phrase.pick_departure_runway(
        nellis,
        atc_phrase.Weather(210, 15, 29.92, ""),
        None,
        local_minutes=night,
    )
    if not str(rwy_night_wind).startswith("21"):
        print(f"  FAIL night 210/15 should override dep to 21: {rwy_night_wind}")
        bad += 1
    else:
        print(f"night §4.1.4 210/15 overrides dep — RWY {rwy_night_wind}")
    rwy_day_dep = atc_phrase.pick_departure_runway(
        nellis, light, None, local_minutes=day
    )
    if not str(rwy_day_dep).startswith("21"):
        print(f"  FAIL 1300L departure should stay 21: {rwy_day_dep}")
        bad += 1
    rwy_0800 = atc_phrase.pick_departure_runway(
        nellis, light, None, local_minutes=8 * 60
    )
    if not str(rwy_0800).startswith("21"):
        print(f"  FAIL 0800L should already be day (21): {rwy_0800}")
        bad += 1
    rwy_2200 = atc_phrase.pick_departure_runway(
        nellis, light, None, local_minutes=22 * 60
    )
    if not str(rwy_2200).startswith("03"):
        print(f"  FAIL 2200L departure should be 03: {rwy_2200}")
        bad += 1
    else:
        print("night §4.1.4 window edges — 2200L dep 03, 0800L dep 21")

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
        "Ground, Fleece 2, ready to taxi", channel="ground", phase="departure",
        callsign="FLEECE 2", runways=RUNWAYS,
    )
    lead = voice_intent.evaluate(
        "Ground, Fleece 1, ready to taxi", channel="ground", phase="departure",
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

    dash3_pic = voice_intent.evaluate(
        "Blackjack, Fleece 1.3, request picture",
        channel="blackjack",
        phase="flight",
        callsign=CALLSIGN,
        seat=3,
        runways=RUNWAYS,
    )
    dash3_flight = voice_intent.evaluate(
        "Blackjack, Fleece 1, request picture",
        channel="blackjack",
        phase="flight",
        callsign=CALLSIGN,
        seat=3,
        runways=RUNWAYS,
    )
    lead_hears_three = voice_intent.evaluate(
        "Blackjack, Fleece 1.3, request picture",
        channel="blackjack",
        phase="flight",
        callsign=CALLSIGN,
        seat=1,
        runways=RUNWAYS,
    )
    dash3_hears_two = voice_intent.evaluate(
        "Blackjack, Fleece 1.2, request picture",
        channel="blackjack",
        phase="flight",
        callsign=CALLSIGN,
        seat=3,
        runways=RUNWAYS,
    )
    if not dash3_pic.fired or dash3_pic.match.intent != "request_picture":
        print(f"  FAIL seat 3 saying 1.3 picture: {dash3_pic.describe()}")
        bad += 1
    elif not dash3_flight.fired or dash3_flight.match.intent != "request_picture":
        print(f"  FAIL seat 3 saying Fleece 1 picture: {dash3_flight.describe()}")
        bad += 1
    elif lead_hears_three.fired:
        print(f"  FAIL seat 1 must ignore 1.3 picture: {lead_hears_three.describe()}")
        bad += 1
    elif dash3_hears_two.fired:
        print(f"  FAIL seat 3 must ignore 1.2 picture: {dash3_hears_two.describe()}")
        bad += 1
    else:
        print("seat 3 — Fleece 1 / 1.3 picture fires; 1.2 stays flight talk")

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
        ("ops", "departure", "clearance"),
        ("tanker", "flight", "bj_check_in"),
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
        if channel == "ops":
            sayings = [s.lower() for s, _d, r, *_ in prompts if r == "advance"]
            if "request current words" not in sayings:
                print(f"  FAIL OPS cues must tip request current WORDS: {prompts}")
                bad += 1
            if any("request clearance" in s or "request taxi" in s for s, *_ in prompts):
                print(f"  FAIL OPS cues must not tip Delivery/Ground: {prompts}")
                bad += 1
        if channel == "tanker":
            sayings = [s.lower() for s, _d, r, *_ in prompts if r == "advance"]
            if "request rejoin" not in sayings:
                print(f"  FAIL tanker cues must tip request rejoin: {prompts}")
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
    if not any(s == "with you" and needs for s, _d, _r, needs in rc_tips):
        print(f"  FAIL radar-contact 'with you' should require the agency: {rc_tips}")
        bad += 1
    else:
        print("kneeboard agency-required flag — clearance yes, check-in yes")

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
    # Still on Delivery — stay there. Taxi waits until they tune Ground.
    cue_gnd = voice_intent.cue_channel(
        mission_phase="departure",
        cursor_channel="ground",
        tuned_channel="delivery",
    )
    if cue_gnd != "delivery":
        print(f"  FAIL still on Delivery — cues must not jump to Ground: {cue_gnd!r}")
        bad += 1
    off_freq = voice_intent.suggestions(
        phase="departure",
        channel="delivery",
        expected="taxi",
        callsign=CALLSIGN,
        airport_name="Nellis",
        steps=[
            {
                "id": "gnd_taxi",
                "channel": "ground",
                "phase": "departure",
                "template": "taxi",
                "voice_phrases": ["request taxi"],
            }
        ],
        current_step_id="gnd_taxi",
        tuned_channel="delivery",
        next_channel="ground",
        next_freq_mhz=275.8,
        limit=5,
        advance_limit=2,
        optional_limit=3,
    )
    off_says = [str(s).casefold() for s, *_ in off_freq]
    off_adv = [str(s).casefold() for s, _d, r, *_ in off_freq if r == "advance"]
    if (
        not off_adv
        or "tune ground on 275.800" not in off_adv[0]
        or any("request taxi" in s for s in off_says)
    ):
        print(f"  FAIL off-freq cues must tip tune Ground, not taxi: {off_freq}")
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

    # Departure radar contact: agency required (bare "with you" is not enough)
    for text, want in (
        ("Departure, Fleece 1, with you", "departure_check_in"),
        ("Departure, Fleece 1, airborne", "departure_check_in"),
        ("Departure, Fleece 1, checking in", "departure_check_in"),
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

    for text in ("with you", "airborne", "Fleece 1, checking in"):
        ev = voice_intent.evaluate(
            text,
            channel="departure",
            phase="departure",
            expected="radar_contact",
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        if ev.fired:
            print(f"  FAIL departure check-in needs the agency: {text!r} — {ev.describe()}")
            bad += 1

    for text, want in (
        ("Approach, Fleece 1, request handoff", "approach_continue"),
        ("Approach, Fleece 1, established", "approach_established"),
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

    for text in ("established", "Fleece 1, established"):
        ev = voice_intent.evaluate(
            text,
            channel="approach",
            phase="approach",
            expected="cleared_approach",
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        if ev.fired:
            print(f"  FAIL established needs Approach: {text!r} — {ev.describe()}")
            bad += 1

    for text, want in (
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
    elif any(
        r != "advance" and d == "line up and wait" for _s, d, r, *_ in tips_luaw
    ):
        print(f"  FAIL lineup kneeboard must not tip request-LUAW (that's expect): {tips_luaw}")
        bad += 1
    else:
        print("lineup tips ready; clear_takeoff tips in position")
    ready_on_luaw = voice_intent.evaluate(
        "Nellis Tower, Fleece 1, ready for departure",
        channel="tower",
        phase="departure",
        expected="lineup",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if not ready_on_luaw.fired or ready_on_luaw.match.intent != "ready_departure":
        print(
            f"  FAIL ready for departure on LUAW must issue it, not expect: "
            f"{ready_on_luaw.describe()}"
        )
        bad += 1
    nlu_luaw = {i.id for i in voice_nlu.allowed_intents(
        channel="tower", phase="departure", expected="lineup"
    )}
    if "request_lineup" in nlu_luaw:
        print("  FAIL NLU must not offer request_lineup on the LUAW step")
        bad += 1
    elif "ready_departure" not in nlu_luaw:
        print("  FAIL NLU must still offer ready_departure on the LUAW step")
        bad += 1

    pos_on_takeoff = voice_intent.evaluate(
        "Tower, Fleece 1, in position",
        channel="tower",
        phase="departure",
        expected="clear_takeoff",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if not pos_on_takeoff.fired or pos_on_takeoff.match.intent != "in_position":
        print(f"  FAIL in position should clear takeoff: {pos_on_takeoff.describe()}")
        bad += 1
    bare_pos = voice_intent.evaluate(
        "in position",
        channel="tower",
        phase="departure",
        expected="clear_takeoff",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if bare_pos.fired:
        print(f"  FAIL bare in position needs Tower: {bare_pos.describe()}")
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
    luaw_as_pos = voice_intent.evaluate(
        "Tower, Fleece 1, line up and wait",
        channel="tower",
        phase="departure",
        expected="clear_takeoff",
        callsign=CALLSIGN,
        runways=RUNWAYS,
    )
    if luaw_as_pos.fired and luaw_as_pos.match.intent == "in_position":
        print(
            f"  FAIL line up and wait must not count as in position: "
            f"{luaw_as_pos.describe()}"
        )
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

    climb_clearance_items = [
        {
            "key": "climb",
            "label": "Climb / maintain",
            "value": "17,000 ft",
            "spoken": "one seven thousand",
            "hinge": True,
            "highlight": True,
        },
        {
            "key": "squawk",
            "label": "Squawk",
            "value": "2324",
            "spoken": "squawk two tree two four",
            "hinge": True,
        },
    ]
    for text in (
        "climb and maintain one seven thousand",
        "seventeen thousand",
        "one seven thousand",
        "climb and maintain one seven zero",
    ):
        result = voice_intent.evaluate(
            text,
            channel="delivery",
            phase="departure",
            expected="clearance",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=True,
            readback_items=climb_clearance_items,
            last_tx_template="clearance",
            require_address=True,
        )
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(f"  FAIL clearance altitude must close readback: {text!r} — {result.describe()}")
            bad += 1
    replay = voice_intent.evaluate(
        "Delivery, Fleece 1, request clearance",
        channel="delivery",
        phase="departure",
        expected="clearance",
        callsign=CALLSIGN,
        runways=RUNWAYS,
        awaiting_readback=True,
        readback_items=climb_clearance_items,
        last_tx_template="clearance",
        require_address=True,
    )
    if replay.fired and replay.match and replay.match.intent == "ready_clearance":
        print(f"  FAIL request clearance during readback must not re-issue: {replay.describe()}")
        bad += 1
    else:
        print("clearance readback — altitude closes; request clearance does not re-issue")

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

    # Repeating Ground's taxi instructions is the readback — not a new taxi call.
    # After Play the cursor may already be monitor_tower; the Fly tab still
    # pins expected to last_tx_template ("taxi") while the card is open.
    taxi_route_items = [
        {
            "key": "runway",
            "label": "Runway",
            "value": "21R",
            "spoken": "runway two one right",
            "hinge": True,
            "highlight": True,
        },
        {
            "key": "eor",
            "label": "Taxi to",
            "value": "NW EOR",
            "spoken": "northwest EOR",
            "hinge": True,
        },
    ]
    for text, expected in (
        (
            "Ground, Fleece 1, taxi northwest EOR via Foxtrot Echo, runway two one right",
            "taxi",
        ),
        (
            "Ground, Fleece 1, taxi northwest EOR via Foxtrot Echo, runway two one right",
            "monitor_tower",
        ),
        (
            "Nellis Ground, Fleece 1, taxi via Foxtrot Echo, runway two one right",
            "taxi",
        ),
        ("Ground, Fleece 1, taxi to northwest EOR", "taxi"),
        ("Ground, Fleece 1, ready to taxi, runway two one right", "taxi"),
    ):
        result = voice_intent.evaluate(
            text,
            channel="ground",
            phase="departure",
            expected=expected,
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=True,
            readback_items=taxi_route_items,
            last_tx_template="taxi",
            require_address=True,
        )
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(
                f"  FAIL taxi-instruction readback must not re-call taxi: "
                f"{text!r} expected={expected!r} — {result.describe()}"
            )
            bad += 1
    still_ready = voice_intent.evaluate(
        "Nellis Ground, Fleece 1, ready to taxi",
        channel="ground",
        phase="departure",
        expected="taxi",
        callsign=CALLSIGN,
        runways=RUNWAYS,
        awaiting_readback=False,
        last_tx_template="",
        require_address=True,
    )
    if not still_ready.fired or still_ready.match.intent != "ready_taxi":
        print(
            f"  FAIL Ground ready-taxi (no readback window) must still fire: "
            f"{still_ready.describe()}"
        )
        bad += 1
    nlu_during = voice_nlu.allowed_intents(
        channel="ground",
        phase="departure",
        awaiting_readback=True,
    )
    nlu_idle = voice_nlu.allowed_intents(channel="ground", phase="departure")
    nlu_during_ids = {i.id for i in nlu_during}
    nlu_idle_ids = {i.id for i in nlu_idle}
    if "ready_taxi" in nlu_during_ids:
        print("  FAIL NLU must not offer ready_taxi during a taxi readback")
        bad += 1
    elif "ready_taxi" not in nlu_idle_ids:
        print("  FAIL NLU must still offer ready_taxi when Ground is idle")
        bad += 1
    else:
        print("taxi instruction readback — not a new taxi request")
    taxi_tips = voice_intent.suggestions(
        phase="departure",
        channel="ground",
        expected="taxi",
        awaiting_readback=True,
        readback_items=taxi_route_items,
    )
    if any(t[1] == "taxi clearance" for t in taxi_tips):
        print(f"  FAIL kneeboard still offers taxi request during taxi readback: {taxi_tips}")
        bad += 1

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
        "Ground, Fleece 1, at EOR",
        "Ground, Fleece 1, at E or",
        "Ground, Fleece 1, E or",
        "Nellis Ground, Fleece 1, e o r",
        "Ground, Fleece 1, end of runway",
        "Ground, Fleece 1, at ee or",
    ):
        result = eor_call(text)
        if not result.fired or result.match.intent != "at_eor":
            print(f"  FAIL at-EOR should fire: {text!r} — {result.describe()}")
            bad += 1
    for text in ("at EOR", "at E or", "end of runway"):
        result = eor_call(text)
        if result.fired:
            print(f"  FAIL at-EOR needs Ground: {text!r} — {result.describe()}")
            bad += 1
    print("at EOR — letters, Whisper splits, or end of runway (agency required)")

    monitor_items = [
        {
            "key": "monitor",
            "label": "Monitor",
            "value": "tower",
            "spoken": "monitor tower",
            "hinge": True,
        },
    ]
    monitor_last = (
        "Dagger one, Nellis Ground, monitor tower on three three three decimal three."
    )
    for text in (
        "Ground, Fleece 1, monitor tower",
        "Fleece 1, monitor tower",
        "monitor tower",
        "Ground, Fleece 1, at the EOR",
    ):
        result = voice_intent.evaluate(
            text,
            channel="ground",
            phase="departure",
            expected="monitor_tower",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            awaiting_readback=True,
            readback_items=monitor_items,
            last_tx_text=monitor_last,
            last_tx_channel="ground",
            last_tx_template="monitor_tower",
            require_address=True,
            tuned_channel="ground",
            cursor_channel="tower",
        )
        if result.fired and result.match and result.match.intent == "at_eor":
            print(f"  FAIL monitor-tower readback must not re-ask EOR: {text!r} — {result.describe()}")
            bad += 1
        elif text.endswith("at the EOR"):
            if result.fired and result.match and result.match.intent not in (
                "acknowledge_readback",
                None,
            ):
                print(f"  FAIL at EOR after monitor tower: {text!r} — {result.describe()}")
                bad += 1
        elif not result.fired or result.match.intent != "acknowledge_readback":
            if result.reason == "readback of last ATC":
                continue
            print(f"  FAIL monitor-tower readback should ack: {text!r} — {result.describe()}")
            bad += 1
    else:
        print("monitor tower readback — ack / echo, not a new EOR request")

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
    after_rb = land_ga(
        "Tower, Fleece 1, going around", awaiting=False, expected="exit_runway"
    )
    if not after_rb.fired or after_rb.match.intent != "going_around":
        print(
            f"  FAIL after land readback, going around should fire: "
            f"{after_rb.describe()}"
        )
        bad += 1
    bare_ga = land_ga("going around", awaiting=False, expected="exit_runway")
    if bare_ga.fired:
        print(
            f"  FAIL after land readback, bare going around needs Tower: "
            f"{bare_ga.describe()}"
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
    for text in (
        "line up and wait",
        "Tower, Fleece 1, line up and wait",
        "Tower, Fleece 1, ready for line up",
        "Tower, Fleece 1, request line up",
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
        if not result.fired or result.match.intent != "acknowledge_readback":
            print(
                f"  FAIL LUAW already issued — {text!r} is the readback, "
                f"not expect-LUAW: {result.describe()}"
            )
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

    if not atc_phrase.rolling_offer_awaiting_reply(
        {"pending_takeoff_offer": "rolling", "last_tx_template": "rolling_accept"}
    ):
        print("  FAIL Next/Prev should answer after Tower has asked rolling")
        bad += 1
    elif atc_phrase.rolling_offer_awaiting_reply(
        {"pending_takeoff_offer": "rolling", "last_tx_template": "taxi"}
    ):
        print("  FAIL first Next must still play the rolling question, not answer it")
        bad += 1
    elif atc_phrase.rolling_offer_awaiting_reply(
        {"pending_takeoff_offer": None, "last_tx_template": "rolling_accept"}
    ):
        print("  FAIL Next/Prev must not steal after the offer is closed")
        bad += 1
    else:
        print("rolling offer Next=accept / Prev=decline gate — ok")

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

    wx_exit = atc_phrase.Weather(210, 5, 29.92, "", visibility_sm=10)

    def _exit_text(rwy: str, **kwargs: object) -> str:
        return atc_phrase.build_template_text(
            nellis, "exit_runway", "Fleece 1", wx_exit, rwy, **kwargs  # type: ignore[arg-type]
        ).lower()

    exit_21l = _exit_text("21L")
    if (
        "exit right" not in exit_21l
        or "cross" not in exit_21l
        or "two one right" not in exit_21l
        or "ground" not in exit_21l
    ):
        print(f"  FAIL 21L west parking should exit right, cross 21R, Ground: {exit_21l}")
        bad += 1
    else:
        print(f"tower exit 21L (ramp) — {exit_21l}")

    exit_21l_g = _exit_text("21L", config={"parking_override": "G Revetments"})
    if (
        "exit left" not in exit_21l_g
        or "cross" in exit_21l_g
        or "ground" not in exit_21l_g
    ):
        print(f"  FAIL 21L G revetments should exit left, no cross: {exit_21l_g}")
        bad += 1
    else:
        print(f"tower exit 21L (G) — {exit_21l_g}")

    exit_21r = _exit_text("21R")
    if "exit right" not in exit_21r or "cross" in exit_21r or "ground" not in exit_21r:
        print(f"  FAIL 21R west parking should exit right, no cross: {exit_21r}")
        bad += 1
    else:
        print(f"tower exit 21R (ramp) — {exit_21r}")

    exit_landed_left = _exit_text("21R", state={"landed_runway": "21L"})
    if "cross" not in exit_landed_left or "two one right" not in exit_landed_left:
        print(
            f"  FAIL overhead plan 21R but landed 21L must still cross 21R: "
            f"{exit_landed_left}"
        )
        bad += 1
    else:
        print(f"tower exit landed 21L (plan 21R) — {exit_landed_left}")

    taxi_21l = atc_phrase.build_template_text(
        nellis, "taxi", "Razor 1", wx_exit, "21L"
    ).lower()
    if (
        "hold short" not in taxi_21l
        or "two one right" not in taxi_21l
        or "two one left" not in taxi_21l
    ):
        print(f"  FAIL Ground taxi to 21L must hold short of 21R: {taxi_21l}")
        bad += 1
    else:
        print(f"ground taxi 21L — {taxi_21l}")

    taxi_21r = atc_phrase.build_template_text(
        nellis, "taxi", "Razor 1", wx_exit, "21R"
    ).lower()
    if "hold short" in taxi_21r or "cross" in taxi_21r:
        print(f"  FAIL Ground taxi to 21R must not cross/hold 21R: {taxi_21r}")
        bad += 1
    else:
        print(f"ground taxi 21R — {taxi_21r}")

    to_21l = atc_phrase.build_template_text(
        nellis, "clear_takeoff", "Razor 1", wx_exit, "21L", state={}
    ).lower()
    if (
        "cross runway two one right" not in to_21l
        or "runway two one left" not in to_21l
        or "cleared for takeoff" not in to_21l
    ):
        print(f"  FAIL Tower takeoff 21L from NW EOR must cross 21R: {to_21l}")
        bad += 1
    else:
        print(f"tower takeoff 21L — {to_21l}")

    to_21r = atc_phrase.build_template_text(
        nellis, "clear_takeoff", "Razor 1", wx_exit, "21R", state={}
    ).lower()
    if "cross" in to_21r:
        print(f"  FAIL Tower takeoff 21R must not cross a parallel: {to_21r}")
        bad += 1
    else:
        print(f"tower takeoff 21R — {to_21r}")

    lineup_21l = atc_phrase.build_template_text(
        nellis, "lineup", "Razor 1", wx_exit, "21L", state={}
    ).lower()
    if "cross runway two one right" not in lineup_21l:
        print(f"  FAIL LUAW 21L from NW EOR must cross 21R: {lineup_21l}")
        bad += 1
    else:
        print(f"tower lineup 21L — {lineup_21l}")

    to_after_luaw = atc_phrase.build_template_text(
        nellis,
        "clear_takeoff",
        "Razor 1",
        wx_exit,
        "21L",
        state={"last_tx_template": "lineup"},
    ).lower()
    if "cross" in to_after_luaw:
        print(
            f"  FAIL takeoff after LUAW must not repeat the 21R cross: {to_after_luaw}"
        )
        bad += 1
    elif "cleared for takeoff" not in to_after_luaw:
        print(f"  FAIL takeoff after LUAW still needs the clearance: {to_after_luaw}")
        bad += 1
    else:
        print(f"tower takeoff after LUAW — {to_after_luaw}")

    luaw_then_to: dict = {}
    atc_phrase.build_template_text(
        nellis, "lineup", "Razor 1", wx_exit, "21L", state=luaw_then_to
    )
    to_stamped = atc_phrase.build_template_text(
        nellis, "clear_takeoff", "Razor 1", wx_exit, "21L", state=luaw_then_to
    ).lower()
    if str(luaw_then_to.get("departure_cross_issued") or "") != "21R":
        print(
            f"  FAIL LUAW should stamp departure_cross_issued 21R: {luaw_then_to}"
        )
        bad += 1
    elif "cross" in to_stamped:
        print(
            f"  FAIL takeoff after stamped LUAW cross must not repeat it: {to_stamped}"
        )
        bad += 1
    else:
        print(f"tower takeoff after stamped LUAW — {to_stamped}")

    rb_after_luaw = atc_phrase.build_readback_checklist(
        "clear_takeoff",
        nellis,
        None,
        wx_exit,
        "21L",
        state={"last_tx_template": "lineup"},
    )
    rb_cross = [i for i in rb_after_luaw if str(i.get("key") or "") == "cross"]
    if rb_cross:
        print(f"  FAIL takeoff readback after LUAW must not list Cross: {rb_after_luaw}")
        bad += 1
    else:
        print("tower takeoff readback after LUAW — no Cross item")

    taxi_in_missed_exit = atc_phrase.build_template_text(
        nellis,
        "taxi_in",
        "Razor 1",
        wx_exit,
        "21L",
        state={"last_tx_template": "clear_land"},
    ).lower()
    if "hold short" not in taxi_in_missed_exit or "two one right" not in taxi_in_missed_exit:
        print(
            f"  FAIL taxi-in after 21L without Tower exit must hold short 21R: "
            f"{taxi_in_missed_exit}"
        )
        bad += 1
    else:
        print(f"ground taxi-in 21L (no exit) — {taxi_in_missed_exit}")

    taxi_in_after_exit = atc_phrase.build_template_text(
        nellis,
        "taxi_in",
        "Razor 1",
        wx_exit,
        "21L",
        state={"last_tx_template": "exit_runway"},
    ).lower()
    if "hold short" in taxi_in_after_exit or "cross" in taxi_in_after_exit:
        print(
            f"  FAIL taxi-in after Tower already crossed 21R must not repeat: "
            f"{taxi_in_after_exit}"
        )
        bad += 1
    else:
        print(f"ground taxi-in 21L (after exit) — {taxi_in_after_exit}")

    parked_21l = atc_phrase.resolve_taxi_route(nellis, "21L")
    if str(parked_21l.get("departure_cross") or "") != "21R":
        print(f"  FAIL 21L from NW EOR must list departure_cross 21R: {parked_21l}")
        bad += 1
    if str(parked_21l.get("exit_cross") or "") != "21R":
        print(f"  FAIL 21L west parking exit must cross 21R: {parked_21l}")
        bad += 1

    park_g = atc_phrase.resolve_parking(nellis, squadron_name="64th AGRS")
    if park_g != "G Revetments":
        print(f"  FAIL 64th AGRS should park G Revetments, got {park_g!r}")
        bad += 1
    elif atc_phrase.parking_field_side(nellis, park_g) != "east":
        print(f"  FAIL G Revetments must be east of the field: {park_g}")
        bad += 1
    else:
        print("parking 64th AGRS -> G Revetments (east)")

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
        if trig_brk_si is not None and trig_brk_si.within_nm is not None:
            print(
                f"  FAIL straight-in tower check-in should have no NM gate: {trig_brk_si}"
            )
            bad += 1
            gates_ok = False
        st_ils = {
            "approach_plan": {"pattern": "instrument", "runway": "21L"},
            "active_recovery": "instrument",
        }
        trig_brk_ils = rp.resolve_step_trigger(
            {"template": "right_break"},
            state=st_ils,
        )
        if trig_brk_ils is not None and trig_brk_ils.within_nm is not None:
            print(
                f"  FAIL ILS tower check-in should have no NM gate: {trig_brk_ils}"
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
                f"tower distance gates — contact 12 NM, check-in voice, "
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

        st_miss = {
            "active_recovery": "instrument",
            "approach_plan": {
                "pattern": "instrument",
                "runway": "21L",
                "instrument_id": "ILS_Z_21L",
                "iaf": "ARCOE",
                "iaf_say": "Arcoe",
                "fix_lat": 36.737683,
                "fix_lon": -114.917067,
            },
        }
        ga_miss = atc_phrase.assign_go_around_plan(
            nellis, runway="21L", state=st_miss
        )
        st_miss["last_tx_at"] = 0.0
        st_miss["ownship_ll"] = [36.236, -115.034]
        ready_far, wait_far = atc_phrase.approach_clearance_auto_ready(
            airport=nellis, state=st_miss, gap_s=0
        )
        st_miss["ownship_ll"] = [36.737683, -114.917067]
        ready_near, wait_near = atc_phrase.approach_clearance_auto_ready(
            airport=nellis, state=st_miss, gap_s=0
        )
        st_first = {
            "approach_plan": dict(st_miss["approach_plan"]),
            "ownship_ll": [36.236, -115.034],
            "last_tx_at": 0.0,
        }
        ready_first, _ = atc_phrase.approach_clearance_auto_ready(
            airport=nellis, state=st_first, gap_s=0
        )
        if (
            str(ga_miss.get("kind") or "") != "instrument_missed"
            or not st_miss.get("approach_clearance_need_fix")
            or ready_far
            or "need" not in wait_far.lower()
            or not ready_near
            or "near fix" not in wait_near.lower()
            or not ready_first
        ):
            print(
                f"  FAIL missed approach IAF gate: ga={ga_miss} "
                f"far={ready_far}/{wait_far} near={ready_near}/{wait_near} "
                f"first={ready_first}"
            )
            bad += 1
        else:
            print(
                "instrument missed — wait until near IAF (ARCOE) "
                "before auto approach clearance"
            )

        import tanker as tanker_mod

        boom_ok = tanker_mod.tanker_is_f16_boom({"aircraft": "KC-135"})
        mprs_no = tanker_mod.tanker_is_f16_boom({"aircraft": "KC-135MPRS"})
        c130_no = tanker_mod.tanker_is_f16_boom({"aircraft": "KC-130"})
        tex = {
            "callsign": "TEXACO 1",
            "track": "ARLNS",
            "aircraft": "KC-135",
            "altitude": "23000",
            "freq_mhz": 322.3,
            "tcn": "39X",
            "boom": True,
            "bearing_deg": 45,
            "distance_nm": 32,
            "live_alt_ft": 23000,
            "aspect": "hot",
            "bullseye": {
                "name": "ELVIS",
                "bearing": 56,
                "range_nm": 32,
                "spoken": "ELVIS zero five six, thirty two",
            },
        }
        vec = tanker_mod.build_c2_tanker_vectors("blackjack", "Fleece 1", tex)
        join = tanker_mod.build_tanker_check_in("Fleece 1", tex)
        tacan = tanker_mod.build_tanker_tacan_reply("blackjack", "Fleece 1", tex)
        freq = tanker_mod.build_tanker_freq_reply("blackjack", "Fleece 1", tex)
        be = tanker_mod.build_tanker_bullseye_reply("blackjack", "Fleece 1", tex)
        ret = tanker_mod.build_tanker_return_checkin("blackjack", "Fleece 1")
        dcs_contact = tanker_mod.dcs_tanker_radio_hint("tanker_contact")
        dcs_abort = tanker_mod.dcs_tanker_radio_hint("tanker_dcs_abort")
        vec_l = vec.lower()
        track_ar = tanker_mod.speak_aar_track("AR231V").lower()
        catalog_pick = tanker_mod.choose_catalog_tanker(
            [
                {
                    "callsign": "ARCO 1",
                    "aircraft": "KC-130",
                    "track": "AR231V",
                },
                {
                    "callsign": "TEXACO 1",
                    "aircraft": "KC-135",
                    "track": "ARLNS",
                    "boom": True,
                },
            ],
            name="ARCO",
            boom_only=True,
        )
        no_boom = tanker_mod.choose_catalog_tanker(
            [{"callsign": "ARCO 1", "aircraft": "KC-130", "track": "AR231V"}],
            boom_only=True,
        )
        if (
            not boom_ok
            or mprs_no
            or c130_no
            or "texaco one" not in vec_l
            or "kc-135 boom" not in vec_l
            or "air refuel" in vec_l
            or "track " not in vec_l
            or "braa" in vec_l
            or "braw" not in vec_l
            or "hot" in vec_l
            or "zero four fife" not in vec_l
            or "flight level" not in vec_l
            or "tacan" in vec_l
            or "frequency change approved" not in vec_l
            or "tree two two" in vec_l
            or "elvis" in vec_l
            or "identified" in join.lower()
            or "cleared rejoin left" not in join.lower()
            or "niner x-ray" not in tacan.lower()
            or "tree two two" not in freq.lower()
            or "elvis" not in be.lower()
            or "continue" not in ret.lower()
            or "tanker radio" in vec_l
            or "tanker radio" in join.lower()
            or "ready pre-contact" not in dcs_contact.lower()
            or "abort" not in dcs_abort.lower()
            or "a r" not in track_ar
            or "victor" not in track_ar
            or "air refuel" in track_ar
            or not catalog_pick
            or str(catalog_pick.get("callsign") or "").upper() != "TEXACO 1"
            or no_boom is not None
        ):
            print(
                f"  FAIL tanker boom comms: boom={boom_ok} mprs={mprs_no} "
                f"c130={c130_no} vec={vec} join={join} tacan={tacan} "
                f"freq={freq} be={be} ret={ret} track={track_ar} "
                f"pick={catalog_pick} no_boom={no_boom} "
                f"dcs={dcs_contact} abort={dcs_abort}"
            )
            bad += 1
        else:
            print("tanker — F-16 KC-135 track/braw + check-in after AAR")

        joins = [
            tanker_mod.build_tanker_check_in("Fleece 1", tex).lower()
            for _ in range(40)
        ]
        if (
            any("identified" in j for j in joins)
            or not any("left observation" in j for j in joins)
            or not any(
                "cleared rejoin left" in j and "observation" not in j for j in joins
            )
        ):
            print(f"  FAIL tanker rejoin variants: {joins[:6]}")
            bad += 1
        else:
            print("tanker rejoin — left / left observation, never identified")

        own_lat, own_lon = 36.2362, -115.0343
        tgt_lat, tgt_lon = 36.2362, -114.0343
        tx, tz = atc_phrase.caoc_ll_to_xz(tgt_lat, tgt_lon)
        live = tanker_mod._enrich_live(
            {"callsign": "TEXACO 1"},
            {
                "xMeters": tx,
                "zMeters": tz,
                "headingDeg": 90,
                "altMeters": 7000,
            },
            {"bullseye_magnetic_declination_deg": 12},
            own_ll=(own_lat, own_lon),
        )
        true_brg = atc_phrase._true_bearing_deg(own_lat, own_lon, tgt_lat, tgt_lon)
        mag_brg = float(live.get("bearing_deg") or 0)
        if abs(mag_brg - ((true_brg - 12.0) % 360.0)) > 0.5:
            print(
                f"  FAIL tanker BRAA magnetic: true={true_brg:.1f} "
                f"live={mag_brg:.1f}"
            )
            bad += 1
        else:
            print(
                f"tanker BRAA is magnetic "
                f"({mag_brg:.0f} vs true {true_brg:.0f})"
            )

        if (
            tanker_mod.extract_tanker_name("request texaco 5") != "texaco 5"
            or tanker_mod.extract_tanker_name("request texaco five") != "texaco 5"
            or tanker_mod.tanker_name_is_specific("texaco")
            or not tanker_mod.tanker_name_is_specific("texaco 5")
            or not tanker_mod.wants_reassign_tanker(
                "blackjack fleece 1 request another tanker"
            )
            or tanker_mod.wants_reassign_tanker("request tanker")
        ):
            print("  FAIL tanker name / reassign parse")
            bad += 1
        else:
            print("tanker name parse — texaco 5 + another tanker")

        own_ll = (36.2362, -115.0343)
        far_lat, far_lon = 36.2362, -112.0343
        near_lat, near_lon = 36.2362, -114.8343
        far_xz = atc_phrase.caoc_ll_to_xz(far_lat, far_lon)
        near_xz = atc_phrase.caoc_ll_to_xz(near_lat, near_lon)
        catalog = [
            {
                "callsign": "TEXACO 1",
                "aircraft": "KC-135",
                "track": "ARLNS",
                "boom": True,
                "freq_mhz": 322.3,
            },
            {
                "callsign": "TEXACO 5",
                "aircraft": "KC-135",
                "track": "AR231V",
                "boom": True,
                "freq_mhz": 317.5,
            },
        ]
        live_units = [
            {
                "name": "TEXACO 1",
                "objectName": "KC-135",
                "xMeters": far_xz[0],
                "zMeters": far_xz[1],
                "headingDeg": 90,
                "altMeters": 7000,
            },
            {
                "name": "TEXACO 5",
                "objectName": "KC-135",
                "xMeters": near_xz[0],
                "zMeters": near_xz[1],
                "headingDeg": 90,
                "altMeters": 7000,
            },
        ]
        cfg = {"bullseye_magnetic_declination_deg": 12}
        nearest = tanker_mod.choose_catalog_tanker(
            catalog,
            boom_only=True,
            units=live_units,
            own_ll=own_ll,
            config=cfg,
        )
        skipped = tanker_mod.choose_catalog_tanker(
            catalog,
            boom_only=True,
            units=live_units,
            own_ll=own_ll,
            config=cfg,
            exclude="TEXACO 5",
        )
        named_five = tanker_mod.choose_catalog_tanker(
            catalog,
            name="texaco 5",
            boom_only=True,
            units=live_units,
            own_ll=own_ll,
            config=cfg,
        )
        old_fetch = tanker_mod.fetch_opus_tankers
        old_units = tanker_mod._caoc_tanker_units
        tanker_mod.fetch_opus_tankers = lambda *a, **k: list(catalog)
        tanker_mod._caoc_tanker_units = lambda *a, **k: list(live_units)
        try:
            remembered = {"tanker_callsign": "TEXACO 1", "tanker_id": "tex1"}
            sticky = tanker_mod.pick_tanker(
                cfg,
                state=remembered,
                own_ll=own_ll,
                boom_only=True,
            )
            fresh = tanker_mod.pick_tanker(
                cfg,
                state=remembered,
                own_ll=own_ll,
                boom_only=True,
                prefer_remembered=False,
            )
            asked = tanker_mod.pick_tanker(
                cfg,
                state=remembered,
                name="texaco 5",
                own_ll=own_ll,
                boom_only=True,
            )
            info = tanker_mod.pick_tanker(
                cfg,
                state=remembered,
                own_ll=own_ll,
                boom_only=True,
                prefer_remembered=True,
            )
        finally:
            tanker_mod.fetch_opus_tankers = old_fetch
            tanker_mod._caoc_tanker_units = old_units
        if (
            str((nearest or {}).get("callsign") or "").upper() != "TEXACO 5"
            or str((skipped or {}).get("callsign") or "").upper() != "TEXACO 1"
            or str((named_five or {}).get("callsign") or "").upper() != "TEXACO 5"
            or str((sticky or {}).get("callsign") or "").upper() != "TEXACO 1"
            or str((fresh or {}).get("callsign") or "").upper() != "TEXACO 5"
            or str((asked or {}).get("callsign") or "").upper() != "TEXACO 5"
            or str((info or {}).get("callsign") or "").upper() != "TEXACO 1"
        ):
            print(
                f"  FAIL tanker re-pick: nearest={nearest} skipped={skipped} "
                f"named={named_five} sticky={sticky} fresh={fresh} "
                f"asked={asked} info={info}"
            )
            bad += 1
        else:
            print("tanker re-pick — nearest / named Texaco 5, TACAN stays on 1")

        import tanker_chat as tanker_chat_mod

        st = {}
        opener = tanker_chat_mod.start_chat(
            st, "Fleece 1", {"callsign": "TEXACO 1"}, thread_id="dunkin_starbucks"
        )
        dunk = tanker_chat_mod.answer_chat(
            st, "Fleece 1", {"callsign": "TEXACO 1"}, choice_id="dunkin"
        )
        st2 = {}
        tanker_chat_mod.start_chat(
            st2, "Fleece 1", {"callsign": "TEXACO 1"}, thread_id="dunkin_starbucks"
        )
        choices = tanker_chat_mod.current_choices(st2)
        bare = voice_intent.evaluate(
            "Dunkin",
            channel="tanker",
            phase="flight",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            tanker_chat_choices=choices,
        )
        silent = voice_intent.evaluate(
            "Dunkin",
            channel="tanker",
            phase="flight",
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        if (
            "identified" in (opener or "").lower()
            or "dunkin or starbucks" not in (opener or "").lower()
            or not dunk
            or "keurig" not in dunk.lower()
            or dunk.lower().startswith("dunkin, copy")
            or dunk.lower().startswith("copy dunkin")
            or not tanker_chat_mod.is_open(st)
            or not bare.fired
            or bare.match.intent != "tanker_chat_reply"
            or str((bare.match.slots or {}).get("choice") or "") != "dunkin"
            or silent.fired
        ):
            print(
                f"  FAIL tanker chat: opener={opener!r} dunk={dunk!r} "
                f"bare={bare.describe()} silent={silent.describe()} open={st}"
            )
            bad += 1
        else:
            print("tanker chat — Dunkin/Starbucks, reply without parroted copy")

        hello_am = tanker_chat_mod.greeting_opener(hour=8)
        hello_pm = tanker_chat_mod.greeting_opener(hour=14)
        hello_eve = tanker_chat_mod.greeting_opener(hour=20)
        if (
            tanker_chat_mod._daypart(8) != "morning"
            or tanker_chat_mod._daypart(0) != "morning"
            or tanker_chat_mod._daypart(14) != "afternoon"
            or tanker_chat_mod._daypart(20) != "evening"
            or "morning" not in hello_am.lower()
            or "sir" not in hello_am.lower()
            or "?" in hello_am
            or "afternoon" not in hello_pm.lower()
            or "sir" not in hello_pm.lower()
            or "evening" not in hello_eve.lower()
            or "sir" not in hello_eve.lower()
        ):
            print(
                f"  FAIL tanker hello lines: am={hello_am!r} pm={hello_pm!r} "
                f"eve={hello_eve!r}"
            )
            bad += 1
        else:
            print(f"tanker chat hello lines — {hello_am}")

        st_hi: dict = {}
        hi = tanker_chat_mod.start_chat(
            st_hi,
            "Fleece 1",
            {"callsign": "TEXACO 1"},
            config={"tanker_chat_llm": "off"},
        )
        hi_l = (hi or "").lower()
        hi_waiting = (
            tanker_chat_mod.is_awaiting_react(st_hi)
            and not tanker_chat_mod.current_choices(st_hi)
        )
        hi_ack = tanker_chat_mod.answer_chat(
            st_hi,
            "Fleece 1",
            {"callsign": "TEXACO 1"},
            transcript="hello",
            config={"tanker_chat_llm": "off"},
        )
        nxt = tanker_chat_mod.start_chat(
            st_hi,
            "Fleece 1",
            {"callsign": "TEXACO 1"},
            thread_id="dunkin_starbucks",
            config={"tanker_chat_llm": "off"},
        )
        if (
            "sir" not in hi_l
            or "?" in (hi or "")
            or not any(p in hi_l for p in ("morning", "afternoon", "evening"))
            or "dunkin" in hi_l
            or not hi_waiting
            or not st_hi.get("tanker_chat_greeted")
            or not hi_ack
            or tanker_chat_mod.is_awaiting_react(st_hi)
            or "dunkin or starbucks" not in (nxt or "").lower()
            or tanker_chat_mod._should_greet(st_hi, None)
        ):
            print(
                f"  FAIL tanker first hello: hi={hi!r} ack={hi_ack!r} nxt={nxt!r} "
                f"waiting={hi_waiting} state={st_hi.get('tanker_chat')}"
            )
            bad += 1
        else:
            print("tanker chat hello — first contact only, then questions")

        soft = tanker_chat_mod.naturalize_reply(
            "Navy, copy. Correct. They show up like a boat.", force=True
        )
        if soft.lower().startswith("navy") or "copy" in soft.lower().split(",")[0]:
            print(f"  FAIL naturalize_reply still echoes: {soft!r}")
            bad += 1
        else:
            print(f"tanker chat naturalize — {soft}")

        forty = tanker_chat_mod._fill("I'm good for someone over 40.")
        prepared = atc_phrase.prepare_radio_tts_text(forty)
        if (
            "four zero" in forty.casefold()
            or "forty" not in forty.casefold()
            or "four zero" in prepared.casefold()
        ):
            print(
                f"  FAIL boom chat should say forty not four zero: "
                f"{forty!r} → {prepared!r}"
            )
            bad += 1
        else:
            print(f"tanker chat casual numbers — {forty}")

        import tanker_chat_library as tanker_chat_lib

        lib_n = tanker_chat_lib.library_size()
        ids = [str(t.get("id") or "") for t in tanker_chat_lib.THREADS]
        if lib_n < 50 or len(set(ids)) != lib_n or "dunkin_starbucks" not in ids:
            print(f"  FAIL tanker chat library size/ids: n={lib_n} unique={len(set(ids))}")
            bad += 1
        elif "desert_glow" not in ids or "weirdest_cockpit" not in ids:
            print(f"  FAIL tanker chat library missing riff/open bits: {ids[-10:]}")
            bad += 1
        else:
            print(f"tanker chat library — {lib_n} unique bits (A/B + riff + open)")

        st_riff = {}
        riff_open = tanker_chat_mod.start_chat(
            st_riff, "Fleece 1", {"callsign": "TEXACO 1"}, thread_id="desert_glow"
        )
        riff_waiting = (
            tanker_chat_mod.is_awaiting_react(st_riff)
            and not tanker_chat_mod.current_choices(st_riff)
        )
        riff_ans = tanker_chat_mod.answer_chat(
            st_riff,
            "Fleece 1",
            {"callsign": "TEXACO 1"},
            transcript="yeah that is pretty",
        )
        free = voice_intent.evaluate(
            "ha yeah pretty out here",
            channel="tanker",
            phase="flight",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            tanker_chat_session=True,
            tanker_chat_awaiting_react=True,
        )
        if (
            not riff_open
            or "fleece" in (riff_open or "").lower()
            or "texaco" in (riff_open or "").lower()
            or not riff_waiting
            or not riff_ans
            or not tanker_chat_mod.is_session_active(st_riff)
            or not tanker_chat_mod.is_awaiting_react(st_riff)
            or int((st_riff.get("tanker_chat") or {}).get("turns") or 0) < 1
            or not free.fired
            or free.match.intent != "tanker_chat_reply"
        ):
            print(
                f"  FAIL tanker riff chat: open={riff_open!r} ans={riff_ans!r} "
                f"waiting={riff_waiting} free={free.describe()} "
                f"state={st_riff.get('tanker_chat')}"
            )
            bad += 1
        else:
            print("tanker chat riff — freeform react keeps the bit open")

        session_only = voice_intent.evaluate(
            "Where do you see that?",
            channel="tanker",
            phase="flight",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            tanker_chat_session=True,
        )
        no_session = voice_intent.evaluate(
            "Where do you see that?",
            channel="tanker",
            phase="flight",
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        leaked_ops = voice_intent.evaluate(
            "Ops, Fleece 1, request current WORDS",
            channel="ops",
            phase="departure",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            tanker_chat_session=True,
        )
        leaked_c2 = voice_intent.evaluate(
            "Where do you see that?",
            channel="blackjack",
            phase="flight",
            callsign=CALLSIGN,
            runways=RUNWAYS,
            tanker_chat_session=True,
        )
        if (
            not session_only.fired
            or session_only.match.intent != "tanker_chat_reply"
            or no_session.fired
            or not leaked_ops.fired
            or leaked_ops.match.intent != "ops_request_words"
            or leaked_c2.fired
        ):
            print(
                f"  FAIL tanker chat session-only reply: "
                f"session={session_only.describe()} none={no_session.describe()} "
                f"ops={leaked_ops.describe()} c2={leaked_c2.describe()}"
            )
            bad += 1
        else:
            print("tanker chat session — freeform on tanker freq only; not OPS / C2")

        ram = tanker_chat_mod._fill("Ram two, altitude and airspeed.")
        if ram.lower().startswith("ram") or not tanker_chat_mod.looks_like_official_tanker(
            "Ram two, altitude and airspeed."
        ):
            print(f"  FAIL tanker chat official-radio strip: {ram!r}")
            bad += 1
        else:
            print("tanker chat — strips callsign / flags official tanker radio")

        if (
            tanker_chat_mod.invites_reply("Sun is low on the ridge tonight")
            or not tanker_chat_mod.invites_reply(
                "Sun is low on the ridge — you ever get that view from the Viper?"
            )
            or not tanker_chat_mod.invites_reply("Dunkin or Starbucks?")
        ):
            print("  FAIL tanker chat invites_reply gate")
            bad += 1
        else:
            print("tanker chat — statements rejected, questions/hooks invited")

        if not tanker_chat_mod.looks_like_airline(
            "Folks we'll be cruising at thirty thousand with beverage service."
        ) or tanker_chat_mod.looks_like_airline(
            "You ever get bored on the boom during a range day?"
        ):
            print("  FAIL tanker chat airline vs military gate")
            bad += 1
        else:
            print("tanker chat — rejects airline talk, keeps military small talk")

        male_cfg = {
            "tts_provider": "google",
            "tts_voice": "en-US-Neural2-D",
            "tts_voices": {"tanker": "en-US-Neural2-D"},
        }
        tanker_v, tanker_g = atc_phrase.voice_for_channel(male_cfg, "tanker")
        step_v, step_g = atc_phrase.voice_for_step(
            male_cfg, "tanker", {"voice": "en-US-Neural2-I", "channel": "tanker"}
        )
        ground_v, ground_g = atc_phrase.voice_for_channel(male_cfg, "ground")
        if (
            tanker_g != "female"
            or atc_phrase.voice_gender(tanker_v) != "female"
            or step_g != "female"
            or ground_g != "male"
        ):
            print(
                f"  FAIL tanker voice must be female: tanker={tanker_v!r}/{tanker_g} "
                f"step={step_v!r}/{step_g} ground={ground_v!r}/{ground_g}"
            )
            bad += 1
        else:
            print(f"tanker voice — always female ({tanker_v})")

        st_cap = {
            "tanker_chat": {
                "session": True,
                "awaiting": "react",
                "opener": "Is it weird that the desert can look so peaceful from up here?",
            },
            "tanker_chat_last_spoke": (
                "Is it weird that the desert can look so peaceful from up here?"
            ),
        }
        cap = tanker_chat_mod.fly_boom_caption(st_cap, "say anything")
        if "TEXACO:" not in cap or "desert" not in cap.lower():
            print(f"  FAIL tanker chat boom caption: {cap!r}")
            bad += 1
        else:
            print("tanker chat Fly caption keeps Texaco's line")

        llm_ok = tanker_chat_mod.normalize_llm_thread(
            {
                "opener": "Coffee or tea while you hang out?",
                "choices": [
                    {"say": "Coffee", "reply": "Coffee, copy. Boom approved."},
                    {"say": "Tea", "reply": "Tea, copy. Fancy."},
                ],
            }
        )
        llm_riff = tanker_chat_mod.normalize_llm_thread(
            {
                "kind": "riff",
                "opener": "Desert looks like a glowing parking lot from up here tonight.",
            }
        )
        llm_demote = tanker_chat_mod.normalize_llm_thread(
            {
                "kind": "ab",
                "opener": "Anybody else cold on the boom today up here?",
                "choices": [{"say": "Yes", "reply": "Yep."}],
            }
        )
        llm_salvage = tanker_chat_mod.normalize_llm_thread(
            "Man, the desert looks like a glowing parking lot from up here."
        )
        llm_bad = tanker_chat_mod.normalize_llm_thread(
            {"opener": "hi", "choices": [{"say": "Rejoin", "reply": "nope"}]}
        )
        llm_off = tanker_chat_mod.resolve_llm_provider(
            {"tanker_chat_llm": "gemini", "tanker_chat_llm_key": ""}
        )
        llm_auto = tanker_chat_mod.resolve_llm_provider(
            {"tanker_chat_llm": "auto", "tanker_chat_llm_key": "sk-test"}
        )
        st_llm = {}
        pinned = tanker_chat_mod.start_chat(
            st_llm,
            "Fleece 1",
            {"callsign": "TEXACO 1"},
            thread_id="dunkin_starbucks",
            config={"tanker_chat_llm": "gemini", "tanker_chat_llm_key": "AIza-fake"},
        )
        if (
            not llm_ok
            or "{cs}" in str(llm_ok.get("opener") or "").lower()
            or "fleece" in str(llm_ok.get("opener") or "").lower()
            or len(llm_ok.get("choices") or []) != 2
            or "coffee" not in (llm_ok["choices"][0].get("hits") or ())
            or not llm_riff
            or llm_riff.get("kind") != "riff"
            or not llm_demote
            or llm_demote.get("kind") != "riff"
            or not llm_salvage
            or llm_salvage.get("kind") != "riff"
            or llm_bad is not None
            or llm_off is not None
            or llm_auto != ("openai", "sk-test")
            or "dunkin or starbucks" not in (pinned or "").lower()
            or "fleece" in (pinned or "").lower()
            or "texaco" in (pinned or "").lower()
        ):
            print(
                f"  FAIL tanker chat LLM normalize: ok={llm_ok} riff={llm_riff} "
                f"demote={llm_demote} salvage={llm_salvage} "
                f"bad={llm_bad} off={llm_off} auto={llm_auto} pinned={pinned!r}"
            )
            bad += 1
        else:
            print("tanker chat LLM — normalize A/B + riff/demote/salvage, pin dunkin")

        multi = (
            '{"id":1234,"kind":"riff","opener":"Still getting used to these new headphones."} '
            '{"id":1235,"kind":"ab","opener":"Favorite snack?","choices":[{"say":"Trail mix","reply":"ok"}]}'
        )
        multi_node = tanker_chat_mod.normalize_llm_thread(multi)
        raw_json_salvage = tanker_chat_mod._salvage_riff_text(
            '{"id":1,"kind":"riff","opener":"Orbit boredom hits different at sunset."}'
        )
        if (
            not multi_node
            or multi_node.get("kind") != "riff"
            or "headphones" not in str(multi_node.get("opener") or "").lower()
            or str(multi_node.get("opener") or "").lstrip().startswith("{")
            or not raw_json_salvage
            or "sunset" not in str(raw_json_salvage.get("opener") or "").lower()
            or str(raw_json_salvage.get("opener") or "").lstrip().startswith("{")
        ):
            print(
                f"  FAIL tanker LLM multi-object parse: multi={multi_node} "
                f"salvage={raw_json_salvage}"
            )
            bad += 1
        else:
            print("tanker chat LLM — multi-object JSON takes first opener only")

        # With LLM on, freeform speech can riff — not locked to A/B buttons.
        st_live = {}
        tanker_chat_mod.start_chat(
            st_live, "Fleece 1", {"callsign": "TEXACO 1"}, thread_id="dunkin_starbucks"
        )
        live_cfg = {"tanker_chat_llm": "ollama"}
        real_try = tanker_chat_mod.try_llm_react
        real_models = tanker_chat_mod.list_ollama_models

        def _fake_react(config, *, opener, pilot, state=None):
            assert "coffee" in (opener or "").lower() or "dunkin" in (opener or "").lower()
            assert "hate both" in (pilot or "").lower()
            return "Ha — pick neither, then. Boom crew respects chaos."

        tanker_chat_mod.try_llm_react = _fake_react  # type: ignore[assignment]
        tanker_chat_mod.list_ollama_models = lambda config=None: ["llama3.2"]  # type: ignore[assignment]
        try:
            live_reply = tanker_chat_mod.answer_chat(
                st_live,
                "Fleece 1",
                {"callsign": "TEXACO 1"},
                transcript="I hate both of them honestly",
                config=live_cfg,
            )
            free_live = voice_intent.evaluate(
                "I hate both of them honestly",
                channel="tanker",
                phase="flight",
                callsign=CALLSIGN,
                runways=RUNWAYS,
                tanker_chat_choices=tanker_chat_mod.current_choices(
                    {"tanker_chat": {"choices": [{"id": "dunkin", "say": "Dunkin", "hits": ("dunkin",)}]}}
                ),
                tanker_chat_freeform=True,
            )
        finally:
            tanker_chat_mod.try_llm_react = real_try  # type: ignore[assignment]
            tanker_chat_mod.list_ollama_models = real_models  # type: ignore[assignment]
        if (
            not live_reply
            or "chaos" not in live_reply.lower()
            or live_reply.lower().startswith("dunkin")
            or not free_live.fired
            or free_live.match.intent != "tanker_chat_reply"
            or not tanker_chat_mod.is_awaiting_react(st_live)
        ):
            print(
                f"  FAIL tanker LLM freeform riff: reply={live_reply!r} "
                f"free={free_live.describe()} state={st_live.get('tanker_chat')}"
            )
            bad += 1
        else:
            print("tanker chat LLM freeform — riff beyond A/B choices")

        # Long / question speech must not get trapped in a one-word A/B canned reply.
        st_q = {}
        tanker_chat_mod.start_chat(
            st_q, "Fleece 1", {"callsign": "TEXACO 1"}, thread_id="dunkin_starbucks"
        )
        saw = {"pilot": ""}

        def _fake_answer(config, *, opener, pilot, state=None):
            saw["pilot"] = str(pilot or "")
            return "Mostly sandwiches — catering surprises us on Fridays."

        tanker_chat_mod.try_llm_react = _fake_answer  # type: ignore[assignment]
        tanker_chat_mod.list_ollama_models = lambda config=None: ["llama3.2"]  # type: ignore[assignment]
        try:
            q_reply = tanker_chat_mod.answer_chat(
                st_q,
                "Fleece 1",
                {"callsign": "TEXACO 1"},
                transcript="Dunkin is fine, but what do you guys actually eat for lunch?",
                config={"tanker_chat_llm": "ollama"},
            )
        finally:
            tanker_chat_mod.try_llm_react = real_try  # type: ignore[assignment]
            tanker_chat_mod.list_ollama_models = real_models  # type: ignore[assignment]
        if (
            not tanker_chat_mod.looks_like_question_or_chat(
                "Dunkin is fine, but what do you guys actually eat for lunch?"
            )
            or not q_reply
            or "sandwich" not in q_reply.lower()
            or "keurig" in q_reply.lower()
            or "lunch" not in saw["pilot"].lower()
            or (st_q.get("tanker_chat") or {}).get("opener", "").lower().startswith("dunkin")
        ):
            # After a live answer, opener should track her last reply, not the A/B poll.
            print(
                f"  FAIL tanker question should bypass A/B: reply={q_reply!r} "
                f"saw={saw} state={st_q.get('tanker_chat')}"
            )
            bad += 1
        else:
            print("tanker chat questions — bypass A/B canned reply, answer live")

        # No models → clear note, library fallback (do not pretend Ollama fired).
        st_mem = {}
        tanker_chat_mod.append_history(
            st_mem, "boom", "Dunkin or Starbucks — boom poll, go."
        )
        tanker_chat_mod.append_history(st_mem, "pilot", "Starbucks, easy.")
        tanker_chat_mod.append_history(
            st_mem, "boom", "Starbucks it is — I'll pretend the Keurig agrees."
        )
        hist_blob = tanker_chat_mod.format_history(st_mem)
        cool = tanker_chat_mod.coffee_on_cooldown(st_mem)
        prompt_ban = tanker_chat_mod._llm_prompt(
            [], history=hist_blob, ban_coffee=True
        )
        coffee_picks = 0
        for _ in range(24):
            node = tanker_chat_mod._pick_library_for_state(st_mem, [], None)
            opener_n = str(node.get("opener") or "")
            tid_n = str(node.get("id") or "")
            if tanker_chat_mod._mentions_coffee(opener_n) or tanker_chat_mod._mentions_coffee(
                tid_n
            ):
                coffee_picks += 1
        st_hist_chat = {}
        tanker_chat_mod.start_chat(
            st_hist_chat,
            "Fleece 1",
            {"callsign": "TEXACO 1"},
            thread_id="dunkin_starbucks",
            config={"tanker_chat_llm": "off"},
        )
        tanker_chat_mod.answer_chat(
            st_hist_chat,
            "Fleece 1",
            {"callsign": "TEXACO 1"},
            choice_id="starbucks",
            config={"tanker_chat_llm": "off"},
        )
        hist_after = tanker_chat_mod.format_history(st_hist_chat)
        if (
            "Boom:" not in hist_blob
            or "Pilot:" not in hist_blob
            or "Starbucks" not in hist_blob
            or not cool
            or "exhausted" not in prompt_ban.casefold()
            or "enlisted" not in prompt_ban.casefold()
            or "officer" not in prompt_ban.casefold()
            or "invite a reply" not in prompt_ban.casefold()
            or "not an airliner" not in prompt_ban.casefold()
            or "military" not in prompt_ban.casefold()
            or "gas-up chatter" not in prompt_ban.casefold()
            or "kc-135 boom operator" not in prompt_ban.casefold()
            or "Recent chat:" not in prompt_ban
            or coffee_picks > 0
            or "Dunkin" not in hist_after
            or "Pilot:" not in hist_after
        ):
            print(
                f"  FAIL tanker chat memory/coffee: hist={hist_blob!r} cool={cool} "
                f"coffee_picks={coffee_picks} after={hist_after!r}"
            )
            bad += 1
        else:
            print("tanker chat memory — history + coffee cooldown skips coffee bits")

        # No models / daemon down → library chat, no "pull llama" stall on Fly.
        st_empty = {tanker_chat_mod._GREETED_KEY: True}
        tanker_chat_mod._set_llm_error("", st_empty)
        tanker_chat_mod.list_ollama_models = lambda config=None: []  # type: ignore[assignment]
        tanker_chat_mod._OLLAMA_MODELS_CACHE["t"] = 0.0
        tanker_chat_mod._OLLAMA_MODELS_CACHE["names"] = []
        try:
            opener = tanker_chat_mod.start_chat(
                st_empty,
                "Fleece 1",
                {"callsign": "TEXACO 1"},
                config={"tanker_chat_llm": "ollama"},
            )
            note = tanker_chat_mod.llm_note(st_empty)
            reply = tanker_chat_mod.answer_chat(
                st_empty,
                "Fleece 1",
                {"callsign": "TEXACO 1"},
                transcript="What do you guys actually eat for lunch?",
                config={"tanker_chat_llm": "ollama"},
            )
        finally:
            tanker_chat_mod.list_ollama_models = real_models  # type: ignore[assignment]
        blob = f"{opener} {reply} {note}".lower()
        if (
            not opener
            or not reply
            or "boom's thinking" in blob
            or "hang on, boom" in blob
            or "no ollama models installed" in blob
            or "pull llama" in blob
            or "generat" in note.lower()
        ):
            print(
                f"  FAIL ollama empty should use library: opener={opener!r} "
                f"reply={reply!r} note={note!r}"
            )
            bad += 1
        else:
            print(f"tanker chat Ollama empty — library fallback ({note or 'quiet'})")

        captured: dict = {}

        def _fake_http(url, payload, headers, timeout=6.5):
            captured["url"] = str(url)
            captured["payload"] = dict(payload)
            captured["timeout"] = timeout
            return {
                "message": {
                    "content": '{"id":"llm_x","kind":"open","opener":"Sun is low on the ridge tonight — you ever get that view from the Viper?"}'
                }
            }

        real_http = tanker_chat_mod._http_json
        tanker_chat_mod._http_json = _fake_http  # type: ignore[assignment]
        tanker_chat_mod.list_ollama_models = lambda config=None: ["llama3.2:latest"]  # type: ignore[assignment]
        tanker_chat_mod._OLLAMA_MODELS_CACHE["t"] = 0.0
        tanker_chat_mod._OLLAMA_MODELS_CACHE["names"] = ["llama3.2:latest"]
        st_nat: dict = {}
        try:
            node_nat = tanker_chat_mod.try_llm_thread(
                {"tanker_chat_llm": "ollama"}, [], state=st_nat
            )
        finally:
            tanker_chat_mod._http_json = real_http  # type: ignore[assignment]
            tanker_chat_mod.list_ollama_models = real_models  # type: ignore[assignment]
        opts = (captured.get("payload") or {}).get("options") or {}
        if (
            "/api/chat" not in str(captured.get("url") or "")
            or int(opts.get("num_predict") or 0) < 40
            or int(opts.get("num_gpu") if opts.get("num_gpu") is not None else -1) != 0
            or not node_nat
            or str(node_nat.get("source") or "") != "ollama"
            or "sun is low" not in str(node_nat.get("opener") or "").lower()
        ):
            print(
                f"  FAIL ollama native chat: url={captured.get('url')!r} "
                f"opts={opts} node={node_nat}"
            )
            bad += 1
        else:
            print("tanker chat Ollama native — /api/chat CPU-only (num_gpu=0)")

        if (
            tanker_chat_mod.ollama_num_gpu({}) != 0
            or tanker_chat_mod.ollama_num_gpu({"tanker_chat_ollama_num_gpu": 2}) != 2
            or tanker_chat_mod.ollama_num_gpu({"tanker_chat_ollama_num_gpu": "nope"})
            != 0
        ):
            print("  FAIL ollama_num_gpu default/override")
            bad += 1
        else:
            print("tanker chat Ollama — default CPU pin, override honored")

        unload_hits: list[tuple[str, dict]] = []

        def _fake_ps(url, timeout=0.8):
            if "/api/ps" not in str(url):
                raise AssertionError(f"unexpected GET {url}")
            return {
                "models": [
                    {"name": "llama3.2:latest", "size_vram": 2_000_000_000},
                    {"name": "cpu-resident", "size_vram": 0},
                ]
            }

        def _unload_http(url, payload, headers, timeout=6.5):
            unload_hits.append((str(url), dict(payload)))
            return {}

        real_get = tanker_chat_mod._http_get_json
        real_http2 = tanker_chat_mod._http_json
        tanker_chat_mod._http_get_json = _fake_ps  # type: ignore[assignment]
        tanker_chat_mod._http_json = _unload_http  # type: ignore[assignment]
        tanker_chat_mod._OLLAMA_GPU_RELEASED = False
        try:
            unloaded = tanker_chat_mod.release_ollama_gpu({"tanker_chat_llm": "ollama"})
        finally:
            tanker_chat_mod._http_get_json = real_get  # type: ignore[assignment]
            tanker_chat_mod._http_json = real_http2  # type: ignore[assignment]
            tanker_chat_mod._OLLAMA_GPU_RELEASED = False
        if (
            unloaded != 1
            or len(unload_hits) != 1
            or "/api/generate" not in unload_hits[0][0]
            or unload_hits[0][1].get("keep_alive") != 0
            or unload_hits[0][1].get("model") != "llama3.2:latest"
        ):
            print(f"  FAIL release_ollama_gpu: n={unloaded} hits={unload_hits}")
            bad += 1
        else:
            print("tanker chat Ollama — unloads VRAM-resident models")

        scripted = (
            "Sir, I think it's the angle of the Viper's nose cone, plus the "
            "seat's a lot wider than my pad. Pilot: really?"
        )
        stripped = tanker_chat_mod.strip_scripted_dialogue(scripted)
        cleaned_script = tanker_chat_mod._clean_llm_plain(scripted)
        salvage_script = tanker_chat_mod._salvage_riff_text(
            "Boom: Boom pad or Viper seat, who got robbed?\nPilot: the pad"
        )
        llm_hist = tanker_chat_mod.format_history_for_llm(
            {
                "tanker_chat_history": [
                    {"role": "boom", "text": "Boom pad or Viper seat?"},
                    {"role": "pilot", "text": "You got a better view."},
                ]
            }
        )
        sys_plain = tanker_chat_mod._boom_llm_system(json_out=False)
        react_p = tanker_chat_mod._llm_react_prompt(
            "Boom pad or Viper seat?",
            "You got a better view of it than I do.",
        )
        keep_q = tanker_chat_mod.strip_scripted_dialogue(
            "You ever get bored on the boom during a range day?"
        )
        stop_list = list(
            ((captured.get("payload") or {}).get("options") or {}).get("stop") or []
        )
        if (
            "pilot:" in stripped.casefold()
            or "really" in stripped.casefold()
            or "wider than my pad" not in stripped.casefold()
            or not cleaned_script
            or "pilot:" in cleaned_script.casefold()
            or not salvage_script
            or "pilot:" in str(salvage_script.get("opener") or "").casefold()
            or "who got robbed" not in str(salvage_script.get("opener") or "").casefold()
            or "YOU (boom operator)" not in llm_hist
            or "F-16 (them" not in llm_hist
            or "Pilot:" in llm_hist
            or "kc-135 boom operator" not in sys_plain.casefold()
            or "pilot: really" not in sys_plain.casefold()
            or "do not write a pilot:" not in react_p.casefold()
            or "end with a question mark" not in react_p.casefold()
            or "you ever get bored" not in keep_q.casefold()
            or "Pilot:" not in stop_list
        ):
            print(
                f"  FAIL boom operator dialogue lock: stripped={stripped!r} "
                f"clean={cleaned_script!r} salvage={salvage_script} hist={llm_hist!r}"
            )
            bad += 1
        else:
            print("tanker chat — boom speaks, never writes the pilot's line")

        # Echo of Texaco's own TX must not trigger a freeform LLM reply.
        st_echo = {}
        tanker_chat_mod.start_chat(
            st_echo, "Fleece 1", {"callsign": "TEXACO 1"}, thread_id="desert_glow"
        )
        opener_echo = str((st_echo.get("tanker_chat") or {}).get("opener") or "")
        tanker_chat_mod.arm_tx_guard(st_echo, opener_echo)
        echoed = tanker_chat_mod.answer_chat(
            st_echo,
            "Fleece 1",
            {"callsign": "TEXACO 1"},
            transcript=opener_echo,
            config={"tanker_chat_llm": "off"},
        )
        if echoed is not None or not tanker_chat_mod.looks_like_own_echo(
            opener_echo, st_echo
        ):
            print(f"  FAIL boom echo should be ignored: echoed={echoed!r}")
            bad += 1
        else:
            print("tanker chat echo guard — ignores Texaco talking to herself")

        st_closed = {tanker_chat_mod._GREETED_KEY: True}
        tanker_chat_mod.start_chat(
            st_closed, "Fleece 1", {"callsign": "TEXACO 1"}, thread_id="pizza"
        )
        tanker_chat_mod.schedule_next_question(st_closed, llm=False)
        follow = tanker_chat_mod.answer_chat(
            st_closed,
            "Fleece 1",
            {"callsign": "TEXACO 1"},
            choice_id="_any",
            transcript="What do you like to order in and out?",
            config={"tanker_chat_llm": "off"},
        )
        echo_q = tanker_chat_mod.looks_like_own_echo(
            "What do you like to order in and out?",
            {
                "tanker_chat_last_spoke": (
                    "Do you guys like a good In-N-Out? Say yes or no"
                ),
                "tanker_chat_guard_until": 4e12,
            },
        )
        st_topic = {}
        tanker_chat_mod.append_history(
            st_topic, "boom", "Last slice of pizza — pepperoni or pineapple?"
        )
        tanker_chat_mod.append_history(st_topic, "pilot", "Pepperoni.")
        tanker_chat_mod.append_history(
            st_topic, "boom", "Do you guys like a good In-N-Out?"
        )
        banned = tanker_chat_mod.exhausted_topics(st_topic)
        skip_node = tanker_chat_mod._pick_library_for_state(st_topic, [], None)
        skip_blob = str((skip_node or {}).get("opener") or "").lower()
        if (
            not follow
            or not tanker_chat_mod.is_awaiting_react(st_closed)
            or echo_q
            or "pizza" not in banned
            or "in-n-out" not in banned
            or "pizza" in skip_blob
            or "in-n-out" in skip_blob
            or "in and out" in skip_blob
        ):
            print(
                f"  FAIL tanker follow-up/topic cooldown: follow={follow!r} "
                f"echo_q={echo_q} banned={banned} skip={skip_blob!r}"
            )
            bad += 1
        else:
            print("tanker chat — follow-up after closed bit; pizza/In-N-Out cooldown")

        st_rep = {tanker_chat_mod._GREETED_KEY: True}
        first_bit = tanker_chat_mod.start_chat(
            st_rep, "Fleece 1", {"callsign": "TEXACO 1"}, thread_id="pizza"
        )
        tanker_chat_mod.append_history(
            st_rep, "boom", "Do you guys like a good In-N-Out? Say yes or no"
        )
        next_bit = tanker_chat_mod.start_chat(
            st_rep, "Fleece 1", {"callsign": "TEXACO 1"}
        )
        same_pizza = tanker_chat_mod.line_is_repeat(st_rep, first_bit)
        same_innout = tanker_chat_mod.line_is_repeat(
            st_rep, "Do you guys like a good In-N-Out? Say yes or no"
        )
        next_l = (next_bit or "").lower()
        if (
            not first_bit
            or not next_bit
            or not same_pizza
            or not same_innout
            or "pizza" in next_l
            or "in-n-out" in next_l
            or "in and out" in next_l
            or tanker_chat_mod._line_fingerprint(next_bit)
            == tanker_chat_mod._line_fingerprint(first_bit)
        ):
            print(
                f"  FAIL tanker must not repeat a bit unless say again: "
                f"first={first_bit!r} next={next_bit!r}"
            )
            bad += 1
        else:
            print("tanker chat — no repeat unless say again")

        import tanker as tanker_mod

        early = tanker_mod.boom_chat_gate(
            rejoined=True,
            chat_open=False,
            dist_nm=5.0,
            receivers=1,
            auto_done=False,
            now=100.0,
            in_range_since=None,
        )
        hold = tanker_mod.boom_chat_gate(
            rejoined=True,
            chat_open=False,
            dist_nm=0.3,
            receivers=1,
            auto_done=False,
            now=100.0,
            in_range_since=None,
        )
        too_soon = tanker_mod.boom_chat_gate(
            rejoined=True,
            chat_open=False,
            dist_nm=0.3,
            receivers=1,
            auto_done=False,
            now=120.0,
            in_range_since=100.0,
            dwell_s=45.0,
        )
        ready_g = tanker_mod.boom_chat_gate(
            rejoined=True,
            chat_open=False,
            dist_nm=0.3,
            receivers=1,
            auto_done=False,
            now=160.0,
            in_range_since=100.0,
            dwell_s=45.0,
        )
        nojoin = tanker_mod.boom_chat_gate(
            rejoined=False,
            chat_open=False,
            dist_nm=0.3,
            receivers=1,
            auto_done=False,
            now=110.0,
            in_range_since=100.0,
        )
        st_vis = {}
        tanker_mod.mark_rejoined(st_vis, True)
        rows = tanker_chat_mod.fly_request_rows(st_vis)
        boom_on_c2 = atc_phrase.pilot_requests_for_channel(
            "blackjack", st_vis, airport=nellis, phase="flight"
        )
        boom_on_tanker = atc_phrase.pilot_requests_for_channel(
            "tanker", st_vis, airport=nellis, phase="flight"
        )
        ollama = tanker_chat_mod.resolve_llm_provider({"tanker_chat_llm": "ollama"})
        if (
            early.get("ready")
            or hold.get("ready")
            or too_soon.get("ready")
            or not ready_g.get("ready")
            or nojoin.get("ready")
            or not tanker_chat_mod.fly_controls_visible(st_vis)
            or rows[0][0] != "tanker_chat_start"
            or "how" not in str(rows[0][1] or "").lower()
            or any(k == "tanker_chat_stop" for k, _ in boom_on_tanker)
            or any(k == "tanker_chat_start" for k, _ in boom_on_c2)
            or not any(k == "tanker_chat_start" for k, _ in boom_on_tanker)
            or ollama != ("ollama", "")
        ):
            print(
                f"  FAIL tanker boom gate: early={early} hold={hold} soon={too_soon} "
                f"ready={ready_g} nojoin={nojoin} rows={rows} c2={boom_on_c2} "
                f"tanker={boom_on_tanker} ollama={ollama}"
            )
            bad += 1
        else:
            print(
                "tanker boom chat — proximity gate still scores range; "
                "Fly start-chat button only on tanker freq"
            )
        st_live = {
            "tanker_callsign": "TEXACO 1",
            "tanker_chat": {
                "session": True,
                "choices": [{"id": "a", "say": "Coffee", "hits": ("coffee",)}],
            },
        }
        live_btns = atc_phrase.pilot_requests_for_channel(
            "tanker", st_live, airport=nellis, phase="flight"
        )
        live_keys = [k for k, _ in live_btns]
        idle_cues = voice_intent.suggestions(
            phase="flight",
            channel="tanker",
            expected="bj_check_in",
            callsign=CALLSIGN,
            limit=5,
            advance_limit=2,
            optional_limit=3,
        )
        live_cues = voice_intent.suggestions(
            phase="flight",
            channel="tanker",
            expected="bj_check_in",
            callsign=CALLSIGN,
            tanker_chat_session=True,
            tanker_chat_choices=[{"id": "a", "say": "Coffee"}],
            limit=5,
            advance_limit=2,
            optional_limit=3,
        )
        idle_says = [str(s).casefold() for s, *_ in idle_cues]
        live_says = [str(s).casefold() for s, *_ in live_cues]
        if (
            "tanker_chat_start" in live_keys
            or "tanker_chat_stop" not in live_keys
            or not any("talk later" in lab.casefold() for k, lab in live_btns if k == "tanker_chat_stop")
            or not any("how's it going" in s or "hows it going" in s for s in idle_says)
            or any("talk later" in s for s in idle_says)
            or any("how's it going" in s or "hows it going" in s for s in live_says)
            or not any("talk later" in s for s in live_says)
        ):
            print(
                f"  FAIL tanker chat cues must swap start/stop: "
                f"btns={live_btns} idle={idle_cues} live={live_cues}"
            )
            bad += 1
        else:
            print("tanker chat cues — how's it going idle, talk later mid-chat")
        if (
            not tanker_chat_mod.match_stop("Texaco stop talking")
            or not tanker_chat_mod.match_stop("stop")
            or not tanker_chat_mod.match_stop("that's enough")
            or not tanker_chat_mod.match_stop("that s enough")
            or tanker_chat_mod.match_stop("how's it going")
        ):
            print("  FAIL tanker chat stop phrases")
            bad += 1
        else:
            print("tanker chat stop — stop / stop talking / that's enough")

        ev_miss = voice_intent.evaluate(
            "Nellis Ground, Fleece 1, permission to leave the ramp",
            channel="ground",
            phase="departure",
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        ev_chatter = voice_intent.evaluate(
            "Two, go button five",
            channel="ground",
            phase="departure",
            callsign=CALLSIGN,
            runways=RUNWAYS,
        )
        gnd = voice_nlu.allowed_intents(channel="ground", phase="departure")
        gnd_ids = {i.id for i in gnd}
        parsed_ok = voice_nlu.parse_choice({"intent": "ready_taxi"}, gnd)
        parsed_c2 = voice_nlu.parse_choice({"intent": "request_picture"}, gnd)
        parsed_none = voice_nlu.parse_choice({"intent": "none"}, gnd)
        nlu_match = voice_nlu.match_from_choice(
            "ready_taxi",
            {},
            transcript="Nellis Ground, Fleece 1, permission to leave the ramp",
            allowed=gnd,
            addressed="ground",
        )
        nlu_off = voice_nlu.nlu_enabled(
            {"voice_nlu_enabled": False, "tanker_chat_llm": "ollama"}
        )
        nlu_on = voice_nlu.nlu_enabled({"tanker_chat_llm": "ollama"})
        if (
            ev_miss.fired
            or not voice_nlu.should_try(ev_miss)
            or voice_nlu.should_try(ev_chatter)
            or "ready_taxi" not in gnd_ids
            or "request_picture" in gnd_ids
            or parsed_ok is None
            or parsed_ok[0] != "ready_taxi"
            or parsed_c2 is not None
            or parsed_none is not None
            or nlu_match is None
            or nlu_match.intent != "ready_taxi"
            or nlu_off
            or not nlu_on
        ):
            print(
                f"  FAIL voice NLU: miss={ev_miss.describe()} chatter={ev_chatter.describe()} "
                f"ids={sorted(gnd_ids)} ok={parsed_ok} c2={parsed_c2} none={parsed_none} "
                f"match={nlu_match} off={nlu_off} on={nlu_on}"
            )
            bad += 1
        else:
            print(
                "voice NLU — allowed intents only; chatter skipped; no network"
            )

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

        radar_hi = atc_phrase.build_template_text(
            nellis,
            "radar_contact",
            "Fleece 1",
            atc_phrase.Weather(210, 5, 29.92, ""),
            "21R",
            opus=opus_hi,
            initial_climb_ft=15000,
            state={"initial_climb_ft": 15000},
        )
        no_fp_radar_opus = atc_phrase.synthetic_flight_context("Fleece 1")
        radar_lo = atc_phrase.build_template_text(
            nellis,
            "radar_contact",
            "Fleece 1",
            atc_phrase.Weather(210, 5, 29.92, ""),
            "21R",
            opus=no_fp_radar_opus,
            initial_climb_ft=15000,
            state={"initial_climb_ft": 15000},
        )
        skip_after_radar = atc_phrase.should_skip_cruise_climb_step(
            {"template": "climb_cruise"},
            state={
                "initial_climb_ft": 15000,
                "filed_altitude_ft": 22000,
                "departure_assigned_ft": 22000,
            },
            opus=opus_hi,
        )
        hi = radar_hi.lower()
        lo = radar_lo.lower()
        if (
            "flight level two two zero" not in hi
            or "fifteen" in hi
            or "one five" in hi
            or ("thousand" not in lo and "fifteen" not in lo)
            or not skip_after_radar
        ):
            print(
                f"  FAIL departure radar to filed FL: hi={radar_hi!r} lo={radar_lo!r} "
                f"skip_after={skip_after_radar}"
            )
            bad += 1
        else:
            print("departure radar contact — climb to filed FL, not the Delivery interim")

        import json
        from pathlib import Path

        import flow_engine as fe

        flow_path = Path(__file__).resolve().parent / "flows" / "nellis_default.json"
        dep_mission = json.loads(flow_path.read_text(encoding="utf-8"))
        dep_eng = fe.FlowEngine(
            dry_run=True,
            persist_state=False,
            config={
                "dry_run": True,
                "freq_gate_enabled": False,
                "flow_file": "flows/nellis_default.json",
            },
            mission=dep_mission,
            airports=airports,
        )
        dep_eng.state.update(
            {
                "index": 19,
                "last_step_id": "dep_radar",
                "last_tx_template": "climb_cruise",
                "departure_assigned_ft": 24000,
                "filed_altitude_ft": 24000,
                "control_checked_in": True,
                "last_agency": "departure",
                "contact_phase": "airborne",
            }
        )
        parked = dep_eng.current_step() or {}
        if str(parked.get("template") or "") != "departure_handoff":
            print(
                f"  FAIL after radar, cursor must stay on departure handoff, "
                f"not {parked.get('id')!r} / {parked.get('template')!r} "
                f"index={dep_eng.state.get('index')}"
            )
            bad += 1
        else:
            print("departure radar — stay on Blackjack handoff, not Approach")

        parked_i = int(dep_eng.state.get("index") or 0)
        dep_eng.sync_readback_for_cursor = lambda: None
        seeked = dep_eng.seek(parked_i + 1)
        after_seek = dep_eng.current_step() or {}
        if (
            not seeked.get("seeked")
            or str(after_seek.get("template") or "") == "departure_handoff"
            or int(dep_eng.state.get("index") or 0) <= parked_i
        ):
            print(
                f"  FAIL manual seek past departure handoff snapped back to "
                f"{after_seek.get('id')!r} / {after_seek.get('template')!r} "
                f"index={dep_eng.state.get('index')}"
            )
            bad += 1
        else:
            print("departure radar — manual seek past Blackjack handoff stays put")

        # Arrows after the Control handoff: every press must move exactly one
        # step, including onto the check-ins the flow now marks skippable.
        # Fly used to pin the header to pending_contact, so cycling looked dead.
        import agencies as agencies_mod

        cyc_eng = fe.FlowEngine(
            dry_run=True,
            persist_state=False,
            config={
                "dry_run": True,
                "freq_gate_enabled": False,
                "flow_file": "flows/nellis_default.json",
            },
            mission=dep_mission,
            airports=airports,
        )
        cyc_eng.sync_readback_for_cursor = lambda: None
        cyc_steps = cyc_eng.steps
        app_i = next(
            i
            for i, s in enumerate(cyc_steps)
            if str(s.get("template") or "") == "approach_check_in"
        )
        cyc_eng.state.update(
            {
                "index": app_i,
                "control_checked_in": True,
                "control_channel": "control_east",
                "pending_contact": "approach",
                "last_agency": "control_east",
                "last_tx_template": "control_handoff",
                "contact_phase": "airborne",
            }
        )
        walk = [int(cyc_eng.seek_relative(-1)["index"]) for _ in range(4)]
        if walk != [app_i - 1, app_i - 2, app_i - 3, app_i - 4]:
            print(f"  FAIL Back must step one at a time, got {walk} from {app_i}")
            bad += 1
        elif not any(
            cyc_eng._step_is_skippable(cyc_steps[i]) for i in walk
        ):
            print(f"  FAIL Back should have reached a skippable Control step: {walk}")
            bad += 1
        elif not cyc_eng.state.get("manual_step_view"):
            print("  FAIL an arrow press must mark the cursor hand-parked")
            bad += 1
        else:
            parked_i = int(cyc_eng.state.get("index") or 0)
            if int(cyc_eng.status().get("index") or -1) != parked_i:
                print("  FAIL a hand-parked cursor must survive the next status poll")
                bad += 1
            else:
                agencies_mod.note_tx(cyc_eng.state, "control_east", "control_check_in")
                if cyc_eng.state.get("manual_step_view"):
                    print("  FAIL a transmission must release the hand-parked cursor")
                    bad += 1
                else:
                    print(
                        "manual cycling — one step per press, lands on skipped "
                        "steps, released by TX"
                    )

        rb_climb = voice_intent.evaluate(
            "Nellis Departure, Fleece 1, climb and maintain flight level two four zero",
            channel="departure",
            phase="departure",
            expected="radar_contact",
            awaiting_readback=True,
            readback_items=[
                {
                    "key": "climb",
                    "label": "Climb",
                    "value": "FL240",
                    "spoken": "flight level two four zero",
                    "highlight": True,
                    "hinge": True,
                }
            ],
            callsign=CALLSIGN,
            current_step_id="dep_handoff",
            steps=list(dep_eng.steps),
        )
        if (
            not rb_climb.fired
            or rb_climb.match is None
            or rb_climb.match.intent != "acknowledge_readback"
        ):
            print(f"  FAIL radar climb readback must close the card: {rb_climb.describe()}")
            bad += 1
        else:
            print("departure radar readback — climb and maintain is the readback")

        no_fp_opus = atc_phrase.synthetic_flight_context("Fleece 1")
        wx = atc_phrase.Weather(210, 5, 29.92, "")
        no_fp_txt, no_fp_climb = atc_phrase.build_clearance_delivery(
            nellis,
            "Fleece 1",
            wx,
            "21R",
            no_fp_opus,
            initial_climb_ft=12000,
            channel="delivery",
        )
        no_fp_low = no_fp_txt.lower()
        no_fp_rb = atc_phrase.build_readback_checklist(
            "clearance", nellis, no_fp_opus, wx, "21R", climb_ft=12000
        )
        no_fp_confirm = atc_phrase.build_clearance_readback(
            nellis, "Fleece 1", wx, "21R", channel="delivery", opus=no_fp_opus
        ).lower()
        if (
            "flight plan on file" not in no_fp_low
            or "climb" in no_fp_low
            or "maintain" in no_fp_low
            or no_fp_climb
            or no_fp_rb
            or "readback correct" in no_fp_confirm
            or "contact" in no_fp_confirm
        ):
            print(
                f"  FAIL no-FP clearance must not assign a climb: "
                f"txt={no_fp_txt!r} climb={no_fp_climb} rb={no_fp_rb} "
                f"confirm={no_fp_confirm!r}"
            )
            bad += 1
        else:
            print("no flight plan — Delivery does not invent climb / IFR")

        peaks_route = "KLSV.PEAKS21R.HAYFD.ST LOUIS.TORYE.KLSV.21R"
        peaks = atc_phrase.match_departure(nellis, peaks_route)
        peaks_say = atc_phrase.speak_departure_clearance(nellis, peaks_route) or ""
        peaks_low = peaks_say.lower()
        if (
            peaks.instrument_id != "PEAKS"
            or str(peaks.transition_id or "").upper() != "HAYFD"
            or "peaks" not in peaks_low
            or "hayford peak" not in peaks_low
            or "transition" not in peaks_low
            or peaks.runway != "21R"
        ):
            print(f"  FAIL PEAKS Hayford Peak: {peaks} {peaks_say!r}")
            bad += 1
        else:
            print(f"PEAKS Hayford Peak — {peaks_say}")

        peaks_mormon_route = "KLSV.PEAKS21R.MORMONPK.ELGIN.KLSV.21R"
        peaks_mormon = atc_phrase.match_departure(nellis, peaks_mormon_route)
        peaks_mormon_say = (
            atc_phrase.speak_departure_clearance(nellis, peaks_mormon_route) or ""
        )
        peaks_mormon_low = peaks_mormon_say.lower()
        if (
            peaks_mormon.instrument_id != "PEAKS"
            or str(peaks_mormon.transition_id or "").upper() != "MORMONPK"
            or "peaks" not in peaks_mormon_low
            or "mormon peak" not in peaks_mormon_low
            or "transition" not in peaks_mormon_low
            or peaks_mormon.runway != "21R"
        ):
            print(f"  FAIL PEAKS Mormon Peak: {peaks_mormon} {peaks_mormon_say!r}")
            bad += 1
        else:
            print(f"PEAKS Mormon Peak — {peaks_mormon_say}")

        flex_opus = atc_phrase.synthetic_flight_context("Fleece 1")
        flex_opus.fp_route_string = "KLSV FLEX21R DREAM JUNNO STRYK KLSV"
        flex_opus.fp_altitude = "FL240"
        flex_opus.arr_icao = "KLSV"
        flex_txt, flex_climb = atc_phrase.build_clearance_delivery(
            nellis, "Fleece 1", wx, "21R", flex_opus, channel="delivery"
        )
        flex_low = flex_txt.lower()
        sid_opus = atc_phrase.synthetic_flight_context("Fleece 1")
        sid_opus.fp_route_string = "KLSV DREAM7 MINTT KLSV"
        sid_opus.fp_altitude = "FL240"
        sid_opus.arr_icao = "KLSV"
        sid_txt, _sid_climb = atc_phrase.build_clearance_delivery(
            nellis,
            "Fleece 1",
            wx,
            "21R",
            sid_opus,
            initial_climb_ft=14000,
            channel="delivery",
        )
        sid_low = sid_txt.lower()
        if (
            "climb as published" not in flex_low
            or "via the sid" in flex_low
            or flex_climb
            or "flex" not in flex_low
        ):
            print(f"  FAIL VFR Flex clearance climb as published: {flex_txt!r}")
            bad += 1
        elif "climb via the sid" not in sid_low or "climb as published" in sid_low:
            print(f"  FAIL instrument SID should climb via the SID: {sid_txt!r}")
            bad += 1
        else:
            print("VFR Flex — climb as published; SID — climb via the SID")

        # Dotted Opus/map routes stuffed into dest must not be spelled letter-by-letter.
        dotted = "KLSV.MMM8.ILC171028.KRYSS.KLSV"
        mmm_opus = atc_phrase.synthetic_flight_context("Dagger 1")
        mmm_opus.fp_route_string = dotted
        mmm_opus.fp_altitude = "FL240"
        mmm_opus.arr_icao = dotted
        mmm_txt, _mmm_climb = atc_phrase.build_clearance_delivery(
            nellis, "Dagger 1", wx, "21R", mmm_opus, initial_climb_ft=14000,
            channel="delivery",
        )
        mmm_low = mmm_txt.lower()
        if atc_phrase.destination_icao(dotted) != "KLSV":
            print(f"  FAIL dotted route dest should be KLSV: {dotted}")
            bad += 1
        elif "k l s v . m m m" in mmm_low or "m m m 8" in mmm_low or "i l c" in mmm_low:
            print(f"  FAIL clearance must not spell the flight plan: {mmm_txt!r}")
            bad += 1
        elif "cleared to nellis" not in mmm_low:
            print(f"  FAIL local MMM8 should clear to Nellis: {mmm_txt!r}")
            bad += 1
        elif "mormon mesa" not in mmm_low or "then as filed" not in mmm_low:
            print(f"  FAIL MMM8 clearance should name the SID then as filed: {mmm_txt!r}")
            bad += 1
        else:
            print(f"clearance dest — Nellis, not spelled route ({mmm_txt})")

        if "squawk" not in mmm_low:
            print(f"  FAIL filed-plan clearance must assign a squawk: {mmm_txt!r}")
            bad += 1
        else:
            print(f"clearance squawk — assigned when Opus has none ({mmm_txt})")

        known_sq = atc_phrase.synthetic_flight_context("Dagger 1")
        known_sq.fp_route_string = dotted
        known_sq.fp_altitude = "FL240"
        known_sq.arr_icao = "KLSV"
        known_sq.mode3 = "0551"
        known_txt, _ = atc_phrase.build_clearance_delivery(
            nellis, "Dagger 1", wx, "21R", known_sq, initial_climb_ft=14000,
            channel="delivery",
        )
        if "squawk zero fife fife one" not in known_txt.lower():
            print(f"  FAIL Opus Mode 3 must be spoken: {known_txt!r}")
            bad += 1
        else:
            print("clearance squawk — uses Opus Mode 3 when filed")

        vfr_low = atc_phrase.synthetic_flight_context("Dagger 1")
        vfr_low.fp_route_string = "KLSV FLEX21R DREAM JUNNO STRYK KLSV"
        vfr_low.fp_altitude = "10000"
        vfr_low.arr_icao = "KLSV"
        vfr_need, vfr_cruise, vfr_rules = atc_phrase.clearance_msa_amendment(
            nellis, vfr_low
        )
        ifr_low = atc_phrase.synthetic_flight_context("Dagger 1")
        ifr_low.fp_route_string = "KLSV.MMM8.KLSV"
        ifr_low.fp_altitude = "10000"
        ifr_low.arr_icao = "KLSV"
        ifr_need, ifr_cruise, ifr_rules = atc_phrase.clearance_msa_amendment(
            nellis, ifr_low
        )
        vfr_ok = atc_phrase.synthetic_flight_context("Dagger 1")
        vfr_ok.fp_route_string = "KLSV FLEX21R DREAM KLSV"
        vfr_ok.fp_altitude = "FL240"
        vfr_ok.arr_icao = "KLSV"
        ok_need, _, _ = atc_phrase.clearance_msa_amendment(nellis, vfr_ok)
        flex_west_ok = atc_phrase.synthetic_flight_context("Dagger 1")
        flex_west_ok.fp_route_string = "KLSV FLEX21R FYTTR KLSV"
        flex_west_ok.fp_altitude = "13000"
        flex_west_ok.arr_icao = "KLSV"
        west_ok_need, _, west_ok_rules = atc_phrase.clearance_msa_amendment(
            nellis, flex_west_ok
        )
        flex_west_low = atc_phrase.synthetic_flight_context("Dagger 1")
        flex_west_low.fp_route_string = "KLSV FLEX21R FYTTR KLSV"
        flex_west_low.fp_altitude = "10000"
        flex_west_low.arr_icao = "KLSV"
        west_need, west_cruise, _ = atc_phrase.clearance_msa_amendment(
            nellis, flex_west_low
        )
        flex_n_ok = atc_phrase.synthetic_flight_context("Dagger 1")
        flex_n_ok.fp_route_string = "KLSV FLEX21R DREAM KLSV"
        flex_n_ok.fp_altitude = "15000"
        flex_n_ok.arr_icao = "KLSV"
        north_ok_need, _, _ = atc_phrase.clearance_msa_amendment(nellis, flex_n_ok)
        mintt_ok = atc_phrase.synthetic_flight_context("Dagger 1")
        mintt_ok.fp_route_string = "KLSV DREAM7 MINTT KLSV"
        mintt_ok.fp_altitude = "17000"
        mintt_ok.arr_icao = "KLSV"
        mintt_need, _, _ = atc_phrase.clearance_msa_amendment(nellis, mintt_ok)
        mintt_low = atc_phrase.synthetic_flight_context("Dagger 1")
        mintt_low.fp_route_string = "KLSV DREAM7 MINTT KLSV"
        mintt_low.fp_altitude = "16000"
        mintt_low.arr_icao = "KLSV"
        mintt_low_need, mintt_low_cruise, _ = atc_phrase.clearance_msa_amendment(
            nellis, mintt_low
        )
        vfr_req = atc_phrase.clearance_required_altitude_ft(nellis, vfr_low)
        west_req = atc_phrase.clearance_required_altitude_ft(nellis, flex_west_ok)
        amend_txt = atc_phrase.build_template_text(
            nellis, "clearance", "Dagger 1", wx, "21R", opus=vfr_low, state={}
        ).lower()
        after_copy, after_climb = atc_phrase.build_clearance_delivery(
            nellis,
            "Dagger 1",
            wx,
            "21R",
            vfr_low,
            channel="delivery",
            state={"clearance_amendment_copied": True},
        )
        after_low = after_copy.lower()
        ifr_after, ifr_climb = atc_phrase.build_clearance_delivery(
            nellis,
            "Dagger 1",
            wx,
            "21R",
            ifr_low,
            initial_climb_ft=14000,
            channel="delivery",
            state={"clearance_amendment_copied": True},
        )
        ifr_after_low = ifr_after.lower()
        west_after, west_after_climb = atc_phrase.build_clearance_delivery(
            nellis,
            "Dagger 1",
            wx,
            "21R",
            flex_west_low,
            channel="delivery",
            state={"clearance_amendment_copied": True},
        )
        west_after_low = west_after.lower()
        if (
            not vfr_need
            or vfr_rules != "vfr"
            or vfr_req != 11500
            or vfr_cruise < 17000
            or not ifr_need
            or ifr_rules != "ifr"
            or ifr_cruise < 17000
            or ok_need
            or west_ok_need
            or west_ok_rules != "vfr"
            or west_req != 11500
            or not west_need
            or west_cruise != 13000
            or north_ok_need
            or mintt_need
            or not mintt_low_need
            or mintt_low_cruise < 17000
        ):
            print(
                f"  FAIL altitude judgment: vfr={vfr_need}/{vfr_rules}/"
                f"{vfr_req}/{vfr_cruise} ifr={ifr_need}/{ifr_rules}/{ifr_cruise} "
                f"ok={ok_need} west13={west_ok_need}/{west_req} west10="
                f"{west_need}/{west_cruise} north15={north_ok_need} "
                f"mintt17={mintt_need} mintt16={mintt_low_need}/{mintt_low_cruise}"
            )
            bad += 1
        elif "amendment to your flight plan" not in amend_txt or "ready to copy" not in amend_txt:
            print(f"  FAIL low filed alt must offer an amendment first: {amend_txt}")
            bad += 1
        elif (
            "cleared to" not in after_low
            or "climb as published" not in after_low
            or "flight level one niner" not in after_low
            or after_climb < 19000
        ):
            print(f"  FAIL Flex north missing-alt expect usual 19000: {after_copy}")
            bad += 1
        elif (
            "cleared to" not in ifr_after_low
            or "climb via the sid" not in ifr_after_low
            or (
                "seven thousand" not in ifr_after_low
                and "seventeen thousand" not in ifr_after_low
            )
            or ifr_climb < 17000
        ):
            print(f"  FAIL IFR amendment must expect typical 17000: {ifr_after}")
            bad += 1
        elif (
            "cleared to" not in west_after_low
            or "climb as published" not in west_after_low
            or (
                "tree thousand" not in west_after_low
                and "thirteen thousand" not in west_after_low
            )
            or west_after_climb != 13000
        ):
            print(f"  FAIL Flex west amendment must expect 13000: {west_after}")
            bad += 1
        else:
            print("clearance altitude — MSA hard, Flex west typical, SID crossings IFR")

        change = atc_phrase.clearance_amendment_change(nellis, vfr_low)
        no_change = atc_phrase.clearance_amendment_change(nellis, vfr_ok)
        amend_rb = atc_phrase.build_readback_checklist(
            "clearance_amendment", nellis, vfr_low, wx, "21R"
        )
        after_rb = atc_phrase.build_readback_checklist(
            "clearance",
            nellis,
            vfr_low,
            wx,
            "21R",
            state={"clearance_amendment_copied": True},
        )
        amend_vals = [str(i.get("value") or "") for i in amend_rb]
        after_expect = [
            i for i in after_rb if str(i.get("key") or "") == "expect"
        ]
        if (
            not change
            or change.get("summary") != "10,000 → FL190"
            or no_change is not None
            or not any("10,000 → FL190" in v for v in amend_vals)
            or not any(i.get("highlight") for i in amend_rb if str(i.get("key") or "") == "amend_alt")
            or not after_expect
            or not after_expect[0].get("highlight")
            or "10,000 → FL190" not in str(after_expect[0].get("value") or "")
        ):
            print(
                f"  FAIL amendment cue must show 10,000 -> FL190: "
                f"change={change} no={no_change} amend_rb={amend_rb} after={after_expect}"
            )
            bad += 1
        else:
            print("clearance amendment cue — 10,000 -> FL190 highlighted")
        before_call = voice_intent.suggestions(
            phase="departure",
            channel="delivery",
            expected="clearance",
            callsign=CALLSIGN,
            last_tx_template="",
            limit=5,
            advance_limit=2,
            optional_limit=3,
        )
        after_offer = voice_intent.suggestions(
            phase="departure",
            channel="delivery",
            expected="clearance_amendment",
            callsign=CALLSIGN,
            last_tx_template="clearance_amendment",
            limit=5,
            advance_limit=2,
            optional_limit=3,
        )
        before_says = [str(s).casefold() for s, *_ in before_call]
        after_adv = [
            str(s).casefold()
            for s, _d, r, *_ in after_offer
            if r == "advance"
        ]
        if any("ready to copy" in s for s in before_says):
            print(f"  FAIL do not tip ready to copy before Delivery offers: {before_call}")
            bad += 1
        elif not after_adv or "ready to copy" not in after_adv[0]:
            print(f"  FAIL after amendment offer tip ready to copy: {after_offer}")
            bad += 1
        else:
            print("clearance amendment cues — ready to copy only after CD offers")

        alias_sq = atc_phrase._opus_mode3_from_fields({"squawk": "4321"})
        if alias_sq != "4321":
            print(f"  FAIL Opus squawk field should map to Mode 3: {alias_sq!r}")
            bad += 1
        camel_sq = atc_phrase._opus_mode3_from_fields({"Mode3": 551})
        if camel_sq != "0551":
            print(f"  FAIL Opus Mode3 551 should keep the leading zero: {camel_sq!r}")
            bad += 1
        nested_sq = atc_phrase._opus_mode3_from_fields(
            {"fp_route_string": "KLSV.MMM8.KLSV", "flight_plan": {"squawk": "3210"}}
        )
        if nested_sq != "3210":
            print(f"  FAIL nested flight_plan squawk: {nested_sq!r}")
            bad += 1
        remark_sq = atc_phrase._opus_mode3_from_fields(
            {"fp_remarks": "MODE3 4321 / TCN 17Y"}
        )
        if remark_sq != "4321":
            print(f"  FAIL remarks Mode 3: {remark_sq!r}")
            bad += 1
        st_sq: dict = {}
        first = atc_phrase.ensure_clearance_squawk(mmm_opus, state=st_sq)
        again = atc_phrase.ensure_clearance_squawk(
            atc_phrase.synthetic_flight_context("Dagger 1"),
            state=st_sq,
        )
        if not first or first != again or first != st_sq.get("assigned_squawk"):
            print(f"  FAIL assigned squawk must stick: {first!r} {again!r} {st_sq}")
            bad += 1

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
        dep_rolling = {
            k
            for k, _ in atc_phrase.pilot_requests_for_channel(
                "tower",
                {"active_takeoff_mode": "rolling"},
                phase="departure",
                template="lineup",
            )
        }
        app_reqs = {
            k
            for k, _ in atc_phrase.pilot_requests_for_channel(
                "tower", phase="approach", template="clear_land"
            )
        }
        if "request_rolling" not in dep_reqs or "request_lineup" in dep_reqs:
            print(f"  FAIL default LUAW should only offer rolling (not expect-LUAW): {dep_reqs}")
            bad += 1
        elif "request_lineup" not in dep_rolling or "request_rolling" in dep_rolling:
            print(f"  FAIL rolling mode should only offer LUAW: {dep_rolling}")
            bad += 1
        elif atc_phrase.takeoff_request_changes_mode("request_lineup"):
            print("  FAIL default takeoff is LUAW — request_lineup is not a mode change")
            bad += 1
        elif not atc_phrase.takeoff_request_changes_mode("request_rolling"):
            print("  FAIL request_rolling from LUAW must be a mode change")
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
                "tower pilot requests — other takeoff type only; "
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

        # Alpha check: altitude in thousands — 6k / 18k / 24k → angels 6 / 18 / 24.
        if atc_phrase.speak_angels(6000) != "angels six":
            print(f"  FAIL angels 6000: {atc_phrase.speak_angels(6000)!r}")
            bad += 1
        elif atc_phrase.speak_angels(18000) != "angels one eight":
            print(f"  FAIL angels 18000: {atc_phrase.speak_angels(18000)!r}")
            bad += 1
        elif atc_phrase.speak_angels(24000) != "angels two four":
            print(f"  FAIL angels 24000: {atc_phrase.speak_angels(24000)!r}")
            bad += 1
        spoken_alpha = atc_phrase.speak_alpha_bullseye("ELVIS", 305, 25, alt_ft=24000)
        if "angels two four" not in spoken_alpha or "zero" in spoken_alpha.split("angels", 1)[-1]:
            print(f"  FAIL alpha speech angels: {spoken_alpha!r}")
            bad += 1
        bj = atc_phrase.build_standalone_alpha_check("Fleece 1", spoken_alpha)
        bs = atc_phrase.build_standalone_alpha_check(
            "Fleece 1", spoken_alpha, agency="bandsaw"
        )
        cin = atc_phrase.build_bandsaw_check_in(
            "Fleece 1", alpha_bullseye=spoken_alpha
        )
        unit = {
            "name": "FLEECE 1",
            "flightLabel": "FLEECE 1",
            "xMeters": 0,
            "zMeters": 0,
            "altMeters": 24000 / 3.28084,
            "atcPosition": "ELVIS 305 25",
        }
        fix = atc_phrase.bullseye_for_caoc_unit(unit, {})
        spoken_fix = str((fix or {}).get("spoken") or "")
        if "Blackjack" not in bj or "angels two four" not in bj:
            print(f"  FAIL blackjack alpha check: {bj!r}")
            bad += 1
        elif "Bandsaw" not in bs or "angels two four" not in bs:
            print(f"  FAIL bandsaw alpha check: {bs!r}")
            bad += 1
        elif "angels two four" not in cin:
            print(f"  FAIL bandsaw check-in alpha: {cin!r}")
            bad += 1
        elif "angels two four" not in spoken_fix or "zero" in spoken_fix.split("angels", 1)[-1]:
            print(f"  FAIL CAOC alpha altitude: {fix}")
            bad += 1
        else:
            print("alpha check — Angels 24 (two four) from Blackjack and Bandsaw")

        import tanker as tanker_mod

        class _TankerEng:
            def __init__(self) -> None:
                self.steps = [
                    {
                        "id": "bj",
                        "channel": "blackjack",
                        "template": "bj_check_in",
                    },
                    {
                        "id": "tk",
                        "channel": "tanker",
                        "template": "radio_check",
                    },
                    {
                        "id": "bs",
                        "channel": "bandsaw",
                        "template": "bandsaw_check_in",
                    },
                    {
                        "id": "ex",
                        "channel": "blackjack",
                        "template": "bj_range_exit",
                    },
                ]
                self.state: dict = {"index": 0}

            def save_state(self) -> None:
                return None

            def _advance_past_skippable(self) -> None:
                idx = int(self.state.get("index") or 0)
                while idx < len(self.steps) and tanker_mod.should_skip_tanker_step(
                    self.steps[idx], self.state
                ):
                    idx += 1
                self.state["index"] = idx

        eng = _TankerEng()
        skip_idle = tanker_mod.should_skip_tanker_step(eng.steps[1], eng.state)
        tanker_mod.enter_tanker_overlay(eng)
        skip_aar = tanker_mod.should_skip_tanker_step(
            eng.steps[int(eng.state["index"])], eng.state
        )
        on_tk = str(eng.steps[int(eng.state["index"])].get("id")) == "tk"
        eng.state["tanker_seen_tune"] = True
        leave_bs = tanker_mod.leave_tanker_overlay(eng, "bandsaw", checkin=True)
        on_bs = str(eng.steps[int(eng.state["index"])].get("id")) == "bs"
        eng2 = _TankerEng()
        tanker_mod.enter_tanker_overlay(eng2)
        eng2.state["tanker_seen_tune"] = True
        tanker_mod.leave_tanker_overlay(eng2, "blackjack", checkin=True)
        on_bj = str(eng2.steps[int(eng2.state["index"])].get("id")) == "bj"
        hold_tk = voice_intent.step_holds_after_play(eng.steps[1])
        follow = tanker_mod.note_tanker_tune(
            {"tanker_overlay": True, "tanker_seen_tune": True},
            "blackjack",
        )
        stay = tanker_mod.note_tanker_tune(
            {"tanker_overlay": True}, "blackjack"
        )
        if (
            not skip_idle
            or skip_aar
            or not on_tk
            or leave_bs != "bandsaw"
            or not on_bs
            or not on_bj
            or not hold_tk
            or not follow
            or stay
        ):
            print(
                f"  FAIL tanker side trip: skip_idle={skip_idle} skip_aar={skip_aar} "
                f"on_tk={on_tk} leave_bs={leave_bs} on_bs={on_bs} on_bj={on_bj} "
                f"hold={hold_tk} follow={follow} stay={stay}"
            )
            bad += 1
        else:
            print(
                "tanker side trip — skip in timeline; request parks on tanker; "
                "return follows Blackjack or Bandsaw"
            )

        ground = _TankerEng()
        ground.steps.insert(
            0,
            {"id": "del", "channel": "delivery", "template": "clearance"},
        )
        ground.state = {
            "index": 0,
            "tanker_overlay": True,
            "tanker_rejoined": True,
            "tanker_chat_last_spoke": "Standing by, sir — looking good.",
        }
        cleared = tanker_mod.reconcile_aar_overlay(ground)
        c2 = _TankerEng()
        c2.state["tanker_overlay"] = True
        kept = not tanker_mod.reconcile_aar_overlay(c2)
        if (
            not cleared
            or tanker_mod.tanker_overlay_active(ground.state)
            or tanker_mod.has_rejoined(ground.state)
            or not kept
            or not tanker_mod.tanker_overlay_active(c2.state)
        ):
            print("  FAIL tanker overlay should clear on Delivery, stay on Blackjack")
            bad += 1
        else:
            print("tanker overlay — stripped on Delivery, kept on C2")

    # Blackjack check-in holds so optional Bandsaw does not steal the cursor.
    import agencies
    import tanker as tanker_mod
    if not voice_intent.step_holds_after_play(
        {"id": "bj", "channel": "blackjack", "template": "bj_check_in", "mode": "tts"}
    ):
        print("  FAIL Blackjack check-in must hold (no auto Bandsaw)")
        bad += 1
    else:
        print("blackjack check-in holds — Bandsaw stays opt-in")

    before_bj = voice_intent.suggestions(
        phase="flight",
        channel="blackjack",
        expected="bj_check_in",
        callsign=CALLSIGN,
        airport_name="Nellis",
        limit=8,
    )
    before_says = [str(s).lower() for s, *_ in before_bj]
    if not any("checking in" in s for s in before_says):
        print(f"  FAIL Blackjack should tip check-in before the flight is in: {before_bj}")
        bad += 1

    after_bj = voice_intent.suggestions(
        phase="flight",
        channel="blackjack",
        expected="bj_check_in",
        callsign=CALLSIGN,
        airport_name="Nellis",
        last_tx_template="bj_check_in",
        last_tx_channel="blackjack",
        blackjack_checked_in=True,
        limit=8,
    )
    after_says = [str(s).lower() for s, *_ in after_bj]
    if any("checking in" in s for s in after_says):
        print(f"  FAIL Blackjack must not tip check-in after the flight is in: {after_bj}")
        bad += 1
    else:
        print("blackjack cues after check-in — check-in removed")

    back_from_bs = voice_intent.suggestions(
        phase="flight",
        channel="blackjack",
        expected="bj_check_in",
        callsign=CALLSIGN,
        airport_name="Nellis",
        last_tx_template="bandsaw_check_out",
        last_tx_channel="bandsaw",
        blackjack_checked_in=True,
        limit=8,
    )
    back_says = [str(s).lower() for s, *_ in back_from_bs]
    if not any("checking in" in s for s in back_says):
        print(
            f"  FAIL Blackjack should tip check-in (continue) after Bandsaw: {back_from_bs}"
        )
        bad += 1
    else:
        print("blackjack cues after Bandsaw — check-in continue")

    stamped: dict = {}
    atc_phrase.build_template_text(
        nellis, "bj_check_in", "Fleece 1", vmc, "21R", state=stamped
    )
    if not stamped.get("blackjack_checked_in"):
        print(f"  FAIL Blackjack check-in must stamp blackjack_checked_in: {stamped}")
        bad += 1

    pic_tips = voice_intent.suggestions(
        phase="flight",
        channel="blackjack",
        expected="bj_check_in",
        callsign=CALLSIGN,
        airport_name="Nellis",
        limit=8,
    )
    pic_says = " ".join(str(s).lower() for s, *_ in pic_tips)
    if "picture" in pic_says or "bogey" in pic_says or "declare" in pic_says:
        print(f"  FAIL Blackjack cues must not list picture/dope/declare: {pic_tips}")
        bad += 1

    pic_txt = atc_phrase.build_blackjack_c2_redirect(nellis, "Fleece 1", "picture")
    if "bandsaw" not in pic_txt.lower() or "blackjack" not in pic_txt.lower():
        print(f"  FAIL Blackjack picture should push Bandsaw: {pic_txt}")
        bad += 1
    else:
        print(f"blackjack picture redirect — {pic_txt}")

    tanker_step = {
        "id": "tanker_opt",
        "channel": "tanker",
        "template": "radio_check",
        "c2": False,
    }
    bj_step = {
        "id": "bj",
        "channel": "blackjack",
        "template": "bj_check_in",
    }
    if not voice_intent.step_offers_c2(tanker_step, channel="bandsaw"):
        print("  FAIL Bandsaw tune must offer C2 even on tanker cursor")
        bad += 1
    if voice_intent.step_offers_c2(tanker_step, channel="tanker"):
        print("  FAIL tanker step must not offer C2 on tanker freq")
        bad += 1
    pic_on_tanker = voice_intent.evaluate(
        "Bandsaw, Fleece 1, request picture",
        channel="bandsaw",
        phase="flight",
        expected="radio_check",
        callsign=CALLSIGN,
        steps=[bj_step, tanker_step],
        current_step_id="tanker_opt",
        tuned_channel="bandsaw",
        cursor_channel="tanker",
    )
    if not pic_on_tanker.fired or pic_on_tanker.match.intent != "request_picture":
        print(
            f"  FAIL Bandsaw picture must fire off tanker cursor: {pic_on_tanker.describe()}"
        )
        bad += 1
    else:
        print("bandsaw picture — fires while cursor is on optional tanker")

    bj_far = agencies.blackjack_exit_handoff(nellis, 37.4, -114.5, "approach")
    if bj_far == "approach":
        print(f"  FAIL far-out BJ exit must not skip Control: {bj_far}")
        bad += 1
    else:
        print(f"blackjack exit outside Approach — {bj_far}")

    st_75 = {
        "approach_plan": {
            "vfr_recovery": "ARCOE",
            "vfr_recovery_say": "Arcoe",
            "fix_lat": 36.737683,
            "fix_lon": -114.917067,
        },
        "ownship_ll": [37.99, -114.92],
        "last_tx_at": 0.0,
    }
    ready_75, wait_75 = atc_phrase.approach_clearance_auto_ready(
        airport=nellis, state=st_75, gap_s=0
    )
    if ready_75:
        print(f"  FAIL Approach must not auto-clear at ~75 NM: {wait_75}")
        bad += 1
    else:
        print("approach clearance — waits outside 40 NM of the IAF")

    land_bare = voice_intent.evaluate(
        "Gear down, stop 21R",
        channel="tower",
        phase="approach",
        expected="clear_land",
        callsign=CALLSIGN,
        runways=RUNWAYS,
        require_address=True,
    )
    land_cs = voice_intent.evaluate(
        "Fleece 1, gear down, full stop",
        channel="tower",
        phase="approach",
        expected="clear_land",
        callsign=CALLSIGN,
        runways=RUNWAYS,
        require_address=True,
    )
    if not land_bare.fired or land_bare.match.intent != "request_landing":
        print(f"  FAIL OHB gear-down without Tower should land: {land_bare.describe()}")
        bad += 1
    elif not land_cs.fired or land_cs.match.intent != "request_landing":
        print(f"  FAIL callsign + gear down should land: {land_cs.describe()}")
        bad += 1
    else:
        print("overhead land — gear down / full stop without addressing Tower")

    bye = tanker_mod.build_tanker_depart_reply("Fleece 1", {"callsign": "TEXACO 5"})
    if "texaco" not in bye.lower():
        print(f"  FAIL tanker depart goodbye: {bye}")
        bad += 1
    else:
        print(f"tanker depart goodbye — {bye}")

    import ops as ops_mod
    import agencies as agencies_mod
    from datetime import date, datetime, timezone

    if ops_mod.ato_day_letters(date(2026, 1, 1)) != "AA":
        print(f"  FAIL ATO AA is January 1: {ops_mod.ato_day_letters(date(2026, 1, 1))}")
        bad += 1
    elif ops_mod.ato_day_letters(date(2026, 1, 2)) != "AB":
        print(f"  FAIL ATO AB is January 2: {ops_mod.ato_day_letters(date(2026, 1, 2))}")
        bad += 1
    elif ops_mod.words_id_for(date(2026, 1, 1), 1) != "AA01":
        print(f"  FAIL WORDS AA01: {ops_mod.words_id_for(date(2026, 1, 1), 1)}")
        bad += 1
    elif "alpha" not in ops_mod.speak_words_id("AA01").casefold():
        print(f"  FAIL spoken WORDS: {ops_mod.speak_words_id('AA01')}")
        bad += 1
    elif "-" in ops_mod.speak_words_id("AA01"):
        print(f"  FAIL WORDS digits must be spaced, not hyphenated: {ops_mod.speak_words_id('AA01')}")
        bad += 1
    elif "juliet" not in ops_mod.speak_words_id("JP02").casefold():
        print(f"  FAIL spoken WORDS JP02: {ops_mod.speak_words_id('JP02')}")
        bad += 1
    else:
        print(f"ATO WORDS — AA/AB, {ops_mod.speak_words_id('AA01')}")

    opus_row = {
        "id": 3,
        "identifier": "JP02",
        "ato_letters": "JP",
        "revision": 2,
        "body_text": (
            "WX\nNTTR WEATHER.\nNO SIGNIFICANT WEATHER.\n\n"
            "RANGE\n61B IS HOT FOR SCHEDULED VUL.\n\n"
            "TWC\nTHREAT WARNING CONDITION WHITE."
        ),
    }
    live = ops_mod.bulletin_from_payload(
        opus_row,
        when=datetime(2026, 9, 7, tzinfo=timezone.utc),
        source="opus",
    )
    if live.id != "JP02" or live.update != 2 or live.ato_day != "JP" or live.source != "opus":
        print(f"  FAIL Opus WORDS map: {live}")
        bad += 1
    elif "NTTR WEATHER" not in " ".join(live.items) or "61B" not in live.range_status:
        print(f"  FAIL Opus WORDS body: items={live.items} range={live.range_status!r}")
        bad += 1
    else:
        print("Opus WORDS payload — JP02 from identifier + body_text")

    fake_payload = {"current": opus_row}

    def _fake_words_get(url: str, _ua: str) -> dict:
        if "/opus/words" not in url:
            raise AssertionError(url)
        return fake_payload

    ops_mod._WORDS_CACHE.update({"key": "", "exp": 0.0, "current": None})
    orig_get = atc_phrase.http_get_json
    atc_phrase.http_get_json = _fake_words_get  # type: ignore[assignment]
    try:
        fetched = ops_mod.current_words(
            {
                "ops_words_provider": "opus",
                "opus_backend_url": "https://example.test/backend",
                "opus_theater_id": 1,
            },
            when=datetime(2026, 9, 7, tzinfo=timezone.utc),
        )
    finally:
        atc_phrase.http_get_json = orig_get
        ops_mod._WORDS_CACHE.update({"key": "", "exp": 0.0, "current": None})
    spoken_live = ops_mod.build_words_reply("FLEECE 1", live)
    spoken_low = spoken_live.casefold()
    if fetched.id != "JP02" or fetched.source != "opus":
        print(f"  FAIL live WORDS fetch: {fetched}")
        bad += 1
    elif "juliet papa" not in spoken_low or "current" not in spoken_low:
        print(f"  FAIL OPS WORDS id phrase: {spoken_live}")
        bad += 1
    elif any(bit in spoken_low for bit in ("nttr weather", "61b", "threat warning")):
        print(f"  FAIL OPS must not read bulletin body: {spoken_live}")
        bad += 1
    else:
        print(f"Opus WORDS fetch — {fetched.id} {fetched.source}; radio {spoken_live}")

    fallback = ops_mod.current_words(
        {
            "ops_words_provider": "opus",
            "ops_words_id": "AA01",
            "ops_words_items": ["NTTR hot. Recoveries as published."],
        },
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    if fallback.id != "AA01" or fallback.source != "mock":
        print(f"  FAIL WORDS fallback without backend: {fallback}")
        bad += 1
    else:
        print("Opus WORDS fallback — mock when backend unset")

    if abs(ops_mod.decimal_hours(1 * 3600 + 42 * 60) - 1.7) > 0.05:
        print(f"  FAIL 1h42m should be 1.7: {ops_mod.decimal_hours(6120)}")
        bad += 1
    else:
        print("OPS decimal hours — 1h42m ~= 1.7")

    codes = ops_mod.parse_aircraft_codes(
        "Knight Ops, Snake 5. Snake 5-1 Code 1, Snake 5-2 Code 1, "
        "Snake 5-3 Code 2, Snake 5-4 Code 1.",
        flight_callsign="Snake 5",
    )
    got_codes = [(c.seat, c.code) for c in codes]
    if got_codes != [(1, 1), (2, 1), (3, 2), (4, 1)]:
        print(f"  FAIL OPS code parse: {got_codes}")
        bad += 1
    else:
        print("OPS code parse — 1/1/2/1")

    st: dict = {}
    t0 = datetime(2026, 1, 1, 21, 25, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 23, 7, tzinfo=timezone.utc)
    first = ops_mod.approve_start(st, callsign="FLEECE 1", when=t0)
    second = ops_mod.approve_start(st, callsign="FLEECE 1", when=t1)
    if first.start_utc != second.start_utc or first.start_hhmm != "2125":
        print(f"  FAIL second start must keep first timer: {first} / {second}")
        bad += 1
    else:
        end = ops_mod.record_status(st, codes, callsign="FLEECE 1", when=t1)
        if end.total_hours != 1.7:
            print(f"  FAIL total time 1.7 from 21:25–23:07: {end.total_hours}")
            bad += 1
        else:
            print(f"OPS timer — start {first.start_hhmm} total {end.total_hours}")
        atc_phrase.clear_flight_session_cache(state=st, reset_runway=False, invalidate_lookups=False)
        if ops_mod.sortie_from_state(st) is not None:
            print("  FAIL Reset must clear OPS start timer")
            bad += 1
        else:
            fresh = ops_mod.build_words_reply(
                "FLEECE 1",
                ops_mod.WordsBulletin(id="AA01", ato_day="AA", update=1),
                start=True,
                when=t0,
                already_started=bool(ops_mod.sortie_from_state(st) and ops_mod.sortie_from_state(st).start_utc),
            )
            if "start approved" not in fresh.casefold():
                print(f"  FAIL Reset WORDS must start-approve again: {fresh}")
                bad += 1
            else:
                print("OPS reset — start timer cleared, Start approved again")

    if agencies.resolve(
        tuned_channel="ops",
        cursor_channel="delivery",
        mission_phase="departure",
    ) != "ops":
        print("  FAIL backup radio on OPS must score as OPS during departure")
        bad += 1
    else:
        print("OPS backup radio — default agency on the ramp")

    wool = atc_phrase.resolve_ops_callsign(nellis, squadron_name="8th FS Wool")
    knight = atc_phrase.resolve_ops_callsign(nellis, squadron_name="561st FS")
    toro = atc_phrase.resolve_ops_callsign(nellis, squadron_name="469th FS")
    none = atc_phrase.resolve_ops_callsign(nellis)
    if wool != "Wool Ops" or knight != "Knight Ops" or toro != "Toro Ops":
        print(f"  FAIL squadron OPS names: wool={wool} knight={knight} toro={toro}")
        bad += 1
    elif none != "Ops":
        print(f"  FAIL unknown squadron must be Ops, not Nellis: {none}")
        bad += 1
    else:
        print("OPS squadron names — Wool / Knight / Toro")

    zulu = ops_mod.speak_time_now(t0)
    if "zulu" not in zulu.casefold():
        print(f"  FAIL OPS time must say Zulu: {zulu}")
        bad += 1
    else:
        print(f"OPS time now — {zulu}")

    nttr_win = atc_phrase.apply_windows_radio_pronunciations("NTTR hot")
    if "nitter" not in nttr_win.casefold():
        print(f"  FAIL NTTR should speak Nih-tter: {nttr_win}")
        bad += 1
    else:
        print("NTTR pronunciation — nitter")

    if voice_intent.step_offers_c2(None, channel="ops"):
        print("  FAIL OPS must not offer picture / dope / declare")
        bad += 1

    class _OpsEng:
        def __init__(self) -> None:
            self.config = {
                "dry_run": True,
                "freq_gate_enabled": False,
                "ops_words_provider": "mock",
                "ops_words_id": "AA01",
            }
            self.mission = {"steps": []}
            self.steps = []
            self.state = {"index": 0}
            self.airports = atc_phrase.load_json(atc_phrase.AIRPORTS_PATH)

        def airport(self) -> dict:
            return self.airports["nellis"]

        def current_step(self) -> dict | None:
            return None

        def save_state(self) -> None:
            return None

    words_ev = voice_intent.evaluate(
        "Knight Ops, Fleece 1, request current WORDS",
        channel="ops",
        phase="departure",
        callsign=CALLSIGN,
    )
    if not words_ev.fired or words_ev.match.intent != "ops_request_words":
        print(f"  FAIL WORDS intent: {words_ev.describe()}")
        bad += 1
    else:
        orig_opus = atc_phrase.resolve_opus_and_metar
        atc_phrase.resolve_opus_and_metar = lambda *_a, **_k: (
            atc_phrase.synthetic_flight_context("FLEECE 1"),
            atc_phrase.Weather(210, 8, 29.92, "KLSV 010000Z 21008KT 10SM FEW100 20/05 A2992"),
        )
        import voice_engine as _ve

        try:
            played = _ve.execute_intent(words_ev.match, _OpsEng())
        finally:
            atc_phrase.resolve_opus_and_metar = orig_opus
        body = str(played.get("text") or "")
        low = body.casefold()
        if played.get("channel") != "ops":
            print(f"  FAIL WORDS TX channel: {played}")
            bad += 1
        elif "words" not in low or "start approved" not in low:
            print(f"  FAIL WORDS+start phrase: {body}")
            bad += 1
        elif "alpha alpha" not in low:
            print(f"  FAIL spoken WORDS id: {body}")
            bad += 1
        elif "zulu" not in low:
            print(f"  FAIL WORDS time must say Zulu: {body}")
            bad += 1
        elif "nellis ops" in low:
            print(f"  FAIL OPS must not say Nellis Ops: {body}")
            bad += 1
        elif not low.rstrip(".").endswith("start approved"):
            print(f"  FAIL initial OPS must end with Start approved: {body}")
            bad += 1
        else:
            print(f"OPS WORDS + start — {body}")

        chirp_voice = "en-US-Chirp3-HD-Aoede"
        chirp = atc_phrase.prepare_radio_tts_chirp_text(body, voice=chirp_voice)
        mid_periods = chirp.rstrip(".").count(".")
        if mid_periods:
            print(f"  FAIL Chirp OPS must not mid-sentence period-pause: {chirp!r}")
            bad += 1
        elif "zero-one" in chirp or "fleece\u00a0one" in chirp:
            print(f"  FAIL Chirp OPS digit glue / hyphen: {chirp!r}")
            bad += 1
        elif "fleece one" not in chirp or "start approved" not in chirp:
            print(f"  FAIL Chirp OPS phrasing: {chirp!r}")
            bad += 1
        else:
            print(f"Chirp OPS phrasing — {chirp}")

    ops_tips = voice_intent.suggestions(
        phase="departure",
        channel="ops",
        expected="clearance",
        callsign=CALLSIGN,
        airport_name="Nellis",
        steps=[
            {
                "id": "del_clearance",
                "channel": "delivery",
                "phase": "departure",
                "template": "clearance",
                "voice_phrases": ["request clearance"],
            }
        ],
        current_step_id="del_clearance",
        tuned_channel="ops",
        next_channel="delivery",
        next_freq_mhz=289.4,
        limit=5,
        advance_limit=2,
        optional_limit=3,
    )
    ops_says = [str(s).casefold() for s, _d, r, *_ in ops_tips]
    ops_adv = [str(s).casefold() for s, _d, r, *_ in ops_tips if r == "advance"]
    if not ops_adv or "request current words" not in ops_adv[0]:
        print(f"  FAIL OPS Fly tips must lead with WORDS, not Delivery: {ops_tips}")
        bad += 1
    elif any("tune" in s and "delivery" in s for s in ops_says):
        print(f"  FAIL OPS before start must not tip Delivery: {ops_tips}")
        bad += 1
    elif any("request clearance" in s or "runway" in s for s in ops_says):
        print(f"  FAIL OPS Fly tips must not show Delivery/runway: {ops_tips}")
        bad += 1
    else:
        print(f"OPS Fly tips — {ops_says}")

    stale_pending = voice_intent.suggestions(
        phase="departure",
        channel="ops",
        expected="clearance",
        callsign=CALLSIGN,
        airport_name="Nellis",
        pending_contact="delivery",
        ops_start_done=False,
        tuned_channel="ops",
        next_channel="delivery",
        next_freq_mhz=289.4,
        limit=5,
        advance_limit=2,
        optional_limit=3,
    )
    stale_adv = [
        str(s).casefold() for s, _d, r, *_ in stale_pending if r == "advance"
    ]
    if not stale_adv or "request current words" not in stale_adv[0]:
        print(
            f"  FAIL leftover Delivery pending must not steal OPS cues: {stale_pending}"
        )
        bad += 1
    else:
        print("OPS Fly tips — leftover Delivery pending ignored until start")

    after_start_tips = voice_intent.suggestions(
        phase="departure",
        channel="ops",
        expected="clearance",
        callsign=CALLSIGN,
        airport_name="Nellis",
        steps=[
            {
                "id": "del_clearance",
                "channel": "delivery",
                "phase": "departure",
                "template": "clearance",
                "voice_phrases": ["request clearance"],
            }
        ],
        current_step_id="del_clearance",
        limit=5,
        advance_limit=2,
        optional_limit=3,
        pending_contact="delivery",
        ops_start_done=True,
    )
    after_says = [str(s).casefold() for s, _d, r, *_ in after_start_tips]
    after_adv = [
        str(s).casefold()
        for s, _d, r, *_ in after_start_tips
        if r == "advance"
    ]
    if not after_adv or "tune" not in after_adv[0] or "delivery" not in after_adv[0]:
        print(f"  FAIL after OPS start tip must lead with tune Delivery: {after_start_tips}")
        bad += 1
    elif any("request clearance" in s for s in after_says):
        print(f"  FAIL still on OPS — do not tip Delivery call yet: {after_start_tips}")
        bad += 1
    else:
        print(f"OPS after start — {after_adv[0]}")

    leaked = voice_intent.suggestions(
        phase="departure",
        channel="ops",
        expected="clearance",
        callsign=CALLSIGN,
        tanker_chat_session=True,
        tanker_chat_choices=[{"id": "a", "say": "how's it going"}],
        limit=5,
        advance_limit=2,
        optional_limit=3,
    )
    leaked_says = " ".join(str(s).casefold() for s, *_ in leaked)
    if any(bit in leaked_says for bit in ("how's it going", "hows it going", "request rejoin", "talk later", "say anything")):
        print(f"  FAIL OPS Fly tips must not show tanker cues: {leaked}")
        bad += 1
    else:
        print("OPS Fly tips — no tanker leak after WORDS")

    cue_ops = voice_intent.cue_channel(
        mission_phase="departure",
        cursor_channel="delivery",
        tuned_channel="ops",
    )
    cue_after = voice_intent.cue_channel(
        mission_phase="departure",
        cursor_channel="delivery",
        tuned_channel=None,
        last_tx_channel="ops",
    )
    cue_handoff = voice_intent.cue_channel(
        mission_phase="departure",
        cursor_channel="delivery",
        tuned_channel=None,
        last_tx_channel="ops",
        pending_contact="delivery",
        ops_start_done=True,
    )
    if cue_ops != "ops" or cue_after != "ops":
        print(f"  FAIL OPS cue channel: tuned={cue_ops!r} after={cue_after!r}")
        bad += 1
    elif cue_handoff != "delivery":
        print(f"  FAIL after start + pending Delivery cue: {cue_handoff!r}")
        bad += 1
    else:
        print("OPS cue channel — tune stays Ops; after start tips Delivery")

    handoff_st: dict[str, object] = {}
    agencies_mod.note_tx(handoff_st, "ops", "ops_words")
    if agencies_mod.pending_contact(handoff_st) != "delivery":
        print(f"  FAIL ops_words must pending Delivery: {handoff_st}")
        bad += 1
    else:
        print("OPS note_tx — pending Delivery after WORDS/start")

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
    if bare.fired:
        print(f"  FAIL authored phrase still needs the agency: {bare.describe()}")
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

    leftover = [
        {
            "id": "dep_custom",
            "label": "Custom airborne",
            "channel": "departure",
            "phase": "departure",
            "template": "radar_contact",
            "text": "Nellis Departure, Fleece 1, picture.",
            "voice_phrases": ["picture"],
        }
    ]
    leftover_cues = voice_intent.suggestions(
        phase="departure",
        channel="departure",
        expected="radar_contact",
        steps=leftover,
        current_step_id="dep_custom",
        limit=5,
        advance_limit=2,
        optional_limit=3,
    )
    leftover_adv = [
        str(s).lower()
        for s, _d, role, *_rest in leftover_cues
        if role == "advance"
    ]
    if "with you" in leftover_adv:
        print(f"  FAIL custom step must not inherit WITH YOU: {leftover_cues}")
        bad += 1
    elif "picture" not in leftover_adv:
        print(f"  FAIL custom step cue should be the authored phrase: {leftover_cues}")
        bad += 1
    else:
        print("authored custom cues — leftover template hidden")

    empty_custom = [
        {
            "id": "dep_empty",
            "label": "Custom empty",
            "channel": "departure",
            "phase": "departure",
            "template": "radar_contact",
            "text": "Nellis Departure, Fleece 1, checking in.",
        }
    ]
    empty_cues = voice_intent.suggestions(
        phase="departure",
        channel="departure",
        expected="radar_contact",
        steps=empty_custom,
        current_step_id="dep_empty",
        limit=5,
        advance_limit=2,
        optional_limit=3,
    )
    empty_adv = [
        str(s).lower()
        for s, _d, role, *_rest in empty_cues
        if role == "advance"
    ]
    if "with you" in empty_adv:
        print(f"  FAIL empty custom step must not show WITH YOU: {empty_cues}")
        bad += 1
    else:
        print("empty custom cues — no leftover WITH YOU")

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


def custom_agency_behavior() -> int:
    """Center / Joshua on channel other must not inherit Bandsaw C2 or hold."""
    bad = 0
    center = {
        "id": "la_center",
        "label": "LA Center check-in",
        "channel": "other",
        "phase": "flight",
        "template": "bandsaw_check_in",
        "mode": "file",
        "file": "center.mp3",
    }
    if voice_intent.step_holds_after_play(center):
        print("  FAIL authored leftover Bandsaw template must not hold after play")
        bad += 1
    if voice_intent.step_offers_c2(center):
        print("  FAIL Other/Center must not offer C2 unless c2=true")
        bad += 1
    if not voice_intent.step_holds_after_play({**center, "hold": True}):
        print("  FAIL hold=true must keep the cursor after play")
        bad += 1
    live_bs = {
        "id": "bs",
        "channel": "bandsaw",
        "template": "bandsaw_check_in",
        "mode": "tts",
    }
    live_joshua = {
        "id": "joshua_checkin",
        "channel": "joshua",
        "template": "joshua_check_in",
        "mode": "tts",
        "c2": False,
    }
    if not voice_intent.step_holds_after_play(live_joshua):
        print("  FAIL live Joshua check-in must hold")
        bad += 1
    if voice_intent.step_offers_c2(live_joshua, channel="joshua"):
        print("  FAIL Joshua must not offer C2 unless c2=true")
        bad += 1
    if voice_intent.extract_channel("joshua control fleece 1 checking in") != "joshua":
        print("  FAIL Joshua Control must address joshua, not Center")
        bad += 1

    if not voice_intent.step_holds_after_play(live_bs):
        print("  FAIL live Bandsaw check-in must still hold")
        bad += 1
    if not voice_intent.step_offers_c2(live_bs, channel="bandsaw"):
        print("  FAIL Bandsaw must still offer C2")
        bad += 1
    tanker_opt = {
        "id": "aar",
        "channel": "tanker",
        "template": "radio_check",
        "c2": False,
    }
    if not voice_intent.step_offers_c2(tanker_opt, channel="bandsaw"):
        print("  FAIL live Bandsaw C2 must ignore tanker c2:false")
        bad += 1

    dope = voice_intent.evaluate(
        "Center, Fleece 1, bogey dope",
        channel="other",
        phase="flight",
        expected="bandsaw_check_in",
        callsign=CALLSIGN,
        steps=[center],
        current_step_id="la_center",
    )
    if dope.fired:
        print(f"  FAIL Center must not answer bogey dope: {dope.describe()}")
        bad += 1

    cues = voice_intent.suggestions(
        phase="flight",
        channel="other",
        expected="bandsaw_check_in",
        steps=[center],
        current_step_id="la_center",
        limit=8,
        advance_limit=2,
        optional_limit=6,
    )
    says = " ".join(str(s).lower() for s, *_ in cues)
    if "bogey" in says or "picture" in says or "declare" in says:
        print(f"  FAIL Center cues must not list C2 calls: {cues}")
        bad += 1

    c2_step = {**center, "c2": True}
    dope_on = voice_intent.evaluate(
        "Center, Fleece 1, bogey dope",
        channel="other",
        phase="flight",
        callsign=CALLSIGN,
        steps=[c2_step],
        current_step_id="la_center",
    )
    if not dope_on.fired or dope_on.match.intent != "request_bogey_dope":
        print(f"  FAIL c2=true Other step should answer bogey dope: {dope_on.describe()}")
        bad += 1

    if bad:
        print(f"custom agency behavior — {bad} problem(s)")
    else:
        print("custom agency behavior — Center is transit; C2/hold are opt-in")
    return bad


def instruction_readback_echo() -> int:
    """Repeating a handoff / instruction must not fire the next step."""
    bad = 0
    last = "Fleece one, Nellis Departure, contact Blackjack 377.8, good day."

    echo = voice_intent.evaluate(
        "Departure, Fleece 1, contact Blackjack 377.8",
        channel="departure",
        phase="departure",
        expected="departure_handoff",
        callsign=CALLSIGN,
        last_tx_text=last,
    )
    if echo.fired:
        print(f"  FAIL handoff readback must not fire: {echo.describe()}")
        bad += 1

    bj_last = (
        "Dagger one, Blackjack, radar contact, cleared tactical, "
        "frequency change approved, check out this frequency when range work complete."
    )
    bj_echo = voice_intent.evaluate(
        "Copy wheel, check out this frequency, dagger one",
        channel="blackjack",
        phase="flight",
        expected="bj_check_in",
        callsign=CALLSIGN,
        last_tx_text=bj_last,
        last_tx_channel="blackjack",
        last_tx_template="bj_check_in",
    )
    if bj_echo.fired:
        print(f"  FAIL Blackjack checkout readback must not TX tanker freq: {bj_echo.describe()}")
        bad += 1
    tanker_ask = voice_intent.evaluate(
        "Blackjack, Fleece 1, say tanker frequency",
        channel="blackjack",
        phase="flight",
        expected="bj_check_in",
        callsign=CALLSIGN,
        last_tx_text=bj_last,
        last_tx_channel="blackjack",
        last_tx_template="bj_check_in",
    )
    if not tanker_ask.fired or tanker_ask.match.intent != "tanker_freq":
        print(f"  FAIL say tanker frequency must still fire: {tanker_ask.describe()}")
        bad += 1

    checkin = voice_intent.evaluate(
        "Blackjack, Fleece 1, with you",
        channel="blackjack",
        phase="flight",
        expected="bj_check_in",
        callsign=CALLSIGN,
        last_tx_text=last,
    )
    if not checkin.fired or checkin.match.intent != "range_entry":
        print(f"  FAIL real check-in after handoff should fire: {checkin.describe()}")
        bad += 1

    tower_last = "Fleece one, Nellis Approach, contact tower, Local four, good day."
    tower_echo = voice_intent.evaluate(
        "Approach, Fleece 1, contact tower",
        channel="approach",
        phase="approach",
        expected="cleared_approach",
        callsign=CALLSIGN,
        last_tx_text=tower_last,
        last_tx_channel="approach",
    )
    if tower_echo.fired:
        print(f"  FAIL tower-handoff readback must not fire: {tower_echo.describe()}")
        bad += 1

    delivery_last = (
        "Fleece one, Nellis Delivery, readback correct, "
        "contact ground when ready for taxi."
    )
    taxi = voice_intent.evaluate(
        "Nellis Ground, Fleece 1, ready taxi",
        channel="ground",
        phase="departure",
        expected="taxi",
        callsign=CALLSIGN,
        last_tx_text=delivery_last,
        last_tx_channel="delivery",
    )
    if not taxi.fired or taxi.match.intent != "ready_taxi":
        print(
            f"  FAIL Ground ready-taxi after Delivery must fire, not echo: "
            f"{taxi.describe()}"
        )
        bad += 1

    taxi_no_ch = voice_intent.evaluate(
        "Nellis Ground, Fleece 1, ready taxi",
        channel="ground",
        phase="departure",
        expected="taxi",
        callsign=CALLSIGN,
        last_tx_text=delivery_last,
    )
    if not taxi_no_ch.fired or taxi_no_ch.match.intent != "ready_taxi":
        print(
            f"  FAIL ready taxi must not look like Delivery readback: "
            f"{taxi_no_ch.describe()}"
        )
        bad += 1

    taxi_last = (
        "Fleece one, Nellis Ground, runway two one right, "
        "taxi northwest EOR via Foxtrot Echo, Nellis altimeter two niner niner two."
    )
    eor = voice_intent.evaluate(
        "Nellis Ground, Fleece 1 is at the EOR",
        channel="ground",
        phase="departure",
        expected="monitor_tower",
        callsign=CALLSIGN,
        last_tx_text=taxi_last,
        last_tx_channel="ground",
        last_tx_template="taxi",
    )
    if not eor.fired or eor.match.intent != "at_eor":
        print(f"  FAIL at EOR after taxi must fire, not echo: {eor.describe()}")
        bad += 1

    eor_short = voice_intent.evaluate(
        "Nellis Ground, Fleece 1, at the EOR",
        channel="ground",
        phase="departure",
        expected="monitor_tower",
        callsign=CALLSIGN,
        last_tx_text=taxi_last,
        last_tx_channel="ground",
        last_tx_template="taxi",
    )
    if not eor_short.fired or eor_short.match.intent != "at_eor":
        print(f"  FAIL at the EOR after taxi must fire: {eor_short.describe()}")
        bad += 1

    luaw_last = "Fleece one, Nellis Tower, runway two one right, line up-and wait."
    in_pos = voice_intent.evaluate(
        "Tower, Fleece 1, in position",
        channel="tower",
        phase="departure",
        expected="clear_takeoff",
        callsign=CALLSIGN,
        last_tx_text=luaw_last,
        last_tx_channel="tower",
        last_tx_template="lineup",
    )
    if not in_pos.fired or in_pos.match.intent != "in_position":
        print(f"  FAIL in position after LUAW must fire, not echo: {in_pos.describe()}")
        bad += 1

    takeoff_last = (
        "Fleece one, Nellis Tower, runway two one right, cleared for takeoff, "
        "switch to departure."
    )
    airborne = voice_intent.evaluate(
        "Departure, Fleece 1, airborne",
        channel="departure",
        phase="departure",
        expected="radar_contact",
        callsign=CALLSIGN,
        last_tx_text=takeoff_last,
        last_tx_channel="tower",
        last_tx_template="clear_takeoff",
    )
    if not airborne.fired or airborne.match.intent != "departure_check_in":
        print(
            f"  FAIL airborne after takeoff must fire, not echo: "
            f"{airborne.describe()}"
        )
        bad += 1

    range_last = (
        "Fleece one, Blackjack, range exit approved, proceed direct Arcoe, "
        "contact Approach Local six, good day."
    )
    inbound = voice_intent.evaluate(
        "Approach, Fleece 1, checking in",
        channel="approach",
        phase="approach",
        expected="approach_check_in",
        callsign=CALLSIGN,
        last_tx_text=range_last,
        last_tx_channel="blackjack",
        last_tx_template="bj_range_exit",
    )
    if not inbound.fired or inbound.match.intent != "inbound_recovery":
        print(
            f"  FAIL Approach check-in after range exit must fire: "
            f"{inbound.describe()}"
        )
        bad += 1

    land_last = (
        "Fleece one, Nellis Tower, wind two one zero at fife, "
        "runway two one right, cleared to land, check gear down."
    )
    clear = voice_intent.evaluate(
        "Ground, Fleece 1, clear of the runway",
        channel="ground",
        phase="approach",
        expected="taxi_in",
        callsign=CALLSIGN,
        last_tx_text=land_last,
        last_tx_channel="tower",
        last_tx_template="clear_land",
    )
    if not clear.fired or clear.match.intent != "clear_of_runway":
        print(
            f"  FAIL clear of runway after land must fire: {clear.describe()}"
        )
        bad += 1

    if bad:
        print(f"instruction readback echo — {bad} problem(s)")
    else:
        print("instruction readback echo — next-step calls fire; handoff echoes stay silent")
    return bad


def agency_sandbox() -> int:
    """IFG core agencies answer from address + freq, not the Plan cursor."""
    bad = 0
    import agencies
    import atc_phrase
    import voice_engine

    if voice_intent.extract_channel("nellis control fleece 1 checking in") != "control_east":
        print("  FAIL Nellis Control must address control_east")
        bad += 1
    if voice_intent.extract_channel("sally fleece 1 checking in") != "control_east":
        print("  FAIL Sally must address control_east")
        bad += 1
    if voice_intent.extract_channel("lee fleece 1 checking in") != "control_west":
        print("  FAIL Lee must address control_west")
        bad += 1
    if voice_intent.extract_channel("control west fleece 1 checking in") != "control_west":
        print("  FAIL Control West must address control_west, not East")
        bad += 1
    if voice_intent.extract_channel("la center fleece 1 checking in") != "center":
        print("  FAIL LA Center must address center, not other")
        bad += 1
    if voice_intent.extract_channel("joshua control fleece 1 checking in") != "joshua":
        print("  FAIL Joshua Control must still address joshua")
        bad += 1

    josh_off = voice_intent.evaluate(
        "Joshua, Fleece 1, checking in",
        channel="blackjack",
        phase="flight",
        expected="bj_check_in",
        callsign=CALLSIGN,
    )
    if not josh_off.fired or josh_off.match.intent != "joshua_check_in":
        print(f"  FAIL Joshua check-in while expected Blackjack: {josh_off.describe()}")
        bad += 1

    if agencies.resolve(tuned_channel="control_west", addressed="control_east") != "control_west":
        print("  FAIL Control address remaps to the tuned East/West UHF")
        bad += 1
    if (
        agencies.resolve(
            tuned_channel="control_west",
            addressed="control_east",
            transcript="Sally Fleece 1 checking in",
        )
        != "control_east"
    ):
        print("  FAIL explicit Sally must not remap to tuned Lee")
        bad += 1

    nellis = atc_phrase.load_json(atc_phrase.AIRPORTS_PATH)["nellis"]
    if agencies.control_for_ll(nellis, 37.0, -114.8) != "control_east":
        print("  FAIL Desert MOA should be Control East")
        bad += 1
    if agencies.control_for_ll(nellis, 37.5, -116.5) != "control_west":
        print("  FAIL R-4807A should be Control West")
        bad += 1
    if agencies.control_for_ll(nellis, 36.23, -115.03) != "control_east":
        print("  FAIL field default should be Control East")
        bad += 1
    chk_e = atc_phrase.build_control_check_in("Fleece 1", channel="control_east")
    if "nellis control" not in chk_e.lower():
        print(f"  FAIL Control East check-in should speak Nellis Control: {chk_e}")
        bad += 1
    ready_far, wait_far = atc_phrase.control_handoff_auto_ready(
        airport=nellis,
        state={
            "approach_plan": {"vfr_recovery": "ARCOE", "vfr_recovery_say": "Arcoe"},
            "ownship_ll": [37.4, -114.5],
            "last_tx_at": 1.0,
            "control_checked_in": True,
            "last_agency": "control_east",
        },
        gap_s=0,
    )
    # 74 NM out should wait; inside the 42 NM handoff range should fire.
    ready_near, wait_near = atc_phrase.control_handoff_auto_ready(
        airport=nellis,
        state={
            "approach_plan": {"vfr_recovery": "ARCOE", "vfr_recovery_say": "Arcoe"},
            "ownship_ll": [36.85, -114.85],
            "last_tx_at": 1.0,
            "control_checked_in": True,
            "last_agency": "control_east",
        },
        gap_s=0,
    )
    ready_dep, wait_dep = atc_phrase.control_handoff_auto_ready(
        airport=nellis,
        state={
            "approach_plan": {
                "vfr_recovery": "TORYE",
                "vfr_recovery_say": "Torye",
                "fix_lat": 36.742033,
                "fix_lon": -114.607644,
            },
            "ownship_ll": [36.59347887826919, -114.72747802734376],
            "last_tx_at": 1.0,
            "control_checked_in": True,
            "last_agency": "departure",
        },
        gap_s=0,
    )
    if ready_far:
        print(f"  FAIL Control handoff should wait 74 NM out: {wait_far}")
        bad += 1
    elif not ready_near:
        print(f"  FAIL Control handoff should fire inside 42 NM: {wait_near}")
        bad += 1
    elif ready_dep:
        print(
            f"  FAIL Control handoff must not fire while still with Departure: {wait_dep}"
        )
        bad += 1
    else:
        print(
            "control handoff gate — far="
            + wait_far.replace("\u2264", "<=")
            + " near="
            + wait_near.replace("\u2264", "<=")
            + " dep="
            + wait_dep
        )
    # Client Watch often has last_tx_channel (synced) but not last_agency.
    ready_txch, wait_txch = atc_phrase.control_handoff_auto_ready(
        airport=nellis,
        state={
            "approach_plan": {"vfr_recovery": "ARCOE", "vfr_recovery_say": "Arcoe"},
            "ownship_ll": [36.85, -114.85],
            "last_tx_at": 1.0,
            "last_tx_channel": "control_east",
            "last_agency": "blackjack",
        },
        gap_s=0,
    )
    if not ready_txch:
        print(f"  FAIL Control handoff should trust last_tx_channel: {wait_txch}")
        bad += 1
    # Past ARCOE, already inbound to the field — distance to the fix grows again.
    ready_field, wait_field = atc_phrase.control_handoff_auto_ready(
        airport=nellis,
        state={
            "approach_plan": {"vfr_recovery": "ARCOE", "vfr_recovery_say": "Arcoe"},
            "ownship_ll": [36.30, -115.03],
            "last_tx_at": 1.0,
            "control_checked_in": True,
            "last_agency": "control_east",
        },
        gap_s=0,
    )
    if not ready_field:
        print(f"  FAIL Control handoff should fire near the field past the IAF: {wait_field}")
        bad += 1
    # Blackjack said "contact Nellis Control" and nobody has spoken on NATCF
    # yet — Approach must wait for the check-in the pilot actually makes.
    st_pending = {
        "approach_plan": {"vfr_recovery": "ARCOE", "vfr_recovery_say": "Arcoe"},
        "ownship_ll": [36.30, -115.03],
        "last_tx_at": 1.0,
        "last_agency": "blackjack",
        "last_tx_channel": "blackjack",
        "control_channel": "control_east",
        "pending_contact": "control_east",
        "control_checked_in": True,
    }
    ready_pending, wait_pending = atc_phrase.control_handoff_auto_ready(
        airport=nellis, state=st_pending, gap_s=0
    )
    if ready_pending:
        print(
            "  FAIL Control handoff must wait while 'contact Nellis Control' "
            f"is still pending: {wait_pending}"
        )
        bad += 1
    ctrl_step = {"channel": "control_east", "template": "control_check_in"}
    if atc_phrase.should_skip_control_step(ctrl_step, st_pending):
        print("  FAIL cursor must not skip the NATCF check-in while it is pending")
        bad += 1
    # Missing fix or position used to mean "fire now", which dumped the pilot
    # on Approach ninety miles out the instant he checked in with Control.
    st_blind = {
        "approach_plan": {"iaf": "KRYSS"},  # named, but no coordinates
        "ownship_ll": [37.7305, -114.6835],  # ~91 NM out
        "ownship_ll_t": __import__("time").time(),
        "last_tx_at": 1.0,
        "control_checked_in": True,
        "last_agency": "control_east",
    }
    ready_nofix, wait_nofix = atc_phrase.control_handoff_auto_ready(
        airport=nellis, state=st_blind, gap_s=0
    )
    st_nopos = {
        "approach_plan": {"vfr_recovery": "ARCOE", "vfr_recovery_say": "Arcoe"},
        "last_tx_at": 1.0,
        "control_checked_in": True,
        "last_agency": "control_east",
    }
    ready_nopos, wait_nopos = atc_phrase.control_handoff_auto_ready(
        airport=nellis, state=st_nopos, gap_s=0, config={}
    )
    ready_nofix_near, _wait_nfn = atc_phrase.control_handoff_auto_ready(
        airport=nellis,
        state={**st_blind, "ownship_ll": [36.30, -115.03]},
        gap_s=0,
    )
    if ready_nofix:
        print(f"  FAIL no exit fix must gate on the field, not fire at 91 NM: {wait_nofix}")
        bad += 1
    elif ready_nopos:
        print(f"  FAIL Control handoff must not fire with no position: {wait_nopos}")
        bad += 1
    elif not ready_nofix_near:
        print("  FAIL no exit fix should still hand off near the field")
        bad += 1
    else:
        print("control handoff gate — no fix / no position waits, field still fires")
    # NATCF lets go in the 40-45 NM band. The 18 NM further out belongs to the
    # Departure → Blackjack handoff and must not creep back in here.
    fld = atc_phrase._airport_field_latlon(nellis)
    band = []
    for want_nm in (50.0, 44.0, 41.0, 30.0):
        # Due north of the field at the requested range.
        lat = fld[0] + want_nm / 60.0
        st_band = {
            "approach_plan": {"vfr_recovery": "ARCOE", "vfr_recovery_say": "Arcoe"},
            "ownship_ll": [lat, fld[1]],
            "last_tx_at": 1.0,
            "control_checked_in": True,
            "last_agency": "control_east",
        }
        rdy, _w = atc_phrase.control_handoff_auto_ready(
            airport=nellis, state=st_band, gap_s=0
        )
        band.append((want_nm, rdy))
    if [r for _n, r in band] != [False, False, True, True]:
        print(f"  FAIL Control handoff should open in the 40-45 NM band: {band}")
        bad += 1
    elif abs(atc_phrase.control_handoff_nm({}) - 42.0) > 0.01:
        print("  FAIL default Control handoff range should be 42 NM")
        bad += 1
    elif abs(atc_phrase.control_handoff_nm({"control_handoff_nm": 45}) - 45.0) > 0.01:
        print("  FAIL control_handoff_nm should be configurable")
        bad += 1
    else:
        import json as _json
        from pathlib import Path as _Path

        _flow = _json.loads(
            (_Path(__file__).resolve().parent / "flows" / "nellis_default.json").read_text(
                encoding="utf-8"
            )
        )
        _rows = _flow.get("steps") or (
            (_flow.get("outbound") or []) + (_flow.get("inbound") or [])
        )
        dep_nm = next(
            (
                (s.get("trigger") or {}).get("within_nm")
                for s in _rows
                if str(s.get("template") or "") == "departure_handoff"
            ),
            None,
        )
        if dep_nm != 18:
            print(f"  FAIL Departure → Blackjack should still be 18 NM, got {dep_nm}")
            bad += 1
        else:
            print("handoff ranges — NATCF at 42 NM, Departure → Blackjack at 18 NM")
    # Fly must tip the call that is due. It used to hide the check-in and show
    # "contact Approach" while the pilot was still trying to check in.
    def _advance_cue(expected: str) -> str:
        for say, _does, kind, _ok in voice_intent.suggestions(
            channel="control_east",
            phase="flight",
            expected=expected,
            callsign="Fleece 1",
        ):
            if kind == "advance":
                return str(say)
        return ""

    cue_in = _advance_cue("control_check_in")
    cue_off = _advance_cue("control_handoff")
    if "check" not in cue_in.lower():
        print(f"  FAIL Control check-in step should tip the check-in, got {cue_in!r}")
        bad += 1
    elif "approach" not in cue_off.lower():
        print(f"  FAIL after check-in Fly should tip the handoff, got {cue_off!r}")
        bad += 1
    else:
        print(f"control cues — due={cue_in!r} then {cue_off!r}")
    for said in (
        "Nellis Control, Fleece 1, with you",
        "Nellis Control, Fleece 1, checking in",
        "NATCF, Fleece 1, with you",
        "Control, Fleece 1, with you",
    ):
        ev_ci = voice_intent.evaluate(
            said,
            channel="control_east",
            phase="flight",
            callsign="Fleece 1",
            expected="control_check_in",
            tuned_channel="control_east",
        )
        if (ev_ci.match.intent if ev_ci.match else None) != "control_check_in":
            print(f"  FAIL {said!r} should check in with Control: {ev_ci.reason}")
            bad += 1
    # The Fly hero card reads the live tanker UHF, not the airports.json
    # 'other' placeholder it falls back to when there is no tanker entry.
    tkr_step = {"id": "tkr", "channel": "tanker", "template": "radio_check"}
    tkr_freq, _tm, _tn = atc_phrase.step_radio(
        nellis,
        "tanker",
        tkr_step,
        state={"tanker_callsign": "TEXACO 1", "tanker_freq_mhz": 295.4},
        config={},
    )
    placeholder, _pm, _pn = atc_phrase.channel_radio(nellis, "tanker")
    if abs(float(tkr_freq) - 295.4) > 0.0005:
        print(f"  FAIL tanker step freq should be the assigned UHF, got {tkr_freq}")
        bad += 1
    elif abs(float(placeholder) - float(tkr_freq)) < 0.0005:
        print("  FAIL tanker test is vacuous — placeholder already matches")
        bad += 1
    else:
        print(f"tanker radio — assigned {tkr_freq:.3f}, not the {placeholder:.3f} fallback")
    # Fly / Plan previews render the *upcoming* phrase against live state.
    # Rendering must not claim the check-in went out on the radio.
    wx_ctrl = atc_phrase.Weather(210, 8, 29.92, "")
    st_preview = {"control_channel": "control_east"}
    atc_phrase.build_flow_step_phrase(
        nellis,
        "control_east",
        "control_check_in",
        "Fleece 1",
        wx_ctrl,
        "21L",
        state=st_preview,
        commit=False,
    )
    if st_preview.get("control_checked_in"):
        print("  FAIL phrase preview must not commit the NATCF check-in")
        bad += 1
    st_tx = {"control_channel": "control_east"}
    atc_phrase.build_flow_step_phrase(
        nellis,
        "control_east",
        "control_check_in",
        "Fleece 1",
        wx_ctrl,
        "21L",
        state=st_tx,
    )
    if not st_tx.get("control_checked_in"):
        print("  FAIL a real NATCF check-in TX must record the check-in")
        bad += 1
    st_bs = {}
    agencies.note_tx(st_bs, "blackjack", "contact_bandsaw")
    if agencies.pending_contact(st_bs) != "bandsaw":
        print(f"  FAIL contact Bandsaw should set pending_contact: {st_bs}")
        bad += 1
    agencies.note_tx(st_bs, "bandsaw", "bandsaw_check_in")
    if agencies.pending_contact(st_bs):
        print(f"  FAIL Bandsaw check-in should clear pending_contact: {st_bs}")
        bad += 1
    miles = atc_phrase.speak_field_miles(22.4)
    if miles != "twenty two miles":
        print(f"  FAIL Approach DME speech: {miles}")
        bad += 1
    wx_dme = atc_phrase.Weather(210, 8, 29.92, "KLSV 010000Z 21008KT 10SM FEW100 20/05 A2992")
    plan_dme = atc_phrase.assign_approach_plan(nellis, wx_dme, state={}, force=True)
    phrase_dme = atc_phrase.build_approach_recovery(
        nellis,
        "Fleece 1",
        wx_dme,
        str(plan_dme.get("runway") or "21R"),
        plan=plan_dme,
        distance_nm=22.4,
    )
    if "twenty two miles" not in phrase_dme.lower():
        print(f"  FAIL Approach check-in should include DME: {phrase_dme}")
        bad += 1
    elif "radar contact" in phrase_dme.lower():
        print(f"  FAIL Approach check-in still must not say radar contact: {phrase_dme}")
        bad += 1
    chk_w = atc_phrase.build_control_check_in("Fleece 1", channel="control_west")
    if "nellis control" not in chk_w.lower():
        print(f"  FAIL Control West check-in should speak Nellis Control: {chk_w}")
        bad += 1
    lee_exit = atc_phrase.build_blackjack_range_exit(
        nellis, "Fleece 1", handoff_channel="control_west"
    )
    if "nellis control" not in lee_exit.lower() or "eight" not in lee_exit.lower():
        print(f"  FAIL Blackjack → Control West (Local 8) wording: {lee_exit}")
        bad += 1
    if agencies.resolve(
        tuned_channel="joshua",
        cursor_channel="blackjack",
        mission_phase="flight",
    ) != "joshua":
        print("  FAIL Fly tips follow Joshua when that UHF is tuned")
        bad += 1
    if agencies.resolve(
        tuned_channel="tower",
        cursor_channel="ground",
        mission_phase="departure",
    ) != "ground":
        print("  FAIL field tips stay on the cursor, not a jumped Tower radio")
        bad += 1

    if not agencies.too_early_field("ground", "tower"):
        print("  FAIL Tower while still with Ground is too early")
        bad += 1
    if agencies.too_early_field("tower", "ground"):
        print("  FAIL calling Ground from Tower is not a skip-ahead")
        bad += 1
    redir = agencies.build_field_redirect(
        {"name": "Nellis"}, "Fleece 1", "tower", "ground"
    )
    if "contact" not in redir.lower() or "ground" not in redir.lower():
        print(f"  FAIL field redirect should send them to Ground: {redir}")
        bad += 1
    elif "tower" not in redir.lower():
        print(f"  FAIL field redirect should speak as Tower: {redir}")
        bad += 1
    else:
        print(f"field redirect — {redir}")

    class _FieldEng:
        def __init__(self) -> None:
            self.config = {"dry_run": True, "freq_gate_enabled": False}
            self.mission = {
                "steps": [
                    {
                        "id": "gnd_taxi",
                        "channel": "ground",
                        "template": "taxi",
                        "phase": "departure",
                        "enabled": True,
                    },
                    {
                        "id": "twr_lineup",
                        "channel": "tower",
                        "template": "lineup",
                        "phase": "departure",
                        "enabled": True,
                    },
                ]
            }
            self.steps = self.mission["steps"]
            self.state = {
                "index": 0,
                "contact_phase": "field",
                "last_agency": "ground",
            }
            self.played_id = None
            self.airports = atc_phrase.load_json(atc_phrase.AIRPORTS_PATH)

        def airport(self) -> dict:
            return self.airports["nellis"]

        def current_step(self) -> dict:
            return self.steps[int(self.state.get("index") or 0)]

        def save_state(self) -> None:
            return None

        def play_id(self, step_id: str, **_k: object) -> dict:
            self.played_id = step_id
            return {"text": "should not grant lineup", "channel": "tower"}

    eng = _FieldEng()
    match = voice_intent.evaluate(
        "Nellis Tower, Fleece 1, ready for departure",
        channel="tower",
        phase="departure",
        expected="taxi",
        callsign=CALLSIGN,
    )
    if not match.fired or match.match.intent != "ready_departure":
        print(f"  FAIL Tower-too-early call must still parse: {match.describe()}")
        bad += 1
    else:
        orig_opus = atc_phrase.resolve_opus_and_metar
        atc_phrase.resolve_opus_and_metar = lambda *_a, **_k: (
            atc_phrase.synthetic_flight_context("FLEECE 1"),
            atc_phrase.Weather(210, 8, 29.92, "KLSV 010000Z 21008KT 10SM FEW100 20/05 A2992"),
        )
        try:
            played = voice_engine.execute_intent(match.match, eng)
        finally:
            atc_phrase.resolve_opus_and_metar = orig_opus
        text = str(played.get("text") or "").lower()
        if eng.played_id is not None:
            print(f"  FAIL Tower-too-early must not play lineup: {eng.played_id}")
            bad += 1
        elif not played.get("field_redirect"):
            print(f"  FAIL Tower-too-early should redirect, not clear: {played}")
            bad += 1
        elif "contact" not in text or "ground" not in text:
            print(f"  FAIL Tower-too-early redirect wording: {played}")
            bad += 1
        else:
            print(f"tower too early — {played.get('text')}")

    if not voice_intent.step_holds_after_play(
        {"id": "ce", "channel": "control_east", "template": "control_check_in", "mode": "tts"}
    ):
        print("  FAIL Control check-in must hold")
        bad += 1

    local = agencies.infer_flight(
        route="KLSV MINTT DREAM ARCOE KLSV", dep_icao="KLSV", arr_icao="KLSV"
    )
    if not local.uses("blackjack") or not local.uses("control"):
        print(f"  FAIL local NTTR hop needs Blackjack + Control: {local.areas}")
        bad += 1
    elif local.uses("joshua") or local.uses("center"):
        print(f"  FAIL local NTTR hop should not add Joshua/Center: {local.areas}")
        bad += 1
    r2508 = agencies.infer_flight(
        route="KLSV BTY DAG EDW", dep_icao="KLSV", arr_icao="KEDW"
    )
    if not r2508.uses("joshua") or not r2508.uses("control") or not r2508.uses("center"):
        print(f"  FAIL R-2508 hop needs Control + Center + Joshua: {r2508.areas}")
        bad += 1
    elif r2508.uses("blackjack"):
        print(f"  FAIL Nellis→Edwards should not invent Blackjack: {r2508.areas}")
        bad += 1
    enroute = agencies.infer_flight(
        route="KLSV STRYK BTY BAM MLF DTA PUC HVE MTU FFU OGD KLSV",
        dep_icao="KLSV",
        arr_icao="KLSV",
    )
    if not enroute.uses("center") or not enroute.uses("control"):
        print(f"  FAIL enroute hop needs Center + Control: {enroute.areas}")
        bad += 1
    ranges = agencies.infer_flight(
        airspace_names=["CALA", "R63A"], airport={"icao": "KLSV"}
    )
    if not ranges.uses("blackjack") or not ranges.uses("control"):
        print(f"  FAIL reserved Desert/R-61 airspace needs Blackjack + Control: {ranges.areas}")
        bad += 1
    owens = agencies.infer_flight(airspace_names=["OWENS"], airport={"icao": "KLSV"})
    if not owens.uses("joshua"):
        print(f"  FAIL Owens airspace needs Joshua: {owens.areas}")
        bad += 1
    if agencies.handoff_from_plan("departure", local) != "blackjack":
        print("  FAIL local Departure should still hand to Blackjack")
        bad += 1
    dep_r2508 = agencies.handoff_from_plan("departure", r2508)
    if dep_r2508 == "joshua":
        print("  FAIL R-2508 Departure must not skip transit to Joshua")
        bad += 1
    elif dep_r2508 != "control_east":
        print(f"  FAIL R-2508 Departure should hand to Nellis Control: {dep_r2508}")
        bad += 1
    if agencies.handoff_from_plan("blackjack", local) != "control_east":
        print("  FAIL local Blackjack should hand to Nellis Control")
        bad += 1
    ctrl_gap = agencies.handoff_from_plan("control_east", r2508)
    if ctrl_gap != "center":
        print(f"  FAIL Control still in the NTTR should hand to Center, not Joshua: {ctrl_gap}")
        bad += 1
    edwards_ll = (36.0, -117.7)
    if not agencies.approaching_joshua(nellis, edwards_ll[0], edwards_ll[1]):
        print("  FAIL Edwards should count as approaching R-2508")
        bad += 1
    if agencies.approaching_joshua(nellis, 36.23, -115.03):
        print("  FAIL Nellis field must not count as approaching R-2508")
        bad += 1
    if agencies.owning_agency_for_ll(nellis, 36.23, -115.03) == "joshua":
        print("  FAIL Nellis field must not be owned by Joshua")
        bad += 1
    if agencies.owning_agency_for_ll(nellis, edwards_ll[0], edwards_ll[1]) != "joshua":
        print("  FAIL Edwards should be owned by Joshua")
        bad += 1
    if agencies.control_for_ll(nellis, 37.5, -116.5) != "control_west":
        print("  FAIL R-4807A should be NATCF West")
        bad += 1
    if agencies.owning_agency_for_ll(nellis, 37.5, -116.5) not in (
        "blackjack",
        "control_west",
    ):
        print(
            f"  FAIL R-4807A owner should be Blackjack or NATCF West: "
            f"{agencies.owning_agency_for_ll(nellis, 37.5, -116.5)}"
        )
        bad += 1
    if agencies.owning_agency_for_ll(nellis, 37.0, -114.8) not in (
        "control_east",
        "blackjack",
    ):
        print(
            f"  FAIL Desert MOA should be NATCF East or Blackjack: "
            f"{agencies.owning_agency_for_ll(nellis, 37.0, -114.8)}"
        )
        bad += 1
    josh_step = {"channel": "joshua", "template": "joshua_check_in"}
    if not atc_phrase.should_skip_joshua_step(
        josh_step,
        airport=nellis,
        state={"ownship_ll": [36.23, -115.03], "ownship_ll_t": __import__("time").time()},
    ):
        print("  FAIL Joshua step should skip over Nellis / NTTR")
        bad += 1
    if atc_phrase.should_skip_joshua_step(
        josh_step,
        airport=nellis,
        state={
            "ownship_ll": [edwards_ll[0], edwards_ll[1]],
            "ownship_ll_t": __import__("time").time(),
        },
    ):
        print("  FAIL Joshua step must stay live over Edwards")
        bad += 1
    if atc_phrase.should_skip_joshua_step(
        josh_step,
        airport=nellis,
        state={"pending_contact": "joshua"},
    ):
        print("  FAIL Joshua step must stay live after contact Joshua")
        bad += 1
    if agencies.fly_label("control_west") != "Nellis Control West":
        print(f"  FAIL Fly label West: {agencies.fly_label('control_west')}")
        bad += 1
    josh_now = agencies.handoff_from_plan(
        "center", r2508, airport=nellis, lat=edwards_ll[0], lon=edwards_ll[1]
    )
    if josh_now != "joshua":
        print(f"  FAIL Center approaching R-2508 should hand to Joshua: {josh_now}")
        bad += 1
    home = agencies.infer_flight(
        route="KEDW BTY ARCOE KLSV", dep_icao="KEDW", arr_icao="KLSV"
    )
    if agencies.handoff_from_plan("joshua", home) not in ("center", "control_east"):
        print(f"  FAIL Joshua recovering to Nellis should hand to Center/Control: {agencies.handoff_from_plan('joshua', home)}")
        bad += 1
    sandbox_cfg = {"flow_file": "nellis_default.json"}
    dep_2508 = atc_phrase.resolve_handoff_channel(
        from_channel="departure",
        default="blackjack",
        config=sandbox_cfg,
        opus=type(
            "O",
            (),
            {
                "fp_route_string": "KLSV BTY DAG EDW",
                "dep_icao": "KLSV",
                "arr_icao": "KEDW",
                "flight_id": 0,
            },
        )(),
        airport=nellis,
    )
    if dep_2508 == "joshua":
        print(f"  FAIL sandbox Departure on an Edwards hop must not name Joshua: {dep_2508}")
        bad += 1
    elif dep_2508 != "control_east":
        print(f"  FAIL sandbox Departure on an Edwards hop should name Control: {dep_2508}")
        bad += 1
    else:
        print(f"flight-plan agencies — local {agencies.format_hop(local)}")
        print(f"flight-plan agencies — R-2508 {agencies.format_hop(r2508)}")
        print(f"flight-plan agencies — enroute {agencies.format_hop(enroute)}")

    timeline = [
        {
            "id": "bj",
            "channel": "blackjack",
            "template": "bj_check_in",
            "phase": "flight",
            "label": "Blackjack check-in / alpha",
            "enabled": True,
        },
        {
            "id": "bs",
            "channel": "bandsaw",
            "template": "bandsaw_check_in",
            "phase": "flight",
            "label": "Bandsaw check-in (optional sandbox)",
            "enabled": True,
        },
        {
            "id": "bso",
            "channel": "bandsaw",
            "template": "bandsaw_check_out",
            "phase": "flight",
            "label": "Bandsaw check-out",
            "enabled": True,
        },
    ]
    bs_while_bj = agencies.display_step_for_agency(
        timeline, "bandsaw", cursor_index=0, last_tx_template="bj_check_in"
    )
    if not bs_while_bj or str(bs_while_bj.get("id") or "") != "bs":
        print(
            f"  FAIL tuned Bandsaw while Blackjack holds must show Bandsaw check-in: "
            f"{bs_while_bj}"
        )
        bad += 1
    bj_hold = agencies.display_step_for_agency(
        timeline, "blackjack", cursor_index=0, last_tx_template="bj_check_in"
    )
    if not bj_hold or str(bj_hold.get("id") or "") != "bj":
        print(f"  FAIL Blackjack hold must still show Blackjack check-in: {bj_hold}")
        bad += 1
    bs_after = agencies.display_step_for_agency(
        timeline, "bandsaw", cursor_index=0, last_tx_template="bandsaw_check_in"
    )
    if not bs_after or str(bs_after.get("id") or "") != "bso":
        print(f"  FAIL after Bandsaw check-in Fly should show checkout: {bs_after}")
        bad += 1
    else:
        print("agency display step — Bandsaw while Blackjack holds")

    if bad:
        print(f"agency sandbox — {bad} problem(s)")
    else:
        print("agency sandbox — Control / Joshua / Center / field redirect")
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
    failures += custom_agency_behavior()
    failures += instruction_readback_echo()
    failures += agency_sandbox()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
