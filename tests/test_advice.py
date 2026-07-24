"""suggest_for_today: weight suggestions derived from logged Sets + the doc.

Integration over the real stores. The clock is injected so gaps are exact.
"""

from datetime import timedelta

import pytest
from sqlalchemy import text

from conftest import FakeClock

from agentg.advice import suggest_for_today
from agentg.db import create_engine
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.linking_store import LinkingStore
from agentg.training import TrainingStore


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'advice.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    clock = FakeClock()  # 2026-07-15, a Wednesday (weekday 2)
    training = TrainingStore(engine, clock=clock)
    await training.ensure_seeded()
    routines = RoutineStore(engine, clock=clock)
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Dani", "telegram", "42")
    # A Wednesday Workout: bench 3x8-12.
    await routines.save_routine(
        member.id,
        gym.id,
        [WorkoutSpec(weekday=2, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="8-12")])],
    )

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.training = training
    env.routines = routines
    env.clock = clock
    env.member_id = member.id
    env.gym_id = gym.id
    yield env
    await engine.dispose()


async def log_bench_session(env, weight, reps, *, day_offset):
    """Log a completed (closed) bench Session `day_offset` days before today."""
    env.clock.now = FakeClock().now - timedelta(days=day_offset)
    for rep in reps:
        await env.training.log_sets(env.member_id, env.gym_id, f"bench {weight} {rep}")
    await env.training.close_session(env.member_id)


async def bench(env):
    return {s.exercise: s for s in await suggest_for_today(env.training, env.routines, env.member_id, env.gym_id)}


async def test_all_sets_completed_suggests_the_increment(env):
    await log_bench_session(env, 80, [12, 12, 12], day_offset=3)  # 3x12 = top of 8-12
    env.clock.now = FakeClock().now  # back to today

    s = (await bench(env))["bench press"]
    assert s.action == "increment"
    assert s.suggested_weight == 82.5


async def test_missing_the_rep_target_holds(env):
    await log_bench_session(env, 80, [8, 8, 7], day_offset=3)  # not all at the top
    env.clock.now = FakeClock().now

    s = (await bench(env))["bench press"]
    assert s.action == "hold"
    assert s.suggested_weight == 80.0


async def test_a_stall_across_two_sessions_deloads(env):
    await log_bench_session(env, 80, [8, 8, 6], day_offset=8)
    await log_bench_session(env, 80, [8, 7, 6], day_offset=3)  # missed twice at 80
    env.clock.now = FakeClock().now

    s = (await bench(env))["bench press"]
    assert s.action == "deload"
    assert s.suggested_weight == 72.5


async def test_a_long_gap_eases_back(env):
    await log_bench_session(env, 80, [12, 12, 12], day_offset=20)  # 20 days ago
    env.clock.now = FakeClock().now

    s = (await bench(env))["bench press"]
    assert s.action == "gap_deload"
    assert s.suggested_weight == 72.5  # ~10% under 80, not +2.5


async def test_gap_ease_back_survives_opening_todays_session_first(env):
    # the Agent opens today's Session before asking for suggestions; the gap
    # must still be measured to the last *prior* Session, not collapse to 0.
    await log_bench_session(env, 80, [12, 12, 12], day_offset=20)
    env.clock.now = FakeClock().now
    await env.training.open_session(env.member_id, env.gym_id)

    s = (await bench(env))["bench press"]
    assert s.action == "gap_deload"
    assert s.suggested_weight == 72.5


async def test_an_unverifiable_scheme_holds_and_never_deloads(env):
    await env.routines.save_routine(
        env.member_id,
        env.gym_id,
        [WorkoutSpec(weekday=2, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="AMRAP")])],
    )
    await log_bench_session(env, 80, [10, 9, 8], day_offset=8)
    await log_bench_session(env, 80, [9, 8, 7], day_offset=3)  # two "misses" we can't verify
    env.clock.now = FakeClock().now

    s = (await bench(env))["bench press"]
    assert s.action == "hold"  # no spurious deload from an AMRAP scheme


async def test_editing_the_doc_changes_the_suggestion_without_code(env):
    await env.routines.set_rules_doc(
        env.gym_id, "## Progression\n- increment: 5\n"
    )
    await log_bench_session(env, 80, [12, 12, 12], day_offset=3)
    env.clock.now = FakeClock().now

    s = (await bench(env))["bench press"]
    assert s.suggested_weight == 85.0  # 80 + the doc's 5, not the default 2.5


async def test_suggestions_are_never_written_to_workout_rows(env):
    await log_bench_session(env, 80, [12, 12, 12], day_offset=3)
    env.clock.now = FakeClock().now

    await suggest_for_today(env.training, env.routines, env.member_id, env.gym_id)

    async with env.engine.connect() as conn:  # the plan is untouched by suggesting
        cols = [
            row[1] for row in (await conn.execute(text("PRAGMA table_info(workout_exercises)"))).all()
        ]
        rows = (await conn.execute(text("SELECT count(*) FROM workout_exercises"))).scalar()
    assert not any("weight" in c.lower() for c in cols)  # no weight column exists at all
    assert rows == 1  # still just the one prescribed bench, unchanged


async def test_no_history_yields_a_none_suggestion(env):
    s = (await bench(env))["bench press"]
    assert s.action == "none"
    assert s.suggested_weight is None


async def test_a_rest_day_has_no_suggestions(env):
    # move the clock to Sunday (a rest day in this routine)
    env.clock.now = FakeClock().now + timedelta(days=4)  # Wed + 4 = Sunday
    assert await suggest_for_today(env.training, env.routines, env.member_id, env.gym_id) == []
