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
    ("Blackjack, Fleece 1, bogey dope", "blackjack", "flight", True, "request_bogey_dope"),
    ("Blackjack, Fleece 1, declare", "blackjack", "flight", True, "request_declare"),
    ("Blackjack, Fleece 1, alpha check bullseye", "blackjack", "flight", True, "request_alpha_check"),
    # Blackjack check-in — callsign + mission colour; "checking in" is enough.
    ("Blackjack, Fleece 1, checking in", "blackjack", "flight", True, "range_entry"),
    ("Blackjack, Fleece 1, checking in, mission zero eight zero seven zero two", "blackjack", "flight", True, "range_entry"),
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
    # Approach / recovery
    ("Nellis Approach, Fleece 1, checking in", "approach", "approach", True, "inbound_recovery"),
    ("Approach, Fleece 1, with you", "approach", "approach", True, "inbound_recovery"),
    ("Approach, Fleece 1, inbound for recovery", "approach", "approach", True, "inbound_recovery"),
    ("Approach, Fleece 1, request ARCOE recovery", "approach", "approach", True, "request_approach"),
    ("Approach, Fleece 1, request tactical overhead", "approach", "approach", True, "request_approach"),
    ("Approach, Fleece 1, request instrument", "approach", "approach", True, "request_approach"),
    ("Approach, Fleece 1, request hold", "approach", "approach", True, "request_hold"),
    ("Approach, Fleece 1, cancel hold", "approach", "approach", True, "cancel_hold"),
    ("Approach, Fleece 1, request vectors", "approach", "approach", True, "request_vectors"),
    ("Approach, Fleece 1, airport in sight, request tower", "approach", "approach", True, "approach_continue"),
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
    else:
        print(f"blackjack range exit — {bj_exit}")

    phrase_vmc = atc_phrase.build_approach_recovery(
        nellis, "Fleece 1", vmc, str(plan_vmc.get("runway") or "21R"), plan=plan_vmc
    )
    need_bits = (
        "landing south",
        "expect arcoe recovery for the overhead",
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
    elif "cross arcoe at or above one fife thousand" not in clear_ifr.lower():
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
    if not any("wind" in say.lower() for say, _ in tips_app):
        print(f"  FAIL approach kneeboard should tip winds: {tips_app}")
        bad += 1
    else:
        print(f"approach kneeboard winds tip — {[s for s, _ in tips_app]}")

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
