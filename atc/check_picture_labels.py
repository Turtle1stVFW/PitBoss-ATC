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

    # Sticky declarations: same group keeps its label; only Hostile upgrades.
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
    d4 = mem.assign(["u1"], brg=61, rng=73, feet=19800, coalition="red", hostile_side="red")
    other = mem.assign(["u2"], brg=200, rng=40, feet=15000, coalition="red", hostile_side="red")
    other2 = mem.assign(["u2"], brg=201, rng=41, feet=15100, coalition="red", hostile_side="red")
    friendly = mem.assign(["u3"], brg=10, rng=10, feet=10000, coalition="blue", hostile_side="red")
    bandsaw_up = pl.agency_can_upgrade_hostile("Bandsaw", "bandsaw")
    jack_no = pl.agency_can_upgrade_hostile("Blackjack", "blackjack")
    lead_up = pl.transcript_upgrades_hostile("Blackjack Fleece 1 declare hostile 056 67")
    lead_plain = pl.transcript_upgrades_hostile("Blackjack Fleece 1 declare 056 67")
    if (
        d1 != "bandit"
        or d2 != "bandit"
        or d3 != "hostile"
        or d4 != "hostile"
        or other != other2
        or friendly != "friendly"
        or not bandsaw_up
        or jack_no
        or not lead_up
        or lead_plain
    ):
        print(
            f"FAIL sticky decl: {d1=} {d2=} {d3=} {d4=} {other=} {other2=} "
            f"{friendly=} bandsaw={bandsaw_up} jack={jack_no} lead={lead_up}/{lead_plain}"
        )
        bad += 1
    else:
        print(f"OK sticky declarations bandit->hostile; other group {other}")

    state = {}
    mem.to_state(state)
    again = pl.DeclarationMemory.from_state(state).assign(
        ["u1"], brg=62, rng=74, feet=19000, coalition="red", hostile_side="red"
    )
    if again != "hostile":
        print(f"FAIL declaration persisted in state: {again} state={state}")
        bad += 1
    else:
        print("OK declaration memory survives state round-trip")

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
    neu_g = _g(12.0, 250, heading=90, feet=32000, bearing=40, range_nm=20)
    neu_g.declaration = "bogey spades"
    neu_g.coalition = "neutral"
    neu_g.object = "Yak-40"
    bandsaw_red = pl.declare_may_upgrade_hostile(
        red_g, agency="Bandsaw", channel="bandsaw", hostile_side="red"
    )
    bandsaw_neu = pl.declare_may_upgrade_hostile(
        neu_g, agency="Bandsaw", channel="bandsaw", hostile_side="red"
    )
    if (
        neu_first != "bogey spades"
        or neu_up != "bogey spades"
        or civ_red != "hostile"
        or repaired != "bogey spades"
        or not bandsaw_red
        or bandsaw_neu
    ):
        print(
            f"FAIL civilian/neutral declare: {neu_first=} {neu_up=} {civ_red=} "
            f"{repaired=} bandsaw_red={bandsaw_red} bandsaw_neu={bandsaw_neu}"
        )
        bad += 1
    else:
        print("OK declare: CAOC neutrals stay bogey spades; red can be hostile")

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

    # OPUS affiliation wins over coalition / random rolls.
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
    if (
        opus_unk != "bogey spades"
        or opus_bandit != "bandit"
        or opus_hostile != "hostile"
        or ti_bare != "bogey spades"
        or legacy_enemy != "bandit"
    ):
        print(
            f"FAIL opus affiliation: {opus_unk=} {opus_bandit=} {opus_hostile=} "
            f"{ti_bare=} {legacy_enemy=}"
        )
        bad += 1
    else:
        print("OK OPUS affiliation: UNKNOWN/TI bogey spades; BANDIT/HOSTILE as-is")

    unk_g = _g(20, 270, heading=90, feet=24000, bearing=56, range_nm=67)
    unk_g.declaration = "bogey spades"
    unk_g.affiliation = "UNKNOWN"
    unk_g.ti_training = True
    known = _g(25, 270, heading=90, feet=25000, bearing=40, range_nm=30)
    known.declaration = "hostile"
    known.affiliation = "HOSTILE"
    if not pl.needs_vid_cue(unk_g) or pl.needs_vid_cue(known):
        print(
            f"FAIL VID cue: unk={pl.needs_vid_cue(unk_g)} known={pl.needs_vid_cue(known)}"
        )
        bad += 1
    else:
        print("OK VID cue only on UNKNOWN / undeclared TI")

    ti_unit = {
        "affiliation": "UNKNOWN",
        "tiTraining": True,
        "name": "TI_TRAINING_1",
        "coalition": "red",
    }
    friend_unit = {"affiliation": "FRIENDLY", "coalition": "red", "name": "FLEECE 1"}
    known_unit = {"affiliation": "HOSTILE", "coalition": "red", "name": "IVAN 11"}
    if (
        not pl.picture_include_unit(ti_unit, "red")
        or pl.picture_include_unit(friend_unit, "red")
        or not pl.picture_include_unit(known_unit, "red")
        or not pl.caoc_unit_is_ti_training(ti_unit)
    ):
        print("FAIL picture include / TI flag")
        bad += 1
    else:
        print("OK picture includes TI/HOSTILE and skips FRIENDLY")

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
    if spoken != "BANDIT" or pl.spoken_to_opus_affiliation("bogey spades") != "UNKNOWN":
        print(f"FAIL spoken_to_opus_affiliation: {spoken}")
        bad += 1
    else:
        print("OK spoken declaration maps to OPUS affiliation enum")

    print(f"\n{bad} failure(s)" if bad else "\nall picture label checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
