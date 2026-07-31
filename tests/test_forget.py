"""Forget-me: a Member's hard delete across all three stores (spec §Privacy)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentg.dashboard_store import DashboardStore
from agentg.db import create_engine
from agentg.forget import ForgetStore
from agentg.models import (
    DashboardLoginToken,
    Member,
    MemberChannel,
    MemberNote,
    Routine,
    Session,
    Set,
    Workout,
    WorkoutExercise,
)
from agentg.notes import NotesStore
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.runtime import AgentRuntime
from agentg.linking_store import LinkingStore
from agentg.training import TrainingStore


@pytest.fixture
async def env(tmp_path):
    from agents.extensions.memory import SQLAlchemySession

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'forget.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    # The SDK session tables, as the app creates them at startup.
    await SQLAlchemySession("startup:schema", engine=engine, create_tables=True).get_items(limit=1)
    training = TrainingStore(engine)
    await training.ensure_seeded()
    routines = RoutineStore(engine)
    notes = NotesStore(engine)
    forget = ForgetStore(engine)
    gym = await linking.create_gym("Iron Temple")

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.linking = linking
    env.training = training
    env.routines = routines
    env.notes = notes
    env.forget = forget
    env.gym_id = gym.id
    yield env
    await engine.dispose()


async def populate(env, channel_user_id="42", name="Dani"):
    """A Member with a footprint in every store."""
    member = await env.linking.link_member(env.gym_id, name, "telegram", channel_user_id)
    await env.training.log_sets(member.id, env.gym_id, "bench 60 8,8,8")
    await env.training.close_session(member.id)
    await env.routines.save_routine(
        member.id,
        env.gym_id,
        [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="5")])],
    )
    await env.notes.remember(member.id, env.gym_id, "injury", "trick shoulder")
    return member


async def count(env, model, **where):
    async with async_sessionmaker(env.engine)() as db:
        query = select(func.count()).select_from(model)
        for col, val in where.items():
            query = query.where(getattr(model, col) == val)
        return await db.scalar(query)


async def test_forget_leaves_no_trace_in_any_store(env):
    member = await populate(env)

    await env.forget.forget_member(member.id)

    assert await count(env, Member, id=member.id) == 0
    assert await count(env, MemberChannel, member_id=member.id) == 0
    assert await count(env, Session, member_id=member.id) == 0
    assert await count(env, MemberNote, member_id=member.id) == 0
    assert await count(env, Routine, member_id=member.id) == 0
    # child rows go too
    assert await count(env, Set) == 0
    assert await count(env, Workout) == 0
    assert await count(env, WorkoutExercise) == 0


async def test_after_forget_the_channel_is_a_cold_start(env):
    member = await populate(env, channel_user_id="42")
    await env.forget.forget_member(member.id)
    # messaging the bot again resolves to nobody → linking dead-ends
    assert await env.linking.identity_for("telegram", "42") is None


async def test_forget_clears_the_sdk_conversation_history(env):
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([{"role": "user", "content": "my shoulder hurts"}])

    await env.forget.forget_member(member.id)

    assert await session.get_items() == []


async def test_forget_touches_only_the_asking_member(env):
    victim = await populate(env, channel_user_id="42", name="Dani")
    bystander = await populate(env, channel_user_id="7", name="Sam")

    await env.forget.forget_member(victim.id)

    assert await count(env, Member, id=bystander.id) == 1
    assert await count(env, Session, member_id=bystander.id) == 1
    assert await count(env, MemberNote, member_id=bystander.id) == 1
    assert await count(env, Routine, member_id=bystander.id) == 1
    assert await env.linking.identity_for("telegram", "7") is not None


async def test_forget_is_idempotent(env):
    member = await populate(env)
    await env.forget.forget_member(member.id)
    await env.forget.forget_member(member.id)  # a second call must not error
    assert await count(env, Member, id=member.id) == 0


# --- safety-flag and dashboard residue (issue #101, review on PR #120) ---


async def test_the_test_engine_enforces_foreign_keys(env):
    """SQLite only enforces FKs when asked; the fixtures ask, so a forget
    that leaves a dangling reference fails loudly here instead of only on
    Postgres in production."""
    async with async_sessionmaker(env.engine)() as db:
        db.add(
            MemberNote(
                gym_id=env.gym_id,
                member_id=999999,  # no such Member
                kind="other",
                text="ghost",
                created_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_forgetting_a_coach_clears_their_flag_acknowledgements(env):
    """A coach who ticked off a flag and is later forgotten must not block
    the delete: the flag stays acknowledged, but by nobody (NULL), not by a
    dangling reference."""
    member = await populate(env)
    coach = await env.linking.link_member(env.gym_id, "Coach Sam", "telegram", "7")
    await env.linking.set_coach(coach.id)
    safety = await env.notes.remember_safety(member.id, env.gym_id, "sharp knee pain")
    store = DashboardStore(env.engine)
    await store.acknowledge_flag(env.gym_id, member.id, safety.id, coach.id)

    await env.forget.forget_member(coach.id)

    remaining = await env.notes.active(member.id)
    flag = next(n for n in remaining if n.kind == "safety")
    assert flag.acknowledged_at is not None  # still ticked...
    assert flag.acknowledged_by_member_id is None  # ...by a coach who is gone
    assert await count(env, Member, id=coach.id) == 0


async def test_forgetting_a_member_deletes_their_dashboard_login_tokens(env):
    """Flag pings mint a login token per coach; those rows reference the
    Member and must die with them, residue-free."""
    coach = await env.linking.link_member(env.gym_id, "Coach Sam", "telegram", "7")
    await env.linking.set_coach(coach.id)
    store = DashboardStore(env.engine)
    # what a safety-flag ping mints for the coach (issue #101)
    await store.create_login_token(coach.id, env.gym_id, next_path="/members/1")

    await env.forget.forget_member(coach.id)

    assert await count(env, DashboardLoginToken, member_id=coach.id) == 0


async def test_messaging_after_forget_dead_ends_in_linking(env):
    from agentg.messages import IncomingMessage
    from agentg.linking import DEAD_END_INSTRUCTION, Linking
    from conftest import identity_phraser

    member = await populate(env, channel_user_id="42")
    await env.forget.forget_member(member.id)

    # a fresh linking sees no identity → the polite invite-code dead end
    linking = Linking(env.linking, identity_phraser)
    msg = IncomingMessage(channel="telegram", channel_user_id="42", text="hey again")
    linked = await env.linking.identity_for("telegram", "42")
    reply = await linking.handle(msg, linked)
    assert reply == DEAD_END_INSTRUCTION
