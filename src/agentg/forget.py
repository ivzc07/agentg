"""Forget-me: a Member's complete hard delete (spec §Privacy & data retention).

A Member asks in chat, confirms once, and everything about them is wiped from
all three stores — domain rows, member notes, and the SDK conversation
session. No grace period, no anonymized residue. Member-initiated only.
"""

from __future__ import annotations

from agents.extensions.memory import SQLAlchemySession
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.models import (
    Member,
    MemberChannel,
    MemberNote,
    Routine,
    Session,
    Set,
    Workout,
    WorkoutExercise,
)


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
            await db.execute(delete(MemberChannel).where(MemberChannel.member_id == member_id))
            await db.execute(delete(Member).where(Member.id == member_id))
            await db.commit()
