"""The per-turn member snapshot — injected at the end of model input via
call_model_input_filter (#175) so the system prompt prefix is cacheable."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentg.agent import dynamic_instructions
from agentg.runtime import _inject_snapshot
from conftest import FakeClock

from agentg.db import create_engine
from agentg.notes import NotesStore
from agentg.checkin_store import CheckinStore
from agentg.dashboard_store import DashboardStore
from agentg.demos import DemoStore
from agentg.forget import ForgetStore
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.safety_outbox import SafetyOutbox
from agentg.snapshot import member_snapshot
from agentg.linking_store import LinkingStore
from agentg.context import MemberContext
from agents.run_config import CallModelData, ModelInputData
from agentg.stores import Stores
from agentg.training import TrainingStore


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'snap.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    clock = FakeClock()
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
            safety_outbox=SafetyOutbox(engine),
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
    env.clock = clock
    env.context = context
    env.training = training
    env.notes = notes
    env.member_id = member.id
    env.gym_id = gym.id
    yield env
    await engine.dispose()


async def test_snapshot_carries_identity_gap_headline_and_notes(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,7")
    await env.training.close_session(env.member_id)
    note = await env.notes.remember(env.member_id, env.gym_id, "injury", "shoulder's acting up")
    env.clock.advance(timedelta(days=2))

    snapshot = await member_snapshot(env.context)

    assert "Dani" in snapshot and "Iron Temple" in snapshot  # identity
    assert "2 day" in snapshot  # gap
    assert "bench press" in snapshot and "8/8/7" in snapshot and "60" in snapshot  # headline
    assert "shoulder's acting up" in snapshot  # active note
    assert f"#{note.id}" in snapshot  # id the Agent can retire by
    assert "kg" in snapshot


async def test_snapshot_before_any_session_or_note(env):
    snapshot = await member_snapshot(env.context)
    assert "Dani" in snapshot
    assert "no sessions" in snapshot.lower()
    assert "no active notes" in snapshot.lower()


async def test_snapshot_today_honours_the_gyms_timezone(env):
    env.clock.now = datetime(2026, 7, 14, 2, 0, tzinfo=UTC)  # Jul 13, 21:00 in Chicago
    await env.context.stores.routines.save_routine(
        env.member_id,
        env.gym_id,
        [
            WorkoutSpec(weekday=0, name="Piernas", exercises=[ExerciseSpec("squat", sets=3, reps="5")]),
            WorkoutSpec(weekday=1, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="5")]),
        ],
    )
    context = replace(env.context, timezone="America/Chicago")  # UTC-5 in July

    snapshot = await member_snapshot(context)

    assert "Today is 2026-07-13." in snapshot  # not the UTC Jul 14
    assert "Piernas" in snapshot  # Monday's Workout locally, though UTC says Tuesday
    assert "Push" not in snapshot


async def test_retired_notes_leave_the_snapshot(env):
    note = await env.notes.remember(env.member_id, env.gym_id, "injury", "shoulder pain")
    await env.notes.retire(env.member_id, note.id)
    snapshot = await member_snapshot(env.context)
    assert "shoulder pain" not in snapshot


async def test_snapshot_stays_a_few_hundred_tokens(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    await env.training.log_sets(env.member_id, env.gym_id, "dips 10,10,9")
    await env.training.close_session(env.member_id)
    for i in range(30):  # far more notes than a member realistically has
        await env.notes.remember(env.member_id, env.gym_id, "other", f"note number {i} " + "x" * 80)

    snapshot = await member_snapshot(env.context)

    assert len(snapshot) < 2500  # ~ a few hundred tokens


async def test_dynamic_instructions_are_static_no_snapshot(env):
    """The system prompt is identical across turns for a given Member role.

    The snapshot is injected via call_model_input_filter at the end of the
    model input, not appended to the instructions — so the prompt prefix
    stays cacheable (#175).
    """
    class Wrapper:
        context = env.context

    text = await dynamic_instructions(Wrapper(), agent=None)

    # The protocol is present.
    assert "coach" in text.lower()
    # But the snapshot is NOT — it's injected by the filter instead.
    assert "Dani" not in text
    assert "Iron Temple" not in text


async def test_notes_sit_at_the_attention_favored_edge_of_the_snapshot(env):
    """Notes ride at the tail of the snapshot (end of model input), not mid-context."""
    await env.notes.remember(env.member_id, env.gym_id, "injury", "left shoulder impingement")

    text = await member_snapshot(env.context)

    # Facts first, notes last — attention-favored edge.
    assert text.index("Today is") < text.index("left shoulder impingement")
    assert text.rstrip().endswith("left shoulder impingement")


async def test_independent_reads_run_concurrently(env):
    """The two pure-read operations (routine load, notes query) overlap.

    latest_session_info runs serial because it may write — only the two
    pure reads are gathered.  This test proves they overlapped and fails
    if they are reverted to serial."""
    import asyncio

    events: list[str] = []

    original_get = env.context.turn_cache.get_or_load_routine
    original_active = env.context.stores.notes.active

    async def tracked_get(*args, **kwargs):
        events.append("routine_start")
        await asyncio.sleep(0.005)
        result = await original_get(*args, **kwargs)
        events.append("routine_end")
        return result

    async def tracked_active(*args, **kwargs):
        events.append("notes_start")
        await asyncio.sleep(0.005)
        result = await original_active(*args, **kwargs)
        events.append("notes_end")
        return result

    env.context.turn_cache.get_or_load_routine = tracked_get  # type: ignore[method-assign]
    env.context.stores.notes.active = tracked_active  # type: ignore[method-assign]

    await member_snapshot(env.context)

    # The two tracked reads overlap: each started before the other finished.
    routine_start = events.index("routine_start")
    routine_end = events.index("routine_end")
    notes_start = events.index("notes_start")
    notes_end = events.index("notes_end")

    assert routine_start < notes_end, "routine started before notes finished"
    assert notes_start < routine_end, "notes started before routine finished"


async def test_snapshot_injected_as_developer_message_at_end_of_input(env):
    """The call_model_input_filter appends the snapshot as a developer
    message at the end of the input items, without modifying the static
    instructions."""
    snapshot = await member_snapshot(env.context)
    model_data = ModelInputData(
        input=[{"role": "user", "content": "hey", "type": "message"}],
        instructions="static instructions",
    )
    data = CallModelData(
        model_data=model_data,
        agent=None,
        context=env.context,
    )

    result = await _inject_snapshot(data)

    # The static instructions are unchanged.
    assert result.instructions == "static instructions"
    # The original input item is still there.
    assert len(result.input) == 2
    assert result.input[0]["role"] == "user"
    # The snapshot is appended as a developer message.
    assert result.input[1]["role"] == "developer"
    assert "Dani" in result.input[1]["content"]
    assert "Iron Temple" in result.input[1]["content"]


async def test_module_docstring_reflects_input_filter_mechanism():
    """The snapshot module docstring must describe the call_model_input_filter
    mechanism, not the old dynamic-instructions path."""
    import agentg.snapshot as snap
    doc = snap.__doc__ or ""
    assert "call_model_input_filter" in doc
    assert "dynamic instructions" not in doc


async def test_filter_passes_through_when_context_is_none(env):
    """When no MemberContext is available, the filter is a no-op."""
    model_data = ModelInputData(
        input=[{"role": "user", "content": "hello", "type": "message"}],
        instructions="generic prompt",
    )
    data = CallModelData(
        model_data=model_data,
        agent=None,
        context=None,
    )

    result = await _inject_snapshot(data)

    assert result is model_data  # returned unchanged
