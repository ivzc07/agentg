"""Routine tools + Session opener naming today's Workout (spec §Routine gen)."""

import pytest

from conftest import FakeClock

from agentg.agent import build_agent
from agentg.config import Settings
from agentg.db import create_engine
from agentg.notes import NotesStore
from agentg.checkin_store import CheckinStore
from agentg.dashboard_store import DashboardStore
from agentg.demos import DemoStore
from agentg.forget import ForgetStore
from agentg.routines import DEFAULT_RULES_DOC, ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.snapshot import member_snapshot
from agentg.linking_store import LinkingStore
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
            dashboard=DashboardStore(engine),
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


async def test_open_session_payload_names_todays_workout_and_suggestions(env):
    await env.routines.save_routine(env.member_id, env.gym_id, wednesday_push())

    payload = await open_session_payload(env.context)

    assert payload["todays_workout"] is not None
    assert payload["todays_workout"]["name"] == "Push"
    # Suggestions are included in the payload (#171).
    assert "suggestions" in payload
    assert len(payload["suggestions"]) == 1
    assert payload["suggestions"][0]["exercise"] == "bench press"


async def test_open_session_payload_without_a_routine_has_no_workout(env):
    payload = await open_session_payload(env.context)
    assert payload["todays_workout"] is None
    # Suggestions are empty when there is no Workout today (#171).
    assert payload["suggestions"] == []


async def test_snapshot_names_todays_workout_when_a_routine_exists(env):
    await env.routines.save_routine(env.member_id, env.gym_id, wednesday_push())
    snapshot = await member_snapshot(env.context)
    assert "Push" in snapshot


async def test_snapshot_without_a_routine_says_so(env):
    snapshot = await member_snapshot(env.context)
    assert "no routine" in snapshot.lower()


class _CountingRoutineStore:
    """Wraps a RoutineStore to count active_routine calls (#162)."""

    def __init__(self, store: RoutineStore) -> None:
        self._store = store
        self.active_routine_calls = 0

    async def active_routine(self, member_id: int):
        self.active_routine_calls += 1
        return await self._store.active_routine(member_id)

    # Delegate everything else so the cache helper's duck-type access works.
    def __getattr__(self, name: str):
        return getattr(self._store, name)


async def test_active_routine_is_loaded_exactly_once_per_turn(env):
    """A full turn (snapshot + open_session with suggestions) loads
    the active Routine exactly once — the cache reuses it (#162, #171)."""
    await env.routines.save_routine(env.member_id, env.gym_id, wednesday_push())

    counter = _CountingRoutineStore(env.routines)
    context = MemberContext(
        stores=Stores(
            linking=env.context.stores.linking,
            training=env.context.stores.training,
            notes=env.context.stores.notes,
            routines=counter,
            checkins=env.context.stores.checkins,
            demos=env.context.stores.demos,
            forget=env.context.stores.forget,
            dashboard=env.context.stores.dashboard,
        ),
        member_id=env.member_id,
        gym_id=env.gym_id,
        member_name="Dani",
        gym_name="Iron Temple",
        weight_unit="kg",
    )

    # Simulate a full turn: snapshot first, then session opener (which now
    # includes suggestions inline — #171).
    snapshot = await member_snapshot(context)
    assert "Push" in snapshot

    payload = await open_session_payload(context)
    assert payload["todays_workout"] is not None
    assert payload["todays_workout"]["name"] == "Push"
    # Suggestions are folded into the opener payload.
    assert "suggestions" in payload
    assert len(payload["suggestions"]) == 1
    assert payload["suggestions"][0]["exercise"] == "bench press"

    # The active Routine was loaded exactly once — the cache fed
    # both the snapshot and the session opener with suggestions.
    assert counter.active_routine_calls == 1


async def test_no_routine_loads_exactly_once_per_turn(env):
    """When a Member has no Routine, a full turn still queries active_routine
    exactly once — cached None avoids a re-query via the sentinel (#162)."""
    counter = _CountingRoutineStore(env.routines)
    context = MemberContext(
        stores=Stores(
            linking=env.context.stores.linking,
            training=env.context.stores.training,
            notes=env.context.stores.notes,
            routines=counter,
            checkins=env.context.stores.checkins,
            demos=env.context.stores.demos,
            forget=env.context.stores.forget,
            dashboard=env.context.stores.dashboard,
        ),
        member_id=env.member_id,
        gym_id=env.gym_id,
        member_name="Dani",
        gym_name="Iron Temple",
        weight_unit="kg",
    )

    # Full turn: snapshot then session opener (which now includes
    # suggestions inline — #171).
    snapshot = await member_snapshot(context)
    assert "no routine" in snapshot.lower()

    payload = await open_session_payload(context)
    assert payload["todays_workout"] is None
    # Suggestions are folded into the opener payload (#171).
    assert payload["suggestions"] == []

    # Exactly one DB load — cached None fed the rest, no fallback re-query.
    assert counter.active_routine_calls == 1


async def test_the_cache_lives_exactly_one_turn(env):
    """A fresh MemberContext (a new turn) reloads the Routine from the DB,
    and a Routine edited between turns shows the new content (#162)."""
    await env.routines.save_routine(env.member_id, env.gym_id, wednesday_push())

    counter = _CountingRoutineStore(env.routines)

    # First turn.
    ctx1 = MemberContext(
        stores=Stores(
            linking=env.context.stores.linking,
            training=env.context.stores.training,
            notes=env.context.stores.notes,
            routines=counter,
            checkins=env.context.stores.checkins,
            demos=env.context.stores.demos,
            forget=env.context.stores.forget,
            dashboard=env.context.stores.dashboard,
        ),
        member_id=env.member_id,
        gym_id=env.gym_id,
        member_name="Dani",
        gym_name="Iron Temple",
        weight_unit="kg",
    )
    snapshot1 = await member_snapshot(ctx1)
    assert "Push" in snapshot1
    assert counter.active_routine_calls == 1

    # Edit the Routine between turns — save a Pull workout instead.
    await env.routines.save_routine(
        env.member_id,
        env.gym_id,
        [
            WorkoutSpec(
                weekday=2,
                name="Pull",
                exercises=[ExerciseSpec("deadlift", sets=1, reps="5")],
            )
        ],
    )

    # Second turn with a fresh context — should re-query and see the new name.
    ctx2 = MemberContext(
        stores=ctx1.stores,
        member_id=env.member_id,
        gym_id=env.gym_id,
        member_name="Dani",
        gym_name="Iron Temple",
        weight_unit="kg",
    )
    snapshot2 = await member_snapshot(ctx2)
    assert "Pull" in snapshot2
    assert "Push" not in snapshot2
    assert counter.active_routine_calls == 2
