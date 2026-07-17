"""RoutineStore: rules doc resolution and structured Routine/Workout rows.

Spec: docs/spec.md §Routine generation & coach overrides. Structure only —
Workouts pinned to weekdays, exercises from the catalog, never target weights.
"""

import pytest
from sqlalchemy import inspect, text

from agentg.db import create_engine
from agentg.routines import (
    DEFAULT_RULES_DOC,
    ExerciseSpec,
    RoutineStore,
    WorkoutSpec,
)
from agentg.store import LinkingStore
from agentg.training import TrainingStore


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'routines.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    training = TrainingStore(engine)
    await training.ensure_seeded()  # a catalog to draw from
    routines = RoutineStore(engine)
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Dani", "telegram", "42")

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.routines = routines
    env.member_id = member.id
    env.gym_id = gym.id
    yield env
    await engine.dispose()


def push_pull_legs() -> list[WorkoutSpec]:
    return [
        WorkoutSpec(
            weekday=0,
            name="Push",
            exercises=[
                ExerciseSpec("bench press", sets=3, reps="8-12"),
                ExerciseSpec("overhead press", sets=3, reps="8-12"),
                ExerciseSpec("dips", sets=3, reps="AMRAP"),
            ],
        ),
        WorkoutSpec(
            weekday=2,
            name="Pull",
            exercises=[ExerciseSpec("barbell row", sets=3, reps="8-12")],
        ),
        WorkoutSpec(
            weekday=4,
            name="Legs",
            exercises=[ExerciseSpec("squat", sets=3, reps="5")],
        ),
    ]


# --- rules doc: exactly one, gym's own or the default ---


async def test_a_gym_without_its_own_doc_gets_the_shipped_default(env):
    assert await env.routines.effective_rules_doc(env.gym_id) == DEFAULT_RULES_DOC


async def test_a_gym_with_its_own_doc_gets_it_and_never_the_default(env):
    await env.routines.set_rules_doc(env.gym_id, "Iron Temple rules: squats every day.")
    effective = await env.routines.effective_rules_doc(env.gym_id)
    assert effective == "Iron Temple rules: squats every day."
    assert effective != DEFAULT_RULES_DOC


async def test_changing_the_doc_changes_what_generation_would_read(env):
    await env.routines.set_rules_doc(env.gym_id, "v1")
    await env.routines.set_rules_doc(env.gym_id, "v2")  # no code change, just data
    assert await env.routines.effective_rules_doc(env.gym_id) == "v2"


# --- saving structured rows ---


async def test_save_routine_pins_workouts_to_weekdays_with_catalog_exercises(env):
    await env.routines.save_routine(env.member_id, env.gym_id, push_pull_legs())

    routine = await env.routines.active_routine(env.member_id)
    assert routine is not None
    by_day = {w["weekday"]: w for w in routine["workouts"]}
    assert set(by_day) == {0, 2, 4}
    assert by_day[0]["name"] == "Push"
    push = by_day[0]["exercises"]
    assert [e["exercise"] for e in push] == ["bench press", "overhead press", "dips"]
    assert push[0]["sets"] == 3 and push[0]["reps"] == "8-12"


async def test_no_target_weight_is_stored_anywhere_in_the_plan(env):
    await env.routines.save_routine(env.member_id, env.gym_id, push_pull_legs())
    async with env.engine.connect() as conn:
        cols = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(workout_exercises)"))).all()
        }
    assert not any("weight" in c.lower() for c in cols)  # structure only


async def test_exercise_aliases_resolve_to_the_catalog(env):
    await env.routines.save_routine(
        env.member_id,
        env.gym_id,
        [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench")])],
    )
    routine = await env.routines.active_routine(env.member_id)
    assert routine["workouts"][0]["exercises"][0]["exercise"] == "bench press"  # alias resolved


async def test_saving_a_new_routine_replaces_the_active_one(env):
    await env.routines.save_routine(env.member_id, env.gym_id, push_pull_legs())
    await env.routines.save_routine(
        env.member_id,
        env.gym_id,
        [WorkoutSpec(weekday=1, name="Full body", exercises=[ExerciseSpec("squat")])],
    )

    routine = await env.routines.active_routine(env.member_id)
    assert [w["weekday"] for w in routine["workouts"]] == [1]  # only the new plan is active
    async with env.engine.connect() as conn:  # the old routine row is kept, deactivated
        actives = (
            await conn.execute(text("SELECT count(*) FROM routines WHERE is_active = 1"))
        ).scalar()
    assert actives == 1


async def test_no_routine_yet_reads_back_as_none(env):
    assert await env.routines.active_routine(env.member_id) is None


# --- naming today's Workout (feeds the Session opener) ---


async def test_workout_for_weekday_returns_that_days_plan(env):
    await env.routines.save_routine(env.member_id, env.gym_id, push_pull_legs())
    workout = await env.routines.workout_for_weekday(env.member_id, 2)
    assert workout is not None and workout["name"] == "Pull"


async def test_a_rest_day_has_no_workout(env):
    await env.routines.save_routine(env.member_id, env.gym_id, push_pull_legs())
    assert await env.routines.workout_for_weekday(env.member_id, 6) is None  # Sunday: rest


async def test_workout_for_weekday_without_a_routine_is_none(env):
    assert await env.routines.workout_for_weekday(env.member_id, 0) is None


async def test_routines_are_scoped_per_member(env):
    other = await LinkingStore(env.engine).link_member(env.gym_id, "Ben", "telegram", "99")
    await env.routines.save_routine(env.member_id, env.gym_id, push_pull_legs())
    assert await env.routines.active_routine(other.id) is None
