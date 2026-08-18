"""
Boom / reform small talk on tanker freq (ATP-56 join is separate).

Texaco starts a short A/B question while the receiver is in reform or on the
boom. The pilot answers with one word — Dunkin, Starbucks, morning, quiet —
no agency opener required. A large canned library is always available; an
optional LLM can mint a fresh thread mid-sortie and falls back if it is slow
or down.
"""

from __future__ import annotations

import copy
import json
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import atc_phrase
import tanker_chat_library as chat_lib

_STATE_KEY = "tanker_chat"
_LAST_KEY = "tanker_chat_last_id"
_RECENT_KEY = "tanker_chat_recent"
_RECENT_MAX = 12
_LLM_TIMEOUT_S = 6.5

# Soft acks close the thread without picking a side.
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


def is_open(state: dict[str, Any] | None) -> bool:
    row = (state or {}).get(_STATE_KEY) if isinstance(state, dict) else None
    return isinstance(row, dict) and bool(row.get("choices"))


def current_choices(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    row = (state or {}).get(_STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(row, dict):
        return []
    raw = row.get("choices") or []
    return [c for c in raw if isinstance(c, dict) and c.get("id")]


def fly_request_rows(state: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Fly buttons: Texaco starts the question; A/B answers while one is out."""
    rows: list[tuple[str, str]] = [("tanker_chat_start", "Texaco starts chat")]
    for choice in current_choices(state):
        say = str(choice.get("say") or choice.get("id") or "").strip()
        cid = str(choice.get("id") or "").strip()
        if say and cid:
            rows.append((f"tanker_chat_choice_{cid}", say))
    return rows


def fly_controls_visible(state: dict[str, Any] | None) -> bool:
    """Show boom-chat buttons after rejoin, even if the cursor is not on tanker."""
    if is_open(state):
        return True
    try:
        import tanker as tanker_mod

        return tanker_mod.has_rejoined(state)
    except Exception:
        return False


def end_chat(state: dict[str, Any] | None) -> None:
    if isinstance(state, dict):
        state.pop(_STATE_KEY, None)


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
                "reply": "Copy that. You're looking good.",
            }
    return None


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


def _fill(template: str, cs: str, tcs: str) -> str:
    return str(template or "").replace("{cs}", cs).replace("{tcs}", tcs).strip()


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
    say = " ".join(say.split())[:32]
    reply = " ".join(reply.split())[:280]
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
        if cleaned and cleaned.get("choices"):
            row["follow"] = {
                "id": str(cleaned.get("id") or "follow"),
                "opener": str(cleaned.get("opener") or "").strip(),
                "choices": list(cleaned.get("choices") or []),
            }
    return row


def normalize_llm_thread(
    raw: Any, *, allow_follow: bool = True
) -> dict[str, Any] | None:
    """Turn model JSON (or a dict) into a library-shaped thread, or None."""
    if isinstance(raw, str):
        raw = _parse_json_blob(raw)
    if not isinstance(raw, dict):
        return None
    opener = str(raw.get("opener") or raw.get("question") or "").strip()
    opener = " ".join(opener.split())
    if len(opener) < 12:
        return None
    if "{cs}" not in opener:
        opener = "{cs}, {tcs}, " + opener.lstrip(", ")
    choices_raw = raw.get("choices") or raw.get("options") or []
    if not isinstance(choices_raw, list):
        return None
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
        if len(choices) >= 2:
            break
    if len(choices) < 2:
        return None
    tid = _slug(str(raw.get("id") or opener.split(",")[-1][:40] or "llm"))
    if not tid.startswith("llm"):
        tid = f"llm_{tid}"
    return {"id": tid[:40], "opener": opener[:360], "choices": choices}


def _parse_json_blob(text: str) -> Any:
    blob = str(text or "").strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob, flags=re.I)
        blob = re.sub(r"\s*```$", "", blob)
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        pass
    start = blob.find("{")
    end = blob.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(blob[start : end + 1])
        except json.JSONDecodeError:
            return None
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


def _llm_prompt(recent: list[str]) -> str:
    avoid = ", ".join(recent[-8:]) if recent else "(none yet)"
    return (
        "You write one short boom-operator radio bit for a KC-135 refueling an F-16. "
        "Dry, funny, professional. Never mention DCS, AI, games, or that you are a model. "
        "Never use official tanker phraseology (rejoin, contact, disconnect, abort, "
        "observation, identified). Ask exactly one A/B question the pilot can answer "
        "with one or two words. Topics: coffee, food, pets, sports, cars, weather, "
        "weekends, cockpit life, movies, snacks. Avoid these recent ids: "
        f"{avoid}. Return JSON only with keys: id, opener, choices. opener uses "
        "{cs} and {tcs} as the fighter and tanker callsigns. choices is an array of "
        "exactly two objects with say (1-3 words), hits (short spoken aliases), "
        "reply (one or two boom-operator sentences). Optional follow on one choice: "
        "{opener, choices} with two more A/B options. Keep every line radio-short."
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


def _gemini_thread(config: dict[str, Any], key: str, recent: list[str]) -> dict[str, Any] | None:
    model = str(config.get("tanker_chat_llm_model") or _DEFAULT_GEMINI_MODEL).strip()
    model = model or _DEFAULT_GEMINI_MODEL
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(key)}"
    )
    payload = {
        "contents": [{"parts": [{"text": _llm_prompt(recent)}]}],
        "generationConfig": {
            "temperature": 1.15,
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
    return normalize_llm_thread(text or data)


def _openai_compatible_thread(
    config: dict[str, Any],
    *,
    url: str,
    key: str,
    model: str,
    recent: list[str],
    json_mode: bool = True,
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 1.15,
        "max_tokens": 700,
        "messages": [
            {
                "role": "system",
                "content": "Return one JSON object only. No markdown.",
            },
            {"role": "user", "content": _llm_prompt(recent)},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = _http_json(url, payload, headers)
    text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return normalize_llm_thread(text)


def _openai_thread(config: dict[str, Any], key: str, recent: list[str]) -> dict[str, Any] | None:
    model = str(config.get("tanker_chat_llm_model") or _DEFAULT_OPENAI_MODEL).strip()
    model = model or _DEFAULT_OPENAI_MODEL
    return _openai_compatible_thread(
        config,
        url="https://api.openai.com/v1/chat/completions",
        key=key,
        model=model,
        recent=recent,
        json_mode=True,
    )


def _ollama_thread(config: dict[str, Any], recent: list[str]) -> dict[str, Any] | None:
    model = str(config.get("tanker_chat_llm_model") or _DEFAULT_OLLAMA_MODEL).strip()
    model = model or _DEFAULT_OLLAMA_MODEL
    url = str(config.get("tanker_chat_llm_url") or _DEFAULT_OLLAMA_URL).strip()
    url = url or _DEFAULT_OLLAMA_URL
    if not url.endswith("/chat/completions"):
        url = url.rstrip("/") + "/v1/chat/completions"
    return _openai_compatible_thread(
        config,
        url=url,
        key="",
        model=model,
        recent=recent,
        json_mode=False,
    )


def try_llm_thread(
    config: dict[str, Any] | None, recent: list[str]
) -> dict[str, Any] | None:
    resolved = resolve_llm_provider(config)
    if not resolved or not isinstance(config, dict):
        return None
    provider, key = resolved
    try:
        if provider == "gemini":
            return _gemini_thread(config, key, recent)
        if provider == "openai":
            return _openai_thread(config, key, recent)
        if provider == "ollama":
            return _ollama_thread(config, recent)
    except Exception:
        return None
    return None


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


def start_chat(
    state: dict[str, Any] | None,
    callsign: str,
    tanker: dict[str, Any] | None,
    *,
    thread_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Texaco asks an A/B question. Returns spoken text."""
    if not isinstance(state, dict):
        state = {}
    recent = _recent_ids(state)
    node: dict[str, Any] | None = None
    if not thread_id:
        node = try_llm_thread(config, recent)
    if node is None:
        node = _pick_library(recent, thread_id)
    cs, tcs = _names(callsign, tanker)
    opener = _fill(str(node.get("opener") or ""), cs, tcs)
    tid = str(node.get("id") or "")
    state[_STATE_KEY] = {
        "id": tid,
        "choices": list(node.get("choices") or []),
    }
    _remember(state, tid)
    return opener


def answer_chat(
    state: dict[str, Any] | None,
    callsign: str,
    tanker: dict[str, Any] | None,
    *,
    choice_id: str = "",
    transcript: str = "",
) -> str | None:
    """Pilot picked a side (or roger). Returns tanker reply, or None if no match."""
    if not isinstance(state, dict) or not is_open(state):
        return None
    choices = current_choices(state)
    choice: dict[str, Any] | None = None
    want = str(choice_id or "").strip()
    if want:
        choice = next((c for c in choices if str(c.get("id") or "") == want), None)
    if choice is None and transcript:
        choice = match_choice(transcript, choices)
    if choice is None:
        return None
    cs, tcs = _names(callsign, tanker)
    if str(choice.get("id") or "") == "_ack":
        end_chat(state)
        return str(choice.get("reply") or "Copy.")
    reply = str(choice.get("reply") or "Copy.")
    follow = choice.get("follow") if isinstance(choice.get("follow"), dict) else None
    if follow and follow.get("choices"):
        follow_open = _fill(str(follow.get("opener") or ""), cs, tcs)
        state[_STATE_KEY] = {
            "id": str(follow.get("id") or state.get(_STATE_KEY, {}).get("id") or ""),
            "choices": list(follow.get("choices") or []),
        }
        body = f"{reply} {follow_open}".strip()
        return body
    end_chat(state)
    return reply
