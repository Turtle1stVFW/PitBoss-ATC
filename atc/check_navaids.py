"""
Regression check: navaid position calls, elevator requests, point vectors.

Covers the three things ATC gained over bullseye-only phrasing:
  * radar contact fixed off the nearest VOR/TACAN (C2 still uses ELVIS)
  * 'request elevator' answered in feet/flight levels or angels by agency
  * 'vectors to <point>' and 'vectors to the nearest divert'

Run: py -3 check_navaids.py
"""

from __future__ import annotations

import agencies
import atc_phrase
import navaids
import voice_intent

AIRPORT = atc_phrase.load_json(atc_phrase.AIRPORTS_PATH)["nellis"]
CALLSIGN = "DAGGER 1"
CONFIG = {"bullseye_magnetic_declination_deg": 12}

# Known station positions, so a bad catalog edit shows up here.
MMM = (36.76928, -114.27747)
LSV = (36.24467, -115.025)


def _fail(bad: int, message: str) -> int:
    print(f"  FAIL {message}")
    return bad + 1


def catalog() -> int:
    """The JSON parses and carries the stations the phrasing depends on."""
    bad = 0
    ids = {st.id for st in navaids.stations()}
    for want in ("LSV", "LAS", "MMM", "BTY", "BLD", "ILC", "TPH", "GRL", "TTR"):
        if want not in ids:
            bad = _fail(bad, f"navaids.json is missing station {want}")
    kinds = {st.kind for st in navaids.stations()}
    if not kinds <= {"vor", "vortac", "tacan"}:
        bad = _fail(bad, f"unexpected station kinds: {sorted(kinds)}")

    fields = {f.id: f for f in navaids.diverts()}
    for want in ("KLSV", "KINS", "KTNX", "KEDW", "KNID", "KLAS"):
        if want not in fields:
            bad = _fail(bad, f"navaids.json is missing divert {want}")
    # Groom Lake is a position reference, never somewhere to be sent.
    if any("GROOM" in f.id or "Groom" in f.say for f in navaids.diverts()):
        bad = _fail(bad, "Groom Lake must not be a divert option")
    if "GRL" not in ids:
        bad = _fail(bad, "Groom Lake should still be a position reference")
    if not bad:
        print(
            f"catalog — {len(navaids.stations())} stations, "
            f"{len(navaids.diverts())} diverts, no Groom Lake divert"
        )
    return bad


def cardinals() -> int:
    """8-point compass sectors, and the bearing runs station → jet."""
    bad = 0
    for bearing, want in (
        (0, "north"),
        (44, "northeast"),
        (90, "east"),
        (135, "southeast"),
        (180, "south"),
        (225, "southwest"),
        (270, "west"),
        (315, "northwest"),
        (350, "north"),
    ):
        got = navaids.cardinal(bearing)
        if got != want:
            bad = _fail(bad, f"bearing {bearing} should be {want}, got {got}")

    # Due north of Mormon Mesa: the jet is what is north of the station.
    north_of_mmm = (MMM[0] + 0.5, MMM[1])
    fix = navaids.station_position_fix(*north_of_mmm, config=CONFIG)
    if fix is None or fix["id"] != "MMM":
        bad = _fail(bad, f"nearest station north of MMM should be MMM, got {fix}")
    elif fix["cardinal"] not in ("north", "northwest"):
        bad = _fail(bad, f"jet north of MMM should read north, got {fix['spoken']}")
    elif not 28 <= fix["range_nm"] <= 32:
        bad = _fail(bad, f"half a degree of latitude is ~30 NM, got {fix['range_nm']}")
    else:
        print(f"position fix — {fix['display']} / {fix['spoken']}")

    over = navaids.station_position_fix(*LSV, config=CONFIG)
    if over is None or "Nellis" not in over["spoken"]:
        bad = _fail(bad, f"over the field should name Nellis, got {over}")

    # Nothing within range → no clause rather than a silly one.
    if navaids.station_position_fix(0.0, 0.0, config=CONFIG) is not None:
        bad = _fail(bad, "the Gulf of Guinea has no NTTR station in range")
    if not bad:
        print("cardinals — 8 sectors, station-relative, range-limited")
    return bad


def point_lookup() -> int:
    """Spoken point names resolve, including the way Whisper mangles them."""
    bad = 0
    cases = (
        ("stryk", "STRYK"),
        ("strike", "STRYK"),  # Whisper hears the word
        ("arcoe", "ARCOE"),
        ("arco", "ARCOE"),
        ("mormon mesa", "MMM"),
        ("mormon mesa vortac", "MMM"),  # trailing station word dropped
        ("beatty", "BTY"),
        ("nellis", "LSV"),
        ("lincoln county", "LINCOLN"),
    )
    for spoken, want in cases:
        hit = navaids.resolve_point(spoken, airport=AIRPORT)
        got = (hit or {}).get("id")
        if got != want:
            bad = _fail(bad, f"resolve_point({spoken!r}) should be {want}, got {got}")
    if navaids.resolve_point("qwertyuiop", airport=AIRPORT) is not None:
        bad = _fail(bad, "nonsense must not resolve to a fix")

    # Approach fixes bring their spoken casing once the airport is known.
    stryk = navaids.resolve_point("stryk", airport=AIRPORT)
    if (stryk or {}).get("say") != "Stryk":
        bad = _fail(bad, f"STRYK should speak as 'Stryk', got {(stryk or {}).get('say')}")
    if not bad:
        print("point lookup — exact, alias and fuzzy hits; nonsense rejected")
    return bad


def extraction() -> int:
    """extract_nav_point pulls the name and refuses the divert wording."""
    bad = 0
    cases = (
        ("request vectors to stryk", "stryk"),
        ("request vectors to the stryk", "stryk"),
        ("vector to mormon mesa", "mormon mesa"),
        ("how far to beatty", "beatty"),
        ("request bearing to arcoe please", "arcoe"),
        ("request vectors to mormon mesa vortac", "mormon mesa"),
    )
    for raw, want in cases:
        got = voice_intent.extract_nav_point(voice_intent.normalize(raw))
        if got != want:
            bad = _fail(bad, f"extract_nav_point({raw!r}) should be {want!r}, got {got!r}")
    for raw in (
        "request vectors to the nearest divert",
        "request vectors to the closest suitable field",
        "request vectors",
    ):
        got = voice_intent.extract_nav_point(voice_intent.normalize(raw))
        if got is not None:
            bad = _fail(bad, f"{raw!r} names no point, got {got!r}")
    if not bad:
        print("extraction — point names only; divert wording left alone")
    return bad


def diverts() -> int:
    """Nearest field, and the category filter."""
    bad = 0
    # North range: Tonopah Test Range is the closest military field.
    field = navaids.nearest_divert(37.6, -116.7, config=CONFIG)
    if (field or {}).get("id") != "KTNX":
        bad = _fail(bad, f"north range divert should be KTNX, got {field}")
    elif not 0 <= field["bearing"] <= 359 or field["range_nm"] < 1:
        bad = _fail(bad, f"divert bearing/range looks wrong: {field}")
    else:
        print(
            f"divert — north range is {field['say']} "
            f"{field['bearing']:03d}/{field['range_nm']}"
        )

    # Over Las Vegas the civil field wins; military-only sends you to Nellis.
    civil = navaids.nearest_divert(36.08, -115.15, config=CONFIG)
    if (civil or {}).get("id") != "KLAS":
        bad = _fail(bad, f"over McCarran the divert should be KLAS, got {civil}")
    mil = navaids.nearest_divert(
        36.08, -115.15, config={**CONFIG, "divert_categories": ["military"]}
    )
    if (mil or {}).get("id") != "KLSV":
        bad = _fail(bad, f"military-only divert should be KLSV, got {mil}")
    if not bad:
        print("divert — category filter honoured")
    return bad


def agency_split() -> int:
    """C2 and the tanker talk bullseye; radar controllers do not."""
    bad = 0
    for channel in ("blackjack", "bandsaw", "tanker"):
        if not agencies.uses_bullseye(channel):
            bad = _fail(bad, f"{channel} should use bullseye")
    for channel in (
        "departure",
        "approach",
        "control_east",
        "control_west",
        "center",
        "joshua",
        "tower",
    ):
        if agencies.uses_bullseye(channel):
            bad = _fail(bad, f"{channel} should not use bullseye")
    if atc_phrase.atc_position_reference({}) != "navaid":
        bad = _fail(bad, "ATC should default to the navaid reference")
    if atc_phrase.atc_position_reference({"atc_position_reference": "off"}) != "off":
        bad = _fail(bad, "atc_position_reference should be overridable")
    if not bad:
        print("agency split — bullseye for C2/tanker, navaid for radar ATC")
    return bad


def stale_position() -> int:
    """
    An old fix is dropped, not spoken.

    flow_state.json keeps the last position across sorties, so without an age
    gate a controller happily reports the ramp the jet left half an hour ago.
    """
    bad = 0
    import time

    ramp = [36.236, -115.034]  # parked at Nellis
    saved = atc_phrase.OWNSHIP_INJECT_PATH.read_bytes() if (
        atc_phrase.OWNSHIP_INJECT_PATH.is_file()
    ) else None
    if saved is not None:
        atc_phrase.OWNSHIP_INJECT_PATH.unlink()
    try:
        fresh = {"ownship_ll": ramp, "ownship_ll_t": time.time()}
        clause = atc_phrase.agency_position_clause(
            CONFIG, agency="departure", callsign=CALLSIGN, state=fresh
        )
        if "nellis" not in clause.lower():
            bad = _fail(bad, f"a fresh fix at the ramp should say Nellis: {clause!r}")

        old = {
            "ownship_ll": ramp,
            "ownship_ll_t": time.time() - atc_phrase.OWNSHIP_FIX_MAX_AGE_S - 60,
        }
        clause = atc_phrase.agency_position_clause(
            CONFIG, agency="departure", callsign=CALLSIGN, state=old
        )
        if clause:
            bad = _fail(bad, f"a stale fix must not be reported, got {clause!r}")
        if atc_phrase.ownship_latlon(CONFIG, callsign=CALLSIGN, state=old) is not None:
            bad = _fail(bad, "ownship_latlon must not serve an expired cached fix")

        # Departure keeps its clearance when the position has to be dropped.
        text = atc_phrase.build_template_text(
            AIRPORT,
            "radar_contact",
            CALLSIGN,
            atc_phrase.Weather(210, 5, 29.92, ""),
            "21R",
            state=old,
            config=CONFIG,
            channel="departure",
        ).lower()
        if "radar contact" not in text or "of nellis" in text:
            bad = _fail(bad, f"stale radar contact should drop the position: {text!r}")
    finally:
        if saved is not None:
            atc_phrase.OWNSHIP_INJECT_PATH.write_bytes(saved)
    if not bad:
        print("stale fix — position dropped rather than reported from memory")
    return bad


def altitude_phrases() -> int:
    """Feet / flight levels for ATC, angels for C2, and the approval band."""
    bad = 0
    cases = (
        ("control_east", 14000, 22000, "descend and maintain one four thousand"),
        ("control_east", 22000, 14000, "climb and maintain flight level two two zero"),
        ("departure", 17000, 17200, "maintain one seven thousand"),
        ("blackjack", 4000, 22000, "descend angels four"),
        ("bandsaw", 24000, 4000, "climb angels two four"),
    )
    for channel, want_ft, now_ft, want in cases:
        text = atc_phrase.build_altitude_change_clearance(
            CALLSIGN,
            agency=channel,
            altitude_ft=want_ft,
            current_ft=now_ft,
            airport=AIRPORT,
        )
        if want not in text.lower():
            bad = _fail(bad, f"{channel} {want_ft} should say {want!r}, got {text!r}")

    unable = atc_phrase.build_altitude_change_unable(
        CALLSIGN, agency="control_east", current_ft=17000, airport=AIRPORT
    )
    if "unable" not in unable.lower() or "one seven thousand" not in unable.lower():
        bad = _fail(bad, f"unable should hold the current altitude, got {unable!r}")

    low, high = atc_phrase.altitude_request_band_ft({}, None)
    if (low, high) != (
        atc_phrase.ALTITUDE_REQUEST_MIN_FT,
        atc_phrase.ALTITUDE_REQUEST_MAX_FT,
    ):
        bad = _fail(bad, f"default band should be the module default, got {low}-{high}")
    narrowed = atc_phrase.altitude_request_band_ft(
        {"altitude_request_min_ft": 5000, "altitude_request_max_ft": 30000}, None
    )
    if narrowed != (5000, 30000):
        bad = _fail(bad, f"config should narrow the band, got {narrowed}")
    if not bad:
        print("altitude — ATC feet/FL, C2 angels, band configurable")
    return bad


def vector_phrases() -> int:
    """ATC vectors; C2 reads out bearing and range."""
    bad = 0
    vector = atc_phrase.build_point_vector_clearance(
        CALLSIGN,
        agency="control_east",
        point_say="Stryk",
        heading_deg=285,
        range_nm=26,
        airport=AIRPORT,
    )
    for want in ("fly heading two eight fife", "stryk", "twenty six miles"):
        if want not in vector.lower():
            bad = _fail(bad, f"point vector should say {want!r}, got {vector!r}")

    advisory = atc_phrase.build_point_bearing_advisory(
        CALLSIGN,
        agency="blackjack",
        point_say="Stryk",
        bearing_deg=285,
        range_nm=26,
        airport=AIRPORT,
    )
    if "fly heading" in advisory.lower():
        bad = _fail(bad, f"C2 must not vector, got {advisory!r}")
    if "bears two eight fife" not in advisory.lower():
        bad = _fail(bad, f"C2 should read bearing, got {advisory!r}")

    divert = atc_phrase.build_divert_vector_clearance(
        CALLSIGN,
        agency="approach",
        field_say="Creech",
        bearing_deg=330,
        range_nm=42,
        airport=AIRPORT,
    )
    if "nearest suitable field is creech" not in divert.lower():
        bad = _fail(bad, f"divert vector should name the field, got {divert!r}")
    if "fly heading tree tree zero" not in divert.lower():
        bad = _fail(bad, f"divert vector should give a heading, got {divert!r}")
    if not bad:
        print("vectors — heading from ATC, bearing/range from C2")
    return bad


def check_in_phrases() -> int:
    """The position clause lands in the ATC check-ins that say radar contact."""
    bad = 0
    where = "two five miles northeast of Mormon Mesa"
    for label, text in (
        ("joshua", atc_phrase.build_joshua_check_in(CALLSIGN, position=where)),
        ("center", atc_phrase.build_center_check_in(CALLSIGN, position=where)),
        ("control", atc_phrase.build_control_check_in(CALLSIGN, position=where)),
    ):
        low = text.lower()
        if "radar contact" not in low or where.lower() not in low:
            bad = _fail(bad, f"{label} check-in should carry the position: {text!r}")
        if "remain this frequency" not in low:
            bad = _fail(bad, f"{label} check-in lost its closer: {text!r}")

    # No position available → the old wording, unchanged.
    bare = atc_phrase.build_control_check_in(CALLSIGN)
    if "radar contact, remain this frequency" not in bare.lower():
        bad = _fail(bad, f"bare Control check-in should be unchanged, got {bare!r}")

    # Approach deliberately does not say radar contact — leave that alone.
    calm = atc_phrase.Weather(
        wind_dir=210, wind_speed_kt=5, altimeter_inhg=29.92, raw=""
    )
    if "radar contact" in atc_phrase.build_approach_recovery(
        AIRPORT,
        CALLSIGN,
        calm,
        "21R",
        distance_nm=22,
    ).lower():
        bad = _fail(bad, "Approach check-in must still not say radar contact")
    if not bad:
        print("check-ins — position added, closers and Approach untouched")
    return bad


def intents() -> int:
    """The new calls fire on the right agencies and stay quiet elsewhere."""
    bad = 0
    # (transcript, channel, phase, expected intent or None)
    cases = (
        ("Nellis Control, Dagger 1, request elevator one four thousand",
         "control_east", "flight", "request_altitude_change"),
        ("Blackjack, Dagger 1, request elevator angels four",
         "blackjack", "flight", "request_altitude_change"),
        ("Bandsaw, Dagger 1, request descent to angels one zero",
         "bandsaw", "flight", "request_altitude_change"),
        ("Departure, Dagger 1, request climb to flight level two two zero",
         "departure", "departure", "request_altitude_change"),
        ("Nellis Control, Dagger 1, request higher altitude",
         "control_east", "flight", "request_altitude_change"),
        # Ground has no scope — stay silent.
        ("Ground, Dagger 1, request elevator one four thousand",
         "ground", "departure", None),
        # Readbacks and Tower's own call are not elevator requests.
        ("Nellis Control, Dagger 1, roger, descend and maintain one two thousand",
         "control_east", "flight", None),
        ("Tower, Dagger 1, request unrestricted climb",
         "tower", "departure", "request_unrestricted_climb"),
        ("Nellis Control, Dagger 1, request vectors to Stryk",
         "control_east", "approach", "request_point_vectors"),
        ("Blackjack, Dagger 1, request bearing to Mormon Mesa",
         "blackjack", "flight", "request_point_vectors"),
        ("Nellis Control, Dagger 1, how far to Beatty",
         "control_east", "flight", "request_point_vectors"),
        # A name we cannot place still answers on the agency addressed, rather
        # than falling through to an Approach vector.
        ("Nellis Control, Dagger 1, request vectors to Zanzibar",
         "control_east", "flight", "request_point_vectors"),
        # The tanker stays request_tanker's business.
        ("Blackjack, Dagger 1, request vectors to the tanker",
         "blackjack", "flight", "request_tanker"),
        # Divert beats a plain vectors request, on ATC and on C2.
        ("Approach, Dagger 1, request vectors to the nearest divert",
         "approach", "approach", "request_divert"),
        ("Blackjack, Dagger 1, request the closest suitable field",
         "blackjack", "flight", "request_divert"),
        # A bare vectors request is still the approach vector.
        ("Approach, Dagger 1, request vectors",
         "approach", "approach", "request_vectors"),
    )
    for transcript, channel, phase, want in cases:
        ev = voice_intent.evaluate(
            transcript,
            channel=channel,
            phase=phase,
            callsign=CALLSIGN,
            runways=["21L", "21R"],
        )
        got = ev.match.intent if ev.match else None
        if got != want:
            bad = _fail(
                bad, f"{transcript!r} on {channel} should be {want}, got {got}"
            )

    # The altitude lands in a slot, and a named point resolves at score time.
    ev = voice_intent.evaluate(
        "Nellis Control, Dagger 1, request elevator one four thousand",
        channel="control_east",
        phase="flight",
        callsign=CALLSIGN,
    )
    if (ev.match.slots or {}).get("altitude_ft") != 14000:
        bad = _fail(bad, f"elevator slot should be 14000, got {ev.match.slots}")
    ev = voice_intent.evaluate(
        "Nellis Control, Dagger 1, request vectors to Stryk",
        channel="control_east",
        phase="approach",
        callsign=CALLSIGN,
    )
    point = (ev.match.slots or {}).get("nav_point") or {}
    if point.get("id") != "STRYK":
        bad = _fail(bad, f"vector slot should carry STRYK, got {point}")
    if not bad:
        print("intents — elevator, point vectors and divert gated by agency")
    return bad


def main() -> int:
    bad = 0
    for suite in (
        catalog,
        cardinals,
        point_lookup,
        extraction,
        diverts,
        agency_split,
        stale_position,
        altitude_phrases,
        vector_phrases,
        check_in_phrases,
        intents,
    ):
        bad += suite()
    if bad:
        print(f"\n{bad} FAIL")
        return 1
    print("\nnavaids / elevator / vectors — ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
