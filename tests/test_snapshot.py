"""The per-turn member snapshot injected via dynamic instructions."""

from datetime import timedelta

import pytest

from agentg.agent import dynamic_instructions
from conftest import FakeClock

from agentg.db import create_engine
from agentg.notes import NotesStore
from agentg.checkin_store import CheckinStore
from agentg.routines import RoutineStore
from agentg.snapshot import member_snapshot
from agentg.store import LinkingStore
from agentg.tools import MemberContext
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
        training=training,
        notes=notes,
        routines=routines,
        linking=linking,
        checkins=CheckinStore(engine),
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


async def test_dynamic_instructions_prepend_the_protocol_and_append_the_snapshot(env):
    class Wrapper:
        context = env.context

    text = await dynamic_instructions(Wrapper(), agent=None)

    assert "coach" in text.lower()  # the protocol is still there
    assert "Dani" in text  # and the snapshot rides along
