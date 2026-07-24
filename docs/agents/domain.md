# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root, or
- **`CONTEXT-MAP.md`** at the repo root if it exists — it points at one `CONTEXT.md` per context. Read each one relevant to the topic.
- **`docs/adr/`** — read ADRs that touch the area you're about to work in. In multi-context repos, also check `src/<context>/docs/adr/` for context-scoped decisions.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

This is a **single-context** repo:

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-example-decision.md
│   └── 0002-another-decision.md
└── src/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) — but worth reopening because…_

## Term → code map

Where each glossary term lives in `src/agentg/`:

| Term | Code home |
|---|---|
| Linking, Invite code | `linking_store.py` (`LinkingStore`), `linking.py` (the conversation) |
| Session, Set, Gap | `training.py` (`TrainingStore`) |
| Routine, Workout, Rules doc | `routines.py` (`RoutineStore`) |
| Weight suggestion | `progression.py` (pure math), `advice.py` (wiring) |
| Note | `notes.py` (`NotesStore`) |
| Snapshot | `snapshot.py` |
| Check-in, Nudge | `checkin.py` (decision), `checkin_store.py` (state), `checkin_sweep.py` (send) |
| Demo | `demos.py` (`DemoStore`), `demo_media.py`, `demo_ingest.py` |
| Catalog | `catalog.py` |
| Compaction | `compaction.py` |
| Forget-me | `forget.py` (`ForgetStore`) |
| Coach-only actions | `coaching.py` |

## Known naming drift

Flagged during domain modeling (2026-07-24); don't spread further:

- "Session" carries three meanings in code: the domain Session (gym visit), the SDK conversation session (`SQLAlchemySession`, keyed `member:{id}`), and SQLAlchemy DB sessions (`async_sessionmaker`). Domain language reserves **Session** for the gym visit; call the SDK one "chat history".
- docs/spec.md keeps its historical section title "Onboarding & gym linking"; code and glossary say **Linking**.
