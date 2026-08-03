"""Forget-me: a Member's complete hard delete (spec §Privacy & data retention).

A Member asks in chat, confirms once, and everything about them is wiped from
all three stores — domain rows, member notes, and the SDK conversation
session. No grace period, no anonymized residue. Member-initiated only.
"""

from __future__ import annotations

from agents.extensions.memory import SQLAlchemySession
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.models import (
    DashboardLoginToken,
    Member,
    MemberChannel,
    MemberNote,
    Routine,
    SafetyOutboxJob,
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
            # Lock the Member row to serialize with outbox delivery —
            # delivery's _authorized_send locks the Coach Member row,
            # MemberChannel, Note, and job rows.  Locking the same narrow
            # rows here guarantees no notification can send after the
            # note/job are deleted (P1 #1 r6, P1 r8).
            member = await db.get(Member, member_id)
            if member is not None:
                await db.execute(
                    select(Member).where(Member.id == member_id).with_for_update()
                )
                # Lock MemberChannel rows for this Member — serializes
                # with _authorized_send's MemberChannel lock.
                await db.execute(
                    select(MemberChannel)
                    .where(MemberChannel.member_id == member_id)
                    .with_for_update()
                )

            await db.execute(delete(Set).where(Set.session_id.in_(member_session_ids)))
            await db.execute(delete(Session).where(Session.member_id == member_id))
            await db.execute(
                delete(WorkoutExercise).where(WorkoutExercise.workout_id.in_(member_workout_ids))
            )
            await db.execute(delete(Workout).where(Workout.routine_id.in_(member_routine_ids)))
            await db.execute(delete(Routine).where(Routine.member_id == member_id))
            # Explicitly fail pending/sending outbox jobs before deleting
            # Notes so an in-flight worker delivery with retained note text
            # can never send after forget-me (P1 #1).  The cascade delete on
            # note_id would remove them anyway, but the explicit fail gives
            # a clear reason and guards the race window.
            await db.execute(
                update(SafetyOutboxJob)
                .where(
                    SafetyOutboxJob.note_id.in_(
                        select(MemberNote.id).where(
                            MemberNote.member_id == member_id
                        )
                    ),
                    SafetyOutboxJob.status.in_(["pending", "sending"]),
                )
                .values(
                    status="failed",
                    failure_reason="member data deleted (forget-me)",
                    last_error="member data deleted (forget-me)",
                )
            )
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
            await db.execute(delete(MemberChannel).where(MemberChannel.member_id == member_id))
            await db.execute(delete(Member).where(Member.id == member_id))
            await db.commit()
