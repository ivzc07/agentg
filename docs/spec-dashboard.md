# Coach dashboard - build-ready spec (v1)

Assembled from wayfinder map [#70](https://github.com/ivzc07/agentg/issues/70).
Every decision below was resolved on a ticket; each section links its source.
Nothing here is new design - this document folds the closed tickets' answers into one place so building can start.
Building is a **new effort**, not part of the map that produced this spec.

This spec deliberately reverses two lines of [docs/spec.md](spec.md) that ruled a coach dashboard out (`§Multi-gym boundaries`, `§Out of scope`); those lines now carry superseded markers pointing here.

## What the dashboard is

*Sources: [#71](https://github.com/ivzc07/agentg/issues/71), [#72](https://github.com/ivzc07/agentg/issues/72).*

A coach-facing **web dashboard** over one Gym's Members' training.
Members stay on Telegram; there is no member-facing web surface.

At the user level **Coach and Gym are the same operator** - the tenant's superuser.
"Coach" (a solo trainer programming per client) and "gym" (one routine for everyone) are two market segments buying the same product; the only difference is the name shown on the dashboard, never a feature split.
All coach-flagged Members of a Gym have identical powers; there is no owner/admin role and no coach-to-member assignment.

**Three jobs, in priority order** ([#71](https://github.com/ivzc07/agentg/issues/71)):

1. **Who needs me** - the screen the dashboard opens on: a Gym-wide roster sorted by Gap, with new Members and safety flags marked. Read-only: the Coach cannot message a Member, nudge the Agent, or dismiss anything from here. The list points; the Coach acts on the Member's page, in chat, or on the gym floor. This keeps the Agent the only voice in the Member's Telegram thread.
2. **Write a Member's Routine** - the one edit v1 must nail: a structured weekday-to-exercises editor on the Member's page, saved coach-authored so the Agent never restructures it.
3. **One Member's page** - recent Sessions, last weights per Exercise, active Routine, Notes, and any safety flag. Read-only apart from the Routine editor and the flag tick-off.

## What a Coach sees, and consent

*Source: [#72](https://github.com/ivzc07/agentg/issues/72), amended by [#80](https://github.com/ivzc07/agentg/issues/80).*

A Coach sees the Gym's whole coaching record on a Member, and the Member does not opt in.
The private line sits at the conversation, not at the data.

- **Visible by default, no consent**: Sessions, Sets, weights, Gap, the active Routine, and **all Notes** - injuries, preferences, goals, constraints, safety flags.
- **Never visible**: chat history and Compaction summaries. Notes are the curated channel for durable facts to reach a Coach.
- **Why no opt-in**: a Coach who cannot see injuries is programming blind - a consent gate there is a safety hole, not a privacy win. `CONTEXT.md` already defines a Note as "readable by a Coach".
- **Disclosure**: the Member is not told a Coach can see them - no line at Linking or Intake. If a Member asks directly, the Agent answers honestly.
- **No consent flag survives**: [#80](https://github.com/ivzc07/agentg/issues/80) removed `share_with_coach` from `flag_to_coach` entirely - the Agent logs every flag and pings the Gym's Coaches every time (see §Safety flags).
- **Forget-me is residue-free on the dashboard too**: the Member drops off the roster and their page returns a generic 404 indistinguishable from a mistyped id. No tombstone, no "this member left" wording.
- **The roster lists only non-coach Members** - a coach-flagged Member's own training does not appear.

## Access & identity

*Sources: [#73](https://github.com/ivzc07/agentg/issues/73) (research: [docs/research/coach-dashboard-auth.md](research/coach-dashboard-auth.md)), [#79](https://github.com/ivzc07/agentg/issues/79), [#90](https://github.com/ivzc07/agentg/issues/90).*

**The dashboard is a door the bot opens: a bot-issued magic link.** No Telegram OIDC, no email + password (both re-examined and rejected - the Telegram account already proves the identity, and `LinkingStore.identity_for` already turns a Telegram id into Member + Gym).

- The Coach sends `/dashboard` to the bot; the bot resolves the sender through `identity_for`, checks `is_coach`, and replies with a **one-time, short-TTL magic link**.
- Redeeming it sets a **90-day per-browser session cookie, refreshed on every visit** - an active Coach effectively never re-authenticates. An expired bookmark bounces to "send /dashboard to your bot", not an error.
- Safety-flag pings carry the same kind of authenticated deep link straight to the Member's page - **one mechanism serves both entries** (the deciding argument for the magic link).
- Needs a `dashboard_login_tokens` table (token hash, member_id, gym_id, expires_at, used_at). Telegram's link-preview fetcher may pre-burn a single-use token: disable the preview or redeem on POST - test it, don't assume.
- The dashboard checks `is_coach` **per request**, not per session - a demoted coach is out on the next click despite the 90-day cookie.

**Becoming a Coach** ([#90](https://github.com/ivzc07/agentg/issues/90)): a per-gym **coach invite link** - a second regenerable code with a visible `coach-` prefix (`t.me/<bot>?start=coach-<slug>`), created at provisioning and handed privately to the operator; it gives `set_coach()` its first production caller.

- An existing Member of that gym tapping it is promoted in place (`is_coach = true`); a different gym's coach link is the normal gym switch, arriving coach-flagged. A code typed as plain text works too, prefix included.
- A co-coach is added by forwarding the link; no ops involvement.
- Demotion stays an ops action (`set_coach(member, False)`); regenerating the coach code never unflags anyone.
- After coach-linking the Agent gives a coach-aware welcome (rules doc, writing routines, `/dashboard`); Intake and the gym's default Preset wait for an explicit "give me a plan too".

## The roster

*Sources: [#76](https://github.com/ivzc07/agentg/issues/76), [#85](https://github.com/ivzc07/agentg/issues/85), [#87](https://github.com/ivzc07/agentg/issues/87), [#88](https://github.com/ivzc07/agentg/issues/88), [#92](https://github.com/ivzc07/agentg/issues/92).*

**Three views the Coach switches between** via a segmented control in the top bar - the switcher is a product control, not a designer's shortlist:

- **Table** - dense sortable list.
- **Cards** - urgency bands plus an attendance grid: **4 weeks as a 7-column Mon-Sun day grid**, one square per day (dashed for future days), because a Routine pins Workouts to weekdays and a weekly grid could not show *which* day was missed.
- **Split** - permanent left rail, Member page in the right pane. Opening a Member from Table or Cards hides the switcher; in Split it stays.

All three sort by Gap and mark safety flags without re-sorting.

**Severity colour follows the Member's own schedule** ([#85](https://github.com/ivzc07/agentg/issues/85)) - fixed day-count thresholds die:

- Severity = consecutive **missed planned Workout days** since the last Session, judged against the Routine active on each date (see §Attendance). Today does not count until it is over.
- **Amber at 1 missed planned day, red at 3.**
- **Any Session resets the colour, even on an unplanned day**; the attendance grid still shows the miss.
- **No active Routine → no colour**, a grey "new" tag. No grace period: counting starts the moment a Routine exists.
- The top counter, group headers and headline follow the same rule; Gap stays only the sort key and the "away N days" row text.

**Search** ([#87](https://github.com/ivzc07/agentg/issues/87)): one text box beside the view switcher filtering live as you type, case- and accent-insensitive, name-only, identical across the three views, never touching the Gap sort; clearing it restores the full list.

**Lapsed Members** ([#88](https://github.com/ivzc07/agentg/issues/88)) fold into a collapsed **"Lapsed (N)" tail** at the bottom, out of the Gap sort and the headline counters, ordered most-recently-active first, forever - only auto-revival (any reply or Session) or departure removes a row.
A flag marker still shows on a lapsed row; search matches lapsed names and auto-expands the tail.
`snoozed` keeps its place with a "snoozed until" tag and no severity colour while it runs; `off` changes nothing.

**Who is on the roster** ([#92](https://github.com/ivzc07/agentg/issues/92)): only Members with a live channel (join `member_channels`, as `CheckinStore.sweep_rows` already does) - a gym switch's ghost row never appears. Coach-flagged Members are excluded ([#72](https://github.com/ivzc07/agentg/issues/72)).

## The Member page

*Sources: [#76](https://github.com/ivzc07/agentg/issues/76), [#92](https://github.com/ivzc07/agentg/issues/92).*

One shape under all three roster views:

- **Header**: name, member-since, Session count, Gap, last Session; a "Lapsed" or "snoozed until" tag when applicable.
- **Safety flag banner** with Tick off (see §Safety flags).
- **Left column**: Routine (with the Edit entry point) and Sessions.
- **Right column**: last weight per Exercise and Notes.
- **Retired Notes** show behind a collapsed "Retired (N)" tail with retirement dates - [#72](https://github.com/ivzc07/agentg/issues/72)'s "all Notes" stays true.
- A departed or forgotten Member's URL returns the same bare 404 - one shared dead end so the two exits stay indistinguishable.

## Attendance

*Source: [#81](https://github.com/ivzc07/agentg/issues/81).*

The dashboard answers "did they follow the plan" at the **day level only, inferred at read time**:

- A scheduled Workout day with no Session is a miss - a pure date comparison. No `workout_id` on Session; no schema or chat-path change.
- Each day is judged against the **Routine active on that date**, reconstructed as the most recent Routine created on or before it (superseded Routines survive deactivated with their Workouts).
- Reconstruction is **day-grained**: a Routine created mid-day governs that entire day. This is an accepted approximation - [#74](https://github.com/ivzc07/agentg/issues/74) already classed per-date reconstruction as approximate, and [#85](https://github.com/ivzc07/agentg/issues/85)'s today-never-counts-until-it-is-over rule keeps a same-day plan change from flagging a miss while the day still runs.
- **Content-level adherence is never claimed** - the Agent deliberately deviates around injuries, so "wrong exercises" would flag the Agent doing its job as the Member slacking.
- Extra unscheduled Sessions show as trained but never cancel a miss.

## Routines & Presets

*Sources: [#78](https://github.com/ivzc07/agentg/issues/78), [#77](https://github.com/ivzc07/agentg/issues/77), [#83](https://github.com/ivzc07/agentg/issues/83), [#86](https://github.com/ivzc07/agentg/issues/86), [#91](https://github.com/ivzc07/agentg/issues/91).*

**Behavior** ([#78](https://github.com/ivzc07/agentg/issues/78)):

- A Coach saves a named **Preset** (e.g. "Beginner") and applies it to one Member, a multi-select, or the whole Gym.
- A Preset is a **live link**: editing it updates every Member still on it, immediately.
- Editing a Member's Routine directly **silently forks** it into an individual Routine - no dialog; that Member drops off the Preset for good. This matches the existing rule that a hand-touched Routine is never restructured by the Agent again.
- A Gym can mark one Preset as its **default**; a brand-new Member lands on it at the end of Intake ([#77](https://github.com/ivzc07/agentg/issues/77)). Intake always runs in full - its answers become Notes the Agent needs regardless.
- The Agent's in-chat deviation stays ephemeral and never forks anything.

**Reconciling web and chat writes** ([#77](https://github.com/ivzc07/agentg/issues/77)):

- A web save **never disturbs a running Session** - the Member finishes the visit as they started it; the new plan applies from the next chat turn. No pending state.
- **The Member is always told, by the Agent**: "your coach updated your Routine" plus the new plan; a Preset edit messages every affected Member. The notice names the coach ([#91](https://github.com/ivzc07/agentg/issues/91)).
- **A stale web save is refused**: if the active Routine changed since the editor loaded (the Agent replaced it from chat), the save is rejected and the page shows the fresh version.

**Data shape** ([#83](https://github.com/ivzc07/agentg/issues/83)): **copy-on-apply**.

- Applying or editing a Preset stamps each linked Member a fresh Routine copy through the existing `save_routine` supersession machinery, so per-Member past-date reconstruction (§Attendance) works unchanged.
- New table `RoutinePreset` (`gym_id`, per-Gym-unique `name`, retirement marker) - identity only.
- The Preset's master structure is a **Member-less Routine row** (`member_id` nullable, `preset_id` set) reusing the existing Workout tables and save path; the master keeps its own superseded versions. Supersession for a master scopes by `preset_id` - a new master version supersedes only prior versions of that same Preset, never other masters sharing the NULL `member_id`. Accepted cost: "every Routine has a Member" stops being an invariant.
- The live link is a nullable `preset_id` on `Routine`; a direct edit forks by writing a stamp-less row. No provenance column - history carries the lineage.
- Gym default: `default_preset_id` on `Gym`, cleared when that Preset retires.
- Presets **retire, never delete** - Members keep their copies; copies are `coach_authored`, so the Agent never rewrites them.

**The ownership chip** ([#86](https://github.com/ivzc07/agentg/issues/86)): the Routine editor header always carries a chip - **Agent-managed**, **Preset: `<name>`**, or **Coach-authored** - with a one-line consequence in the two forkable states ("Saving makes this plan yours - the Agent will stop adjusting it").
No confirm dialog; the fork stays silent but is never a surprise.
After the first save the chip reads Coach-authored permanently, named ("Coach-authored - Luis") when the stamp survives ([#91](https://github.com/ivzc07/agentg/issues/91)).

## Safety flags

*Source: [#80](https://github.com/ivzc07/agentg/issues/80).*

A safety flag becomes a **Note of its own kind** - not a prefix match, not a new table:

- `safety` joins `NOTE_KINDS`; `flag_to_coach_action` writes `kind="safety"` with the bare summary. The roster filters on the kind.
- One new `acknowledged_at` column on `MemberNote` carries the handled state, plus `acknowledged_by_member_id` ([#91](https://github.com/ivzc07/agentg/issues/91)).
- **Acknowledging is not retiring**: ticking off silences the roster marker but leaves the fact live in the Agent's context - a retired knee-pain note would stop the Agent avoiding squats.
- **The consent ask is dropped**: `share_with_coach` disappears; the Agent logs the flag and pings the Gym's Coaches every time, with an authenticated deep link to the Member's page (§Access & identity).
- **Injuries never mark the roster** (`kind="injury"` stays quiet) - a standing fact, not an event; lit permanently, the marker would mean nothing.
- The flag is a **marker on the row, never a re-sort** - the Gap ordering holds.
- A flag clears when a Coach ticks it or **ages out 30 days after `created_at`** (computed, no job); an expired unacknowledged flag stays on the Member page labelled "expired, never seen".

## Settings

*Source: [#75](https://github.com/ivzc07/agentg/issues/75), amended by [#90](https://github.com/ivzc07/agentg/issues/90).*

**One tenant settings screen**, reachable by any coach-flagged Member, holding exactly three things:

- **The invite link** - `t.me/<bot>?start=<code>`, copyable, with a QR code, and **Regenerate** behind a typed confirm (`regenerate_invite_code`'s first production caller; the confirm is load-bearing because regenerating invalidates half-finished linking conversations).
- **The coach link** - copy and regenerate behind the same typed confirm, **no QR** (a private link forwarded to one person, not a poster).
- **The gym name** - the same `Gym.name` Members see when they join.

**No new settings.** `timezone` and `weight_unit` stay provisioning choices; Demos stay shipped defaults; the Rules doc stays chat-only ([#71](https://github.com/ivzc07/agentg/issues/71)); and there is **no Coach switch over a Member's `checkin_state`** - flipping a Member's snooze from the web makes the Agent break a promise made in the Member's own thread.

One correctness fix rides along ([#74](https://github.com/ivzc07/agentg/issues/74), [#75](https://github.com/ivzc07/agentg/issues/75)): day boundaries are computed in UTC today despite `Gym.timezone` existing - that is a **bug in Gap**, the roster's only sort key.
Gap gets fixed to honour the existing `Gym.timezone`; the field is not surfaced.

## Language

*Sources: [#76](https://github.com/ivzc07/agentg/issues/76), [#89](https://github.com/ivzc07/agentg/issues/89).*

**Per-browser EN/ES**, stored on no row:

- A toggle in the chrome, persisted in a long-lived cookie beside the session cookie; first visit defaults from `Accept-Language`, falling back to **Spanish** (the product's one no-signal default).
- Chrome, weekdays, months, relative time and the decimal mark translate.
- **Three things never translate**: Exercise names (`exercises.name` is the row), Workout names (free text), and the Member's own words in Notes and Set comments - the last carrying a small source-language tag.
- Chat stays fully independent: the Agent keeps mirroring the conversation, and bot-sent dashboard links (magic-link reply, safety-flag pings) follow the chat rule even when the dashboard renders in the other language.

## Data model changes (consolidated)

Everything the screens *show* is already recorded; the changes are new queries plus these rows and columns:

| Change | What | Source |
|---|---|---|
| `member_notes.kind` | `safety` joins `NOTE_KINDS`; prefix match dies | [#80](https://github.com/ivzc07/agentg/issues/80) |
| `member_notes.acknowledged_at` | flag handled state (tick-off); expiry computed from `created_at` | [#80](https://github.com/ivzc07/agentg/issues/80) |
| `member_notes.acknowledged_by_member_id` | nullable FK, who ticked | [#91](https://github.com/ivzc07/agentg/issues/91) |
| `routine_presets` table | `gym_id`, per-Gym-unique `name`, retirement marker | [#83](https://github.com/ivzc07/agentg/issues/83) |
| `routines.member_id` nullable + `routines.preset_id` | Member-less master rows; live link stamp | [#83](https://github.com/ivzc07/agentg/issues/83) |
| `routines.created_by_member_id` | nullable actor stamp; NULL = the Agent via chat | [#91](https://github.com/ivzc07/agentg/issues/91) |
| `gyms.default_preset_id` | one default slot, cleared on retire | [#83](https://github.com/ivzc07/agentg/issues/83) |
| `gyms.coach_invite_code` | second regenerable code, `coach-` prefix | [#90](https://github.com/ivzc07/agentg/issues/90) |
| `dashboard_login_tokens` table | token hash, member_id, gym_id, expires_at, used_at | [#79](https://github.com/ivzc07/agentg/issues/79) |
| index on `sets.exercise_id` | per-Exercise history reads currently scan | [#74](https://github.com/ivzc07/agentg/issues/74) |
| Gap honours `Gym.timezone` | fix UTC day boundaries in `TrainingStore.today()` / `RoutineStore._today()` | [#74](https://github.com/ivzc07/agentg/issues/74), [#75](https://github.com/ivzc07/agentg/issues/75) |

New **queries** (data already there): list a Gym's Members (joining `member_channels` to skip ghosts), Gap as one `GROUP BY`, paginated Session lists (today's collapsing logic is private), superseded Routines per date, retired Notes, weight-over-time reading `sets` directly (`exercise_history` carries no date).

Removed: `share_with_coach` on `flag_to_coach` ([#80](https://github.com/ivzc07/agentg/issues/80)).

## Stack, hosting & deployment

*Source: [#84](https://github.com/ivzc07/agentg/issues/84).*

**The dashboard lives inside the bot's process** - the same single Coolify application, one deploy, one container:

- **HTTP server**: aiohttp (already in the tree via aiogram), started on the existing asyncio event loop next to the long poller and APScheduler. A later polling-to-webhook switch reuses this same server.
- **Rendering**: server-rendered HTML from typed **Python f-string renderers** (no template engine) plus small vanilla JS snippets; new partial-page interactivity, when it lands, goes through **vendored htmx** returning HTML fragments from the same renderers (not yet shipped - ADR 0003 follow-up). No frontend build step, no SPA, no API layer ([ADR 0003](adr/0003-dashboard-stays-server-rendered.md)).
- **Public origin**: a subdomain of the flowstate domain attached in Coolify (automatic TLS). The exact hostname is a deploy-time detail behind a `DASHBOARD_BASE_URL` env var that `/dashboard` magic links point at.
- Delivery stays **long polling**, single replica; only the "no public endpoint" property of [docs/spec.md](spec.md) `§Channel plan` retires.

## In-place interactions (ADR 0003 cluster)

*Issues [#127](https://github.com/ivzc07/agentg/issues/127), [#128](https://github.com/ivzc07/agentg/issues/128), [#129](https://github.com/ivzc07/agentg/issues/129).*

The interaction upgrades the redesign deferred, inside ADR 0003's hard cap (HTML fragments from the same renderers - no client templating, no JSON endpoints, no build step):

- **Live roster numbers** (#127): while a search query filters, the chrome's "Members (N)" reads "X de N" (localized), back to the resting label on an empty box. Client-only - the vanilla search snippet, no htmx.
- **In-place Routine editor saves** (#128): the editor (Member and Preset master) posts through vendored htmx; with the `HX-Request` header the server returns the re-rendered editor - a success line naming the save and the notified Member, or today's exact refusals - so scroll and typed work survive. Without JS the POST/redirect flow stands unchanged.
- **Confirmation notices on the redirect writes** (#129): tick-off, preset create/apply/default/retire, and the Settings saves redirect with `?done=<key>`; the landing renderer turns a known key into a one-line notice, anything else renders nothing.

htmx ships vendored in `/static/` beside the stylesheet, on the same content-hash URL scheme.

## Out of scope for v1

- Editing the Rules doc from the web - stays a chat action until v2 ([#71](https://github.com/ivzc07/agentg/issues/71)).
- A Demos surface (upload, transcode, preview, browse) - a media product, not a field ([#75](https://github.com/ivzc07/agentg/issues/75)).
- Any message from the Coach to the Member through the web; nudging the Agent to check in early ([#71](https://github.com/ivzc07/agentg/issues/71)).
- An audit log; actor stamps on settings writes ([#91](https://github.com/ivzc07/agentg/issues/91)).
- A Gym owner/admin role distinct from Coach; in-product demotion ([#90](https://github.com/ivzc07/agentg/issues/90)).
- The dark visual language (`docs/prototypes/coach-dashboard-v3-dark.html`) - wanted, parked for a later effort ([#76](https://github.com/ivzc07/agentg/issues/76)).
- Coaches or Members spanning more than one Gym - the domain holds one Gym per Member and [#90](https://github.com/ivzc07/agentg/issues/90) treats another gym's coach link as the normal gym switch.
- Self-serve gym signup, plans and billing; member-facing web surfaces.

## Deferred build-time details

Not spec gaps - explicitly classified as build-time choices during resolution:

- Magic-link TTL value and token hashing scheme; verify Telegram's link-preview pre-burn behavior against a real bot ([#79](https://github.com/ivzc07/agentg/issues/79)).
- Exact column types and migration layout for the consolidated changes.
- The dashboard subdomain hostname; attaching the flowstate domain in Coolify is deploy-day work ([#84](https://github.com/ivzc07/agentg/issues/84)).
- How the flag marker and expired state are drawn ([#80](https://github.com/ivzc07/agentg/issues/80) defers to the adopted screens).

## Source index

| Ticket | Answer lands in |
|---|---|
| [#71 What jobs does a Coach do on the dashboard?](https://github.com/ivzc07/agentg/issues/71) | resolution comment |
| [#72 What may a Coach see about a Member, and how does the Member consent?](https://github.com/ivzc07/agentg/issues/72) | resolution comment |
| [#73 How does a Coach sign in to a web dashboard?](https://github.com/ivzc07/agentg/issues/73) | [docs/research/coach-dashboard-auth.md](research/coach-dashboard-auth.md) |
| [#74 What can the current data model answer about a Member's training?](https://github.com/ivzc07/agentg/issues/74) | [docs/research/dashboard-data-model.md](research/dashboard-data-model.md) |
| [#75 Which Gym and Coach settings become editable from the web?](https://github.com/ivzc07/agentg/issues/75) | resolution comment |
| [#76 Rough the member list and member detail screens](https://github.com/ivzc07/agentg/issues/76) | prototypes in [docs/prototypes/](prototypes/), reviews in [docs/design/coach-dashboard-reviews/](design/coach-dashboard-reviews/) |
| [#77 How does a web-written Routine reconcile with the Agent's chat writes?](https://github.com/ivzc07/agentg/issues/77) | resolution comment |
| [#78 One Routine per Member, or one Routine for many Members?](https://github.com/ivzc07/agentg/issues/78) | resolution comment |
| [#79 Is the dashboard a bookmarkable URL, or a door the bot opens?](https://github.com/ivzc07/agentg/issues/79) | resolution comment |
| [#80 What makes a safety flag identifiable on the roster?](https://github.com/ivzc07/agentg/issues/80) | resolution comment |
| [#81 Does the dashboard answer whether a Member followed their plan?](https://github.com/ivzc07/agentg/issues/81) | resolution comment |
| [#83 What is the data shape for a preset Routine shared across Members?](https://github.com/ivzc07/agentg/issues/83) | resolution comment |
| [#84 What stack, hosting and deployment shape does the web app take?](https://github.com/ivzc07/agentg/issues/84) | resolution comment |
| [#85 Does the roster's Gap colour follow the Member's own schedule?](https://github.com/ivzc07/agentg/issues/85) | resolution comment |
| [#86 Is the Coach warned before a web save forks a Routine away from the Agent?](https://github.com/ivzc07/agentg/issues/86) | resolution comment |
| [#87 How does a Coach find a named Member on the roster?](https://github.com/ivzc07/agentg/issues/87) | resolution comment |
| [#88 Where does a lapsed Member go on the roster?](https://github.com/ivzc07/agentg/issues/88) | resolution comment |
| [#89 Which language does the dashboard read, and where is it stored?](https://github.com/ivzc07/agentg/issues/89) | resolution comment |
| [#90 How does someone become a Coach?](https://github.com/ivzc07/agentg/issues/90) | resolution comment |
| [#91 Do dashboard writes record which Coach made them?](https://github.com/ivzc07/agentg/issues/91) | resolution comment |
| [#92 Do soft-retired Notes and departed Members appear on the dashboard?](https://github.com/ivzc07/agentg/issues/92) | resolution comment |
| [#93 Write the coach dashboard spec](https://github.com/ivzc07/agentg/issues/93) | this document |
