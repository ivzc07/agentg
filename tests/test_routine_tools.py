"""Routine tools + Session opener naming today's Workout (spec §Routine gen)."""

import pytest

from conftest import FakeClock

from agentg.agent import build_agent
from agentg.config import Settings
from agentg.db import create_engine
from agentg.notes import NotesStore
from agentg.checkin_store import CheckinStore
from agentg.demos import DemoStore
from agentg.forget import ForgetStore
from agentg.routines import DEFAULT_RULES_DOC, ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.snapshot import member_snapshot
from agentg.store import LinkingStore
from agentg.context import MemberContext
from agentg.stores import Stores
from agentg.tools import open_session_payload
from agentg.training import TrainingStore

ROUTINE_TOOLS = {"get_rules_doc", "list_exercises", "save_routine", "get_routine"}


@pytest.fixture
async def env(tmp_path):
    # A Monday so "today's workout" lands on the Push day of the seed routine.
    clock = FakeClock()  # 2026-07-15 is a Wednesday
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'rt.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    training = TrainingStore(engine, clock=clock)
    await training.ensure_seeded()
    notes = NotesStore(engine, clock=clock)
    routines = RoutineStore(engine, clock=clock)
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Dani", "telegram", "42")
    context = MemberContext(
        stores=Stores(
            linking=linking,
            training=training,
            notes=notes,
            routines=routines,
            checkins=CheckinStore(engine),
            demos=DemoStore(engine),
            forget=ForgetStore(engine),
        ),
        member_id=member.id,
        gym_id=gym.id,
        member_name="Dani",
        gym_name="Iron Temple",
        weight_unit="kg",
    )

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.context = context
    env.routines = routines
    env.member_id = member.id
    env.gym_id = gym.id
    yield env
    await engine.dispose()


def wednesday_push() -> list[WorkoutSpec]:
    # 2026-07-15 is a Wednesday (weekday 2).
    return [
        WorkoutSpec(
            weekday=2,
            name="Push",
            exercises=[ExerciseSpec("bench press", sets=3, reps="8-12")],
        )
    ]


def test_workout_input_rejects_an_out_of_range_weekday():
    import pytest as _pytest
    from pydantic import ValidationError

    from agentg.tools import WorkoutInput

    with _pytest.raises(ValidationError):
        WorkoutInput(weekday=7, name="Nope", exercises=[])
    with _pytest.raises(ValidationError):
        WorkoutInput(weekday=-1, name="Nope", exercises=[])


async def test_the_agent_carries_the_routine_tools():
    settings = Settings(
        telegram_bot_token="123:abc",
        model="openai/gpt-4o-mini",
        model_api_key="sk-test",
        database_url="sqlite+aiosqlite://",
    )
    names = {tool.name for tool in build_agent(settings).tools}
    assert ROUTINE_TOOLS <= names


async def test_the_effective_rules_doc_defaults_to_the_shipped_one(env):
    assert await env.context.stores.routines.effective_rules_doc(env.gym_id) == DEFAULT_RULES_DOC


async def test_the_catalog_is_available_to_draw_a_routine_from(env):
    names = await env.context.stores.training.catalog_names()
    assert "bench press" in names and "squat" in names


async def test_open_session_payload_names_todays_workout(env):
    await env.routines.save_routine(env.member_id, env.gym_id, wednesday_push())

    payload = await open_session_payload(env.context)

    assert payload["todays_workout"] is not None
    assert payload["todays_workout"]["name"] == "Push"


async def test_open_session_payload_without_a_routine_has_no_workout(env):
    payload = await open_session_payload(env.context)
    assert payload["todays_workout"] is None


async def test_snapshot_names_todays_workout_when_a_routine_exists(env):
    await env.routines.save_routine(env.member_id, env.gym_id, wednesday_push())
    snapshot = await member_snapshot(env.context)
    assert "Push" in snapshot


async def test_snapshot_without_a_routine_says_so(env):
    snapshot = await member_snapshot(env.context)
    assert "no routine" in snapshot.lower()
