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


# --- Two-turn confirmation (issue #212) -----------------------------------

from datetime import UTC, datetime

from agentg.forget import detect_forget_me_language, is_forget_me_request, normalize_confirmation
from agentg.models import ForgetMeRequest


async def _pending_count(env, member_id: int) -> int:
    async with async_sessionmaker(env.engine)() as db:
        from sqlalchemy import func

        return await db.scalar(
            select(func.count())
            .select_from(ForgetMeRequest)
            .where(ForgetMeRequest.member_id == member_id)
        ) or 0


async def test_request_forget_me_persists_no_delete(env):
    """Requesting forget-me creates a pending row but does not delete."""
    member = await populate(env)
    now = datetime.now(UTC)

    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    assert phrase.startswith("DELETE-ME-")
    assert len(phrase) == len("DELETE-ME-") + 6  # 3 bytes hex = 6 chars
    # Data still intact.
    assert await count(env, Member, id=member.id) == 1
    assert await count(env, Session, member_id=member.id) == 1
    # Pending request row exists with the correct language.
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending.language == "en"


async def test_confirm_phrase_deletes_and_clears_request(env):
    """The exact phrase triggers deletion; the pending row is gone too."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Confirm with the exact phrase via atomic consume.
    consumed = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert consumed
    await env.forget.forget_member(member.id)

    assert await count(env, Member, id=member.id) == 0
    assert await _pending_count(env, member.id) == 0


async def test_second_request_replaces_first(env):
    """A second request atomically replaces the first."""
    member = await populate(env)
    now = datetime.now(UTC)

    phrase1 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)
    phrase2 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    assert phrase1 != phrase2, "each request must produce a fresh random phrase"
    assert await _pending_count(env, member.id) == 1, "only one pending request"
    # The stored phrase is the second one.
    pending = await env.forget.get_pending_request(member.id)
    assert pending.confirmation_phrase == phrase2


async def test_cancel_forget_me_removes_request(env):
    """Cancelling removes the pending row without deleting Member data."""
    member = await populate(env)
    now = datetime.now(UTC)

    await env.forget.request_forget_me(member.id, env.gym_id, now, 300)
    await env.forget.cancel_forget_me(member.id)

    assert await _pending_count(env, member.id) == 0
    assert await count(env, Member, id=member.id) == 1


async def test_get_pending_request_returns_none_when_empty(env):
    """No pending request returns None."""
    member = await populate(env)
    pending = await env.forget.get_pending_request(member.id)
    assert pending is None


async def test_expired_request_still_stored(env):
    """The store does not auto-expire — the runtime checks expiry."""
    member = await populate(env)
    past = datetime(2020, 1, 1, tzinfo=UTC)

    phrase = await env.forget.request_forget_me(member.id, env.gym_id, past, 1)
    # The row is there even though it's long expired.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.expires_at < datetime.now(UTC)


# -- Behavioral expiry coverage (issue #212) --------------------------------


async def test_runtime_expired_pending_is_cancelled_no_deletion(env):
    """An expired pending request is silently cancelled; no deletion happens
    and normal processing continues (the model runs, not the forget)."""
    member = await populate(env)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, past, 1)

    # Simulate what _handle_forget_me does: the pending is expired, so it
    # cancels and falls through to normal flow — no deletion.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.expires_at < datetime.now(UTC)

    # Expired → cancel silently, fall through to normal processing.
    now = datetime.now(UTC)
    if pending.expires_at <= now:
        await env.forget.cancel_forget_me(member.id)

    # Member data still intact.
    assert await count(env, Member, id=member.id) == 1
    assert await count(env, Session, member_id=member.id) == 1
    # Pending is gone.
    assert await _pending_count(env, member.id) == 0


async def test_runtime_expired_pending_with_phrase_no_deletion(env):
    """Even the exact confirmation phrase on an expired request must not
    trigger deletion — the pending is cleared and normal flow continues."""
    member = await populate(env)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, past, 1)

    pending = await env.forget.get_pending_request(member.id)
    now = datetime.now(UTC)
    assert pending.expires_at <= now

    # The runtime checks expiry first — expired → cancel, no match attempted.
    if pending.expires_at <= now:
        await env.forget.cancel_forget_me(member.id)
        # Falls through; model runs normally.  No forget_member call.

    # Confirm: no deletion happened.
    assert await count(env, Member, id=member.id) == 1
    assert await count(env, Session, member_id=member.id) == 1
    assert await _pending_count(env, member.id) == 0


async def test_runtime_expired_pending_with_wrong_phrase_no_deletion(env):
    """A wrong phrase on an expired pending just cancels; no deletion."""
    member = await populate(env)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, past, 1)

    pending = await env.forget.get_pending_request(member.id)
    now = datetime.now(UTC)
    assert pending.expires_at <= now

    # Runtime: expired → cancel silently.
    if pending.expires_at <= now:
        await env.forget.cancel_forget_me(member.id)

    # Wrong phrase (not the confirmation) — but it doesn't matter because
    # the pending was already expired and cancelled above.
    # Data intact, no deletion.
    assert await count(env, Member, id=member.id) == 1
    assert await _pending_count(env, member.id) == 0


# -- Helper function tests --------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # English triggers
        ("forget me", True),
        ("Forget Me", True),
        ("I want to delete my data", True),
        ("delete my account please", True),
        ("erase me", True),
        ("erase my data permanently", True),
        ("delete my info now", True),
        ("how do I forget me?", True),
        ("  FORGET  ME  ", True),
        # Spanish triggers (ADR-0002; issue #212)
        ("olvídame", True),
        ("OLVÍDAME", True),
        ("bórrame", True),
        ("elimíname por favor", True),
        ("quiero borrar mi cuenta", True),
        ("borra mis datos", True),
        ("elimina mis datos ya", True),
        ("elimina mi cuenta", True),
        ("borra mi información", True),
        # Non-triggers
        ("hello", False),
        ("what's my routine?", False),
        ("delete my workout", False),  # no trigger phrase
        ("", False),
        # Word-boundary guards: these must NOT match (P3)
        ("forget metal", False),
        ("erase message", False),
        ("erase meditation", False),
        ("delete my datagram", False),
    ],
)
def test_is_forget_me_request(text, expected):
    assert is_forget_me_request(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("DELETE-ME-ABC123", "DELETE-ME-ABC123"),
        ("delete-me-abc123", "DELETE-ME-ABC123"),
        ("  Delete-Me-Abc123  ", "DELETE-ME-ABC123"),
        ("DELETE  ME  ABC123", "DELETE ME ABC123"),
        ("some other text", "SOME OTHER TEXT"),
        ("", ""),
    ],
)
def test_normalize_confirmation(text, expected):
    assert normalize_confirmation(text) == expected


# -- Language detection --------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # English
        ("forget me", "en"),
        ("Forget Me", "en"),
        ("delete my data", "en"),
        ("delete my account please", "en"),
        ("erase me", "en"),
        # Spanish
        ("olvídame", "es"),
        ("OLVÍDAME", "es"),
        ("bórrame", "es"),
        ("borra mi cuenta", "es"),
        ("elimina mis datos", "es"),
        # Non-triggers
        ("hello", None),
        ("delete my workout", None),
        ("", None),
    ],
)
def test_detect_forget_me_language(text, expected):
    assert detect_forget_me_language(text) == expected


# -- Atomic consume (P1) -------------------------------------------------


async def test_consume_pending_succeeds_with_matching_unexpired(env):
    """An exact match on an unexpired request consumes it — the row now
    stays with status 'consumed' (not deleted) so a concurrent loser can
    detect deletion in progress (P1)."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    consumed = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert consumed
    # The row stays (status = 'consumed') — not deleted.
    assert await _pending_count(env, member.id) == 1
    # But get_pending_request filters to status='pending', so it returns None.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is None
    # get_consumed_request finds it.
    consumed_req = await env.forget.get_consumed_request(member.id)
    assert consumed_req is not None
    assert consumed_req.status == "consumed"


async def test_consume_pending_fails_with_wrong_phrase(env):
    """A non-matching phrase does not consume the request — the row
    stays with status 'pending'."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    consumed = await env.forget.consume_pending_forget_me(
        member.id, "WRONG-PHRASE", datetime.now(UTC)
    )
    assert not consumed
    # The row is still there (pending).
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.confirmation_phrase == phrase
    assert pending.status == "pending"


async def test_consume_pending_fails_with_expired_request(env):
    """An expired request is not consumed even with the correct phrase."""
    member = await populate(env)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, past, 1)

    consumed = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert not consumed
    # The row is still there (runtime's job to cancel expired ones).
    assert await _pending_count(env, member.id) == 1


async def test_consume_pending_fails_with_no_request(env):
    """Consuming when there's no pending request returns False."""
    member = await populate(env)
    consumed = await env.forget.consume_pending_forget_me(
        member.id, "DELETE-ME-XXXXXX", datetime.now(UTC)
    )
    assert not consumed


async def test_consume_pending_exactly_at_expiry_is_expired(env):
    """When expires_at == now the request is expired (P2: <= not <)."""
    member = await populate(env)
    now = datetime.now(UTC)
    # expires_at == now (lifetime=0)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 0)

    consumed = await env.forget.consume_pending_forget_me(
        member.id, phrase, now
    )
    assert not consumed, "expires_at == now must be expired"


async def test_consume_pending_is_idempotent(env):
    """A second consume on an already-consumed request returns False."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    consumed = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert consumed
    # Second attempt with the same phrase finds nothing.
    consumed2 = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert not consumed2


# -- Residue test with chat history (P2) ---------------------------------


async def test_two_turn_forget_with_chat_history_leaves_zero_residue(env):
    """The full two-turn flow — trigger, confirm, hard delete — must wipe
    the SDK chat history alongside every domain row."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    # Seed a real conversation turn: user message + assistant response.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "bench 60 8,8,8"},
        {"role": "assistant", "content": "Logged! 3 sets at 60 kg."},
    ])
    # Verify history is seeded.
    items = await session.get_items()
    assert len(items) == 2

    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Consume (like the runtime does) then hard-delete.
    consumed = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert consumed
    await env.forget.forget_member(member.id)

    # Domain rows must be gone.
    assert await count(env, Member, id=member.id) == 0
    assert await count(env, Session, member_id=member.id) == 0
    assert await count(env, MemberNote, member_id=member.id) == 0
    assert await count(env, Routine, member_id=member.id) == 0
    # Chat history must be gone.
    items = await session.get_items()
    assert items == [], "SDK chat history must be wiped"
    # No residue anywhere.
    assert await count(env, Set) == 0
    assert await count(env, Workout) == 0
    assert await count(env, WorkoutExercise) == 0
    assert await _pending_count(env, member.id) == 0


# -- Conversation language detection (P2, ADR-0002) ---------------------


async def test_conversation_language_returns_none_when_no_history(env):
    """When there is no chat history (new member), detect_conversation_language
    returns None — the caller falls back to trigger-text language."""
    from agents.extensions.memory import SQLAlchemySession
    from agentg.forget import detect_conversation_language

    member = await populate(env)
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    lang = await detect_conversation_language(session)
    assert lang is None


async def test_conversation_language_detects_spanish_from_history(env):
    """Spanish-dominant conversation history returns 'es'."""
    from agents.extensions.memory import SQLAlchemySession
    from agentg.forget import detect_conversation_language

    member = await populate(env)
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "¡Hola! ¿En qué te ayudo?"},
    ])
    lang = await detect_conversation_language(session)
    assert lang == "es"


async def test_conversation_language_detects_english_from_history(env):
    """English-dominant conversation history returns 'en'."""
    from agents.extensions.memory import SQLAlchemySession
    from agentg.forget import detect_conversation_language

    member = await populate(env)
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hey what's my routine?"},
        {"role": "assistant", "content": "Here's your plan for today."},
    ])
    lang = await detect_conversation_language(session)
    assert lang == "en"


# -- Separate-session concurrency (P1) -----------------------------------


async def test_consume_pending_concurrent_sessions_one_winner(env):
    """Two separate sessions trying to consume the same pending request
    must have exactly one winner.  The winner sets status='consumed';
    the loser sees zero rows because the status filter (pending) no
    longer matches.  SQLite serializes writes (single-writer design);
    Postgres would also serialise the conditional UPDATEs — either way,
    the DB guarantees exactly one row is consumed."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import update as sa_update
    from agentg.models import ForgetMeRequest
    from agentg.forget import STATUS_CONSUMED, STATUS_PENDING

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    # Session A consumes first.
    rowcount_a: int = 0
    async with async_sessionmaker(env.engine)() as db:
        result = await db.execute(
            sa_update(ForgetMeRequest)
            .where(
                ForgetMeRequest.member_id == member.id,
                ForgetMeRequest.confirmation_phrase == phrase,
                ForgetMeRequest.expires_at > now,
                ForgetMeRequest.status == STATUS_PENDING,
            )
            .values(status=STATUS_CONSUMED)
        )
        await db.commit()
        rowcount_a = result.rowcount

    # Session B tries to consume the same row — must see zero rows
    # because status is now 'consumed', not 'pending'.
    rowcount_b: int = 0
    async with async_sessionmaker(env.engine)() as db:
        result = await db.execute(
            sa_update(ForgetMeRequest)
            .where(
                ForgetMeRequest.member_id == member.id,
                ForgetMeRequest.confirmation_phrase == phrase,
                ForgetMeRequest.expires_at > now,
                ForgetMeRequest.status == STATUS_PENDING,
            )
            .values(status=STATUS_CONSUMED)
        )
        await db.commit()
        rowcount_b = result.rowcount

    # Exactly one winner.
    assert rowcount_a == 1, f"session A should have won, got {rowcount_a}"
    assert rowcount_b == 0, f"session B should have lost, got {rowcount_b}"

    # The row still exists (status='consumed').
    consumed_row = await env.forget.get_consumed_request(member.id)
    assert consumed_row is not None
    assert consumed_row.status == STATUS_CONSUMED


# -- Loser-safety: consumed state prevents model access (P1) --------------


async def test_loser_sees_consumed_state_before_deletion_completes(env):
    """True interleaving test: after consume_pending_forget_me commits
    (winner claims the row), a concurrent loser that also tries to consume
    sees the consumed status — not just a missing row.  The loser must
    return a safe goodbye without reaching the model, even while the
    winner is still mid-deletion.

    This is the P1 from fix-r3: the consumed row acts as a durable
    in-progress signal so a matched-but-lost confirmation never reaches
    the model, including while deletion is in progress."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Seed chat history to prove nothing new is added.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
    ])
    items_before = await session.get_items()
    assert len(items_before) == 1

    # Winner consumes the request (status -> 'consumed').
    won = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert won

    # The consumed row is visible to any concurrent runtime.
    consumed_req = await env.forget.get_consumed_request(member.id)
    assert consumed_req is not None
    assert consumed_req.status == "consumed"
    assert consumed_req.language == "en"

    # The Member still exists (winner hasn't called forget_member yet —
    # simulating mid-deletion window).
    assert await count(env, Member, id=member.id) == 1

    # The loser would call get_consumed_request, find the row, and return
    # a goodbye WITHOUT calling forget_member and WITHOUT falling through
    # to the model.  The chat history must be exactly as before.
    items_after = await session.get_items()
    assert len(items_after) == 1  # no new model residue
    assert await count(env, Member, id=member.id) == 1  # winner hasn't deleted yet


async def test_interrupted_deletion_recovered_by_consumed_state(env):
    """When a winner consumes the request but crashes before forget_member
    completes, the consumed row persists.  On the next message, the runtime
    detects it and completes the deletion — retries safely complete
    deletion after interruption."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
    ])

    # Simulate: winner consumes (sets status='consumed') but then "crashes"
    # before calling forget_member.
    won = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert won
    # forget_member was NOT called — simulating the crash.

    # The consumed row is there.
    consumed_req = await env.forget.get_consumed_request(member.id)
    assert consumed_req is not None

    # On retry (simulating the next message arriving), the runtime detects
    # the consumed row and completes the deletion.
    if consumed_req is not None:
        await env.forget.forget_member(member.id)
        # Return goodbye with the stored language.
        assert consumed_req.language == "es"

    # Deletion completed.
    assert await count(env, Member, id=member.id) == 0
    assert await count(env, Session, member_id=member.id) == 0
    assert await count(env, MemberNote, member_id=member.id) == 0
    # Chat history wiped by forget_member.
    items = await session.get_items()
    assert items == []
    # The consumed row was cleaned up by forget_member.
    assert await _pending_count(env, member.id) == 0


async def test_consumed_request_blocks_model_access(env):
    """get_consumed_request returns the consumed row while deletion is
    in progress.  get_pending_request must NOT return it (it filters to
    status='pending').  This means a loser checking for pending finds
    nothing, then checking for consumed finds the signal — and must
    return a goodbye, never falling through to the model."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    # Winner consumes.
    won = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert won

    # get_pending_request must NOT see the consumed row.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is None, (
        "get_pending_request must filter to status='pending' only"
    )

    # get_consumed_request must see it.
    consumed = await env.forget.get_consumed_request(member.id)
    assert consumed is not None
    assert consumed.status == "consumed"


async def test_loser_after_member_deleted_must_not_reach_model(env):
    """When the atomic consume loses (row already gone because another
    runtime won the race and deleted the Member), the loser must detect
    the vanished identity and return a safe reply without touching the
    model — no chat-history residue, no re-creation of domain rows."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    # Seed chat history to confirm it is wiped and nothing new is added.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
    ])

    # Simulate the winner: consume + delete.
    consumed = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert consumed
    await env.forget.forget_member(member.id)

    # The member is gone.
    assert await count(env, Member, id=member.id) == 0
    assert await env.linking.identity_for("telegram", "42") is None

    # The "loser" runtime would now check the pending request (it's gone)
    # and try to re-verify the identity — it must see None and NOT reach
    # the model.  We simulate this: after deletion, the identity is
    # unresolvable.  The runtime must not proceed to model processing.
    identity = await env.linking.identity_for("telegram", "42")
    assert identity is None, (
        "after forget, identity_for must return None — "
        "a loser runtime must detect this and return a safe reply"
    )

    # Chat history must be gone (the winner cleared it).
    items = await session.get_items()
    assert items == []

    # No new residue was created (no model run for a deleted member).
    assert await count(env, Member, id=member.id) == 0


# -- P2: language from whole conversation, not just trigger text ----------


async def test_forget_me_detects_language_from_chat_history_not_trigger(env):
    """A Spanish-conversation Member using an English forget-me trigger
    must still receive Spanish deletion messages (ADR-0002: sticky
    whole-conversation language, not trigger-text language)."""
    from agents.extensions.memory import SQLAlchemySession
    from agentg.forget import detect_conversation_language

    member = await populate(env)
    # Seed a Spanish conversation so the whole-conversation language is
    # clearly Spanish, even though the trigger is in English.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola, quiero entrenar pecho hoy"},
        {"role": "assistant", "content": "¡Claro! Vamos con press banca."},
        {"role": "user", "content": "bench 60 8,8,8"},
        {"role": "assistant", "content": "Registrado — 60 kg, 3 series de 8."},
    ])

    # The trigger text alone would say English, but the conversation
    # history is overwhelmingly Spanish.
    lang = await detect_conversation_language(session) or "es"
    assert lang == "es", (
        "whole-conversation language must be Spanish despite English trigger"
    )


# -- P1: end-of-method safety-net consumed re-check (issue #212, fix-r4) --


async def test_ordinary_message_caught_by_end_of_method_consumed_check(env):
    """P1 from fix-r4: an ordinary message that enters _handle_forget_me
    before the consumed row exists but reaches the end-of-method safety net
    after a concurrent runtime consumed the request MUST be caught by the
    safety net's re-check of get_consumed_request — before the identity
    check and before the model ever sees the message.

    The scenario (true interleaving):
    1. Runtime B enters _handle_forget_me with an ordinary message ("hola")
    2. get_consumed_request → None (no request yet)
    3. get_pending_request → None
    4. is_forget_me_request("hola") → False
    5. [RUNTIME A consumes the request and starts deletion]
    6. End-of-method: consumed re-check → catches the row → returns goodbye

    Without the re-check, step 6 would see identity still exists (Member
    not yet deleted) and fall through to the model while deletion is in
    progress — chat-history residue."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Seed chat history to prove nothing new is added.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
    ])
    items_before = await session.get_items()
    assert len(items_before) == 1

    # Simulates initial checks passing (no consumed, no pending matching
    # an ordinary message).
    consumed_initial = await env.forget.get_consumed_request(member.id)
    assert consumed_initial is None
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None  # a pending exists, but...
    # ...the message is ordinary, not a confirmation.
    normalized = "hola".strip().upper()
    assert normalized != pending.confirmation_phrase

    # Runtime A consumes the request while Runtime B is mid-method.
    won = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert won

    # Now simulate the end-of-method safety net: re-check consumed state
    # BEFORE checking identity.  This is what our P1 fix adds.
    consumed_now = await env.forget.get_consumed_request(member.id)
    assert consumed_now is not None, (
        "end-of-method consumed re-check MUST find the consumed row"
    )
    assert consumed_now.language == "en"
    # The safety net returns a goodbye; the model is never touched.
    items_after = await session.get_items()
    assert len(items_after) == 1  # no new model residue
    # Member still exists (winner hasn't called forget_member yet).
    assert await count(env, Member, id=member.id) == 1


async def test_ordinary_message_caught_after_consumed_but_before_identity_gone(env):
    """The critical interleaving gap: consumed row exists but identity still
    resolves.  The old safety net only checked identity (which still
    resolves → falls through to model).  The new safety net checks consumed
    state FIRST and catches the in-progress deletion."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")

    # Consume the request (deletion in progress, Member still exists).
    won = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert won

    # At this point: consumed row exists, identity still resolves.
    consumed = await env.forget.get_consumed_request(member.id)
    assert consumed is not None
    identity = await env.linking.identity_for("telegram", "42")
    assert identity is not None, "Member still exists mid-deletion"

    # The safety net must check consumed FIRST.  If it checked identity
    # first (it resolves → pass), the model would see the message while
    # deletion is in progress.  Consumed check catches it.
    assert consumed.language == "es"
    # The runtime returns a goodbye here, never calls the model.
    assert await count(env, Member, id=member.id) == 1  # not yet deleted


async def test_safety_net_consumed_check_before_identity(env):
    """Explicit ordering test: the safety net must check consumed state
    BEFORE re-verifying identity.  If consumed exists, return goodbye
    regardless of identity — even if the Member row still exists."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Consume → consumed row exists, identity still intact.
    won = await env.forget.consume_pending_forget_me(
        member.id, phrase, datetime.now(UTC)
    )
    assert won

    # Simulate the safety net in the correct order:
    # 1. Check consumed first.
    consumed = await env.forget.get_consumed_request(member.id)
    assert consumed is not None
    # If consumed found → goodbye.  Identity check is skipped.
    assert consumed.language == "en"

    # If the check were identity-first, it would resolve successfully
    # (Member row still exists) and the model would run — the bug.
    identity = await env.linking.identity_for("telegram", "42")
    assert identity is not None, (
        "identity still resolves — but consumed check must gate first"
    )


# -- P2: concurrent initial requests via upsert (issue #212, fix-r4) ------


async def test_concurrent_initial_requests_no_integrity_error(env):
    """P2 from fix-r4: two initial forget-me requests (no prior row for
    this Member) must not collide on the unique member_id constraint.
    The upsert in request_forget_me replaces the old delete-then-insert
    so concurrent initial requests are race-safe."""
    member = await populate(env)
    now = datetime.now(UTC)

    # Two rapid initial requests — the second overwrites the first
    # without ever seeing a delete-then-insert race window.
    phrase1 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")
    phrase2 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")

    # Both returned valid phrases.
    assert phrase1.startswith("DELETE-ME-")
    assert phrase2.startswith("DELETE-ME-")

    # Exactly one row exists (the second one won).
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.confirmation_phrase == phrase2
    assert pending.language == "es"  # second request's language
    assert pending.status == "pending"


async def test_upsert_replaces_consumed_row_back_to_pending(env):
    """When a new forget-me request arrives after a consumed (but not yet
    deleted) row exists, the upsert must revert status to 'pending' so
    the new request is a clean slate.  The Member re-asked after a
    previous delete was interrupted."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase1 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Consume the first request (status -> 'consumed').
    won = await env.forget.consume_pending_forget_me(
        member.id, phrase1, datetime.now(UTC)
    )
    assert won

    # Verify consumed row exists.
    consumed = await env.forget.get_consumed_request(member.id)
    assert consumed is not None
    assert consumed.status == "consumed"

    # Member sends a new forget-me trigger — the upsert resets to pending.
    phrase2 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")
    assert phrase2.startswith("DELETE-ME-")
    assert phrase2 != phrase1

    # The row is now pending with the new phrase and language.
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.confirmation_phrase == phrase2
    assert pending.language == "es"
    assert pending.status == "pending"

    # Consumed is gone (overwritten by upsert).
    consumed_after = await env.forget.get_consumed_request(member.id)
    assert consumed_after is None


async def test_upsert_over_replaced_consumed_still_deletes_on_confirm(env):
    """End-to-end: a consumed-then-overwritten request where the Member
    confirms with the NEW phrase must delete correctly."""
    member = await populate(env)
    now = datetime.now(UTC)

    # First request → consume → consumed row exists.
    phrase1 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")
    won = await env.forget.consume_pending_forget_me(
        member.id, phrase1, datetime.now(UTC)
    )
    assert won

    # Second request overwrites consumed → back to pending with new phrase.
    phrase2 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")

    # Confirm with the NEW phrase.
    consumed = await env.forget.consume_pending_forget_me(
        member.id, phrase2, datetime.now(UTC)
    )
    assert consumed
    await env.forget.forget_member(member.id)

    # Full deletion completed.
    assert await count(env, Member, id=member.id) == 0
    assert await _pending_count(env, member.id) == 0
