"""
Turn a Whisper transcript of a pilot radio call into an ATC intent.

Deliberately a scored keyword grammar rather than an LLM: it runs in
microseconds, is debuggable from the Fly log, and never invents a clearance.
Anything below the confidence threshold returns no intent so the app stays
silent rather than guessing.

Two families of intent:
  * step intents  — the pilot call that a scripted flow step answers
                    ("ready to taxi" -> the ground taxi step)
  * request intents — ad-hoc asks that are not on the timeline
                    (winds, runway change, picture)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# Mission timeline phases (not radio agencies). Channel = who you talk to;
# phase = where you are in the sortie (Departure → Flight/airwork → Approach).
MISSION_PHASES: tuple[str, ...] = ("departure", "flight", "approach")
MISSION_PHASE_LABELS: dict[str, str] = {
    "departure": "Departure",
    "flight": "Flight / airwork",
    "approach": "Approach",
}
# Agencies that normally belong in each mission phase (tips + scoring).
CHANNELS_IN_MISSION_PHASE: dict[str, frozenset[str]] = {
    "departure": frozenset({"delivery", "ground", "tower", "departure"}),
    "flight": frozenset({"blackjack", "bandsaw", "ops", "other", "tanker"}),
    "approach": frozenset({"approach", "tower", "ground"}),
}
# Default mission phase when a step's channel is set (tower/ground appear in two).
DEFAULT_MISSION_PHASE_FOR_CHANNEL: dict[str, str] = {
    "delivery": "departure",
    "ground": "departure",
    "tower": "departure",
    "departure": "departure",
    "blackjack": "flight",
    "bandsaw": "flight",
    "ops": "flight",
    "other": "flight",
    "tanker": "flight",
    "approach": "approach",
}

# Legacy agency-as-phase names still seen in older flows / tests.
_LEGACY_PHASE_AS_CHANNEL = frozenset(DEFAULT_MISSION_PHASE_FOR_CHANNEL)


def normalize_mission_phase(phase: str = "", channel: str = "") -> str:
    """
    Coerce to departure|flight|approach.

    Accepts the new mission-phase names, or legacy values where `phase` was the
    radio agency (ground/tower/blackjack/…).
    """
    p = (phase or "").strip().lower()
    if p in MISSION_PHASES:
        return p
    if p in _LEGACY_PHASE_AS_CHANNEL:
        return DEFAULT_MISSION_PHASE_FOR_CHANNEL[p]
    ch = (channel or "").strip().lower()
    if ch in DEFAULT_MISSION_PHASE_FOR_CHANNEL:
        return DEFAULT_MISSION_PHASE_FOR_CHANNEL[ch]
    return ""


def default_mission_phase_for_channel(channel: str) -> str:
    ch = (channel or "").strip().lower()
    return DEFAULT_MISSION_PHASE_FOR_CHANNEL.get(ch, "departure")


def channel_allowed_in_mission_phase(channel: str, mission_phase: str) -> bool:
    ch = (channel or "").strip().lower()
    phase = normalize_mission_phase(mission_phase, channel=ch)
    allowed = CHANNELS_IN_MISSION_PHASE.get(phase)
    if not allowed:
        return True
    return ch in allowed


def resolve_context_channel(
    *,
    mission_phase: str,
    cursor_channel: str,
    tuned_channel: str | None,
) -> str:
    """
    Agency for voice scoring.

    Prefer the live radio tune when it is an agency that belongs in this mission
    phase (e.g. Blackjack vs Bandsaw during Flight). Otherwise keep the cursor.
    """
    cursor = (cursor_channel or "").strip().lower()
    tuned = (tuned_channel or "").strip().lower()
    phase = normalize_mission_phase(mission_phase, channel=cursor)
    if tuned and channel_allowed_in_mission_phase(tuned, phase):
        return tuned
    return cursor


def cue_channel(
    *,
    mission_phase: str,
    cursor_channel: str,
    tuned_channel: str | None,
) -> str:
    """
    Agency the Fly tip should address — who you are calling next.

    Departure / Approach follow the next step (Ground taxi is Ground even if
    the radio is still on Delivery). Flight follows the live tune so Blackjack
    vs Bandsaw matches the radio.
    """
    cursor = (cursor_channel or "").strip().lower()
    tuned = (tuned_channel or "").strip().lower()
    phase = normalize_mission_phase(mission_phase, channel=cursor)
    if phase == "flight":
        return resolve_context_channel(
            mission_phase=phase,
            cursor_channel=cursor,
            tuned_channel=tuned,
        )
    return cursor or resolve_context_channel(
        mission_phase=phase,
        cursor_channel=cursor,
        tuned_channel=tuned,
    )


# Spoken aviation digits -> characters. Whisper writes words, ATC needs numbers.
_DIGIT_WORDS = {
    "zero": "0", "nought": "0",
    "one": "1", "wun": "1",
    "two": "2", "tu": "2",
    "three": "3", "tree": "3",
    "four": "4", "fower": "4",
    "five": "5", "fife": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9", "niner": "9",
}

# Homophones that are ordinary English far more often than they are digits
# ("ready for departure"). Only converted when they sit next to another digit,
# so "runway two one" survives a mishearing as "runway to one".
_AMBIGUOUS_DIGIT_WORDS = {
    "oh": "0", "o": "0",
    "won": "1",
    "to": "2", "too": "2",
    "thee": "3",
    "for": "4", "fore": "4",
    "sicks": "6",
    "ate": "8",
}

_SIDE_WORDS = {"left": "L", "right": "R", "center": "C", "centre": "C", "central": "C"}

_RECOVERY_TERMS = {
    "tactical_overhead": ("tactical overhead", "tac overhead", "tactical"),
    "overhead": ("overhead", "over head", "visual overhead"),
    "straight_in": ("straight in", "straight-in", "straight end"),
    "instrument": ("instrument", "ils", "tacan approach", "precision", "hi ils"),
}

# VFR patterns Tower will approve. Instrument / named IAFs stay on Approach.
_TOWER_RECOVERY_KEYS = frozenset(
    {"visual_overhead", "tactical_overhead", "straight_in"}
)

# NellisAFBI 11-250 §4.13.5 VFR recoveries (Whisper-tolerant).
# TORYE and ARCOE are separate; ACTON maps to TORYE (legacy / Elgin name).
_VFR_RECOVERY_TERMS = {
    "STRYK": ("stryk", "strike", "stryker"),
    "TORYE": ("torye", "tory", "torie", "acton", "action"),
    "ARCOE": ("arcoe", "arco", "rco", "our co"),
    "MINTT": ("mintt", "mint", "minute"),
}

# Published KLSV IAFs (CIFP) — each belongs to a specific plate.
_IAF_TERMS = {
    "DUDBE": ("dudbe", "dud be", "dead bee", "deadbe"),
    "ARCOE": ("arcoe", "arco"),
    "KRYSS": ("kryss", "kriss", "chris"),
    "SHEET": ("sheet", "sheat"),
    "ZAPVO": ("zapvo", "zap vo"),
    "JEGET": ("jeget", "jegget"),
    "HULPU": ("hulpu", "hul pu"),
    "KUTME": ("kutme", "kut me"),
    "LUCIL": ("lucil", "lucille"),
    "HUSTS": ("husts", "hustz"),
}


def normalize(text: str) -> str:
    """
    Lowercase, strip punctuation, and collapse spoken digits into numbers.

    'runway two one left' -> 'runway 21 left'. Applied to both transcripts and
    intent patterns so the two always agree.
    """
    lowered = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())
    tokens = [t for t in lowered.split() if t]

    # Pass 1: unambiguous digit words and literal numbers.
    staged: list[tuple[str, bool]] = []  # (token, is_digit)
    for token in tokens:
        if token in _DIGIT_WORDS:
            staged.append((_DIGIT_WORDS[token], True))
        elif token.isdigit():
            staged.append((token, True))
        else:
            staged.append((token, False))

    # Pass 2: homophones only count as digits beside a real digit.
    for i, (token, is_digit) in enumerate(staged):
        if is_digit or token not in _AMBIGUOUS_DIGIT_WORDS:
            continue
        before = staged[i - 1][1] if i > 0 else False
        after = staged[i + 1][1] if i + 1 < len(staged) else False
        if before or after:
            staged[i] = (_AMBIGUOUS_DIGIT_WORDS[token], True)

    # Pass 3: merge digit runs so "2 1" becomes "21".
    out: list[str] = []
    run: list[str] = []
    for token, is_digit in staged:
        if is_digit:
            run.append(token)
            continue
        if run:
            out.append("".join(run))
            run.clear()
        out.append(token)
    if run:
        out.append("".join(run))
    return _fold_eor(" ".join(out))


# Whisper almost never writes the acronym "eor". Pilots say the letters;
# the model writes "E or", "e o r", "ee or", "igor", …
_EOR_FOLD: tuple[tuple[str, str], ...] = (
    (r"(?<!\w)e\s+o\s+r(?!\w)", "eor"),
    (r"(?<!\w)ee\s+o\s+r(?!\w)", "eor"),
    (r"(?<!\w)ee\s+or(?!\w)", "eor"),
    (r"(?<!\w)e\s+or(?!\w)", "eor"),
    (r"(?<!\w)he\s+or(?!\w)", "eor"),
    (r"(?<!\w)igor(?!\w)", "eor"),
    (r"(?<!\w)eore(?!\w)", "eor"),
    (r"(?<!\w)eorr(?!\w)", "eor"),
    (r"(?<!\w)aor(?!\w)", "eor"),
)


def _fold_eor(text: str) -> str:
    """Collapse Whisper's letter-spellings of EOR into the token 'eor'."""
    folded = text
    for pat, repl in _EOR_FOLD:
        folded = re.sub(pat, repl, folded)
    return folded


def extract_runway(text: str, known: list[str] | None = None) -> str | None:
    """
    Runway from a normalized transcript: 'runway 21 left' -> '21L'.

    Whisper often renders '21' as '2 1', so digit runs are already joined by
    normalize(). When `known` is supplied the result must be a real runway.
    """
    # Alternation is ordered so 'left' wins over the bare 'l' of '21l'.
    match = re.search(
        r"\b(?:rwy|runway)\s+(\d{1,3})\s*(left|right|center|centre|l|r|c)?\b", text
    )
    if not match:
        return None
    digits, side = match.group(1), (match.group(2) or "")

    if len(digits) > 2:
        digits = digits[:2]
    number = digits.zfill(2)
    suffix = _SIDE_WORDS.get(side, side.upper() if side else "")
    candidate = f"{number}{suffix}"

    if known:
        options = {r.upper(): r for r in known}
        if candidate.upper() in options:
            return options[candidate.upper()]
        # Side omitted or misheard — fall back to runways with that number.
        siblings = [r for r in known if re.match(rf"^0*{int(number)}[LRC]?$", r.upper())]
        if len(siblings) == 1:
            return siblings[0]
        if siblings:
            return siblings[0]  # ambiguous; ATC states the runway in its reply
        return None
    return candidate


# Whisper often mangles Bandsaw (ANSA, and saw, bansaw, …). Keep aliases shared
# for agency detection and "request Bandsaw" intents.
_BANDSAW_TERMS: tuple[str, ...] = (
    "bandsaw",
    "band saw",
    "band-saw",
    "ansa",
    "and saw",
    "an saw",
    "bansaw",
    "bansah",
    "ban saw",
    "ban sah",
    "bandsa",
    "bands aw",
    "bands all",
    "bands off",
    "band soft",
    "bands of",
    "pantsaw",
    "pant saw",
    "mansaw",
    "van saw",
    "band sawyer",
    "band sore",
)

# Radio discipline puts the agency first ("Nellis Tower, Fleece one, ..."), so
# only the opening tokens are searched. That keeps "ready for departure" from
# being read as a call to Departure.
_AGENCY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("delivery", ("clearance delivery", "delivery")),
    ("ground", ("ground",)),
    ("tower", ("tower",)),
    ("approach", ("approach", "arrival")),
    ("departure", ("departure", "dep")),
    ("blackjack", ("blackjack", "black jack", "magic", "darkstar", "awacs")),
    ("bandsaw", _BANDSAW_TERMS),
    ("ops", ("ops", "operations", "base ops")),
    ("tanker", ("texaco", "shell", "arco", "esso", "tanker")),
    ("other", ("center", "centre", "control")),
)

_ADDRESS_TOKEN_WINDOW = 6


def extract_channel(text: str) -> str | None:
    """Agency the pilot addressed — earliest match in the opening words."""
    head = " ".join(text.split()[:_ADDRESS_TOKEN_WINDOW])
    if not head:
        return None
    best: str | None = None
    best_pos = len(head) + 1
    for channel, terms in _AGENCY_TERMS:
        for term in terms:
            m = re.search(rf"(?<!\w){re.escape(term)}(?!\w)", head)
            if m and m.start() < best_pos:
                best_pos = m.start()
                best = channel
    return best


# ---- who the transmission is actually for -------------------------------
#
# The PTT is shared with the rest of the flight, so most of what we hear is
# not ATC business at all. Deciding *who* was called is what keeps "Two, go
# button five" from taxiing the jet.

# A bare position call opens a transmission to a wingman, never to ATC.
_POSITION_WORDS = frozenset({"2", "3", "4", "5", "6", "lead", "dash"})

# Phrases that are only ever said inside the flight or on a range frequency.
_CREW_ONLY_TERMS = (
    "go button", "push button", "please push", "button up", "chattermark", "switch button",
    "fence in", "fence out", "fence check", "green em up",
    "tally", "no joy", "padlocked", "master arm", "ops check",
    "fox 1", "fox 2", "fox 3", "guns guns", "rifle", "magnum",
    "music on", "music off", "naked", "spike", "singer", "mud",
    "bingo", "joker", "knock it off",
    "combat spread", "line abreast", "route formation",
)

# Ambiguous: normal inside the flight, but also legitimate to ATC
# ("Tower, Fleece 1, visual"). Only suppresses when no agency was addressed.
_CREW_LIKELY_TERMS = (
    "visual", "blind", "terminate", "cleared hot", "off target",
    "playtime", "fuel state", "say state", "anchor", "defending",
)

# Thinking out loud about a call is not making the call.
_CONVERSATIONAL_TERMS = (
    "we should", "we could", "should we", "let s", "lets",
    "i m going to", "i m gonna", "gonna ask", "going to ask",
    "we can ask", "we ll ask", "i ll ask", "maybe we", "what if we",
    "do you want", "you want me to", "i think we", "remind me",
)


@dataclass(frozen=True)
class Address:
    """Who the pilot opened the transmission to."""

    agency: str | None = None
    own_callsign: bool = False
    flight_member: bool = False

    @property
    def to_atc(self) -> bool:
        return self.agency is not None


def _split_callsign(normalized: str) -> tuple[str, str]:
    """'fleece 1' -> ('fleece', '1'). Number is '' when the callsign has none."""
    word = ""
    number = ""
    for token in normalized.split():
        if token.isdigit():
            if not number:
                number = token
        elif not word:
            word = token
    return word, number


def _flight_and_ship(number: str) -> tuple[str, str]:
    """
    Split a flight number into (flight, ship).

    '1' → ('1', '1') — bare flight number is the lead.
    '11' → ('1', '1') — Whisper '1.1' / '1-1' after normalize merges to 11.
    '12' → ('1', '2') — Fleece 1.2, a wingman.
    """
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return "", "1"
    if len(digits) >= 2:
        return digits[:-1], digits[-1]
    return digits, "1"


def _callsign_number_role(said: str | None, ours: str) -> str:
    """
    'own' if the spoken number is us, 'member' if it is someone else.

    Element forms (1.1 / 1-1 → 11) are the same flight as 'Fleece 1'.
    Ship 1 is the lead; 1.2 / 12 is a wingman.
    """
    if said is None:
        return "own"
    if not ours:
        return "own"
    if said == ours:
        return "own"
    our_flight, our_ship = _flight_and_ship(ours)
    said_flight, said_ship = _flight_and_ship(said)
    if not our_flight or our_flight != said_flight:
        return "member"
    return "own" if said_ship == our_ship else "member"


def _callsign_word_forms(word: str) -> tuple[str, ...]:
    """Configured callsign stem plus common Whisper near-misses."""
    w = (word or "").casefold()
    if not w:
        return ()
    safe = {
        "fleece": ("lease", "fleese", "fleas", "fleace"),
        "bruiser": ("browser", "brewer"),
    }
    return (w,) + safe.get(w, ())


def _address_search_heads(normalized: str, raw: str = "") -> tuple[str, ...]:
    """
    Opening words used to decide who was called.

    Normalized text merges '1.1' → '11'. Also keep a lightly cleaned raw
    head so 'Fleece 1.1' / '1-1' stay visible as flight + ship.
    """
    heads: list[str] = []
    if normalized:
        heads.append(" ".join(normalized.split()[: max(_ADDRESS_TOKEN_WINDOW, 8)]))
    if raw:
        light = re.sub(r"[^a-z0-9.\-\s]", " ", raw.lower())
        light = re.sub(r"\s+", " ", light).strip()
        if light and light not in heads:
            heads.append(" ".join(light.split()[: max(_ADDRESS_TOKEN_WINDOW, 8)]))
    return tuple(heads)


def analyze_address(text: str, callsign: str = "", raw: str = "") -> Address:
    """
    Work out who a transmission was addressed to.

    `callsign` is our own ("FLEECE 1" or "FLEECE 1-1"). Element forms
    (1.1 / 1-1) are the lead, not a wingman. If we are Fleece 2 then
    "Fleece 2" is us and "Fleece 3" is someone else.
    """
    tokens = text.split()
    if not tokens:
        return Address()

    agency = extract_channel(text)
    own = False
    member = False

    word, number = _split_callsign(normalize(callsign))
    # Configured 'Fleece 1-1' / '1.1' → flight 1 ship 1.
    raw_cs = str(callsign or "").strip()
    elem = re.match(r"^(.+?)\s*[-.]\s*(\d+)$", raw_cs)
    if elem and not number:
        word, number = _split_callsign(normalize(elem.group(1)))
        if not number:
            number = re.sub(r"\D", "", elem.group(2) or "")

    cs_pat = rf"(?<!\w){{form}}(?:\s+(\d+)(?:\s*[.\-]\s*(\d+))?)?(?!\w)"
    if word:
        for form in _callsign_word_forms(word):
            pat = cs_pat.format(form=re.escape(form))
            for head in _address_search_heads(text, raw):
                for hit in re.finditer(pat, head):
                    said = hit.group(1)
                    ship = hit.group(2)
                    if ship and said:
                        said = f"{said}{ship}"
                    if not number:
                        own = True
                        continue
                    if _callsign_number_role(said, number) == "own":
                        own = True
                    else:
                        member = True
            if any(
                re.search(rf"(?<!\w){re.escape(form)}\s+(?:flight|formation)(?!\w)", head)
                for head in _address_search_heads(text, raw)
            ):
                own = True

    # "Two, ..." / "Dash three, ..." — a call across the formation.
    if tokens[0] in _POSITION_WORDS:
        if tokens[0] == "dash":
            member = len(tokens) > 1 and tokens[1] in _POSITION_WORDS
        else:
            member = True

    # We tagged ourselves — this is our radio call, not a wingman brief.
    if own:
        member = False

    return Address(agency=agency, own_callsign=own, flight_member=member)


def _first_term(text: str, terms: tuple[str, ...]) -> str | None:
    """Literal only — a fuzzy hit here would silently swallow a genuine call."""
    for term in terms:
        pattern = _PATTERN_CACHE.get((term,))
        if pattern is None:
            pattern = _normalized_options((term,))
            _PATTERN_CACHE[(term,)] = pattern
        for option in pattern:
            if re.search(rf"(?<!\w){re.escape(option)}(?!\w)", text):
                return term
    return None


def extract_recovery(text: str) -> str | None:
    for key, terms in _RECOVERY_TERMS.items():
        if any(term in text for term in terms):
            if key == "overhead":
                return "visual_overhead"
            return key
    return None


def extract_vfr_recovery(text: str) -> str | None:
    """Named VFR recovery (STRYK / TORYE / ARCOE / MINTT)."""
    for key, terms in _VFR_RECOVERY_TERMS.items():
        if _group_hit(text, terms, fuzzy=True):
            return key
    return None


def extract_iaf(text: str) -> str | None:
    for key, terms in _IAF_TERMS.items():
        if _group_hit(text, terms, fuzzy=True):
            return key
    return None


@dataclass(frozen=True)
class Intent:
    """
    `groups` is a list of alternative-sets; every group needs at least one hit.
    That keeps 'request winds' from matching a bare 'request'.
    """

    id: str
    groups: tuple[tuple[str, ...], ...]
    kind: str = "request"  # "request" | "step"
    template: str = ""  # flow step template this answers, for kind="step"
    channels: tuple[str, ...] = ()  # radio agencies this makes sense on
    phases: tuple[str, ...] = ()  # mission phases: departure|flight|approach; () = any
    veto: tuple[str, ...] = ()
    weight: float = 1.0
    example: str = ""  # how a pilot would say it, shown as a prompt in Fly
    does: str = ""  # what answering it will do
    step_id: str = ""  # set for phrases defined on a specific flow step


# Every way a pilot asks for something. Shared so adding a phrasing here fixes
# every request at once rather than one intent at a time.
_ASKING = (
    "request", "requesting", "say", "give", "check", "confirm",
    "like", "we d like", "need", "prefer", "how about", "what s", "what is",
    "can we get", "could we get", "can i get", "can you", "could you",
)

# Multiplier for an intent that does not fit the agency or the stage of flight.
# Kept under the default min-confidence so off-context calls stay silent.
_OFF_CONTEXT = 0.55

INTENTS: tuple[Intent, ...] = (
    # ---- ad-hoc requests -------------------------------------------------
    Intent(
        "request_runway",
        (_ASKING, ("runway",)),
        veto=("cleared for", "cleared to land", "cleared takeoff"),
        # Ground / Tower — Departure and Approach; not Flight airwork.
        phases=("departure", "approach"),
        weight=1.2,
        example="request runway two one left",
        does="change the active runway",
    ),
    Intent(
        "request_winds",
        (_ASKING + ("how",), ("wind", "winds")),
        example="say winds",
        does="current winds",
    ),
    Intent(
        "request_altimeter",
        (_ASKING, ("altimeter", "qnh")),
        example="say altimeter",
        does="current altimeter",
    ),
    Intent(
        "request_picture",
        (("picture", "pitcher"),),
        channels=("blackjack", "bandsaw", "ops", "other"),
        phases=("flight",),
        weight=1.2,
        example="request picture",
        does="hostile groups off the live radar",
    ),
    Intent(
        "request_bogey_dope",
        (("bogey dope", "bogie dope", "braa", "snaplock"),),
        channels=("blackjack", "bandsaw", "ops", "other"),
        phases=("flight",),
        weight=1.25,
        example="bogey dope",
        does="BRAA to the closest hostile",
    ),
    Intent(
        "request_declare",
        (("declare",),),
        channels=("blackjack", "bandsaw", "ops", "other"),
        phases=("flight",),
        weight=1.25,
        example="declare bullseye 056 67",
        does="declaration at that bullseye (ELVIS); bare declare = nearest",
    ),
    Intent(
        "request_alpha_check",
        (("alpha check", "alfa check", "position check"),),
        channels=("blackjack", "bandsaw", "ops", "other"),
        phases=("flight",),
        example="alpha check bullseye",
        does="your position off bullseye",
    ),
    Intent(
        "request_bandsaw",
        (
            _ASKING + ("push", "go"),
            _BANDSAW_TERMS,
        ),
        kind="request",
        channels=("blackjack",),
        phases=("flight",),
        weight=1.15,
        # Optional push — keep off Blackjack YOU CAN SAY.
        example="",
        does="",
    ),
    Intent(
        "request_tanker",
        (
            _ASKING + ("push", "go", "going"),
            ("tanker", "texaco", "air refuel", "air refueling", "aar"),
        ),
        veto=(
            "tacan",
            "frequency",
            "freq",
            "bullseye",
            "back from",
            "off the tanker",
            "off tanker",
            "returning from",
            "done with",
        ),
        kind="request",
        channels=("blackjack", "bandsaw", "ops", "other"),
        phases=("flight",),
        weight=1.2,
        example="request tanker",
        does="track and BRAA to the Opus KC-135",
    ),
    Intent(
        "tanker_return",
        (
            (
                "back from the tanker",
                "off the tanker",
                "done with the tanker",
                "returning from the tanker",
                "check back in",
                "checking back in",
                "back from tanker",
                "off tanker",
            ),
        ),
        kind="request",
        channels=("blackjack", "bandsaw", "ops"),
        phases=("flight",),
        weight=1.25,
        example="back from the tanker",
        does="check back in after AAR",
    ),
    Intent(
        "tanker_tacan",
        (_ASKING, ("tacan", "tanker tacan", "tanker channel")),
        kind="request",
        channels=("blackjack", "bandsaw", "ops", "other"),
        phases=("flight",),
        weight=1.3,
        example="say TACAN",
        does="published tanker TACAN",
    ),
    Intent(
        "tanker_freq",
        (_ASKING, ("tanker frequency", "tanker freq", "frequency")),
        kind="request",
        channels=("blackjack", "bandsaw", "ops", "other"),
        phases=("flight",),
        weight=1.3,
        example="say tanker frequency",
        does="published tanker UHF",
    ),
    Intent(
        "tanker_bullseye",
        (_ASKING, ("tanker bullseye", "bullseye")),
        veto=("alpha check", "alfa check", "declare"),
        kind="request",
        channels=("blackjack", "bandsaw", "ops", "other"),
        phases=("flight",),
        weight=1.15,
        example="say tanker bullseye",
        does="live tanker bullseye",
    ),
    Intent(
        "tanker_check_in",
        (
            (
                "request rejoin",
                "request reform",
                "request the rejoin",
                "request the reform",
                "cleared rejoin",
                "rejoin left",
                "reform left",
                "checking in",
                "with you",
            ),
        ),
        kind="request",
        channels=("tanker",),
        phases=("flight",),
        weight=1.25,
        example="request rejoin",
        does="cleared rejoin left (sometimes left observation)",
    ),
    Intent(
        "tanker_astern",
        (
            (
                "astern",
                "pre contact",
                "precontact",
                "pre-contact",
                "left observation",
                "observation",
                "on the left",
                "echelon left",
            ),
        ),
        kind="request",
        channels=("tanker",),
        phases=("flight",),
        weight=1.2,
        example="astern",
        does="use DCS Ready pre-contact (cleared contact)",
    ),
    Intent(
        "tanker_contact",
        (("contact", "in contact", "boom contact", "cleared contact"),),
        veto=("contact tower", "contact approach", "contact blackjack", "contact ground"),
        kind="request",
        channels=("tanker",),
        phases=("flight",),
        weight=1.15,
        example="cleared contact",
        does="use DCS tanker radio for cleared contact",
    ),
    Intent(
        "tanker_disconnect",
        (("disconnect", "request disconnect", "coming off", "abort refueling"),),
        kind="request",
        channels=("tanker",),
        phases=("flight",),
        weight=1.2,
        example="disconnect",
        does="use DCS Abort refueling",
    ),
    Intent(
        "tanker_depart",
        (("request departure", "cleared to depart", "done with the tanker"),),
        kind="request",
        channels=("tanker",),
        phases=("flight",),
        weight=1.15,
        example="request departure",
        does="use DCS tanker radio to leave",
    ),
    Intent(
        "tanker_chat_start",
        (
            (
                "how's it going",
                "hows it going",
                "how you doing",
                "how are you",
                "you busy",
                "pretty quiet",
                "what's for lunch",
                "whats for lunch",
                "shoot the breeze",
                "small talk",
            ),
        ),
        kind="request",
        channels=("tanker",),
        phases=("flight",),
        weight=1.05,
        example="how's it going",
        does="boom / reform small talk",
    ),
    Intent(
        "say_again",
        (("say again", "repeat", "come again", "one more time", "didn t copy", "did not copy"),),
        example="say again",
        does="replay the last transmission",
    ),
    Intent(
        "accept_rolling",
        (
            ("rolling", "roll"),
            ("take", "accept", "affirm", "affirmative", "roger", "we ll", "will", "ready"),
        ),
        channels=("tower",),
        phases=("departure",),
        veto=("unable", "negative", "full length"),
        example="we'll take the rolling",
        does="accept a rolling takeoff",
    ),
    Intent(
        "deny_rolling",
        (("rolling", "roll"), ("unable", "negative", "rather not", "full length")),
        channels=("tower",),
        phases=("departure",),
        weight=1.1,
        example="unable rolling",
        does="decline the rolling takeoff",
    ),
    Intent(
        "request_rolling",
        (
            ("rolling", "roll", "rolling takeoff"),
            (
                "request",
                "requesting",
                "like",
                "want",
                "prefer",
                "can we",
                "can i",
                "able",
            ),
        ),
        channels=("tower",),
        phases=("departure",),
        veto=("unable", "negative", "full length", "line up", "lineup"),
        example="request rolling",
        does="request a rolling takeoff",
    ),
    Intent(
        "request_lineup",
        (
            ("line up", "lineup", "position and hold"),
            ("request", "requesting", "ready", "like"),
        ),
        channels=("tower",),
        phases=("departure",),
        example="ready for line up",
        does="line up and wait",
    ),
    # Hidden kneeboard call — Tower only, before takeoff clearance.
    Intent(
        "request_unrestricted_climb",
        (
            _ASKING,
            ("unrestricted climb", "unrestricted", "unrestricted climb please"),
        ),
        channels=("tower",),
        phases=("departure",),
        weight=1.2,
        example="",
        does="",
        veto=("unable unrestricted", "unable the unrestricted"),
    ),
    # ---- scripted step triggers -----------------------------------------
    Intent(
        "ready_clearance",
        (
            ("clearance", "ifr"),
            ("request", "requesting", "ready", "like", "copy"),
        ),
        kind="step",
        template="clearance",
        channels=("delivery",),
        phases=("departure",),
        example="request clearance",
        does="IFR clearance",
        veto=("readback", "squawk", "as filed"),
    ),
    # After ATC issues a clearance the pilot reads the key items back — no
    # agency opener required while awaiting_readback is set.
    Intent(
        "acknowledge_readback",
        (
            (
                # Short acks for any readback window. Clearance specifically
                # Clearance also matches via _heard_assigned_squawk (squawk /
                # squawking CODE, or the bare code while the hinge is open).
                "roger",
                "wilco",
                "copy",
                "readback",
                "read back",
                "as filed",
                "cleared to",
                "cleared via",
                "climb and maintain",
                "climb via",
                "climb as published",
                "expect flight level",
                "expect",
            ),
        ),
        kind="step",
        template="clearance_readback",
        # Deliberately not pinned to an agency or phase: taxi, takeoff and
        # landing clearances all open a readback window too. Only reachable
        # while one is outstanding.
        weight=1.25,
        example="squawk zero five five one",
        does="confirm your clearance readback",
        veto=("ready to copy", "request clearance", "requesting clearance"),
    ),
    Intent(
        "ready_taxi",
        (("taxi",), ("request", "requesting", "ready", "like")),
        kind="step",
        template="taxi",
        channels=("ground",),
        phases=("departure",),
        veto=("taxi in", "clear of the", "off the active"),
        example="request taxi",
        does="taxi clearance",
    ),
    Intent(
        "at_eor",
        (
            (
                "eor",
                "at eor",
                "at the eor",
                "ready at eor",
                "parked eor",
                "parked at eor",
                "holding eor",
                "holding at eor",
                "holding short",
                "number 1 holding short",
                "number one holding short",
                "at the end",
                "at the end of the runway",
                "end of runway",
                "end of the runway",
                "the end of runway",
                "at end of runway",
                "we re at eor",
                "we are at eor",
                "holding short of the runway",
                "number one at eor",
                "alpha south",
                "at alpha south",
                "northwest eor",
                "at northwest eor",
                "nw eor",
            ),
        ),
        kind="step",
        template="monitor_tower",
        channels=("ground",),
        phases=("departure",),
        example="at EOR",
        does="monitor tower",
        veto=("taxi", "request taxi", "ready to taxi"),
        weight=1.15,
    ),
    Intent(
        "ready_departure",
        (
            ("ready", "number 1", "holding short", "request", "requesting"),
            ("departure", "takeoff", "take off", "the active", "to go", "for the go"),
        ),
        kind="step",
        # LUAW / rolling offer only — takeoff clearance is the in-position zone
        # (or Play), not a second "ready for departure".
        template="lineup",
        channels=("tower",),
        phases=("departure",),
        veto=("contact departure", "with departure", "rolling", "in position", "lined up"),
        example="ready for departure",
        does="line up and wait",
    ),
    # Zone / Play is the primary takeoff trigger; this is the voice fallback.
    Intent(
        "in_position",
        (
            (
                "in position",
                "in-position",
                "we're in position",
                "we are in position",
                "number one in position",
                "number 1 in position",
                "lined up",
                "we're lined up",
                "we are lined up",
                "on the numbers",
            ),
        ),
        kind="step",
        template="clear_takeoff",
        channels=("tower",),
        phases=("departure",),
        veto=("remain", "holding short", "rolling"),
        weight=1.2,
        example="in position",
        does="cleared for takeoff",
    ),
    Intent(
        "inbound_recovery",
        (
            (
                "inbound",
                "recovery",
                "recover",
                "rtb",
                "checking in",
                "with you",
                "check in",
            ),
            ("request", "requesting", "for", "with you", "checking in", "inbound", "nellis"),
        ),
        kind="step",
        template="approach_check_in",
        channels=("approach", "departure"),
        phases=("flight", "approach"),
        example="checking in",
        does="Approach check-in / recovery assignment",
        veto=("request hold", "holding", "vectors", "cancel hold"),
    ),
    Intent(
        "request_approach",
        (
            (
                "request",
                "requesting",
                "want",
                "prefer",
                "change to",
                "switch to",
                "able",
            ),
            (
                "overhead",
                "tactical",
                "straight in",
                "straight-in",
                "instrument",
                "ils",
                "stryk",
                "torye",
                "acton",
                "arcoe",
                "mintt",
                "dudbe",
                "recovery",
            ),
        ),
        kind="action",
        channels=("approach", "tower"),
        phases=("flight", "approach"),
        weight=1.15,
        example="request overhead",
        does="request different recovery / approach",
        veto=(
            "checking in",
            "with you",
            "bogey dope",
            "picture",
            "hold",
            "holding",
            "vector",
            "vectors",
            "altimeter",
            "winds",
        ),
    ),
    Intent(
        "request_hold",
        (
            ("request hold", "need to hold", "holding at", "hold at"),
            ("hold", "holding"),
        ),
        kind="action",
        channels=("approach",),
        phases=("approach", "flight"),
        weight=1.3,
        example="request hold",
        does="hold clearance",
        veto=("hold short", "cancel hold", "leave hold", "holding short"),
    ),
    Intent(
        "cancel_hold",
        (
            ("cancel hold", "leave hold", "leaving hold", "done holding", "outbound"),
        ),
        kind="action",
        channels=("approach",),
        phases=("approach", "flight"),
        example="cancel hold",
        does="leave hold / continue recovery",
    ),
    Intent(
        "request_vectors",
        (
            ("request vectors", "need vectors", "vectors to", "vector to"),
            ("vectors", "vector"),
        ),
        kind="action",
        channels=("approach",),
        phases=("approach", "flight"),
        weight=1.3,
        example="request vectors",
        does="radar vectors",
    ),
    Intent(
        "approach_continue",
        (
            (
                "request handoff",
                "requesting handoff",
                "request the handoff",
                "request tower",
                "requesting tower",
                "contact tower",
                "airport in sight",
                "field in sight",
                "for the overhead",
                "request the break",
                "ready for the overhead",
                "proceeding",
            ),
        ),
        kind="step",
        template="cleared_approach",
        channels=("approach",),
        phases=("approach",),
        weight=1.2,
        example="request handoff",
        does="contact tower",
        veto=("checking in", "with you", "established"),
    ),
    Intent(
        "approach_established",
        (
            (
                "established",
                "we're established",
                "we are established",
                "established on the approach",
                "established inbound",
            ),
        ),
        kind="step",
        template="cleared_approach",
        channels=("approach",),
        phases=("approach",),
        weight=1.2,
        example="established",
        does="contact tower",
        veto=("checking in", "with you", "handoff"),
    ),
    Intent(
        "request_landing",
        (
            ("gear down", "full stop", "landing"),
            ("request", "requesting", "for", "with", "gear", "full"),
        ),
        kind="step",
        template="clear_land",
        channels=("tower",),
        phases=("approach",),
        example="gear down full stop",
        does="landing clearance",
        veto=("low approach", "the option", "low pass", "initial", "with you"),
    ),
    Intent(
        "tower_check_in",
        (
            (
                "with you",
                "with tower",
            ),
        ),
        kind="step",
        template="right_break",
        channels=("tower",),
        phases=("approach",),
        weight=1.2,
        example="with you",
        does="tower check-in",
        veto=("initial",),
    ),
    Intent(
        "tower_initial",
        (
            (
                "initial",
                "at initial",
                "we're initial",
                "we are initial",
                "on initial",
            ),
        ),
        kind="step",
        template="right_break",
        channels=("tower",),
        phases=("approach",),
        weight=1.2,
        example="initial",
        does="tower check-in",
        veto=("with you",),
    ),
    Intent(
        "request_low_approach",
        (
            (
                "low approach",
                "request low approach",
                "requesting low approach",
                "the option",
                "low pass",
            ),
        ),
        kind="action",
        channels=("tower",),
        phases=("approach",),
        weight=1.25,
        example="request low approach",
        does="expect the option (then on the go)",
        veto=("cleared to land", "full stop"),
    ),
    Intent(
        "going_around",
        (
            (
                "going around",
                "go around",
                "waving off",
                "wave off",
                "on the go",
                "we're on the go",
                "we are on the go",
                "on the go tower",
                "missed approach",
                "going missed",
                "we're going missed",
                "we are going missed",
                "executing missed",
                "going missed approach",
                "going round",
                "go ahead and missed",
                "ahead and missed",
                "going a missed",
                "we're missed",
                "we are missed",
                "executing the missed",
            ),
        ),
        kind="action",
        channels=("tower",),
        phases=("approach",),
        weight=1.35,
        example="going around",
        does="go-around / missed approach / pattern reentry",
    ),
    Intent(
        "clear_of_runway",
        (
            ("clear of the runway", "clear of runway", "off the active", "taxi in", "clear the active"),
        ),
        kind="step",
        template="taxi_in",
        channels=("ground", "tower"),
        phases=("approach",),
        weight=1.1,
        example="clear of the runway",
        does="taxi back to parking",
    ),
    Intent(
        "range_entry",
        (
            (
                "checking in",
                "check in",
                "checkin",
                "with you",
                "on frequency",
                "with blackjack",
                "on station",
                "range entry",
                "entering the range",
                "enter the range",
                "for the range",
                "for range entry",
            ),
        ),
        kind="step",
        template="bj_check_in",
        channels=("blackjack",),
        phases=("departure", "flight"),
        weight=1.15,
        # Kneeboard stays short; mission number is optional spoken colour.
        example="checking in",
        does="blackjack check-in",
        # "off station" is one edit from "on station" — do not steal range exit.
        veto=("off station", "range exit", "range complete", "exiting"),
    ),
    Intent(
        "range_exit",
        (("range", "station"), ("exit", "exiting", "departing", "off", "complete", "detached")),
        kind="step",
        template="bj_range_exit",
        channels=("blackjack",),
        phases=("flight",),
        weight=1.2,
        example="off station, range complete",
        does="range exit",
    ),
    Intent(
        "bandsaw_check_in",
        (
            (
                "checking in",
                "check in",
                "checkin",
                "with you",
                "on frequency",
                "with bandsaw",
                "with ansa",
                "with and saw",
            ),
        ),
        kind="step",
        template="bandsaw_check_in",
        channels=("bandsaw",),
        phases=("flight",),
        weight=1.2,
        example="checking in",
        does="bandsaw check-in",
        veto=(
            "checking out",
            "check out",
            "checked out",
            "off frequency",
            "switching",
            "switch blackjack",
            "push blackjack",
            "contact blackjack",
        ),
    ),
    Intent(
        "bandsaw_check_out",
        (
            (
                "checking out",
                "check out",
                "checked out",
                "off frequency",
                "switching to blackjack",
                "switch blackjack",
                "push blackjack",
                "contact blackjack",
                "done with bandsaw",
                "bandsaw complete",
            ),
        ),
        kind="step",
        template="bandsaw_check_out",
        channels=("bandsaw",),
        phases=("flight",),
        weight=1.25,
        example="checking out, switch Blackjack",
        does="bandsaw check-out → Blackjack",
        veto=(
            "checking in",
            "check in",
            "checkin",
            "with you",
            "on station",
        ),
    ),
    # Departure radar contact — airborne check-in (not winds / altimeter).
    Intent(
        "departure_check_in",
        (
            (
                "with you",
                "airborne",
                "we re airborne",
                "we are airborne",
                "checking in",
                "check in",
                "on frequency",
                "radar contact",
                "with departure",
            ),
        ),
        kind="step",
        template="radar_contact",
        channels=("departure",),
        phases=("departure",),
        weight=1.2,
        example="with you",
        does="departure radar contact",
        veto=("say wind", "say winds", "altimeter", "request wind", "request winds"),
    ),
    Intent(
        "request_handoff",
        (
            (
                "request handoff",
                "requesting handoff",
                "request the handoff",
                "request blackjack",
                "switch to blackjack",
                "contact blackjack",
                "push blackjack",
                "handoff please",
            ),
        ),
        kind="step",
        template="departure_handoff",
        channels=("departure",),
        phases=("departure",),
        weight=1.15,
        example="request handoff",
        does="handoff to Blackjack",
    ),
)


@dataclass
class Match:
    intent: str
    kind: str
    template: str
    confidence: float
    slots: dict[str, Any] = field(default_factory=dict)
    transcript: str = ""
    normalized: str = ""
    step_id: str = ""  # exact step to run, when the phrase came from one
    expected: bool = False  # this is the call ATC is waiting on right now
    summarised: bool = False  # heard as a short form of the full call

    def describe(self) -> str:
        extra = " ".join(f"{k}={v}" for k, v in self.slots.items() if v)
        return f"{self.intent}{' ' + extra if extra else ''} ({self.confidence:.0%})"


def parse_phrases(raw: Any) -> tuple[str, ...]:
    """Step phrases from JSON: a list, or one string with newlines / commas."""
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = re.split(r"[\n,;]+", str(raw or ""))
    return tuple(dict.fromkeys(p.strip() for p in parts if p and p.strip()))


def intents_for_template(template: str) -> list[Intent]:
    """
    Built-in step intents that fire a flow step with this template.

    "Ready for departure" answers LUAW only. Cleared takeoff is the
    in-position zone / Play, with "in position" as the voice fallback.
    """
    t = (template or "").strip().lower()
    if not t:
        return []
    out = [i for i in INTENTS if i.kind == "step" and (i.template or "").strip().lower() == t]
    if not out and t in _DEPARTURE_READY_TEMPLATES:
        out = [i for i in INTENTS if i.id == "ready_departure"]
    if t in _TAKEOFF_CLEAR_TEMPLATES:
        extra = [i for i in INTENTS if i.id == "in_position"]
        ids = {i.id for i in out}
        out = out + [i for i in extra if i.id not in ids]
    return out


def format_intent_keywords(intent: Intent) -> str:
    """Human-readable AND-of-OR keyword groups for the Keywords editor."""
    parts: list[str] = []
    for group in intent.groups:
        opts = [str(o).strip() for o in group if str(o).strip()]
        if not opts:
            continue
        if len(opts) == 1:
            parts.append(opts[0])
        else:
            parts.append("(" + " | ".join(opts) + ")")
    return " + ".join(parts)


def format_intent_need_lines(intent: Intent) -> list[str]:
    """One line per required group: you must hit every line, any word on it."""
    lines: list[str] = []
    for group in intent.groups:
        opts = [str(o).strip() for o in group if str(o).strip()]
        if not opts:
            continue
        if len(opts) == 1:
            lines.append(opts[0])
        else:
            lines.append("  or  ".join(opts))
    return lines


def step_advance_keyword_text(
    *,
    template: str = "",
    step: dict[str, Any] | None = None,
) -> str:
    """Read-only Keywords modal copy: built-in AND-groups plus mission phrases."""
    tmpl = (template or "").strip()
    if not tmpl and isinstance(step, dict):
        tmpl = str(step.get("template") or "").strip()
    chunks: list[str] = []
    for intent in intents_for_template(tmpl):
        need = format_intent_need_lines(intent)
        example = (intent.example or "").strip()
        if not need and not example:
            continue
        lines: list[str] = []
        if need:
            lines.append("Need one from each line:")
            for i, line in enumerate(need, start=1):
                lines.append(f"  {i}. {line}")
        if example:
            lines.append(f'e.g. "{example}"')
        chunks.append("\n".join(lines))
    phrases = parse_phrases((step or {}).get("voice_phrases") if step else None)
    if phrases:
        quoted = "\n".join(f'  • "{p}"' for p in phrases)
        chunks.append("Mission phrases (any one):\n" + quoted)
    return "\n\n".join(chunks).strip()


def cue_needs_agency(intent: Intent, *, expected: str = "", awaiting_readback: bool = False) -> bool:
    """True when the Fly tip should show an agency opener as required."""
    _ = (intent, expected)
    return not awaiting_readback


def step_is_authored(step: dict[str, Any] | None) -> bool:
    """True when ATC audio is custom text or a file, not a live template."""
    if not isinstance(step, dict):
        return False
    mode = str(step.get("mode") or "").strip().lower()
    if mode in ("file", "custom"):
        return True
    return bool(str(step.get("text") or "").strip())


# Picture / bogey dope / declare belong on C2 agencies, not Center / transit.
_C2_AGENCIES = frozenset({"blackjack", "bandsaw", "ops"})
_C2_INTENT_IDS = frozenset(
    {
        "request_picture",
        "request_bogey_dope",
        "request_declare",
        "request_alpha_check",
        "request_tanker",
        "tanker_return",
        "tanker_tacan",
        "tanker_freq",
        "tanker_bullseye",
    }
)


def step_offers_c2(step: dict[str, Any] | None, *, channel: str = "") -> bool:
    """
    Whether this step answers picture / bogey dope / declare / alpha check.

    Explicit `c2` on the step wins. Otherwise only Blackjack, Bandsaw, and Ops
    offer those calls — channel `other` (Center, Joshua, …) does not.
    """
    if isinstance(step, dict) and "c2" in step:
        return bool(step.get("c2"))
    ch = ""
    if isinstance(step, dict):
        ch = str(step.get("channel") or "").strip().lower()
    ch = ch or str(channel or "").strip().lower()
    return ch in _C2_AGENCIES


def step_holds_after_play(step: dict[str, Any] | None) -> bool:
    """
    True when Play transmits but leaves the cursor on this step.

    Bandsaw check-in holds until checkout. Custom/file steps with a leftover
    Bandsaw template do not — set `hold: true` to opt back in.
    """
    if not isinstance(step, dict):
        return False
    if "hold" in step:
        return bool(step.get("hold"))
    if step_is_authored(step):
        return False
    return str(step.get("template") or "").strip().lower() == "bandsaw_check_in"


def step_expected_template(step: dict[str, Any] | None) -> str:
    """Template key that drives stock Fly cues. Empty for authored steps."""
    if not isinstance(step, dict) or step_is_authored(step):
        return ""
    return str(step.get("template") or "").strip()


def step_by_id(
    steps: list[dict[str, Any]] | None,
    step_id: str,
) -> dict[str, Any] | None:
    want = str(step_id or "").strip()
    if not want:
        return None
    for step in steps or []:
        if isinstance(step, dict) and str(step.get("id") or "").strip() == want:
            return step
    return None


def step_voice_phrases(
    steps: list[dict[str, Any]] | None,
    step_id: str,
) -> list[str]:
    """Mission voice_phrases on this step, in author order."""
    step = step_by_id(steps, step_id)
    if step is None:
        return []
    return list(parse_phrases(step.get("voice_phrases")))


def step_intents(steps: list[dict[str, Any]] | None) -> tuple[Intent, ...]:
    """
    Extra triggers defined on the flow steps themselves.

    Lets a mission add its own wording for a step without touching the built-in
    grammar. Any one phrase is enough, so these are single-group intents.
    """
    out: list[Intent] = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        phrases = parse_phrases(step.get("voice_phrases"))
        if not phrases:
            continue
        step_id = str(step.get("id") or "").strip()
        out.append(
            Intent(
                id=f"step:{step_id or step.get('label') or 'custom'}",
                groups=(phrases,),
                kind="step",
                template=str(step.get("template") or "").strip(),
                # Do not pin channel/phase: these fire the named step when it is
                # due. A Ground tune during Delivery would otherwise 0.55-cap them.
                weight=1.15,
                example=phrases[0],
                does=str(step.get("label") or "run this step"),
                step_id=step_id,
            )
        )
    return tuple(out)


def _normalized_options(options: tuple[str, ...]) -> tuple[str, ...]:
    """Patterns go through normalize() too, so 'one more time' matches '1 more time'."""
    return tuple(dict.fromkeys(filter(None, (normalize(o) for o in options))))


_PATTERN_CACHE: dict[tuple[str, ...], tuple[str, ...]] = {}

# Whisper mangles radio audio in small ways constantly ("taxi" -> "taxy",
# "altimeter" -> "altimiter"), and pilots never say a phrase the same way
# twice, so exact keywords alone reject too much. An allowance of one typo per
# short word and two per long one covers the mis-hearings without letting
# genuinely different words in — "taxy" matches "taxi", "text" does not.
# Being loose here is safe: the addressing and phase gates decide *whether* to
# act, this only decides what was said.
def _max_edits(option: str) -> int:
    if len(option) < 4:
        return 0  # a typo in a short word is a different word ("ifr" / "if")
    return 1 if len(option) < 8 else 2


def _within_edits(a: str, b: str, budget: int) -> bool:
    """Levenshtein distance <= budget, giving up as soon as it cannot be."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > budget:
        return False
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        best = i
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > budget:
            return False
        previous = current
    return previous[-1] <= budget


def _fuzzy_hit(tokens: list[str], option: str) -> bool:
    """Does any same-length run of words sound near enough to `option`?"""
    budget = _max_edits(option)
    if not budget:
        return False
    span = option.count(" ") + 1
    for i in range(len(tokens) - span + 1):
        window = " ".join(tokens[i : i + span])
        if _within_edits(window, option, budget):
            return True
    return False


def _group_hit(text: str, options: tuple[str, ...], *, fuzzy: bool = True) -> str | None:
    """
    First option that appears in `text`, exactly or near enough.

    Vetoes and the chatter lists pass fuzzy=False: a wrong hit there silently
    swallows a real call, so those stay literal.
    """
    cached = _PATTERN_CACHE.get(options)
    if cached is None:
        cached = _normalized_options(options)
        _PATTERN_CACHE[options] = cached
    for option in cached:
        if re.search(rf"(?<!\w){re.escape(option)}(?!\w)", text):
            return option
    if not fuzzy:
        return None
    tokens = text.split()
    for option in cached:
        if _fuzzy_hit(tokens, option):
            return option
    return None


# Intents that may omit the agency opener while a readback is outstanding —
# the exchange is already open from ATC's last transmission.
_ADDRESS_OPTIONAL_INTENTS = frozenset(
    {"acknowledge_readback", "say_again"}
)


_GO_AROUND_PHRASES = (
    "going around",
    "go around",
    "waving off",
    "wave off",
    "on the go",
    "missed approach",
    "going missed",
    "executing missed",
    "going round",
    "go ahead and missed",
    "ahead and missed",
    "going a missed",
)

_LANDING_GO_AROUND_TEMPLATES = frozenset(
    {
        "right_break",
        "clear_land",
        "exit_runway",
        "go_around",
        "taxi_in",
    }
)


def _is_go_around_call(text: str) -> bool:
    """True when the pilot is waving off / executing a missed approach."""
    return bool(_group_hit(text, _GO_AROUND_PHRASES, fuzzy=False))


def _is_rolling_mode_call(text: str) -> bool:
    """True when the pilot is asking for / accepting / refusing a rolling takeoff."""
    if not _group_hit(text, ("rolling", "roll", "rolling takeoff"), fuzzy=False):
        return False
    return bool(
        _group_hit(
            text,
            (
                "request",
                "requesting",
                "want",
                "like",
                "prefer",
                "accept",
                "take",
                "unable",
                "negative",
                "can we",
                "can i",
                "we ll",
                "will",
            ),
            fuzzy=False,
        )
    )

# Tower steps that "ready for departure" may answer (not cleared takeoff,
# and not the random "will you accept rolling?" question).
_DEPARTURE_READY_TEMPLATES = frozenset(
    {
        "lineup",
    }
)

# Takeoff clearance — zone / Play, or the "in position" voice fallback.
_TAKEOFF_CLEAR_TEMPLATES = frozenset(
    {
        "clear_takeoff",
        "clear_takeoff_rolling",
        "clear_takeoff_intersection",
    }
)

# Approach → Tower check-in (right break / continue).
_TOWER_CHECKIN_TEMPLATES = frozenset({"right_break"})
_TOWER_CHECKIN_INTENTS = frozenset({"tower_check_in", "tower_initial"})

# Approach "contact tower" step — request the handoff, not another check-in.
_APPROACH_TOWER_HANDOFF_TEMPLATES = frozenset({"cleared_approach", "contact_tower"})
_APPROACH_TOWER_HANDOFF_INTENTS = frozenset(
    {"approach_continue", "approach_established"}
)

# Answers to Tower's rolling-takeoff offer.
_ROLLING_OFFER_INTENTS = frozenset(
    {
        "accept_rolling",
        "deny_rolling",
        "request_lineup",
    }
)

# Words too common to carry a shortened call on their own. "Ground, Fleece 1,
# taxi" is a summary of "ready to taxi"; "Ground, Fleece 1, request" is not.
_FILLER_HITS: frozenset[str] = frozenset(
    _normalized_options(
        _ASKING
        + ("for", "with", "with you", "to go", "for the go", "the active", "and")
    )
)

# A shortened call is still a guess at the full one, so it scores a little
# lower — the expectation boost below is what carries it over the line.
_SUMMARISED = 0.85


def _expected_now(
    intent: Intent,
    *,
    expected: str,
    awaiting_readback: bool,
    current_step_id: str = "",
) -> bool:
    """Is this the call ATC is sitting there waiting for?"""
    # During a readback window the pilot is answering the last clearance — not
    # asking for that step again. Otherwise "taxi via … runway 21R" re-fires taxi.
    if awaiting_readback:
        return intent.id in _ADDRESS_OPTIONAL_INTENTS
    if intent.step_id and current_step_id and intent.step_id == current_step_id:
        return True
    if intent.id == "ready_departure" and expected in _DEPARTURE_READY_TEMPLATES:
        return True
    if intent.id == "in_position" and expected in _TAKEOFF_CLEAR_TEMPLATES:
        return True
    if expected == "rolling_accept" and intent.id in _ROLLING_OFFER_INTENTS:
        return True
    if expected and intent.template and intent.template == expected:
        return True
    if expected == "monitor_tower" and intent.id == "at_eor":
        return True
    if expected == "radar_contact" and intent.id == "departure_check_in":
        return True
    return False


def _hinge_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Items that alone close the readback (legacy lists without hinge= still work)."""
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("hinge"):
            out.append(item)
            continue
        key = str(item.get("key") or "")
        if item.get("highlight") and key in ("squawk", "runway", "eor"):
            out.append(item)
    return out


def _squawk_code(items: list[dict[str, Any]] | None) -> str | None:
    """Four-digit Mode 3 from the outstanding readback checklist, if any."""
    for item in items or []:
        if str(item.get("key") or "") != "squawk":
            continue
        digits = re.sub(r"\D", "", str(item.get("value") or ""))
        if len(digits) >= 4:
            return digits[:4]
    return None


def _runway_readback_item(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    for item in items or []:
        if str(item.get("key") or "") == "runway":
            return item if isinstance(item, dict) else None
    return None


def _eor_readback_item(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    for item in items or []:
        if str(item.get("key") or "") == "eor":
            return item if isinstance(item, dict) else None
    return None


def _clearance_phrase_hit(text: str, item: dict[str, Any] | None) -> bool:
    """'cleared for takeoff' / 'line up and wait' / 'cleared to land'."""
    if not item:
        return False
    spoken = normalize(str(item.get("spoken") or ""))
    if spoken and spoken in text:
        return True
    value = normalize(str(item.get("value") or ""))
    if value and value in text:
        return True
    # Common shortenings / STT drops while ATC is waiting.
    # Whisper often hears "clear to land" instead of "cleared to land".
    key_bits = {
        "cleared for takeoff": (
            "cleared for takeoff",
            "cleared takeoff",
            "for takeoff",
            "clear for takeoff",
            "clear takeoff",
        ),
        "cleared to land": (
            "cleared to land",
            "cleared land",
            "clear to land",
            "clear land",
            "cleared for landing",
            "clear for landing",
        ),
        "line up and wait": ("line up and wait", "line up", "luaw"),
    }
    for full, alts in key_bits.items():
        if full in spoken or full in value:
            return any(re.search(rf"(?<!\w){re.escape(a)}(?!\w)", text) for a in alts)
    return False


def _go_around_instruction_hit(text: str, item: dict[str, Any] | None) -> bool:
    """
    Go-around readback: right/left closed traffic, Flex/Duck, or missed as published.
    """
    if not item:
        return False
    spoken = normalize(str(item.get("spoken") or ""))
    value = normalize(str(item.get("value") or ""))
    if spoken and spoken in text:
        return True
    if value and value in text:
        return True
    blob = f"{spoken} {value}"
    # Whisper often drops the -ed: "right, close traffic" for "right closed traffic".
    if "closed" in blob or "close traffic" in blob:
        if re.search(r"(?<!\w)close[d]?\s+traffic(?!\w)", text):
            return True
        if re.search(r"(?<!\w)(?:left|right)\s+close[d]?(?!\w)", text):
            return True
    if "flex" in blob and re.search(r"(?<!\w)flex(?:\s+reentry|\s+entry)?(?!\w)", text):
        return True
    if "duck" in blob and re.search(r"(?<!\w)duck(?:\s+reentry|\s+entry)?(?!\w)", text):
        return True
    if "missed" in blob or "published" in blob:
        if re.search(r"(?<!\w)(?:missed approach|as published|missed as published)(?!\w)", text):
            return True
    return False


# Whisper / casual speech for climb altitudes (and bad habits: angels / FL).
_ALT_CARDINALS = {
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "twenty one": 21,
    "twenty two": 22,
    "twenty three": 23,
    "twenty four": 24,
    "twenty five": 25,
    "twenty six": 26,
    "twenty seven": 27,
    "twenty eight": 28,
    "twenty nine": 29,
}


def _climb_readback_item(items: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    for item in _hinge_items(items):
        if str(item.get("key") or "") == "climb":
            return item
    for item in items or []:
        if isinstance(item, dict) and str(item.get("key") or "") == "climb":
            return item
    return None


def _assigned_climb_ft(item: dict[str, Any] | None) -> int | None:
    if not item:
        return None
    digits = re.sub(r"\D", "", str(item.get("value") or ""))
    if not digits:
        return None
    try:
        n = int(digits)
    except ValueError:
        return None
    return n if n >= 1000 else n * 100  # rare FL-style leftovers


def _add_altitude_ft(out: list[int], feet: int) -> None:
    if 5000 <= feet <= 60000 and feet not in out:
        out.append(feet)


def _heard_altitudes_ft(text: str) -> list[int]:
    """
    Every altitude-like value in a normalized transcript.

    Pilots paraphrase — 'feet' / 'ft' is never required:
      'fifteen thousand', '18 thousand', 'one five thousand',
      'seventeen thousands' (Whisper plural), '17k',
      'flight level 150' / 'fl 250' / 'FL250' / 'FL170',
      'angels 15' / 'angel eighteen', bare '15000' / '15,000'.
    """
    found: list[int] = []
    if not text:
        return found
    # Optional trailing feet/ft must not be required for a hit.
    text = re.sub(r"\b(?:feet|foot|ft)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Flight level / FL — digits already collapsed by normalize().
    for m in re.finditer(r"(?:flight\s+levels?|fl)\s*(\d{2,3})\b", text):
        _add_altitude_ft(found, int(m.group(1)) * 100)
    for m in re.finditer(r"\bfl(\d{2,3})\b", text):
        _add_altitude_ft(found, int(m.group(1)) * 100)

    # Angels N (tactical habit on ATC freqs — still count it).
    for m in re.finditer(r"\bangels?\s+(\d{1,2})\b", text):
        _add_altitude_ft(found, int(m.group(1)) * 1000)
    for word, n in _ALT_CARDINALS.items():
        if " " in word:
            continue
        if re.search(rf"\bangels?\s+{re.escape(word)}\b", text):
            _add_altitude_ft(found, n * 1000)

    # N thousand(s) / cardinal thousand(s) — Whisper often pluralizes.
    for word, n in sorted(_ALT_CARDINALS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"(?<!\w){re.escape(word)}\s+thousands?\b", text):
            _add_altitude_ft(found, n * 1000)
    for m in re.finditer(r"(?<!\w)(\d{1,2})\s+thousands?\b", text):
        _add_altitude_ft(found, int(m.group(1)) * 1000)
    # Casual '17k' / '17 k'
    for m in re.finditer(r"(?<!\w)(\d{1,2})\s*k\b", text):
        _add_altitude_ft(found, int(m.group(1)) * 1000)

    # Bare foot values (15000) — comma already stripped by normalize().
    for m in re.finditer(r"(?<!\w)(\d{4,5})(?!\w)", text):
        _add_altitude_ft(found, int(m.group(1)))

    return found


def _heard_altitude_ft(text: str) -> int | None:
    """First / best altitude heard, or None."""
    found = _heard_altitudes_ft(text)
    return found[0] if found else None


def _altitudes_equivalent(a: int, b: int) -> bool:
    """True when two foot values are the same level (tolerate FL rounding)."""
    if a == b:
        return True
    # 15000 vs FL150 (15000), or minor hundred-foot drift from speech.
    return abs(a - b) < 50


def _climb_readback_hit(text: str, item: dict[str, Any] | None) -> bool:
    """True when the assigned climb altitude was heard (flexible phrasing)."""
    assigned = _assigned_climb_ft(item)
    if assigned is None:
        return False
    for heard in _heard_altitudes_ft(text):
        if _altitudes_equivalent(heard, assigned):
            return True
    # Spoken cue substring (climb and maintain … / flight level …)
    spoken = normalize(str(item.get("spoken") or ""))
    spoken = re.sub(r"\b(?:feet|foot|ft)\b", " ", spoken)
    spoken = re.sub(r"\s+", " ", spoken).strip()
    if spoken and spoken in text:
        return True
    # Altitude-only spoken form still counts (never require trailing 'feet')
    spoken_alt = normalize(
        re.sub(
            r"^(climb\s+and\s+maintain|climb\s+unrestricted\s+up\s+to|climb|maintain)\s+",
            "",
            spoken,
        )
    )
    spoken_alt = re.sub(r"\b(?:feet|foot|ft)\b", " ", spoken_alt)
    spoken_alt = re.sub(r"\s+", " ", spoken_alt).strip()
    if spoken_alt and re.search(rf"(?<!\w){re.escape(spoken_alt)}(?!\w)", text):
        return True
    return False


def _heard_wrong_climb(
    text: str, items: list[dict[str, Any]] | None
) -> tuple[bool, int | None, int | None]:
    """
    (wrong, heard_ft, assigned_ft) when the pilot said a climb altitude that
    is not the one ATC assigned.
    """
    item = _climb_readback_item(items)
    assigned = _assigned_climb_ft(item)
    if assigned is None:
        return False, None, None
    heard_list = _heard_altitudes_ft(text)
    if not heard_list:
        return False, None, assigned
    if any(_altitudes_equivalent(h, assigned) for h in heard_list):
        return False, assigned, assigned
    # Any wrong altitude while this hinge is open counts (angels / FL / thousand).
    return True, heard_list[0], assigned


def _hinge_item_hit(text: str, item: dict[str, Any] | None) -> bool:
    """True when one hinge checklist item was heard."""
    if not item:
        return False
    key = str(item.get("key") or "")
    if key == "squawk":
        code = re.sub(r"\D", "", str(item.get("value") or ""))
        return bool(code and _heard_assigned_squawk(text, code[:4]))
    if key == "runway":
        return _runway_readback_hit(text, item)
    if key == "eor":
        return _eor_readback_hit(text, item)
    if key == "clearance":
        return _clearance_phrase_hit(text, item)
    if key == "instruction":
        return _go_around_instruction_hit(text, item)
    if key == "climb":
        return _climb_readback_hit(text, item)
    if key == "callsign":
        return _callsign_readback_hit(text, item)
    spoken = normalize(str(item.get("spoken") or ""))
    if spoken and spoken in text:
        return True
    return False


def _callsign_readback_hit(text: str, item: dict[str, Any] | None) -> bool:
    """True when the pilot said their own callsign (e.g. 'Bruiser 5')."""
    if not item:
        return False
    raw = str(item.get("value") or "").strip()
    spoken = normalize(str(item.get("spoken") or ""))
    if spoken and re.search(rf"(?<!\w){re.escape(spoken)}(?!\w)", text):
        return True
    if raw:
        # Digits form after normalize: 'bruiser fife' → 'bruiser 5'
        if analyze_address(text, raw).own_callsign:
            return True
        raw_n = normalize(raw)
        if raw_n and re.search(rf"(?<!\w){re.escape(raw_n)}(?!\w)", text):
            return True
    return False


def _any_hinge_hit(text: str, items: list[dict[str, Any]] | None) -> bool:
    return any(_hinge_item_hit(text, item) for item in _hinge_items(items))


def _readback_value_hit(text: str, items: list[dict[str, Any]] | None) -> bool:
    """True when the transcript includes a assigned value (squawk digits, runway…)."""
    if not items:
        return False
    if _any_hinge_hit(text, items):
        return True
    for item in items:
        key = str(item.get("key") or "")
        if key in ("runway", "eor", "squawk", "clearance"):
            continue
        value = re.sub(r"\D", "", str(item.get("value") or ""))
        if len(value) >= 3 and re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text):
            return True
        spoken = normalize(str(item.get("spoken") or ""))
        if spoken and re.search(rf"(?<!\w){re.escape(spoken)}(?!\w)", text):
            return True
    return False


# Compact compass on airport JSON ("NW EOR") ↔ what pilots / TTS usually say.
_PLACE_COMPASS = {
    "nw": "northwest",
    "ne": "northeast",
    "sw": "southwest",
    "se": "southeast",
}
# Whisper often writes the long form as two words: "north west EOR".
_PLACE_COMPASS_PAIRS = {
    ("north", "west"): "northwest",
    ("north", "east"): "northeast",
    ("south", "west"): "southwest",
    ("south", "east"): "southeast",
}


def _collapse_place_compass(text: str) -> str:
    """'NW EOR' / 'north west EOR' / 'northwest EOR' → 'northwest eor'."""
    toks = normalize(text).split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        pair = (toks[i], toks[i + 1]) if i + 1 < len(toks) else None
        if pair and pair in _PLACE_COMPASS_PAIRS:
            out.append(_PLACE_COMPASS_PAIRS[pair])
            i += 2
            continue
        out.append(_PLACE_COMPASS.get(toks[i], toks[i]))
        i += 1
    return " ".join(out)


def _eor_place_forms(value_raw: str) -> list[str]:
    """Distinct normalized place labels for matching (nw eor, northwest eor, …)."""
    forms: list[str] = []
    for raw in (value_raw, _collapse_place_compass(value_raw)):
        n = normalize(raw)
        if n and n not in forms:
            forms.append(n)
        collapsed = _collapse_place_compass(n)
        if collapsed and collapsed not in forms:
            forms.append(collapsed)
    return forms


def _eor_readback_hit(text: str, item: dict[str, Any] | None) -> bool:
    """
    True when the pilot read back the taxi-to-EOR (or equivalent place).

    Accepts the full spoken cue, the place label (NW EOR / northwest EOR /
    north west EOR / Alpha South), or 'taxi … EOR' when the assigned
    destination is an EOR. Bare 'at EOR' without taxi/place wording is not
    enough — that is the later monitor-tower call.
    """
    if not item:
        return False
    text_n = normalize(text)
    text_c = _collapse_place_compass(text_n)

    spoken = normalize(str(item.get("spoken") or ""))
    spoken_c = _collapse_place_compass(spoken)
    for form in dict.fromkeys([spoken, spoken_c]):
        if form and (form in text_n or form in text_c):
            return True
    # Drop a leading "taxi …" so "northwest eor via foxtrot" still counts.
    for cue in (spoken, spoken_c):
        for prefix in ("taxi to the ", "taxi to ", "taxi "):
            if cue.startswith(prefix):
                rest = cue[len(prefix) :].strip()
                if rest and (rest in text_n or rest in text_c):
                    return True

    value_raw = str(item.get("value") or "").strip()
    for form in _eor_place_forms(value_raw):
        if form and re.search(rf"(?<!\w){re.escape(form)}(?!\w)", text_n):
            return True
        if form and re.search(rf"(?<!\w){re.escape(form)}(?!\w)", text_c):
            return True

    forms = _eor_place_forms(value_raw)
    dest_is_eor = any("eor" in f.split() for f in forms)
    if dest_is_eor and re.search(r"(?<!\w)taxi\b.{0,48}\beor(?!\w)", text_c):
        return True
    if dest_is_eor and re.search(
        r"(?<!\w)end of (?:the )?runway(?!\w)", text_c
    ):
        return True
    return False


def _normalize_runway_token(value: str) -> str:
    """'21R' / '21 right' / 'rwy 21l' → '21R'."""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    m = re.match(r"^(\d{1,3})\s*([LRC])?$", raw.replace(" ", ""))
    if m:
        return f"{m.group(1)}{m.group(2) or ''}"
    m = re.match(r"^(\d{1,3})\s*(LEFT|RIGHT|CENTER|CENTRE)?$", raw)
    if m:
        side = {"LEFT": "L", "RIGHT": "R", "CENTER": "C", "CENTRE": "C"}.get(
            m.group(2) or "", ""
        )
        return f"{m.group(1)}{side}"
    return re.sub(r"[^0-9LRC]", "", raw)


def _runway_readback_hit(text: str, item: dict[str, Any] | None) -> bool:
    """True when the pilot said the assigned runway (digits and/or spoken form)."""
    if not item:
        return False
    assigned = _normalize_runway_token(str(item.get("value") or ""))
    if not assigned:
        return False
    digits = re.sub(r"\D", "", assigned)
    side_letter = assigned[-1] if assigned[-1:] in "LRC" else ""
    side_word = {"L": "left", "R": "right", "C": "center"}.get(side_letter, "")

    # Glued STT forms: '21r', '21l' (normalize keeps the letter on the digits).
    if re.search(rf"(?<!\w){re.escape(assigned.lower())}(?!\w)", text):
        return True
    if digits and side_letter and re.search(
        rf"(?<!\w){re.escape(digits)}{side_letter.lower()}(?!\w)", text
    ):
        return True
    # '21 right' / '21 left'
    if digits and side_word and re.search(
        rf"(?<!\w){re.escape(digits)}\s*{side_word}(?!\w)", text
    ):
        return True
    # Bare number only when the assigned runway has no L/R/C side.
    if digits and not side_letter and re.search(
        rf"(?<!\w){re.escape(digits)}(?!\w)", text
    ):
        return True
    # extract_runway on 'runway 21r' / 'runway 21 right'
    heard = extract_runway(text, known=[assigned, digits] if digits else None)
    if heard and _normalize_runway_token(heard) == assigned:
        return True
    spoken = normalize(str(item.get("spoken") or ""))
    if spoken and spoken in text:
        return True
    return False


def _heard_assigned_squawk(text: str, code: str) -> bool:
    """
    Clearance readback hinge: 'squawk 0551', 'squawking 0551', or the bare code.

    Digits are already collapsed by normalize(), so spoken 'zero five five one'
    arrives here as '0551'. Extra words around the code are fine; 'in sequence'
    is never required. Bare code is only used while a squawk hinge is open —
    outside that window altitudes / freqs would look like Mode 3 codes.
    """
    if not code or len(code) < 4:
        return False
    code = code[:4]
    if re.search(rf"(?<!\w)squawk(?:ing)?\s+{re.escape(code)}(?!\w)", text):
        return True
    # Mid-readback: "zero four one one" / "0411" alone is enough.
    return bool(re.search(rf"(?<!\w){re.escape(code)}(?!\w)", text))


def _score_intents(
    text: str,
    transcript: str,
    *,
    channel: str,
    phase: str,
    expected: str,
    addressed: str | None,
    runways: list[str] | None,
    awaiting_readback: bool = False,
    readback_items: list[dict[str, Any]] | None = None,
    extra: tuple[Intent, ...] = (),
    current_step_id: str = "",
    steps: list[dict[str, Any]] | None = None,
) -> Match | None:
    """Highest-scoring intent for a transcript, before any addressing gate."""
    best: Match | None = None
    current_step = step_by_id(steps, current_step_id)
    for intent in tuple(INTENTS) + tuple(extra):
        if intent.veto and _group_hit(text, intent.veto, fuzzy=False):
            continue
        if intent.id in _C2_INTENT_IDS and not step_offers_c2(
            current_step, channel=channel
        ):
            continue
        # Tower will take overhead / tac overhead / straight-in. Instrument
        # and named IAF / recovery fixes stay with Approach.
        if intent.id == "request_approach" and channel == "tower":
            rec = extract_recovery(text)
            if rec not in _TOWER_RECOVERY_KEYS:
                continue
        # A bare acknowledgement only means something while ATC is waiting on
        # one; the rest of the time "roger" is just talk.
        if intent.id == "acknowledge_readback" and not awaiting_readback:
            continue
        # Addressed Blackjack — do not steal the check-in as Bandsaw / Departure.
        if addressed == "blackjack" and intent.id in (
            "bandsaw_check_in",
            "bandsaw_check_out",
            "departure_check_in",
            "tower_check_in",
            "tower_initial",
            "inbound_recovery",
        ):
            continue
        expected_l = (expected or "").strip().lower()
        if awaiting_readback and expected_l == "go_around" and intent.id == "going_around":
            # Tower already issued the go-around — repeating it is the readback.
            continue
        if awaiting_readback and intent.id == "acknowledge_readback":
            if _is_rolling_mode_call(text):
                continue
            # During land readback, "going around" is a waveoff — not an ack.
            # After Tower issued the go-around, repeating it closes the card.
            if _is_go_around_call(text) and (expected or "").strip().lower() != "go_around":
                continue
        if awaiting_readback and intent.id == "at_eor":
            continue
        if awaiting_readback and intent.template == "monitor_tower":
            continue
        # Departure radar contact is an airborne check-in — not weather.
        if expected == "radar_contact" and intent.id in _DEPARTURE_CHECKIN_SKIP_IDS:
            continue
        # Tower check-in after Approach handoff — not gear-down / landing yet.
        if expected in _TOWER_CHECKIN_TEMPLATES and intent.id in (
            "request_landing",
            "request_low_approach",
        ):
            continue
        # Contact-tower step: not another Approach check-in.
        if expected in _APPROACH_TOWER_HANDOFF_TEMPLATES and intent.id == "inbound_recovery":
            continue
        # Cleared takeoff is the in-position zone / Play — not another ready call.
        if intent.id == "ready_departure" and expected in (
            *_TAKEOFF_CLEAR_TEMPLATES,
            "rolling_accept",
        ):
            continue
        # Rolling offer is accept / decline — not "in position" / takeoff.
        if expected == "rolling_accept" and intent.id == "in_position":
            continue
        # Already cleared — don't re-fire takeoff on "in position".
        if (
            awaiting_readback
            and intent.id == "in_position"
            and expected in _TAKEOFF_CLEAR_TEMPLATES
        ):
            continue
        # Rolling offer is accept / decline — not a LUAW / runway readback.
        if expected == "rolling_accept" and intent.id == "acknowledge_readback":
            continue
        expecting = _expected_now(
            intent,
            expected=expected,
            awaiting_readback=awaiting_readback,
            current_step_id=current_step_id,
        )
        # Mission phrases belong to one step — only when that step is due.
        if intent.step_id and not expecting:
            continue
        heard_exactly = True
        summarised = False
        # Instruction readbacks: any hinge item closes the window (agency optional).
        # IFR clearance also accepts a bare roger/copy; taxi/tower need a hinge.
        hinges = (
            _hinge_items(readback_items)
            if intent.id == "acknowledge_readback"
            else []
        )
        squawk_code = _squawk_code(hinges) if hinges else None
        hinge_hit = bool(hinges and _any_hinge_hit(text, hinges))
        if (
            not hinge_hit
            and awaiting_readback
            and intent.id == "acknowledge_readback"
            and (expected or "").strip().lower() == "go_around"
            and _is_go_around_call(text)
        ):
            # Repeating "going around" / "on the go" closes the instruction card.
            hinge_hit = True
        squawk_hit = bool(
            squawk_code and _heard_assigned_squawk(text, squawk_code)
        )
        outstanding_runway = (
            _runway_readback_item(readback_items) if awaiting_readback else None
        )
        if hinge_hit:
            confidence = min(1.0, 0.98 * intent.weight)
            coverage = 1.0
        elif intent.groups:
            hits = [_group_hit(text, group) for group in intent.groups]
            matched = [h for h in hits if h]
            if len(matched) != len(hits):
                # ATC is waiting on this exact call, so a shortened version of
                # it still counts — "taxi" for "ready to taxi". Any other
                # intent needs the full form, and filler alone is never enough.
                if not (expecting and any(h not in _FILLER_HITS for h in matched)):
                    continue
                summarised = True
            short_ack = any(
                h in ("roger", "wilco", "copy", "readback", "read back")
                for h in matched
            )
            # IFR: squawk code or a short ack. Taxi / takeoff / land: need a hinge.
            if intent.id == "acknowledge_readback" and hinges and not hinge_hit:
                if squawk_code:
                    if not squawk_hit and not short_ack:
                        continue
                elif (expected or "").strip().lower() == "go_around" and (
                    short_ack or _is_go_around_call(text)
                ):
                    hinge_hit = True
                else:
                    continue
            heard_exactly = all(
                re.search(rf"(?<!\w){re.escape(h)}(?!\w)", text) for h in matched
            )
            coverage = sum(len(h.split()) for h in matched) / max(1, len(text.split()))
            confidence = min(1.0, 0.55 + 0.45 * min(1.0, coverage * 2.5)) * intent.weight
            if not heard_exactly:
                confidence *= 0.9
            if summarised:
                confidence *= _SUMMARISED
        else:
            coverage = 0.0
            confidence = 0.55 * intent.weight

        # Wrong agency or wrong stage of flight caps the score below the default
        # threshold, so a call that makes no sense here cannot fire on wording
        # alone — but still can if the pilot deliberately lowers the bar.
        if intent.channels and channel:
            confidence *= 1.0 if channel in intent.channels else _OFF_CONTEXT
        if intent.phases and phase:
            confidence *= 1.0 if phase in intent.phases else _OFF_CONTEXT
        # The call ATC is actually waiting for is the likeliest thing to hear.
        if expecting:
            confidence *= 1.3
        if awaiting_readback and intent.id == "acknowledge_readback":
            confidence *= 1.35
            if hinge_hit or _readback_value_hit(text, readback_items):
                confidence *= 1.15
        # Long clearance readbacks bury one keyword in twenty words — once a
        # hinge is heard, do not let coverage pull the score under.
        if hinge_hit:
            confidence = max(confidence, 0.95)
        confidence = min(1.0, confidence)

        slots: dict[str, Any] = {}
        if intent.id == "request_runway":
            runway = extract_runway(text, runways)
            if not runway:
                continue
            slots["runway"] = runway
            # During taxi readback, restating the assigned runway is a readback —
            # not a runway-change request (e.g. "confirm runway 21R").
            if awaiting_readback and outstanding_runway:
                assigned = _normalize_runway_token(
                    str(outstanding_runway.get("value") or "")
                )
                if assigned and _normalize_runway_token(runway) == assigned:
                    continue
        recovery = extract_recovery(text)
        if recovery and intent.id in (
            "inbound_recovery",
            "request_landing",
            "request_approach",
        ):
            slots["recovery"] = recovery
        if intent.id in (
            "inbound_recovery",
            "request_approach",
            "request_hold",
            "approach_continue",
        ):
            vfr = extract_vfr_recovery(text)
            if vfr:
                slots["vfr_recovery"] = vfr
            iaf = extract_iaf(text)
            if iaf:
                slots["iaf"] = iaf

        if addressed:
            slots["channel"] = addressed
        if best is None or confidence > best.confidence:
            best = Match(
                intent=intent.id,
                kind=intent.kind,
                template=intent.template,
                confidence=confidence,
                slots=slots,
                transcript=transcript,
                normalized=text,
                step_id=intent.step_id,
                expected=expecting,
                summarised=summarised,
            )

    # Wrong climb altitude while Departure is waiting on the radar-contact readback.
    if awaiting_readback:
        wrong, heard_ft, assigned_ft = _heard_wrong_climb(text, readback_items)
        if wrong and assigned_ft is not None:
            # Prefer the correction over a partial acknowledge that did not hinge.
            if best is None or best.intent == "acknowledge_readback":
                climb_hit = _climb_readback_hit(text, _climb_readback_item(readback_items))
                if not climb_hit:
                    return Match(
                        intent="correct_climb_readback",
                        kind="request",
                        template="",
                        confidence=0.96,
                        slots={
                            "climb_ft": assigned_ft,
                            "heard_ft": heard_ft,
                            "channel": addressed,
                        },
                        transcript=transcript,
                        normalized=text,
                        expected=True,
                        summarised=False,
                    )
    return best


@dataclass
class Evaluation:
    """
    What we heard, what it looked like, and whether it may fire.

    `match` is None whenever nothing should happen; `reason` then says why, so
    the Fly log can show "heard X — ignored (talking to the flight)" instead of
    leaving the pilot wondering.
    """

    transcript: str = ""
    normalized: str = ""
    match: Match | None = None
    candidate: Match | None = None
    reason: str = ""
    advice: str = ""
    address: Address = field(default_factory=Address)

    @property
    def fired(self) -> bool:
        return self.match is not None

    def describe(self) -> str:
        if self.match:
            return self.match.describe()
        if self.reason:
            heard = f" (heard {self.candidate.intent})" if self.candidate else ""
            tip = f" — {self.advice}" if self.advice else ""
            return f"ignored — {self.reason}{heard}{tip}"
        return "no match — ignored"


# After a handoff / instruction with no READ BACK card, repeating ATC must
# not fire the next timeline step ("contact Blackjack" is the readback).
_ECHO_MAX_AGE_S = 60.0
_CHECKIN_NOT_READBACK = (
    "with you",
    "checking in",
    "check in",
    "airborne",
    "initial",
    "gear down",
    "ready to taxi",
    "ready taxi",
    "ready for taxi",
    "request taxi",
    "requesting taxi",
    "ready for departure",
    "in position",
    "at eor",
    "at the eor",
    "end of runway",
    "end of the runway",
    "holding short",
    "northwest eor",
    "alpha south",
)
_READBACK_STOP = frozenset(
    {
        "a",
        "the",
        "to",
        "and",
        "for",
        "of",
        "on",
        "at",
        "in",
        "is",
        "are",
        "we",
        "your",
        "good",
        "day",
        "see",
        "ya",
        "yeah",
        "please",
        "nellis",
        "one",
        "flight",
    }
)


def _catalog_intent(
    intent_id: str, extra: tuple[Intent, ...] = ()
) -> Intent | None:
    for intent in tuple(INTENTS) + tuple(extra):
        if intent.id == intent_id:
            return intent
    return None


def _trigger_hits_in_text(intent: Intent, text: str) -> list[str]:
    """Normalized trigger phrases from this intent that appear in `text`."""
    hits: list[str] = []
    for group in intent.groups:
        for opt in group:
            n = normalize(opt)
            if n and n in text:
                hits.append(n)
    return hits


def echoes_last_atc(
    transcript: str,
    last_tx: str,
    *,
    intent: Intent | None = None,
    steps: list[dict[str, Any]] | None = None,
    last_tx_at: float = 0.0,
    now: float = 0.0,
    addressed: str = "",
    last_tx_channel: str = "",
    last_tx_template: str = "",
    candidate_template: str = "",
    callsign: str = "",
) -> bool:
    """
    True when the pilot is reading back the last ATC call, not making a new one.

    Used when there is no formal READ BACK card (handoff, custom/file, Center).
    Repeating “contact Blackjack” on Departure is an echo. Reporting the next
    action named in that instruction (taxi after Delivery, at EOR after taxi)
    is not — those are a different flow step.
    """
    last = normalize(last_tx)
    text = normalize(transcript)
    if not last or not text:
        return False
    if last_tx_at and now and (now - last_tx_at) > _ECHO_MAX_AGE_S:
        return False
    addr = str(addressed or "").strip().lower()
    prev = str(last_tx_channel or "").strip().lower()
    if addr and prev and addr != prev:
        return False
    last_tmpl = str(last_tx_template or "").strip().lower()
    cand_tmpl = str(candidate_template or "").strip().lower()
    if last_tmpl and cand_tmpl and last_tmpl != cand_tmpl:
        return False
    if any(cue in text and cue not in last for cue in _CHECKIN_NOT_READBACK):
        return False

    hits = _trigger_hits_in_text(intent, text) if intent is not None else []
    if intent is not None and intent.step_id:
        for phrase in step_voice_phrases(steps, intent.step_id):
            n = normalize(phrase)
            if n and n in text:
                hits.append(n)
    hits.sort(key=len, reverse=True)
    for hit in hits:
        # Single words like "taxi" / "eor" appear inside the previous
        # instruction and must not kill the next call.
        if " " not in hit and len(hit) < 10:
            continue
        if len(hit) >= 4 and hit in last:
            return True

    ignore = set(_READBACK_STOP)
    ignore.update(normalize(callsign).split())
    if prev:
        ignore.add(prev)
    heard = [t for t in text.split() if t not in ignore]
    if len(heard) < 2:
        return False
    said = set(last.split())
    overlap = sum(1 for t in heard if t in said)
    return (overlap / len(heard)) >= 0.7


def _said_authored_phrase(
    candidate: Match | None,
    steps: list[dict[str, Any]] | None,
    text: str,
) -> bool:
    """True when a step's own phrase was spoken verbatim, not just approximated."""
    if candidate is None or not candidate.step_id:
        return False
    for step in steps or []:
        if str(step.get("id") or "").strip() != candidate.step_id:
            continue
        for phrase in parse_phrases(step.get("voice_phrases")):
            normed = normalize(phrase)
            if normed and normed in text:
                return True
    return False


def evaluate(
    transcript: str,
    *,
    channel: str = "",
    phase: str = "",
    expected: str = "",
    callsign: str = "",
    runways: list[str] | None = None,
    min_confidence: float = 0.6,
    require_address: bool = True,
    awaiting_readback: bool = False,
    readback_items: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    current_step_id: str = "",
    last_tx_text: str = "",
    last_tx_at: float = 0.0,
    last_tx_channel: str = "",
    last_tx_template: str = "",
    tanker_chat_choices: list[dict[str, Any]] | None = None,
) -> Evaluation:
    """
    Decide whether a transmission is ATC business, and if so what it asks for.

    The PTT is shared with the rest of the flight, so matching keywords is not
    enough — most of what comes across is chatter that must not move the
    timeline. A transmission only fires when it was addressed to an agency (or
    at least tagged with our own callsign) and does not look like crew talk.

    Exception: while `awaiting_readback` is set, acknowledge/say-again may omit
    the agency opener — ATC just spoke and the exchange is still open.
    """
    text = normalize(transcript)
    if not text:
        return Evaluation(transcript=transcript, reason="nothing heard")

    address = analyze_address(text, callsign, raw=transcript)
    # Who the pilot called outranks where the timeline cursor happens to sit.
    channel = address.agency or channel
    phase = normalize_mission_phase(phase, channel=channel or "")
    if step_is_authored(step_by_id(steps, current_step_id)):
        expected = ""

    extra_intents = step_intents(steps)
    candidate = _score_intents(
        text,
        transcript,
        channel=channel,
        phase=phase,
        expected=expected,
        addressed=address.agency,
        runways=runways,
        awaiting_readback=awaiting_readback,
        readback_items=readback_items,
        extra=extra_intents,
        current_step_id=current_step_id,
        steps=steps,
    )
    if (
        candidate is not None
        and not awaiting_readback
        and (candidate.kind == "step" or candidate.step_id)
        and echoes_last_atc(
            text,
            last_tx_text,
            intent=_catalog_intent(candidate.intent, extra_intents),
            steps=steps,
            last_tx_at=last_tx_at,
            now=time.time() if last_tx_at else 0.0,
            addressed=str(address.agency or ""),
            last_tx_channel=last_tx_channel,
            last_tx_template=last_tx_template,
            candidate_template=str(candidate.template or ""),
            callsign=callsign,
        )
    ):
        return Evaluation(
            transcript=transcript,
            normalized=text,
            candidate=candidate,
            address=address,
            reason="readback of last ATC",
            advice="that was the last instruction — check in when ready",
        )
    result = Evaluation(
        transcript=transcript, normalized=text, candidate=candidate, address=address
    )

    # Boom small-talk answers (Dunkin / Starbucks / …) while Texaco is waiting.
    # Official tanker calls still win. Agency opener is optional.
    if tanker_chat_choices and not (
        candidate is not None
        and candidate.intent
        in {
            "tanker_check_in",
            "tanker_astern",
            "tanker_contact",
            "tanker_disconnect",
            "tanker_depart",
            "tanker_dcs_precontact",
            "tanker_dcs_abort",
            "say_again",
        }
    ):
        try:
            import tanker_chat as tanker_chat_mod

            hit = tanker_chat_mod.match_choice(text, tanker_chat_choices)
        except Exception:
            hit = None
        if hit:
            result.match = Match(
                intent="tanker_chat_reply",
                kind="request",
                template="",
                confidence=1.0,
                slots={"choice": str(hit.get("id") or "")},
                transcript=transcript,
                normalized=text,
            )
            return result

    # Wording the mission author typed for this step, said word for word. It
    # outranks the loose chatter filters — casual phrasing is the whole point of
    # writing your own trigger. Addressing still has to check out.
    authored = _said_authored_phrase(candidate, steps, text)

    crew_term = _first_term(text, _CREW_ONLY_TERMS)
    if crew_term:
        result.reason = f"flight chatter (“{crew_term}”)"
        return result
    # Wingman on the shared PTT. If we also said our own callsign (or
    # Fleece 1.1 while we are Fleece 1), this is our ATC call.
    if address.flight_member and not address.own_callsign:
        result.reason = "talking to the flight"
        return result
    talk_term = _first_term(text, _CONVERSATIONAL_TERMS)
    if talk_term and not authored:
        result.reason = f"crew talk (“{talk_term}”)"
        return result
    if not address.to_atc and not authored:
        likely = _first_term(text, _CREW_LIKELY_TERMS)
        if likely:
            result.reason = f"flight chatter (“{likely}”)"
            return result

    if candidate is None:
        result.reason = "no ATC call recognised"
        result.advice = (
            "say “squawk”/“squawking” + code, the code alone, or roger"
            if awaiting_readback and _squawk_code(readback_items)
            else "read back the highlighted items"
            if awaiting_readback
            else "try one of the calls on the Fly tab"
        )
        return result

    # ATC has just spoken and is holding for an answer, so the reply it is
    # waiting on does not have to open with the agency all over again.
    # Every other call — at EOR, taxi, check-in, in position — must address
    # the agency, not just a couple of cue words.
    address_optional = bool(awaiting_readback)
    if require_address and not address_optional and not address.to_atc:
        result.reason = "no agency addressed"
        result.advice = (
            "read back the highlighted items — agency name optional now"
            if awaiting_readback
            else "open with the agency, e.g. “Ground, …”"
        )
        return result
    if candidate.confidence < min_confidence:
        result.reason = f"low confidence ({candidate.confidence:.0%})"
        result.advice = "say the request on its own, without the extra words"
        return result

    result.match = candidate
    return result


# ---- prompting the pilot -------------------------------------------------

_AGENCY_SPOKEN: dict[str, str] = {
    "delivery": "{ap} Delivery",
    "ground": "{ap} Ground",
    "tower": "{ap} Tower",
    "approach": "{ap} Approach",
    "departure": "{ap} Departure",
    "blackjack": "Blackjack",
    "bandsaw": "Bandsaw",
    "ops": "{ap} Ops",
    "tanker": "Tanker",
    "other": "Control",
}


def agency_spoken(channel: str, airport_name: str = "") -> str:
    """'ground' -> 'Nellis Ground'. Falls back to a bare agency name."""
    template = _AGENCY_SPOKEN.get((channel or "").lower())
    if not template:
        return (channel or "").title()
    return template.format(ap=airport_name.strip() or "").strip()


# Takeoff choices belong on Tower — never offer them on Ground taxi cues.
_TAKEOFF_SUGGESTION_IDS = frozenset(
    {
        "accept_rolling",
        "deny_rolling",
        "request_lineup",
        "request_rolling",
        "ready_departure",
        "in_position",
    }
)


# During departure radar contact: check-in only — not weather or approach recovery.
_DEPARTURE_CHECKIN_SKIP_IDS = frozenset(
    {"request_winds", "request_altimeter", "inbound_recovery"}
)


def suggestions(
    *,
    phase: str = "",
    channel: str = "",
    expected: str = "",
    callsign: str = "",
    airport_name: str = "",
    limit: int = 5,
    awaiting_readback: bool = False,
    readback_items: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    current_step_id: str = "",
    advance_limit: int | None = None,
    optional_limit: int | None = None,
    tanker_chat_choices: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str, str, bool]]:
    """
    Fly kneeboard cues: (payload, what it does, role, agency_required).

    `payload` is the wording that must be said (amber on the card). When
    `agency_required` is True the UI prefixes the agency + callsign opener.
    role is "advance" (plays / advances the expected step) or "optional".

    While a readback is outstanding the lead suggestion is the readback itself
    (role "advance", agency optional). `limit` caps the combined list;
    `advance_limit` / `optional_limit` cap each role when set.

    When `current_step_id` has mission `voice_phrases`, those are the advance
    cues and the stock template call is hidden so custom steps stay in sync.

    Authored (custom/file) steps never inherit leftover template cues such as
    "with you" — only that step's `voice_phrases` appear under TO ADVANCE.
    """
    _ = (callsign, airport_name)  # kept for call-site compatibility
    out: list[tuple[str, str, str, bool]] = []
    channel_l = (channel or "").strip().lower()
    phase_l = normalize_mission_phase(phase, channel=channel_l)
    expected_l = (expected or "").strip().lower()
    current_id = str(current_step_id or "").strip()
    current_step = step_by_id(steps, current_id)
    authored = step_is_authored(current_step)
    if authored:
        # Leftover template on a custom/file step must not drive stock cues.
        expected_l = ""
    if awaiting_readback:
        hinges = _hinge_items(readback_items)
        hinge_says = [
            str(i.get("spoken") or "").strip() for i in hinges if i.get("spoken")
        ]
        if hinge_says:
            if len(hinge_says) > 1:
                out.append(
                    (
                        "   — or —   ".join(hinge_says),
                        "either one closes it · agency name optional",
                        "advance",
                        False,
                    )
                )
            else:
                hinge_key = str(hinges[0].get("key") or "") if hinges else ""
                if hinge_key == "squawk":
                    tip = (
                        "“squawk”/“squawking” + code, code alone, or roger "
                        "· agency optional"
                    )
                elif hinge_key == "climb":
                    tip = "altitude alone is enough · agency optional"
                elif hinge_key == "callsign":
                    tip = "say your callsign · agency optional"
                else:
                    tip = "closes the readback · agency name optional"
                out.append((hinge_says[0], tip, "advance", False))
        else:
            out.append(
                (
                    "roger",
                    "confirm your readback · agency name optional",
                    "advance",
                    False,
                )
            )

    current_phrases = step_voice_phrases(steps, current_id)
    current_does = "run this step"
    if current_id and current_phrases and not awaiting_readback:
        if isinstance(current_step, dict):
            current_does = str(current_step.get("label") or current_does)
        take = advance_limit if advance_limit is not None else limit
        for phrase in current_phrases[:take]:
            out.append((phrase, current_does, "advance", True))

    if tanker_chat_choices and not awaiting_readback:
        for choice in tanker_chat_choices:
            say = str(choice.get("say") or "").strip()
            if say:
                out.append(
                    (say, "answer Texaco · agency optional", "advance", False)
                )

    ranked: list[tuple[int, int, Intent, str]] = []
    # Built-in grammar only here — this step's mission phrases are already above.
    catalog = list(INTENTS)
    for i, intent in enumerate(catalog):
        if not intent.example:
            continue
        if intent.id == "acknowledge_readback":
            # Only ever valid mid-readback, and the lead line above covers it.
            continue
        # Custom/file steps: only the author's cue advances — not "with you".
        if authored and intent.kind == "step":
            continue
        if intent.id in _C2_INTENT_IDS and not step_offers_c2(
            current_step, channel=channel_l
        ):
            continue
        # This step has its own wording — don't also tip the stock template call.
        if (
            current_phrases
            and intent.kind == "step"
            and intent.template
            and intent.template == expected_l
        ):
            continue
        if intent.phases and phase_l and phase_l not in intent.phases:
            continue
        # Keep Delivery free of Ground/Tower-only asks (taxi, runway, …).
        if intent.channels and channel_l and channel_l not in intent.channels:
            continue
        # Taxi / Ground: no rolling or LUAW prompts.
        if (
            intent.id in _TAKEOFF_SUGGESTION_IDS
            and (
                channel_l == "ground"
                or phase_l == "ground"
                or expected_l in ("taxi", "monitor_tower")
            )
        ):
            continue
        # Taxi-to-EOR is outbound — don't offer taxi-in / clear-of-runway yet.
        if expected_l == "taxi" and intent.id == "clear_of_runway":
            continue
        # Finish the taxi readback before offering "at EOR" / monitor tower.
        if awaiting_readback and intent.id == "at_eor":
            continue
        # After taxi, kneeboard is "at EOR" — not another taxi request.
        if expected_l == "monitor_tower" and intent.id in (
            "ready_taxi",
            "clear_of_runway",
        ):
            continue
        # Departure radar contact: check-in cues only — not winds / altimeter.
        if expected_l == "radar_contact" and intent.id in _DEPARTURE_CHECKIN_SKIP_IDS:
            continue
        if expected_l in _TOWER_CHECKIN_TEMPLATES and intent.id in (
            "request_landing",
            "request_low_approach",
        ):
            continue
        if expected_l in _APPROACH_TOWER_HANDOFF_TEMPLATES and intent.id == "inbound_recovery":
            continue
        # Range checkout ends Flight → Approach; tip it on the range-exit step
        # (and allow check-in "continue" while still working the range).
        if intent.id == "range_exit" and expected_l != "bj_range_exit":
            continue
        # Bandsaw: check-in does not advance; tip checkout while still on check-in.
        if intent.id == "bandsaw_check_in" and expected_l in (
            "bandsaw_check_in",
            "bandsaw_check_out",
        ):
            continue
        if intent.step_id:
            rank = 0
        elif intent.id == "ready_departure" and expected_l in _DEPARTURE_READY_TEMPLATES:
            rank = 0
        elif intent.id == "in_position" and expected_l in _TAKEOFF_CLEAR_TEMPLATES:
            rank = 0
        elif expected_l == "rolling_accept" and intent.id in _ROLLING_OFFER_INTENTS:
            rank = 0
        elif intent.id == "departure_check_in" and expected_l == "radar_contact":
            rank = 0
        elif intent.id in _TOWER_CHECKIN_INTENTS and expected_l in _TOWER_CHECKIN_TEMPLATES:
            rank = 0
        elif intent.id == "bandsaw_check_out" and expected_l in (
            "bandsaw_check_in",
            "bandsaw_check_out",
        ):
            # Parked on optional check-in until checkout — tip that call.
            rank = 0
        elif intent.id == "range_entry" and expected_l == "bj_range_exit":
            # Back from Bandsaw / still on the range — tip check-in (continue).
            rank = 0
        elif intent.id == "range_exit" and expected_l == "bj_range_exit":
            rank = 0
        elif intent.id == "inbound_recovery" and expected_l in (
            "approach_check_in",
            "approach_procedure",
        ):
            rank = 0
        elif (
            intent.id in _APPROACH_TOWER_HANDOFF_INTENTS
            and expected_l in _APPROACH_TOWER_HANDOFF_TEMPLATES
        ):
            rank = 0
        elif intent.id in (
            "inbound_recovery",
            "request_approach",
            "request_hold",
            "request_vectors",
            "approach_continue",
            "approach_established",
        ) and (
            expected_l
            in (
                "approach_check_in",
                "approach_procedure",
            )
            or (
                channel_l == "approach"
                and expected_l not in _APPROACH_TOWER_HANDOFF_TEMPLATES
            )
        ):
            rank = 0 if intent.id == "inbound_recovery" else 1
        # Winds / altimeter are fair game on Approach — keep them on the kneeboard.
        elif intent.id == "going_around" and (
            expected_l in _LANDING_GO_AROUND_TEMPLATES
            or (channel_l == "tower" and phase_l == "approach")
        ):
            rank = 0
        elif intent.id in ("request_winds", "request_altimeter") and channel_l in (
            "approach",
            "tower",
            "ground",
        ):
            # Prefer on ALSO AVAILABLE; rank still orders within optional.
            rank = 0 if intent.id == "request_winds" else 1
        elif expected and intent.template and intent.template == expected:
            rank = 0
        elif intent.phases or intent.channels:
            rank = 1
        else:
            rank = 2

        # Step / mission phrases that are the expected call → advance.
        # Requests and actions → optional (even when ranked high for visibility).
        if intent.kind == "step" or intent.step_id:
            role = "advance" if rank == 0 else ""
        elif intent.kind in ("request", "action"):
            role = "optional"
        else:
            role = "optional"
        if not role:
            # Lower-priority step intents stay off ALSO AVAILABLE.
            continue
        ranked.append((rank, i, intent, role))

    ranked.sort(key=lambda r: (0 if r[3] == "advance" else 1, r[0], r[1]))

    adv_cap = advance_limit if advance_limit is not None else limit
    opt_cap = optional_limit if optional_limit is not None else limit
    n_adv = sum(1 for row in out if len(row) >= 3 and row[2] == "advance")
    n_opt = 0
    for _rank, _i, intent, role in ranked:
        if len(out) >= limit:
            break
        if role == "advance":
            if n_adv >= adv_cap:
                continue
            n_adv += 1
        else:
            if n_opt >= opt_cap:
                continue
            n_opt += 1
        out.append((intent.example, intent.does, role, cue_needs_agency(
            intent, expected=expected_l, awaiting_readback=awaiting_readback
        )))
    return out


def suggestion_pairs(
    *args: Any,
    **kwargs: Any,
) -> list[tuple[str, str]]:
    """Compatibility: suggestions as (say, does) without role."""
    return [(say, does) for say, does, _role, *_rest in suggestions(*args, **kwargs)]
