"""Synthetic checks for runway_position geometry (no live feed needed).

Pass --live to dump the current CAOC match against the active flow step.
"""

from __future__ import annotations

import math
import sys

import atc_phrase
import runway_position as rp

AIRPORT = atc_phrase.load_json(atc_phrase.AIRPORTS_PATH)["nellis"]


def _ascii(text: str) -> str:
    return text.replace("\u2264", "<=").replace("\u00b7", "|")


def show(title: str) -> None:
    print(f"\n-- {title}")


def main() -> int:
    bad = 0
    frame = rp.RunwayFrame.build("03L", rp.runway_geometry(AIRPORT, "03L"))
    assert frame is not None, "03L geometry missing"
    show("frame")
    print(
        f"length {frame.length_m:.0f} m ({frame.length_m * rp.FT_PER_M:.0f} ft), "
        f"true heading {frame.heading_deg:.1f}°, width {frame.width_m:.0f} m"
    )
    if not 2900 <= frame.length_m <= 3300:
        print("  FAIL runway length is not ~10,100 ft")
        bad += 1
    if rp.angle_diff(frame.heading_deg, 41.2) > 1.0:
        print("  FAIL 03L should run about 041° true (030 magnetic)")
        bad += 1

    # Reverse direction must be the same strip, opposite heading.
    rev = rp.RunwayFrame.build("21R", rp.runway_geometry(AIRPORT, "21R"))
    assert rev is not None
    if rp.angle_diff(rev.heading_deg, frame.heading_deg + 180.0) > 1.0:
        print("  FAIL 21R should be the reciprocal of 03L")
        bad += 1
    if rp.angle_diff(rev.heading_deg, 221.2) > 2.0:
        print(f"  FAIL 21R should run about 221° true (210 magnetic), got {rev.heading_deg:.1f}")
        bad += 1
    # Drawn backwards (threshold at the far end) must still face 21, not 03.
    swapped = rp.RunwayFrame.build(
        "21R",
        {
            "threshold": {"lat": 36.226792, "lon": -115.046639},
            "far_end": {"lat": 36.247527, "lon": -115.024281},
            "width_m": 45,
        },
    )
    if swapped is None or rp.angle_diff(swapped.heading_deg, 221.2) > 2.0:
        print(
            "  FAIL backwards 21R geometry should flip to ~221° "
            f"(got {None if swapped is None else f'{swapped.heading_deg:.1f}'})"
        )
        bad += 1
    else:
        print(f"ok   backwards 21R geometry flipped to {swapped.heading_deg:.1f}°")

    ils = rp.RunwayFrame.build("21L", rp.runway_geometry(AIRPORT, "21L"))
    if ils is None:
        print("  FAIL 21L should be synthesized from 21R")
        bad += 1
    elif rp.angle_diff(ils.heading_deg, 221.2) > 2.0:
        print(f"  FAIL 21L should parallel 21R (~221°), got {ils.heading_deg:.1f}")
        bad += 1
    elif ils.tx <= rev.tx:
        print("  FAIL 21L threshold should sit east of 21R")
        bad += 1
    else:
        print(f"ok   21L parallel offset east of 21R ({ils.tx - rev.tx:.0f} m)")

    end_status = rp.FlightStatus(
        runway="21L",
        ok=True,
        runway_length_m=3000.0,
        fixes=[
            rp.UnitFix(
                unit_id="me",
                label="Fleece 1",
                along_m=2500.0,
                lateral_m=2.0,
                heading_err_deg=0.0,
                alt_m=570.0,
                height_m=5.0,
                speed_mps=40.0,
                own=True,
                on_runway=True,
            )
        ],
        tuning={"alt_tol": 60.0},
    )
    trig_end = rp.StepTrigger(zone="runway_end", settled=False, flight="me", explicit=True)
    held_end, wait_end = rp.runway_end_held(trig_end, end_status, config={})
    if not held_end:
        print(f"  FAIL 500 m remaining should be the departure end: {wait_end}")
        bad += 1
    end_status.fixes[0].along_m = 1200.0
    held_mid, wait_mid = rp.runway_end_held(trig_end, end_status, config={})
    if held_mid:
        print(f"  FAIL midfield rollout must wait: {wait_mid}")
        bad += 1
    end_status.fixes[0].along_m = 2500.0
    end_status.fixes[0].height_m = 40.0
    held_low, wait_low = rp.runway_end_held(trig_end, end_status, config={})
    if held_low:
        print(f"  FAIL a low approach / missed must not look like rollout: {wait_low}")
        bad += 1
    end_status.fixes[0].height_m = 5.0
    end_status.fixes[0].speed_mps = 80.0
    held_fast, wait_fast = rp.runway_end_held(trig_end, end_status, config={})
    if held_fast:
        print(f"  FAIL 150 kt over the far end must not be an exit: {wait_fast}")
        bad += 1
    end_status.fixes[0].speed_mps = 40.0
    if atc_phrase.runway_exit_hold_reason({"awaiting_on_the_go": True}):
        pass
    else:
        print("  FAIL option / low approach must suppress the exit call")
        bad += 1
    if not atc_phrase.runway_exit_hold_reason({"last_tx_template": "go_around"}):
        print("  FAIL go-around / missed must suppress the exit call")
        bad += 1
    if atc_phrase.runway_exit_hold_reason({"last_tx_template": "clear_land"}):
        print("  FAIL a full-stop land must still be allowed to exit")
        bad += 1
    trig_exit = rp.resolve_step_trigger({"template": "exit_runway"})
    if trig_exit is None or trig_exit.zone != "runway_end" or trig_exit.flight != "me":
        print(f"  FAIL exit_runway should arm on runway_end: {trig_exit}")
        bad += 1
    else:
        print("ok   exit_runway arms at the departure end")

    show("projection")
    # A point 500 m down the centreline from the 03L threshold.
    ux = math.sin(math.radians(frame.heading_deg))
    uz = math.cos(math.radians(frame.heading_deg))
    x, z = frame.tx + 500 * ux, frame.tz + 500 * uz
    along, lateral = frame.project(x, z)
    print(f"centreline +500 m -> along {along:.1f}, lateral {lateral:.1f}")
    if abs(along - 500) > 1 or abs(lateral) > 1:
        print("  FAIL centreline projection is off")
        bad += 1

    # 100 m right of that point.
    x2, z2 = x + 100 * uz, z - 100 * ux
    along2, lateral2 = frame.project(x2, z2)
    print(f"100 m right       -> along {along2:.1f}, lateral {lateral2:.1f}")
    if abs(along2 - 500) > 1 or abs(lateral2 - 100) > 1:
        print("  FAIL lateral offset is off")
        bad += 1

    show("verdicts")
    elev = rp.field_elev_m(AIRPORT)
    print(f"field elevation {elev:.0f} m ({elev * rp.FT_PER_M:.0f} ft)")

    def unit(uid, along, lateral, hdg=None, alt=None, spd=0.0):
        a_x, a_z = frame.tx + along * ux, frame.tz + along * uz
        a_x, a_z = a_x + lateral * uz, a_z - lateral * ux
        return {
            "id": uid,
            "type": "air",
            "name": f"Bruiser {uid}",
            "flightLabel": "BRUISER 5",
            "objectName": "F-16C_50",
            "xMeters": a_x,
            "zMeters": a_z,
            "altMeters": elev if alt is None else alt,
            "headingDeg": frame.heading_deg if hdg is None else hdg,
            "groundSpeedMps": spd,
        }

    cfg: dict[str, object] = {}
    tracker = rp.PositionTracker()
    geo = rp.runway_geometry(AIRPORT, "03L")
    half = frame.width_m / 2 + rp.rule(cfg, "position_lateral_margin_m")

    cases = [
        ("lined up on centreline", unit("1", 120, 0), True),
        ("lined up, 15 m off centre", unit("2", 200, 15), True),
        ("on the parallel taxiway (120 m off)", unit("3", 200, 120), False),
        ("far down the runway (2500 m)", unit("4", 2500, 0), False),
        ("lined up but facing 90° off", unit("5", 120, 0, hdg=frame.heading_deg + 90), False),
        ("overflying at 2000 ft agl", unit("6", 120, 0, alt=elev + 610), False),
    ]
    for title, u, want in cases:
        along, lateral = frame.project(u["xMeters"], u["zMeters"])
        height = float(u["altMeters"]) - elev
        hdg_err = rp.angle_diff(float(u["headingDeg"]), frame.heading_deg)
        got = bool(
            abs(height) <= rp.rule(cfg, "position_alt_tolerance_m")
            and abs(lateral) <= half
            and -rp.rule(cfg, "position_behind_threshold_m") <= along <= rp.rule(cfg, "position_box_m")
            and hdg_err <= rp.rule(cfg, "position_heading_tolerance_deg")
        )
        flag = "ok  " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"{flag} {title:38} in_position={got}")

    show("flight grouping")
    units = [unit("1", 120, 0), unit("2", 200, 15), dict(unit("9", 120, 0), flightLabel="VIPER 1")]
    own = units[0]
    members = rp.flight_members(units, own)
    print(f"flight of {len(members)} (other flight excluded: {len(members) == 2})")
    if len(members) != 2:
        print("  FAIL flight grouping picked up the wrong tracks")
        bad += 1

    show("eor")
    eor = rp.point_xz(geo.get("eor"))
    print(f"EOR at x={eor[0]:.0f} z={eor[1]:.0f}, radius {geo['eor']['radius_m']} m")
    d = math.hypot(eor[0] - frame.tx, eor[1] - frame.tz)
    print(f"EOR is {d:.0f} m from the 03L threshold")

    show("dwell + latch")
    import time as _t

    t0 = _t.time()
    print("held at t+0.0s:", tracker.held_for("k", True, 3.0, now=t0))
    print("held at t+3.1s:", tracker.held_for("k", True, 3.0, now=t0 + 3.1))
    print("breaks and restarts:", tracker.held_for("k", False, 3.0, now=t0 + 3.2))
    print("fire_once first:", tracker.fire_once("clr"), "again:", tracker.fire_once("clr"))
    if not tracker.held_for("k2", True, 0.0, now=t0):
        print("  FAIL zero dwell should pass immediately")
        bad += 1
    if not rp.skip_auto_tx_already_played(
        fire_id="dep_cruise",
        current_step_id="dep_handoff",
        last_step_id="dep_cruise",
    ):
        print("  FAIL auto must not replay a step after Play advanced the cursor")
        bad += 1
    if not rp.skip_auto_tx_already_played(
        fire_id="dep_cruise",
        current_step_id="dep_cruise",
        last_step_id="dep_cruise",
    ):
        print("  FAIL auto must not replay a step still under the cursor")
        bad += 1
    if rp.skip_auto_tx_already_played(
        fire_id="dep_cruise",
        current_step_id="dep_cruise",
        last_step_id="dep_radar",
    ):
        print("  FAIL first auto of the cursor step should still fire")
        bad += 1
    if rp.skip_auto_tx_already_played(
        fire_id="twr_land",
        current_step_id="twr_land",
        last_step_id="twr_land",
        template="clear_land",
        hold_for_landing=True,
    ):
        print("  FAIL next-seat land must still auto")
        bad += 1
    if not rp.skip_auto_tx_already_played(
        fire_id="dep_cruise",
        current_step_id="dep_cruise",
        last_step_id="dep_radar",
        playing_id="dep_cruise",
    ):
        print("  FAIL auto must not overlap Play while it is still talking")
        bad += 1
    if not rp.skip_auto_tx_already_played(
        fire_id="twr_land",
        current_step_id="twr_land",
        last_step_id="twr_land",
        template="clear_land",
        hold_for_landing=True,
        playing_id="twr_land",
    ):
        print("  FAIL land must wait until Play finishes before the next seat")
        bad += 1
    play_tracker = rp.PositionTracker()
    play_tracker.pending_latch = "fire:dep_cruise:21R"
    play_tracker.mark_step_played("dep_cruise")
    if play_tracker.armed("fire:dep_cruise:21R"):
        print("  FAIL Play must disarm Watch for the step it is transmitting")
        bad += 1
    play_tracker.finish_manual_tx("dep_cruise")
    if play_tracker.armed("fire:dep_cruise:21R"):
        print("  FAIL the consumed Watch latch must stay spent after Play")
        bad += 1
    fail_tracker = rp.PositionTracker()
    fail_tracker.pending_latch = "fire:dep_cruise:21R"
    fail_tracker.mark_step_played("dep_cruise")
    fail_tracker.unmark_step_played("dep_cruise")
    if not fail_tracker.armed("fire:dep_cruise:21R"):
        print("  FAIL a failed Play must re-arm Watch")
        bad += 1
    else:
        print("auto vs Play — no double TX; landing seats still auto")

    bad += check_zones()
    bad += check_zone_admission()
    bad += check_step_triggers()
    bad += check_trigger_timing()
    bad += check_distance_or_zone()

    print(f"\n{'all good' if not bad else f'{bad} problem(s)'}")
    return 1 if bad else 0


def check_zones() -> int:
    """Drawn areas: containment, runway scoping, and priority over the box."""
    bad = 0
    show("drawn zones")
    square = {
        "id": "z1",
        "name": "EOR box",
        "kind": "polygon",
        "trigger": "eor",
        "runway": "03L",
        # 200 m square around x 0..200, z 0..200
        "points": [
            {"x": 0, "z": 0},
            {"x": 200, "z": 0},
            {"x": 200, "z": 200},
            {"x": 0, "z": 200},
        ],
    }
    circle = {
        "id": "z2",
        "name": "Ramp",
        "kind": "circle",
        "trigger": "parking",
        "centre": {"x": 1000, "z": 1000},
        "radius_m": 150,
    }
    checks = [
        ("inside the square", square, 100, 100, True),
        ("outside the square", square, 260, 100, False),
        ("on a square corner region", square, 199, 199, True),
        ("inside the circle", circle, 1050, 1050, True),
        ("just outside the circle", circle, 1000, 1160, False),
    ]
    for title, zone, x, z, want in checks:
        got = rp.point_in_zone(x, z, zone)
        flag = "ok  " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"{flag} {title:30} -> {got}")

    airport = {
        "runways": ["03L", "21R"],
        "geometry": {"field_elev_ft": 1870, "zones": [square, circle]},
    }
    if len(rp.zones_for(airport, "eor", "03L")) != 1:
        print("  FAIL 03L should see its own EOR zone")
        bad += 1
    if rp.zones_for(airport, "eor", "21R"):
        print("  FAIL 21R must not inherit an 03L-only zone")
        bad += 1
    # A zone with no runway applies everywhere.
    shared = dict(circle)
    shared.pop("runway", None)
    if len(rp.zones_for(airport, "parking", "21R")) != 1:
        print("  FAIL a zone without a runway should apply to all runways")
        bad += 1
    print("runway scoping — ok")

    hit = rp.in_any_zone(100, 100, [square, circle])
    print(f"in_any_zone picks: {hit and hit['name']}")
    if not hit or hit["name"] != "EOR box":
        print("  FAIL in_any_zone returned the wrong area")
        bad += 1

    show("finding the zone a step asked for")
    # A tag follows the active runway; an id pins to one area.
    other = dict(square, id="z3", name="21R EOR box", runway="21R")
    field = {"geometry": {"zones": [square, other, circle]}}
    tries = [
        ("tag 'eor' on 03L", "eor", "03L", "EOR box"),
        ("tag 'eor' on 21R", "eor", "21R", "21R EOR box"),
        ("id 'z3' regardless of runway", "z3", "03L", "21R EOR box"),
        ("tag with nothing drawn", "hold_short", "03L", None),
        ("id that does not exist", "nope", "03L", None),
    ]
    for title, ref, rwy, want in tries:
        got = rp.zone_by_ref(field, ref, rwy)
        name = got and got.get("name")
        flag = "ok  " if name == want else "FAIL"
        if name != want:
            bad += 1
        print(f"{flag} {title:34} -> {name}")

    # A field-wide area is only picked when nothing runway-specific matches.
    shared_eor = dict(square, id="z4", name="either end")
    shared_eor.pop("runway", None)
    picked = rp.zone_by_ref({"geometry": {"zones": [shared_eor, square]}}, "eor", "03L")
    if not picked or picked.get("name") != "EOR box":
        print("  FAIL a runway-specific area should beat a field-wide one")
        bad += 1

    show("tag resolves to every matching area on that runway")
    # Nellis-style: two EORs on 03L — a tag must arm either box, not just the first.
    as_eor = dict(square, id="eor-as", name="AS EOR", runway="03L")
    an_eor = dict(
        square,
        id="eor-an",
        name="AN EOR",
        runway="03L",
        points=[{"x": 500.0, "z": 500.0}, {"x": 600.0, "z": 500.0},
                {"x": 600.0, "z": 600.0}, {"x": 500.0, "z": 600.0}],
    )
    multi = {"geometry": {"zones": [as_eor, an_eor, other]}}
    got_all = rp.zones_by_ref(multi, "eor", "03L")
    names = sorted(str(z.get("name") or "") for z in got_all)
    if names != ["AN EOR", "AS EOR"]:
        print(f"  FAIL tag eor on 03L should return both EORs, got {names}")
        bad += 1
    else:
        print(f"ok   tag 'eor' on 03L -> {names}")
    got_21 = rp.zones_by_ref(multi, "eor", "21R")
    if len(got_21) != 1 or got_21[0].get("name") != "21R EOR box":
        print(f"  FAIL tag eor on 21R should be only 21R EOR, got {got_21}")
        bad += 1
    else:
        print("ok   tag 'eor' on 21R -> ['21R EOR box']")
    by_id = rp.zones_by_ref(multi, "eor-an", "21R")
    if len(by_id) != 1 or by_id[0].get("name") != "AN EOR":
        print("  FAIL id should still pin one area regardless of runway")
        bad += 1
    else:
        print("ok   id 'eor-an' pins AN EOR alone")
    if rp.zones_ref_label("eor", got_all) != "any eor area":
        print(f"  FAIL zones_ref_label for a multi tag: {rp.zones_ref_label('eor', got_all)!r}")
        bad += 1
    got_as = rp.zones_by_ref(multi, "eor", "03L", place="Alpha South")
    as_names = [str(z.get("name") or "") for z in got_as]
    if as_names != ["AS EOR"]:
        print(f"  FAIL assigned Alpha South should pin AS EOR, got {as_names}")
        bad += 1
    else:
        print("ok   assigned Alpha South -> AS EOR")
    got_an = rp.zones_by_ref(multi, "eor", "03L", place="AN EOR")
    an_names = [str(z.get("name") or "") for z in got_an]
    if an_names != ["AN EOR"]:
        print(f"  FAIL assigned AN EOR should pin AN EOR, got {an_names}")
        bad += 1
    else:
        print("ok   assigned AN EOR -> AN EOR")
    nw_zone = dict(other, name="NW EOR")
    nw_field = {"geometry": {"zones": [as_eor, an_eor, nw_zone]}}
    got_nw = rp.zones_by_ref(nw_field, "eor", "21R", place="NW EOR")
    nw_names = [str(z.get("name") or "") for z in got_nw]
    if nw_names != ["NW EOR"]:
        print(f"  FAIL assigned NW EOR should pin NW EOR, got {nw_names}")
        bad += 1
    else:
        print("ok   assigned NW EOR -> NW EOR")
    return bad


def check_zone_admission() -> int:
    """Altitude bands, and the difference between inside and settled."""
    bad = 0
    show("altitude band")
    tower = {
        "id": "tower-area",
        "name": "Tower area",
        "trigger": "tower",
        "kind": "circle",
        "centre": {"x": 0, "z": 0},
        "radius_m": 9260.0,
        "min_alt_ft": 0,
        "max_alt_ft": 5000,
    }

    def fix(**kw):
        base = dict(
            unit_id="1",
            label="Bruiser 1",
            along_m=0.0,
            lateral_m=0.0,
            heading_err_deg=0.0,
            alt_m=None,
            height_m=0.0,
            speed_mps=0.0,
            x_m=0.0,
            z_m=0.0,
        )
        base.update(kw)
        return rp.UnitFix(**base)

    band = [
        ("on the deck inside the circle", fix(), True),
        ("2000 ft agl, inside the band", fix(height_m=610.0), True),
        ("8000 ft agl, above the band", fix(height_m=2440.0), False),
        ("inside the band but outside the circle", fix(x_m=12000.0), False),
        ("altitude unknown, plainly inside", fix(height_m=None), True),
        # One field elevation for a field that is not flat: parked jets read a
        # little below the surface and must still be in a surface zone.
        ("parked, reading 25 ft below field elevation", fix(height_m=-7.6), True),
    ]
    for title, f, want in band:
        got = rp.zone_admits(tower, f, settled=False)
        flag = "ok  " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"{flag} {title:40} -> {got}")

    # A floor someone deliberately put in the air is still enforced.
    overhead = dict(tower, id="break", min_alt_ft=1500, max_alt_ft=5000)
    if rp.zone_admits(overhead, fix(), settled=False):
        print("  FAIL a floor of 1500 ft should exclude a jet on the ground")
        bad += 1
    if not rp.zone_admits(overhead, fix(height_m=610.0), settled=False):
        print("  FAIL 2000 ft agl is inside a 1500-5000 band")
        bad += 1
    print("ok   a floor above the surface is enforced as written")

    show("settled versus merely inside")
    pad = {
        "id": "pad",
        "name": "21R hold pad",
        "trigger": "in_position",
        "kind": "circle",
        "centre": {"x": 0, "z": 0},
        "radius_m": 300.0,
    }
    settled_cases = [
        ("stopped and lined up", fix(), True),
        ("rolling at 8 kt (4.1 m/s)", fix(speed_mps=4.1), False),
        ("stopped but 90 degrees off", fix(heading_err_deg=90.0), False),
        ("airborne over the pad", fix(height_m=300.0), False),
        ("speed and heading unknown", fix(speed_mps=None, heading_err_deg=None), True),
    ]
    for title, f, want in settled_cases:
        got = rp.zone_admits(pad, f, settled=True)
        flag = "ok  " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"{flag} {title:40} -> {got}")
    # The looser taxi threshold would let that rolling jet through, which is the
    # whole reason `settled` has its own limit.
    if rp.DEFAULTS["position_settled_speed_mps"] >= rp.DEFAULTS["position_max_speed_mps"]:
        print("  FAIL settled speed must be tighter than the taxi speed")
        bad += 1

    show("counting the flight in a zone")
    status = rp.FlightStatus(runway="21R", ok=True, total=2)
    status.fixes = [
        fix(unit_id="1", own=True),
        fix(unit_id="2", speed_mps=6.0),  # inside, still rolling
    ]
    count = status.in_zone(pad, settled=True)
    print(f"{count.describe(need_full=True)}")
    if count.inside != 2 or count.qualified != 1 or count.ok(need_full=True):
        print("  FAIL two inside, one settled, so the whole flight is not ready")
        bad += 1
    if not count.ok(need_full=False):
        print("  FAIL 'just me' should pass — the settled one is own aircraft")
        bad += 1
    # Own aircraft rolling, wingman parked: "just me" must not fire on the wingman.
    status.fixes = [fix(unit_id="1", own=True, speed_mps=6.0), fix(unit_id="2")]
    if status.in_zone(pad, settled=True).ok(need_full=False):
        print("  FAIL 'just me' fired on a wingman's position")
        bad += 1
    print("ok   'just me' means me, not whoever is parked in the right place")

    eor_cap = dict(pad, name="NW EOR", trigger="eor", max_alt_ft=100.0)
    status = rp.FlightStatus(runway="21R", ok=True, total=1)
    status.fixes = [fix(unit_id="1", own=True, height_m=305.0)]
    high = status.in_zone(eor_cap, settled=True)
    desc = high.describe(need_full=False)
    if high.inside or high.ok(need_full=False) or "AGL" not in desc or "100" not in desc:
        print(f"  FAIL airborne over EOR should name altitude, got {desc!r}")
        bad += 1
    else:
        print(f"ok   airborne over EOR: {desc}")

    # Recreate on-deck, 180° from takeoff heading — monitor tower must still arm.
    status.fixes = [
        fix(unit_id="1", own=True, heading_err_deg=180.0, height_m=0.0, speed_mps=0.0)
    ]
    recip = status.in_zone(eor_cap, settled=True)
    if not recip.ok(need_full=False):
        print(
            f"  FAIL EOR reciprocal heading should still settle: "
            f"{recip.describe(need_full=False)}"
        )
        bad += 1
    else:
        print("ok   EOR reciprocal heading still settles (not the lineup heading)")

    # Leaving: nobody inside is what a "clear of the runway" step waits for.
    status.fixes = [fix(unit_id="1", own=True, x_m=5000.0), fix(unit_id="2", x_m=5000.0)]
    out = status.in_zone(pad, settled=False)
    if not out.out_ok(need_full=True) or out.inside:
        print("  FAIL a flight clear of the area should read as out of it")
        bad += 1
    print("ok   flight taxied clear reads as out of the area")

    # Tag with two boxes: sitting in the second one still arms the step.
    other_pad = {
        "id": "pad-b",
        "name": "AN EOR",
        "trigger": "eor",
        "kind": "circle",
        "centre": {"x": 800.0, "z": 0.0},
        "radius_m": 200.0,
    }
    status.total = 1
    status.fixes = [fix(unit_id="1", own=True, x_m=800.0, z_m=0.0)]
    multi = status.in_zones([pad, other_pad], settled=True, label="any eor area")
    if not multi.ok(need_full=True) or multi.label != "AN EOR":
        print(f"  FAIL in_zones should OR across areas, got {multi}")
        bad += 1
    else:
        print("ok   in_zones ORs across every area matching the tag")
    return bad


def check_step_triggers() -> int:
    """Reading a step's trigger block, and the legacy templates."""
    bad = 0
    show("step triggers")
    step = {
        "id": "twr_clear_takeoff",
        "template": "clear_takeoff",
        "trigger": {
            "zone": "in_position",
            "flight": "all",
            "settled": True,
            "dwell_s": 15,
            "gap_s": 8,
        },
    }
    trig = rp.step_trigger(step)
    assert trig is not None
    print(f"explicit: {trig.describe()}")
    if not trig.explicit or trig.zone != "in_position" or trig.when != "inside":
        print("  FAIL explicit trigger read wrong")
        bad += 1
    if trig.dwell({}) != 15.0 or trig.gap_s != 8.0 or not trig.need_full({}):
        print("  FAIL dwell / gap / flight read wrong")
        bad += 1
    if not trig.enabled({"auto_takeoff_clearance": False}):
        print("  FAIL an explicit trigger should not answer to the legacy toggles")
        bad += 1

    loose = rp.step_trigger({"trigger": {"zone": "ramp", "when": "leaving", "flight": "me", "settled": False}})
    assert loose is not None
    if loose.when != "leaving" or loose.need_full({}) or loose.settled:
        print("  FAIL leaving / me / unsettled read wrong")
        bad += 1
    print(f"leaving:  {loose.describe()}")

    # No dwell of its own falls back to the global setting.
    plain = rp.step_trigger({"trigger": {"zone": "eor"}})
    assert plain is not None
    if plain.dwell({"auto_clearance_dwell_s": 9}) != 9.0:
        print("  FAIL a trigger with no dwell should use auto_clearance_dwell_s")
        bad += 1
    if not plain.settled:
        print("  FAIL settled should default on")
        bad += 1

    legacy = rp.step_trigger({"template": "monitor_tower"})
    assert legacy is not None
    print(f"legacy:   {legacy.zone} via template, toggle {legacy.enabled_key}")
    if legacy.explicit or legacy.zone != "eor":
        print("  FAIL monitor_tower should still arm off the EOR area")
        bad += 1
    if legacy.enabled({"auto_monitor_tower": False}):
        print("  FAIL the legacy toggle should still switch it off")
        bad += 1
    for template in (
        "lineup",
        "clear_takeoff",
        "clear_takeoff_rolling",
        "clear_takeoff_intersection",
    ):
        t = rp.step_trigger({"template": template})
        if t is None or t.zone != "in_position":
            print(f"  FAIL {template} lost its in-position trigger")
            bad += 1
    if rp.step_trigger({"template": "radio_check"}) is not None:
        print("  FAIL an ordinary step must not fire off position")
        bad += 1
    if rp.step_trigger({"trigger": {"zone": "   "}}) is not None:
        print("  FAIL a blank zone is not a trigger")
        bad += 1
    print("ok   plain steps stay manual, legacy templates keep working")
    return bad


def check_trigger_timing() -> int:
    """Dwell resetting, the radio gap, and firing once per step."""
    bad = 0
    show("dwell, radio gap, latch")
    import time as _t

    tracker = rp.PositionTracker()
    t0 = _t.time()
    key = "cond:twr_clear_takeoff:21R"
    tracker.held_for(key, True, 15.0, now=t0)
    if tracker.held_for(key, True, 15.0, now=t0 + 14.0):
        print("  FAIL 15 s dwell passed after 14 s")
        bad += 1
    # A member drops out at 14 s: the clock starts again, it does not resume.
    tracker.held_for(key, False, 15.0, now=t0 + 14.0)
    if tracker.held_for(key, True, 15.0, now=t0 + 16.0):
        print("  FAIL dwell resumed instead of restarting after the condition broke")
        bad += 1
    if not tracker.held_for(key, True, 15.0, now=t0 + 31.5):
        print("  FAIL dwell never completed")
        bad += 1
    print("ok   dwell restarts when a jet drops out rather than resuming")

    trig = rp.step_trigger({"trigger": {"zone": "in_position", "gap_s": 8}})
    state = {"last_tx_at": t0}
    left = rp.gap_remaining(trig, state, now=t0 + 3.0)
    print(f"3 s after the last call, {left:.0f}s still to wait")
    if abs(left - 5.0) > 0.01:
        print("  FAIL radio gap arithmetic is wrong")
        bad += 1
    if rp.gap_remaining(trig, state, now=t0 + 9.0) != 0.0:
        print("  FAIL the gap should be spent after 9 s")
        bad += 1
    if rp.gap_remaining(trig, {}, now=t0) != 0.0:
        print("  FAIL nothing said yet means nothing to wait for")
        bad += 1
    if rp.gap_remaining(rp.step_trigger({"trigger": {"zone": "eor"}}), state, now=t0) != 0.0:
        print("  FAIL no gap asked for, none should be imposed")
        bad += 1

    if not tracker.fire_once("fire:a:21R") or tracker.fire_once("fire:a:21R"):
        print("  FAIL a step should fire once per sortie")
        bad += 1
    if not tracker.fire_once("fire:b:21R"):
        print("  FAIL latching one step must not latch another")
        bad += 1
    if tracker.armed("fire:a:21R") or not tracker.armed("fire:c:21R"):
        print("  FAIL armed() disagrees with fire_once()")
        bad += 1
    tracker.reset()
    if not tracker.fire_once("fire:a:21R"):
        print("  FAIL Reset should re-arm the flow for a new sortie")
        bad += 1
    print("ok   fires once per step, re-armed by Reset")

    show("leaving needs to have arrived first")
    tracker = rp.PositionTracker()
    if tracker.has_left("k", False):
        print("  FAIL never been in the area, so it cannot have left")
        bad += 1
    tracker.has_left("k", True)
    if not tracker.has_left("k", False):
        print("  FAIL was inside, now out — that is leaving")
        bad += 1
    print("ok   leaving only counts after arriving")
    return bad


def check_distance_or_zone() -> int:
    """Range-exit / EOR gates must not need a 'at the field' verdict."""
    bad = 0
    show("distance OR zone (range exit) and Nellis EOR boxes")

    if rp.field_proximity_applies(None) is not True:
        print("  FAIL empty watch still uses the field-proximity cutoff")
        bad += 1
    eor_watch = rp.zones_by_ref(AIRPORT, "eor", "21R", place="NW EOR")
    if not rp.field_proximity_applies(eor_watch):
        print("  FAIL assigned EOR should still use the field-proximity cutoff")
        bad += 1
    app_watch = rp.zones_by_ref(AIRPORT, "approach", None)
    if rp.field_proximity_applies(app_watch):
        print("  FAIL approach watch must skip the field-proximity cutoff")
        bad += 1
    else:
        print("ok   field-proximity cutoff: EOR on, approach off")

    far = rp.FlightStatus(
        ok=False, reason="nearest match is 25 NM out — not at the field"
    )
    range_exit = rp.step_trigger(
        {
            "template": "bj_range_exit",
            "trigger": {
                "zone": "approach",
                "within_nm": 40,
                "settled": False,
            },
        }
    )
    held_in, wait_in = rp.condition_held(
        range_exit, far, zones=app_watch, distance_nm=25.0
    )
    if not held_in:
        print(f"  FAIL 25 NM should fire range exit without a field verdict: {_ascii(wait_in)}")
        bad += 1
    else:
        print(f"ok   25 NM + no field verdict -> fire ({_ascii(wait_in)})")

    held_out, wait_out = rp.condition_held(
        range_exit, far, zones=app_watch, distance_nm=50.0
    )
    if held_out:
        print(f"  FAIL 50 NM must not fire range exit: {_ascii(wait_out)}")
        bad += 1
    elif "50" not in wait_out or "40" not in wait_out:
        print(f"  FAIL 50 NM waiting should name the 40 NM gate: {_ascii(wait_out)}")
        bad += 1
    else:
        print(f"ok   50 NM waits on the 40 NM gate ({_ascii(wait_out)})")

    tower = rp.step_trigger(
        {"template": "cleared_approach", "trigger": {"within_nm": 12, "settled": False}}
    )
    held_twr, _ = rp.condition_held(tower, far, distance_nm=11.0)
    if not held_twr:
        print("  FAIL contact-tower 12 NM must fire far from the field")
        bad += 1
    else:
        print("ok   11 NM fires the 12 NM tower gate with no field verdict")

    eor_trig = rp.step_trigger(
        {"template": "monitor_tower", "trigger": {"zone": "eor", "settled": True}}
    )
    held_eor, wait_eor = rp.condition_held(
        eor_trig, far, zones=eor_watch, distance_nm=25.0
    )
    if held_eor:
        print(f"  FAIL EOR must not fire 25 NM out: {_ascii(wait_eor)}")
        bad += 1
    else:
        print(f"ok   EOR stays waiting far from the field ({_ascii(wait_eor)})")

    for rwy, place in (("21R", "NW EOR"), ("03L", "Alpha South")):
        boxes = rp.zones_by_ref(AIRPORT, "eor", rwy, place=place)
        if len(boxes) != 1:
            print(f"  FAIL {rwy} {place} should pin one box, got {len(boxes)}")
            bad += 1
            continue
        centre = rp.zone_centre_xz(boxes[0])
        if centre is None or not rp.point_in_zone(centre[0], centre[1], boxes[0]):
            print(f"  FAIL {place} centroid is not inside its drawn polygon")
            bad += 1
        else:
            print(f"ok   {rwy} {place} centroid is inside the drawn box")

    if not app_watch:
        print("  FAIL Nellis has no approach zone")
        bad += 1
    else:
        centre = rp.zone_centre_xz(app_watch[0])
        assert centre is not None
        inside = (centre[0] + 55_560.0, centre[1])  # ~30 NM east
        outside = (centre[0] + 83_340.0, centre[1])  # ~45 NM east
        if not rp.point_in_zone(*inside, app_watch[0]):
            print("  FAIL 30 NM from APP centre should be inside the approach circle")
            bad += 1
        elif rp.point_in_zone(*outside, app_watch[0]):
            print("  FAIL 45 NM from APP centre should be outside the approach circle")
            bad += 1
        else:
            print("ok   approach circle covers ~30 NM, not 45 NM")

        app_status = rp.FlightStatus(
            ok=True,
            total=1,
            fixes=[
                rp.UnitFix(
                    unit_id="1",
                    label="Fleece 1",
                    along_m=0.0,
                    lateral_m=0.0,
                    heading_err_deg=None,
                    alt_m=None,
                    height_m=None,
                    speed_mps=None,
                    x_m=inside[0],
                    z_m=inside[1],
                    own=True,
                )
            ],
        )
        held_zone, wait_zone = rp.condition_held(
            range_exit, app_status, zones=app_watch, distance_nm=50.0
        )
        if not held_zone:
            print(f"  FAIL inside approach at 50 NM should still OR-fire: {_ascii(wait_zone)}")
            bad += 1
        else:
            print(f"ok   inside approach OR-fires even at 50 NM ({_ascii(wait_zone)})")

    handoff = rp.resolve_step_trigger(
        {
            "template": "departure_handoff",
            "trigger": {"within_nm": 18, "when": "leaving"},
        }
    )
    held_spawn, wait_spawn = rp.within_nm_held(handoff, 87.0)
    held_close, _ = rp.within_nm_held(handoff, 12.0)
    none_held, none_wait = rp.within_nm_held(handoff, None)
    if not held_spawn or held_close or none_held:
        print(
            f"  FAIL handoff must fire when already beyond 18 NM: "
            f"87={held_spawn} 12={held_close} none={none_held}"
        )
        bad += 1
    elif "already beyond" not in wait_spawn.lower():
        print(f"  FAIL already-beyond wording: {_ascii(wait_spawn)}")
        bad += 1
    elif "beyond 18" not in none_wait.lower():
        print(f"  FAIL unknown position should ask for beyond 18 NM: {_ascii(none_wait)}")
        bad += 1
    else:
        print(f"ok   spawn already beyond 18 NM fires handoff ({_ascii(wait_spawn)})")

    return bad


def dump_live_triggers() -> int:
    """Print CAOC vs the current flow step so a parked jet can prove the pipeline."""
    import flow_engine

    cfg = atc_phrase.load_json(atc_phrase.CONFIG_PATH)
    auto = bool(cfg.get("auto_clearance_enabled"))
    print(f"auto_clearance_enabled: {auto}")
    if not auto:
        print("  nothing auto-fires until Setup -> Watch live position is on")

    engine = flow_engine.FlowEngine()
    step = engine.current_step() or {}
    trigger = rp.resolve_step_trigger(
        step, mission=engine.mission, state=engine.state
    )
    print(f"step: {step.get('id') or '(none)'}  {step.get('label') or step.get('template') or ''}")
    print(f"trigger: {trigger.describe() if trigger else '(none - this step is voice/manual)'}")

    airport = engine.airport()
    opus, weather = atc_phrase.resolve_opus_and_metar(cfg, airport["icao"])
    if not opus:
        opus = atc_phrase.synthetic_flight_context(
            atc_phrase.callsign_override(cfg) or "CALLSIGN"
        )
    callsign = (
        atc_phrase.cached_radio_callsign(cfg)
        or (opus.radio_callsign if opus else "")
        or ""
    )
    runway = atc_phrase.pick_departure_runway(
        airport,
        weather,
        opus,
        cfg,
        step=step,
        mission=engine.mission,
        state=engine.state,
        template=str(step.get("template") or "") or None,
    )
    assigned_eor = ""
    if trigger is not None and str(trigger.zone).strip().lower() == "eor" and runway:
        assigned_eor = str(
            (atc_phrase.resolve_taxi_route(airport, runway, opus=opus) or {}).get("eor")
            or ""
        ).strip()
    watch = []
    if trigger is not None and trigger.zone:
        watch = rp.zones_by_ref(
            airport, trigger.zone, runway, place=assigned_eor or None
        )
    print(f"runway: {runway or '?'}  assigned EOR: {assigned_eor or '(n/a)'}")
    print(f"watch zones: {[rp.zone_label(z) for z in watch] or '(none)'}")

    dist = rp.ownship_distance_nm(
        airport, config=cfg, callsign=callsign, opus=opus, state=engine.state
    )
    print(f"ownship NM from field: {dist if dist is None else f'{dist:.1f}'}")

    status = rp.PositionTracker().evaluate(
        cfg, airport, runway, callsign=callsign, opus=opus, watch=watch or None
    )
    print(
        f"evaluate: ok={status.ok}  label={status.own_label or '-'}  "
        f"{_ascii(status.reason or status.summary())}"
    )

    if trigger is not None:
        held, waiting = rp.condition_held(
            trigger,
            status,
            zones=watch,
            distance_nm=dist,
            config=cfg,
        )
        print(f"would hold now: {held}  ({_ascii(waiting) or 'ready'})")
        if held:
            print("  dwell / radio gap still apply before AUTO actually transmits")
        elif not auto:
            print("  even if this held, AUTO is off so Fly will not transmit")

    # Always score the EOR / approach boxes so a ramp jet proves matching.
    for rwy, place in (("21R", "NW EOR"), ("03L", "Alpha South")):
        boxes = rp.zones_by_ref(airport, "eor", rwy, place=place)
        if not boxes:
            print(f"{place}: no drawn box")
            continue
        count = status.in_zones(boxes, settled=True, label=place)
        print(f"{place}: {_ascii(count.describe(need_full=False))}")
    app = rp.zones_by_ref(airport, "approach", None)
    if app:
        count = status.in_zones(app, settled=False, label="approach")
        print(f"approach: {_ascii(count.describe(need_full=False))}")
    return 0


if __name__ == "__main__":
    if "--live" in sys.argv:
        raise SystemExit(dump_live_triggers())
    raise SystemExit(main())
