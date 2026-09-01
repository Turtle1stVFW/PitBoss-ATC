"""
Local-LLM fallback for messy radio: map a Whisper transcript onto an existing
ATC intent. Never invents a clearance — the scripted phrase still plays.
"""

from __future__ import annotations

from typing import Any

import voice_intent

_NLU_TIMEOUT_S = 4.0

# Grammar already decided this is not ATC business — do not second-guess.
_SKIP_REASON_PREFIXES = (
    "nothing heard",
    "flight chatter",
    "crew talk",
    "talking to the flight",
    "no agency addressed",
    "readback of last ATC",
    "boom chat echo",
)

_ALLOW_REASONS = (
    "no ATC call recognised",
    "low confidence",
)


def nlu_enabled(config: dict[str, Any] | None) -> bool:
    if not isinstance(config, dict):
        return False
    if not bool(config.get("voice_nlu_enabled", True)):
        return False
    try:
        import tanker_chat as tanker_chat_mod

        return tanker_chat_mod.resolve_llm_provider(config) is not None
    except Exception:
        return False


def should_try(
    evaluation: voice_intent.Evaluation, *, awaiting_readback: bool = False
) -> bool:
    if evaluation is None or evaluation.match is not None:
        return False
    reason = str(evaluation.reason or "")
    if any(reason.startswith(p) for p in _SKIP_REASON_PREFIXES):
        return False
    if not any(reason.startswith(p) for p in _ALLOW_REASONS):
        return False
    # Unaddressed "no match" stays silent — same discipline as the grammar.
    if reason.startswith("no ATC call recognised") and not awaiting_readback:
        if not (evaluation.address and evaluation.address.to_atc):
            return False
    return True


def allowed_intents(
    *,
    channel: str = "",
    phase: str = "",
    expected: str = "",
    awaiting_readback: bool = False,
    steps: list[dict[str, Any]] | None = None,
    current_step_id: str = "",
) -> list[voice_intent.Intent]:
    extra = voice_intent.step_intents(steps)
    current_step = voice_intent.step_by_id(steps, current_step_id)
    ch = (channel or "").strip().lower()
    ph = voice_intent.normalize_mission_phase(phase, channel=ch)
    out: list[voice_intent.Intent] = []
    seen: set[str] = set()
    for intent in tuple(voice_intent.INTENTS) + tuple(extra):
        if intent.id in seen:
            continue
        if intent.channels and ch and ch not in intent.channels:
            continue
        if intent.phases and ph and ph not in intent.phases:
            continue
        if intent.id == "acknowledge_readback" and not awaiting_readback:
            continue
        # Repeating taxi instructions is the readback — not a new taxi call.
        if awaiting_readback and intent.id == "ready_taxi":
            continue
        # LUAW is already the plan — "ready for departure" issues it.
        if (expected or "").strip().lower() in ("lineup", "line_up_and_wait") and intent.id == "request_lineup":
            continue
        if intent.id in voice_intent._C2_INTENT_IDS and not voice_intent.step_offers_c2(
            current_step, channel=ch
        ):
            continue
        if intent.id == "request_approach" and ch == "tower":
            # Tower only takes visual patterns — keep the same rule as scoring.
            pass
        seen.add(intent.id)
        out.append(intent)
    return out


def parse_choice(
    raw: Any, allowed: list[voice_intent.Intent]
) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(raw, dict):
        return None
    want = str(raw.get("intent") or raw.get("id") or "").strip()
    if not want or want.lower() in {"none", "null", "unknown", "silence", "ignore"}:
        return None
    allowed_ids = {i.id for i in allowed}
    if want not in allowed_ids:
        return None
    slots: dict[str, Any] = {}
    rwy = str(raw.get("runway") or "").strip()
    if rwy:
        slots["runway"] = rwy
    rec = str(raw.get("recovery") or "").strip()
    if rec:
        slots["recovery"] = rec
    return want, slots


def match_from_choice(
    intent_id: str,
    extra_slots: dict[str, Any],
    *,
    transcript: str,
    allowed: list[voice_intent.Intent],
    runways: list[str] | None = None,
    addressed: str = "",
) -> voice_intent.Match | None:
    intent = next((i for i in allowed if i.id == intent_id), None)
    if intent is None:
        return None
    text = voice_intent.normalize(transcript)
    slots: dict[str, Any] = dict(extra_slots or {})
    if intent.id == "request_runway":
        runway = voice_intent.extract_runway(transcript, runways) or slots.get("runway")
        if runways and runway:
            known = voice_intent.extract_runway(str(runway), runways)
            runway = known or None
        elif runway:
            runway = voice_intent.extract_runway(str(runway), runways) or str(runway)
        if not runway:
            return None
        slots["runway"] = runway
    recovery = voice_intent.extract_recovery(transcript) or slots.get("recovery")
    if recovery and intent.id in (
        "inbound_recovery",
        "request_landing",
        "request_approach",
    ):
        if intent.id == "request_approach" and addressed == "tower":
            if recovery not in voice_intent._TOWER_RECOVERY_KEYS:
                recovery = None
        if recovery:
            slots["recovery"] = recovery
    if intent.id in (
        "inbound_recovery",
        "request_approach",
        "request_hold",
        "approach_continue",
    ):
        vfr = voice_intent.extract_vfr_recovery(transcript)
        feeder = voice_intent.extract_stryk_feeder(transcript)
        if feeder:
            slots["vfr_recovery"] = feeder
        elif vfr:
            slots["vfr_recovery"] = vfr
        iaf = voice_intent.extract_iaf(transcript)
        if iaf:
            slots["iaf"] = iaf
    if addressed:
        slots["channel"] = addressed
    return voice_intent.Match(
        intent=intent.id,
        kind=intent.kind,
        template=intent.template,
        confidence=0.82,
        slots=slots,
        transcript=transcript,
        normalized=text,
        step_id=intent.step_id,
        expected=False,
        summarised=True,
    )


def classify(
    evaluation: voice_intent.Evaluation,
    *,
    config: dict[str, Any] | None,
    channel: str = "",
    phase: str = "",
    expected: str = "",
    callsign: str = "",
    runways: list[str] | None = None,
    awaiting_readback: bool = False,
    steps: list[dict[str, Any]] | None = None,
    current_step_id: str = "",
) -> voice_intent.Match | None:
    """Ask the LLM to pick one allowed intent, or None."""
    if not nlu_enabled(config) or not should_try(
        evaluation, awaiting_readback=awaiting_readback
    ):
        return None
    transcript = str(evaluation.transcript or "").strip()
    if len(transcript.split()) < 2:
        return None
    addressed = str(
        (evaluation.address.agency if evaluation.address else "") or channel or ""
    ).strip().lower()
    allowed = allowed_intents(
        channel=addressed or channel,
        phase=phase,
        expected=expected,
        awaiting_readback=awaiting_readback,
        steps=steps,
        current_step_id=current_step_id,
    )
    if not allowed:
        return None
    lines = []
    for intent in allowed[:40]:
        bit = intent.id
        if intent.example:
            bit += f" — e.g. {intent.example}"
        elif intent.does:
            bit += f" — {intent.does}"
        lines.append(bit)
    expected_l = (expected or "").strip()
    prompt = (
        "You classify one US military ATC radio call from a fighter pilot. "
        "The keyword matcher did not fire. Pick the single best intent id from "
        "the allowed list, or none if it is chatter / unsure / not a request. "
        "Do not invent clearances. Do not pick an id that is not listed.\n"
        f"Agency: {addressed or channel or '(unknown)'}\n"
        f"Phase: {phase or '(unknown)'}\n"
        f"Callsign: {callsign or '(unknown)'}\n"
        f"Expected step: {expected_l or '(none)'}\n"
        f"Transcript: {transcript}\n\n"
        "Allowed intents:\n- "
        + "\n- ".join(lines)
        + "\n\nReturn JSON only: {\"intent\": \"<id or none>\"}. "
        "For request_runway also include \"runway\" like \"21L\"."
    )
    try:
        import tanker_chat as tanker_chat_mod

        raw = tanker_chat_mod.llm_json(
            config, prompt, timeout=_NLU_TIMEOUT_S, temperature=0.05, max_tokens=80
        )
    except Exception:
        return None
    parsed = parse_choice(raw, allowed)
    if not parsed:
        return None
    intent_id, extra = parsed
    return match_from_choice(
        intent_id,
        extra,
        transcript=transcript,
        allowed=allowed,
        runways=runways,
        addressed=addressed,
    )
