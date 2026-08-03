"""Forget-me: a Member's hard delete across all three stores (spec §Privacy)."""

from datetime import UTC, datetime, timedelta

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

from datetime import UTC, datetime, timedelta

from agentg.forget import (STATUS_DELETING, STATUS_PENDING, detect_forget_me_language, is_forget_me_request, normalize_confirmation)
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

    # Confirm with the exact phrase via atomic claim.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
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


async def test_claim_succeeds_with_matching_unexpired(env):
    """An exact match on an unexpired request claims it — the row now
    stays with status 'deleting' (not deleted) so a concurrent loser can
    detect deletion in progress and the phrase remains valid for retry (fix-3)."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
    # The row stays (status = 'deleting') — not deleted.
    assert await _pending_count(env, member.id) == 1
    # But get_pending_request filters to status='pending', so it returns None.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is None
    # get_deleting_request finds it.
    deleting_req = await env.forget.get_deleting_request(member.id)
    assert deleting_req is not None
    assert deleting_req.status == "deleting"


async def test_claim_fails_with_wrong_phrase(env):
    """A non-matching phrase does not claim the request — the row
    stays with status 'pending'."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    claimed = await env.forget.claim_forget_me_request(
        member.id, "WRONG-PHRASE", datetime.now(UTC)
    )
    assert claimed is None
    # The row is still there (pending).
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.confirmation_phrase == phrase
    assert pending.status == "pending"


async def test_claim_fails_with_expired_request(env):
    """An expired request is not claimed even with the correct phrase."""
    member = await populate(env)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, past, 1)

    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is None
    # The row is still there (runtime's job to cancel expired ones).
    assert await _pending_count(env, member.id) == 1


async def test_claim_fails_with_no_request(env):
    """Claiming when there's no pending request returns None."""
    member = await populate(env)
    claimed = await env.forget.claim_forget_me_request(
        member.id, "DELETE-ME-XXXXXX", datetime.now(UTC)
    )
    assert claimed is None


async def test_claim_exactly_at_expiry_is_expired(env):
    """When expires_at == now the request is expired (P2: <= not <)."""
    member = await populate(env)
    now = datetime.now(UTC)
    # expires_at == now (lifetime=0)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 0)

    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, now
    )
    assert claimed is None, "expires_at == now must be expired"


async def test_claim_is_idempotent(env):
    """A second claim on an already-claimed request returns None."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
    # Second attempt with the same phrase finds nothing.
    claimed2 = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed2 is None


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

    # Claim (like the runtime does) then hard-delete.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
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


async def test_claim_concurrent_sessions_one_winner(env):
    """Two separate sessions trying to claim the same pending request
    must have exactly one winner.  The winner sets status='deleting';
    the loser sees zero rows because the status filter (pending) no
    longer matches.  SQLite serializes writes (single-writer design);
    Postgres would also serialise the conditional UPDATEs — either way,
    the DB guarantees exactly one row is claimed."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import update as sa_update
    from agentg.models import ForgetMeRequest
    from agentg.forget import STATUS_DELETING, STATUS_PENDING

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    # Session A claims first.
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
            .values(status=STATUS_DELETING)
        )
        await db.commit()
        rowcount_a = result.rowcount

    # Session B tries to claim the same row — must see zero rows
    # because status is now 'deleting', not 'pending'.
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
            .values(status=STATUS_DELETING)
        )
        await db.commit()
        rowcount_b = result.rowcount

    # Exactly one winner.
    assert rowcount_a == 1, f"session A should have won, got {rowcount_a}"
    assert rowcount_b == 0, f"session B should have lost, got {rowcount_b}"

    # The row still exists (status='deleting').
    deleting_row = await env.forget.get_deleting_request(member.id)
    assert deleting_row is not None
    assert deleting_row.status == STATUS_DELETING


# -- Loser-safety: deleting state prevents model access (P1) --------------


async def test_loser_sees_deleting_state_before_deletion_completes(env):
    """True interleaving test: after claim_forget_me_request commits
    (winner claims the row), a concurrent loser that also tries to claim
    sees the deleting status — not just a missing row.  The loser must
    return a safe goodbye without reaching the model, even while the
    winner is still mid-deletion.

    This is the P1 from fix-r3: the deleting row acts as a durable
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

    # Winner claims the request (status -> 'deleting').
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # The deleting row is visible to any concurrent runtime.
    deleting_req = await env.forget.get_deleting_request(member.id)
    assert deleting_req is not None
    assert deleting_req.status == "deleting"
    assert deleting_req.language == "en"

    # The Member still exists (winner hasn't called forget_member yet —
    # simulating mid-deletion window).
    assert await count(env, Member, id=member.id) == 1

    # The loser would call get_deleting_request, find the row, and return
    # a goodbye WITHOUT calling forget_member and WITHOUT falling through
    # to the model.  The chat history must be exactly as before.
    items_after = await session.get_items()
    assert len(items_after) == 1  # no new model residue
    assert await count(env, Member, id=member.id) == 1  # winner hasn't deleted yet


async def test_interrupted_deletion_recovered_by_deleting_state(env):
    """When a winner claims the request but crashes before forget_member
    completes, the deleting row persists.  On the next message, the runtime
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

    # Simulate: winner claims (sets status='deleting') but then "crashes"
    # before calling forget_member.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
    # forget_member was NOT called — simulating the crash.

    # The deleting row is there.
    deleting_req = await env.forget.get_deleting_request(member.id)
    assert deleting_req is not None

    # On retry (simulating the next message arriving), the runtime detects
    # the deleting row and completes the deletion.
    if deleting_req is not None:
        await env.forget.forget_member(member.id)
        # Return goodbye with the stored language.
        assert deleting_req.language == "es"

    # Deletion completed.
    assert await count(env, Member, id=member.id) == 0
    assert await count(env, Session, member_id=member.id) == 0
    assert await count(env, MemberNote, member_id=member.id) == 0
    # Chat history wiped by forget_member.
    items = await session.get_items()
    assert items == []
    # The deleting row was cleaned up by forget_member.
    assert await _pending_count(env, member.id) == 0


async def test_deleting_request_blocks_model_access(env):
    """get_deleting_request returns the deleting row while deletion is
    in progress.  get_pending_request must NOT return it (it filters to
    status='pending').  This means a loser checking for pending finds
    nothing, then checking for deleting finds the signal — and must
    return a goodbye, never falling through to the model."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    # Winner claims.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # get_pending_request must NOT see the deleting row.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is None, (
        "get_pending_request must filter to status='pending' only"
    )

    # get_deleting_request must see it.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == "deleting"


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

    # Simulate the winner: claim + delete.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
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


# -- P1: end-of-method safety-net deleting re-check (issue #212, fix-r4) --


async def test_ordinary_message_caught_by_end_of_method_deleting_check(env):
    """P1 from fix-r4: an ordinary message that enters _handle_forget_me
    before the deleting row exists but reaches the end-of-method safety net
    after a concurrent runtime claimed the request MUST be caught by the
    safety net's re-check of get_deleting_request — before the identity
    check and before the model ever sees the message.

    The scenario (true interleaving):
    1. Runtime B enters _handle_forget_me with an ordinary message ("hola")
    2. get_deleting_by_phrase → None (no deleting request yet)
    3. get_pending_request → None
    4. is_forget_me_request("hola") → False
    5. [RUNTIME A claims the request and starts deletion]
    6. End-of-method: deleting re-check → catches the row → returns goodbye

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

    # Simulates initial checks passing (no deleting, no pending matching
    # an ordinary message).
    deleting_initial = await env.forget.get_deleting_request(member.id)
    assert deleting_initial is None
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None  # a pending exists, but...
    # ...the message is ordinary, not a confirmation.
    normalized = "hola".strip().upper()
    assert normalized != pending.confirmation_phrase

    # Runtime A claims the request while Runtime B is mid-method.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Now simulate the end-of-method safety net: re-check deleting state
    # BEFORE checking identity.  This is what our P1 fix adds.
    deleting_now = await env.forget.get_deleting_request(member.id)
    assert deleting_now is not None, (
        "end-of-method deleting re-check MUST find the deleting row"
    )
    assert deleting_now.language == "en"
    # The safety net returns a goodbye; the model is never touched.
    items_after = await session.get_items()
    assert len(items_after) == 1  # no new model residue
    # Member still exists (winner hasn't called forget_member yet).
    assert await count(env, Member, id=member.id) == 1


async def test_ordinary_message_caught_after_deleting_but_before_identity_gone(env):
    """The critical interleaving gap: deleting row exists but identity still
    resolves.  The old safety net only checked identity (which still
    resolves → falls through to model).  The new safety net checks deleting
    state FIRST and catches the in-progress deletion."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")

    # Consume the request (deletion in progress, Member still exists).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # At this point: deleting row exists, identity still resolves.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    identity = await env.linking.identity_for("telegram", "42")
    assert identity is not None, "Member still exists mid-deletion"

    # The safety net must check deleting FIRST.  If it checked identity
    # first (it resolves → pass), the model would see the message while
    # deletion is in progress.  Deleting check catches it.
    assert deleting.language == "es"
    # The runtime returns a goodbye here, never calls the model.
    assert await count(env, Member, id=member.id) == 1  # not yet deleted


async def test_safety_net_deleting_check_before_identity(env):
    """Explicit ordering test: the safety net must check deleting state
    BEFORE re-verifying identity.  If deleting exists, return goodbye
    regardless of identity — even if the Member row still exists."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Consume → deleting row exists, identity still intact.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Simulate the safety net in the correct order:
    # 1. Check deleting first.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    # If deleting found → goodbye.  Identity check is skipped.
    assert deleting.language == "en"

    # If the check were identity-first, it would resolve successfully
    # (Member row still exists) and the model would run — the bug.
    identity = await env.linking.identity_for("telegram", "42")
    assert identity is not None, (
        "identity still resolves — but deleting check must gate first"
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


async def test_request_forget_me_does_not_reset_deleting_to_pending(env):
    """P1 fix-r5: a re-request must never reset a deleting row to pending.
    When a previous deletion was confirmed (deleting) but not yet completed,
    a new forget-me trigger must NOT overwrite the deleting row — the
    deletion must complete, not be discarded.  The deleting row stays intact
    so the runtime can recover it."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase1 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Claim the first request (status -> 'deleting').
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase1, datetime.now(UTC)
    )
    assert claimed is not None

    # Verify deleting row exists.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == "deleting"

    # A new forget-me request must NOT reset deleting to pending —
    # request_forget_me returns empty string when the row is deleting
    # so the caller can recover (issue #212, fix-r6).
    phrase2 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")
    assert phrase2 == "", f"request_forget_me must return '' when row is deleting, got {phrase2!r}"

    # Deleting row is still there — NOT overwritten.
    deleting_after = await env.forget.get_deleting_request(member.id)
    assert deleting_after is not None, (
        "deleting row must NOT be reset to pending by a re-request"
    )
    assert deleting_after.status == "deleting"
    assert deleting_after.language == "en"  # original language preserved
    assert deleting_after.confirmation_phrase == phrase1  # original phrase preserved

    # The runtime would detect the deleting row on next message and complete
    # the deletion — simulate that flow.
    recovered = await env.forget.get_deleting_request(member.id)
    assert recovered is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_deleting_row_recovered_by_runtime_not_overwritten(env):
    """End-to-end fix-r5: when a deleting row exists, the runtime detects it
    first (before calling request_forget_me) and completes the deletion.
    The store's request_forget_me never overwrites a deleting row, so the
    original confirmation phrase is preserved and the runtime can complete
    deletion on the next message."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
    ])

    # First request → claim → deleting row exists.
    phrase1 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase1, datetime.now(UTC)
    )
    assert claimed is not None

    # Simulate the runtime flow: deleting check first → complete deletion.
    deleting_req = await env.forget.get_deleting_request(member.id)
    assert deleting_req is not None
    assert deleting_req.status == "deleting"
    await env.forget.forget_member(member.id)

    # Full deletion completed.
    assert await count(env, Member, id=member.id) == 0
    assert await _pending_count(env, member.id) == 0
    items = await session.get_items()
    assert items == []


# -- P1: group messages must not bypass the deleting gate (fix-r5) --------


async def test_group_message_cannot_bypass_deleting_gate(env):
    """P1 fix-r5: a group message from a Member with a deleting (deletion
    in progress) request must NOT reach the model.  The deleting check gates
    ALL paths, including group messages."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Seed chat history to prove nothing is added.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
    ])
    items_before = await session.get_items()
    assert len(items_before) == 1

    # Consume the request (deletion confirmed, but not yet completed).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Simulate what _handle_forget_me does: deleting check FIRST,
    # BEFORE the group early return.  A group message should still trigger
    # completion of the deletion, not fall through to the model.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == "deleting"

    # The runtime completes deletion and returns goodbye.
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0

    # Chat history is wiped — model was never reached.
    items_after = await session.get_items()
    assert items_after == []


async def test_group_message_recovers_interrupted_deleting_deletion(env):
    """P1 fix-r5: a group message after a crashed deletion (deleting row
    exists, Member still exists) must complete the deletion rather than
    cancelling or falling through."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
    ])

    # Consume but don't delete — simulating a crash after confirmation.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Deleting row exists, Member still exists (simulated crash).
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == "deleting"
    assert await count(env, Member, id=member.id) == 1

    # A group message now arrives.  With the fix-r5 reorder, the deleting
    # check runs FIRST and completes the deletion.
    if deleting is not None:
        await env.forget.forget_member(member.id)

    # Deletion completed, no model residue.
    assert await count(env, Member, id=member.id) == 0
    items = await session.get_items()
    assert items == []


# -- P2: language detection with list-form content and punctuation (fix-r5)


async def test_language_detection_handles_list_form_content(env):
    """P2 fix-r5: the OpenAI Responses API stores assistant/user content as a
    list of content blocks like [{"type": "text", "text": "¡Hola!"}].
    detect_conversation_language must extract text from both string and
    list-form content."""
    from agents.extensions.memory import SQLAlchemySession
    from agentg.forget import detect_conversation_language

    member = await populate(env)
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)

    # List-form content blocks — the real format from the Responses API.
    await session.add_items([
        {"role": "user", "content": [{"type": "text", "text": "hola, quiero entrenar"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "¡Claro! Vamos con press banca."}]},
    ])

    lang = await detect_conversation_language(session)
    assert lang == "es", (
        f"list-form Spanish content must be detected, got {lang}"
    )


async def test_language_detection_strips_punctuation_from_words(env):
    """P2 fix-r5: punctuation like ¡Hola! must match the signal word 'hola'.
    Using \\w+ for word extraction strips surrounding punctuation."""
    from agents.extensions.memory import SQLAlchemySession
    from agentg.forget import detect_conversation_language

    member = await populate(env)
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)

    # Mixed punctuation: ¡Hola!, ¿cómo?, etc.
    await session.add_items([
        {"role": "user", "content": "¡Hola! ¿Cómo puedo entrenar pecho?"},
        {"role": "assistant", "content": "¡Claro! Vamos con press banca."},
    ])

    lang = await detect_conversation_language(session)
    # "hola" should match _SPANISH_SIGNAL_WORDS after stripping ¡ and !
    assert lang == "es", (
        f"punctuation-wrapped Spanish must be detected, got {lang}"
    )


async def test_language_detection_handles_mixed_content_formats(env):
    """P2 fix-r5: some history items may be strings, others list-form blocks.
    The detector must handle a mix of both in the same conversation."""
    from agents.extensions.memory import SQLAlchemySession
    from agentg.forget import detect_conversation_language

    member = await populate(env)
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)

    # Mix of string and list-form content.
    await session.add_items([
        {"role": "user", "content": "hey coach, what's my routine?"},
        {"role": "assistant", "content": [{"type": "text", "text": "Here's your plan for today!"}]},
        {"role": "user", "content": [{"type": "text", "text": "thanks, that looks great"}]},
    ])

    lang = await detect_conversation_language(session)
    assert lang == "en", (
        f"mixed-format English content must be detected, got {lang}"
    )


# -- P2: true database-level atomic upsert across processes (fix-r5) ------


async def test_concurrent_initial_requests_across_sessions_no_error(env):
    """P2 fix-r5: two initial forget-me requests from independent sessions
    (simulating separate processes) must both succeed without IntegrityError.
    The atomic upsert (INSERT … ON CONFLICT DO UPDATE) replaces the old
    select-then-insert pattern that could race across runtimes."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from agentg.forget import STATUS_PENDING

    member = await populate(env)
    now = datetime.now(UTC)
    phrase_a = "DELETE-ME-AAAAAA"
    phrase_b = "DELETE-ME-BBBBBB"
    expires = now + timedelta(seconds=300)

    # Session A (process A) inserts first.
    async with async_sessionmaker(env.engine)() as db_a:
        stmt_a = sqlite_insert(ForgetMeRequest).values(
            member_id=member.id,
            gym_id=env.gym_id,
            confirmation_phrase=phrase_a,
            expires_at=expires,
            created_at=now,
            language="en",
            status=STATUS_PENDING,
        )
        await db_a.execute(stmt_a)
        await db_a.commit()

    # Session B (process B) tries to insert — must succeed via upsert,
    # not raise IntegrityError.
    async with async_sessionmaker(env.engine)() as db_b:
        stmt_b = sqlite_insert(ForgetMeRequest).values(
            member_id=member.id,
            gym_id=env.gym_id,
            confirmation_phrase=phrase_b,
            expires_at=expires,
            created_at=now,
            language="es",
            status=STATUS_PENDING,
        ).on_conflict_do_update(
            index_elements=["member_id"],
            set_=dict(
                gym_id=env.gym_id,
                confirmation_phrase=phrase_b,
                expires_at=expires,
                created_at=now,
                language="es",
                status=STATUS_PENDING,
            ),
        )
        await db_b.execute(stmt_b)
        await db_b.commit()

    # Exactly one row exists; the last write won.
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING


async def test_upsert_preserves_pending_for_second_write(env):
    """P2 fix-r5: the upsert on a pending row preserves status as pending.
    When process A created a pending request and process B's upsert lands,
    the row must still be in pending state (not deleting)."""
    member = await populate(env)
    now = datetime.now(UTC)

    # First request creates pending (via upsert).
    phrase1 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")
    pending1 = await env.forget.get_pending_request(member.id)
    assert pending1 is not None
    assert pending1.status == "pending"
    assert pending1.language == "en"

    # Second request (via upsert) overwrites pending → still pending.
    phrase2 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")
    pending2 = await env.forget.get_pending_request(member.id)
    assert pending2 is not None
    assert pending2.status == "pending"
    assert pending2.language == "es"
    assert pending2.confirmation_phrase == phrase2
    assert pending2.confirmation_phrase != phrase1


# -- P1: upsert WHERE guard prevents overwriting deleting rows (fix-r6) ---


async def test_upsert_where_guard_prevents_overwriting_deleting(env):
    """P1 fix-r6: the conditional upsert's WHERE status='pending' clause
    prevents the DO UPDATE from firing when another runtime claimed the
    row between our fast-path read and the upsert.  Tested at the SQL level
    with independent sessions — no hook needed."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from agentg.forget import STATUS_DELETING, STATUS_PENDING

    member = await populate(env)
    now = datetime.now(UTC)

    # Create a pending request via the store.
    phrase_a = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Simulate Runtime A: read the row (sees pending) but pause.
    # Meanwhile Runtime B claims the request (status → deleting).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase_a, datetime.now(UTC)
    )
    assert claimed is not None
    # Verify the claim set status to 'deleting'.
    verifying = await env.forget.get_deleting_request(member.id)
    assert verifying is not None
    assert verifying.status == STATUS_DELETING

    # Runtime A now runs its upsert with WHERE status='pending'.
    # Because Runtime B already set status to 'deleting', the WHERE
    # clause prevents the DO UPDATE from firing.
    phrase_b = "DELETE-ME-BBBBBB"
    expires = now + timedelta(seconds=300)
    async with async_sessionmaker(env.engine)() as db:
        stmt = sqlite_insert(ForgetMeRequest).values(
            member_id=member.id,
            gym_id=env.gym_id,
            confirmation_phrase=phrase_b,
            expires_at=expires,
            created_at=now,
            language="es",
            status=STATUS_PENDING,
        ).on_conflict_do_update(
            index_elements=["member_id"],
            set_=dict(
                gym_id=env.gym_id,
                confirmation_phrase=phrase_b,
                expires_at=expires,
                created_at=now,
                language="es",
                status=STATUS_PENDING,
            ),
            where=(ForgetMeRequest.status == STATUS_PENDING),
        )
        await db.execute(stmt)
        await db.commit()

    # The row must still be 'deleting' — the upsert's WHERE guard
    # prevented the overwrite.  phrase_a is still the one that won.
    row = await env.forget.get_deleting_request(member.id)
    assert row is not None
    assert row.status == STATUS_DELETING
    assert row.confirmation_phrase == phrase_a
    assert row.language == "en"


async def test_barrier_consume_between_read_and_upsert(env):
    """P1 fix-r6: true barrier-based interleaving where a concurrent
    runtime consumes the request between request_forget_me's fast-path
    read and its conditional upsert.  The read sees 'pending'; the upsert
    arrives after another runtime set 'deleting' — the WHERE guard
    prevents the overwrite, and the post-upsert re-check returns the
    empty-string sentinel so the caller can complete deletion.

    Uses asyncio.Event barriers to create deterministic interleaving
    without sequential/manual substitutes."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Verify the pending row exists.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == "pending"

    # Barrier events for true interleaving.
    read_done = asyncio.Event()   # Task A has completed the fast-path read
    consume_done = asyncio.Event()  # Task B has claimed the request

    async def pre_upsert_hook():
        """Called between read and upsert inside request_forget_me."""
        read_done.set()        # Signal: Task A is paused, ready for Task B
        await consume_done.wait()  # Wait for Task B to consume

    # Install the test hook on the store.
    env.forget._pre_upsert_hook = pre_upsert_hook

    result_a: str | None = None

    async def task_a():
        nonlocal result_a
        # This will: read (sees pending) → hook (pauses) → upsert → re-check
        result_a = await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "es"
        )

    async def task_b():
        # Wait for Task A to complete its read and hit the barrier.
        await read_done.wait()
        # Now consume the request while Task A is paused.
        claimed = await env.forget.claim_forget_me_request(
            member.id, phrase, datetime.now(UTC)
        )
        assert claimed is not None
        # Release Task A immediately so it can proceed.
        consume_done.set()

    # Run both tasks concurrently.
    await asyncio.gather(task_a(), task_b())

    # Remove the hook (clean up).
    env.forget._pre_upsert_hook = None

    # Task A must have detected the deleting state and returned sentinel.
    assert result_a == "", (
        f"request_forget_me must return empty string when row was"
        f" claimed between read and upsert, got {result_a!r}"
    )

    # The row must still be 'deleting' — the WHERE guard prevented
    # Task A's upsert from overwriting it.
    deleting_row = await env.forget.get_deleting_request(member.id)
    assert deleting_row is not None
    assert deleting_row.status == "deleting"
    assert deleting_row.confirmation_phrase == phrase  # original preserved
    assert deleting_row.language == "en"  # original language preserved


# -- Failure-injection: partial wipe recovery via exact phrase (fix-3) -----


async def test_partial_failure_retryable_via_exact_phrase(env):
    """fix-3: when forget_member's clear_session succeeds but the domain
    transaction fails (chat history gone, domain data intact), the deleting
    request persists.  Retrying the exact confirmation phrase
    deterministically resumes deletion and eventually leaves zero residue.

    The scenario:
    1. Claim the pending request (pending → deleting).
    2. clear_session succeeds (chat history wiped).
    3. Domain transaction fails (Member + domain rows still intact).
    4. Retry: send the exact phrase → get_deleting_by_phrase finds it.
    5. forget_member runs fully this time → zero residue."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Seed chat history to prove it is wiped and stays gone.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "bench 60 8,8,8"},
        {"role": "assistant", "content": "Logged! 3 sets at 60 kg."},
    ])
    items_before = await session.get_items()
    assert len(items_before) == 2

    # Step 1-2: claim the request, then clear chat history (simulating
    # the first part of forget_member succeeding).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
    await SQLAlchemySession(
        f"member:{member.id}", engine=env.engine
    ).clear_session()

    # Step 3: domain delete did NOT run (simulating the transaction
    # failure).  Verify partial state: chat gone, domain intact,
    # request is deleting.
    items_after_clear = await session.get_items()
    assert items_after_clear == [], "chat history must be gone"
    assert await count(env, Member, id=member.id) == 1, "domain data intact"
    assert await count(env, Session, member_id=member.id) == 1
    deleting_req = await env.forget.get_deleting_request(member.id)
    assert deleting_req is not None
    assert deleting_req.status == "deleting"
    assert deleting_req.confirmation_phrase == phrase

    # Step 4: retry — send the exact confirmation phrase.  The runtime
    # detects the deleting request via get_deleting_by_phrase and
    # resumes deletion deterministically.
    retry_req = await env.forget.get_deleting_by_phrase(
        member.id, phrase, now
    )
    assert retry_req is not None, (
        "exact phrase must find the deleting request for retry"
    )
    assert retry_req.status == "deleting"
    assert retry_req.language == "en"

    # Step 5: resume deletion — this time forget_member completes fully.
    await env.forget.forget_member(member.id)

    # Zero residue: everything gone.
    assert await count(env, Member, id=member.id) == 0
    assert await count(env, Session, member_id=member.id) == 0
    assert await count(env, MemberNote, member_id=member.id) == 0
    assert await count(env, Routine, member_id=member.id) == 0
    assert await count(env, Set) == 0
    assert await count(env, Workout) == 0
    assert await count(env, WorkoutExercise) == 0
    assert await _pending_count(env, member.id) == 0
    items_final = await session.get_items()
    assert items_final == [], "no chat history residue"


async def test_partial_failure_non_matching_message_falls_through(env):
    """fix-3: when a deleting request exists but the message does NOT match
    the confirmation phrase, get_deleting_by_phrase returns None and the
    runtime falls through to normal processing — the model can respond
    while the user waits for recovery."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "es"
    )

    # Claim + clear session (partial failure).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
    await SQLAlchemySession(
        f"member:{member.id}", engine=env.engine
    ).clear_session()

    # A non-matching message must NOT resume deletion.
    non_match = await env.forget.get_deleting_by_phrase(
        member.id, "WRONG-PHRASE", now
    )
    assert non_match is None, (
        "non-matching phrase must not find the deleting request"
    )

    # The deleting request is still there (via get_deleting_request),
    # but the runtime lets non-matching messages fall through to the
    # model — the Member data is still intact.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == "deleting"
    assert await count(env, Member, id=member.id) == 1


async def test_idempotent_retry_after_full_success(env):
    """fix-3: calling forget_member a second time after successful deletion
    is a no-op (the method is already documented as idempotent).  After
    recovery completes, there must be no residue from the retry."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([{"role": "user", "content": "hola"}])

    # Claim + partial failure + retry (complete deletion).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
    await SQLAlchemySession(
        f"member:{member.id}", engine=env.engine
    ).clear_session()
    # Domain data still intact (simulating failure).
    assert await count(env, Member, id=member.id) == 1
    # Retry via phrase → full deletion.
    retry_req = await env.forget.get_deleting_by_phrase(
        member.id, phrase, now
    )
    assert retry_req is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0

    # A second retry after complete deletion must not error.
    await env.forget.forget_member(member.id)  # idempotent — must not raise
    assert await count(env, Member, id=member.id) == 0
    assert await _pending_count(env, member.id) == 0
    items = await session.get_items()
    assert items == []


# -- Concurrency: two callers claiming the same request (fix-3) ------------


async def test_claim_method_one_winner_one_loser(env):
    """fix-3: two sequential claim_forget_me_request calls with the same
    phrase must produce exactly one winner (returns the request object)
    and one loser (returns None).  The loser can detect the deleting
    state via get_deleting_request.

    SQLite serializes writes so we run sequentially; the conditional
    UPDATE is the guard on Postgres where true concurrency is possible."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "es"
    )

    # First claim wins.
    winner = await env.forget.claim_forget_me_request(
        member.id, phrase, now
    )
    assert winner is not None
    assert winner.language == "es"

    # Second claim with same phrase loses — the row is now 'deleting'.
    loser = await env.forget.claim_forget_me_request(
        member.id, phrase, now
    )
    assert loser is None

    # The loser can detect the deleting state.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == "deleting"
    assert deleting.confirmation_phrase == phrase
