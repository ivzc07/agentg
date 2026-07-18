"""The check-in sweep end to end over the real stores, with a fake notifier."""

from datetime import UTC, date, datetime, timedelta

import pytest

from agentg.checkin_store import CheckinStore
from agentg.checkin_sweep import run_sweep
from agentg.db import create_engine
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.store import LinkingStore
from agentg.training import TrainingStore


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, channel, channel_user_id, text):
        self.sent.append((channel, channel_user_id, text))


class SettableClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'sweep.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    clock = SettableClock(datetime(2026, 7, 16, 0, 0, tzinfo=UTC))
    training = TrainingStore(engine, clock=clock)
    await training.ensure_seeded()
    routines = RoutineStore(engine, clock=clock)
    checkins = CheckinStore(engine)

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.linking = linking
    env.training = training
    env.routines = routines
    env.checkins = checkins
    env.clock = clock
    yield env
    await engine.dispose()


async def make_member(env, tz="UTC", channel_user_id="42", signup=date(2026, 7, 1)):
    gym = await env.linking.create_gym("Iron Temple", timezone=tz)
    member = await env.linking.link_member(gym.id, "Dani", "telegram", channel_user_id)
    # created_at is a DB-side default; backdate it so the gap math is deterministic.
    async with env.checkins._sessions() as db:
        from agentg.models import Member

        m = await db.get(Member, member.id)
        m.created_at = datetime(signup.year, signup.month, signup.day, tzinfo=UTC)
        await db.commit()
    return gym, member


async def sweep(env, now_utc):
    notifier = FakeNotifier()
    count = await run_sweep(now_utc, env.checkins, env.training, env.routines, notifier)
    return count, notifier


# --- fallback member, 3-day gap, at 09:00 local ---


async def test_a_fallback_member_is_nudged_at_9am_local(env):
    gym, member = await make_member(env, tz="America/Chicago")  # UTC-5 in July
    # 14:00 UTC == 09:00 America/Chicago; last activity is signup 2026-07-01
    count, notifier = await sweep(env, datetime(2026, 7, 16, 14, 0, tzinfo=UTC))
    assert count == 1
    assert notifier.sent[0][1] == "42"
    assert "?" in notifier.sent[0][2]


async def test_no_send_outside_the_9am_local_hour(env):
    await make_member(env, tz="America/Chicago")
    count, notifier = await sweep(env, datetime(2026, 7, 16, 3, 0, tzinfo=UTC))  # 22:00 local
    assert count == 0 and notifier.sent == []


async def test_the_send_respects_each_gyms_timezone(env):
    # a UTC gym at 09:00 UTC gets nudged; the Chicago member (04:00 local) does not
    await make_member(env, tz="UTC", channel_user_id="1")
    await make_member(env, tz="America/Chicago", channel_user_id="2")
    count, notifier = await sweep(env, datetime(2026, 7, 16, 9, 0, tzinfo=UTC))
    assert count == 1
    assert {s[1] for s in notifier.sent} == {"1"}


# --- recording and the frequency cap across days ---


async def test_a_nudge_is_recorded_and_not_repeated_next_day(env):
    await make_member(env, tz="UTC")
    await sweep(env, datetime(2026, 7, 16, 9, 0, tzinfo=UTC))  # nudged
    count, notifier = await sweep(env, datetime(2026, 7, 17, 9, 0, tzinfo=UTC))  # consecutive day
    assert count == 0 and notifier.sent == []


async def test_a_reply_resets_the_rhythm_and_revives_a_lapsed_member(env):
    _, member = await make_member(env, tz="UTC")
    await env.checkins.lapse(member.id)
    await env.checkins.reset_rhythm(member.id)  # the runtime calls this on any inbound message
    state, _ = await env.checkins.get_state(member.id)
    assert state == "on"


# --- routine member: missed pinned day ---


async def test_a_routine_member_is_nudged_on_the_next_pinned_day(env):
    gym, member = await make_member(env, tz="UTC")
    # pin Wed (2); today 2026-07-15 is Wed. No sessions since signup → missed.
    await env.routines.save_routine(
        member.id,
        gym.id,
        [WorkoutSpec(weekday=2, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="5")])],
    )
    count, notifier = await sweep(env, datetime(2026, 7, 15, 9, 0, tzinfo=UTC))
    assert count == 1
    assert "Push" in notifier.sent[0][2]  # names today's workout warmly


async def test_a_routine_member_who_trained_the_pinned_day_is_left_alone(env):
    gym, member = await make_member(env, tz="UTC")
    await env.routines.save_routine(
        member.id,
        gym.id,
        [WorkoutSpec(weekday=2, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="5")])],
    )
    # log a Session today (Wed) so the pinned day is not missed
    env.clock.now = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
    await env.training.log_sets(member.id, gym.id, "bench 60 5,5,5")
    count, _ = await sweep(env, datetime(2026, 7, 15, 9, 0, tzinfo=UTC))
    assert count == 0


# --- wind-down / lapse ---


async def test_an_ignored_member_winds_down_and_is_flagged_lapsed(env):
    gym, member = await make_member(env, tz="UTC")
    # simulate 4 prior ignored nudges, last one not yesterday
    async with env.checkins._sessions() as db:  # arrange state directly
        from agentg.models import Member

        m = await db.get(Member, member.id)
        m.ignored_nudges = 4
        m.last_nudge_on = date(2026, 7, 13)  # Mon, not consecutive to Thu 16
        m.nudges_this_week = 1
        await db.commit()

    count, notifier = await sweep(env, datetime(2026, 7, 16, 9, 0, tzinfo=UTC))

    assert count == 1
    assert "cuando" in notifier.sent[0][2].lower()  # the wind-down copy (Spanish)
    state, _ = await env.checkins.get_state(member.id)
    assert state == "lapsed"
    # and now silent
    count2, _ = await sweep(env, datetime(2026, 7, 20, 9, 0, tzinfo=UTC))
    assert count2 == 0


# --- opt-out states ---


async def test_an_off_member_gets_nothing(env):
    _, member = await make_member(env, tz="UTC")
    await env.checkins.turn_off(member.id)
    count, _ = await sweep(env, datetime(2026, 7, 16, 9, 0, tzinfo=UTC))
    assert count == 0


async def test_a_snoozed_member_is_quiet_then_wakes_when_the_snooze_passes(env):
    _, member = await make_member(env, tz="UTC")
    await env.checkins.snooze_until(member.id, date(2026, 7, 18))

    quiet, _ = await sweep(env, datetime(2026, 7, 16, 9, 0, tzinfo=UTC))
    assert quiet == 0

    count, _ = await sweep(env, datetime(2026, 7, 20, 9, 0, tzinfo=UTC))  # after the snooze
    assert count == 1
    state, until = await env.checkins.get_state(member.id)
    assert state == "on" and until is None  # snooze cleared
