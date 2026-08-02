# Per-member memory design

Resolves [Design per-member memory (#7)](https://github.com/ivzc07/agentg/issues/7).
Framework: OpenAI Agents SDK (Python) on Postgres/SQLAlchemy, per
[ADR 0001](../adr/0001-agent-framework.md). Terms per [CONTEXT.md](../../CONTEXT.md).

## Summary

Memory is three layers, each with one job:

1. **Domain tables (Postgres)** — the facts: Members, Routines, Workouts, Sessions, Sets, Exercises. Source of truth; the agent reads and writes them only through tools.
2. **Conversation history (SDK Sessions)** — what was said: the Agents SDK's `SQLAlchemySession`, one session per member, stored in the same Postgres.
3. **Member notes (our table)** — what the agent learned: injuries, preferences, goals, tone. Written by a tool when the member volunteers something durable; small enough to load whole.

Each chat turn gets a compact **member snapshot** (identity, today's workout, days since last session, open notes) injected at the tail of the model input via `call_model_input_filter` so the static system prompt stays cacheable; everything bigger sits behind a tool. Gap awareness is never stored — it is derived from the newest Session by one indexed query.

## 1. Structured records — domain tables

All training facts live in our own Postgres tables, shaped by the domain model: `members`, `routines`, `workouts`, `sessions`, `sets`, `exercises` (+ `gyms`). Every row carries `gym_id`. Plan vs fact stays separated: Routine/Workout is prescription, Session/Set is record, and every performed Set is stored individually.

The agent touches these only through **function tools** (e.g. `log_session`, `get_routine`, `get_last_sets`). It never "remembers" a weight or a date from conversation history when a table holds it — the table wins. This keeps facts correct after model swaps, channel swaps, or history compaction, and keeps the memory design framework-independent (the SDK sees tools, not tables).

## 2. Conversation history — SDK session per member

The SDK's built-in session memory persists chat history and replays it into each run automatically. We use `SQLAlchemySession` ([docs](https://openai.github.io/openai-agents-python/sessions/)) pointed at the same Postgres, `create_tables=True`, with one session per member:

```
session_id = f"member:{member_id}"
```

Keyed by **member, not by channel chat id** — moving Telegram → WhatsApp keeps the whole history; the channel adapter only maps its own chat id to a member.

**Growth policy** (the SDK stores raw items and has no built-in summarizer, so this is ours): before each run, estimate the session's tokens with a chars/4 heuristic (no tokenizer dependency). History may occupy up to `HISTORY_TOKEN_BUDGET` (12 000) estimated tokens; compact when the estimate exceeds ~70% of that budget (`COMPACT_AT_TOKENS` = 8 400), so we fire well before the attention cliff rather than waiting on item count. Item count is not the primary signal — many small turns stay put; a few huge ones trigger. Always keep the newest `KEEP_RECENT` (20) items raw so the live exchange is never folded away. Compaction asks the model for a short summary of the oldest turns, writes anything durable to member notes first, then replaces the old items with one summary item at the **start** of history (`Session.get_items`/`clear_session`/`add_items`). Notes ride at the tail of the per-turn snapshot message (attention-favored edge of the U-curve), not mid-context. History is a working buffer, not an archive; nothing of record lives only in it (facts are in tables, durables in notes).

## 3. Member notes — long-term agent memory

One table, `member_notes`: `id, gym_id, member_id, kind, text, created_at, retired_at`. `kind` is a small enum: `injury`, `preference`, `goal`, `constraint`, `other`.

- **Written** by a `remember_note` tool the agent calls when a member volunteers something durable ("my shoulder is acting up", "I hate burpees"). Never elicited — same rule as RPE.
- **Read** in full every turn (they stay small — tens of rows, not thousands); `retired_at` soft-deletes outdated notes (`retire_note` tool: "shoulder's fine now").
- **Ours, not the framework's**: plain rows a Coach can read and edit later, portable across frameworks, and inspectable when the agent says something odd.

## 4. Recall — the per-turn snapshot

Before each run, the bot builds a **member snapshot** and injects it via `call_model_input_filter` at the tail of the model input (the attention-favored U-curve edge), keeping the static system prompt cacheable across turns:

- who: name, gym, member id
- plan: today's workout from the active Routine (name + exercise list)
- gap: date of newest Session and days since
- last time: date + headline of the most recent Session (e.g. "legs, 12 sets")
- notes: all active member notes

Target size: a few hundred tokens. **Rule: always-true, always-cheap facts go in the snapshot; anything member-specific but bulky goes behind a tool** — `get_recent_sessions(n)`, `get_last_sets(exercise)`, `get_routine()`, `get_exercise(name)`. The model pulls detail only when the conversation needs it ("what did I squat last week?" → one tool call, three rows).

## 5. Gap awareness

"Your last session was 2 days ago" must be cheap and reliable, so it is **derived, never stored**:

```sql
SELECT max(started_at) FROM sessions WHERE member_id = :id
```

with an index on `sessions (member_id, started_at DESC)`. Sessions are append-only facts reported in chat, so the newest one is the ground truth for attendance; there is no attendance counter to drift out of sync.

The same query, flipped around, powers proactive check-ins: the APScheduler sweep selects members whose newest session (or sign-up date, if none yet) is older than a threshold. This doc only guarantees the data is cheap; *when* to ping and what to say is [Set proactive check-in rules (#8)](https://github.com/ivzc07/agentg/issues/8).

## Out of scope here

- Check-in rules and tone — #8.
- Routine-generation rules and coach overrides — #6 (this doc only says where a Routine is stored and recalled).
- Retention/deletion of member data — raised to the map's fog (privacy & data retention).
