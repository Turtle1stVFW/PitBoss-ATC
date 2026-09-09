# Plan: docs/AGENCY_CALLS.md

Approved: 2026-09-08

## Goal
One maintainer/operator reference of pilot calls, match keywords, and agency replies — by agency — with placeholders like `<Callsign>`, `<Runway>`.

## Scope
- Canonical agencies from agencies._CORE / CHANNELS, with spoken aliases under each.
- Timeline/step intents, ad-hoc requests, and boom chat from tanker_chat_library.
- Mission-specific voice_phrases (e.g. f2c_swll.json) in separate sections.

## Approach
1. Extract from voice_intent.INTENTS (examples, keyword groups, channels, template links).
2. Resolve replies from atc_phrase builders / TEMPLATE_CHOICES, voice_actions, tanker*, agencies redirects.
3. Normalize spoken lines with <bracket> placeholders.
4. Structure: intro + data-source map; per-agency sections; cross-agency ad-hoc; tanker boom chat; mission overrides.
5. Link from docs/README.md.

## Files
- docs/AGENCY_CALLS.md (new)
- docs/README.md (index link)

## Risks
- Some builders are highly conditional; examples are representative, not every branch.
- Keyword groups are combinatorial; doc lists intent keyword groups / example phrasing.
