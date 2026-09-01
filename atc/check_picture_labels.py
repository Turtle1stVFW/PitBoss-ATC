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

    print(f"\n{bad} failure(s)" if bad else "\nall picture label checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
