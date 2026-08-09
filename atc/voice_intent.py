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
from dataclasses import dataclass, field
from typing import Any

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
    "overhead": ("overhead", "over head"),
    "straight_in": ("straight in", "straight-in", "straight end"),
    "instrument": ("instrument", "ils", "tacan approach", "precision"),
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
    return " ".join(out)


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
    ("ops", ("ops", "operations", "base ops")),
    ("other", ("center", "centre", "control")),
)

_ADDRESS_TOKEN_WINDOW = 6


def extract_channel(text: str) -> str | None:
    """Agency the pilot addressed, from the opening words of the call."""
    head = " ".join(text.split()[:_ADDRESS_TOKEN_WINDOW])
    if not head:
        return None
    for channel, terms in _AGENCY_TERMS:
        for term in terms:
            if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", head):
                return channel
    return None


# ---- who the transmission is actually for -------------------------------
#
# The PTT is shared with the rest of the flight, so most of what we hear is
# not ATC business at all. Deciding *who* was called is what keeps "Two, go
# button five" from taxiing the jet.

# A bare position call opens a transmission to a wingman, never to ATC.
_POSITION_WORDS = frozenset({"2", "3", "4", "5", "6", "lead", "dash"})

# Phrases that are only ever said inside the flight or on a range frequency.
_CREW_ONLY_TERMS = (
    "go button", "push button", "button up", "chattermark", "switch button",
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


def analyze_address(text: str, callsign: str = "") -> Address:
    """
    Work out who a normalized transmission was addressed to.

    `callsign` is our own ("FLEECE 1"), which is what separates us from the
    rest of the formation — if we are Fleece 2 then "Fleece 2" is us and
    "Fleece 3" is someone else.
    """
    tokens = text.split()
    if not tokens:
        return Address()

    head = " ".join(tokens[:_ADDRESS_TOKEN_WINDOW])
    agency = extract_channel(text)
    own = False
    member = False

    word, number = _split_callsign(normalize(callsign))
    if word:
        for hit in re.finditer(rf"(?<!\w){re.escape(word)}(?:\s+(\d+))?(?!\w)", head):
            said = hit.group(1)
            if said is None or (number and said == number) or not number:
                own = True
            else:
                member = True
        if re.search(rf"(?<!\w){re.escape(word)}\s+(?:flight|formation)(?!\w)", head):
            own = True

    # "Two, ..." / "Dash three, ..." — a call across the formation.
    if tokens[0] in _POSITION_WORDS:
        if tokens[0] == "dash":
            member = len(tokens) > 1 and tokens[1] in _POSITION_WORDS
        else:
            member = True

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
    channels: tuple[str, ...] = ()  # agencies this makes sense on
    phases: tuple[str, ...] = ()  # flight phases this makes sense in; () = any
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
        weight=1.2,
        example="request runway two one left",
        does="change the active runway",
    ),
    Intent(
        "request_winds",
        (_ASKING + ("how",), ("wind", "winds")),
        example="say winds",
        does="current wind from the METAR",
    ),
    Intent(
        "request_altimeter",
        (_ASKING, ("altimeter", "qnh")),
        example="say altimeter",
        does="current altimeter setting",
    ),
    Intent(
        "request_picture",
        (
            _ASKING + ("declare",),
            ("picture", "pitcher", "bogey dope"),
        ),
        channels=("blackjack", "ops", "other"),
        weight=1.2,
        example="request picture",
        does="hostile groups off the live radar",
    ),
    Intent(
        "request_alpha_check",
        (("alpha check", "alfa check", "position check"),),
        channels=("blackjack", "ops", "other"),
        example="alpha check bullseye",
        does="your position off bullseye",
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
        phases=("ground", "tower"),
        veto=("unable", "negative", "full length"),
        example="we'll take the rolling",
        does="accept a rolling takeoff",
    ),
    Intent(
        "deny_rolling",
        (("rolling", "roll"), ("unable", "negative", "rather not", "full length")),
        phases=("ground", "tower"),
        weight=1.1,
        example="unable rolling",
        does="decline the rolling takeoff",
    ),
    Intent(
        "request_lineup",
        (
            ("line up", "lineup", "position and hold"),
            ("request", "requesting", "ready", "like"),
        ),
        phases=("ground", "tower"),
        example="ready for line up",
        does="line up and wait",
    ),
    # ---- scripted step triggers -----------------------------------------
    Intent(
        "ready_clearance",
        (("clearance", "ifr", "copy"), ("request", "requesting", "ready", "like")),
        kind="step",
        template="clearance",
        channels=("delivery",),
        phases=("delivery",),
        example="ready to copy IFR clearance",
        does="get your clearance",
        veto=("readback", "squawk", "as filed"),
    ),
    # After ATC issues a clearance the pilot reads the key items back — no
    # agency opener required while awaiting_readback is set.
    Intent(
        "acknowledge_readback",
        (
            (
                # Short acks for any readback window. Clearance specifically
                # also matches via _heard_assigned_squawk (squawk / squawking CODE).
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
        phases=("delivery", "ground"),
        veto=("taxi in", "clear of the", "off the active"),
        example="ready to taxi",
        does="taxi clearance",
    ),
    Intent(
        "ready_departure",
        (
            ("ready", "number 1", "holding short", "request", "requesting"),
            ("departure", "takeoff", "take off", "the active", "to go", "for the go"),
        ),
        kind="step",
        template="clear_takeoff",
        channels=("tower",),
        phases=("ground", "tower"),
        veto=("contact departure", "with departure", "rolling"),
        example="ready for departure",
        does="takeoff clearance",
    ),
    Intent(
        "inbound_recovery",
        (
            ("inbound", "recovery", "recover", "rtb", "overhead", "straight in"),
            ("request", "requesting", "for", "with you", "checking in", "inbound"),
        ),
        kind="step",
        template="approach_check_in",
        channels=("approach", "departure"),
        phases=("departure", "blackjack", "approach"),
        example="inbound for the overhead",
        does="recovery instructions",
    ),
    Intent(
        "request_landing",
        (
            ("gear down", "full stop", "landing", "initial", "the break"),
            ("request", "requesting", "for", "with", "gear", "full"),
        ),
        kind="step",
        template="clear_land",
        channels=("tower",),
        phases=("approach", "tower"),
        example="gear down full stop",
        does="landing clearance",
    ),
    Intent(
        "going_around",
        (("going around", "go around", "waving off", "wave off"),),
        kind="step",
        template="go_around",
        channels=("tower",),
        phases=("approach", "tower"),
        example="going around",
        does="go-around instructions",
    ),
    Intent(
        "clear_of_runway",
        (
            ("clear of the runway", "clear of runway", "off the active", "taxi in", "clear the active"),
        ),
        kind="step",
        template="taxi_in",
        channels=("ground", "tower"),
        phases=("tower", "ground"),
        weight=1.1,
        example="clear of the runway",
        does="taxi back to parking",
    ),
    Intent(
        "range_entry",
        (("range",), ("entry", "enter", "entering", "check in", "checking in", "on station")),
        kind="step",
        template="bj_check_in",
        channels=("blackjack",),
        phases=("departure", "blackjack"),
        example="checking in for range entry",
        does="range check-in",
    ),
    Intent(
        "range_exit",
        (("range", "station"), ("exit", "exiting", "departing", "off", "complete", "detached")),
        kind="step",
        template="bj_range_exit",
        channels=("blackjack",),
        phases=("blackjack", "approach"),
        example="off station, range complete",
        does="range exit",
    ),
    Intent(
        "check_in",
        (("checking in", "check in", "with you", "on frequency", "as fragged", "radio check"),),
        kind="step",
        template="",
        weight=0.9,
        example="checking in",
        does="check in on this frequency",
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
        channel = str(step.get("channel") or step.get("phase") or "").strip().lower()
        phase = str(step.get("phase") or step.get("channel") or "").strip().lower()
        step_id = str(step.get("id") or "").strip()
        out.append(
            Intent(
                id=f"step:{step_id or step.get('label') or 'custom'}",
                groups=(phrases,),
                kind="step",
                template=str(step.get("template") or "").strip(),
                channels=(channel,) if channel else (),
                phases=(phase,) if phase else (),
                # Nudged above the stock grammar: hand-written for this step.
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
_ADDRESS_OPTIONAL_INTENTS = frozenset({"acknowledge_readback", "say_again"})

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


def _expected_now(intent: Intent, *, expected: str, awaiting_readback: bool) -> bool:
    """Is this the call ATC is sitting there waiting for?"""
    if expected and intent.template and intent.template == expected:
        return True
    return awaiting_readback and intent.id in _ADDRESS_OPTIONAL_INTENTS


def _readback_value_hit(text: str, items: list[dict[str, Any]] | None) -> bool:
    """True when the transcript includes a assigned value (squawk digits, runway…)."""
    if not items:
        return False
    for item in items:
        value = re.sub(r"\D", "", str(item.get("value") or ""))
        if len(value) >= 3 and re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text):
            return True
        spoken = normalize(str(item.get("spoken") or ""))
        if spoken and re.search(rf"(?<!\w){re.escape(spoken)}(?!\w)", text):
            return True
    return False


def _squawk_code(items: list[dict[str, Any]] | None) -> str | None:
    """Four-digit Mode 3 from the outstanding readback checklist, if any."""
    for item in items or []:
        if str(item.get("key") or "") != "squawk":
            continue
        digits = re.sub(r"\D", "", str(item.get("value") or ""))
        if len(digits) >= 4:
            return digits[:4]
    return None


def _heard_assigned_squawk(text: str, code: str) -> bool:
    """
    Clearance readback hinge: 'squawk 0551' or 'squawking 0551'.

    Digits are already collapsed by normalize(), so spoken 'zero five five one'
    arrives here as '0551'. Extra words around the code are fine; 'in sequence'
    is never required. A bare code with no squawk/squawking word does not count
    — altitudes and frequencies look the same as Mode 3 codes.
    """
    if not code or len(code) < 4:
        return False
    return bool(
        re.search(rf"(?<!\w)squawk(?:ing)?\s+{re.escape(code)}(?!\w)", text)
    )


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
) -> Match | None:
    """Highest-scoring intent for a transcript, before any addressing gate."""
    best: Match | None = None
    for intent in tuple(INTENTS) + tuple(extra):
        if intent.veto and _group_hit(text, intent.veto, fuzzy=False):
            continue
        # A bare acknowledgement only means something while ATC is waiting on
        # one; the rest of the time "roger" is just talk.
        if intent.id == "acknowledge_readback" and not awaiting_readback:
            continue
        expecting = _expected_now(
            intent, expected=expected, awaiting_readback=awaiting_readback
        )
        heard_exactly = True
        summarised = False
        # Clearance readback: the assigned squawk is the hinge. A long call that
        # includes "squawk 0551" / "squawking 0551" fires; "in sequence" and the
        # rest of the clearance items are optional colour.
        squawk_code = (
            _squawk_code(readback_items)
            if intent.id == "acknowledge_readback"
            else None
        )
        squawk_hit = bool(
            squawk_code and _heard_assigned_squawk(text, squawk_code)
        )
        if squawk_hit:
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
            # With a known squawk outstanding, "squawk" alone is not enough —
            # that word without the digits used to false-fire. Short acks
            # (roger / copy) and the assigned code still count.
            if (
                intent.id == "acknowledge_readback"
                and squawk_code
                and not squawk_hit
                and not any(
                    h in ("roger", "wilco", "copy", "readback", "read back")
                    for h in matched
                )
            ):
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
            if squawk_hit or _readback_value_hit(text, readback_items):
                confidence *= 1.15
        # Long clearance readbacks bury one keyword in twenty words — once the
        # assigned squawk is heard, do not let coverage pull the score under.
        if squawk_hit:
            confidence = max(confidence, 0.95)
        confidence = min(1.0, confidence)

        slots: dict[str, Any] = {}
        if intent.id == "request_runway":
            runway = extract_runway(text, runways)
            if not runway:
                continue
            slots["runway"] = runway
        recovery = extract_recovery(text)
        if recovery and intent.id in ("inbound_recovery", "request_landing"):
            slots["recovery"] = recovery

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

    address = analyze_address(text, callsign)
    # Who the pilot called outranks where the timeline cursor happens to sit.
    channel = address.agency or channel

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
        extra=step_intents(steps),
    )
    result = Evaluation(
        transcript=transcript, normalized=text, candidate=candidate, address=address
    )

    # Wording the mission author typed for this step, said word for word. It
    # outranks the loose chatter filters — casual phrasing is the whole point of
    # writing your own trigger. Addressing still has to check out.
    authored = _said_authored_phrase(candidate, steps, text)

    crew_term = _first_term(text, _CREW_ONLY_TERMS)
    if crew_term:
        result.reason = f"flight chatter (“{crew_term}”)"
        return result
    if address.flight_member:
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
            "say squawk / squawking + your code (extra words are fine)"
            if awaiting_readback and _squawk_code(readback_items)
            else "read back the highlighted items"
            if awaiting_readback
            else "try one of the calls on the Fly tab"
        )
        return result

    # ATC has just spoken and is holding for an answer, so the reply it is
    # waiting on does not have to open with the agency all over again.
    address_optional = awaiting_readback and (
        candidate.expected or candidate.intent in _ADDRESS_OPTIONAL_INTENTS
    )
    if require_address and not address_optional and not (
        address.to_atc or address.own_callsign
    ):
        result.reason = "no agency addressed"
        result.advice = (
            "read back the squawk / clearance items — agency name optional now"
            if awaiting_readback
            else "open with the agency, e.g. “Tower, …”"
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
    "ops": "{ap} Ops",
    "other": "Control",
}


def agency_spoken(channel: str, airport_name: str = "") -> str:
    """'ground' -> 'Nellis Ground'. Falls back to a bare agency name."""
    template = _AGENCY_SPOKEN.get((channel or "").lower())
    if not template:
        return (channel or "").title()
    return template.format(ap=airport_name.strip() or "").strip()


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
) -> list[tuple[str, str]]:
    """
    (what to say, what it does) for where the flight is right now.

    Normally written out in full — agency, callsign, request. While a readback
    is outstanding the lead suggestion is the readback itself, with no agency
    opener, because that is what ATC is waiting for.
    """
    out: list[tuple[str, str]] = []
    if awaiting_readback:
        squawk_item = next(
            (i for i in (readback_items or []) if i.get("key") == "squawk"),
            None,
        )
        if squawk_item and squawk_item.get("spoken"):
            # Clearance: the code is the only required piece. Extra route /
            # climb colour is fine; "in sequence" is never required.
            out.append(
                (
                    str(squawk_item["spoken"]).strip(),
                    "must hear — squawk / squawking + code (rest optional)",
                )
            )
        else:
            spoken_bits = [
                str(item.get("spoken") or "").strip()
                for item in (readback_items or [])
                if item.get("spoken")
            ]
            if spoken_bits:
                out.append(
                    (
                        ", ".join(spoken_bits[:3]),
                        "confirm your readback — agency name optional",
                    )
                )
            else:
                out.append(
                    (
                        "roger",
                        "confirm your readback — agency name optional",
                    )
                )

    ranked: list[tuple[int, int, Intent]] = []
    # Mission-authored phrases first — they were written for this exact step.
    catalog = list(step_intents(steps)) + list(INTENTS)
    for i, intent in enumerate(catalog):
        if not intent.example:
            continue
        if intent.id == "acknowledge_readback":
            # Only ever valid mid-readback, and the lead line above covers it.
            continue
        if intent.phases and phase and phase not in intent.phases:
            continue
        if intent.step_id:
            rank = 0
        elif expected and intent.template and intent.template == expected:
            rank = 0
        elif intent.phases:
            rank = 1
        else:
            rank = 2
        ranked.append((rank, i, intent))

    ranked.sort(key=lambda r: (r[0], r[1]))

    for _rank, _i, intent in ranked:
        if len(out) >= limit:
            break
        agency = intent.channels[0] if intent.channels else (channel or "")
        opener = ", ".join(
            bit for bit in (agency_spoken(agency, airport_name), callsign.title()) if bit
        )
        # Readback replies stay bare; everything else keeps the agency opener.
        if intent.id in _ADDRESS_OPTIONAL_INTENTS and awaiting_readback:
            say = intent.example
        else:
            say = f"{opener}, {intent.example}" if opener else intent.example
        out.append((say, intent.does))
    return out
