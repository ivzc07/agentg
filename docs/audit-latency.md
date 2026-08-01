# Latency & code audit — Telegram Agent reply path

Produced by a 4-agent parallel audit (hot-path trace, LLM layer, I/O layer, code quality),
cross-verified against the source. Read-only; no behaviour changed.

Baseline measured by the I/O agent (in-memory SQLite, SQL statement counter):

| Reply-path step | SQL statements |
|---|---|
| `linking.handle` on an ordinary message | 2 |
| `checkins.reset_rhythm` | 1 (+1 write) |
| `maybe_compact` full-history read | 1 (large) |
| `member_snapshot` | 7 |
| `open_session` tool | 7 |
| `suggest_weights` tool (6 exercises) | 38 |

A typical "I'm here" turn ≈ **60 sequential DB round-trips + 3 sequential LLM calls**,
with **zero feedback to the user** for the whole 3–7 s.

## Ranked actions (latency saved / effort)

| # | Action | File | Effort | Win |
|---|---|---|---|---|
| 1 | Typing indicator (`ChatActionSender.typing`) around the `reply_fn` await | `channels/telegram.py:77` | S | removes 100% of dead air (perceived) |
| 2 | Fold `suggest_weights` into `open_session`'s payload; reword prompt | `tools.py:56-67`, `agent.py:58` | S | −1 LLM round trip (~1–2.5 s) on arrival turns |
| 3 | `max_tokens` / `temperature` / **`timeout`** / `num_retries` on the model | `agent.py:171`, `compaction.py:106`, `linking.py:146` | S | bounds P99; today the litellm fallback is **600 s** |
| 4 | Short-circuit `_gym_for_code` on code shape before hitting the DB | `linking.py:248` | S | −2 queries on 100% of turns |
| 5 | `asyncio.gather` the 3 snapshot reads | `snapshot.py:31-34` | S | −2 serial waits before the first token |
| 6 | Defer `reset_rhythm` past the reply, or make it one conditional UPDATE | `runtime.py:99` | S | −1–2 queries before the LLM |
| 7 | Memoize the active Routine on `MemberContext` (loaded 3× per turn) | `snapshot.py:32`, `tools.py:65`, `advice.py:52` | S | −4–8 queries |
| 8 | Move compaction off the critical path + add a convergence guard | `runtime.py:101`, `compaction.py:63-86` | M | −2–6 s on compaction turns; kills a latent per-turn doubling |
| 9 | Batch `exercise_history` (N+1 → one `IN` query) | `advice.py:60`, `training.py:365-405` | M | 38 → ~4 statements |
| 10 | Defer `flag_to_coach` coach pings to `after_send` | `coaching.py:112-160` | S/M | −0.4–3 s on the worst turn (member reports pain) |
| 11 | Static system prompt + snapshot injected last via `call_model_input_filter` | `agent.py:155-159` | M | makes the prompt prefix cacheable (~0.2–0.9 s/turn) |
| 12 | Gate coach/intake tool schemas with `is_enabled` | `tools.py:455-478` | S | −~400 tok/call + fewer stray tool calls |
| 13 | Streaming replies (`Runner.run_streamed`) | `runtime.py:105` | M/L | −1–1.8 s perceived on every turn |
| 14 | Deterministic set-logging fast path (bypass the LLM) | `runtime.py`, `parsing.py:37-61` | M/L | −2 LLM calls on the most frequent message; needs product sign-off |

## Correctness findings (independent of latency)

**P1 — no timeout on any LLM call.** litellm 1.93 falls back to
`COMPLETION_HTTP_FALLBACK_SECONDS = 600`. `handle_message` holds a per-identity lock for
the whole turn (`runtime.py:89`), so one hung call wedges that member's chat for 10 minutes.
Fix: `extra_args={"_skip_mcp_handler": True, "timeout": 30, "num_retries": 1}`.

**P2 — a transient compaction failure kills a good message.** `runtime.py:101` awaits
`maybe_compact` unguarded; it makes a network LLM call. Wrap in try/except + log.

**P2 — compaction is not crash-safe.** `clear_session()` then `add_items()`
(`compaction.py:84-85`): a failure between them destroys the whole history, not just the
folded prefix. Write first, then delete, or use one transaction.

**P2 — forget-me leaves chat residue.** The SDK persists the turn's items *after*
`delete_my_data` returns, so the tool call + goodbye survive the wipe. Add
`context.forgotten` and `clear_session()` after the run.

**P2 — unvalidated coach progression numbers.** `stall_sessions: 0` turns every hold into a
deload; `deload_percent: 150` yields `suggested_weight = -50.0`, which the prompt tells the
Agent to state verbatim. Clamp in `parse_progression_rules`.

**P2 — one bad Member aborts the whole check-in sweep** for that hour
(`checkin_sweep.py:81-105` guards only `notifier.send`).

**P3** — duplicate previous-pinned-weekday logic; `show_demo` resolves twice; dead
`stores.dashboard is not None`; incomplete shutdown (`scheduler`, `bot.session`);
`ConfigError` bypassed on a bad `DASHBOARD_PORT`; `_open_session_row` missing `LIMIT 1`;
`edit_logged_sets` drops `rpe`/`note` on grown rows; unbounded `_locks` map (populated
pre-auth); `find_exercise` full-table scan per alias; missing indexes on `members.gym_id`
and `member_channels.member_id`; `main.py` has zero test coverage.

## What is already right

Transport adds no idle latency (aiogram long poll, `handle_as_tasks=True`); one engine and
one `Bot` per process, no per-request client creation; demo sends already deferred behind
the reply; `/dashboard` and the linking state machine already bypass the LLM; tracing
disabled; `_skip_mcp_handler` already avoids litellm's proxy import per tool call; no
sync-blocking call anywhere on the async chat path.

## Caveats

Wall-clock figures are estimates from measured token counts and standard `gpt-4o-mini`
throughput — no live model call was made. Instrument per-turn model-call count and total
latency first so the wins are confirmed rather than assumed.
