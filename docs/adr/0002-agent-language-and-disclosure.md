# ADR 0002: Agent language rule & AI-disclosure

- **Status:** Accepted
- **Date:** 2026-07-18
- **Decision drivers:** Product owner wants the Agent to speak Spanish for a Spanish-first market. Amends the disclaimer rule from [Define safety rules (#20)](https://github.com/ivzc07/agentg/issues/20) / spec §Safety rules.

## Context

Nothing in the build pinned what language the Agent speaks. The `Agent`'s instructions were English, so the model trended English. The market is Spanish-first, but some Members may write in English. Two surfaces speak without an LLM turn to mirror: the onboarding state machine (`onboarding.py`) and the proactive check-in templates (`checkin.py`) — both were hardcoded English. There is no `language` column on `Gym` or `Member`.

Separately, the spec's safety section had the Agent say a spoken disclaimer — *"I'm an AI coach, not a medical professional"* — at intake, first routine, and on any injury. The owner does not want the Agent to announce that it is an AI.

## Decision

**Language — mirror the Member, sticky, default Spanish.**

1. The `Agent` replies in the Member's language, inferred from the whole conversation (not just the last message). Terse lift logs (`bench 60 8,8,8`) and stray loan-words carry no language signal — the Agent keeps the language it has been using and switches only on a clear, sustained change. With no signal yet (a Member's first messages), it speaks **Spanish**.
2. **Onboarding** fixed strings (`onboarding.py`) are **Spanish**.
3. **Check-in** templates (`checkin.py`) are **Spanish** — the one always-Spanish surface, because a proactive nudge has no incoming message to mirror and there is no stored per-Member language. Per-Member nudge language is a **deferred build-time detail**; it would need a `language` column and per-language copies, which no bilingual gym has asked for yet.
4. The English `exercises` catalog is **not** translated. Tool arguments must match catalog names exactly (`save_routine` rejects others); the Agent may say exercises naturally in chat but passes catalog names to tools.

**Disclosure — no spoken AI/medical disclaimer.**

5. The spoken disclaimer is **removed** entirely (from the Agent instructions and the default rules doc). The Agent never announces it is an AI or a medical professional.
6. The Agent never volunteers that it is an AI. If a Member asks directly ("eres un bot?" / "are you an AI?"), it **deflects** warmly and stays in its coaching role — it does not confirm, and it does not claim to be human.
7. The **behavioral safety floor is unchanged**: the Agent still never diagnoses or prescribes, and still refers acute pain, injuries, and medical questions to a qualified professional. It simply does so with no disclaimer preamble.

## Rationale

- **Mirroring beats always-Spanish** without costing anything: the chat is LLM-driven and the model already sees the conversation history, so it can match language with zero new state. Sticky inference avoids flip-flopping on language-neutral lift shorthand.
- **Spanish templates over a `language` column**: the two template surfaces are a handful of short strings. Adding schema + migration + per-language copies + a first-population path is real scope the spec never asked for, to serve a bilingual-gym case that does not exist yet. Deferring it is cheap to revisit.
- **Dropping the disclaimer** is the owner's product/tone call. Splitting "I'm an AI" from "not a medical professional" matters: the medical *behavior* (never diagnose, always refer) is what protects Members and is kept in full as the non-editable floor. Only the spoken line is gone.

## Consequences

- `src/agentg/agent.py` — instructions carry the language rule and the deflect-if-asked rule; the `Disclaimers:` paragraph is gone; the safety floor keeps its behavior but no longer instructs a spoken disclaimer.
- `src/agentg/onboarding.py` and `src/agentg/checkin.py` — fixed strings are Spanish; `WEEKDAY_NAMES` used in nudge copy is Spanish.
- `src/agentg/routines.py` — the default rules doc drops its `## Disclaimer` section.
- **This amends spec §Safety rules (#20).** The recorded disclaimer requirement is superseded by this ADR; the behavioral floor from #20 stands.
- Bilingual gyms wanting per-Member nudge language remain unserved until a `language` column is added — tracked here as deferred, not forgotten.
