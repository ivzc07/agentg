"""Coach overrides at the store layer (spec §Routine gen & coach overrides)."""

import pytest

from agentg.db import create_engine
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.store import LinkingStore
from agentg.training import TrainingStore


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'coach.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    training = TrainingStore(engine)
    await training.ensure_seeded()
    routines = RoutineStore(engine)
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Dani", "telegram", "42")

    class Env:
        pass

    env = Env()
    env.linking = linking
    env.routines = routines
    env.gym_id = gym.id
    env.member_id = member.id
    yield env
    await engine.dispose()


def push() -> list[WorkoutSpec]:
    return [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="5")])]


# --- member lookup, gym-scoped ---


async def test_members_are_found_by_name_within_the_gym(env):
    matches = await env.linking.members_by_name(env.gym_id, "  dani ")  # case/space forgiving
    assert [m.id for m in matches] == [env.member_id]


async def test_an_unknown_name_matches_nobody(env):
    assert await env.linking.members_by_name(env.gym_id, "nobody") == []


async def test_name_matching_collapses_whitespace_on_both_sides(env):
    spaced = await env.linking.link_member(env.gym_id, "Ana  Lee", "telegram", "55")
    matches = await env.linking.members_by_name(env.gym_id, "ana lee")  # single space, lower
    assert [m.id for m in matches] == [spaced.id]


async def test_a_duplicate_name_returns_every_match(env):
    twin = await env.linking.link_member(env.gym_id, "Dani", "telegram", "77")
    matches = await env.linking.members_by_name(env.gym_id, "Dani")
    assert {m.id for m in matches} == {env.member_id, twin.id}


async def test_member_lookup_does_not_cross_gyms(env):
    other_gym = await env.linking.create_gym("Steel Yard")
    outsider = await env.linking.link_member(other_gym.id, "Sam", "telegram", "88")
    assert await env.linking.member_in_gym(env.gym_id, outsider.id) is None
    assert await env.linking.members_by_name(env.gym_id, "Sam") == []


# --- coach-authored routines ---


async def test_a_coach_authored_routine_is_flagged(env):
    await env.routines.save_routine(env.member_id, env.gym_id, push(), coach_authored=True)
    routine = await env.routines.active_routine(env.member_id)
    assert routine["coach_authored"] is True


async def test_generation_refuses_to_overwrite_a_coach_routine(env):
    await env.routines.save_routine(env.member_id, env.gym_id, push(), coach_authored=True)
    with pytest.raises(ValueError, match="coach-written"):
        await env.routines.save_routine(env.member_id, env.gym_id, push())  # coach_authored=False


async def test_a_coach_may_replace_a_coach_routine(env):
    await env.routines.save_routine(env.member_id, env.gym_id, push(), coach_authored=True)
    # a coach re-writing it is allowed and stays coach-authored
    await env.routines.save_routine(
        env.member_id,
        env.gym_id,
        [WorkoutSpec(weekday=1, name="Legs", exercises=[ExerciseSpec("squat", sets=5, reps="5")])],
        coach_authored=True,
    )
    routine = await env.routines.active_routine(env.member_id)
    assert [w["name"] for w in routine["workouts"]] == ["Legs"]
    assert routine["coach_authored"] is True
