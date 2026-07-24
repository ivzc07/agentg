"""Coach-tool actions: the is_coach gate, rules-doc edit, hand-written Routine."""

import pytest

from agentg.db import create_engine
from agentg.notes import NotesStore
from agentg.checkin_store import CheckinStore
from agentg.demos import DemoStore
from agentg.forget import ForgetStore
from agentg.routines import DEFAULT_RULES_DOC, ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.coaching import update_rules_doc_action, write_routine_action
from agentg.context import MemberContext
from agentg.linking_store import LinkingStore
from agentg.stores import Stores
from agentg.tools import build_tools
from agentg.training import TrainingStore


async def make_context(engine, *, is_coach):
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    training = TrainingStore(engine)
    await training.ensure_seeded()
    routines = RoutineStore(engine)
    notes = NotesStore(engine)
    gym = await linking.create_gym("Iron Temple")
    actor = await linking.link_member(gym.id, "Coach Sam" if is_coach else "Dani", "telegram", "1")
    if is_coach:
        await linking.set_coach(actor.id)
    member = await linking.link_member(gym.id, "Ana", "telegram", "2")
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
        member_id=actor.id,
        gym_id=gym.id,
        member_name=actor.name,
        gym_name="Iron Temple",
        weight_unit="kg",
        is_coach=is_coach,
    )
    return context, gym, member


@pytest.fixture
async def engine(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'ct.db'}")
    yield engine
    await engine.dispose()


def bench_plan() -> list[WorkoutSpec]:
    return [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="5")])]


# --- the gate (AC: non-coaches cannot reach coach tools) ---


async def test_a_non_coach_cannot_edit_the_rules_doc(engine):
    context, gym, _ = await make_context(engine, is_coach=False)
    result = await update_rules_doc_action(context, "hacked rules")
    assert "error" in result
    assert await context.stores.routines.effective_rules_doc(gym.id) == DEFAULT_RULES_DOC  # unchanged


async def test_a_non_coach_cannot_write_a_routine(engine):
    context, _, member = await make_context(engine, is_coach=False)
    result = await write_routine_action(context, "Ana", None, bench_plan())
    assert "error" in result
    assert await context.stores.routines.active_routine(member.id) is None  # nothing saved


# --- rules-doc editing (AC: preview→confirm→save; default gym gets its copy) ---


async def test_a_coach_edits_the_rules_doc_and_the_gym_gets_its_own_copy(engine):
    context, gym, _ = await make_context(engine, is_coach=True)
    assert await context.stores.routines.effective_rules_doc(gym.id) == DEFAULT_RULES_DOC  # was default

    result = await update_rules_doc_action(context, "Iron Temple: squats daily. increment: 5")

    assert result["saved"] is True
    effective = await context.stores.routines.effective_rules_doc(gym.id)
    assert "squats daily" in effective
    assert effective != DEFAULT_RULES_DOC  # its own copy now


# --- hand-written routine (AC: coach writes for a Member, coach-authored, delivered) ---


async def test_a_coach_hand_writes_a_routine_for_a_member(engine):
    context, _, member = await make_context(engine, is_coach=True)

    result = await write_routine_action(context, "Ana", None, bench_plan())

    assert result["coach_authored"] is True
    assert result["member"] == "Ana"
    routine = await context.stores.routines.active_routine(member.id)  # delivered to Ana
    assert routine is not None and routine["coach_authored"] is True
    assert routine["workouts"][0]["name"] == "Push"


async def test_writing_for_an_unknown_member_errors(engine):
    context, _, _ = await make_context(engine, is_coach=True)
    result = await write_routine_action(context, "Nobody", None, bench_plan())
    assert "error" in result and "Nobody" in result["error"]


async def test_an_ambiguous_name_asks_for_a_member_id(engine):
    context, gym, member = await make_context(engine, is_coach=True)
    twin = await context.stores.linking.link_member(gym.id, "Ana", "telegram", "3")  # a second Ana

    result = await write_routine_action(context, "Ana", None, bench_plan())
    assert "error" in result
    assert str(member.id) in result["error"] and str(twin.id) in result["error"]

    # disambiguating by id works
    ok = await write_routine_action(context, "Ana", twin.id, bench_plan())
    assert ok["member_id"] == twin.id


async def test_a_coach_cannot_write_for_a_member_of_another_gym(engine):
    context, _, _ = await make_context(engine, is_coach=True)
    other_gym = await context.stores.linking.create_gym("Steel Yard")
    outsider = await context.stores.linking.link_member(other_gym.id, "Rex", "telegram", "9")

    result = await write_routine_action(context, "Rex", outsider.id, bench_plan())
    assert "error" in result
    assert await context.stores.routines.active_routine(outsider.id) is None


def test_the_coach_tools_are_registered_on_the_agent():
    names = {tool.name for tool in build_tools()}
    assert {"update_rules_doc", "write_routine"} <= names
