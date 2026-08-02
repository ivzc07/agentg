"""Timezone stratum: Gap and "today" honour Gym.timezone end to end (#95).

Every eval puts the gym in America/Chicago (UTC-5 in July) and logs a
Session in the local evening that falls after UTC midnight — the case that
used to land on the wrong day. Assertions read only user-visible surfaces:
the facts the tools hand the Agent, the snapshot in the instructions, the
proactive sweep message, and end state via Stores.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from conftest import FakeClock

from agentg.checkin_sweep import run_sweep
from agentg.routines import ExerciseSpec, WorkoutSpec
from behavioral.harness import ConversationHarness, message, tool

CHICAGO = "America/Chicago"  # UTC-5 in July


def _tool_payloads(h: ConversationHarness) -> str:
    """Everything the tools returned to the model, as searchable text."""
    return " ".join(
        json.dumps(call["input"], default=str)
        for call in h.model.calls
        if isinstance(call["input"], list)
    )


def _snapshot(h: ConversationHarness) -> str:
    """The developer-message snapshot the model saw, injected via
    call_model_input_filter at the end of each model call's input (#175)."""
    parts: list[str] = []
    for call in h.model.calls:
        items = call["input"]
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("role") == "developer":
                parts.append(str(item.get("content", "")))
    return "\n".join(parts)


async def test_late_evening_session_counts_on_the_local_day_in_the_gap(tmp_path):
    clock = FakeClock(datetime(2026, 7, 14, 2, 0, tzinfo=UTC))  # Mon Jul 13, 21:00 local
    async with ConversationHarness.create(tmp_path, clock=clock) as h:
        await h.linked_member(timezone=CHICAGO)
        await h.say(
            "I'm here",
            steps=[tool("open_session"), message("Let's go.")],
        )
        await h.say(
            "bench 60 8,8,8",
            steps=[tool("log_sets", line="bench 60 8,8,8"), message("Logged.")],
        )
        await h.say("done", steps=[tool("close_session"), message("Done.")])

        # Wed Jul 15, 09:00 at the gym. Local gap: Jul 15 - Jul 13 = 2 days;
        # UTC math would call the visit Jul 14 and report a 1-day gap.
        clock.now = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
        await h.say(
            "I'm here",
            steps=[tool("open_session"), message("Welcome back.")],
        )

        # The opener hands the Agent the local-day gap and session date.
        payloads = _tool_payloads(h)
        assert "days_since_last_session': 2" in payloads
        assert "2026-07-13" in payloads
        # The snapshot the Agent speaks from agrees.
        assert "Last Session: 2 days ago (2026-07-13)" in _snapshot(h)
        # End state via Stores: same local-day gap.
        days, last = await h.stores.training.latest_session_info(h.member_id)
        assert days == 2
        assert last is not None and last["date"] == "2026-07-13"


async def test_snapshot_today_and_todays_workout_are_gym_local(tmp_path):
    # 02:00 UTC Jul 14 is still Monday Jul 13, 21:00 at the gym.
    clock = FakeClock(datetime(2026, 7, 14, 2, 0, tzinfo=UTC))
    async with ConversationHarness.create(tmp_path, clock=clock) as h:
        await h.linked_member(timezone=CHICAGO)
        await h.stores.routines.save_routine(
            h.member_id,
            h.gym_id,
            [
                WorkoutSpec(weekday=0, name="Piernas", exercises=[ExerciseSpec("squat", sets=3, reps="5")]),
                WorkoutSpec(weekday=1, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="5")]),
            ],
        )

        await h.say("hey", steps=[message("Hola.")])

        snapshot = _snapshot(h)
        assert "Today is 2026-07-13." in snapshot  # not the UTC Jul 14
        assert "Piernas" in snapshot  # Monday's Workout locally…
        assert "Push" not in snapshot  # …though UTC says Tuesday


async def test_sweep_fires_the_fallback_nudge_on_the_local_gap(tmp_path):
    clock = FakeClock(datetime(2026, 7, 14, 2, 0, tzinfo=UTC))  # Mon Jul 13, 21:00 local
    async with ConversationHarness.create(tmp_path, clock=clock) as h:
        await h.linked_member(timezone=CHICAGO)
        await h.say(
            "bench 60 5,5,5",
            steps=[
                tool("open_session"),
                tool("log_sets", line="bench 60 5,5,5"),
                message("Logged."),
            ],
        )
        await h.say("done", steps=[tool("close_session"), message("Done.")])

        # Thu Jul 16, 09:00 at the gym: a 3-day local gap → the fallback nudge
        # is due. UTC math would see a 2-day gap and stay silent.
        sent = await run_sweep(
            datetime(2026, 7, 16, 14, 0, tzinfo=UTC),
            h.stores.checkins,
            h.stores.training,
            h.stores.routines,
            h.notifier,
        )
        assert sent == 1
        channel, channel_user_id, text = h.notifier.sent[0]
        assert (channel, channel_user_id) == ("telegram", "42")
        assert "3 días" in text  # the local-day count, not the UTC 2

        # Sweep cadence is otherwise unchanged: the next day is capped.
        sent = await run_sweep(
            datetime(2026, 7, 17, 14, 0, tzinfo=UTC),
            h.stores.checkins,
            h.stores.training,
            h.stores.routines,
            h.notifier,
        )
        assert sent == 0


async def test_snapshot_not_persisted_to_session_history(tmp_path):
    """AC: the snapshot injected via call_model_input_filter is transient — it
    reaches the model but is never saved into the persisted session history, so
    stale snapshots cannot accumulate across turns (#175 criterion 3)."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        session = h.runtime.session_for_member(h.member_id)

        # Turn 1
        await h.say("hey", steps=[message("Hola.")])

        # Turn 2
        await h.say("I'm here", steps=[message("Let's go.")])

        # (a) The persisted session history contains no developer snapshot items.
        session_items = await session.get_items()
        developer_items = [
            item for item in session_items
            if isinstance(item, dict) and item.get("role") == "developer"
        ]
        assert len(developer_items) == 0, (
            f"session history must not contain snapshot items, "
            f"found {len(developer_items)}"
        )

        # (b) The latest model call's input contains exactly one developer
        # snapshot message (no accumulation, no omission).
        last_input = h.model.calls[-1]["input"]
        dev_messages = [
            item for item in last_input
            if isinstance(item, dict) and item.get("role") == "developer"
        ]
        assert len(dev_messages) == 1, (
            f"latest model call must have exactly 1 developer message, "
            f"found {len(dev_messages)}"
        )
        assert "Dani" in dev_messages[0]["content"]


async def test_gap_deload_advice_uses_the_local_day_count(tmp_path):
    # Session logged Tue Jun 30, 21:00 local (after UTC midnight).
    clock = FakeClock(datetime(2026, 7, 1, 2, 0, tzinfo=UTC))
    async with ConversationHarness.create(tmp_path, clock=clock) as h:
        await h.linked_member(timezone=CHICAGO)
        await h.stores.routines.save_routine(
            h.member_id,
            h.gym_id,
            [WorkoutSpec(weekday=4, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="5")])],
        )
        await h.say(
            "bench 60 8,8,8",
            steps=[
                tool("open_session"),
                tool("log_sets", line="bench 60 8,8,8"),
                message("Logged."),
            ],
        )
        await h.say("done", steps=[tool("close_session"), message("Done.")])

        # Fri Jul 10, 09:00 at the gym: a 10-day local gap → ease back. UTC
        # math would see 9 days and coach as if no deload were due.
        clock.now = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
        await h.say(
            "I'm here",
            steps=[
                tool("open_session"),
                tool("suggest_weights"),
                message("Ease back in today."),
            ],
        )

        payloads = _tool_payloads(h)
        assert "gap_deload" in payloads
        assert "10 days off" in payloads
