"""
Boom / reform small talk on tanker freq (ATP-56 join is separate).

Texaco keeps an informal side-channel going while the receiver is in reform or
on the boom — no callsign addressing. The boom operator is enlisted USAF talking
to an F-16 officer on a military tanker: dry and funny, not buddy-buddy, not
airline. Bits are mixed:

  • hello — first contact only: time-of-day pleasantry (Good morning, sir)
  • ab    — short A/B (or A/B/C) poll; one-word answers
  • riff  — observation / banter; optional freeform react, then auto-continues
  • open  — open question; freeform reply, then continues

Not every turn is Ask → Answer → Respond. Replies can run a couple sentences,
and a choice can chain into another bit. After a turn Texaco pauses, then comes
back until the pilot stops the chat or leaves the tanker. Canned library always
available; optional LLM mints fresh bits and falls back if slow or down.
"""

from __future__ import annotations

import copy
import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import atc_phrase
import tanker_chat_library as chat_lib

_STATE_KEY = "tanker_chat"
_LAST_KEY = "tanker_chat_last_id"
_RECENT_KEY = "tanker_chat_recent"
_HISTORY_KEY = "tanker_chat_history"
_GREETED_KEY = "tanker_chat_greeted"
_GREET_ID = "hello_sir"
_HISTORY_MAX = 8
_RECENT_MAX = 28
_LLM_TIMEOUT_S = 6.5
_OLLAMA_TIMEOUT_S = 35.0
_OLLAMA_REACT_TIMEOUT_S = 20.0
_OLLAMA_NUM_PREDICT = 220
_OLLAMA_REACT_NUM_PREDICT = 110
_OLLAMA_KEEP_ALIVE = "10m"
# llama3.2 likes to keep writing the pilot's next line. Cut it off.
_OLLAMA_STOP = (
    "Pilot:",
    "\nPilot",
    "Fighter:",
    "\nFighter",
    "MIC:",
    "Boom:",
    "Texaco:",
    "F-16:",
    "F16:",
    "Receiver:",
)
_LLM_NOTE_KEY = "tanker_chat_llm_note"
_GUARD_UNTIL_KEY = "tanker_chat_guard_until"
_LAST_SPOKE_KEY = "tanker_chat_last_spoke"
_MAX_FREEFORM_TURNS = 6
_ECHO_GUARD_S = 5.0

_COFFEE_WORDS = (
    "coffee",
    "keurig",
    "dunkin",
    "starbucks",
    "espresso",
    "caffeine",
    "latte",
    "cappuccino",
    "french press",
    "k cup",
    "brew",
)

# Pause between one answered bit and the next question (ongoing session).
_BREAK_MIN_S = 14.0
_BREAK_MAX_S = 36.0
# Shorter gaps when a live model is driving the chat.
_LLM_BREAK_MIN_S = 8.0
_LLM_BREAK_MAX_S = 22.0

# Last Ollama/Gemini/OpenAI failure (for Fly status / logs).
_LAST_LLM_ERROR: str = ""
_OLLAMA_LOCK = threading.Lock()
_OLLAMA_MODELS_CACHE: dict[str, Any] = {"t": 0.0, "names": []}

# Soft acks acknowledge without picking a side — still keep the session going.
_SOFT_ACK = (
    "roger",
    "copy",
    "yeah",
    "yup",
    "yes",
    "affirm",
    "affirmative",
    "got it",
)

# Pilot ends the conversation (checkout / "enough").
_STOP_HITS = (
    "talk later",
    "talk to you later",
    "catch you later",
    "gotta go",
    "got to go",
    "gotta run",
    "check out",
    "checking out",
    "stop chatting",
    "stop the chat",
    "end chat",
    "enough chat",
    "enough talking",
    "quiet for a bit",
    "standing by",
    "back to work",
    "that'll be all",
    "that will be all",
    "later texaco",
    "see you later",
)

_STOP_REPLY = "Standing by, sir — looking good."

# Freeform / soft reacts — skip the endless "copy" chorus.
# Enlisted boom operator to officer: respectful, not peer banter.
_DEFAULT_REACT_REPLIES = (
    "Ha — looking good on the boom, sir.",
    "We'll keep her steady, sir.",
    "Yeah, fair. You're looking stable from here.",
    "Good day for gas, sir.",
    "Alright. Stay boring.",
    "True enough. Hang in there.",
    "Fair enough, sir.",
    "That tracks.",
)

_QUESTION_CUES = {
    "what",
    "what's",
    "whats",
    "why",
    "how",
    "when",
    "where",
    "who",
    "which",
    "whose",
    "do",
    "does",
    "did",
    "is",
    "are",
    "was",
    "were",
    "can",
    "could",
    "would",
    "will",
    "have",
    "has",
    "any",
    "ever",
    "got",
    "prefer",
    "better",
    "think",
    "know",
    "tell",
    "ask",
}


# "Dunkin, copy. …" / "Copy Dunkin. …" / "Copy that. …" — usually drop the echo.
_ECHO_LEAD = re.compile(
    r"""^
    (?:
        [\w']+(?:\s+[\w']+){0,3},?\s+(?:copy|roger)\.?\s+
      | Copy\s+[\w']+(?:\s+[\w']+){0,2}\.?\s+
      | Copy(?:\s+that)?\.?\s+
      | Roger(?:\s+that)?\.?\s+
      | Ha\s*[—\-]+\s*copy(?:\s+that)?\.?\s+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_BAN_HITS = {
    "abort",
    "astern",
    "boom",
    "contact",
    "disconnect",
    "identified",
    "observation",
    "pre contact",
    "pre-contact",
    "precontact",
    "ready",
    "reform",
    "rejoin",
    "tanker",
    "texaco",
}

_DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_OLLAMA_MODEL = "llama3.2"
_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/v1/chat/completions"


def _row(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    row = state.get(_STATE_KEY)
    return row if isinstance(row, dict) else None


def is_awaiting_react(state: dict[str, Any] | None) -> bool:
    """True while a riff/open bit is waiting on any freeform / soft reply."""
    row = _row(state)
    return bool(row and str(row.get("awaiting") or "") == "react")


def is_open(state: dict[str, Any] | None) -> bool:
    """True while Texaco is waiting on an A/B pick or a freeform react."""
    row = _row(state)
    if not row:
        return False
    if row.get("choices"):
        return True
    return str(row.get("awaiting") or "") == "react"


def is_session_active(state: dict[str, Any] | None) -> bool:
    """True for the whole chat — open bit or short break between bits."""
    row = _row(state)
    if not row:
        return False
    if row.get("choices") or str(row.get("awaiting") or "") == "react":
        return True
    return bool(row.get("session"))


def current_choices(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    row = _row(state)
    if not row:
        return []
    raw = row.get("choices") or []
    return [c for c in raw if isinstance(c, dict) and c.get("id")]


def current_reacts(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    row = _row(state)
    if not row:
        return []
    raw = row.get("reacts") or []
    return [c for c in raw if isinstance(c, dict)]


def fly_request_rows(state: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Fly buttons: Texaco starts / answers / stop while a session is live."""
    rows: list[tuple[str, str]] = []
    if is_session_active(state):
        if not is_open(state):
            rows.append(("tanker_chat_start", "Texaco next bit"))
        elif is_awaiting_react(state) and not current_choices(state):
            rows.append(("tanker_chat_choice__ack", "Roger"))
        rows.append(("tanker_chat_stop", "Stop chat"))
    else:
        rows.append(("tanker_chat_start", "Texaco starts chat"))
    for choice in current_choices(state):
        say = str(choice.get("say") or choice.get("id") or "").strip()
        cid = str(choice.get("id") or "").strip()
        if say and cid:
            rows.append((f"tanker_chat_choice_{cid}", say))
    return rows


def fly_controls_visible(state: dict[str, Any] | None) -> bool:
    """Stop/answer boom-chat buttons after rejoin; start-chat stays tanker-freq only."""
    if is_session_active(state):
        return True
    try:
        import tanker as tanker_mod

        return tanker_mod.has_rejoined(state)
    except Exception:
        return False


def end_chat(state: dict[str, Any] | None) -> None:
    if isinstance(state, dict):
        state.pop(_STATE_KEY, None)
        state.pop(_HISTORY_KEY, None)
        state.pop(_GUARD_UNTIL_KEY, None)
        state.pop(_LAST_SPOKE_KEY, None)
        state.pop(_LLM_NOTE_KEY, None)


def last_llm_error() -> str:
    return str(_LAST_LLM_ERROR or "").strip()


def llm_note(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return last_llm_error()
    return str(state.get(_LLM_NOTE_KEY) or last_llm_error() or "").strip()


def last_spoke(state: dict[str, Any] | None) -> str:
    """Texaco's last boom line — opener or last TX, for Fly / voice cues."""
    if not isinstance(state, dict):
        return ""
    spoke = str(state.get(_LAST_SPOKE_KEY) or "").strip()
    if spoke:
        return spoke
    row = _row(state) or {}
    return str(row.get("opener") or "").strip()


def fly_boom_caption(state: dict[str, Any] | None, extra: str = "") -> str:
    """Kneeboard BOOM line: keep Texaco's words visible, not just a status note."""
    extra = str(extra or "").strip()
    if extra.lower().startswith("boom:"):
        extra = extra[5:].strip()
    session = is_session_active(state)
    spoke = last_spoke(state)
    note = ""
    if isinstance(state, dict):
        note = str(state.get(_LLM_NOTE_KEY) or "").strip()
    generating = "generat" in note.lower()
    if not session and not extra and not generating:
        return ""
    parts: list[str] = []
    if spoke:
        parts.append(f"TEXACO: {spoke}")
    elif session:
        parts.append("BOOM: chatting — say anything")
    elif extra:
        parts.append(f"BOOM: {extra}")
        extra = ""
    elif generating:
        parts.append(f"BOOM: {note}")
        note = ""
    if extra and extra not in (spoke or "") and extra not in " · ".join(parts):
        parts.append(extra)
    if note and note not in " · ".join(parts):
        parts.append(note)
    return " · ".join(p for p in parts if p)


def _set_llm_error(msg: str, state: dict[str, Any] | None = None) -> None:
    global _LAST_LLM_ERROR
    text = str(msg or "").strip()
    _LAST_LLM_ERROR = text
    if isinstance(state, dict):
        if text:
            state[_LLM_NOTE_KEY] = text
        else:
            state.pop(_LLM_NOTE_KEY, None)


def arm_tx_guard(state: dict[str, Any] | None, text: str = "") -> None:
    """
    After Texaco transmits, ignore freeform 'replies' for a few seconds.

    SRS playback into the mic was making Ollama answer itself, hang, then fall
    back to the offline library.
    """
    if not isinstance(state, dict):
        return
    state[_GUARD_UNTIL_KEY] = time.time() + _ECHO_GUARD_S
    spoke = " ".join(str(text or "").split()).strip()
    if spoke:
        state[_LAST_SPOKE_KEY] = spoke[:420]


def _word_set(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9']+", str(text or "").casefold())
        if len(w) > 2 and w not in {"the", "and", "for", "you", "that", "this"}
    }


def looks_like_own_echo(transcript: str, state: dict[str, Any] | None) -> bool:
    """True when the 'pilot' line is probably Texaco's last boom TX."""
    if not isinstance(state, dict):
        return False
    blob = " ".join(str(transcript or "").split()).strip()
    if len(blob) < 4:
        return True
    try:
        guard_until = float(state.get(_GUARD_UNTIL_KEY) or 0)
    except (TypeError, ValueError):
        guard_until = 0.0
    in_guard = time.time() < guard_until
    spoke = str(state.get(_LAST_SPOKE_KEY) or "").strip()
    row = _row(state) or {}
    if not spoke:
        spoke = str(row.get("opener") or state.get("last_tx_text") or "").strip()
    if not spoke:
        return bool(in_guard)
    wb = _word_set(blob)
    ws = _word_set(spoke)
    if not wb:
        return True
    if not ws:
        return bool(in_guard)
    overlap = len(wb & ws) / float(max(1, min(len(wb), len(ws))))
    # During the post-TX guard, be aggressive — mic often hears the boom play back.
    if in_guard and overlap >= 0.28:
        return True
    # One-word acks ("morning", "hey") share a word with the hello; that is not echo.
    if overlap >= 0.55 and (len(blob) >= 12 or len(wb) >= 4):
        return True
    # Near-substring echo of the opener / last line.
    compact_b = re.sub(r"[^a-z0-9]+", "", blob.casefold())
    compact_s = re.sub(r"[^a-z0-9]+", "", spoke.casefold())
    if len(compact_b) >= 12 and (
        compact_b in compact_s or compact_s in compact_b
    ):
        return True
    return False


def _pick_break_s(*, llm: bool = False) -> float:
    if llm:
        return random.uniform(_LLM_BREAK_MIN_S, _LLM_BREAK_MAX_S)
    return random.uniform(_BREAK_MIN_S, _BREAK_MAX_S)


def schedule_next_question(
    state: dict[str, Any],
    *,
    delay_s: float | None = None,
    llm: bool = False,
) -> float:
    """Keep the session, clear the open bit, and time the next opener."""
    wait = float(delay_s) if delay_s is not None else _pick_break_s(llm=llm)
    wait = max(6.0, wait)
    prev = _row(state) or {}
    state[_STATE_KEY] = {
        "id": str(prev.get("id") or ""),
        "choices": [],
        "reacts": [],
        "awaiting": None,
        "kind": None,
        "opener": "",
        "turns": 0,
        "session": True,
        "next_at": time.time() + wait,
        "break_s": wait,
    }
    return wait


def continuation_delay_s(state: dict[str, Any] | None) -> float | None:
    """Seconds until the next bit, or None if not waiting on a break/riff timer."""
    row = _row(state)
    if not row or not row.get("session"):
        return None
    # A/B still waiting on a pick — do not auto-advance.
    if row.get("choices"):
        return None
    try:
        next_at = float(row.get("next_at") or 0)
    except (TypeError, ValueError):
        return 0.0
    if next_at <= 0:
        return None
    return max(0.0, next_at - time.time())


def continuation_due(state: dict[str, Any] | None, *, now: float | None = None) -> bool:
    delay = continuation_delay_s(state)
    if delay is None:
        return False
    return delay <= 0.0


def match_stop(text: str) -> bool:
    try:
        import voice_intent

        blob = voice_intent.normalize(text)
    except Exception:
        blob = str(text or "").casefold()
    if not blob:
        return False
    for hit in _STOP_HITS:
        token = hit.casefold()
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", blob):
            return True
    return False


def stop_chat(
    state: dict[str, Any] | None,
    callsign: str,
    tanker: dict[str, Any] | None,
) -> str:
    """Pilot closes the conversation. Returns Texaco's sign-off (no callsigns)."""
    del callsign, tanker  # boom chat stays informal
    end_chat(state)
    return _STOP_REPLY


def naturalize_reply(reply: str, *, force: bool = False) -> str:
    """
    Drop the habitual 'Dunkin, copy.' / 'Copy that.' lead-in most of the time.

    Boom chat should react, not parrot the pilot's answer back every turn.
    """
    text = " ".join(str(reply or "").split()).strip()
    if not text:
        return text
    # Keep the echo rarely so it still sounds like radio once in a while.
    if not force and random.random() < 0.12:
        return text
    stripped = _ECHO_LEAD.sub("", text, count=1).strip(" ,")
    if len(stripped) < 8:
        return text
    return stripped[0].upper() + stripped[1:]


def looks_like_question_or_chat(text: str) -> bool:
    """
    True when the pilot said more than a one-word A/B pick.

    Long / question-shaped speech should go to the live LLM instead of a
    canned A/B reply that ignores what they actually asked.
    """
    blob = " ".join(str(text or "").split()).strip()
    if not blob:
        return False
    if "?" in blob:
        return True
    words = re.findall(r"[a-z0-9']+", blob.casefold())
    if len(words) >= 4:
        return True
    if len(words) >= 2 and any(w in _QUESTION_CUES for w in words):
        return True
    return False


def last_boom_line(state: dict[str, Any] | None, fallback: str = "") -> str:
    if isinstance(state, dict):
        for row in reversed(list(state.get(_HISTORY_KEY) or [])):
            if not isinstance(row, dict):
                continue
            if str(row.get("role") or "") != "boom":
                continue
            text = str(row.get("text") or "").strip()
            if text:
                return text
    return str(fallback or "").strip()


def match_choice(
    text: str, choices: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    try:
        import voice_intent

        blob = voice_intent.normalize(text)
    except Exception:
        blob = str(text or "").casefold()
    if not blob:
        return None
    ranked: list[tuple[int, dict[str, Any]]] = []
    for choice in choices or []:
        for hit in choice.get("hits") or ():
            token = str(hit or "").strip().casefold()
            if not token:
                continue
            if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", blob):
                ranked.append((len(token), choice))
    if ranked:
        ranked.sort(key=lambda x: x[0], reverse=True)
        return ranked[0][1]
    for ack in _SOFT_ACK:
        if re.search(rf"(?<!\w){re.escape(ack)}(?!\w)", blob):
            return {
                "id": "_ack",
                "say": "Roger",
                "reply": random.choice(_DEFAULT_REACT_REPLIES),
            }
    return None


def match_react(
    text: str, reacts: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """
    Freeform boom reply for a riff / open bit.

    Prefer a patterned react when the pilot hit a known phrase; otherwise any
    speech (including soft ack) gets a short default react so the chat keeps
    moving without forcing A/B buttons.
    """
    try:
        import voice_intent

        blob = voice_intent.normalize(text)
    except Exception:
        blob = str(text or "").casefold()
    ranked: list[tuple[int, dict[str, Any]]] = []
    for react in reacts or []:
        if not isinstance(react, dict):
            continue
        for hit in react.get("hits") or ():
            token = str(hit or "").strip().casefold()
            if not token:
                continue
            if blob and re.search(rf"(?<!\w){re.escape(token)}(?!\w)", blob):
                ranked.append((len(token), react))
    if ranked:
        ranked.sort(key=lambda x: x[0], reverse=True)
        hit = ranked[0][1]
        return {
            "id": str(hit.get("id") or _slug(str(hit.get("say") or "react"))),
            "say": str(hit.get("say") or "Roger"),
            "reply": str(hit.get("reply") or random.choice(_DEFAULT_REACT_REPLIES)),
            "follow": hit.get("follow") if isinstance(hit.get("follow"), dict) else None,
        }
    for ack in _SOFT_ACK:
        if blob and re.search(rf"(?<!\w){re.escape(ack)}(?!\w)", blob):
            return {
                "id": "_ack",
                "say": "Roger",
                "reply": random.choice(_DEFAULT_REACT_REPLIES),
            }
    return {
        "id": "_any",
        "say": "Roger",
        "reply": random.choice(_DEFAULT_REACT_REPLIES),
    }


def _names(callsign: str, tanker: dict[str, Any] | None) -> tuple[str, str]:
    cs = atc_phrase.speak_callsign(callsign)
    try:
        import tanker as tanker_mod

        tcs = tanker_mod.speak_tanker_callsign(
            str((tanker or {}).get("callsign") or "Texaco")
        )
    except Exception:
        tcs = "Texaco"
    return cs, tcs


def _speak_casual_numbers(text: str) -> str:
    """
    Boom chat is conversational — say 'forty', not ICAO 'four zero'.

    Formal ATC TX still digit-expands bare numerals in prepare_radio_*; this
    path pre-expands so Chirp never list-reads ages/counts on small talk.
    """

    def _one(match: re.Match[str]) -> str:
        raw = match.group(0)
        if len(raw) > 1 and raw.startswith("0"):
            return atc_phrase.speak_digits(raw)
        try:
            n = int(raw)
        except ValueError:
            return raw
        return atc_phrase.speak_natural_number(n)

    return re.sub(r"\d+", _one, str(text or ""))


def _fill(template: str, cs: str = "", tcs: str = "") -> str:
    """
    Expand a boom-chat template.

    Small talk is informal side-channel chatter — strip formal
    "{cs}, {tcs}," addressing even when the library still has it. Directive
    tanker calls (rejoin / contact / etc.) live outside this module.
    """
    text = str(template or "")
    # Drop the formal opener patterns before substituting leftovers.
    text = re.sub(
        r"\{cs\}\s*,\s*\{tcs\}\s*,?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\{tcs\}\s*,\s*\{cs\}\s*,?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    if cs:
        text = text.replace("{cs}", cs)
    else:
        text = text.replace("{cs}", "")
    if tcs:
        text = text.replace("{tcs}", tcs)
    else:
        text = text.replace("{tcs}", "")
    # If spoken callsigns somehow landed in the string, peel a leading
    # "Burner four, Texaco fife," style address.
    if cs and tcs:
        text = re.sub(
            rf"^\s*{re.escape(cs)}\s*,\s*{re.escape(tcs)}\s*,?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
    text = re.sub(r"\s+", " ", text).strip(" ,")
    # LLM sometimes opens with a spoken flight callsign anyway ("Ram two, …").
    text = re.sub(
        r"^\s*[A-Za-z]{2,}(?:\s+(?:zero|one|two|three|four|five|fife|six|"
        r"seven|eight|nine|niner|ten|\d+))+\s*,\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = _speak_casual_numbers(text)
    if text:
        text = text[0].upper() + text[1:]
    return text


_OFFICIAL_TANKER_RE = re.compile(
    r"\b("
    r"altitude and airspeed|airspeed and altitude|pre[- ]?contact|"
    r"cleared (?:contact|boom)|you're cleared|you are cleared|"
    r"boom connected|fuel state"
    r")\b",
    re.IGNORECASE,
)


def looks_like_official_tanker(text: str) -> bool:
    """True when a boom line leaked ATP-56 / DCS tanker radio."""
    return bool(_OFFICIAL_TANKER_RE.search(str(text or "")))


_AIRLINE_RE = re.compile(
    r"\b("
    r"passengers?|first class|business class|cabin crew|flight attendant|"
    r"beverage service|overhead bin|tray table|jetway|connecting flight|"
    r"boarding(?:\s+group)?|gate agent|layover|in-flight|"
    r"this is your captain|folks we(?:'ll| will)|"
    r"welcome aboard|cruising (?:at|altitude)"
    r")\b",
    re.IGNORECASE,
)


def looks_like_airline(text: str) -> bool:
    """True when boom chat slipped into civilian airliner talk."""
    return bool(_AIRLINE_RE.search(str(text or "")))


_REPLY_HOOKS = (
    "you ever",
    "ever notice",
    "what's your",
    "whats your",
    "what do you",
    "how about",
    "how do you",
    "would you",
    "do you",
    "did you",
    "you think",
    "your take",
    "or is it",
    "is it just me",
    "or just me",
    "pick one",
    "which one",
    "you like",
    "you prefer",
    "got a favorite",
    "what's worse",
    "whats worse",
    "what about",
    "how's that",
    "hows that",
    "right sir",
)


def invites_reply(text: str) -> bool:
    """True when the boom line is a question or leaves an obvious hook."""
    blob = " ".join(str(text or "").split()).strip()
    if not blob:
        return False
    if "?" in blob:
        return True
    low = blob.casefold()
    return any(hook in low for hook in _REPLY_HOOKS)


def _remember(state: dict[str, Any], tid: str) -> None:
    recent = [
        str(x)
        for x in (state.get(_RECENT_KEY) or [])
        if str(x).strip() and str(x) != tid
    ]
    if tid:
        recent.append(tid)
    state[_RECENT_KEY] = recent[-_RECENT_MAX:]
    state[_LAST_KEY] = tid


def _recent_ids(state: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for x in state.get(_RECENT_KEY) or []:
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
    last = str(state.get(_LAST_KEY) or "").strip()
    if last and last not in out:
        out.append(last)
    return out


def append_history(
    state: dict[str, Any] | None, role: str, text: str
) -> None:
    """Keep a short boom/pilot transcript so Ollama can continue the thread."""
    if not isinstance(state, dict):
        return
    line = " ".join(str(text or "").split()).strip()
    if not line:
        return
    who = "pilot" if str(role or "").casefold().startswith("p") else "boom"
    hist = [
        row
        for row in (state.get(_HISTORY_KEY) or [])
        if isinstance(row, dict) and row.get("text")
    ]
    hist.append({"role": who, "text": line[:260]})
    state[_HISTORY_KEY] = hist[-_HISTORY_MAX:]


def format_history(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return "(new conversation)"
    lines: list[str] = []
    for row in state.get(_HISTORY_KEY) or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        who = "Pilot" if str(row.get("role") or "") == "pilot" else "Boom"
        lines.append(f"{who}: {text}")
    return "\n".join(lines) if lines else "(new conversation)"


def format_history_for_llm(
    state: dict[str, Any] | None, *, drop_trailing_pilot: bool = False
) -> str:
    """History labels that do not teach the model to write a Pilot: line next."""
    if not isinstance(state, dict):
        return "(new conversation)"
    rows = [
        row
        for row in (state.get(_HISTORY_KEY) or [])
        if isinstance(row, dict) and str(row.get("text") or "").strip()
    ]
    if drop_trailing_pilot and rows and str(rows[-1].get("role") or "") == "pilot":
        rows = rows[:-1]
    if not rows:
        return "(no earlier lines)" if drop_trailing_pilot else "(new conversation)"
    lines: list[str] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if str(row.get("role") or "") == "pilot":
            who = "F-16 (them, already said)"
        else:
            who = "YOU (boom operator)"
        lines.append(f"{who}: {text}")
    return "\n".join(lines)


def _mentions_coffee(text: str) -> bool:
    blob = str(text or "").casefold()
    return any(word in blob for word in _COFFEE_WORDS)


def coffee_on_cooldown(state: dict[str, Any] | None) -> bool:
    """True when recent boom chat already leaned on coffee — ban the next bit."""
    if not isinstance(state, dict):
        return False
    recent_lines = [
        str(row.get("text") or "")
        for row in (state.get(_HISTORY_KEY) or [])[-5:]
        if isinstance(row, dict)
    ]
    hits = sum(1 for line in recent_lines if _mentions_coffee(line))
    if hits >= 1:
        return True
    # Library / LLM ids that are coffee-flavored.
    for tid in _recent_ids(state)[-8:]:
        low = tid.casefold()
        if any(
            key in low
            for key in (
                "coffee",
                "dunkin",
                "starbucks",
                "keurig",
                "espresso",
                "caffeine",
            )
        ):
            return True
    return False


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").casefold())[:24] or "opt"


def _hits_from_say(say: str, extra: list[str] | tuple[str, ...] | None = None) -> list[str]:
    words = [
        w
        for w in (say or "").casefold().replace("-", " ").replace("'", "").split()
        if len(w) > 1 and w not in {"the", "and", "for", "or", "a", "an"}
    ]
    seen: list[str] = []
    for token in list(extra or []) + words + [str(say or "").strip().casefold()]:
        hit = str(token or "").strip().casefold()
        if not hit or hit in _BAN_HITS or hit in seen:
            continue
        seen.append(hit)
    return seen[:8]


def _clean_choice(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    say = str(raw.get("say") or raw.get("id") or "").strip()
    reply = str(raw.get("reply") or "").strip()
    if not say or not reply:
        return None
    say = " ".join(say.split())[:40]
    reply = strip_scripted_dialogue(" ".join(reply.split())[:480])
    if not reply:
        return None
    cid = _slug(str(raw.get("id") or say))
    extra = raw.get("hits") if isinstance(raw.get("hits"), (list, tuple)) else ()
    hits = _hits_from_say(say, extra)
    if not hits:
        return None
    row: dict[str, Any] = {
        "id": cid,
        "say": say,
        "hits": tuple(hits),
        "reply": reply,
    }
    follow = raw.get("follow")
    if isinstance(follow, dict):
        cleaned = normalize_llm_thread(follow, allow_follow=False)
        if cleaned and (cleaned.get("choices") or cleaned.get("kind") in {"riff", "open"}):
            row["follow"] = cleaned
    return row


_SELF_SPEAKER = re.compile(
    r"^(?:boom(?:\s*operator)?|texaco|tanker|operator)\s*:\s*",
    re.IGNORECASE,
)
_OTHER_SPEAKER = re.compile(
    r"(?:^|(?<=\s))(?:Pilot|Fighter|Viper|Receiver|MIC|You|Them|F-?16(?:\s*pilot)?)\s*:\s*",
    re.IGNORECASE,
)


def strip_scripted_dialogue(text: str) -> str:
    """Keep only the boom operator's words — drop fake Pilot:/script labels."""
    blob = " ".join(str(text or "").split()).strip().strip('"').strip("'")
    if not blob:
        return ""
    blob = _SELF_SPEAKER.sub("", blob, count=1).strip()
    match = _OTHER_SPEAKER.search(blob)
    if match:
        blob = blob[: match.start()].strip()
    return blob.strip(" ,;:-")


def _clean_react(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    reply = str(raw.get("reply") or "").strip()
    if not reply:
        return None
    say = str(raw.get("say") or raw.get("id") or "Roger").strip() or "Roger"
    say = " ".join(say.split())[:40]
    reply = strip_scripted_dialogue(" ".join(reply.split())[:480])
    if not reply:
        return None
    extra = raw.get("hits") if isinstance(raw.get("hits"), (list, tuple)) else ()
    hits = _hits_from_say(say, extra)
    row: dict[str, Any] = {
        "id": _slug(str(raw.get("id") or say)),
        "say": say,
        "hits": tuple(hits),
        "reply": reply,
    }
    follow = raw.get("follow")
    if isinstance(follow, dict):
        cleaned = normalize_llm_thread(follow, allow_follow=False)
        if cleaned:
            row["follow"] = cleaned
    return row


def _salvage_riff_text(text: str) -> dict[str, Any] | None:
    """Turn messy model prose into a usable riff opener."""
    blob = str(text or "").strip()
    if not blob:
        return None
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:\w+)?\s*", "", blob, flags=re.I)
        blob = re.sub(r"\s*```$", "", blob)

    # Prefer a real opener field from broken / multi-object JSON dumps.
    parsed = _parse_json_blob(blob)
    if isinstance(parsed, dict):
        opener = str(
            parsed.get("opener")
            or parsed.get("question")
            or parsed.get("line")
            or parsed.get("text")
            or ""
        ).strip()
        opener = " ".join(opener.split())
        if len(opener) >= 12 and not _opener_looks_like_json(opener):
            kind = str(parsed.get("kind") or "riff").strip().casefold() or "riff"
            if kind not in {"ab", "riff", "open"}:
                kind = "riff"
            tid = _slug(str(parsed.get("id") or opener))
            if not tid.startswith("llm"):
                tid = f"llm_{tid}"
            return {
                "id": tid[:40],
                "kind": "riff" if kind == "ab" else kind,
                "opener": _fill(opener)[:420],
                "choices": [],
            }
    extracted = _extract_quoted_opener(blob)
    if extracted:
        blob = extracted
    elif _opener_looks_like_json(blob):
        # Never speak raw JSON on freq.
        return None

    # Drop obvious JSON leftovers / labels.
    blob = re.sub(
        r'^(?:opener|question|line|text)\s*[:=]\s*["\']?',
        "",
        blob,
        flags=re.I,
    )
    blob = " ".join(blob.split()).strip().strip('"').strip("'")
    blob = strip_scripted_dialogue(_fill(blob))
    if len(blob) < 12 or _opener_looks_like_json(blob):
        return None
    tid = _slug(blob)
    if not tid.startswith("llm"):
        tid = f"llm_{tid}"
    return {
        "id": tid[:40],
        "kind": "riff",
        "opener": blob[:420],
        "choices": [],
    }


def _opener_looks_like_json(text: str) -> bool:
    blob = str(text or "").strip()
    if not blob:
        return False
    if blob.startswith("{") or blob.startswith("["):
        return True
    head = blob[:160].casefold()
    return '"opener"' in head or '"kind"' in head or '"choices"' in head


def _extract_quoted_opener(text: str) -> str | None:
    match = re.search(r'"opener"\s*:\s*"((?:\\.|[^"\\])*)"', str(text or ""), flags=re.I)
    if not match:
        return None
    try:
        return str(json.loads(f'"{match.group(1)}"')).strip()
    except json.JSONDecodeError:
        return match.group(1).replace('\\"', '"').strip() or None


def normalize_llm_thread(
    raw: Any, *, allow_follow: bool = True
) -> dict[str, Any] | None:
    """Turn model JSON (or a dict / plain prose) into a library-shaped thread."""
    original = raw
    if isinstance(raw, str):
        parsed = _parse_json_blob(raw)
        raw = parsed if parsed is not None else raw
    if not isinstance(raw, dict):
        if isinstance(original, str):
            return _salvage_riff_text(original)
        if isinstance(raw, str):
            return _salvage_riff_text(raw)
        return None
    opener = str(
        raw.get("opener") or raw.get("question") or raw.get("line") or raw.get("text") or ""
    ).strip()
    opener = " ".join(opener.split())
    if len(opener) < 12 or _opener_looks_like_json(opener):
        # Broken A/B JSON with a usable nested string — last resort salvage.
        return _salvage_riff_text(str(original if isinstance(original, str) else opener))
    # Boom chat is informal — strip any formal address the model added.
    opener = strip_scripted_dialogue(_fill(opener))
    kind = str(raw.get("kind") or "").strip().casefold()
    if kind not in {"ab", "riff", "open"}:
        kind = ""

    choices_raw = raw.get("choices") or raw.get("options") or []
    if not isinstance(choices_raw, list):
        choices_raw = []
    choices: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in choices_raw:
        if not allow_follow and isinstance(item, dict):
            item = dict(item)
            item.pop("follow", None)
        choice = _clean_choice(item)
        if not choice:
            continue
        cid = str(choice["id"])
        if cid in seen_ids:
            cid = cid + str(len(choices) + 1)
            choice["id"] = cid
        seen_ids.add(cid)
        choices.append(choice)
        if len(choices) >= 3:
            break

    reacts_raw = raw.get("reacts") or raw.get("reactions") or []
    if not isinstance(reacts_raw, list):
        reacts_raw = []
    reacts: list[dict[str, Any]] = []
    for item in reacts_raw:
        if not allow_follow and isinstance(item, dict):
            item = dict(item)
            item.pop("follow", None)
        react = _clean_react(item)
        if react:
            reacts.append(react)
        if len(reacts) >= 4:
            break

    if not kind:
        if len(choices) >= 2:
            kind = "ab"
        else:
            kind = "riff"

    # Incomplete A/B from small local models → keep the opener as a riff.
    if kind == "ab" and len(choices) < 2:
        kind = "riff"
        choices = []
    if kind in {"riff", "open"} and choices:
        choices = choices[:2]

    tid = _slug(str(raw.get("id") or opener.split(",")[-1][:40] or "llm"))
    if not tid.startswith("llm"):
        tid = f"llm_{tid}"
    out: dict[str, Any] = {
        "id": tid[:40],
        "kind": kind,
        "opener": opener[:420],
        "choices": choices,
    }
    if reacts:
        out["reacts"] = reacts
    return out


def _parse_json_blob(text: str) -> Any:
    """Parse model JSON, including concatenated multi-object dumps (take the first)."""
    blob = str(text or "").strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob, flags=re.I)
        blob = re.sub(r"\s*```$", "", blob)
    try:
        data = json.loads(blob)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return data
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(blob):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(blob, idx)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            return obj[0]
    return None


def _llm_mode(config: dict[str, Any] | None) -> str:
    if not isinstance(config, dict):
        return "off"
    return str(config.get("tanker_chat_llm") or "off").strip().lower()


def resolve_llm_provider(config: dict[str, Any] | None) -> tuple[str, str] | None:
    """Return (provider, api_key) or None if LLM should not run."""
    mode = _llm_mode(config)
    if mode in {"", "off", "0", "false", "no", "library"}:
        return None
    key = str((config or {}).get("tanker_chat_llm_key") or "").strip()
    if mode == "auto":
        if not key:
            return None
        if key.startswith("sk-"):
            return "openai", key
        if key.startswith("AIza"):
            return "gemini", key
        return None
    if mode in {"ollama", "local"}:
        return "ollama", key
    if not key:
        return None
    if mode in {"gemini", "google"}:
        return "gemini", key
    if mode in {"openai", "gpt"}:
        return "openai", key
    return None


def llm_json(
    config: dict[str, Any] | None,
    prompt: str,
    *,
    timeout: float = 4.0,
    temperature: float = 0.1,
    max_tokens: int = 160,
) -> Any:
    """
    One JSON object from the configured boom-chat LLM, or None.

    Used for intent classification (and similar). Does not generate boom riffs.
    """
    resolved = resolve_llm_provider(config)
    if not resolved or not isinstance(config, dict):
        return None
    prompt = str(prompt or "").strip()
    if not prompt:
        return None
    provider, key = resolved
    try:
        if provider == "ollama":
            if not resolve_ollama_model(config):
                return None
            text = _ollama_chat(
                config,
                [
                    {
                        "role": "system",
                        "content": _boom_llm_system(json_out=True),
                    },
                    {"role": "user", "content": prompt},
                ],
                timeout=timeout,
                temperature=temperature,
                num_predict=min(int(max_tokens or 160), 180),
            )
        elif provider == "openai":
            model = str(
                config.get("tanker_chat_llm_model") or _DEFAULT_OPENAI_MODEL
            ).strip() or _DEFAULT_OPENAI_MODEL
            data = _http_json(
                "https://api.openai.com/v1/chat/completions",
                {
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": _boom_llm_system(json_out=True),
                        },
                        {"role": "user", "content": prompt},
                    ],
                },
                {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
                timeout=timeout,
            )
            text = str(
                ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
                or ""
            )
        elif provider == "gemini":
            model = str(
                config.get("tanker_chat_llm_model") or _DEFAULT_GEMINI_MODEL
            ).strip() or _DEFAULT_GEMINI_MODEL
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{urllib.parse.quote(model)}:generateContent?key="
                f"{urllib.parse.quote(key)}"
            )
            data = _http_json(
                url,
                {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": temperature,
                        "maxOutputTokens": max_tokens,
                        "responseMimeType": "application/json",
                    },
                },
                {"Content-Type": "application/json"},
                timeout=timeout,
            )
            parts = (
                (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts"))
                or []
            )
            text = ""
            for part in parts:
                if isinstance(part, dict) and part.get("text"):
                    text += str(part.get("text") or "")
        else:
            return None
    except Exception:
        return None
    parsed = _parse_json_blob(text)
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    return parsed if isinstance(parsed, dict) else None


_BOOM_RANK_TONE = (
    "You are an enlisted USAF KC-135 boom operator talking to an F-16 officer "
    "while you gas the fighter on a military tanker track. "
    "This is enlisted-to-officer, not two buddies. Dry and funny is fine; "
    "stay respectful. No dude, bro, buddy, or first names. Do not lecture, "
    "talk down, or give the pilot orders. Self-deprecating tanker jokes are good. "
    "An occasional 'sir' is natural — not every sentence, and never sarcastic. "
)


_BOOM_IDENTITY = (
    "You ARE the KC-135 boom operator. First person, on the radio, talking to "
    "a real F-16 officer on your boom. You are not a narrator and you are not "
    "writing a screenplay. Never invent the fighter's next line. Never output "
    "labels like Pilot:, Boom:, Texaco:, You:, MIC:, or F-16:. Speak only YOUR "
    "radio words, then STOP and wait — the human answers. This is dialogue: "
    "answer them, then ask something back they can answer. "
)


_BOOM_SETTING = (
    "Setting: USAF military aerial refueling — KC-135 boom, F-16 Viper, range / "
    "orbit / AAR. This is NOT an airliner, NOT civil aviation, NOT a passenger "
    "flight. Never talk like airline crew (passengers, cabin, gate, first class, "
    "layover hotels, beverage service, 'folks', boarding, cruise PA). "
    "Small talk does not have to be aviation every turn — weekends, chow, sports, "
    "cars, movies, pets, TDY, dorms, squadron life are good — but it is always "
    "military crew talking, never airline. "
)


_BOOM_STYLE = (
    "While the boom is in, talk like real tanker/fighter gas-up chatter: "
    "morale-boosting, a little goofy, two-way. Short riddles, food they miss "
    "from home, who has the worse seat (boom pad vs Viper), TDY boredom. "
    "You lead, they answer, you riff back. Not a briefing. Not scenery narration. "
    "Riddles must be short and answerable on the radio. "
)


def _boom_llm_system(*, json_out: bool) -> str:
    if json_out:
        return (
            f"{_BOOM_IDENTITY}"
            "Return exactly one JSON object only. opener is only YOUR next "
            "radio sentence — never a fake pilot answer. "
            "Never concatenate multiple objects. No markdown."
        )
    return (
        f"{_BOOM_IDENTITY}"
        "Answer the F-16 directly and stay on their topic. "
        "Plain radio text only. No JSON. No markdown. "
        'Good: "Ha — that\'s the view I live with. Boom pad or Viper seat, who got robbed?" '
        'Bad: "The pad\'s wider. Pilot: really?"'
    )


_BOOM_DIALOGUE = (
    "This is two-way small talk, not a monologue. "
    "The opener MUST invite a reply: ask a question, offer A/B, or leave a hook "
    "('you ever…', 'what's your take', 'or is it just me'). "
    "Do not just announce scenery, a fact, or an observation and stop. "
    "Prefer kind=open (a real question) or kind=ab (a short poll). "
    "Use kind=riff only if the line still ends with a question or hook. "
)


def _llm_prompt(
    recent: list[str],
    *,
    history: str = "",
    ban_coffee: bool = False,
) -> str:
    avoid = ", ".join(recent[-8:]) if recent else "(none yet)"
    coffee_rule = (
        "Do NOT mention coffee, Keurig, Dunkin, Starbucks, caffeine, or brewing — "
        "that topic is exhausted for now. "
        if ban_coffee
        else "Coffee is allowed only rarely — prefer other topics. "
    )
    hist = (history or "").strip() or "(new conversation)"
    return (
        "You write one short boom-operator radio bit for a KC-135 refueling an F-16. "
        f"{_BOOM_IDENTITY}"
        f"{_BOOM_RANK_TONE}"
        f"{_BOOM_SETTING}"
        f"{_BOOM_STYLE}"
        "Informal small talk on a side frequency — "
        "do NOT use callsigns, do NOT say the fighter name or Texaco/tanker name, "
        "do NOT open with 'X, Y,'. Never mention DCS, AI, games, or that you are a model. "
        "Never use official tanker phraseology (rejoin, contact, disconnect, abort, "
        "observation, identified, altitude and airspeed, fuel state, pre-contact). "
        f"{_BOOM_DIALOGUE}"
        "Mix it up — not every bit is an A/B poll. Pick kind: "
        "'open' (a question the pilot can answer in their own words), "
        "'ab' (short A/B or A/B/C poll), or "
        "'riff' (banter that still ends with a question or hook). "
        "Replies may be one or two short sentences. "
        "Continue the same conversation when history exists — reference what was just "
        "said instead of starting a brand-new random topic. "
        f"{coffee_rule}"
        "Good topic pool (rotate): short riddles, food they miss from home "
        "(Chick-fil-A / Whataburger / In-N-Out — not only coffee), boom pad vs "
        "Viper seat, Viper / Eagle / Mudhen / Navy jokes, boom-pod life, range days, "
        "TDY, chow, dorms, squadron, pets, sports, cars, weekends, movies. "
        "Not airline, not passenger flying. "
        "Avoid these recent ids: "
        f"{avoid}.\n\nRecent chat:\n{hist}\n\n"
        "Return JSON only with keys: "
        "id, kind, opener, and when useful choices and/or reacts. opener is the spoken "
        "line with no callsigns. For kind=ab, choices is 2-3 objects with say (1-3 words), "
        "hits, reply. For kind=riff/open, choices may be empty; optional reacts are "
        "patterned freeform replies with hits + reply. Keep it radio-short but human. "
        "Important: do NOT start every reply with the pilot's answer plus 'copy' "
        "(avoid 'Dunkin, copy.' / 'Navy, copy.'). Just react — joke first, ack optional. "
        "opener is ONLY Texaco's next line — never invent a pilot answer in the opener. "
        "Prefer kind=open; ab is fine; riff only with a hook. "
        "Return exactly one JSON object — never two objects back-to-back."
    )


def _llm_riff_prompt(
    recent: list[str],
    *,
    history: str = "",
    ban_coffee: bool = False,
) -> str:
    avoid = ", ".join(recent[-8:]) if recent else "(none yet)"
    coffee_rule = (
        "No coffee/caffeine jokes this turn. "
        if ban_coffee
        else "Skip coffee unless it clearly continues the last line. "
    )
    hist = (history or "").strip() or "(new conversation)"
    return (
        "Write one short KC-135 boom-operator QUESTION an F-16 officer can answer. "
        f"{_BOOM_IDENTITY}"
        f"{_BOOM_RANK_TONE}"
        f"{_BOOM_SETTING}"
        f"{_BOOM_STYLE}"
        f"{_BOOM_DIALOGUE}"
        "No callsigns. No official tanker words "
        "(rejoin, contact, disconnect, abort, observation, altitude, airspeed). "
        "Continue the recent chat when there is history. "
        f"{coffee_rule}"
        "Topics: riddles, food from home, worse seat, Viper/Eagle/Mudhen/Navy "
        "jokes, chow, TDY, snacks, boom boredom, pets, sports — military crew, "
        "not airline. "
        f"Avoid sounding like these recent ids: {avoid}.\n\n"
        f"Recent chat:\n{hist}\n\n"
        "Return JSON only: {\"id\":\"...\",\"kind\":\"open\",\"opener\":\"...\"}. "
        "opener must end with a question mark."
    )


def _llm_react_prompt(
    opener: str,
    pilot: str,
    *,
    history: str = "",
    ban_coffee: bool = False,
) -> str:
    coffee_rule = (
        "Do not bring up coffee or caffeine. "
        if ban_coffee
        else ""
    )
    hist = (history or "").strip() or "(no earlier lines)"
    return (
        "You ARE the KC-135 boom operator answering on the radio. "
        f"{_BOOM_IDENTITY}"
        f"{_BOOM_RANK_TONE}"
        f"{_BOOM_SETTING}"
        f"{_BOOM_STYLE}"
        "No callsigns, no Texaco/fighter names, "
        "no official tanker phraseology (rejoin, contact, disconnect, abort, "
        "observation, identified). Do not parrot the pilot with 'X, copy.' "
        "Do not write a Pilot: / Fighter: line. The F-16 human will answer next. "
        f"{coffee_rule}"
        "MOST IMPORTANT: respond to what they just said. "
        "If they asked a question, answer that question first in plain words. "
        "If they made a point or joke, acknowledge that point — do not change "
        "the subject to desert sunsets, random orbit banter, or a new poll. "
        "If they answered a riddle, tell them if they got it, then keep talking. "
        "Stay on their topic; history is only for continuity.\n\n"
        f"Earlier chat:\n{hist}\n\n"
        "Your previous line (context only):\n"
        f"{opener.strip() or '(small talk)'}\n\n"
        "The F-16 just said (answer THIS):\n"
        f"{pilot.strip() or '(roger)'}\n\n"
        "Reply with one or two short radio sentences: answer them first, then "
        "toss the ball back with one short follow-up question on the same topic. "
        "End with a question mark. Do not close with a statement they cannot answer. "
        "Plain text only — no JSON, no markdown, no Pilot: line."
    )


def _llm_answer_prompt(
    pilot: str,
    *,
    history: str = "",
    ban_coffee: bool = False,
) -> str:
    """Stricter second-chance prompt when the first react wandered off topic."""
    coffee_rule = (
        "Do not mention coffee or caffeine. "
        if ban_coffee
        else ""
    )
    hist = (history or "").strip() or "(none)"
    return (
        "You ARE the KC-135 boom operator. Short informal radio reply. "
        f"{_BOOM_IDENTITY}"
        f"{_BOOM_RANK_TONE}"
        f"{_BOOM_SETTING}"
        f"{_BOOM_STYLE}"
        "Answer what they just said directly. "
        "Do not start a new topic. No callsigns. No official tanker words. "
        "Never write the F-16's next line. "
        f"{coffee_rule}"
        f"Earlier chat:\n{hist}\n\n"
        f"They said: {pilot.strip() or '(roger)'}\n\n"
        "Answer first, then one short follow-up question on the same topic. "
        "End with a question mark. Plain text only — no Pilot: label."
    )


def _llm_dialogue_nudge_prompt(
    pilot: str,
    *,
    history: str = "",
    ban_coffee: bool = False,
) -> str:
    coffee_rule = (
        "Do not mention coffee or caffeine. "
        if ban_coffee
        else ""
    )
    hist = (history or "").strip() or "(none)"
    return (
        f"{_BOOM_IDENTITY}"
        f"{coffee_rule}"
        "One or two short radio sentences as the boom operator. Answer what they "
        "said, then end with a real question mark so they can talk back. No labels. "
        "No invented Pilot line.\n\n"
        f"Earlier:\n{hist}\n\n"
        f"They just said: {pilot.strip() or '(roger)'}\n"
    )



def _http_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float = _LLM_TIMEOUT_S,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        raise
    data = json.loads(raw) if raw else {}
    if not isinstance(data, dict):
        raise ValueError("llm response was not an object")
    return data


def _gemini_thread(
    config: dict[str, Any],
    key: str,
    recent: list[str],
    *,
    history: str = "",
    ban_coffee: bool = False,
) -> dict[str, Any] | None:
    model = str(config.get("tanker_chat_llm_model") or _DEFAULT_GEMINI_MODEL).strip()
    model = model or _DEFAULT_GEMINI_MODEL
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(key)}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": _llm_prompt(
                            recent, history=history, ban_coffee=ban_coffee
                        )
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 1.05,
            "maxOutputTokens": 700,
            "responseMimeType": "application/json",
        },
    }
    data = _http_json(
        url,
        payload,
        {"Content-Type": "application/json"},
    )
    parts = (
        (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
    )
    text = ""
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            text += str(part.get("text") or "")
    node = normalize_llm_thread(text or data)
    if node is None and text.strip():
        node = _salvage_riff_text(text)
    return node


def _openai_compatible_thread(
    config: dict[str, Any],
    *,
    url: str,
    key: str,
    model: str,
    recent: list[str],
    json_mode: bool = True,
    timeout: float = _LLM_TIMEOUT_S,
    temperature: float = 1.15,
    prompt: str | None = None,
    history: str = "",
    ban_coffee: bool = False,
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": 700,
        "messages": [
            {
                "role": "system",
                "content": _boom_llm_system(json_out=True),
            },
            {
                "role": "user",
                "content": prompt
                or _llm_prompt(recent, history=history, ban_coffee=ban_coffee),
            },
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = _http_json(url, payload, headers, timeout=timeout)
    text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    node = normalize_llm_thread(text)
    if node is None and text.strip():
        node = _salvage_riff_text(text)
    return node


def _openai_thread(
    config: dict[str, Any],
    key: str,
    recent: list[str],
    *,
    history: str = "",
    ban_coffee: bool = False,
) -> dict[str, Any] | None:
    model = str(config.get("tanker_chat_llm_model") or _DEFAULT_OPENAI_MODEL).strip()
    model = model or _DEFAULT_OPENAI_MODEL
    return _openai_compatible_thread(
        config,
        url="https://api.openai.com/v1/chat/completions",
        key=key,
        model=model,
        recent=recent,
        json_mode=True,
        history=history,
        ban_coffee=ban_coffee,
    )


def _ollama_base_url(config: dict[str, Any]) -> str:
    url = str(config.get("tanker_chat_llm_url") or _DEFAULT_OLLAMA_URL).strip()
    url = url or _DEFAULT_OLLAMA_URL
    # Accept either .../v1/chat/completions or the Ollama root.
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url.rstrip("/")


def list_ollama_models(config: dict[str, Any] | None = None) -> list[str]:
    """Installed Ollama model names, or [] if the daemon is down / empty."""
    cfg = config if isinstance(config, dict) else {}
    now = time.time()
    cached = _OLLAMA_MODELS_CACHE.get("names")
    cached_t = float(_OLLAMA_MODELS_CACHE.get("t") or 0)
    # Fresh hits cache longer; misses retry sooner so a restarting daemon recovers.
    ttl = 30.0 if cached else 5.0
    if isinstance(cached, list) and cached and (now - cached_t) < ttl:
        return list(cached)
    if isinstance(cached, list) and not cached and (now - cached_t) < 5.0:
        return []
    base = _ollama_base_url(cfg)
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=0.8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw else {}
    except Exception:
        _OLLAMA_MODELS_CACHE["t"] = now
        _OLLAMA_MODELS_CACHE["names"] = []
        return []
    out: list[str] = []
    for row in (data.get("models") or []) if isinstance(data, dict) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("model") or "").strip()
        if name and name not in out:
            out.append(name)
    _OLLAMA_MODELS_CACHE["t"] = now
    _OLLAMA_MODELS_CACHE["names"] = list(out)
    return out


def _ollama_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    timeout: float,
    temperature: float,
    num_predict: int,
) -> str:
    """
    Native Ollama /api/chat — short num_predict so DCS load does not time out.

    The OpenAI-compat endpoint with max_tokens=700 was hanging until urllib
    gave up (Fly: 'ollama: timed out') and boom chat fell back to the library.
    """
    model = resolve_ollama_model(config)
    if not model:
        raise RuntimeError("No Ollama models installed — run: ollama pull llama3.2")
    url = _ollama_base_url(config) + "/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "keep_alive": _OLLAMA_KEEP_ALIVE,
        "options": {
            "temperature": float(temperature),
            "num_predict": max(40, int(num_predict)),
            "stop": list(_OLLAMA_STOP),
        },
        "messages": messages,
    }
    with _OLLAMA_LOCK:
        data = _http_json(
            url,
            payload,
            {"Content-Type": "application/json"},
            timeout=timeout,
        )
    return str((data.get("message") or {}).get("content") or "")


def resolve_ollama_model(config: dict[str, Any]) -> str | None:
    """
    Pick an installed Ollama model.

    Prefers tanker_chat_llm_model / llama3.2 when present; otherwise the first
    installed model. Returns None when the daemon has nothing pulled.
    """
    want = str(config.get("tanker_chat_llm_model") or _DEFAULT_OLLAMA_MODEL).strip()
    want = want or _DEFAULT_OLLAMA_MODEL
    models = list_ollama_models(config)
    if not models:
        return None
    if want in models:
        return want
    # Tags often look like llama3.2:latest
    for name in models:
        if name == want or name.startswith(want + ":") or name.startswith(want + "-"):
            return name
    return models[0]


def _ollama_thread(
    config: dict[str, Any],
    recent: list[str],
    *,
    history: str = "",
    ban_coffee: bool = False,
) -> dict[str, Any] | None:
    system = _boom_llm_system(json_out=True)
    text = _ollama_chat(
        config,
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": _llm_prompt(
                    recent, history=history, ban_coffee=ban_coffee
                ),
            },
        ],
        timeout=_OLLAMA_TIMEOUT_S,
        temperature=0.7,
        num_predict=_OLLAMA_NUM_PREDICT,
    )
    node = normalize_llm_thread(text)
    if node is None and text.strip():
        node = _salvage_riff_text(text)
    if node is not None and not (
        ban_coffee and _mentions_coffee(str(node.get("opener") or ""))
    ) and invites_reply(str(node.get("opener") or "")):
        return node
    # Parse miss / coffee / monologue — one short question retry.
    text = _ollama_chat(
        config,
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": _llm_riff_prompt(
                    recent, history=history, ban_coffee=ban_coffee
                ),
            },
        ],
        timeout=_OLLAMA_TIMEOUT_S,
        temperature=0.55,
        num_predict=140,
    )
    node = normalize_llm_thread(text)
    if node is None and text.strip():
        node = _salvage_riff_text(text)
    if node is not None and not invites_reply(str(node.get("opener") or "")):
        return None
    return node


def try_llm_thread(
    config: dict[str, Any] | None,
    recent: list[str],
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    resolved = resolve_llm_provider(config)
    if not resolved or not isinstance(config, dict):
        return None
    provider, key = resolved
    history = format_history_for_llm(state)
    ban_coffee = coffee_on_cooldown(state)
    if provider == "ollama":
        if not resolve_ollama_model(config):
            _set_llm_error("Ollama unavailable — using library", state)
            return None
        _set_llm_error("Ollama · generating…", state)
    try:
        if provider == "gemini":
            node = _gemini_thread(
                config, key, recent, history=history, ban_coffee=ban_coffee
            )
        elif provider == "openai":
            node = _openai_thread(
                config, key, recent, history=history, ban_coffee=ban_coffee
            )
        elif provider == "ollama":
            node = _ollama_thread(
                config, recent, history=history, ban_coffee=ban_coffee
            )
        else:
            node = None
    except Exception as exc:
        _set_llm_error(f"{provider}: {exc}", state)
        return None
    if node is None:
        _set_llm_error(f"{provider}: bad/empty response — using library", state)
        return None
    if ban_coffee and _mentions_coffee(str(node.get("opener") or "")):
        _set_llm_error(f"{provider}: coffee cooldown — using library", state)
        return None
    opener = _fill(str(node.get("opener") or ""))
    if looks_like_official_tanker(opener):
        _set_llm_error(f"{provider}: official radio — using library", state)
        return None
    if looks_like_airline(opener):
        _set_llm_error(f"{provider}: airline talk — using library", state)
        return None
    if not invites_reply(opener):
        _set_llm_error(f"{provider}: statement — using library", state)
        return None
    node = dict(node)
    if opener:
        node["opener"] = opener
    _set_llm_error("", state)
    node["source"] = provider
    if provider == "ollama":
        node["model"] = resolve_ollama_model(config) or ""
    return node


def llm_live_enabled(config: dict[str, Any] | None) -> bool:
    """True when Ollama / Gemini / OpenAI can mint live freeform reacts."""
    if resolve_llm_provider(config) is None:
        return False
    if not isinstance(config, dict):
        return False
    if _llm_mode(config) in {"ollama", "local"}:
        # Don't advertise freeform if the daemon is down or nothing is pulled.
        return resolve_ollama_model(config) is not None
    return True


def _clean_llm_plain(text: str) -> str | None:
    blob = str(text or "").strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:\w+)?\s*", "", blob, flags=re.I)
        blob = re.sub(r"\s*```$", "", blob)
    # Prefer a JSON {"reply": "..."} if the model wrapped it.
    parsed = _parse_json_blob(blob)
    if isinstance(parsed, dict):
        for key in ("reply", "text", "line", "response"):
            if parsed.get(key):
                blob = str(parsed.get(key) or "")
                break
    blob = " ".join(blob.split()).strip().strip('"').strip("'")
    blob = strip_scripted_dialogue(_fill(blob))
    if looks_like_official_tanker(blob) or looks_like_airline(blob):
        return None
    if len(blob) < 6:
        return None
    return blob[:480]


def _gemini_react(
    config: dict[str, Any],
    key: str,
    opener: str,
    pilot: str,
    *,
    history: str = "",
    ban_coffee: bool = False,
    prompt: str | None = None,
) -> str | None:
    model = str(config.get("tanker_chat_llm_model") or _DEFAULT_GEMINI_MODEL).strip()
    model = model or _DEFAULT_GEMINI_MODEL
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(key)}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                        or _llm_react_prompt(
                            opener,
                            pilot,
                            history=history,
                            ban_coffee=ban_coffee,
                        )
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.65,
            "maxOutputTokens": 220,
        },
    }
    data = _http_json(url, payload, {"Content-Type": "application/json"}, timeout=5.5)
    parts = (
        (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
    )
    text = ""
    for part in parts:
        if isinstance(part, dict) and part.get("text"):
            text += str(part.get("text") or "")
    return _clean_llm_plain(text)


def _openai_compatible_react(
    config: dict[str, Any],
    *,
    url: str,
    key: str,
    model: str,
    opener: str,
    pilot: str,
    timeout: float = 5.5,
    history: str = "",
    ban_coffee: bool = False,
    temperature: float = 0.65,
    prompt: str | None = None,
) -> str | None:
    payload: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": 220,
        "messages": [
            {
                "role": "system",
                "content": _boom_llm_system(json_out=False),
            },
            {
                "role": "user",
                "content": prompt
                or _llm_react_prompt(
                    opener, pilot, history=history, ban_coffee=ban_coffee
                ),
            },
        ],
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = _http_json(url, payload, headers, timeout=timeout)
    text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return _clean_llm_plain(text)


def _react_prompt_for_pilot(
    opener: str,
    pilot: str,
    *,
    history: str = "",
    ban_coffee: bool = False,
) -> str:
    if looks_like_question_or_chat(pilot):
        return _llm_answer_prompt(pilot, history=history, ban_coffee=ban_coffee)
    return _llm_react_prompt(
        opener, pilot, history=history, ban_coffee=ban_coffee
    )


def _history_without_trailing_pilot(state: dict[str, Any] | None) -> str:
    """Prior chat only — current pilot line is passed separately to the model."""
    return format_history_for_llm(state, drop_trailing_pilot=True)


def try_llm_react(
    config: dict[str, Any] | None,
    *,
    opener: str,
    pilot: str,
    state: dict[str, Any] | None = None,
) -> str | None:
    """Live freeform boom reply to whatever the pilot just said."""
    resolved = resolve_llm_provider(config)
    if not resolved or not isinstance(config, dict):
        return None
    provider, key = resolved
    boom_line = last_boom_line(state, opener)
    history = _history_without_trailing_pilot(state)
    ban_coffee = (
        coffee_on_cooldown(state)
        or _mentions_coffee(opener)
        or _mentions_coffee(boom_line)
    )
    prompt = _react_prompt_for_pilot(
        boom_line, pilot, history=history, ban_coffee=ban_coffee
    )
    if provider == "ollama":
        if not resolve_ollama_model(config):
            _set_llm_error("Ollama unavailable — using library", state)
            return None
        _set_llm_error("Ollama · generating…", state)

    def _call(active_prompt: str, *, ban: bool) -> str | None:
        if provider == "gemini":
            return _gemini_react(
                config,
                key,
                boom_line,
                pilot,
                history=history,
                ban_coffee=ban,
                prompt=active_prompt,
            )
        if provider == "openai":
            model = str(
                config.get("tanker_chat_llm_model") or _DEFAULT_OPENAI_MODEL
            ).strip()
            return _openai_compatible_react(
                config,
                url="https://api.openai.com/v1/chat/completions",
                key=key,
                model=model or _DEFAULT_OPENAI_MODEL,
                opener=boom_line,
                pilot=pilot,
                history=history,
                ban_coffee=ban,
                prompt=active_prompt,
            )
        if provider == "ollama":
            raw = _ollama_chat(
                config,
                [
                    {
                        "role": "system",
                        "content": _boom_llm_system(json_out=False),
                    },
                    {"role": "user", "content": active_prompt},
                ],
                timeout=_OLLAMA_REACT_TIMEOUT_S,
                temperature=0.55,
                num_predict=_OLLAMA_REACT_NUM_PREDICT,
            )
            return _clean_llm_plain(raw)
        return None

    try:
        text = _call(prompt, ban=ban_coffee)
        if text and not invites_reply(text):
            nudged = _call(
                _llm_dialogue_nudge_prompt(
                    pilot, history=history, ban_coffee=True
                ),
                ban=True,
            )
            if nudged:
                text = nudged
        elif not text and looks_like_question_or_chat(pilot):
            text = _call(
                _llm_answer_prompt(pilot, history=history, ban_coffee=True),
                ban=True,
            )
    except Exception as exc:
        _set_llm_error(f"{provider} react: {exc}", state)
        return None
    if not text:
        _set_llm_error(f"{provider} react: empty reply", state)
        return None
    cleaned = _clean_llm_plain(text) or strip_scripted_dialogue(text)
    if cleaned:
        text = cleaned
    if not text or looks_like_official_tanker(text) or looks_like_airline(text):
        _set_llm_error(f"{provider} react: empty reply", state)
        return None
    if ban_coffee and _mentions_coffee(text):
        try:
            retry = _call(
                _llm_answer_prompt(pilot, history=history, ban_coffee=True),
                ban=True,
            )
        except Exception:
            retry = None
        if retry and not _mentions_coffee(retry):
            text = retry
        elif _mentions_coffee(text):
            # Keep something on-topic-ish rather than a coffee canned line.
            text = "Ha — fair point. Hang tight."
    _set_llm_error("", state)
    return text


def _daypart(hour: int | None = None) -> str:
    """USAF greeting window from local clock (or an explicit hour)."""
    h = time.localtime().tm_hour if hour is None else int(hour)
    h = h % 24
    if h < 12:
        return "morning"
    if h < 17:
        return "afternoon"
    return "evening"


_GREETING_LINES: dict[str, tuple[str, ...]] = {
    "morning": (
        "Good morning, sir.",
        "Morning, sir.",
        "Good morning, sir. Looking good from here.",
    ),
    "afternoon": (
        "Good afternoon, sir.",
        "Afternoon, sir.",
        "Good afternoon, sir. We'll keep her steady.",
    ),
    "evening": (
        "Good evening, sir.",
        "Evening, sir.",
        "Good evening, sir. Looking good from here.",
    ),
}


def greeting_opener(*, hour: int | None = None) -> str:
    """First-contact boom hello — time of day, then sir. Not a question."""
    part = _daypart(hour)
    pool = _GREETING_LINES.get(part) or _GREETING_LINES["morning"]
    return random.choice(pool)


def _should_greet(state: dict[str, Any] | None, thread_id: str | None) -> bool:
    """True only for the first boom chat of the sortie (not pinned bits)."""
    if str(thread_id or "").strip():
        return False
    if not isinstance(state, dict):
        return True
    if state.get(_GREETED_KEY):
        return False
    for row in state.get(_HISTORY_KEY) or []:
        if isinstance(row, dict) and str(row.get("text") or "").strip():
            return False
    return True


def _greeting_bit(*, hour: int | None = None) -> dict[str, Any]:
    """Riff hello so a quiet jet still gets the first real question after a pause."""
    return chat_lib.Riff(
        _GREET_ID,
        greeting_opener(hour=hour),
        chat_lib.R(
            "Morning",
            "Morning. We'll keep her steady, sir.",
            "morning",
            "good morning",
        ),
        chat_lib.R(
            "Afternoon",
            "Afternoon. Hang in there, sir.",
            "afternoon",
            "good afternoon",
        ),
        chat_lib.R(
            "Evening",
            "Evening. Stay boring, sir.",
            "evening",
            "good evening",
        ),
        chat_lib.R(
            "Hey",
            "Hey. Looking stable from here, sir.",
            "hey",
            "hello",
            "howdy",
        ),
    )


def _pick_library(avoid: list[str], thread_id: str | None) -> dict[str, Any]:
    if thread_id:
        found = chat_lib.thread_by_id(thread_id)
        if found:
            return copy.deepcopy(found)
    skip = set(avoid)
    pool = [t for t in chat_lib.THREADS if str(t.get("id") or "") not in skip]
    if not pool:
        pool = list(chat_lib.THREADS)
    return copy.deepcopy(random.choice(pool))


def _pick_library_for_state(
    state: dict[str, Any], avoid: list[str], thread_id: str | None
) -> dict[str, Any]:
    """Library fallback that skips coffee bits while coffee is on cooldown."""
    node = _pick_library(avoid, thread_id)
    if thread_id or not coffee_on_cooldown(state):
        return node
    skip = set(avoid)
    pool = []
    for t in chat_lib.THREADS:
        tid = str(t.get("id") or "")
        if tid in skip:
            continue
        opener = str(t.get("opener") or "")
        if _mentions_coffee(opener) or _mentions_coffee(tid):
            continue
        pool.append(t)
    if not pool:
        return node
    return copy.deepcopy(random.choice(pool))


def _apply_open_bit(
    state: dict[str, Any],
    node: dict[str, Any],
    *,
    schedule_auto: bool,
    opener: str = "",
) -> None:
    """Store an open riff/open/ab bit. Riffs auto-continue if the pilot stays quiet."""
    kind = str(node.get("kind") or "").strip().casefold()
    choices = list(node.get("choices") or [])
    if not kind:
        kind = "ab" if len(choices) >= 2 else "riff"
    tid = str(node.get("id") or "")
    reacts = list(node.get("reacts") or [])
    spoken = str(opener or node.get("opener") or "").strip()
    source = str(node.get("source") or "").strip()
    if kind in {"riff", "open"}:
        # Library riffs may auto-continue if the pilot stays quiet. Live LLM
        # bits wait for the pilot — auto-continue made Texaco answer herself.
        wait = None
        if schedule_auto and kind == "riff" and not source:
            wait = _pick_break_s(llm=False)
        state[_STATE_KEY] = {
            "id": tid,
            "kind": kind,
            "opener": spoken,
            "source": source,
            "choices": choices[:2] if choices else [],
            "reacts": reacts,
            "awaiting": "react",
            "turns": 0,
            "session": True,
            "next_at": (time.time() + wait) if wait is not None else None,
            "break_s": wait,
        }
    else:
        state[_STATE_KEY] = {
            "id": tid,
            "kind": "ab",
            "opener": spoken,
            "source": source,
            "choices": choices,
            "reacts": [],
            "awaiting": None,
            "turns": 0,
            "session": True,
            "next_at": None,
            "break_s": None,
        }


def start_chat(
    state: dict[str, Any] | None,
    callsign: str,
    tanker: dict[str, Any] | None,
    *,
    thread_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Texaco opens a boom bit (A/B, riff, or open). Keeps the session going."""
    if not isinstance(state, dict):
        state = {}
    recent = _recent_ids(state)
    node: dict[str, Any] | None = None
    if _should_greet(state, thread_id):
        node = _greeting_bit()
        state[_GREETED_KEY] = True
    if node is None and not thread_id:
        node = try_llm_thread(config, recent, state=state)
    if node is None:
        node = _pick_library_for_state(state, recent, thread_id)
        if not thread_id and resolve_llm_provider(config) is not None and not last_llm_error():
            _set_llm_error("LLM off-line — library bit", state)
    else:
        _set_llm_error("", state)
        if node.get("source") == "ollama" and node.get("model"):
            state[_LLM_NOTE_KEY] = f"Ollama · {node.get('model')}"
        elif node.get("source"):
            state[_LLM_NOTE_KEY] = str(node.get("source"))
    cs, tcs = _names(callsign, tanker)
    opener = _fill(str(node.get("opener") or ""), cs, tcs)
    if (
        looks_like_official_tanker(opener) or looks_like_airline(opener)
    ) and not thread_id:
        node = _pick_library_for_state(state, recent, None)
        opener = _fill(str((node or {}).get("opener") or ""), cs, tcs)
        _set_llm_error("off-register — library bit", state)
    tid = str((node or {}).get("id") or "")
    _apply_open_bit(state, node, schedule_auto=True, opener=opener)
    _remember(state, tid)
    append_history(state, "boom", opener)
    return opener


def _keep_react_open(
    state: dict[str, Any],
    *,
    opener: str,
    turns: int,
    llm: bool,
) -> None:
    """Stay on the same bit so the pilot can keep riffing (no auto-advance)."""
    del llm  # reserved if we later soft-timeout live chats
    prev = _row(state) or {}
    state[_STATE_KEY] = {
        "id": str(prev.get("id") or ""),
        "kind": str(prev.get("kind") or "open") or "open",
        "opener": opener,
        "source": str(prev.get("source") or ""),
        "choices": [],
        "reacts": list(prev.get("reacts") or []),
        "awaiting": "react",
        "turns": turns,
        "session": True,
        # Wait for the pilot — do not fire a new opener mid-conversation.
        "next_at": None,
        "break_s": None,
    }


def answer_chat(
    state: dict[str, Any] | None,
    callsign: str,
    tanker: dict[str, Any] | None,
    *,
    choice_id: str = "",
    transcript: str = "",
    config: dict[str, Any] | None = None,
) -> str | None:
    """
    Pilot answered an A/B, soft-acked, or freeform-reacted.

    With Ollama/Gemini/OpenAI on, freeform speech can riff live — you do not
    have to pick one of the two buttons. Freeform turns stay on the same bit
    for a few exchanges before Texaco rotates to a new opener.
    """
    if not isinstance(state, dict) or not is_open(state):
        return None
    choices = current_choices(state)
    awaiting = is_awaiting_react(state)
    row = _row(state) or {}
    opener = str(row.get("opener") or "").strip()
    turns = int(row.get("turns") or 0)
    live_on = llm_live_enabled(config)
    choice: dict[str, Any] | None = None
    want = str(choice_id or "").strip()
    freeform = False
    chatty = looks_like_question_or_chat(transcript)
    if want and want in {"_ack", "ack"}:
        choice = {
            "id": "_ack",
            "say": "Roger",
            "reply": random.choice(_DEFAULT_REACT_REPLIES),
        }
        freeform = True
    elif want and want not in {"_any", "any"}:
        choice = next((c for c in choices if str(c.get("id") or "") == want), None)
    elif want in {"_any", "any"}:
        freeform = True
    if choice is None and transcript and choices:
        choice = match_choice(transcript, choices)
        if choice and str(choice.get("id") or "") == "_ack":
            freeform = True
    # Real questions / multi-word chat must not get trapped in a one-word A/B
    # canned reply when the live LLM is available.
    if (
        live_on
        and transcript.strip()
        and chatty
        and choice is not None
        and str(choice.get("id") or "") not in {"_ack", "ack"}
    ):
        choice = None
        freeform = True
    # Freeform when awaiting a react, or with live LLM (even over A/B buttons).
    # Echo of Texaco's own TX is rejected below so she doesn't answer herself.
    if choice is None and transcript.strip() and (awaiting or live_on or chatty):
        freeform = True
    if choice is None and awaiting and not live_on:
        choice = match_react(transcript or "roger", current_reacts(state))
    if choice is None and not freeform:
        return None
    if freeform and looks_like_own_echo(transcript, state):
        return None

    cs, tcs = _names(callsign, tanker)
    if transcript.strip():
        append_history(state, "pilot", transcript)
    elif choice is not None and not freeform:
        said = str(choice.get("say") or choice.get("id") or "").strip()
        if said:
            append_history(state, "pilot", said)
    reply = ""
    follow = None
    used_live = False
    if freeform and transcript.strip() and live_on:
        live = try_llm_react(
            config, opener=opener, pilot=transcript, state=state
        )
        if live:
            reply = naturalize_reply(_fill(live, cs, tcs))
            used_live = True
    if not reply and choice is not None and not freeform:
        reply = naturalize_reply(
            _fill(str(choice.get("reply") or "Fair enough."), cs, tcs),
            force=True,
        )
        follow = choice.get("follow") if isinstance(choice.get("follow"), dict) else None
    if not reply and freeform:
        # LLM miss / Ollama down → canned boom chat, never a "hold on" stall.
        reacted = match_react(transcript or "roger", current_reacts(state))
        soft = str(
            reacted.get("reply") or random.choice(_DEFAULT_REACT_REPLIES)
        )
        follow = (
            reacted.get("follow")
            if isinstance(reacted.get("follow"), dict)
            else None
        )
        reply = naturalize_reply(_fill(soft, cs, tcs), force=True)
    if not reply and choice is not None:
        reply = naturalize_reply(
            _fill(str(choice.get("reply") or "Fair enough."), cs, tcs),
            force=True,
        )
        follow = choice.get("follow") if isinstance(choice.get("follow"), dict) else None
    if not reply:
        return None
    append_history(state, "boom", reply)
    if follow and (follow.get("choices") or follow.get("kind") in {"riff", "open"}):
        follow_open = _fill(str(follow.get("opener") or ""), cs, tcs)
        _apply_open_bit(state, follow, schedule_auto=True, opener=follow_open)
        append_history(state, "boom", follow_open)
        return f"{reply} {follow_open}".strip()

    # First-contact hello is done — next bit is a real question, not more small talk.
    if str(row.get("id") or "") == _GREET_ID:
        wait = schedule_next_question(state, llm=used_live or live_on)
        state[_STATE_KEY]["break_s"] = wait
        return reply

    # Keep the same bit open for a few freeform / live exchanges.
    if freeform and (used_live or live_on or awaiting or chatty):
        nxt = turns + 1
        if nxt < _MAX_FREEFORM_TURNS:
            _keep_react_open(
                state,
                opener=reply or opener,
                turns=nxt,
                llm=used_live or live_on,
            )
            return reply

    wait = schedule_next_question(state, llm=used_live or live_on)
    state[_STATE_KEY]["break_s"] = wait
    return reply


def attach_continuation_deferred(
    result: dict[str, Any], state: dict[str, Any] | None
) -> dict[str, Any]:
    """If a break / riff timer is armed, tell the UI when to fire the next bit."""
    if not isinstance(result, dict):
        return result
    delay = continuation_delay_s(state)
    if delay is None:
        return result
    row = _row(state) or {}
    try:
        stored = float(row.get("break_s") or 0)
    except (TypeError, ValueError):
        stored = 0.0
    wait = stored if stored >= 6.0 else max(delay, _BREAK_MIN_S)
    result["deferred"] = {
        "kind": "tanker_chat_continue",
        "delay_s": wait,
    }
    return result
