"""Forget-me: a Member's complete hard delete (spec §Privacy & data retention).

A Member asks in chat, the system persists an expiring confirmation, and
only the exact confirmation phrase in a later private message triggers the
wipe — deterministic, model-free, two-turn (issue #212).
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

from agents.extensions.memory import SQLAlchemySession
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.models import (
    DashboardLoginToken,
    ForgetMeRequest,
    Member,
    MemberChannel,
    MemberNote,
    Routine,
    Session,
    Set,
    Workout,
    WorkoutExercise,
)

# Forget-me trigger phrases grouped by language so the deterministic
# reply language can be derived from the raw text that started the flow
# (ADR-0002: mirror the Member, default Spanish when no signal).
_FORGET_ME_TRIGGERS_EN: tuple[str, ...] = (
    "forget me",
    "delete my data",
    "delete my account",
    "delete my info",
    "erase my data",
    "erase me",
)

_FORGET_ME_TRIGGERS_ES: tuple[str, ...] = (
    "olvídame",
    "bórrame",
    "elimíname",
    "borra mi cuenta",
    "borrar mi cuenta",
    "borra mis datos",
    "borrar mis datos",
    "elimina mis datos",
    "eliminar mis datos",
    "elimina mi cuenta",
    "eliminar mi cuenta",
    "borra mi información",
    "borrar mi información",
)

# Combined list for backward-compatible is_forget_me_request checks.
_FORGET_ME_TRIGGERS: tuple[str, ...] = _FORGET_ME_TRIGGERS_EN + _FORGET_ME_TRIGGERS_ES


class ForgetStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine)

    async def forget_member(self, member_id: int) -> None:
        """Hard-delete every trace of a Member. Idempotent: a second call on an
        already-forgotten Member is a no-op, not an error.

        The conversation history (the SDK's own tables) is cleared FIRST, then
        the domain rows in one transaction. The two stores can't share a
        transaction, so ordering is the guarantee: if the history clear fails,
        nothing else has run and the still-linked Member can simply retry; the
        Member's channel identity is only removed once the domain delete
        commits, so we never strand orphaned history behind a cold-started id.
        """
        # 1. Conversation history — the most sensitive residue — goes first.
        await SQLAlchemySession(f"member:{member_id}", engine=self.engine).clear_session()

        # 2. Domain rows, atomically. Child rows before parents so foreign keys
        #    never block the delete (Postgres); the channel identity dies last.
        member_session_ids = select(Session.id).where(Session.member_id == member_id)
        member_routine_ids = select(Routine.id).where(Routine.member_id == member_id)
        member_workout_ids = select(Workout.id).where(Workout.routine_id.in_(member_routine_ids))

        async with self._sessions() as db:
            await db.execute(delete(Set).where(Set.session_id.in_(member_session_ids)))
            await db.execute(delete(Session).where(Session.member_id == member_id))
            await db.execute(
                delete(WorkoutExercise).where(WorkoutExercise.workout_id.in_(member_workout_ids))
            )
            await db.execute(delete(Workout).where(Workout.routine_id.in_(member_routine_ids)))
            await db.execute(delete(Routine).where(Routine.member_id == member_id))
            await db.execute(delete(MemberNote).where(MemberNote.member_id == member_id))
            # References OTHER Members' rows hold back: a forgotten Coach's
            # Routine actor stamps stay coach-authored but by nobody (NULL) —
            # the chip degrades to plain "Coach-authored" (issue #91), and
            # the Member delete below can't trip the FK (Postgres would abort
            # the whole wipe).
            await db.execute(
                update(Routine)
                .where(Routine.created_by_member_id == member_id)
                .values(created_by_member_id=None)
            )
            # Same for the safety-flag tick-offs: they stay acknowledged but
            # by nobody (NULL), and the Member's dashboard login tokens die
            # with them — residue-free.
            await db.execute(
                update(MemberNote)
                .where(MemberNote.acknowledged_by_member_id == member_id)
                .values(acknowledged_by_member_id=None)
            )
            await db.execute(
                delete(DashboardLoginToken).where(DashboardLoginToken.member_id == member_id)
            )
            # Pending confirmation dies with the Member; must run before the
            # Member delete so the FK doesn't block.
            await db.execute(
                delete(ForgetMeRequest).where(ForgetMeRequest.member_id == member_id)
            )
            await db.execute(delete(MemberChannel).where(MemberChannel.member_id == member_id))
            await db.execute(delete(Member).where(Member.id == member_id))
            await db.commit()

    # -- Two-turn confirmation (issue #212) -----------------------------------

    async def request_forget_me(
        self,
        member_id: int,
        gym_id: int,
        now: datetime,
        lifetime_seconds: int,
        language: str = "es",
    ) -> str:
        """Persist an expiring confirmation and return the exact phrase the
        Member must send to complete deletion.

        Replaces any prior pending request for this Member atomically.
        ``language`` is the two-letter code detected from the triggering
        message so the confirmation goodbye can mirror the Member.
        """
        phrase = "DELETE-ME-" + secrets.token_hex(3).upper()
        expires_at = now + timedelta(seconds=lifetime_seconds)
        async with self._sessions() as db:
            await db.execute(
                delete(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id
                )
            )
            db.add(
                ForgetMeRequest(
                    member_id=member_id,
                    gym_id=gym_id,
                    confirmation_phrase=phrase,
                    expires_at=expires_at,
                    created_at=now,
                    language=language,
                )
            )
            await db.commit()
        return phrase

    async def get_pending_request(
        self, member_id: int
    ) -> ForgetMeRequest | None:
        """Return the pending confirmation for this Member, or None."""
        async with self._sessions() as db:
            return await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id
                )
            )

    async def cancel_forget_me(self, member_id: int) -> None:
        """Remove any pending confirmation without deleting Member data."""
        async with self._sessions() as db:
            await db.execute(
                delete(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id
                )
            )
            await db.commit()

    async def consume_pending_forget_me(
        self, member_id: int, confirmation_phrase: str, now: datetime
    ) -> bool:
        """Atomically delete the pending request only when the confirmation
        phrase matches and hasn't expired yet.

        A single conditional DELETE is the compare-and-consume primitive:
        two concurrent sessions can't both delete the same row — exactly
        one sees ``rowcount > 0`` and becomes the winner.  The loser
        (wrong phrase, expired, or beaten by a concurrent winner) sees
        zero rows deleted and must not proceed.
        """
        async with self._sessions() as db:
            result = await db.execute(
                delete(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.confirmation_phrase == confirmation_phrase,
                    ForgetMeRequest.expires_at > now,
                )
            )
            await db.commit()
            return result.rowcount > 0


# -- Module-level helpers --------------------------------------------------


def is_forget_me_request(text: str) -> bool:
    """True when *text* looks like a Member asking to be forgotten.

    Matches whole phrases (word boundaries) so ordinary text like
    "forget metal" or "erase message" does not trigger.
    """
    return detect_forget_me_language(text) is not None


def detect_forget_me_language(text: str) -> str | None:
    """Return ``"en"``, ``"es"``, or ``None`` depending on which
    language's trigger phrases appear in *text* (English checked first
    so a mixed message picks English; in practice a Member will use one
    or the other).

    The caller defaults to ``"es"`` when this returns ``None`` — mirror
    the Member, safe-default Spanish (ADR-0002).
    """
    collapsed = " ".join(text.lower().split())
    for trigger in _FORGET_ME_TRIGGERS_EN:
        if re.search(r"\b" + re.escape(trigger) + r"\b", collapsed):
            return "en"
    for trigger in _FORGET_ME_TRIGGERS_ES:
        if re.search(r"\b" + re.escape(trigger) + r"\b", collapsed):
            return "es"
    return None


def normalize_confirmation(text: str) -> str:
    """Normalize raw text for confirmation-phrase comparison: collapse
    whitespace and uppercase so the Member's casing and spacing don't
    matter."""
    return " ".join(text.strip().upper().split())
