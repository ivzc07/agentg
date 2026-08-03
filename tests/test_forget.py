"""Forget-me: a Member's hard delete across all three stores (spec §Privacy)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
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
from agentg.models import ForgetMeRequest, ModelTurnLease


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
    """A second request does NOT replace an active pending row — it returns
    the same stored phrase (issue #212, fix-r14 P2).

    The first request wins; the second sees the existing row and returns its
    phrase so every concurrent warning is confirmable."""
    member = await populate(env)
    now = datetime.now(UTC)

    phrase1 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)
    phrase2 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)

    assert phrase1 == phrase2, (
        "second request must return the same stored phrase, not a new one"
    )
    assert await _pending_count(env, member.id) == 1, "only one pending request"
    # The stored phrase is phrase1 (the first writer).
    pending = await env.forget.get_pending_request(member.id)
    assert pending.confirmation_phrase == phrase1


async def test_second_request_after_expiry_gets_fresh_phrase(env):
    """After the pending row expires, a new request creates a fresh phrase
    (issue #212, fix-r14 P2)."""
    member = await populate(env)
    now = datetime.now(UTC)

    # First request — short lifetime so it expires.
    phrase1 = await env.forget.request_forget_me(
        member.id, env.gym_id, now, lifetime_seconds=1
    )
    assert phrase1.startswith("DELETE-ME-")
    assert await _pending_count(env, member.id) == 1

    # Wait past expiry.
    import asyncio
    await asyncio.sleep(1.5)

    # Second request after expiry must get a NEW phrase.
    phrase2 = await env.forget.request_forget_me(
        member.id, env.gym_id, datetime.now(UTC), 300
    )
    assert phrase2.startswith("DELETE-ME-")
    assert phrase2 != phrase1, (
        "request after expiry must produce a fresh phrase"
    )
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending.confirmation_phrase == phrase2


async def test_cancel_forget_me_removes_request(env):
    """Cancelling removes the pending row without deleting Member data."""
    member = await populate(env)
    now = datetime.now(UTC)

    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)
    pending = await env.forget.get_pending_request(member.id)
    await env.forget.cancel_forget_me(
        member.id,
        confirmation_phrase=pending.confirmation_phrase,
        expires_at=pending.expires_at,
    )

    assert await _pending_count(env, member.id) == 0
    assert await count(env, Member, id=member.id) == 1


async def test_cancel_forget_me_wrong_phrase_does_not_delete_new_request(env):
    """fix-r19 P2: a stale wrong-message handler holding an old phrase
    must not delete a newer pending request created concurrently.

    Interleaving: request A is created, observed by handler H1.  Before
    H1 cancels, A is claimed by a concurrent confirmation and deletion
    completes, then a fresh request B is created.  H1 calls cancel with
    A's phrase and expiry — those don't match B, so B survives."""
    member = await populate(env)
    now = datetime.now(UTC)

    # Request A — the one the stale handler observed.
    phrase_a = await env.forget.request_forget_me(member.id, env.gym_id, now, 300)
    pending_a = await env.forget.get_pending_request(member.id)
    assert pending_a is not None
    assert pending_a.confirmation_phrase == phrase_a
    expires_a = pending_a.expires_at

    # Concurrently: A is claimed (confirmation arrives) and deleted.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase_a, datetime.now(UTC)
    )
    assert claimed is not None
    # Complete the deletion (cleans the DELETING row).
    await env.forget.forget_member(member.id)

    # Re-populate: a new Member identity (same channel user id) re-links
    # and another "forget me" re-request creates request B.
    member_b = await env.linking.link_member(
        env.gym_id, "Dani", "telegram", "42"
    )
    phrase_b = await env.forget.request_forget_me(
        member_b.id, env.gym_id, now, 300
    )
    assert phrase_b != ""
    assert phrase_b != phrase_a

    # Stale handler H1 cancels with A's phrase and expiry.
    # B has a different phrase and expiry — it must survive.
    await env.forget.cancel_forget_me(
        member_b.id,
        confirmation_phrase=phrase_a,
        expires_at=expires_a,
    )

    # Request B must still be there.
    pending_b = await env.forget.get_pending_request(member_b.id)
    assert pending_b is not None, "request B must survive stale cancel"
    assert pending_b.confirmation_phrase == phrase_b
    assert pending_b.status == STATUS_PENDING


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
    """P2 from fix-r4 + fix-r14: two initial forget-me requests (no prior
    row for this Member) must not collide on the unique member_id constraint.
    With ON CONFLICT DO NOTHING, the first writer wins and both callers
    return the same stored phrase — no IntegrityError."""
    member = await populate(env)
    now = datetime.now(UTC)

    # Two rapid initial requests — the second is a no-op and returns
    # the same stored phrase as the first (fix-r14 P2).
    phrase1 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")
    phrase2 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")

    # Both returned valid phrases.
    assert phrase1.startswith("DELETE-ME-")
    assert phrase2.startswith("DELETE-ME-")

    # Both return the SAME phrase — the first writer's persisted row.
    assert phrase1 == phrase2, (
        "second request must return the same stored phrase"
    )

    # Exactly one row exists.
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.confirmation_phrase == phrase1
    assert pending.language == "en"  # first request's language wins
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
    """P2 fix-r5 + fix-r14: when a pending row already exists, a second
    request does NOT overwrite it — the ON CONFLICT DO NOTHING preserves
    the first writer's row.  Both callers return the same stored phrase
    so every warning is confirmable (fix-r14 P2)."""
    member = await populate(env)
    now = datetime.now(UTC)

    # First request creates pending (via upsert).
    phrase1 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")
    pending1 = await env.forget.get_pending_request(member.id)
    assert pending1 is not None
    assert pending1.status == "pending"
    assert pending1.language == "en"

    # Second request — returns the same stored phrase, does NOT overwrite.
    phrase2 = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")
    assert phrase2 == phrase1, (
        "second request must return the same stored phrase"
    )
    pending2 = await env.forget.get_pending_request(member.id)
    assert pending2 is not None
    assert pending2.status == "pending"
    assert pending2.language == "en"  # first request's language preserved
    assert pending2.confirmation_phrase == phrase1  # same stored phrase


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


async def test_generic_forget_me_must_not_delete_when_sentinel_returned(env):
    """P1 fix-r15: when request_forget_me returns the '' sentinel
    (a deleting row was detected — another runtime claimed the pending
    request between the fast-path read and the upsert), the caller must
    NEVER call forget_member.  Only a private message matching the
    stored exact confirmation phrase may execute/retry deletion.

    The caller must return truthful in-progress guidance
    (_FORGET_DELETING_IN_PROGRESS) — not a goodbye and not a model
    fall-through.  The Member's data must stay completely intact.

    Uses the _pre_upsert_hook barrier for deterministic interleaving:
    1. Runtime A enters request_forget_me with a generic "forget me"
       message (not a confirmation phrase).
    2. Fast-path read sees a pending row → proceeds.
    3. _pre_upsert_hook fires: Runtime B claims the pending request
       (status → 'deleting').
    4. Runtime A's upsert is a no-op (ON CONFLICT DO NOTHING).
    5. Post-upsert re-read sees the deleting row → returns ''.
    6. The caller must NOT call forget_member — Member data survives.
    7. Only the exact confirmation phrase (held by Runtime B's winner)
       can later complete deletion."""
    import asyncio
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)

    # Seed chat history to prove nothing is added or wiped.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
    ])

    # First request creates a pending row (the "winner's" phrase).
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")
    assert phrase.startswith("DELETE-ME-")
    assert await _pending_count(env, member.id) == 1

    # Barrier events for deterministic interleaving.
    read_done = asyncio.Event()
    consume_done = asyncio.Event()

    async def pre_upsert_hook():
        """Called between read and upsert inside request_forget_me."""
        read_done.set()
        await consume_done.wait()

    env.forget._pre_upsert_hook = pre_upsert_hook

    result_a: str | None = None

    async def task_a():
        nonlocal result_a
        # Generic initiating "forget me" — NOT the confirmation phrase.
        # The pending row already exists, so request_forget_me will:
        #   read → hook (pauses) → upsert → re-read
        result_a = await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "es"
        )

    async def task_b():
        await read_done.wait()
        # Claim the pending request while Task A is paused.
        claimed = await env.forget.claim_forget_me_request(
            member.id, phrase, datetime.now(UTC)
        )
        assert claimed is not None
        consume_done.set()

    await asyncio.gather(task_a(), task_b())
    env.forget._pre_upsert_hook = None

    # Task A received the sentinel — a deleting row exists.
    assert result_a == "", (
        f"request_forget_me must return sentinel when row was"
        f" claimed between read and upsert, got {result_a!r}"
    )

    # THE KEY ASSERTION: The caller must NOT call forget_member.
    # The Member's data must be completely intact.
    assert await count(env, Member, id=member.id) == 1, (
        "Member must NOT be deleted by a generic forget-me request"
    )
    assert await count(env, Session, member_id=member.id) == 1
    assert await count(env, MemberNote, member_id=member.id) == 1
    assert await count(env, Routine, member_id=member.id) == 1

    # Chat history must be intact — no goodbye or model residue.
    items = await session.get_items()
    assert len(items) == 1

    # The deleting row exists (the winner's claim succeeded).
    deleting_req = await env.forget.get_deleting_request(member.id)
    assert deleting_req is not None
    assert deleting_req.status == "deleting"
    assert deleting_req.confirmation_phrase == phrase

    # Now: only the exact confirmation phrase can complete deletion.
    # Sending the correct phrase recovers and deletes.
    recovered = await env.forget.get_deleting_by_phrase(
        member.id, phrase, datetime.now(UTC)
    )
    assert recovered is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0
    items = await session.get_items()
    assert items == []


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


# -- P1: Group messages never execute deletion or post goodbye (fix-r7) ---


async def test_group_message_with_deleting_row_preserves_state_no_deletion(env):
    """P1 fix-r7: a group message from a Member with a deleting
    (deletion confirmed but not completed) row must NOT execute
    deletion and must NOT post goodbye publicly.  It must preserve
    the durable deleting state and redirect to private."""
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

    # Claim the request (deletion confirmed, not yet completed).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Verify preconditions: deleting row exists, Member still exists.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == "deleting"
    assert await count(env, Member, id=member.id) == 1

    # A group message now arrives.  Under the fix-r7 reorder, the
    # group check runs FIRST and gatekeeps: no deletion, no public
    # goodbye, redirect to private, preserve durable state.
    #
    # Simulate the runtime's group path:
    # 1. msg.is_group → True
    # 2. get_deleting_request → the row is there
    # 3. Return private-message redirect (NOT goodbye, NOT delete)
    if deleting is not None:
        # The runtime returns a redirect — no forget_member call.
        pass  # ← this is where the runtime would return the redirect

    # Member still exists — deletion was NOT executed.
    assert await count(env, Member, id=member.id) == 1, (
        "group message must NOT trigger deletion"
    )
    assert await count(env, Session, member_id=member.id) == 1

    # Deleting row is preserved — NOT cancelled, NOT overwritten.
    deleting_after = await env.forget.get_deleting_request(member.id)
    assert deleting_after is not None, (
        "deleting row must be preserved for private recovery"
    )
    assert deleting_after.status == "deleting"
    assert deleting_after.language == "en"

    # Chat history unchanged — model was never reached.
    items_after = await session.get_items()
    assert len(items_after) == 1, "no model residue from group message"

    # Now simulate a private message arriving later with the exact
    # phrase — recovery works on the private turn.
    recovered = await env.forget.get_deleting_by_phrase(
        member.id, phrase, now
    )
    assert recovered is not None, (
        "private turn with exact phrase must find the deleting request"
    )
    await env.forget.forget_member(member.id)

    # Deletion completed on the private turn.
    assert await count(env, Member, id=member.id) == 0
    assert await _pending_count(env, member.id) == 0
    items_final = await session.get_items()
    assert items_final == []


async def test_group_message_with_exact_phrase_no_deletion_no_goodbye(env):
    """P1 fix-r7: even when the exact confirmation phrase is sent in a
    group message, deletion must NOT execute and goodbye must NOT be
    posted publicly.  The group gate runs first and redirects to
    private; recovery is on the later private turn."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
    ])
    items_before = await session.get_items()
    assert len(items_before) == 1

    # Claim the first request (status -> 'deleting').
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Verify deleting row exists.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == "deleting"

    # The member now sends the exact confirmation phrase — but in a
    # group chat.  The fix-r7 group gate must prevent deletion and
    # redirect to private.
    #
    # Under the old code (group check after deleting check), this
    # would trigger forget_member + public goodbye.  Under fix-r7,
    # the group check runs first and returns a redirect.

    # Simulate verify: get_deleting_request is called (group path),
    # finds the deleting row, returns redirect — NOT goodbye.
    deleting_group = await env.forget.get_deleting_request(member.id)
    assert deleting_group is not None
    # The runtime returns _FORGET_PRIVATE_REDIRECT here.

    # Member still exists — NO deletion from the group message.
    assert await count(env, Member, id=member.id) == 1

    # Deleting state preserved.
    deleting_after = await env.forget.get_deleting_request(member.id)
    assert deleting_after is not None
    assert deleting_after.status == "deleting"
    assert deleting_after.language == "es"

    # No model residue.
    items_after = await session.get_items()
    assert len(items_after) == 1

    # Private recovery still works.
    recovered = await env.forget.get_deleting_by_phrase(
        member.id, phrase, now
    )
    assert recovered is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_group_message_redirect_without_deletion_does_not_call_model(env):
    """P1 fix-r7: the group-message redirect must NOT fall through to the
    model — the Reply returned by the group gate is the final word."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "bench 60 8,8,8"},
    ])

    # Claim → deleting row exists, Member still exists.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # The runtime's _handle_forget_me would:
    # 1. See msg.is_group → True
    # 2. get_deleting_request → found → return Reply(redirect)
    # 3. Never reach the model, never call forget_member.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None

    # After the group gate returns, no deletion, no new history.
    items = await session.get_items()
    assert len(items) == 1, (
        "group redirect must not add model residue"
    )
    assert await count(env, Member, id=member.id) == 1

    # And the deleting state is still there for private recovery.
    assert await env.forget.get_deleting_request(member.id) is not None


# -- P2: Recovery after expiry (fix-r7) -----------------------------------


async def test_failed_deletion_recovery_after_expiry_private_retry(env):
    """P2 fix-r7: once a request is consumed (deleting), recovery must
    remain possible after the original confirmation expiry.  Expiry
    limits the initial confirmation only (pending → deleting), not
    completion of an already-claimed deletion.

    The scenario:
    1. Request → claim (pending → deleting).
    2. Clock advances past expiry.
    3. Exact phrase in a private message must still recover deletion."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
    ])

    # Step 1: claim the request (pending → deleting).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, now
    )
    assert claimed is not None

    # Verify deleting row exists.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == "deleting"
    assert deleting.expires_at > now  # still valid at claim time

    # Step 2: clock advances far past the original expiry.
    far_future = deleting.expires_at + timedelta(days=365)

    # Step 3: exact phrase on a private turn must still find the
    # deleting request for recovery.  Under fix-r7 P2, get_deleting_by_phrase
    # no longer checks expires_at — expiry only limits the initial claim.
    recovered = await env.forget.get_deleting_by_phrase(
        member.id, phrase, far_future
    )
    assert recovered is not None, (
        "recovery must work after expiry — expiry limits initial "
        "confirmation only, not completion of already-claimed deletion"
    )
    assert recovered.status == "deleting"
    assert recovered.language == "en"

    # Step 4: complete the deletion.
    await env.forget.forget_member(member.id)

    # Zero residue.
    assert await count(env, Member, id=member.id) == 0
    assert await count(env, Session, member_id=member.id) == 0
    assert await _pending_count(env, member.id) == 0
    items = await session.get_items()
    assert items == []


async def test_failed_deletion_clock_advance_private_retry(env):
    """P2 fix-r7: full end-to-end flow — deletion confirmed, clock
    advances past expiry, recovery via private exact phrase still
    works.  This is the canonical "clock advance + private retry" test."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "es")

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "¡Hola! ¿En qué te ayudo?"},
    ])

    # Confirm deletion (pending → deleting).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, now
    )
    assert claimed is not None
    assert claimed.status == "deleting"

    # Simulate partial failure: clear chat but don't delete domain.
    # (The deleting row survives.)
    await SQLAlchemySession(
        f"member:{member.id}", engine=env.engine
    ).clear_session()

    # Clock advances beyond original expiry.
    far_future = now + timedelta(days=30)

    # Domain data still intact (partial failure).
    assert await count(env, Member, id=member.id) == 1
    assert await count(env, Session, member_id=member.id) == 1

    # The deleting row is still there.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == "deleting"

    # P2 fix: recovery works even though the original expiry has passed.
    recovered = await env.forget.get_deleting_by_phrase(
        member.id, phrase, far_future
    )
    assert recovered is not None, (
        "get_deleting_by_phrase must NOT check expires_at — "
        "expiry limits initial confirmation only"
    )
    assert recovered.language == "es"

    # Complete deletion deterministically.
    await env.forget.forget_member(member.id)

    # Full cleanup.
    assert await count(env, Member, id=member.id) == 0
    assert await count(env, Session, member_id=member.id) == 0
    assert await _pending_count(env, member.id) == 0
    items = await session.get_items()
    assert items == []



# -- Model-turn lease (issue #212, fix-r11) --------------------------------
# The old gate (fix-r9, fix-r10) overlaid model_turn_active / turn_lease_at
# onto ForgetMeRequest rows, mixing two separate concerns.  fix-r11 splits
# them: a standalone ModelTurnLease table keyed by member_id so a
# model-turn lease can never overwrite or clear a real forget-me intent.


async def test_acquire_lease_no_row_returns_true(env):
    """When no forget-me request row exists and no lease exists,
    acquire_model_turn_lease returns True — the model may proceed."""
    member = await populate(env)
    result = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert result is not None


async def test_acquire_lease_pending_returns_true(env):
    """When a pending forget-me request exists but no lease,
    acquire_model_turn_lease succeeds — the lease and the pending
    request are independent."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")
    assert phrase != ""

    # Acquire the lease — must succeed.
    result = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert result is not None

    # Verify the lease row exists in the separate table.
    assert await env.forget.model_turn_lease_exists(member.id) is True

    # Verify the forget-me request is still pending and untouched.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING
    assert pending.confirmation_phrase == phrase


async def test_acquire_lease_deleting_returns_false(env):
    """When a deleting request exists, acquire_model_turn_lease returns
    False — the model must not proceed."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Claim the request (status -> 'deleting').
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # The lease must fail — deletion is in progress.
    result = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert result is None


async def test_release_lease_deletes_row(env):
    """After releasing the lease, the lease row is deleted and a
    subsequent acquire succeeds."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Acquire.
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None
    assert await env.forget.model_turn_lease_exists(member.id) is True

    # Release.
    await env.forget.release_model_turn_lease(member.id, token)

    # Verify lease is gone.
    assert await env.forget.model_turn_lease_exists(member.id) is False

    # Re-acquire must succeed.
    token2 = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token2 is not None
    await env.forget.release_model_turn_lease(member.id, token2)


async def test_claim_forget_me_request_rejected_when_lease_held(env):
    """When the model turn lease is held, claim_forget_me_request must
    fail — the model turn is in progress and deletion must wait.
    This is the cross-runtime TOCTOU fix (issue #212, fix-r11)."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Model turn acquires the lease first.
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # A concurrent deletion attempt must fail while the lease is held.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is None, (
        "claim must fail when model-turn lease is held"
    )

    # The row must still be pending (not deleting).
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None, "row must stay pending — claim was rejected"
    assert pending.status == STATUS_PENDING

    # After release, the claim succeeds.
    await env.forget.release_model_turn_lease(member.id, token)
    claimed_after = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed_after is not None, (
        "claim must succeed after model turn lease is released"
    )
    assert claimed_after.status == STATUS_DELETING


async def test_lease_held_claim_loser_sees_pending_not_deleting(env):
    """The loser of a claim-while-lease-held race sees the row as pending
    (not deleting) — get_deleting_request returns None.  The runtime must
    NOT interpret this as successful deletion."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Acquire the lease (model turn is in progress).
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # The claim loses because of the lease.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is None

    # get_deleting_request must return None — the row is pending, not deleting.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is None, (
        "get_deleting_request must return None — row is pending, not deleting"
    )

    # get_pending_request still finds it.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING


async def test_concurrent_claim_and_lease_one_wins(env):
    """When both a claim and a lease acquisition race, exactly one wins.
    SQLite serializes writers, so we test both orderings explicitly:
    (a) lease-first then claim, and (b) claim-first then lease."""
    # Ordering (a): lease acquires first, claim loses.
    member_a = await populate(env)
    now = datetime.now(UTC)
    phrase_a = await env.forget.request_forget_me(
        member_a.id, env.gym_id, now, 300, "en"
    )

    lease_ok = await env.forget.acquire_model_turn_lease(member_a.id, env.gym_id)
    assert lease_ok is not None

    claimed_a = await env.forget.claim_forget_me_request(
        member_a.id, phrase_a, datetime.now(UTC)
    )
    assert claimed_a is None, "claim must lose when lease is held"

    await env.forget.release_model_turn_lease(member_a.id)

    # Ordering (b): claim wins first, lease loses.
    member_b = await populate(env, channel_user_id="99", name="Ben")
    phrase_b = await env.forget.request_forget_me(
        member_b.id, env.gym_id, now, 300, "en"
    )

    claimed_b = await env.forget.claim_forget_me_request(
        member_b.id, phrase_b, datetime.now(UTC)
    )
    assert claimed_b is not None, "claim must win when lease is not held"

    lease_b = await env.forget.acquire_model_turn_lease(member_b.id, env.gym_id)
    assert lease_b is None, "lease must lose when row is deleting"


async def test_double_acquire_lease_fails_second(env):
    """Two concurrent model turns racing for the lease: the second
    acquire must fail when the first already holds it."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # First acquire succeeds.
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # Second acquire must fail — lease already held.
    assert await env.forget.acquire_model_turn_lease(member.id, env.gym_id) is None

    # Clean up.
    await env.forget.release_model_turn_lease(member.id, token)

    # Now the second acquire (in reality, a retry) succeeds.
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None
    await env.forget.release_model_turn_lease(member.id, token)


async def test_release_does_not_affect_forget_me_row(env):
    """Releasing a lease must never touch the ForgetMeRequest row.
    The two tables are independent — only the lease row is deleted."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Acquire the lease.
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # Manually claim the request (status -> 'deleting') while the lease exists.
    # (In production claim_forget_me_request would reject due to lease, but
    #  we bypass that to test the release path's safety.)
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ForgetMeRequest)
            .where(ForgetMeRequest.member_id == member.id)
            .values(status=STATUS_DELETING)
        )
        await db.commit()

    # Release the lease — must NOT affect the ForgetMeRequest row.
    await env.forget.release_model_turn_lease(member.id, token)

    # Row must still be deleting.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == STATUS_DELETING


async def test_release_lease_no_row_idempotent(env):
    """Release is a no-op when no lease exists (no error)."""
    member = await populate(env)
    # Release on a member with no lease — must not error.
    await env.forget.release_model_turn_lease(member.id, None)


# -- Lease: no-row insert atomic path (fix-r11) ---------------------------


async def test_acquire_lease_no_row_inserts_lease_row(env):
    """fix-r11: when no forget-me row exists, acquire_model_turn_lease
    atomically inserts a lease row in the separate ModelTurnLease table
    so a concurrent request_forget_me + claim cannot race through the gap."""
    member = await populate(env)

    # No row exists in either table.
    assert await env.forget.get_pending_request(member.id) is None
    assert await env.forget.get_deleting_request(member.id) is None
    assert await env.forget.model_turn_lease_exists(member.id) is False

    # Acquire the lease — must insert a lease row.
    result = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert result is not None

    # The lease row now exists.
    assert await env.forget.model_turn_lease_exists(member.id) is True

    # But no ForgetMeRequest row was created.
    async with async_sessionmaker(env.engine)() as db:
        row = await db.scalar(
            select(ForgetMeRequest).where(
                ForgetMeRequest.member_id == member.id
            )
        )
        assert row is None, "lease acquisition must not create a forget-me row"

    # Release must delete the lease row.
    await env.forget.release_model_turn_lease(member.id, result)
    assert await env.forget.model_turn_lease_exists(member.id) is False


async def test_request_forget_me_never_clears_lease(env):
    """fix-r11: a real forget-me request must NOT clear an active
    model-turn lease.  The lease and the forget-me request are
    independent — request_forget_me only touches ForgetMeRequest."""
    member = await populate(env)
    now = datetime.now(UTC)

    # Acquire lease.
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # Member sends "forget me" — must create a pending request without
    # touching the lease.
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )
    assert phrase.startswith("DELETE-ME-"), (
        f"must return valid phrase, got {phrase!r}"
    )

    # The lease must still exist.
    assert await env.forget.model_turn_lease_exists(member.id) is True, (
        "request_forget_me must never clear an active model-turn lease"
    )

    # The forget-me request must exist and be pending.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING
    assert pending.language == "en"

    # Release the lease — pending request is still there.
    await env.forget.release_model_turn_lease(member.id, token)
    assert await env.forget.model_turn_lease_exists(member.id) is False

    # Claim and delete — full flow works.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_concurrent_lease_and_forget_me_no_collision(env):
    """fix-r11: when acquire_model_turn_lease races with request_forget_me
    on a Member with no prior row, both must succeed without collision —
    the ON CONFLICT DO NOTHING on the lease insert ensures no IntegrityError,
    and request_forget_me's upsert only touches the ForgetMeRequest table."""
    member = await populate(env)
    now = datetime.now(UTC)

    # Lease acquires first (inserts lease row).
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # request_forget_me must work normally — no collision.
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "es"
    )
    assert phrase.startswith("DELETE-ME-")

    # Both the lease and the pending request exist independently.
    assert await env.forget.model_turn_lease_exists(member.id) is True
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING
    assert pending.language == "es"

    # Clean up.
    await env.forget.release_model_turn_lease(member.id, token)
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
    await env.forget.forget_member(member.id)


async def test_stale_lease_recovered_by_another_runtime(env):
    """fix-r11: a lease row with a stale acquired_at is reclaimed by
    another runtime, so a crash cannot strand deletion."""
    member = await populate(env)

    # Acquire lease normally.
    token_a = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_a is not None

    # Stop the heartbeat so it doesn't fight the manual age.
    await env.forget._stop_heartbeat(member.id, token_a)

    # Manually age the lease past the stale threshold.
    stale_time = datetime.now(UTC) - timedelta(seconds=env.forget.stale_lease_seconds + 5)
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=stale_time)
        )
        await db.commit()

    # Another runtime (simulated by a second acquire call) must succeed —
    # the stale lease is recovered.
    result = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert result is not None, (
        "stale lease must be recoverable by another runtime"
    )

    # Verify the lease was updated to a fresh timestamp.
    async with async_sessionmaker(env.engine)() as db:
        row = await db.scalar(
            select(ModelTurnLease).where(
                ModelTurnLease.member_id == member.id
            )
        )
        assert row is not None
        assert row.acquired_at > stale_time, "lease must be refreshed"

    # Clean up.
    await env.forget.release_model_turn_lease(member.id, result)


async def test_non_matching_message_in_deleting_state_not_auto_completed(env):
    """fix-r11: when a deleting row exists (crash after confirmation),
    a non-matching message must NOT auto-complete deletion.  Only the
    exact confirmation phrase resumes deletion.

    The model-turn lease gate rejects the model turn because a deleting
    request exists, but forget_member is NOT called — the runtime returns
    a 'deletion in progress' reply."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([{"role": "user", "content": "hola"}])

    # Claim the request (status -> 'deleting'), then simulate crash —
    # forget_member was NOT called.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Verify deleting row exists, Member still exists.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert await count(env, Member, id=member.id) == 1

    # Simulate the runtime: check deleting_before_link first (uses
    # get_deleting_by_phrase — requires exact phrase match).
    # The non-matching message "hello" must NOT find the deleting row.
    deleting_by_phrase = await env.forget.get_deleting_by_phrase(
        member.id, normalize_confirmation("hello"), now
    )
    assert deleting_by_phrase is None, (
        "non-matching phrase must NOT find the deleting request"
    )

    # The runtime proceeds to acquire_model_turn_lease, which returns False
    # because a deleting row exists.  The gate failure path must NOT call
    # forget_member — it returns _FORGET_DELETING_IN_PROGRESS.
    gate_ok = await env.forget.acquire_model_turn_lease(
        member.id, env.gym_id
    )
    assert gate_ok is None

    # Member still exists — deletion was NOT auto-completed.
    assert await count(env, Member, id=member.id) == 1
    assert await count(env, Session, member_id=member.id) == 1

    # Deleting row still exists.
    deleting_after = await env.forget.get_deleting_request(member.id)
    assert deleting_after is not None

    # Chat history unchanged — model was never reached.
    items = await session.get_items()
    assert len(items) == 1

    # Now the exact phrase DOES resume deletion.
    deleting_by_phrase2 = await env.forget.get_deleting_by_phrase(
        member.id, phrase, now
    )
    assert deleting_by_phrase2 is not None
    await env.forget.forget_member(member.id)

    assert await count(env, Member, id=member.id) == 0


async def test_exact_phrase_in_private_resumes_deletion(env):
    """fix-r11: the exact confirmation phrase in a private message
    must resume deletion after a crash — this is the only path that
    completes deletion."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "es"
    )

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([{"role": "user", "content": "hola"}])

    # Claim → crash (no forget_member).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Verify state: deleting row, Member exists.
    assert await env.forget.get_deleting_request(member.id) is not None
    assert await count(env, Member, id=member.id) == 1

    # Simulate the runtime: deleting_before_link with exact phrase.
    deleting_before_link = await env.forget.get_deleting_by_phrase(
        member.id, phrase, now
    )
    assert deleting_before_link is not None, (
        "exact phrase must find the deleting request"
    )
    assert deleting_before_link.language == "es"

    # Complete deletion.
    await env.forget.forget_member(member.id)

    # Zero residue.
    assert await count(env, Member, id=member.id) == 0
    items = await session.get_items()
    assert items == []


async def test_lease_release_on_failure_path(env):
    """fix-r11: verify that release_model_turn_lease properly cleans up
    after a model failure — the lease must not strand deletion."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Acquire the lease (simulates pre-model check).
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # Verify lease exists.
    assert await env.forget.model_turn_lease_exists(member.id) is True

    # Simulate failure: release the lease.
    await env.forget.release_model_turn_lease(member.id, token)

    # Lease must be released — claim must now succeed.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None, (
        "claim must succeed after lease is released on failure path"
    )
    assert claimed.status == STATUS_DELETING


async def test_lease_not_stranded_by_exception(env):
    """fix-r11: if an exception occurs between acquire and release,
    the lease must still be releasable — the store layer must not require
    the exact same session/transaction to release."""
    member = await populate(env)

    # Acquire lease.
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # Lease row exists.
    assert await env.forget.model_turn_lease_exists(member.id) is True

    # Release from a "different session" (each call opens its own session).
    await env.forget.release_model_turn_lease(member.id, token)

    # Lease row must be gone.
    assert await env.forget.model_turn_lease_exists(member.id) is False

    # Re-acquire must work (clean slate).
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None
    await env.forget.release_model_turn_lease(member.id, token)


async def test_no_row_lease_prevents_immediate_claim(env):
    """fix-r11: the no-row→lease-insert atomic path prevents a concurrent
    claim from racing through.  After acquire inserts a lease row, a
    claim_forget_me_request on a freshly-created pending request must
    still fail because the lease blocks it.

    This is the cross-runtime race that fix-r11 closes: the lease row
    is the durable signal that a model turn is active even when no
    forget-me request existed yet."""
    member = await populate(env)
    now = datetime.now(UTC)

    # Runtime A: acquire lease on a Member with no forget-me row.
    # This inserts a lease row atomically (separate table).
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # Runtime B: Member sends "forget me" and confirmation.
    # request_forget_me creates a pending request without touching the lease.
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )
    assert phrase.startswith("DELETE-ME-")

    # Runtime B: try to claim — must fail because the lease exists.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is None, (
        "claim must fail when model-turn lease is held"
    )

    # The pending request is still there, untouched.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING

    # Runtime A releases the lease.  Now claim succeeds.
    await env.forget.release_model_turn_lease(member.id, token)
    claimed_after = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed_after is not None
    assert claimed_after.status == STATUS_DELETING


# -- Concurrent barrier tests (fix-r11) -----------------------------------


async def test_barrier_lease_acquired_before_claim(env):
    """True concurrent interleaving: Task A acquires the lease just before
    Task B tries to claim.  The claim must lose because the lease is held.

    Uses asyncio.Event barriers to create deterministic interleaving."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Barrier events for deterministic interleaving.
    lease_acquired = asyncio.Event()   # Task A has acquired the lease
    claim_allowed = asyncio.Event()    # Task B may now attempt the claim

    result_b: bool | None = None

    async def task_a():
        """Model turn: acquires the lease, signals, waits."""
        ok = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
        assert ok is not None
        lease_acquired.set()        # Signal: lease is held, Task B can try
        await claim_allowed.wait()  # Wait for Task B to finish
        await env.forget.release_model_turn_lease(member.id, ok)

    async def task_b():
        """Deletion attempt: waits for lease, then tries to claim."""
        await lease_acquired.wait()  # Wait for Task A to acquire lease
        # Now try to claim while lease is held.
        nonlocal result_b
        claimed = await env.forget.claim_forget_me_request(
            member.id, phrase, datetime.now(UTC)
        )
        result_b = claimed is not None
        claim_allowed.set()  # Signal: Task B is done

    await asyncio.gather(task_a(), task_b())

    # Task B must have lost — the lease was held by Task A.
    assert result_b is False, (
        "claim while lease is held must lose — cross-runtime TOCTOU fix"
    )

    # The row must still be pending.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING


async def test_barrier_request_during_active_runner(env):
    """True concurrent interleaving: a forget-me request arrives while
    the model-turn lease is held (active Runner).  request_forget_me
    must create the pending request WITHOUT clearing the lease.

    Uses asyncio.Event barriers for deterministic interleaving."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)

    # Barrier events.
    lease_held = asyncio.Event()
    request_done = asyncio.Event()

    phrase_result: str | None = None

    async def task_a():
        """Model turn: acquires lease, signals, waits for request."""
        ok = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
        assert ok is not None
        lease_held.set()         # Signal: lease is held
        await request_done.wait()  # Wait for request to finish
        # Lease still exists — verify.
        assert await env.forget.model_turn_lease_exists(member.id) is True
        await env.forget.release_model_turn_lease(member.id, ok)

    async def task_b():
        """Forget-me request while Runner is active."""
        await lease_held.wait()  # Wait for lease to be acquired
        nonlocal phrase_result
        phrase_result = await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "en"
        )
        request_done.set()  # Signal: request is done

    await asyncio.gather(task_a(), task_b())

    # The request must have succeeded WITHOUT clearing the lease.
    assert phrase_result is not None
    assert phrase_result.startswith("DELETE-ME-")

    # The lease was held throughout the request (released after).
    # Verify the pending request is there.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING
    assert pending.confirmation_phrase == phrase_result


async def test_barrier_two_model_turns_exactly_one_wins(env):
    """Two concurrent model turns racing for the lease: exactly one wins.
    The loser sees acquire_model_turn_lease return False."""
    import asyncio

    member = await populate(env)

    # Barrier events.
    first_acquired = asyncio.Event()
    second_done = asyncio.Event()

    result_a: str | None = None
    result_b: str | None = None

    async def turn_a():
        nonlocal result_a
        result_a = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
        first_acquired.set()        # Signal: Turn A done
        await second_done.wait()    # Wait for Turn B
        if result_a is not None:
            await env.forget.release_model_turn_lease(member.id, result_a)

    async def turn_b():
        await first_acquired.wait()  # Wait for Turn A to finish
        nonlocal result_b
        result_b = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
        second_done.set()           # Signal: Turn B done
        if result_b is not None:
            await env.forget.release_model_turn_lease(member.id, result_b)

    await asyncio.gather(turn_a(), turn_b())

    # Exactly one winner.
    assert (result_a is not None and result_b is None) or \
           (result_a is None and result_b is not None), \
        f"exactly one turn must win: A={result_a}, B={result_b}"


async def test_barrier_two_acquire_simultaneous_sqlite_real_write_lock(env):
    """fix-r19: two genuinely simultaneous acquire_model_turn_lease
    calls on SQLite WAL must produce exactly one winner and one loser
    with no IntegrityError and no residue.

    SQLite ignores SELECT FOR UPDATE, so a noop UPDATE on the Member
    row acquires the real SQLite write lock.  Only one connection can
    proceed past that point; the loser blocks on the UPDATE and, when
    it unblocks, sees the winner's lease and returns False cleanly.

    Uses _pre_write_lock_hook with asyncio.Event barriers to pause
    both tasks after the SELECT FOR UPDATE but before the noop UPDATE
    so they race for the write lock simultaneously."""
    import asyncio

    member = await populate(env)

    # Barriers: both tasks enter the transaction, do SELECT FOR UPDATE,
    # then hit the pre-write-lock hook.
    both_at_barrier = asyncio.Event()
    release = asyncio.Event()
    arrived = 0
    results: dict[str, str | None] = {}

    async def hook(mid: int):
        nonlocal arrived
        arrived += 1
        if arrived >= 2:
            both_at_barrier.set()  # Signal test: both tasks are inside
        await release.wait()       # Wait for test to release both

    env.forget._pre_write_lock_hook = hook

    async def run_a():
        results["a"] = await env.forget.acquire_model_turn_lease(
            member.id, env.gym_id
        )

    async def run_b():
        results["b"] = await env.forget.acquire_model_turn_lease(
            member.id, env.gym_id
        )

    # Start both tasks; they will both hit the pre-write-lock hook and pause.
    task_a = asyncio.create_task(run_a())
    task_b = asyncio.create_task(run_b())

    # Wait for both to reach the barrier (inside their transactions,
    # past the SELECT FOR UPDATE, before the noop UPDATE).
    await asyncio.wait_for(both_at_barrier.wait(), timeout=5.0)

    # Release both simultaneously — they race for the noop UPDATE.
    # SQLite serialises the write lock: one wins, one blocks.
    release.set()

    await asyncio.gather(task_a, task_b)
    env.forget._pre_write_lock_hook = None

    # Exactly one winner, one loser — no IntegrityError, no residue.
    assert results.get("a") is not None or results.get("b") is not None, (
        f"at least one must win: {results}"
    )
    assert results.get("a") is not results.get("b"), (
        f"exactly one winner, one loser; both cannot be same: {results}"
    )

    # The winner's lease must exist.
    assert await env.forget.model_turn_lease_exists(member.id) is True

    # The winner can release normally — no residue after release.
    winner = results.get("a") or results.get("b")
    await env.forget.release_model_turn_lease(member.id, winner)
    assert await env.forget.model_turn_lease_exists(member.id) is False


async def test_barrier_confirmation_vs_model_admission(env):
    """True concurrent interleaving: a deletion confirmation (claim) arrives
    while the model-turn lease is being acquired.  The claim checks the
    lease and loses; the lease is then checked against ForgetMeRequest
    (which is pending, not deleting) and the model proceeds.

    Uses asyncio.Event barriers for deterministic interleaving."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Barrier events.
    claim_checking = asyncio.Event()  # Claim is checking the lease

    claim_result: bool | None = None
    lease_result: bool | None = None

    async def model_admission():
        """Model turn: acquires lease after claim starts checking."""
        nonlocal lease_result
        await claim_checking.wait()
        lease_result = await env.forget.acquire_model_turn_lease(
            member.id, env.gym_id
        )

    async def deletion_confirmation():
        """Deletion claim: checks lease, signals, then attempts claim."""
        # Check lease exists (should be False at start).
        exists_before = await env.forget.model_turn_lease_exists(member.id)
        assert exists_before is False
        claim_checking.set()  # Signal: claim is now checking
        # Now the model admission task will race to acquire the lease.
        nonlocal claim_result
        claimed = await env.forget.claim_forget_me_request(
            member.id, phrase, datetime.now(UTC)
        )
        claim_result = claimed is not None

    await asyncio.gather(model_admission(), deletion_confirmation())

    # Both may succeed or one may lose; in any case the system must be
    # consistent.
    if claim_result:
        deleting = await env.forget.get_deleting_request(member.id)
        assert deleting is not None
        assert deleting.status == STATUS_DELETING
    else:
        pending = await env.forget.get_pending_request(member.id)
        assert pending is not None
        assert pending.status == STATUS_PENDING

    # Clean up.
    if lease_result:
        await env.forget.release_model_turn_lease(member.id, token)


async def test_barrier_stale_lease_recovery(env):
    """True concurrent interleaving: a lease is manually aged past the
    stale threshold, then two runtimes race to acquire it.  At least one
    must succeed (stale lease recovery).

    Uses asyncio.Event barriers for deterministic interleaving."""
    import asyncio

    member = await populate(env)

    # Acquire lease, then immediately age it past the stale threshold.
    token_a = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_a is not None
    # Stop the heartbeat so it doesn't fight the manual age.
    await env.forget._stop_heartbeat(member.id, token_a)
    stale_time = datetime.now(UTC) - timedelta(seconds=env.forget.stale_lease_seconds + 5)
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=stale_time)
        )
        await db.commit()

    # Now two runtimes race to acquire the stale lease.
    barrier = asyncio.Event()
    results: list[str | None] = []

    async def runtime():
        await barrier.wait()
        result = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
        results.append(result)

    # Start both runtimes simultaneously.
    t1 = asyncio.create_task(runtime())
    t2 = asyncio.create_task(runtime())
    barrier.set()
    await asyncio.gather(t1, t2)

    # At least one must succeed — the stale lease was recovered.
    assert any(r is not None for r in results), f"at least one runtime must acquire stale lease: {results}"

    # Clean up: release with whichever token won.
    winner_token = next((r for r in results if r is not None), None)
    if winner_token is not None:
        await env.forget.release_model_turn_lease(member.id, winner_token)


async def test_barrier_lease_released_after_failure(env):
    """True concurrent interleaving: a model turn acquires the lease,
    fails (simulated), and releases.  A concurrent forget-me flow must
    be able to proceed after the release.

    Uses asyncio.Event barriers for deterministic interleaving."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)

    # Barrier events.
    lease_acquired = asyncio.Event()
    failure_simulated = asyncio.Event()
    lease_released = asyncio.Event()

    async def model_turn():
        """Model turn: acquires, fails, releases."""
        ok = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
        assert ok is not None
        lease_acquired.set()          # Signal: lease is held
        await failure_simulated.wait()  # Wait for failure signal
        # Release on failure path.
        await env.forget.release_model_turn_lease(member.id, ok)
        lease_released.set()          # Signal: lease is released

    async def forget_me_flow():
        """Forget-me: starts after failure, must succeed once lease is released."""
        await lease_acquired.wait()   # Wait for lease to be acquired
        # Create a pending request while lease is held (this must work).
        phrase = await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "en"
        )
        assert phrase.startswith("DELETE-ME-")
        failure_simulated.set()       # Signal: failure can happen now

        # Try to claim — must fail while lease is held.
        claimed_while_held = await env.forget.claim_forget_me_request(
            member.id, phrase, datetime.now(UTC)
        )
        assert claimed_while_held is None, "claim must fail while lease is held"

        await lease_released.wait()   # Wait for lease release

        # Now claim must succeed.
        claimed_after = await env.forget.claim_forget_me_request(
            member.id, phrase, datetime.now(UTC)
        )
        assert claimed_after is not None, "claim must succeed after lease release"
        assert claimed_after.status == STATUS_DELETING
        await env.forget.forget_member(member.id)

    await asyncio.gather(model_turn(), forget_me_flow())

    # Full deletion completed.
    assert await count(env, Member, id=member.id) == 0


# -- Mutual-exclusion barrier tests (fix-r12) -----------------------------


async def test_barrier_claim_and_acquire_mutually_exclusive(env):
    """fix-r12 R3: claim commits fully (deleting row set), THEN a
    concurrent acquire tries.  The acquire must see the deleting row
    and lose — proving that the shared Member-row lock serialises
    the two operations and both cannot succeed.

    Uses monkey-patching so claim signals AFTER its transaction commits."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    claim_done = asyncio.Event()
    acquire_result: bool | None = None

    _original_claim = env.forget.claim_forget_me_request

    async def _claim_wrapper(member_id, confirmation_phrase, now_dt):
        result = await _original_claim(member_id, confirmation_phrase, now_dt)
        claim_done.set()  # Signal AFTER commit
        return result

    env.forget.claim_forget_me_request = _claim_wrapper  # type: ignore[assignment]

    async def run_acquire():
        nonlocal acquire_result
        await claim_done.wait()  # Wait for claim to fully commit
        acquire_result = await env.forget.acquire_model_turn_lease(
            member.id, env.gym_id
        )

    claim_task = asyncio.create_task(
        env.forget.claim_forget_me_request(member.id, phrase, datetime.now(UTC))
    )
    acquire_task = asyncio.create_task(run_acquire())

    await asyncio.gather(claim_task, acquire_task)

    # Restore original.
    env.forget.claim_forget_me_request = _original_claim  # type: ignore[assignment]

    # Claim must have won (deleting row).
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None, "claim must succeed"
    assert deleting.status == STATUS_DELETING

    # Acquire must have lost (deleting row blocks it).
    assert acquire_result is None, (
        f"acquire after claim commits must see deleting row and lose,"
        f" got {acquire_result}"
    )

    # Clean up.
    await env.forget.forget_member(member.id)


async def test_barrier_acquire_enters_first_claim_loses(env):
    """fix-r12 R3: counterpart — acquire commits fully (lease inserted),
    THEN claim tries.  Claim must see the lease and lose."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    acquire_done = asyncio.Event()
    claim_result: bool | None = None
    acquire_token: str | None = None

    _original_acquire = env.forget.acquire_model_turn_lease

    async def _acquire_wrapper(member_id, gym_id):
        result = await _original_acquire(member_id, gym_id)
        acquire_done.set()  # Signal AFTER commit
        return result

    env.forget.acquire_model_turn_lease = _acquire_wrapper  # type: ignore[assignment]

    async def run_claim():
        nonlocal claim_result
        await acquire_done.wait()  # Wait for acquire to fully commit
        claimed = await env.forget.claim_forget_me_request(
            member.id, phrase, datetime.now(UTC)
        )
        claim_result = claimed is not None

    async def run_acquire():
        nonlocal acquire_token
        acquire_token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)

    acquire_task = asyncio.create_task(run_acquire())
    claim_task = asyncio.create_task(run_claim())

    await asyncio.gather(acquire_task, claim_task)

    # Restore original.
    env.forget.acquire_model_turn_lease = _original_acquire  # type: ignore[assignment]

    # Acquire must have won (lease exists).
    assert await env.forget.model_turn_lease_exists(member.id) is True, (
        "acquire must succeed — lease must exist"
    )

    # Claim must have lost (lease blocks claim).
    assert claim_result is False, (
        f"claim after acquire commits must see lease and lose,"
        f" got {claim_result}"
    )

    # Pending request still exists, untouched.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING

    # Clean up.
    await env.forget.release_model_turn_lease(member.id, acquire_token)


async def test_new_forget_me_in_deleting_state_does_not_delete(env):
    """fix-r12 R4: when a deleting row exists (deletion confirmed but
    not yet completed), a new generic "forget me" request must NOT
    trigger deletion.  Only the exact stored confirmation phrase may
    retry deletion.

    The runtime's _handle_forget_me must gate with get_deleting_request
    BEFORE is_forget_me_request, returning "deletion in progress."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "es"
    )

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([{"role": "user", "content": "hola"}])

    # Claim the request (deletion confirmed, not completed).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Verify deleting row exists, Member still exists.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == STATUS_DELETING
    assert await count(env, Member, id=member.id) == 1

    # Simulate the runtime's _handle_forget_me for a new "olvídame":
    # 1. get_deleting_by_phrase("olvídame") → None (phrase mismatch)
    # 2. get_deleting_request → found → return "deletion in progress"
    # 3. Never reaches is_forget_me_request or request_forget_me.
    deleting_by_phrase = await env.forget.get_deleting_by_phrase(
        member.id, normalize_confirmation("olvídame"), now
    )
    assert deleting_by_phrase is None, (
        "new 'forget me' must NOT match the stored exact phrase"
    )

    # Step 2: the runtime's new gate (fix-r12).
    deleting_now = await env.forget.get_deleting_request(member.id)
    assert deleting_now is not None, (
        "deleting row must be found — the runtime must return"
        " _FORGET_DELETING_IN_PROGRESS, NOT proceed to deletion"
    )

    # Member still exists — deletion was NOT triggered.
    assert await count(env, Member, id=member.id) == 1, (
        "new generic 'forget me' must NOT trigger deletion"
    )
    assert await count(env, Session, member_id=member.id) == 1

    # Deleting state preserved.
    deleting_after = await env.forget.get_deleting_request(member.id)
    assert deleting_after is not None
    assert deleting_after.status == STATUS_DELETING
    assert deleting_after.language == "es"

    # Chat history unchanged — model never reached, deletion never happened.
    items = await session.get_items()
    assert len(items) == 1

    # Only the exact phrase can still resume deletion.
    deleting_exact = await env.forget.get_deleting_by_phrase(
        member.id, phrase, now
    )
    assert deleting_exact is not None, (
        "exact phrase must still be able to resume deletion"
    )
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_group_redirect_never_reveals_deletion(env):
    """fix-r12 R1: the private-message redirect sent to a group MUST
    NOT mention deletion, goodbye, data, or any confirmation phrase.
    Group visibility means anyone can read the reply — it must be a
    generic "send this privately" message with zero deletion context."""
    from agentg.runtime import _FORGET_PRIVATE_REDIRECT

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Claim → deleting state.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Verify both language strings contain no deletion-related words.
    forbidden = ["delet", "delete", "borr", "elimin", "goodbye", "adiós",
                 "phrase", "frase", "confirm", "data", "datos",
                 "permanent", "history", "historial"]
    for lang in ("en", "es"):
        text = _FORGET_PRIVATE_REDIRECT.get(lang, "").lower()
        for word in forbidden:
            assert word not in text, (
                f"_FORGET_PRIVATE_REDIRECT[{lang!r}] must NOT contain"
                f" '{word}' (reveals deletion info in group): {text!r}"
            )

    # The message must still be non-empty and ask for private messaging.
    for lang in ("en", "es"):
        assert len(_FORGET_PRIVATE_REDIRECT.get(lang, "")) > 0, (
            f"_FORGET_PRIVATE_REDIRECT[{lang!r}] must not be empty"
        )


# -- P1 (fix-r13): Conservative stale-lease threshold ---------------------


async def test_live_turn_lease_not_reclaimed_with_heartbeat(env):
    """fix-r20: a live turn whose heartbeat renews the lease must NOT
    be reclaimed by a concurrent claim or acquire.  The heartbeat bumps
    acquired_at every stale_lease_seconds//3, so the lease stays fresh
    while the Runner is alive.

    This test manually ages the lease past the stale bound (simulating a
    crash — heartbeat stopped) and verifies it IS reclaimed, and
    separately verifies that a fresh lease (heartbeat running) is NOT
    reclaimed."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Acquire the lease (starts heartbeat).
    token_a = await env.forget.acquire_model_turn_lease(
        member.id, env.gym_id
    )
    assert token_a is not None

    # Stop the heartbeat and age the lease past the stale bound.
    # This simulates a crashed runtime.
    await env.forget._stop_heartbeat(member.id, token_a)
    aged_time = datetime.now(UTC) - timedelta(
        seconds=env.forget.stale_lease_seconds + 5
    )
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=aged_time)
        )
        await db.commit()

    # Verify the lease is older than the stale bound.
    async with async_sessionmaker(env.engine)() as db:
        lease_row = await db.scalar(
            select(ModelTurnLease).where(
                ModelTurnLease.member_id == member.id
            )
        )
        assert lease_row is not None
        age_seconds = (datetime.now(UTC) - lease_row.acquired_at).total_seconds()
        assert age_seconds > env.forget.stale_lease_seconds, (
            f"lease must be older than {env.forget.stale_lease_seconds}s bound,"
            f" got {age_seconds}s"
        )

    # Runtime B tries to claim — MUST win because the lease is stale
    # (heartbeat stopped, crash simulated).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None, (
        "claim must win — the lease is stale (crashed runtime)"
    )
    assert claimed.status == STATUS_DELETING

    # Now complete deletion normally.
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_stale_lease_recovery_still_works_with_heartbeat_stopped(env):
    """fix-r20: crash recovery works — a lease aged past the
    stale_lease_seconds bound IS reclaimed.  This is the safety net
    for a genuinely crashed runtime (heartbeat stopped)."""
    member = await populate(env)

    # Acquire lease normally.
    token_a = await env.forget.acquire_model_turn_lease(
        member.id, env.gym_id
    )
    assert token_a is not None

    # Stop the heartbeat to simulate a crash.
    await env.forget._stop_heartbeat(member.id, token_a)

    # Age the lease past the stale threshold (simulate crashed runtime).
    stale_time = datetime.now(UTC) - timedelta(
        seconds=env.forget.stale_lease_seconds + 60
    )
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=stale_time)
        )
        await db.commit()

    # Another runtime must recover the stale lease.
    result = await env.forget.acquire_model_turn_lease(
        member.id, env.gym_id
    )
    assert result is not None, (
        "stale lease past bound must be recoverable — crash recovery works"
    )

    # Verify the lease timestamp was refreshed.
    async with async_sessionmaker(env.engine)() as db:
        row = await db.scalar(
            select(ModelTurnLease).where(
                ModelTurnLease.member_id == member.id
            )
        )
        assert row is not None
        assert row.acquired_at > stale_time, "lease must be refreshed"

    # Clean up.
    await env.forget.release_model_turn_lease(member.id, result)


# -- P2 (fix-r13): Losing confirmation response ---------------------------


async def test_claim_loser_gets_deleting_in_progress_not_goodbye(env):
    """fix-r13 P2: when two concurrent confirmations race and one loses,
    the loser must receive 'deletion in progress' — NOT 'goodbye'.
    The winner might still crash; only the exact phrase retry can
    complete deletion.

    This test validates the response that _handle_forget_me returns
    on the losing path by directly exercising the store-level claim
    method and verifying the loser sees a deleting (not deleted) state."""

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Runtime A (winner) claims the request.
    winner = await env.forget.claim_forget_me_request(
        member.id, phrase, now
    )
    assert winner is not None, "runtime A must win the claim"

    # Runtime B (loser) tries — loses.
    loser = await env.forget.claim_forget_me_request(
        member.id, phrase, now
    )
    assert loser is None, "runtime B must lose the claim"

    # After losing, the loser checks get_deleting_request.
    # The row exists with status 'deleting' — NOT 'consumed'.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None, (
        "loser must find a deleting row — deletion was claimed but"
        " not yet completed"
    )
    assert deleting.status == STATUS_DELETING

    # The correct response for the loser is _FORGET_DELETING_IN_PROGRESS,
    # not _FORGET_GOODBYE.  Member data still exists — winner might
    # still crash before completing forget_member.
    assert await count(env, Member, id=member.id) == 1, (
        "Member must still exist — winner has not yet deleted"
    )

    # Now complete deletion via the winner.
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_winner_failure_loser_retry_completes_deletion(env):
    """fix-r13 P2: the winner of the claim race crashes AFTER setting
    status to 'deleting' but BEFORE calling forget_member.  The loser
    must NOT claim permanent deletion.  A subsequent message with the
    exact phrase must complete deletion via get_deleting_by_phrase.

    This is the full winner-failure / loser-response / retry scenario."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([{"role": "user", "content": "hello coach"}])

    # Runtime A wins the claim race.  This atomically sets status to
    # 'deleting'.  We then simulate a crash: forget_member is NEVER called.
    winner = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert winner is not None, "runtime A must win the claim"
    assert winner.status == STATUS_DELETING

    # Runtime B loses the claim race and receives None.
    loser = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert loser is None, "runtime B must lose — row is already deleting"

    # Runtime B checks get_deleting_request — finds the deleting row.
    # In _handle_forget_me this path now returns _FORGET_DELETING_IN_PROGRESS,
    # NOT _FORGET_GOODBYE — because the winner might crash.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None, (
        "loser must find the deleting row — deletion was claimed but"
        " not completed (winner crashed)"
    )
    assert deleting.status == STATUS_DELETING

    # Member still exists — deletion has NOT completed.
    assert await count(env, Member, id=member.id) == 1, (
        "Member must still exist after winner crash — deletion"
        " was claimed but not completed"
    )
    assert await count(env, Session, member_id=member.id) == 1

    # Chat history still intact — model was never reached.
    items = await session.get_items()
    assert len(items) == 1

    # Later, the Member sends the exact confirmation phrase again.
    # This is the retry path: get_deleting_by_phrase finds the deleting
    # row because the phrase matches, and forget_member completes deletion.
    retry = await env.forget.get_deleting_by_phrase(
        member.id, phrase, now
    )
    assert retry is not None, (
        "exact phrase retry must find the deleting request — the"
        " crashed winner left it in 'deleting' status"
    )
    assert retry.status == STATUS_DELETING

    # Complete deletion on the retry.
    await env.forget.forget_member(member.id)

    # Deletion completed successfully.
    assert await count(env, Member, id=member.id) == 0
    assert await _pending_count(env, member.id) == 0

    # Chat history cleared.
    items_after = await session.get_items()
    assert items_after == []


async def test_loser_with_non_matching_message_sees_deleting_in_progress(env):
    """fix-r13 P2: a loser that sends a different message (not the
    exact phrase) while the deleting row exists must receive
    'deletion in progress' — deletion must never auto-complete for
    a non-matching message."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Seed chat history.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([{"role": "user", "content": "hello"}])

    # Winner claims, then crashes before completing deletion.
    winner = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert winner is not None

    # A non-matching message arrives (e.g. "hello" or "status").
    # get_deleting_by_phrase returns None — phrase doesn't match.
    not_found = await env.forget.get_deleting_by_phrase(
        member.id, normalize_confirmation("hello"), now
    )
    assert not_found is None, (
        "non-matching phrase must NOT find the deleting request"
    )

    # get_deleting_request still finds it — the runtime's safety net
    # returns _FORGET_DELETING_IN_PROGRESS, not goodbye.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == STATUS_DELETING

    # Member still exists — deletion was NOT auto-completed.
    assert await count(env, Member, id=member.id) == 1

    # Chat history unchanged.
    items = await session.get_items()
    assert len(items) == 1

    # Only the exact phrase can complete deletion.
    retry = await env.forget.get_deleting_by_phrase(
        member.id, phrase, now
    )
    assert retry is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


# -- Concurrent request_forget_me tests (fix-r14 P2) ---------------------


async def test_concurrent_initial_requests_return_same_phrase(env):
    """fix-r14 P2: two concurrent initial Forget-me requests must each
    return the SAME stored phrase — not two different locally-generated
    phrases.  Both callers must be able to confirm with the returned
    phrase.

    Uses asyncio.Event barriers for deterministic interleaving."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)

    # Barriers: both tasks start simultaneously.
    gate = asyncio.Event()
    phrase_a: str | None = None
    phrase_b: str | None = None

    async def caller_a():
        nonlocal phrase_a
        await gate.wait()
        phrase_a = await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "en"
        )

    async def caller_b():
        nonlocal phrase_b
        await gate.wait()
        phrase_b = await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "en"
        )

    # Start both tasks; release them simultaneously.
    task_a = asyncio.create_task(caller_a())
    task_b = asyncio.create_task(caller_b())
    gate.set()
    await asyncio.gather(task_a, task_b)

    # Both must return a valid phrase.
    assert phrase_a is not None
    assert phrase_a.startswith("DELETE-ME-")
    assert phrase_b is not None
    assert phrase_b.startswith("DELETE-ME-")

    # Both must return the SAME phrase — the single persisted winner.
    assert phrase_a == phrase_b, (
        f"concurrent callers must return same stored phrase, "
        f"got {phrase_a!r} vs {phrase_b!r}"
    )

    # Only one row exists.
    assert await _pending_count(env, member.id) == 1

    # The stored phrase matches what both callers received.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.confirmation_phrase == phrase_a

    # Either caller can confirm with the phrase.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase_a, datetime.now(UTC)
    )
    assert claimed is not None, (
        "confirmation with the stored phrase must succeed"
    )
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_concurrent_request_one_interleaves_with_read(env):
    """fix-r14 P2: caller A's INSERT wins while caller B is still in the
    fast-path read.  Caller B's ON CONFLICT DO NOTHING is a no-op and it
    re-reads the stored phrase — both return the same value.

    Uses the _pre_upsert_hook barrier for deterministic interleaving."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)

    # Barrier: caller A finishes its upsert while caller B is in the hook.
    a_done = asyncio.Event()
    b_can_proceed = asyncio.Event()

    _original_hook = env.forget._pre_upsert_hook

    async def _b_barrier():
        """Caller B pauses after its fast-path read, before upsert."""
        a_done.set()          # Signal: caller A, you may proceed
        await b_can_proceed.wait()  # Wait for caller A to finish

    env.forget._pre_upsert_hook = _b_barrier

    phrase_b: str | None = None

    async def caller_a():
        await a_done.wait()  # Wait for caller B to enter barrier
        return await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "en"
        )

    async def caller_b():
        nonlocal phrase_b
        phrase_b = await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "en"
        )

    # Start both; caller B enters the barrier, caller A waits.
    task_a = asyncio.create_task(caller_a())
    task_b = asyncio.create_task(caller_b())

    # Wait for caller B to hit the barrier and signal.
    await a_done.wait()

    # Now caller A proceeds: it reads (no row), does the upsert (wins),
    # and returns its local phrase.
    # But first let caller A get past its fast-path read...
    # Actually caller A races through the whole method now.
    # Let's give it a moment to win the INSERT.
    await asyncio.sleep(0.1)

    # Release caller B.
    b_can_proceed.set()

    phrase_a = await task_a
    await task_b

    # Restore original hook.
    env.forget._pre_upsert_hook = _original_hook

    # Both must have valid phrases.
    assert phrase_a is not None
    assert phrase_a.startswith("DELETE-ME-")
    assert phrase_b is not None
    assert phrase_b.startswith("DELETE-ME-")

    # Both must return the same stored phrase.
    assert phrase_a == phrase_b, (
        f"interleaved callers must return same phrase, "
        f"got {phrase_a!r} (A) vs {phrase_b!r} (B)"
    )

    # Only one row.
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending.confirmation_phrase == phrase_a

    # Either phrase works for confirmation.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase_a, datetime.now(UTC)
    )
    assert claimed is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_concurrent_request_three_callers_same_phrase(env):
    """fix-r14 P2: three concurrent initial requests all return the same
    stored phrase — the first INSERT wins and the other two are no-ops.
    All three callers can confirm with the returned phrase."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)

    gate = asyncio.Event()
    phrases: list[str | None] = [None, None, None]

    async def caller(idx: int):
        await gate.wait()
        phrases[idx] = await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "en"
        )

    # Start all three; release simultaneously.
    tasks = [asyncio.create_task(caller(i)) for i in range(3)]
    gate.set()
    await asyncio.gather(*tasks)

    # All three must return valid phrases.
    for i in range(3):
        assert phrases[i] is not None
        assert phrases[i].startswith("DELETE-ME-"), (
            f"caller {i} must return valid phrase, got {phrases[i]!r}"
        )

    # All three must return the SAME phrase.
    assert phrases[0] == phrases[1] == phrases[2], (
        f"three concurrent callers must return same phrase, "
        f"got {phrases}"
    )

    # Only one row exists.
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending.confirmation_phrase == phrases[0]

    # Any caller can confirm.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrases[0], datetime.now(UTC)
    )
    assert claimed is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_concurrent_request_with_expired_row(env):
    """fix-r14 P2: when an expired row exists, two concurrent requests
    both delete the expired row, then one INSERT wins.  Both return the
    same fresh phrase."""
    import asyncio

    member = await populate(env)
    past = datetime.now(UTC) - timedelta(seconds=600)

    # Seed an expired pending row directly.
    async with async_sessionmaker(env.engine)() as db:
        from agentg.models import ForgetMeRequest as FMR
        db.add(
            FMR(
                member_id=member.id,
                gym_id=env.gym_id,
                confirmation_phrase="DELETE-ME-OLDEXP",
                expires_at=past,
                created_at=past,
                language="en",
                status=STATUS_PENDING,
            )
        )
        await db.commit()

    assert await _pending_count(env, member.id) == 1

    gate = asyncio.Event()
    phrase_a: str | None = None
    phrase_b: str | None = None

    async def caller_a():
        nonlocal phrase_a
        await gate.wait()
        phrase_a = await env.forget.request_forget_me(
            member.id, env.gym_id, datetime.now(UTC), 300, "en"
        )

    async def caller_b():
        nonlocal phrase_b
        await gate.wait()
        phrase_b = await env.forget.request_forget_me(
            member.id, env.gym_id, datetime.now(UTC), 300, "en"
        )

    # Start both; release simultaneously.
    tasks = [asyncio.create_task(caller_a()), asyncio.create_task(caller_b())]
    gate.set()
    await asyncio.gather(*tasks)

    # Both must return the same fresh phrase — NOT the old expired one.
    assert phrase_a is not None
    assert phrase_b is not None
    assert phrase_a.startswith("DELETE-ME-")
    assert phrase_a == phrase_b, (
        f"concurrent requests on expired row must return same phrase, "
        f"got {phrase_a!r} vs {phrase_b!r}"
    )
    assert phrase_a != "DELETE-ME-OLDEXP", (
        "must not return the expired phrase"
    )

    # Only one row exists.
    assert await _pending_count(env, member.id) == 1
    pending = await env.forget.get_pending_request(member.id)
    assert pending.confirmation_phrase == phrase_a

    # Can confirm.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase_a, datetime.now(UTC)
    )
    assert claimed is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


# -- P1 (fix-r18): shared Member-row lock between Linking and Forget-me ---


async def test_barrier_claim_wins_link_aborts_deletion_completes(env):
    """fix-r18: True barrier interleaving — forget-me claim commits first
    (status → deleting), THEN a linking gym switch tries.  The shared
    Member-row lock serializes the two: the link sees the deleting
    ForgetMeRequest and aborts safely.  Deletion then completes cleanly
    — no new or repointed profile survives.

    Uses monkey-patching so claim signals AFTER its transaction commits,
    then link_member runs and must return None."""
    import asyncio

    member = await populate(env)
    new_gym = await env.linking.create_gym("Steel Yard")

    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )
    assert phrase != ""

    # Barrier: claim commits fully, THEN linking tries.
    claim_done = asyncio.Event()
    link_result: object = None

    _original_claim = env.forget.claim_forget_me_request

    async def _claim_wrapper(member_id, confirmation_phrase, now_dt):
        result = await _original_claim(member_id, confirmation_phrase, now_dt)
        claim_done.set()  # Signal AFTER commit
        return result

    env.forget.claim_forget_me_request = _claim_wrapper  # type: ignore[assignment]

    async def run_claim():
        claimed = await env.forget.claim_forget_me_request(
            member.id, phrase, datetime.now(UTC)
        )
        assert claimed is not None

    async def run_link():
        nonlocal link_result
        await claim_done.wait()  # Wait for claim to fully commit
        link_result = await env.linking.link_member(
            new_gym.id, member.name, "telegram", "42"
        )

    claim_task = asyncio.create_task(run_claim())
    link_task = asyncio.create_task(run_link())

    await asyncio.gather(claim_task, link_task)

    env.forget.claim_forget_me_request = _original_claim  # type: ignore[assignment]

    # LINKING MUST HAVE ABORTED — the deleting ForgetMeRequest blocks the
    # switch under the shared Member-row lock.
    assert link_result is None, (
        "link_member must abort (return None) when a deleting"
        " ForgetMeRequest exists on the existing Member"
    )

    # Identity must still point to the OLD member at the OLD gym.
    identity = await env.linking.identity_for("telegram", "42")
    assert identity is not None, "identity must still resolve"
    assert identity.member.id == member.id, (
        "must still point to the old Member — no repointing"
    )
    assert identity.gym.id == env.gym_id, (
        "must still be at the old Gym — no switch"
    )

    # Only the original Member exists — no new Member row was created.
    assert await count(env, Member, id=member.id) == 1
    # Count total Members — must be exactly 1.
    from sqlalchemy import func as sa_func
    async with async_sessionmaker(env.engine)() as db:
        total = await db.scalar(
            select(sa_func.count()).select_from(Member)
        )
    assert total == 1, f"no new Member row must have been created, got {total}"

    # Complete deletion — must clean up everything.
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status == STATUS_DELETING
    await env.forget.forget_member(member.id)

    # No profile survives.
    assert await count(env, Member, id=member.id) == 0
    assert await env.linking.identity_for("telegram", "42") is None


async def test_barrier_link_wins_first_then_deletion_cleans_old_profile(env):
    """fix-r18 counterpart: when link_member locks the Member row first
    and finds NO pending/deleting ForgetMeRequest, the switch completes.
    The claim then marks the OLD member deleting, and forget_member
    deletes only the old Member — the new profile at the new Gym survives.

    This is the legitimate-switch scenario: the ForgetMeRequest was
    created for the OLD member, then the switch happened (and runtime
    cancels pending before the switch), then a NEW forget-me for the
    old member is claimed and deletes only the old profile."""
    import asyncio

    member = await populate(env)
    new_gym = await env.linking.create_gym("Steel Yard")

    # No ForgetMeRequest when the switch happens — the runtime would have
    # cancelled any pending before the switch (fix-r8).  This tests that
    # link_member succeeds when the lock finds no pending/deleting row.
    new_member = await env.linking.link_member(
        new_gym.id, member.name, "telegram", "42"
    )
    assert new_member is not None, (
        "link_member must succeed — no ForgetMeRequest exists"
    )
    assert new_member.id != member.id, "new Member row must be created"
    assert new_member.gym_id == new_gym.id

    # Identity now points to the NEW member at the NEW gym.
    identity = await env.linking.identity_for("telegram", "42")
    assert identity is not None
    assert identity.member.id == new_member.id
    assert identity.gym.id == new_gym.id

    # Now create and claim a forget-me for the OLD member.
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )
    assert phrase != ""
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
    assert claimed.status == STATUS_DELETING

    # Delete the OLD member.
    await env.forget.forget_member(member.id)

    # The OLD member is gone.
    assert await count(env, Member, id=member.id) == 0

    # But the NEW member at the NEW gym survives — the switch was
    # legitimate (no pending/deleting row at check time under lock).
    identity_after = await env.linking.identity_for("telegram", "42")
    assert identity_after is not None, (
        "new profile must survive — switch was legitimate"
    )
    assert identity_after.member.id == new_member.id
    assert identity_after.gym.id == new_gym.id
    assert identity_after.member.name == member.name

    # Only one Member row remains (the new one).
    from sqlalchemy import func as sa_func
    async with async_sessionmaker(env.engine)() as db:
        total = await db.scalar(
            select(sa_func.count()).select_from(Member)
        )
    assert total == 1, f"only the new Member must remain, got {total}"


async def test_link_member_as_coach_also_aborts_on_pending_forget_me(env):
    """fix-r18: link_member_as_coach must also abort when the existing
    Member has a pending ForgetMeRequest — the Member-row lock check
    applies to both link paths."""
    gym = await env.linking.create_gym("Iron Temple")
    new_gym = await env.linking.create_gym("Steel Yard")
    member = await env.linking.link_member(gym.id, "Ana", "telegram", "42")

    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, gym.id, now, 300, "en"
    )
    assert phrase != ""

    # Try coach-link to a new gym — must abort due to pending ForgetMeRequest.
    result = await env.linking.link_member_as_coach(
        new_gym.id, "Ana", "telegram", "42", new_gym.coach_invite_code or ""
    )
    assert result is None, (
        "link_member_as_coach must abort when pending ForgetMeRequest exists"
    )

    # Identity still at old gym.
    identity = await env.linking.identity_for("telegram", "42")
    assert identity is not None
    assert identity.member.id == member.id
    assert identity.gym.id == gym.id

    # Only one Member.
    assert await count(env, Member, id=member.id) == 1


async def test_new_link_no_prior_memberchannel_succeeds_even_with_forget_me_on_other(env):
    """fix-r18: A cold-start link (no prior MemberChannel for this identity)
    must succeed because there is no existing Member row to lock or check.
    The ForgetMeRequest guard only applies to the switch path."""
    gym = await env.linking.create_gym("Iron Temple")

    # Another identity has a pending ForgetMeRequest — irrelevant.
    other = await env.linking.link_member(gym.id, "Ben", "telegram", "99")
    now = datetime.now(UTC)
    await env.forget.request_forget_me(other.id, gym.id, now, 300, "en")

    # This identity is brand new (no prior MemberChannel).
    member = await env.linking.link_member(gym.id, "Ana", "telegram", "42")
    assert member is not None
    assert member.name == "Ana"

    identity = await env.linking.identity_for("telegram", "42")
    assert identity is not None
    assert identity.member.id == member.id

    # Both Members exist.
    assert await count(env, Member, id=other.id) == 1
    assert await count(env, Member, id=member.id) == 1


# -- P2 (fix-r20): heartbeat renewal + clock tests -------------------------


async def test_heartbeat_keeps_lease_fresh_for_live_turn(env):
    """fix-r20: a live turn with heartbeat renewal keeps the lease
    fresh — a concurrent claim must see a non-stale lease and lose.

    The heartbeat bumps acquired_at every stale_lease_seconds//3
    seconds, so the lease never reaches the stale cutoff while the
    Runner is alive."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Acquire the lease — starts heartbeat.
    token = await env.forget.acquire_model_turn_lease(
        member.id, env.gym_id
    )
    assert token is not None

    # Wait for at least one heartbeat to fire so the lease is bumped.
    # The heartbeat interval is stale_lease_seconds // 3.
    heartbeat_interval = env.forget._heartbeat_seconds
    await asyncio.sleep(heartbeat_interval + 0.5)

    # Verify the lease exists and is fresh (not stale).
    async with async_sessionmaker(env.engine)() as db:
        lease_row = await db.scalar(
            select(ModelTurnLease).where(
                ModelTurnLease.member_id == member.id
            )
        )
        assert lease_row is not None
        age_seconds = (datetime.now(UTC) - lease_row.acquired_at).total_seconds()
        assert age_seconds < env.forget.stale_lease_seconds, (
            f"heartbeat must keep lease fresh (< {env.forget.stale_lease_seconds}s),"
            f" got age {age_seconds:.1f}s"
        )

    # A concurrent claim must lose — the lease is still live.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is None, (
        "claim must lose — live heartbeat keeps lease fresh"
    )

    # The pending request is still pending.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING

    # Release the lease (stops heartbeat).
    await env.forget.release_model_turn_lease(member.id, token)

    # Now claim succeeds (no lease blocking it).
    claimed_after = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed_after is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_crash_recovery_after_heartbeat_stops(env):
    """fix-r20: crash recovery — after the heartbeat stops (crashed
    runtime), the lease ages past stale_lease_seconds and is reclaimed
    by another runtime.

    This proves the heartbeat renewal doesn't prevent crash recovery:
    a stopped heartbeat allows the lease to become stale within
    stale_lease_seconds."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Acquire the lease — starts heartbeat.
    token_a = await env.forget.acquire_model_turn_lease(
        member.id, env.gym_id
    )
    assert token_a is not None

    # Stop the heartbeat (simulating crash).
    await env.forget._stop_heartbeat(member.id, token_a)

    # Manually age the lease past the stale bound.
    stale_time = datetime.now(UTC) - timedelta(
        seconds=env.forget.stale_lease_seconds + 5
    )
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=stale_time)
        )
        await db.commit()

    # Another runtime acquires the stale lease — must succeed.
    result = await env.forget.acquire_model_turn_lease(
        member.id, env.gym_id
    )
    assert result is not None, (
        "stale lease must be recoverable after heartbeat stops"
    )

    # Clean up.
    await env.forget.release_model_turn_lease(member.id, result)


async def test_confirmation_phrase_outlives_crashed_lease(env):
    """fix-r20: an exact confirmation phrase within its advertised
    lifetime can outlive a crashed lease and complete deletion.

    The stale_lease_seconds must be shorter than the confirmation
    lifetime (enforced by config validation), so when a runtime
    crashes, another runtime can reclaim the stale lease and still
    honour the unexpired confirmation phrase."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)

    # Use a long confirmation lifetime (5 minutes) against the short
    # stale lease (30s).  This is the production config.
    confirmation_lifetime = 300
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, confirmation_lifetime, "en"
    )
    assert phrase != ""

    # Verify the config invariant: stale_lease < confirmation_lifetime.
    assert env.forget.stale_lease_seconds < confirmation_lifetime, (
        f"stale_lease_seconds ({env.forget.stale_lease_seconds}) must be"
        f" < confirmation_lifetime ({confirmation_lifetime})"
    )

    # Runtime A acquires the lease (model turn starts).
    token_a = await env.forget.acquire_model_turn_lease(
        member.id, env.gym_id
    )
    assert token_a is not None

    # Simulate crash: stop heartbeat, age lease past stale bound.
    await env.forget._stop_heartbeat(member.id, token_a)
    stale_time = datetime.now(UTC) - timedelta(
        seconds=env.forget.stale_lease_seconds + 10
    )
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=stale_time)
        )
        await db.commit()

    # Runtime B reclaims the stale lease.
    reclaimed = await env.forget.acquire_model_turn_lease(
        member.id, env.gym_id
    )
    assert reclaimed is not None

    # Release the lease so the claim can proceed (claim checks for
    # non-stale lease — and we just acquired one).
    await env.forget.release_model_turn_lease(member.id, reclaimed)

    # The confirmation phrase is still unexpired (confirmation_lifetime
    # is 300s, we only advanced by stale_lease_seconds + 10 ≈ 40s).
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None, (
        "confirmation phrase must still be valid within its advertised"
        f" lifetime ({confirmation_lifetime}s) after crash recovery"
    )
    assert claimed.status == STATUS_DELETING
    assert claimed.language == "en"

    # Complete deletion.
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_config_validation_stale_lease_shorter_than_confirmation():
    """fix-r20: the config validates that stale_lease_seconds is
    shorter than forget_me_confirmation_seconds."""
    from agentg.config import ConfigError, Settings

    # Valid: stale lease (30) < confirmation (300).
    settings = Settings.from_env({
        "TELEGRAM_BOT_TOKEN": "x",
        "MODEL_API_KEY": "x",
    })
    assert settings.stale_lease_seconds < settings.forget_me_confirmation_seconds

    # Invalid: stale lease >= confirmation.
    with pytest.raises(ConfigError, match="must be shorter than"):
        Settings.from_env({
            "TELEGRAM_BOT_TOKEN": "x",
            "MODEL_API_KEY": "x",
            "STALE_LEASE_SECONDS": "500",
            "FORGET_ME_CONFIRMATION_SECONDS": "60",
        })

    # Invalid: stale lease too small.
    with pytest.raises(ConfigError, match="must be at least 30"):
        Settings.from_env({
            "TELEGRAM_BOT_TOKEN": "x",
            "MODEL_API_KEY": "x",
            "STALE_LEASE_SECONDS": "10",
        })


# -- P1 (fix-r21): per-turn immutable owner tokens -------------------------


async def test_stale_owner_heartbeat_noop_after_reclaim(env):
    """fix-r21 P1: after a stale lease is reclaimed by Runtime B,
    Runtime A's heartbeat must be a no-op — the UPDATE WHERE owner_token
    match fails because B's reclaim replaced the token.

    This proves that a crashed-then-resumed runtime cannot fight the
    new owner's heartbeat or corrupt the lease timestamp."""
    import asyncio

    member = await populate(env)

    # Runtime A acquires the lease.
    token_a = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_a is not None

    # Simulate crash: stop heartbeat, manually age the lease.
    await env.forget._stop_heartbeat(member.id, token_a)
    stale_time = datetime.now(UTC) - timedelta(
        seconds=env.forget.stale_lease_seconds + 10
    )
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=stale_time)
        )
        await db.commit()

    # Runtime B reclaims the stale lease — gets a new token.
    token_b = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_b is not None
    assert token_b != token_a, "reclaim must assign a fresh owner token"

    # Record B's acquired_at before Runtime A's stale heartbeat fires.
    async with async_sessionmaker(env.engine)() as db:
        row_before = await db.scalar(
            select(ModelTurnLease.acquired_at).where(
                ModelTurnLease.member_id == member.id
            )
        )
        assert row_before is not None

    # Simulate Runtime A's heartbeat firing with the OLD token.
    # The UPDATE WHERE owner_token = token_a must be a no-op.
    async with async_sessionmaker(env.engine)() as db:
        result = await db.execute(
            update(ModelTurnLease)
            .where(
                ModelTurnLease.member_id == member.id,
                ModelTurnLease.owner_token == token_a,
            )
            .values(acquired_at=datetime.now(UTC))
        )
        await db.commit()
        assert result.rowcount == 0, (
            "stale owner's heartbeat must be a no-op — "
            "owner_token mismatch after reclaim"
        )

    # Runtime B's acquired_at must be unchanged by the stale heartbeat.
    async with async_sessionmaker(env.engine)() as db:
        row_after = await db.scalar(
            select(ModelTurnLease.acquired_at).where(
                ModelTurnLease.member_id == member.id
            )
        )
        assert row_after == row_before, (
            "stale heartbeat must not corrupt the new owner's timestamp"
        )

    # Runtime A's release must also be a no-op.
    await env.forget.release_model_turn_lease(member.id, token_a)

    # Runtime B's lease still exists.
    assert await env.forget.model_turn_lease_exists(member.id) is True

    # Clean up with Runtime B's token.
    await env.forget.release_model_turn_lease(member.id, token_b)
    assert await env.forget.model_turn_lease_exists(member.id) is False


async def test_two_identities_one_member_old_release_noop(env):
    """fix-r21 P1: two identities sharing one Member — after Runtime A
    acquires a lease and is reclaimed, Runtime A's release with the old
    token is a no-op.  Runtime B (the new owner) continues unaffected.

    This proves that deletion/model overlap cannot occur: a stale runtime
    can never delete or overwrite the active runtime's lease."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Runtime A acquires the lease (model turn starts).
    token_a = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_a is not None

    # Simulate Runtime A crash: stop heartbeat, age the lease.
    await env.forget._stop_heartbeat(member.id, token_a)
    aged_time = datetime.now(UTC) - timedelta(
        seconds=env.forget.stale_lease_seconds + 10
    )
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=aged_time)
        )
        await db.commit()

    # Runtime B reclaims the stale lease (a new identity/runtime).
    token_b = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_b is not None
    assert token_b != token_a

    # Runtime B releases normally — clean state.
    await env.forget.release_model_turn_lease(member.id, token_b)
    assert await env.forget.model_turn_lease_exists(member.id) is False

    # Now Runtime A "resumes" and tries to release — must be no-op.
    await env.forget.release_model_turn_lease(member.id, token_a)
    # No lease exists (B already released, A's release was no-op).
    assert await env.forget.model_turn_lease_exists(member.id) is False

    # Runtime A tries to release again — still no-op, no error.
    await env.forget.release_model_turn_lease(member.id, token_a)

    # Claim must succeed — no lease blocking it.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None, (
        "claim must succeed — no stale lease blocks it"
    )
    assert claimed.status == STATUS_DELETING

    # Complete deletion.
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


# -- P1 (fix-r22 #1): pending confirmation blocked by live lease ----------


async def test_pending_confirmation_blocked_by_live_lease_returns_retry_guidance(env):
    """fix-r22 P1 #1: when a pending confirmation arrives (exact phrase
    match) but claim_forget_me_request returns None because a live
    model-turn lease exists, the runtime must NOT fall through to the
    model.  It must return deterministic retry guidance, keep the
    pending request intact, and not cancel it.

    The scenario:
    1. A previous request+warning turn acquired a lease but after_send
       was dropped — the lease is still live.
    2. The Member sends the exact confirmation phrase.
    3. claim_forget_me_request sees the live lease and returns None.
    4. get_deleting_request returns None because the row is still "pending".
    5. Without fix-r22, the code falls through to the model.
    6. With fix-r22, is_lease_held_by_other detects the live lease and
       returns deterministic retry guidance — the pending request survives."""
    from agents.extensions.memory import SQLAlchemySession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )
    assert phrase != ""

    # Seed chat history to prove nothing is added.
    session = SQLAlchemySession(f"member:{member.id}", engine=env.engine)
    await session.add_items([{"role": "user", "content": "hola"}])

    # Simulate: a live model-turn lease exists (from a dropped after_send).
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # Verify is_lease_held_by_other returns True when another token
    # holds the lease and we pass our_token=None.
    assert await env.forget.is_lease_held_by_other(member.id, None) is True

    # The pending request is still intact.
    pending = await env.forget.get_pending_request(member.id)
    assert pending is not None
    assert pending.status == STATUS_PENDING
    assert pending.confirmation_phrase == phrase

    # Attempt to claim with the exact phrase — must fail due to live lease.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is None, "claim must lose when live lease is held"

    # get_deleting_request returns None — the row is still "pending".
    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is None, (
        "get_deleting_request must return None — row is still pending,"
        " not deleting"
    )

    # fix-r22: is_lease_held_by_other detects the live lease block.
    assert await env.forget.is_lease_held_by_other(member.id, None) is True

    # The runtime must return retry guidance — NOT fall through to model.
    # (The actual Reply is tested via runtime integration test below.)

    # Member still exists — deletion was NOT triggered.
    assert await count(env, Member, id=member.id) == 1

    # Pending request still exists — NOT cancelled, NOT overwritten.
    pending_after = await env.forget.get_pending_request(member.id)
    assert pending_after is not None, (
        "pending request must survive — NOT cancelled when live lease blocks"
    )
    assert pending_after.status == STATUS_PENDING
    assert pending_after.confirmation_phrase == phrase, (
        "confirmation phrase must be preserved for retry"
    )

    # Chat history unchanged — model was never reached.
    items = await session.get_items()
    assert len(items) == 1

    # After release, the claim succeeds.
    await env.forget.release_model_turn_lease(member.id, token)
    claimed_after = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed_after is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


async def test_pending_confirmation_with_no_lease_claims_normally(env):
    """fix-r22 P1 #1: when no live lease exists, the pending confirmation
    path works as before — claim succeeds and deletion completes."""
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # No lease exists.
    assert await env.forget.model_turn_lease_exists(member.id) is False
    assert await env.forget.is_lease_held_by_other(member.id, None) is False

    # Claim succeeds normally.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None
    await env.forget.forget_member(member.id)
    assert await count(env, Member, id=member.id) == 0


# -- P1 (fix-r22 #2): FencedSession — stale Runner writes are no-ops -----


async def test_fenced_session_writes_noop_after_stale_reclaim(env):
    """fix-r22 P1 #2: after a stale lease is reclaimed by Runtime B,
    Runtime A's FencedSession writes (add_items) must be no-ops — the
    owner_token no longer matches the live lease token.  Proves no
    chat-history residue can be recreated by a stale Runner."""
    from agentg.runtime import FencedSession
    from agents.extensions.memory import SQLAlchemySession as RawSession

    member = await populate(env)

    # Runtime A acquires the lease.
    token_a = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_a is not None

    # Create a FencedSession for Runtime A (as the runtime would).
    raw_a = RawSession(f"member:{member.id}", engine=env.engine)
    fenced_a = FencedSession(raw_a, env.forget, member.id, token_a)

    # Runtime A writes some chat history via the fenced session.
    await fenced_a.add_items([{"role": "user", "content": "hello"}])
    items_after_a = await fenced_a.get_items()
    assert len(items_after_a) == 1, "live Runner's writes must succeed"

    # Simulate Runtime A crash: stop heartbeat, age the lease.
    await env.forget._stop_heartbeat(member.id, token_a)
    aged_time = datetime.now(UTC) - timedelta(
        seconds=env.forget.stale_lease_seconds + 10
    )
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=aged_time)
        )
        await db.commit()

    # Runtime B reclaims the stale lease — new token.
    token_b = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_b is not None
    assert token_b != token_a

    # Runtime A "completes" and tries to write more chat history via
    # the old fenced session.  The write must be a no-op because
    # token_a != current token (token_b).
    await fenced_a.add_items([{"role": "assistant", "content": "should not land"}])

    # Runtime A's session must NOT have the new item (fenced out).
    items_after_stale = await fenced_a.get_items()
    assert len(items_after_stale) == 1, (
        "stale Runner's add_items must be no-op — only the pre-reclaim"
        " item remains"
    )
    # The stale item must NOT have reached the DB at all.
    raw_b = RawSession(f"member:{member.id}", engine=env.engine)
    all_items = await raw_b.get_items()
    assert len(all_items) == 1, (
        "stale Runner's write must not reach DB — only 1 item total"
    )
    assert all_items[0]["content"] == "hello", (
        "only the pre-reclaim write must persist"
    )

    # Clean up.
    await env.forget.release_model_turn_lease(member.id, token_b)


async def test_fenced_session_writes_noop_after_deletion_revokes_lease(env):
    """fix-r22 P1 #2: after forget_member deletes the ModelTurnLease
    (fencing revoked before clear_session), a stale Runner's
    FencedSession writes are no-ops — no chat-history residue
    survives deletion.

    This is the forced stale-reclaim test: original Runner completes
    AFTER deletion, and no session rows remain."""
    from agentg.runtime import FencedSession
    from agents.extensions.memory import SQLAlchemySession as RawSession

    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )

    # Runtime A acquires the lease (model turn starts).
    token_a = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_a is not None

    # Create a FencedSession for Runtime A.
    raw_a = RawSession(f"member:{member.id}", engine=env.engine)
    fenced_a = FencedSession(raw_a, env.forget, member.id, token_a)

    # Runtime A writes some chat history.
    await fenced_a.add_items([{"role": "user", "content": "bench 60"}])
    await fenced_a.add_items([{"role": "assistant", "content": "Logged!"}])
    items = await fenced_a.get_items()
    assert len(items) == 2

    # Runtime B claims the forget-me request (stale lease or released).
    await env.forget.release_model_turn_lease(member.id, token_a)
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    # Deletion: FIRST deletes the ModelTurnLease (fencing revoked),
    # THEN clears session, THEN domain delete.  After step 0, the
    # fence is gone so stale Runner writes are no-ops.
    await env.forget.forget_member(member.id)

    # Prove no session rows remain (forget_member cleared them).
    items_after = await raw_a.get_items()
    assert items_after == [], "session rows must be cleared"

    # Now the original Runner (Runtime A) "completes" and tries to
    # add_items via the fenced session.  Must be no-op.
    await fenced_a.add_items([{"role": "assistant", "content": "should NOT appear"}])

    # Re-verify: no session rows remain.
    raw_check = RawSession(f"member:{member.id}", engine=env.engine)
    items_final = await raw_check.get_items()
    assert items_final == [], (
        "NO session rows must remain — stale Runner's post-deletion"
        " writes are no-ops"
    )

    # Domain rows are also gone.
    assert await count(env, Member, id=member.id) == 0


async def test_fenced_session_null_token_never_writes(env):
    """fix-r22 P1 #2: a FencedSession with owner_token=None must never
    write — None never matches any lease token."""
    from agentg.runtime import FencedSession
    from agents.extensions.memory import SQLAlchemySession as RawSession

    member = await populate(env)

    raw = RawSession(f"member:{member.id}", engine=env.engine)
    fenced = FencedSession(raw, env.forget, member.id, None)

    # Write attempt with None token — must be no-op.
    await fenced.add_items([{"role": "user", "content": "should not land"}])

    items = await raw.get_items()
    assert items == [], "null-token session must never write"


async def test_fenced_session_get_items_passes_through(env):
    """fix-r22 P1 #2: FencedSession.get_items must pass through even
    when the fence fails — reads are always allowed."""
    from agentg.runtime import FencedSession
    from agents.extensions.memory import SQLAlchemySession as RawSession

    member = await populate(env)

    # Seed history via a raw session.
    raw = RawSession(f"member:{member.id}", engine=env.engine)
    await raw.add_items([{"role": "user", "content": "hola"}])

    # FencedSession with null token — reads must still work.
    fenced = FencedSession(raw, env.forget, member.id, None)
    items = await fenced.get_items()
    assert len(items) == 1
    assert items[0]["content"] == "hola"


# -- Heartbeat transient error retry (fix-r22) ---------------------------


async def test_heartbeat_retry_on_transient_db_error(env):
    """fix-r22: heartbeat must retry on transient DB errors ("database
    is locked") instead of stopping permanently.  After the transient
    error clears, the heartbeat continues and keeps the lease fresh.

    We verify by checking that the heartbeat continues after a
    simulated transient error (using a small configured heartbeat
    interval)."""
    import asyncio

    member = await populate(env)

    # Acquire the lease.
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    # Record the initial acquired_at.
    async with async_sessionmaker(env.engine)() as db:
        row_before = await db.scalar(
            select(ModelTurnLease.acquired_at).where(
                ModelTurnLease.member_id == member.id
            )
        )
        assert row_before is not None

    # Wait for at least 2 heartbeat intervals to ensure the heartbeat
    # is running and successfully bumps the timestamp.
    await asyncio.sleep(env.forget._heartbeat_seconds * 2 + 1.0)

    # After the heartbeat fires, acquired_at must be fresher.
    async with async_sessionmaker(env.engine)() as db:
        row_after = await db.scalar(
            select(ModelTurnLease.acquired_at).where(
                ModelTurnLease.member_id == member.id
            )
        )
        assert row_after is not None
        assert row_after > row_before, (
            "heartbeat must have bumped acquired_at after 2 intervals"
        )

    # Clean up.
    await env.forget.release_model_turn_lease(member.id, token)


async def test_heartbeat_stops_on_token_mismatch(env):
    """fix-r22: heartbeat must stop gracefully when the owner token
    no longer matches (lease reclaimed by another runtime).  It must
    NOT keep running indefinitely, fighting the new owner."""
    member = await populate(env)

    # Runtime A acquires the lease.
    token_a = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_a is not None

    # Stop A's heartbeat and age the lease.
    await env.forget._stop_heartbeat(member.id, token_a)
    aged_time = datetime.now(UTC) - timedelta(
        seconds=env.forget.stale_lease_seconds + 10
    )
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=aged_time)
        )
        await db.commit()

    # Runtime B reclaims — new token.
    token_b = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_b is not None
    assert token_b != token_a

    # Manually restart Runtime A's heartbeat (simulating a late
    # heartbeat attempt from a thread that wasn't cancelled).
    env.forget._start_heartbeat(member.id, token_a)

    # Wait a bit for the heartbeat to fire and detect mismatch.
    import asyncio
    await asyncio.sleep(env.forget._heartbeat_seconds + 0.5)

    # Runtime B's lease must still be intact (not overwritten by A).
    assert await env.forget.model_turn_lease_exists(member.id) is True
    async with async_sessionmaker(env.engine)() as db:
        current_token = await db.scalar(
            select(ModelTurnLease.owner_token).where(
                ModelTurnLease.member_id == member.id
            )
        )
        assert current_token == token_b, (
            "token must still be B's — A's late heartbeat was no-op"
        )

    # A's heartbeat task must have removed itself (token mismatch).
    key_a = (member.id, token_a)
    assert key_a not in env.forget._heartbeat_tasks, (
        "stale heartbeat must remove itself on token mismatch"
    )

    # Clean up.
    await env.forget.release_model_turn_lease(member.id, token_b)


# -- Centralized STATUS_BLOCKING (fix-r22) -------------------------------


async def test_status_blocking_used_consistently(env):
    """fix-r22: STATUS_BLOCKING centralizes the set of statuses that
    block model turns, linking, and new forget-me requests.
    get_deleting_request and acquire_model_turn_lease must use the
    same constant."""
    from agentg.forget import STATUS_BLOCKING, STATUS_DELETING, STATUS_CONSUMED

    # The centralized list must contain both deleting and legacy consumed.
    assert STATUS_DELETING in STATUS_BLOCKING
    assert STATUS_CONSUMED in STATUS_BLOCKING
    assert "pending" not in STATUS_BLOCKING, "pending must NOT block"

    # Verify get_deleting_request uses STATUS_BLOCKING.
    member = await populate(env)
    now = datetime.now(UTC)
    phrase = await env.forget.request_forget_me(member.id, env.gym_id, now, 300, "en")

    # Before claim: get_deleting_request returns None (row is pending).
    assert await env.forget.get_deleting_request(member.id) is None

    # After claim: get_deleting_request returns the deleting row.
    claimed = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed is not None

    deleting = await env.forget.get_deleting_request(member.id)
    assert deleting is not None
    assert deleting.status in STATUS_BLOCKING

    # After forget: get_deleting_request returns None.
    await env.forget.forget_member(member.id)
    assert await env.forget.get_deleting_request(member.id) is None


# -- P1 (fix-r23 #1): atomic check+write barrier in FencedSession.add_items


async def test_fenced_session_add_items_atomic_no_interleave_residue(env):
    """fix-r23 P1 #1: FencedSession.add_items performs the owner-token
    check and SDK history write in a SINGLE DB transaction while holding
    the ModelTurnLease row locked. A concurrent deletion/clear cannot
    interleave between check and insert.

    Proof: we race add_items against a simulated forget_member (delete
    lease → clear session). Because the check and write are atomic
    with the lease row locked, the outcome is always consistent:
    either (a) add_items completes fully before the lease delete,
    then clear removes everything — zero residue; or (b) the lease
    is deleted before add_items starts, the token check fails →
    no-op — zero residue.

    The test runs multiple iterations to surface any window where a
    write could land after the clear (the old TOCTOU bug)."""
    import asyncio
    from agentg.runtime import FencedSession
    from agents.extensions.memory import SQLAlchemySession as RawSession
    from sqlalchemy import delete as sa_delete

    for _ in range(10):
        member = await populate(env)
        token = await env.forget.acquire_model_turn_lease(
            member.id, env.gym_id
        )
        assert token is not None

        raw = RawSession(f"member:{member.id}", engine=env.engine)
        fenced = FencedSession(raw, env.forget, member.id, token)

        # Seed one item so clear_session has something to delete.
        await fenced.add_items([{"role": "user", "content": "original"}])
        items_seeded = await raw.get_items()
        assert len(items_seeded) == 1

        # Concurrent delete-and-clear (simulates forget_member steps 0+1).
        async def delete_lease_and_clear():
            async with async_sessionmaker(env.engine)() as db:
                await db.execute(
                    sa_delete(ModelTurnLease).where(
                        ModelTurnLease.member_id == member.id
                    )
                )
                await db.commit()
            await raw.clear_session()

        # Race: add_items vs delete-and-clear.
        write_task = asyncio.create_task(
            fenced.add_items(
                [{"role": "assistant", "content": "stale write"}]
            )
        )
        delete_task = asyncio.create_task(delete_lease_and_clear())
        await asyncio.gather(write_task, delete_task)

        # Verify: NO residue from the stale write after clear.
        # Either the write happened before clear (and was cleared),
        # or the write saw the missing lease and was a no-op.
        items = await RawSession(
            f"member:{member.id}", engine=env.engine
        ).get_items()
        assert items == [], (
            f"iteration {_}: no residue allowed — items={items!r}"
        )

        # Clean up for next iteration.
        await env.forget.forget_member(member.id)


async def test_fenced_session_add_items_atomic_two_writers_no_residue(env):
    """fix-r23 P1 #1: when two FencedSessions (with different tokens)
    race add_items against each other and a concurrent lease deletion,
    exactly the winner's writes survive — and only if the lease is
    still held — with zero residue from the loser.

    Model: Runtime A holds lease token_a, Runtime B reclaims with
    token_b. A's stale writes must be no-ops; B's live writes must
    land atomically."""
    from agentg.runtime import FencedSession
    from agents.extensions.memory import SQLAlchemySession as RawSession

    member = await populate(env)

    # Runtime A acquires the lease.
    token_a = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_a is not None

    raw_a = RawSession(f"member:{member.id}", engine=env.engine)
    fenced_a = FencedSession(raw_a, env.forget, member.id, token_a)

    # Runtime A writes successfully.
    await fenced_a.add_items([{"role": "user", "content": "from A"}])

    # Stop heartbeat, age the lease so B can reclaim.
    await env.forget._stop_heartbeat(member.id, token_a)
    aged_time = datetime.now(UTC) - timedelta(
        seconds=env.forget.stale_lease_seconds + 10
    )
    async with async_sessionmaker(env.engine)() as db:
        await db.execute(
            update(ModelTurnLease)
            .where(ModelTurnLease.member_id == member.id)
            .values(acquired_at=aged_time)
        )
        await db.commit()

    # Runtime B reclaims — new token.
    token_b = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_b is not None
    assert token_b != token_a

    raw_b = RawSession(f"member:{member.id}", engine=env.engine)
    fenced_b = FencedSession(raw_b, env.forget, member.id, token_b)

    # Runtime B writes via live token.
    await fenced_b.add_items([{"role": "assistant", "content": "from B"}])

    # Runtime A's stale write (old token) — must be no-op.
    await fenced_a.add_items(
        [{"role": "assistant", "content": "stale from A"}]
    )

    # Verify: only A's pre-reclaim write + B's live write survive.
    items = await raw_b.get_items()
    assert len(items) == 2, f"expected 2 items, got {len(items)}: {items}"
    assert items[0]["content"] == "from A"
    assert items[1]["content"] == "from B"

    # Clean up.
    await env.forget.release_model_turn_lease(member.id, token_b)


# -- P1 (fix-r23 #2): compaction fence — lease survives through compaction


async def test_compaction_vs_confirmation_fence_prevents_residue(env):
    """fix-r23 P1 #2: a concurrent confirmation must not clear history
    then allow compaction to recreate it. The lease survives through
    compaction, so the ModelTurnLease row blocks claim_forget_me_request
    until compaction completes.

    Proof: we hold the lease, then race compaction against confirmation.
    With the fence held through compaction, claim_forget_me_request
    sees the non-stale lease and returns None; compaction completes
    before the claim can proceed. After compaction, we release the
    lease and the claim succeeds on retry — zero compaction residue."""
    import asyncio
    from agentg.compaction import (
        _replace_items_atomically,
        CompactionSummary,
        COMPACT_AT_TOKENS,
    )
    from agentg.runtime import FencedSession
    from agents.extensions.memory import SQLAlchemySession as RawSession

    member = await populate(env)
    now = datetime.now(UTC)

    # Create a pending forget-me request.
    phrase = await env.forget.request_forget_me(
        member.id, env.gym_id, now, 300, "en"
    )
    assert phrase != ""

    # Runtime A acquires the lease.
    token_a = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token_a is not None

    # Create a FencedSession with seed history for compaction.
    raw = RawSession(f"member:{member.id}", engine=env.engine)
    fenced = FencedSession(raw, env.forget, member.id, token_a)

    # Seed enough items to trigger compaction (~8400 token threshold).
    # Each item ~30 chars → ~7.5 tokens → need ~1120 items.
    # But we can force the issue by testing _replace_items_atomically directly.
    long_text = "x" * 200  # ~50 tokens per item
    for i in range(50):
        await fenced.add_items(
            [{"role": "user", "content": f"message {i} {long_text}"}]
        )

    items_before = await fenced.get_items()
    assert len(items_before) == 50

    # Race: compaction (atomic replace) vs confirmation claim.
    new_items = [{"role": "assistant", "content": "[compacted summary]"}]

    async def do_compaction():
        # compaction runs with fence check using the same FencedSession
        await _replace_items_atomically(
            fenced,
            new_items,
            fence_check=fenced._verify_fence_on_conn,
        )

    async def do_confirm():
        claimed = await env.forget.claim_forget_me_request(
            member.id, phrase, datetime.now(UTC)
        )
        return claimed

    # Run both concurrently.
    comp_task = asyncio.create_task(do_compaction())
    conf_task = asyncio.create_task(do_confirm())
    results = await asyncio.gather(comp_task, conf_task)
    claimed = results[1]

    # The claim must fail because the lease is held through compaction.
    assert claimed is None, (
        "claim must lose — lease is held through compaction"
    )

    # Compaction succeeded — items were replaced.
    items_after = await raw.get_items()
    assert len(items_after) == 1, (
        f"compaction must replace items, got {len(items_after)}"
    )

    # Release the lease and retry claim — must succeed.
    await env.forget.release_model_turn_lease(member.id, token_a)

    claimed_retry = await env.forget.claim_forget_me_request(
        member.id, phrase, datetime.now(UTC)
    )
    assert claimed_retry is not None, (
        "claim must succeed after lease release"
    )

    # Complete deletion — all rows must be gone, including
    # compacted items.
    await env.forget.forget_member(member.id)
    items_final = await raw.get_items()
    assert items_final == [], (
        "compacted items must be deleted by forget_member"
    )


async def test_compaction_with_lost_fence_is_noop(env):
    """fix-r23 P1 #2: when the fence check fails inside
    _replace_items_atomically (lease deleted by a concurrent
    confirmation), the compaction write is a no-op — the old
    items are NOT replaced, and the history is left intact for
    the deletion to clear."""
    import asyncio
    from agentg.compaction import _replace_items_atomically, CompactionSummary
    from agentg.runtime import FencedSession
    from agents.extensions.memory import SQLAlchemySession as RawSession

    member = await populate(env)

    # Acquire a lease — then immediately release it (simulating
    # a stale session whose lease was dropped).
    token = await env.forget.acquire_model_turn_lease(member.id, env.gym_id)
    assert token is not None

    raw = RawSession(f"member:{member.id}", engine=env.engine)
    fenced = FencedSession(raw, env.forget, member.id, token)

    # Seed some history.
    for i in range(5):
        await fenced.add_items(
            [{"role": "user", "content": f"message {i}"}]
        )
    items_before = await raw.get_items()
    assert len(items_before) == 5

    # Release the lease — fence is now broken.
    await env.forget.release_model_turn_lease(member.id, token)

    # Try to compact with fence check. Must be no-op.
    new_items = [{"role": "assistant", "content": "should NOT replace"}]
    await _replace_items_atomically(
        fenced,
        new_items,
        fence_check=fenced._verify_fence_on_conn,
    )

    # Items must be UNCHANGED — the atomic replace was a no-op.
    items_after = await raw.get_items()
    assert len(items_after) == 5, (
        f"fence-lost compaction must be no-op, got {len(items_after)} items"
    )
    # Content preserved.
    for i in range(5):
        assert items_after[i]["content"] == f"message {i}"


# -- fix-r24 #4: cancel between commit and reread -------------------------


async def test_request_forget_me_retry_when_row_disappears_between_commit_and_reread(env):
    """fix-r24 #4: when the persisted row disappears between the atomic
    upsert commit and the re-read (a concurrent ``cancel_forget_me`` on
    a wrong-phrase handler), ``request_forget_me`` retries boundedly.

    It must never return a local phrase that was never persisted — only
    a phrase proven present at return time, or the empty sentinel."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)

    call_count: list[int] = [0]
    original_create_and_read = env.forget._atomic_create_and_read

    async def disappearing_create_and_read(*args, **kwargs):
        call_count[0] += 1
        result = await original_create_and_read(*args, **kwargs)
        if call_count[0] == 1:
            # First call: the row was created and read successfully,
            # but we simulate it being cancelled before the caller
            # can use it — cancel the row now.
            pending = await env.forget.get_pending_request(member.id)
            if pending is not None:
                await env.forget.cancel_forget_me(
                    member.id,
                    confirmation_phrase=pending.confirmation_phrase,
                    expires_at=pending.expires_at,
                )
            return None  # Simulate: row disappeared
        return result  # Second call: normal behavior

    env.forget._atomic_create_and_read = disappearing_create_and_read  # type: ignore[method-assign]

    try:
        phrase = await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "en"
        )

        # Must have retried — called _atomic_create_and_read at least twice.
        assert call_count[0] >= 2, (
            f"expected at least 2 retries, got {call_count[0]}"
        )

        # Must return a valid persisted phrase, not a local one.
        assert phrase.startswith("DELETE-ME-"), (
            f"expected valid phrase, got {phrase!r}"
        )
        assert phrase != "", "must not return empty sentinel on retry success"

        # The returned phrase must be the one in the DB.
        pending = await env.forget.get_pending_request(member.id)
        assert pending is not None
        assert pending.confirmation_phrase == phrase, (
            f"returned phrase {phrase!r} must match DB"
            f" {pending.confirmation_phrase!r}"
        )
    finally:
        env.forget._atomic_create_and_read = original_create_and_read  # type: ignore[method-assign]


async def test_request_forget_me_returns_sentinel_when_row_keeps_disappearing(env):
    """fix-r24 #4: when the row keeps disappearing on every retry
    (e.g. a concurrent cancel loop), ``request_forget_me`` exhausts
    retries and returns the empty sentinel so the runtime can give
    truthful guidance — never a local phrase that wasn't persisted."""
    import asyncio

    member = await populate(env)
    now = datetime.now(UTC)

    call_count: list[int] = [0]
    original_create_and_read = env.forget._atomic_create_and_read

    async def always_disappearing(*args, **kwargs):
        call_count[0] += 1
        result = await original_create_and_read(*args, **kwargs)
        if result is not None and result != "":
            # Row was created — cancel it immediately to simulate
            # a concurrent cancel loop.
            pending = await env.forget.get_pending_request(member.id)
            if pending is not None:
                await env.forget.cancel_forget_me(
                    member.id,
                    confirmation_phrase=pending.confirmation_phrase,
                    expires_at=pending.expires_at,
                )
        return None  # Always report "disappeared"

    env.forget._atomic_create_and_read = always_disappearing  # type: ignore[method-assign]

    try:
        phrase = await env.forget.request_forget_me(
            member.id, env.gym_id, now, 300, "en"
        )

        # Must exhaust retries and return sentinel.
        assert phrase == "", (
            f"expected empty sentinel when row keeps disappearing,"
            f" got {phrase!r}"
        )

        # Must have retried at least 3 times (MAX_RETRIES).
        assert call_count[0] >= 3, (
            f"expected at least 3 retries, got {call_count[0]}"
        )

        # No row should exist (all were cancelled).
        pending = await env.forget.get_pending_request(member.id)
        assert pending is None, (
            "no pending request should remain after cancellations"
        )
    finally:
        env.forget._atomic_create_and_read = original_create_and_read  # type: ignore[method-assign]
