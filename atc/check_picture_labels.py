"""
Synthetic AFTTP 3-2.8 picture-label checks (no CAOC / mic).

Run: py -3 check_picture_labels.py
"""

from __future__ import annotations

import picture_labels as pl
import voice_actions
import voice_intent


def _g(
    dist: float,
    brg_from_own: float,
    *,
    heading: float | None = 90.0,
    feet: int = 25000,
    count: int = 1,
    bearing: int = 270,
    range_nm: int = 40,
) -> pl.FightGroup:
    g = pl.FightGroup(
        bearing=bearing,
        range_nm=range_nm,
        bullseye_name="ELVIS",
        distance_nm=float(dist),
        count=count,
        feet=feet,
        feet_list=[feet],
        heading_deg=heading,
        declaration="hostile",
    )
    g._brg_from_own = float(brg_from_own)  # type: ignore[attr-defined]
    return g


def main() -> int:
    bad = 0

    def check(title: str, got: str, expect_sub: str) -> None:
        nonlocal bad
        ok = expect_sub in got.casefold()
        print(f"{'OK' if ok else 'FAIL'} {title}: {got}")
        if not ok:
            bad += 1

    # Two groups same range, split azimuth → AZIMUTH
    c = pl.classify_picture([_g(40, 250), _g(41, 290)])
    check("azimuth label", c.head, "azimuth")
    check("azimuth names", " ".join(g.name for g in c.groups), "group")

    # Two groups lead/trail → RANGE
    c = pl.classify_picture([_g(30, 270), _g(60, 270)])
    check("range label", c.head, "range")
    names = [g.name for g in c.groups]
    check("range lead", names[0], "lead")
    check("range trail", names[1], "trail")

    # Three shallow wall
    c = pl.classify_picture([_g(50, 240), _g(51, 270), _g(52, 300)])
    check("wall label", c.head, "wall")

    # Three ladder (same bearing, spaced in range)
    c = pl.classify_picture([_g(30, 270), _g(50, 270), _g(70, 271)])
    check("ladder label", c.head, "ladder")

    # Champagne: two near + one trail
    c = pl.classify_picture([_g(40, 250), _g(42, 290), _g(70, 270)])
    check("champagne label", c.head, "champagne")

    # Vic: one near + two trail
    c = pl.classify_picture([_g(35, 270), _g(65, 250), _g(66, 290)])
    check("vic label", c.head, "vic")

    # Single
    c = pl.classify_picture([_g(40, 270)])
    check("single", c.kind, "single")

    # STACK speech
    stack = pl.speak_stack([35000, 8000])
    check("stack", stack, "stack")

    # AT BULLSEYE
    loc = pl.speak_bullseye_location("ELVIS", 225, 3)
    check("at bullseye", loc.casefold(), "at")

    # Aspect hot (target nose toward fighter)
    # Fighter at 0,0-ish: target south of fighter heading north → hot
    asp = pl.aspect_to_fighter(
        own_lat=36.0,
        own_lon=-115.0,
        tgt_lat=35.5,
        tgt_lon=-115.0,
        tgt_heading=0.0,
    )
    check("aspect hot", asp or "", "hot")

    # DECLARE cue — name always theater ELVIS; bullseye / bare digits / mishears OK
    cases = [
        ("ANSA FLEECE 1. Declare group, Elvis 05667-21000.", 56, 67, 21000),
        ("bandsaw fleece 1 declare bullseye 056 67 21000", 56, 67, 21000),
        ("bandsaw fleece 1 declare 056 67 21000", 56, 67, 21000),
        ("bandsaw fleece 1 declare group 05667 21000", 56, 67, 21000),
        ("bandsaw fleece 1 declare ellis 056 67", 56, 67, None),
        ("bandsaw fleece 1 declare alvis 05667", 56, 67, None),
        ("blackjack fleece 1 declare 056 67 28k", 56, 67, 28000),
        ("blackjack fleece 1 declare 056 67 28 k", 56, 67, 28000),
        ("blackjack fleece 1 declare 056 67 angels 28", 56, 67, 28000),
        ("blackjack fleece 1 declare 056 67 twenty eight thousand", 56, 67, 28000),
        ("Declare group Bullseye 020 15 Twenty eight thousand", 20, 15, 28000),
        ("Declare group Elvis 020 15 Twenty eight thousand", 20, 15, 28000),
        ("Declare group Bullseye 020 15 Angels 28", 20, 15, 28000),
        ("Declare group Elvis 020 15 angels twenty eight", 20, 15, 28000),
        ("Declare group Bullseye 020 15 twenty eight", 20, 15, 28000),
        ("Bandsaw, Fleece 1, declare Elvis one four four, sixty eight", 144, 68, None),
        ("Bandsaw, declare Elvis two niner fife, one hundred fourteen", 295, 114, None),
    ]
    for raw, brg, rng, alt in cases:
        cue = voice_actions.parse_declare_cue(raw)
        ok = (
            cue is not None
            and cue["name"].casefold() == "elvis"
            and cue["bearing"] == brg
            and cue["range_nm"] == rng
            and cue.get("altitude_ft") == alt
        )
        print(f"{'OK' if ok else 'FAIL'} declare {raw!r} -> {cue}")
        if not ok:
            bad += 1

    # Bullseye polar match helper
    err = voice_actions._cue_bullseye_error_nm(56, 67, {"bearing": 56, "range_nm": 67})
    check("cue error zero", str(err), "0.0")
    err2 = voice_actions._cue_bullseye_error_nm(60, 67, {"bearing": 56, "range_nm": 67})
    if err2 <= 0 or err2 > 10:
        print(f"FAIL cue error lateral: {err2}")
        bad += 1
    else:
        print(f"OK cue error lateral: {err2:.1f} nm")

    cue_28k = {"bearing": 56, "range_nm": 67, "altitude_ft": 28000}
    hit = voice_actions.declare_cue_score(56, 67, 27500, cue_28k)
    miss_alt = voice_actions.declare_cue_score(56, 67, 12000, cue_28k)
    miss_pos = voice_actions.declare_cue_score(40, 20, 28000, cue_28k)
    if hit is None or hit > 3.0 or miss_alt is not None or miss_pos is not None:
        print(
            f"FAIL declare gates: hit={hit} miss_alt={miss_alt} miss_pos={miss_pos}"
        )
        bad += 1
    else:
        print(f"OK declare altitude + bullseye gates (hit {hit:.1f} nm)")

    friendly = _g(12.0, 250, heading=90, feet=15000, bearing=40, range_nm=20)
    friendly.declaration = "friendly"
    hostile = _g(6.0, 270, heading=90, feet=28000, bearing=56, range_nm=67)
    hostile.declaration = "hostile"
    picked = voice_actions.prefer_declare_group(
        [friendly, hostile], cue=cue_28k
    )
    if picked is None or picked.declaration != "hostile":
        print(f"FAIL prefer hostile over loose friendly: {picked}")
        bad += 1
    else:
        print("OK declare prefers hostile over a loose friendly")

    weak_friendly = _g(14.0, 250, heading=90, feet=15000, bearing=40, range_nm=20)
    weak_friendly.declaration = "friendly"
    none = voice_actions.prefer_declare_group([weak_friendly], cue=cue_28k)
    if none is not None:
        print(f"FAIL weak friendly-only cue should be unable: {none}")
        bad += 1
    else:
        print("OK declare says unable instead of guessing friendly")

    # Picture call comes from affiliation. Red coalition is not bandit/hostile.
    mem = pl.DeclarationMemory()
    mem.force(["u1"], "bandit", brg=56, rng=67, feet=21000)
    d1 = mem.assign(["u1"], brg=58, rng=70, feet=20500, coalition="red", hostile_side="red")
    d2 = mem.assign(["u1"], brg=60, rng=72, feet=20000, coalition="red", hostile_side="red")
    d3 = mem.assign(
        ["u1"],
        brg=60,
        rng=72,
        feet=20000,
        coalition="red",
        hostile_side="red",
        upgrade_hostile=True,
    )
    d_bandit = mem.assign(
        ["u1"],
        brg=61,
        rng=73,
        feet=19800,
        coalition="red",
        hostile_side="red",
        affiliation="BANDIT",
    )
    d_hostile = mem.assign(
        ["u1"],
        brg=61,
        rng=73,
        feet=19800,
        coalition="blue",
        hostile_side="red",
        affiliation="HOSTILE",
    )
    d_unk = mem.assign(
        ["u1"],
        brg=62,
        rng=74,
        feet=19000,
        coalition="red",
        hostile_side="red",
        affiliation="UNKNOWN",
    )
    other = mem.assign(["u2"], brg=200, rng=40, feet=15000, coalition="red", hostile_side="red")
    other2 = mem.assign(["u2"], brg=201, rng=41, feet=15100, coalition="red", hostile_side="red")
    friendly = mem.assign(["u3"], brg=10, rng=10, feet=10000, coalition="blue", hostile_side="red")
    bandsaw_up = pl.agency_can_upgrade_hostile("Bandsaw", "bandsaw")
    jack_no = pl.agency_can_upgrade_hostile("Blackjack", "blackjack")
    lead_up = pl.transcript_upgrades_hostile("Blackjack Fleece 1 declare hostile 056 67")
    lead_plain = pl.transcript_upgrades_hostile("Blackjack Fleece 1 declare 056 67")
    if (
        d1 != "bogey spades"
        or d2 != "bogey spades"
        or d3 != "bogey spades"
        or d_bandit != "bandit"
        or d_hostile != "hostile"
        or d_unk != "bogey spades"
        or other != "bogey spades"
        or other2 != "bogey spades"
        or friendly != "friendly"
        or not bandsaw_up
        or jack_no
        or not lead_up
        or lead_plain
    ):
        print(
            f"FAIL picture call: {d1=} {d2=} {d3=} {d_bandit=} {d_hostile=} "
            f"{d_unk=} {other=} {friendly=} bandsaw={bandsaw_up} jack={jack_no} "
            f"lead={lead_up}/{lead_plain}"
        )
        bad += 1
    else:
        print("OK affiliation is the picture call; red coalition stays unknown")

    state = {}
    pinned = pl.DeclarationMemory()
    pinned.assign(
        ["u1"],
        brg=62,
        rng=74,
        feet=19000,
        coalition="red",
        hostile_side="red",
        affiliation="HOSTILE",
    )
    pinned.to_state(state)
    again = pl.DeclarationMemory.from_state(state).assign(
        ["u1"],
        brg=62,
        rng=74,
        feet=19000,
        coalition="red",
        hostile_side="red",
        affiliation="HOSTILE",
    )
    dropped = pl.DeclarationMemory.from_state(state).assign(
        ["u1"], brg=62, rng=74, feet=19000, coalition="red", hostile_side="red"
    )
    if again != "hostile" or dropped != "bogey spades":
        print(f"FAIL declaration round-trip: {again=} {dropped=} state={state}")
        bad += 1
    else:
        print("OK HOSTILE sticks from affiliation; a missing token is unknown")

    neu = pl.DeclarationMemory()
    neu_first = neu.assign(
        ["civ1"], brg=40, rng=30, feet=32000, coalition="neutral", hostile_side="red"
    )
    neu_up = neu.assign(
        ["civ1"],
        brg=41,
        rng=31,
        feet=31800,
        coalition="neutral",
        hostile_side="red",
        upgrade_hostile=True,
    )
    civ_red = pl.DeclarationMemory().assign(
        ["airliner"],
        brg=80,
        rng=50,
        feet=35000,
        coalition="red",
        hostile_side="red",
        upgrade_hostile=True,
    )
    sticky_wrong = pl.DeclarationMemory()
    sticky_wrong.force(["civ2"], "hostile", brg=12, rng=20, feet=28000)
    repaired = sticky_wrong.assign(
        ["civ2"], brg=13, rng=21, feet=27800, coalition="neutral", hostile_side="red"
    )
    red_g = _g(6.0, 270, heading=90, feet=28000, bearing=56, range_nm=67)
    red_g.declaration = "bandit"
    red_g.coalition = "red"
    red_g.affiliation = "BANDIT"
    unk_red = _g(8.0, 270, heading=90, feet=28000, bearing=50, range_nm=40)
    unk_red.declaration = "bogey spades"
    unk_red.coalition = "red"
    unk_red.affiliation = "UNKNOWN"
    neu_g = _g(12.0, 250, heading=90, feet=32000, bearing=40, range_nm=20)
    neu_g.declaration = "bogey spades"
    neu_g.coalition = "neutral"
    neu_g.object = "Yak-40"
    bandsaw_red = pl.declare_may_upgrade_hostile(
        red_g, agency="Bandsaw", channel="bandsaw", hostile_side="red"
    )
    bandsaw_unk = pl.declare_may_upgrade_hostile(
        unk_red, agency="Bandsaw", channel="bandsaw", hostile_side="red"
    )
    bandsaw_neu = pl.declare_may_upgrade_hostile(
        neu_g, agency="Bandsaw", channel="bandsaw", hostile_side="red"
    )
    if (
        neu_first != "bogey spades"
        or neu_up != "bogey spades"
        or civ_red != "bogey spades"
        or repaired != "bogey spades"
        or not bandsaw_red
        or bandsaw_unk
        or bandsaw_neu
    ):
        print(
            f"FAIL civilian/neutral declare: {neu_first=} {neu_up=} {civ_red=} "
            f"{repaired=} bandsaw_red={bandsaw_red} bandsaw_unk={bandsaw_unk} "
            f"bandsaw_neu={bandsaw_neu}"
        )
        bad += 1
    else:
        print("OK declare: unknown stays bogey; red coalition is not hostile")

    import atc_phrase

    # Spoken BRAA is magnetic (true − 12°E on NTTR).
    if abs(atc_phrase.true_to_magnetic_deg(221.0) - 209.0) > 0.01:
        print(f"FAIL true 221 -> mag 209, got {atc_phrase.true_to_magnetic_deg(221.0)}")
        bad += 1
    else:
        print("OK true-to-magnetic 221 -> 209")
    own_lat, own_lon = 36.0, -115.0
    tgt_lat, tgt_lon = 36.0, -114.0
    true_brg = atc_phrase._true_bearing_deg(own_lat, own_lon, tgt_lat, tgt_lon)
    mag_brg = atc_phrase.magnetic_bearing_deg(own_lat, own_lon, tgt_lat, tgt_lon)
    braa_brg, _rng = pl.braa_from_own(own_lat, own_lon, tgt_lat, tgt_lon)
    expect_mag = (true_brg - 12.0) % 360.0
    if abs(mag_brg - expect_mag) > 0.01 or braa_brg != int(round(expect_mag)) % 360:
        print(
            f"FAIL BRAA magnetic: true={true_brg:.1f} mag={mag_brg:.1f} "
            f"spoken={braa_brg} expect={expect_mag:.1f}"
        )
        bad += 1
    else:
        print(f"OK spoken BRAA is magnetic ({braa_brg:03d}, true was {true_brg:.0f})")

    wreck = {
        "type": "air",
        "name": "Pilot",
        "coalition": "red",
        "objectName": "Parachutist",
    }
    live = {
        "type": "air",
        "name": "MiG-29",
        "coalition": "red",
        "flightLabel": "IVAN 11",
        "objectName": "MiG-29S",
    }
    flagged = {
        "type": "air",
        "name": "Bandit 2",
        "coalition": "red",
        "objectName": "Su-27",
        "alive": False,
    }
    if (
        not atc_phrase.caoc_unit_is_dead_or_wreck(wreck)
        or atc_phrase.caoc_unit_is_picture_eligible(wreck)
        or not atc_phrase.caoc_unit_is_picture_eligible(live)
        or atc_phrase.caoc_unit_is_picture_eligible(flagged)
    ):
        print(
            f"FAIL dead/wreck picture filter: wreck={atc_phrase.caoc_unit_is_dead_or_wreck(wreck)} "
            f"live={atc_phrase.caoc_unit_is_picture_eligible(live)} "
            f"flagged={atc_phrase.caoc_unit_is_picture_eligible(flagged)}"
        )
        bad += 1
    else:
        print("OK shot-down Pilot / wreck tracks are excluded from picture")

    f5 = {"objectName": "F-5E-3", "name": "Aggro 1", "coalition": "red"}
    e3 = {"objectName": "E-3A", "name": "Overlord", "coalition": "blue"}
    kc = {"objectName": "KC-135", "name": "Texaco", "coalition": "blue"}
    if (
        atc_phrase.caoc_unit_is_picture_fixture(f5)
        or not atc_phrase.caoc_unit_is_picture_fixture(e3)
        or not atc_phrase.caoc_unit_is_picture_fixture(kc)
    ):
        print(
            f"FAIL fixture token boundaries: "
            f"f5={atc_phrase.caoc_unit_is_picture_fixture(f5)} "
            f"e3={atc_phrase.caoc_unit_is_picture_fixture(e3)} "
            f"kc={atc_phrase.caoc_unit_is_picture_fixture(kc)}"
        )
        bad += 1
    else:
        print("OK F-5E-3 is pictured; E-3 / KC-135 stay fixtures")

    # Affiliation is the picture call. Coalition does not invent bandit.
    opus_unk = pl.DeclarationMemory().assign(
        ["ti1"],
        brg=56,
        rng=67,
        feet=24000,
        coalition="red",
        hostile_side="red",
        affiliation="UNKNOWN",
        ti_training=True,
    )
    opus_bandit = pl.DeclarationMemory().assign(
        ["red1"],
        brg=10,
        rng=20,
        feet=20000,
        coalition="red",
        hostile_side="red",
        affiliation="BANDIT",
    )
    opus_hostile = pl.DeclarationMemory().assign(
        ["red2"],
        brg=11,
        rng=21,
        feet=20100,
        coalition="red",
        hostile_side="red",
        affiliation="HOSTILE",
    )
    ti_bare = pl.DeclarationMemory().assign(
        ["ti2"],
        brg=80,
        rng=40,
        feet=18000,
        coalition="red",
        hostile_side="red",
        ti_training=True,
    )
    legacy_enemy = pl.DeclarationMemory().assign(
        ["legacy"],
        brg=90,
        rng=50,
        feet=22000,
        coalition="red",
        hostile_side="red",
    )
    blue_default = pl.DeclarationMemory().assign(
        ["blue1"],
        brg=12,
        rng=15,
        feet=18000,
        coalition="blue",
        hostile_side="red",
    )
    blue_unk = pl.DeclarationMemory().assign(
        ["blue2"],
        brg=13,
        rng=16,
        feet=18100,
        coalition="blue",
        hostile_side="red",
        affiliation="UNKNOWN",
    )
    named_hostile = pl.picture_call_declaration(
        affiliation="UNKNOWN",
        coalition="red",
        hostile_side="red",
    )
    gateway_words = (
        pl.normalize_opus_affiliation("interceptor") == "friendly"
        and pl.normalize_opus_affiliation("suspect") == "hostile"
        and pl.normalize_opus_affiliation("special interest") == "bogey spades"
        and pl.normalize_opus_affiliation("neutral land") == "neutral"
        and pl.normalize_opus_affiliation("assumed friendly") == "friendly"
    )
    if (
        opus_unk != "bogey spades"
        or opus_bandit != "bandit"
        or opus_hostile != "hostile"
        or ti_bare != "bogey spades"
        or legacy_enemy != "bogey spades"
        or blue_default != "friendly"
        or blue_unk != "bogey spades"
        or named_hostile != "bogey spades"
        or not gateway_words
    ):
        print(
            f"FAIL opus affiliation: {opus_unk=} {opus_bandit=} {opus_hostile=} "
            f"{ti_bare=} {legacy_enemy=} {blue_default=} {blue_unk=} "
            f"{named_hostile=} {gateway_words=}"
        )
        bad += 1
    else:
        print("OK affiliation: UNKNOWN/red bogey; blue friendly; BANDIT/HOSTILE as-is")

    unk_g = _g(20, 270, heading=90, feet=24000, bearing=56, range_nm=67)
    unk_g.declaration = "bogey spades"
    unk_g.affiliation = "UNKNOWN"
    unk_g.ti_training = True
    known = _g(25, 270, heading=90, feet=25000, bearing=40, range_nm=30)
    known.declaration = "hostile"
    known.affiliation = "HOSTILE"
    bandit_g = _g(30, 270, heading=90, feet=22000, bearing=10, range_nm=40)
    bandit_g.declaration = "bandit"
    bandit_g.affiliation = "BANDIT"
    if (
        not pl.needs_vid_cue(unk_g)
        or pl.needs_vid_cue(known)
        or pl.needs_commit_cue(unk_g)
        or not pl.needs_commit_cue(known)
        or pl.needs_commit_cue(bandit_g)
        or pl.needs_vid_cue(bandit_g)
        or pl.recommend_cue([unk_g]) != pl.VID_INTERCEPT_CUE
        or pl.recommend_cue([known]) != pl.COMMIT_CUE
        or pl.recommend_cue([bandit_g]) != ""
        or pl.recommend_cue([known, unk_g]) != pl.MIXED_COMMIT_VID_CUE
    ):
        print(
            f"FAIL recommend cue: vid={pl.needs_vid_cue(unk_g)} "
            f"commit={pl.needs_commit_cue(known)} "
            f"unk={pl.recommend_cue([unk_g])!r} hostile={pl.recommend_cue([known])!r} "
            f"bandit={pl.recommend_cue([bandit_g])!r}"
        )
        bad += 1
    else:
        print("OK unknown gets intercept for VID; hostile gets commit; bandit gets neither")

    ti_unit = {
        "affiliation": "UNKNOWN",
        "tiTraining": True,
        "name": "TI_TRAINING_1",
        "coalition": "red",
        "displayCallsign": "UNK",
    }
    friend_unit = {"affiliation": "FRIENDLY", "coalition": "red", "name": "FLEECE 1"}
    known_unit = {"affiliation": "HOSTILE", "coalition": "red", "name": "IVAN 11"}
    red_bare = {"type": "air", "coalition": "red", "name": "Hostile", "groupName": "Bandit 1"}
    blue_bare = {"type": "air", "coalition": "blue", "name": "FLEECE 1"}
    blue_bogey = {"type": "air", "coalition": "blue", "affiliation": "UNKNOWN", "name": "Bogey"}
    neutral_air = {"type": "air", "affiliation": "NEUTRAL", "coalition": "red", "name": "CIV"}
    unk_display = {"type": "air", "displayCallsign": "UNK", "name": "Bandit 1", "coalition": "blue"}
    dal = {
        "type": "air",
        "affiliation": "UNKNOWN",
        "coalition": "neutral",
        "name": "DAL 3698",
        "groupName": "DAL 3698",
        "objectName": "A_320",
    }
    aal = {
        "type": "air",
        "affiliation": "UNKNOWN",
        "coalition": "neutral",
        "name": "AAL 4595-1",
        "groupName": "AAL 4595",
    }
    janet = {
        "type": "air",
        "affiliation": "UNKNOWN",
        "coalition": "neutral",
        "name": "Janet 88",
        "objectName": "B_737",
    }
    cessna = {
        "type": "air",
        "affiliation": "UNKNOWN",
        "coalition": "neutral",
        "name": "N9572H #IFF:0166FR",
        "objectName": "Cessna_210N",
    }
    dal_hostile = dict(dal, affiliation="HOSTILE")
    magic = {
        "type": "air",
        "affiliation": "UNKNOWN",
        "coalition": "red",
        "name": "MAGIC #IFF:6611FR-1-1",
        "objectName": "E-3A",
    }
    ti_group = _g(20, 270, heading=90, feet=24000, bearing=56, range_nm=67)
    ti_group.declaration = "bogey spades"
    ti_group.ti_training = True
    ti_group.label = "UNK"
    ti_group.name = "single group"
    ti_said = pl.core_group_clause(ti_group, include_bullseye=False, include_track=False)
    if (
        not pl.picture_include_unit(ti_unit, "red")
        or pl.picture_include_unit(friend_unit, "red")
        or not pl.picture_include_unit(known_unit, "red")
        or not pl.picture_include_unit(red_bare, "red")
        or pl.picture_include_unit(blue_bare, "red")
        or not pl.picture_include_unit(blue_bogey, "red")
        or pl.picture_include_unit(neutral_air, "red")
        or pl.picture_include_unit(dal, "red")
        or pl.picture_include_unit(aal, "red")
        or pl.picture_include_unit(janet, "red")
        or pl.picture_include_unit(cessna, "red")
        or not pl.picture_include_unit(dal_hostile, "red")
        or not pl.picture_include_unit(magic, "red")
        or not pl.caoc_unit_is_ti_training(ti_unit)
        or not pl.caoc_unit_is_ti_training(unk_display)
        or "UNK" not in ti_said
        or "hostile" in ti_said.casefold()
        or "bandit" in ti_said.casefold()
    ):
        print(
            f"FAIL picture include / TI flag: said={ti_said!r} "
            f"unk_display={pl.caoc_unit_is_ti_training(unk_display)}"
        )
        bad += 1
    else:
        print("OK picture includes unknown/TI/HOSTILE, skips FRIENDLY and NEUTRAL")

    if voice_actions.parse_vid_affiliation("Bandsaw Fleece 1 VID hostile") != "hostile":
        print("FAIL parse VID hostile")
        bad += 1
    elif voice_actions.parse_vid_affiliation("declare as bandit") != "bandit":
        print("FAIL parse declare as bandit")
        bad += 1
    elif voice_actions.parse_vid_affiliation("group is friendly") != "friendly":
        print("FAIL parse group is friendly")
        bad += 1
    elif voice_actions.parse_vid_affiliation("upgrade group hostel") != "hostile":
        print("FAIL parse hostel as hostile")
        bad += 1
    elif (
        voice_actions.parse_vid_affiliation(
            "ID group elvis 020 21 21000 mig 23"
        )
        != "bandit"
    ):
        print("FAIL type-only ID should default to bandit")
        bad += 1
    elif voice_actions.parse_vid_affiliation("declare Elvis 056 67") is not None:
        print("FAIL declare Elvis should not parse an affiliation")
        bad += 1
    else:
        print("OK VID affiliation parse vs DECLARE query")

    id_cue = voice_actions.parse_declare_cue(
        "Bandsaw, RAZOR 1. ID group, Elvis 09017-19000. Bandit."
    )
    up_cue = voice_actions.parse_declare_cue(
        "Bandsaw, upgrade group, L. This. 357.005. 9000. Hostel."
    )
    if (
        not id_cue
        or id_cue.get("bearing") != 90
        or id_cue.get("range_nm") != 17
        or id_cue.get("altitude_ft") != 19000
    ):
        print(f"FAIL ID group packed cue: {id_cue}")
        bad += 1
    elif (
        not up_cue
        or up_cue.get("bearing") != 357
        or up_cue.get("range_nm") != 5
        or up_cue.get("altitude_ft") != 9000
    ):
        print(f"FAIL upgrade packed cue: {up_cue}")
        bad += 1
    else:
        print("OK ID/upgrade packed Elvis cues (skip seat digit)")

    if "visual i-d" not in pl.VID_INTERCEPT_CUE.casefold() and "i-d" not in pl.VID_INTERCEPT_CUE.casefold():
        print(f"FAIL VID cue wording: {pl.VID_INTERCEPT_CUE}")
        bad += 1
    else:
        print(f"OK recommend cue says visual I-D ({pl.VID_INTERCEPT_CUE})")

    # Last picture labels → ID by name.
    state = {}
    north = _g(40, 250, heading=90, feet=21000, bearing=20, range_nm=40)
    north.name = "north group"
    north.unit_ids = ["n1"]
    north.declaration = "bogey spades"
    south = _g(42, 290, heading=90, feet=22000, bearing=40, range_nm=42)
    south.name = "south group"
    south.unit_ids = ["s1"]
    south.declaration = "bogey spades"
    voice_actions.remember_picture_groups(state, [north, south])
    hit = voice_actions.match_picture_group_ref(
        "Bandsaw Razor 1 ID north group MiG 23", state
    )
    miss = voice_actions.match_picture_group_ref(
        "Bandsaw Razor 1 ID trail group MiG", state
    )
    if not hit or hit.get("ids") != ["n1"] or miss is not None:
        print(f"FAIL picture group name match: {hit=} {miss=}")
        bad += 1
    else:
        print("OK ID resolves north group from last picture")

    patched: list[tuple[str, str]] = []
    orig_patch = atc_phrase.patch_caoc_unit_affiliation

    def _capture_patch(config, unit_id, affiliation):
        patched.append((str(unit_id), str(affiliation)))
        return True

    atc_phrase.patch_caoc_unit_affiliation = _capture_patch  # type: ignore[method-assign]
    try:
        text, groups = voice_actions.build_vid_affiliation_reply(
            {},
            {"coalition": 2},
            "RAZOR 1",
            agency="Bandsaw",
            transcript="Bandsaw, Razor 1, ID north group, MiG 23.",
            state=state,
        )
    finally:
        atc_phrase.patch_caoc_unit_affiliation = orig_patch
    if (
        "bandit" not in text.casefold()
        or "north" not in text.casefold()
        or patched != [("n1", "BANDIT")]
        or not groups
    ):
        print(f"FAIL named-group VID reply: {text=} {patched=} {groups=}")
        bad += 1
    else:
        print(f"OK named-group VID defaults to bandit ({text})")

    no_key = atc_phrase.patch_caoc_unit_affiliation(
        {"opus_backend_url": "", "caoc_affiliation_key": ""}, "u9", "BANDIT"
    )
    captured: list[tuple[str, dict, dict | None]] = []

    def _fake_http_patch(url, user_agent, body, extra_headers=None):
        captured.append((url, body, extra_headers))
        return {}

    orig_http = atc_phrase.http_patch_json
    atc_phrase.http_patch_json = _fake_http_patch  # type: ignore[method-assign]
    try:
        ok = atc_phrase.patch_caoc_unit_affiliation(
            {
                "opus_backend_url": "https://opus.example/backend",
                "caoc_affiliation_key": "test-key",
                "user_agent": "test",
            },
            "unit-42",
            "BANDIT",
        )
    finally:
        atc_phrase.http_patch_json = orig_http
    if no_key or not ok or not captured:
        print(f"FAIL affiliation PATCH helper: {no_key=} {ok=} {captured=}")
        bad += 1
    else:
        url, body, headers = captured[0]
        if (
            "/opus/caoc/radar/units/" not in url
            or not str(url).endswith("/affiliation")
            or body.get("affiliation") != "BANDIT"
            or (headers or {}).get("X-Caoc-Affiliation-Key") != "test-key"
        ):
            print(f"FAIL PATCH payload: {url=} {body=} {headers=}")
            bad += 1
        else:
            print("OK affiliation PATCH helper (stubbed HTTP)")

    spoken = pl.spoken_to_opus_affiliation("bandit")
    if (
        spoken != "BANDIT"
        or pl.spoken_to_opus_affiliation("bogey spades") != "UNKNOWN"
        or pl.spoken_to_opus_affiliation("neutral") != "NEUTRAL"
    ):
        print(f"FAIL spoken_to_opus_affiliation: {spoken}")
        bad += 1
    else:
        print("OK spoken declaration maps to OPUS affiliation enum")

    missile = {
        "type": "air",
        "name": "AIM-120",
        "objectName": "AIM-120C",
        "coalition": "red",
        "affiliation": "HOSTILE",
    }
    flanker = {
        "type": "air",
        "name": "Ivan 11",
        "objectName": "Su-27",
        "coalition": "red",
        "affiliation": "UNKNOWN",
    }
    ground = {
        "type": "ground",
        "name": "SA-10",
        "objectName": "S-300PS",
        "coalition": "red",
        "affiliation": "HOSTILE",
    }
    air = atc_phrase.caoc_air_units([missile, flanker, ground])
    if (
        air != [flanker]
        or not atc_phrase.caoc_unit_is_weapon(missile)
        or atc_phrase.caoc_unit_is_picture_eligible(missile)
        or not atc_phrase.caoc_unit_is_picture_eligible(flanker)
    ):
        print(f"FAIL air-track filter: {air}")
        bad += 1
    else:
        print("OK air list keeps fighters and drops missiles and ground")

    # Live CAOC rows carry lat/lon and no xMeters. Picture must still see them,
    # including when the host only has the client's seat fix.
    nellis = atc_phrase.load_json(atc_phrase.AIRPORTS_PATH)["nellis"]
    radar = {
        "units": [
            {
                "id": "me",
                "type": "air",
                "name": "Turtle",
                "pilotName": "Turtle",
                "lat": 36.22,
                "lon": -115.04,
                "coalition": "blue",
                "affiliation": "FRIENDLY",
                "objectName": "F-16C_50",
                "altMeters": 3000,
                "headingDeg": 40,
            },
            {
                "id": "bogey",
                "type": "air",
                "name": "Ivan 11",
                "lat": 36.40,
                "lon": -115.20,
                "coalition": "red",
                "affiliation": "UNKNOWN",
                "objectName": "MiG-29S",
                "altMeters": 8000,
                "headingDeg": 180,
            },
        ]
    }
    orig_fetch = atc_phrase.fetch_caoc_radar
    orig_qnh = atc_phrase.metar_altimeter_inhg
    atc_phrase.fetch_caoc_radar = lambda *_a, **_k: radar  # type: ignore[method-assign]
    atc_phrase.metar_altimeter_inhg = lambda *_a, **_k: None  # type: ignore[method-assign]
    try:
        groups, _own, oll = voice_actions.collect_hostile_groups(
            {"opus_user_name": "Turtle"},
            nellis,
            callsign="SUBPAR 2",
        )
        seat_cfg = {
            "opus_user_name": "nobody",
            atc_phrase.OWNSHIP_SEAT_BOUND_KEY: True,
            atc_phrase.OWNSHIP_SEAT_LL_KEY: [36.22, -115.04],
        }
        seat_groups, _seat_own, seat_ll = voice_actions.collect_hostile_groups(
            seat_cfg, nellis, callsign="GHOST 1"
        )
    finally:
        atc_phrase.fetch_caoc_radar = orig_fetch  # type: ignore[method-assign]
        atc_phrase.metar_altimeter_inhg = orig_qnh  # type: ignore[method-assign]
    if oll is None or len(groups) != 1 or groups[0].declaration != "bogey spades":
        print(f"FAIL lat/lon picture: {oll=} groups={[(g.declaration, g.object) for g in groups]}")
        bad += 1
    elif seat_ll is None or len(seat_groups) != 1:
        print(f"FAIL seat fix picture: {seat_ll=} n={len(seat_groups)}")
        bad += 1
    else:
        print("OK picture reads lat/lon tracks from the fighter fix")

    print(f"\n{bad} failure(s)" if bad else "\nall picture label checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
