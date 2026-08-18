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

    print(f"\n{bad} failure(s)" if bad else "\nall picture label checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
