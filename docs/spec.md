# Gym coach agent — build-ready spec (v1)

Assembled from wayfinder map [#1](https://github.com/ivzc07/agentg/issues/1). Every decision below was resolved on a ticket; each section links its source. Nothing here is new design — this document folds the closed tickets' answers into one place so building can start. Building is a **new effort**, not part of the map that produced this spec.

Relative links (`docs/...`, `CONTEXT.md`) resolve once the draft PRs listed in the [source index](#source-index) merge to main.

## What v1 is

A chat-based gym coach agent. The agent **is** the coach: each gym member chats with it directly on Telegram, and it remembers each member — lifts, sessions, gaps ("your last session was 2 days ago").

**v1 scope:** routine generation, lift logging, per-member memory + gap awareness, proactive check-ins. Everything else is [out of scope](#out-of-scope).

## Framework & stack

*Source: [Research: pick the agent framework #2](https://github.com/ivzc07/agentg/issues/2), approved unchanged in [Approve the framework and stack #3](https://github.com/ivzc07/agentg/issues/3); recorded as [ADR 0001](docs/adr/0001-agent-framework-openai-agents-sdk.md) (PR [#12](https://github.com/ivzc07/agentg/pull/12)). Full comparison: [docs/research/agent-framework-comparison.md](docs/research/agent-framework-comparison.md) (PR [#10](https://github.com/ivzc07/agentg/pull/10)).*

**OpenAI Agents SDK (Python)** — the only candidate pairing a minimal agent loop with self-hostable session persistence out of the box, MIT-licensed, model-agnostic via LiteLLM. Owner-approved; implementation treats this as fixed.

| Piece | Choice |
|---|---|
| Language | Python 3.12+ |
| Agent framework | `openai-agents` |
| Model access | LiteLLM (any model — Claude, GPT, Gemini — switchable per task) |
| Telegram | aiogram v3, isolated in a `channels/` adapter |
| Database | PostgreSQL via SQLAlchemy — domain tables (every row carries `gym_id`) + SDK `SQLAlchemySession` for conversation memory |
| Scheduling | APScheduler (check-in sweeps), in-process |
| Deploy | Coolify: Dockerfile app + standalone Postgres resource (see §Hosting & deployment) |

Structured coach memory (lifts, session dates, injuries) is deliberately **our own Postgres tables accessed through tools**, not framework memory — the memory design stays framework-independent.

## Channel plan: Telegram first, WhatsApp later

*Sources: [#2](https://github.com/ivzc07/agentg/issues/2), [Design per-member memory #7](https://github.com/ivzc07/agentg/issues/7), [Link a new member to their gym on Telegram #14](https://github.com/ivzc07/agentg/issues/14), [Decide hosting and deployment #11](https://github.com/ivzc07/agentg/issues/11).*

Every design keeps the chat channel swappable:

- All Telegram code lives in a **`channels/` adapter module** — Telegram→WhatsApp is a one-module swap.
- Identity lives in a **`member_channels` table** (`member_id`, `channel`, `channel_user_id`), unique on `('telegram', <numeric user id>)` — never the mutable `@username`. WhatsApp later = a new row, not a schema migration.
- Conversation memory is keyed **by member** (`member:{id}`), not by channel chat id — history survives a channel switch.
- Gym linking uses Telegram deep links, conceptually portable to WhatsApp (`wa.me/…?text=`).
- **Delivery: long polling** — no public endpoint, domain, or webhook secret; dev and prod run identically. aiogram keeps webhook as a one-line switch later. Constraint: exactly one instance may poll → single replica. *(The "no public endpoint" property retires with the coach dashboard — [docs/spec-dashboard.md](spec-dashboard.md) adds a public HTTPS origin; delivery itself stays long polling.)*

WhatsApp migration details (provider, costs, message templates) are deliberately unspecified — they sharpen when the switch gets near.

## Domain model & glossary

*Source: [Model the domain #4](https://github.com/ivzc07/agentg/issues/4); canonical glossary in [CONTEXT.md](CONTEXT.md) (PR [#13](https://github.com/ivzc07/agentg/pull/13)), each term with an _Avoid_ list.*

- **Gym** — the tenant; every record belongs to exactly one gym, and nothing more is modeled about gyms.
- **Member** — the person training. Exactly one gym per member; switching gyms is a fresh start at the new gym, history stays behind.
- **Agent** — the software members chat with. May *present* as "your coach" in chat; in docs/code it is the Agent.
- **Coach** — reserved for the *human* trainer at a gym; adjusts routine rules, can hand-write routines.
- **Routine** — the plan a member follows: workouts pinned to weekdays ("Wednesday: legs"). The agent may deviate on request.
- **Workout** — the plan for one training day: a named exercise list; a routine day or improvised on the spot.
- **Session** — one real gym visit: what actually happened. Known **only from chat**; a session with no lifts still counts as a visit; it may not match the scheduled workout.
- **Exercise** — a named movement; can carry a short demo animation the agent sends.
- **Set** — one performed set: weight × reps. RPE/notes stored only when volunteered — never asked for.
- **Invite code** — the short random slug linking a new member (or coach) to their gym on Telegram; one active, regenerable code per gym (from [#14](https://github.com/ivzc07/agentg/issues/14)).

**Data-shape rules:**

- **Plan vs fact separation** — Routine/Workout = prescription; Session/Set = record.
- **Every set is stored**, not summaries: "squat 80 3×5" → three rows of 80×5. Enables "you did 80 last week — try 82.5".
- **Gaps are derived, never stored** — days since the newest Session. No attendance record.
- Deltas surfaced by the logging prototype ([#5](https://github.com/ivzc07/agentg/issues/5), recorded on [#4](https://github.com/ivzc07/agentg/issues/4)):
  - **Weight units**: kg/lb per-gym default, possibly per-member override — `Set.weight` is meaningless without it.
  - **Bodyweight sets**: `Set.weight` is nullable (dips: reps, no weight).
  - **Session lifecycle**: sets log into an *open* Session; "done" closes it; auto-close fallback for members who never say done.
  - **Set corrections**: Sets are updatable; the agent needs a current-session-scoped edit tool.
  - **Per-visit targets are ephemeral**: proposed numbers live in chat only, never on Workout.

## Data model (consolidated)

Every domain row carries `gym_id`. Fields below are consolidated from ticket resolutions; exact column types are build-time detail.

| Table | Decided fields / notes | Source |
|---|---|---|
| `gyms` | `invite_code` (unique, regenerable), `timezone`, weight-unit default; per-gym rules doc (own copy or fall through to the shipped default) | [#14](https://github.com/ivzc07/agentg/issues/14), [#8](https://github.com/ivzc07/agentg/issues/8), [#4](https://github.com/ivzc07/agentg/issues/4), [#6](https://github.com/ivzc07/agentg/issues/6) |
| `members` | `name`, `gym_id`, `created_at` at birth; coach flag; check-in state `on / off / snoozed_until(date) / lapsed` | [#14](https://github.com/ivzc07/agentg/issues/14), [#6](https://github.com/ivzc07/agentg/issues/6), [#8](https://github.com/ivzc07/agentg/issues/8) |
| `member_channels` | `(member_id, channel, channel_user_id)`, unique per channel identity | [#14](https://github.com/ivzc07/agentg/issues/14) |
| `member_notes` | plain rows: injuries, preferences, goals; soft-retired, coach-inspectable; written only on volunteered durable facts | [#7](https://github.com/ivzc07/agentg/issues/7) |
| `routines` / `workouts` | structure only — exercises, weekday pins; **never target weights**; coach-authored flag (pinned structure) | [#6](https://github.com/ivzc07/agentg/issues/6) |
| `sessions` | `started_at`, open/closed state; index `(member_id, started_at DESC)` powers gap queries and the check-in sweep | [#4](https://github.com/ivzc07/agentg/issues/4), [#7](https://github.com/ivzc07/agentg/issues/7) |
| `sets` | weight (nullable) × reps; RPE/notes optional; updatable | [#4](https://github.com/ivzc07/agentg/issues/4) |
| `exercises` | named movement + demo media reference; per-gym demo override (gym-scoped demo wins) | [#4](https://github.com/ivzc07/agentg/issues/4), [#15](https://github.com/ivzc07/agentg/issues/15) |
| SDK session storage | the framework's `SQLAlchemySession` tables, same Postgres, one session per member | [#7](https://github.com/ivzc07/agentg/issues/7) |

## Per-member memory

*Source: [Design per-member memory #7](https://github.com/ivzc07/agentg/issues/7); full design in [docs/design/memory.md](docs/design/memory.md) (PR [#16](https://github.com/ivzc07/agentg/pull/16)).*

Three layers, each with one job:

1. **Domain tables (Postgres)** — the facts. Source of truth; the agent reads/writes only through function tools (`log_session`, `get_last_sets`, …) and never trusts chat history for a fact a table holds.
2. **Conversation history (SDK Sessions)** — what was said. `SQLAlchemySession` in the same Postgres, one session per member (`member:{id}`). Growth is ours to manage: compact when a chars/4 token estimate exceeds ~70% of the history budget (not on item count), keep the newest turns raw, fold older ones into a summary item, and push anything durable into notes.
3. **Member notes (`member_notes`)** — what the agent learned. Written only when the member volunteers something durable, via a `remember_note` tool. Deliberately not framework memory — portable and editable.

**Recall**: each turn injects a compact member snapshot (identity, today's workout, days-since-last-session, last-session headline, active notes — a few hundred tokens) via dynamic instructions; anything bulkier sits behind a tool.

**Gap awareness**: derived, never stored — `max(sessions.started_at)` per member. The same query powers the check-in sweep.

## Onboarding & gym linking

*Source: [Link a new member to their gym on Telegram #14](https://github.com/ivzc07/agentg/issues/14).*

- **Mechanism**: per-gym Telegram deep link — `t.me/<bot>?start=<invite-code>`. Distribution is the gym's affair (QR poster, coach shares the link).
- **Invite code**: one active, regenerable code per gym (unique column on the gym record). Leaked → regenerate; the old code stops matching. No admin UI in v1 — regeneration is an operational update.
- **Cold start (no payload)**: polite dead end — the agent explains it coaches partner gyms and points at the gym's link/QR; a code typed as plain text is accepted too. It **never lists gyms**. No member row until a valid code arrives.
- **Sign-up collects name only**, prefilled from the Telegram profile and confirmed in the greeting. No phone or email. Goals/injuries arrive later as volunteered notes.
- **Re-links**: same gym → recognized, plain greeting. A *different* gym's link → explicit confirm ("switching means a fresh start — your history stays with <old gym>"); on yes: old member row untouched, new member row at the new gym, `member_channels` re-pointed. No silent switch.
- **Coach identification**: linking also covers how a coach gets linked to their gym and flagged as coach — coaches use the same bot as coach-flagged users.

## Routine generation & coach overrides

*Source: [Define routine rules and coach overrides #6](https://github.com/ivzc07/agentg/issues/6).*

- **Generation** — the LLM writes each routine but must follow a plain-text **per-gym rules doc**. Output is saved as structured Routine/Workout rows using only exercises from the `exercises` table, and goes straight to the member — no coach approval gate; members tweak in chat.
- **Where rules live** — in data, as plain text. One default doc ships with the product; a gym that wants different rules gets its own editable copy; the agent follows exactly **one** doc — the gym's if it exists, else the default. Progression rules live in the same doc. No third rules layer.
- **Intake — four things, conversationally**: goal, experience level, training days + which weekdays, injuries & limitations (which land in `member_notes`). No body stats, no equipment question (equipment is a gym-level fact for the rules doc).
- **Adaptation — derived weights, consented structure.** Routines store structure only. Each session the agent computes suggested weights from logged Sets using the doc's progression rules ("+2.5 kg once all sets done; deload after stalls"). Structural changes are proposed in chat and applied only on member agreement. The Sets table stays the single source of truth.
- **Gap-deload rule** (from the logging prototype): after a long gap the agent offers ~10% lighter weights — the threshold and percentage are a coach-adjustable rule in the doc, not hardcoded.
- **Coach overrides — chat, as a flagged coach.** Coach-only tools: view/edit the gym's rules doc, hand-write a routine for a member. The agent previews the result and saves only on the coach's confirm.
- **Coach-authored routines — pinned structure, live weights.** The agent never restructures a coach-written routine; permanent-change requests are referred to the coach (one-off improvised workouts remain fine). Weight suggestions still derive from logged sets unless the coach's routine says otherwise.

## The logging conversation

*Source: [Prototype the workout-logging conversation #5](https://github.com/ivzc07/agentg/issues/5); accepted script (Variant A) in [docs/prototypes/workout-logging-conversation.md](docs/prototypes/workout-logging-conversation.md) (PR [#19](https://github.com/ivzc07/agentg/pull/19)).*

Owner chose **Variant A — "Just type it": free-text logging, no inline buttons for sets.** Tap-through buttons (B) and assume-last-time checklists (C) are rejected. The conversation's shape:

- **Open**: member says "I'm here" → agent recalls the last visit ("2 days ago — legs"), names today's workout from the Routine, lists each exercise with last-time numbers as *reference, not assumption*.
- **Log**: terse free text per exercise — "bench 60 8,8,8", "same as last time" (= last Session's sets for that exercise), "dips 10,10,9". The agent parses and echoes each as confirmed sets.
- **Correct**: "actually bench was 62.5 not 60" edits the just-logged sets.
- **Close**: "done" → short summary (sets, what went up) + specific encouragement + next routine day.
- **Gap return**: after a long gap, a "no guilt" opener plus the ease-back offer from the rules doc.

Tone as scripted: light emoji, specific encouragement.

> **Load-bearing UX risk**: free-text parsing quality. The whole logging experience rests on the agent parsing terse lifting shorthand correctly — worth early, focused testing in the build.

## Proactive check-ins

*Source: [Set proactive check-in rules #8](https://github.com/ivzc07/agentg/issues/8).*

- **Trigger** — routine-aware: a missed pinned Routine day arms a nudge. Members without a Routine: flat 3 days since last Session.
- **When** — nothing on the miss itself; the nudge lands the morning of the member's *next* pinned day ("Missed legs Monday — back on it today?"). Fallback members: the morning the 3-day gap trips.
- **Send window** — 09:00 gym-local time; hard quiet hours 21:00–08:00, nothing proactive ever.
- **Frequency cap** — max 2 nudges per calendar week, never consecutive days. Any reply or logged Session resets the rhythm.
- **Give-up rule** — after ~2 weeks of ignored nudges (≈4 sends, no reply, no Session): one wind-down message, then silence; member flagged **lapsed** (queryable, no dashboard).
- **Opt-out** — plain chat, no commands: "stop checking in on me" → off; "I'm traveling for 2 weeks" → snoozed until a date. The agent confirms and says how to turn it back on.
- **Tone** — warm, direct, zero guilt. Canonical examples live in the [#8 resolution](https://github.com/ivzc07/agentg/issues/8).
- **Accepted edge** — a nudge may fire when the member trained but didn't log; fine — it doubles as a logging prompt.

## Exercise demo media

*Source: [Source exercise demo videos/GIFs #15](https://github.com/ivzc07/agentg/issues/15), owner override recorded there; research in [docs/research/exercise-demo-media.md](docs/research/exercise-demo-media.md) (PR [#17](https://github.com/ivzc07/agentg/pull/17)).*

- **Source (owner override of the research recommendation)**: the free [hasaneyldrm/exercises-dataset](https://github.com/hasaneyldrm/exercises-dataset) GIFs (1,324 exercises, 180×180), as-is, no Gymvisual purchase. **Accepted rights gap**: that repo's own README states the media is © Gymvisual and reusers must buy their own license — flagged and knowingly accepted by the owner. ([Confirm Gymvisual license #18](https://github.com/ivzc07/agentg/issues/18) closed as moot.)
- **Delivery**: transcode GIFs to short soundless H.264 MP4s, self-host as the system of record, send via Telegram `sendAnimation` (autoplaying loop), cache a per-Exercise `file_id` after first upload — every later send is an instant no-upload resend. `file_id`s are per-bot, so our storage stays canonical.
- **Per-gym overrides from day one**: an override is the same media asset scoped to a Gym — gym-scoped demo wins over the Exercise default. A coach's own filmed demo flows through the identical transcode + `file_id` path.

## Safety rules

*Source: [Define safety rules #20](https://github.com/ivzc07/agentg/issues/20).*

- **Where safety lives** — in the per-gym rules doc, coach-editable like everything else. The single non-editable floor, baked into the agent: **never diagnose or prescribe; always refer acute pain or medical questions to a professional.** No special warning when a coach edits safety language out; no silent re-injection of defaults. If a gym strips its safety section, the backstop is all that remains — accepted knowingly.
- **Injuries — hard avoid until cleared.** Never program exercises that load an injured area; prefer pain-free alternatives; when in doubt, leave it out ("loads the area" is LLM judgment guided by the doc). The restriction stands until the member says it's healed — the agent confirms, updates `member_notes`, only then reintroduces.
- **Refuse-or-refer defaults shipped in the doc** (all coach-editable): nutrition/supplements → decline, point to the coach; steroids/PEDs → refuse outright with a health warning; rehab programming → refer to a physio (the agent programs *around* injuries, never treatment *for* them); disordered-eating red flags → warm referral to coach + professional support, no coaching toward the goal; urgent symptoms → "stop training now, seek emergency care".
- **Referral mechanics — consent-gated coach ping.** "Want me to flag this to your coach?" — on yes, message the gym's coach on Telegram and log to `member_notes`; on no, just log. The member controls what's shared.
- **Disclaimers** — ~~one short warm line at intake ("I'm an AI coach, not a medical professional…"), repeated on first routine delivery and whenever an injury or new pain comes up.~~ **Superseded by [ADR 0002](docs/adr/0002-agent-language-and-disclosure.md):** the spoken AI/medical disclaimer is removed; the Agent never announces it is an AI and deflects if asked. The behavioral floor below (never diagnose, always refer) stands.

## Privacy & data retention

*Source: [Define privacy and data retention rules #21](https://github.com/ivzc07/agentg/issues/21).*

The bar is **good practice, not a legal regime** — no timelines committed to anyone.

- **Forget-me = hard-delete everything.** Member asks in chat; one confirm; then all three stores are wiped — domain rows, `member_notes`, SDK chat session. No grace period, no anonymized residue. Member-initiated only; the coach is not notified.
- **No export in v1** — polite "not yet"; revisit on demand.
- **Chat retention = compaction.** When old turns compact into a summary, the raw turns are deleted — not archived. The summary lives as long as the member. No time-based purges.
- **Health info gets no special regime.** The consent gate from safety is the only sharing control; injury notes persist while the member is active and die with the member on deletion.

## Hosting & deployment

*Source: [Decide hosting and deployment #11](https://github.com/ivzc07/agentg/issues/11).*

- **Hosting**: the existing Coolify VPS (already running production bots). Shared-box risk surfaced and accepted.
- **Shape**: a Coolify **application** (Dockerfile deploy from this repo) + a **standalone Coolify Postgres resource** — not Postgres inside the app's compose file, because Coolify's scheduled backups only cover databases it manages as resources.
- **Backups**: Coolify scheduled backup, daily, to an S3-compatible offsite bucket, ~30-day retention. Bucket provider (R2/B2/Hetzner) is a build-time detail.
- **Telegram**: long polling, single replica (exactly one instance may poll).
- **Environments**: production only. Developers test with a separate dev bot token locally; no persistent staging for v1.
- **Defaults**: auto-deploy on push to main, secrets as Coolify env vars, single app instance, APScheduler in-process.

## Multi-gym boundaries

Every record knows which gym it belongs to (`gym_id` on every row), and **nothing more**: no gym onboarding flow, no admin panel, no billing, ~~no coach dashboard~~. Per-gym rules-doc creation is an operational update, not a product surface. **Superseded in part by map [#70](https://github.com/ivzc07/agentg/issues/70):** a coach dashboard is now specified in [docs/spec-dashboard.md](spec-dashboard.md), and invite-code regeneration moves onto its settings screen.

## Out of scope

Consciously excluded from this effort (map [#1](https://github.com/ivzc07/agentg/issues/1)):

- Gym onboarding, admin panels, billing.
- Nutrition advice (declined for v1; refusal script ships in the default rules doc).
- ~~Coach view / dashboard over all members (the `lapsed` flag is queryable data only).~~ **Superseded by map [#70](https://github.com/ivzc07/agentg/issues/70):** now specified in [docs/spec-dashboard.md](spec-dashboard.md).
- Building the agent itself — starts as a new effort, not on this map.

## Deferred build-time details

Not spec gaps — explicitly classified as build-time choices during resolution:

- S3-compatible backup bucket provider (R2 / B2 / Hetzner) — [#11](https://github.com/ivzc07/agentg/issues/11).
- Session auto-close timeout value — [#4](https://github.com/ivzc07/agentg/issues/4).
- Exact column types / migration layout for the consolidated data model.
- WhatsApp migration (provider, costs, templates) — sharpens when the switch gets near.

## Source index

| Ticket | Answer lands in |
|---|---|
| [#2 Research: pick the agent framework](https://github.com/ivzc07/agentg/issues/2) | [docs/research/agent-framework-comparison.md](docs/research/agent-framework-comparison.md) via [PR #10](https://github.com/ivzc07/agentg/pull/10) |
| [#3 Approve the framework and stack](https://github.com/ivzc07/agentg/issues/3) | [ADR 0001](docs/adr/0001-agent-framework-openai-agents-sdk.md) via [PR #12](https://github.com/ivzc07/agentg/pull/12) |
| [#4 Model the domain](https://github.com/ivzc07/agentg/issues/4) | [CONTEXT.md](CONTEXT.md) via [PR #13](https://github.com/ivzc07/agentg/pull/13) |
| [#5 Prototype the workout-logging conversation](https://github.com/ivzc07/agentg/issues/5) | [docs/prototypes/workout-logging-conversation.md](docs/prototypes/workout-logging-conversation.md) via [PR #19](https://github.com/ivzc07/agentg/pull/19) |
| [#6 Define routine rules and coach overrides](https://github.com/ivzc07/agentg/issues/6) | resolution comment |
| [#7 Design per-member memory](https://github.com/ivzc07/agentg/issues/7) | [docs/design/memory.md](docs/design/memory.md) via [PR #16](https://github.com/ivzc07/agentg/pull/16) |
| [#8 Set proactive check-in rules](https://github.com/ivzc07/agentg/issues/8) | resolution comment |
| [#11 Decide hosting and deployment](https://github.com/ivzc07/agentg/issues/11) | resolution comment |
| [#14 Link a new member to their gym on Telegram](https://github.com/ivzc07/agentg/issues/14) | resolution comment |
| [#15 Source exercise demo videos/GIFs](https://github.com/ivzc07/agentg/issues/15) | [docs/research/exercise-demo-media.md](docs/research/exercise-demo-media.md) via [PR #17](https://github.com/ivzc07/agentg/pull/17) + owner override on the ticket |
| [#18 Confirm Gymvisual license](https://github.com/ivzc07/agentg/issues/18) | closed as moot (owner override on #15) |
| [#20 Define safety rules](https://github.com/ivzc07/agentg/issues/20) | resolution comment |
| [#21 Define privacy and data retention rules](https://github.com/ivzc07/agentg/issues/21) | resolution comment |
