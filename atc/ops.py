"""
Squadron OPS — preflight WORDS / start approval and postflight codes.

Separate from field ATC. WORDS and sortie logs are built so an Opus/CAOC
provider can be dropped in later without rewriting the radio logic.

INTEGRATION POINTS (not implemented):
  * OpusWordsProvider.current_words  — live WORDS from CAOC
  * log_sortie                       — persist start / codes / time to CAOC
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol
import json
import re

import atc_phrase

HERE = Path(__file__).resolve().parent
WORDS_PATH = HERE / "ops_words.json"
SORTIE_STATE_KEY = "ops_sortie"

# Shared across seats on the same Opus flight (timer + codes).
SHARED_STATE_KEYS: tuple[str, ...] = (SORTIE_STATE_KEY,)

_NATO = (
    "Alpha",
    "Bravo",
    "Charlie",
    "Delta",
    "Echo",
    "Foxtrot",
    "Golf",
    "Hotel",
    "India",
    "Juliet",
    "Kilo",
    "Lima",
    "Mike",
    "November",
    "Oscar",
    "Papa",
    "Quebec",
    "Romeo",
    "Sierra",
    "Tango",
    "Uniform",
    "Victor",
    "Whiskey",
    "X-ray",
    "Yankee",
    "Zulu",
)
_DIGIT_WORDS = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "tree": "3",
    "four": "4",
    "fower": "4",
    "five": "5",
    "fife": "5",
}


@dataclass(frozen=True)
class WordsBulletin:
    """Current WORDS. Extra fields stay empty until CAOC fills them."""

    id: str
    ato_day: str
    update: int
    items: tuple[str, ...] = ()
    weather: str = ""
    range_status: str = ""
    timing: str = ""
    threat: str = ""
    package: str = ""
    frequencies: str = ""
    source: str = "mock"
    raw: dict[str, Any] = field(default_factory=dict)

    def spoken_id(self) -> str:
        return speak_words_id(self.id)


@dataclass
class AircraftCode:
    seat: int
    code: int
    callsign: str = ""


@dataclass
class OpsSortie:
    """Structured log for one flight — ready to POST to CAOC later."""

    flight_id: str = ""
    callsign: str = ""
    words_id: str = ""
    start_utc: str = ""
    start_hhmm: str = ""
    start_epoch: float = 0.0
    end_utc: str = ""
    end_hhmm: str = ""
    end_epoch: float = 0.0
    total_hours: float | None = None
    codes: list[AircraftCode] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class WordsProvider(Protocol):
    def current_words(
        self,
        *,
        when: datetime,
        config: dict[str, Any] | None = None,
        opus: Any = None,
    ) -> WordsBulletin: ...


class MockWordsProvider:
    """Configurable / file-backed WORDS. Default until CAOC is wired."""

    def current_words(
        self,
        *,
        when: datetime,
        config: dict[str, Any] | None = None,
        opus: Any = None,
    ) -> WordsBulletin:
        del opus
        path = WORDS_PATH
        raw_path = str((config or {}).get("ops_words_file") or "").strip()
        if raw_path:
            try:
                path = atc_phrase.resolve_repo_path(raw_path)
            except Exception:
                path = WORDS_PATH
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}
        cfg = config or {}
        if cfg.get("ops_words_id"):
            data = dict(data)
            data["id"] = str(cfg.get("ops_words_id") or "")
        if cfg.get("ops_words_update") not in (None, ""):
            data = dict(data)
            data["update"] = cfg.get("ops_words_update")
        if cfg.get("ops_words_items"):
            data = dict(data)
            data["items"] = cfg.get("ops_words_items")
        return bulletin_from_payload(data, when=when, source="mock")


class OpusWordsProvider:
    """
    INTEGRATION POINT: OPUS/CAOC WORDS retrieval.

    Not implemented. current_words() falls back to the mock file so radio
    logic does not need a rewrite when this is filled in.
    """

    def current_words(
        self,
        *,
        when: datetime,
        config: dict[str, Any] | None = None,
        opus: Any = None,
    ) -> WordsBulletin:
        # TODO: GET CAOC WORDS for this ATO day / package. Use `opus` + config
        # (opus_backend_url) when the endpoint exists. Do not invent WORDS.
        del opus
        return MockWordsProvider().current_words(when=when, config=config, opus=None)


def words_provider(config: dict[str, Any] | None = None) -> WordsProvider:
    kind = str((config or {}).get("ops_words_provider") or "mock").strip().lower()
    if kind in ("opus", "caoc"):
        return OpusWordsProvider()
    return MockWordsProvider()


def ato_day_letters(day: date) -> str:
    """AA = January 1, AB = January 2, … wrapping AA–ZZ through the year."""
    n = max(0, int(day.timetuple().tm_yday) - 1)
    return chr(ord("A") + (n // 26)) + chr(ord("A") + (n % 26))


def words_id_for(day: date, update: int = 1) -> str:
    n = max(1, int(update))
    return f"{ato_day_letters(day)}{n:02d}"


def speak_words_id(raw: str) -> str:
    """AA01 → Alpha Alpha Zero-One."""
    token = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()
    if len(token) < 2:
        return token
    letters, digits = token[:2], token[2:]
    bits = [_NATO[ord(ch) - ord("A")] for ch in letters if "A" <= ch <= "Z"]
    if digits:
        bits.append(
            "-".join(
                w.title() if w.isalpha() else w
                for w in atc_phrase.speak_digits(digits).split()
            )
        )
    return " ".join(bits)


def bulletin_from_payload(
    data: dict[str, Any] | None,
    *,
    when: datetime,
    source: str = "mock",
) -> WordsBulletin:
    row = data if isinstance(data, dict) else {}
    update = 1
    try:
        update = max(1, int(row.get("update") or 1))
    except (TypeError, ValueError):
        update = 1
    ato = ato_day_letters(when.date())
    raw_id = str(row.get("id") or "").strip().upper()
    wid = raw_id if len(raw_id) >= 3 else words_id_for(when.date(), update)
    items_raw = row.get("items")
    items: list[str] = []
    if isinstance(items_raw, str) and items_raw.strip():
        items = [items_raw.strip()]
    elif isinstance(items_raw, list):
        items = [str(x).strip() for x in items_raw if str(x).strip()]
    extras = []
    for key in (
        "weather",
        "range_status",
        "timing",
        "threat",
        "package",
        "frequencies",
    ):
        val = str(row.get(key) or "").strip()
        if val and val not in items:
            extras.append(val)
    return WordsBulletin(
        id=wid,
        ato_day=ato,
        update=update,
        items=tuple(items + extras),
        weather=str(row.get("weather") or ""),
        range_status=str(row.get("range_status") or ""),
        timing=str(row.get("timing") or ""),
        threat=str(row.get("threat") or ""),
        package=str(row.get("package") or ""),
        frequencies=str(row.get("frequencies") or ""),
        source=source,
        raw=dict(row),
    )


def resolve_ops_clock(config: dict[str, Any] | None = None) -> datetime:
    """Real-world UTC/Zulu. Not the DCS / CAOC mission clock."""
    del config
    return datetime.now(timezone.utc)


def hhmm_of(when: datetime) -> str:
    return f"{when.hour:02d}{when.minute:02d}"


def speak_time_now(when: datetime) -> str:
    spoken = atc_phrase.speak_zulu_clock(f"{when.hour:02d}:{when.minute:02d}") or ""
    return f"time now {spoken}" if spoken else "time now unavailable"


def decimal_hours(seconds: float) -> float:
    """1 hour 42 minutes → 1.7."""
    if seconds < 0:
        seconds += 24 * 3600
    return round(seconds / 3600.0, 1)


def speak_decimal_hours(hours: float) -> str:
    whole = int(hours)
    tenth = int(round((hours - whole) * 10))
    if tenth >= 10:
        whole += 1
        tenth = 0
    if tenth == 0:
        return atc_phrase.speak_digits(str(whole))
    return f"{atc_phrase.speak_digits(str(whole))} point {atc_phrase.speak_digits(str(tenth))}"


def spoken_ops_name(
    airport: dict[str, Any] | None = None,
    *,
    opus: Any = None,
    config: dict[str, Any] | None = None,
    squadron_name: str | None = None,
) -> str:
    """Wool / Knight / Toro Ops from the same squadron match as parking."""
    return atc_phrase.resolve_ops_callsign(
        airport,
        opus=opus,
        config=config,
        squadron_name=squadron_name,
    )


def current_words(
    config: dict[str, Any] | None = None,
    *,
    opus: Any = None,
    when: datetime | None = None,
) -> WordsBulletin:
    clock = when or resolve_ops_clock(config)
    return words_provider(config).current_words(when=clock, config=config, opus=opus)


def sortie_from_state(state: dict[str, Any] | None) -> OpsSortie | None:
    raw = (state or {}).get(SORTIE_STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(raw, dict):
        return None
    codes = []
    for row in raw.get("codes") or []:
        if not isinstance(row, dict):
            continue
        try:
            codes.append(
                AircraftCode(
                    seat=int(row.get("seat") or 0),
                    code=int(row.get("code") or 0),
                    callsign=str(row.get("callsign") or ""),
                )
            )
        except (TypeError, ValueError):
            continue
    try:
        total = raw.get("total_hours")
        total_f = float(total) if total is not None else None
    except (TypeError, ValueError):
        total_f = None
    return OpsSortie(
        flight_id=str(raw.get("flight_id") or ""),
        callsign=str(raw.get("callsign") or ""),
        words_id=str(raw.get("words_id") or ""),
        start_utc=str(raw.get("start_utc") or ""),
        start_hhmm=str(raw.get("start_hhmm") or ""),
        start_epoch=float(raw.get("start_epoch") or 0) or 0.0,
        end_utc=str(raw.get("end_utc") or ""),
        end_hhmm=str(raw.get("end_hhmm") or ""),
        end_epoch=float(raw.get("end_epoch") or 0) or 0.0,
        total_hours=total_f,
        codes=codes,
    )


def write_sortie(state: dict[str, Any] | None, sortie: OpsSortie) -> None:
    if not isinstance(state, dict):
        return
    state[SORTIE_STATE_KEY] = sortie.as_dict()


def _flight_label(opus: Any, callsign: str) -> tuple[str, str]:
    cs = str(callsign or "").strip()
    fid = ""
    if opus is not None:
        fid = str(getattr(opus, "flight_id", "") or "")
        cs = str(getattr(opus, "radio_callsign", None) or cs)
        if hasattr(opus, "flight_callsign"):
            cs = str(opus.flight_callsign or cs)
    return fid, cs


def approve_start(
    state: dict[str, Any] | None,
    *,
    config: dict[str, Any] | None = None,
    opus: Any = None,
    callsign: str = "",
    words: WordsBulletin | None = None,
    when: datetime | None = None,
) -> OpsSortie:
    """Stamp START APPROVED. A second start does not reset an existing timer."""
    clock = when or resolve_ops_clock(config)
    existing = sortie_from_state(state)
    if existing and existing.start_utc:
        if words and not existing.words_id:
            existing.words_id = words.id
            write_sortie(state, existing)
        return existing
    fid, cs = _flight_label(opus, callsign)
    sortie = OpsSortie(
        flight_id=fid,
        callsign=cs,
        words_id=str((words.id if words else "") or ""),
        start_utc=clock.isoformat(),
        start_hhmm=hhmm_of(clock),
        start_epoch=clock.timestamp(),
    )
    write_sortie(state, sortie)
    log_sortie(sortie, config=config)
    return sortie


def record_status(
    state: dict[str, Any] | None,
    codes: list[AircraftCode],
    *,
    config: dict[str, Any] | None = None,
    opus: Any = None,
    callsign: str = "",
    when: datetime | None = None,
) -> OpsSortie:
    clock = when or resolve_ops_clock(config)
    sortie = sortie_from_state(state) or OpsSortie()
    fid, cs = _flight_label(opus, callsign)
    sortie.flight_id = sortie.flight_id or fid
    sortie.callsign = sortie.callsign or cs
    sortie.end_utc = clock.isoformat()
    sortie.end_hhmm = hhmm_of(clock)
    sortie.end_epoch = clock.timestamp()
    if codes:
        sortie.codes = list(codes)
    if sortie.start_epoch:
        delta = sortie.end_epoch - sortie.start_epoch
        if delta < 0:
            delta += 24 * 3600
        sortie.total_hours = decimal_hours(delta)
    else:
        sortie.total_hours = None
    write_sortie(state, sortie)
    log_sortie(sortie, config=config)
    return sortie


def log_sortie(sortie: OpsSortie, *, config: dict[str, Any] | None = None) -> None:
    """
    INTEGRATION POINT: flight-time / aircraft-status logging to OPUS/CAOC.

    Today this is a no-op besides keeping the structured object on flow state.
    """
    del sortie, config


_CODE_WORD = {
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    **{word: int(n) for word, n in _DIGIT_WORDS.items() if n in "12345"},
}


def parse_aircraft_codes(
    transcript: str,
    *,
    flight_callsign: str = "",
) -> list[AircraftCode]:
    """
    'Snake 5-1 Code 1, Snake 5-2 Code 2' or 'dash 3 code two'.
    """
    text = re.sub(r"[^a-z0-9\s\-]", " ", (transcript or "").casefold())
    text = re.sub(r"\s+", " ", text).strip()
    flight = re.sub(r"[^a-z0-9]+", " ", (flight_callsign or "").casefold()).strip()
    out: list[AircraftCode] = []
    seen: set[int] = set()

    def _add(seat: int, code: int, label: str = "") -> None:
        if seat <= 0 or code not in {1, 2, 3, 4, 5} or seat in seen:
            return
        seen.add(seat)
        out.append(AircraftCode(seat=seat, code=code, callsign=label.strip()))

    pat = re.compile(
        r"(?:(?P<label>[a-z]+)\s+)?"
        r"(?:(?P<num>\d+)\s*[-.]?\s*)?"
        r"(?:dash\s+)?"
        r"(?P<seat>\d+|one|two|three|tree|four|fower|five|fife)"
        r"\s+code\s+"
        r"(?P<code>one|two|three|tree|four|fower|five|fife|[1-5])",
        re.I,
    )
    for m in pat.finditer(text):
        seat_tok = (m.group("seat") or "").casefold()
        code_tok = (m.group("code") or "").casefold()
        try:
            seat = int(seat_tok) if seat_tok.isdigit() else int(_DIGIT_WORDS.get(seat_tok) or 0)
        except (TypeError, ValueError):
            continue
        code = _CODE_WORD.get(code_tok)
        if code is None:
            continue
        label = (m.group("label") or "").strip()
        if m.group("num") and label:
            label = f"{label} {m.group('num')}-{seat}"
        elif flight:
            label = f"{flight} {seat}".strip()
        _add(seat, code, label)

    if not out:
        bare = re.search(
            r"\bcode\s+(one|two|three|tree|four|fower|five|fife|[1-5])\b",
            text,
        )
        if bare:
            code = _CODE_WORD.get(bare.group(1).casefold())
            if code:
                _add(1, code, flight_callsign)
    return out


def build_words_reply(
    callsign: str,
    words: WordsBulletin,
    *,
    airport: dict[str, Any] | None = None,
    start: bool = False,
    when: datetime | None = None,
    already_started: bool = False,
    opus: Any = None,
    config: dict[str, Any] | None = None,
) -> str:
    cs = atc_phrase.speak_callsign(callsign)
    agency = spoken_ops_name(airport, opus=opus, config=config)
    bits = [f"{cs}, {agency}, WORDS {words.spoken_id()} current"]
    for item in words.items:
        if item and item.casefold() not in bits[-1].casefold():
            bits.append(item.rstrip("."))
    if start and when is not None:
        bits.append(speak_time_now(when).rstrip(".").replace("time now ", "Time now ", 1))
    if start and not already_started:
        bits.append("Start approved")
    body = ". ".join(bits)
    if not body.endswith("."):
        body += "."
    return body


def build_start_reply(
    callsign: str,
    *,
    airport: dict[str, Any] | None = None,
    words: WordsBulletin | None = None,
    when: datetime,
    already_started: bool = False,
    opus: Any = None,
    config: dict[str, Any] | None = None,
) -> str:
    if words is not None:
        return build_words_reply(
            callsign,
            words,
            airport=airport,
            start=True,
            when=when,
            already_started=already_started,
            opus=opus,
            config=config,
        )
    cs = atc_phrase.speak_callsign(callsign)
    agency = spoken_ops_name(airport, opus=opus, config=config)
    if already_started:
        return f"{cs}, {agency}, start already approved. {speak_time_now(when)}."
    return f"{cs}, {agency}, start approved. {speak_time_now(when)}."


def build_status_reply(
    callsign: str,
    sortie: OpsSortie,
    *,
    airport: dict[str, Any] | None = None,
    when: datetime,
    opus: Any = None,
    config: dict[str, Any] | None = None,
) -> str:
    cs = atc_phrase.speak_callsign(callsign)
    agency = "Ops"
    bits = [f"{cs}, {agency}, copy codes"]
    bits.append(speak_time_now(when))
    if sortie.total_hours is not None:
        bits.append(f"total time {speak_decimal_hours(float(sortie.total_hours))}")
    return ", ".join(bits) + "."


def build_ops_check_in(
    callsign: str,
    airport: dict[str, Any] | None = None,
    *,
    opus: Any = None,
    config: dict[str, Any] | None = None,
) -> str:
    cs = atc_phrase.speak_callsign(callsign)
    return f"{cs}, {spoken_ops_name(airport, opus=opus, config=config)}, go ahead."
