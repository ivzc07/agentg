"""Member notes — what the Agent learned (docs/design/memory.md, layer 3).

Plain, coach-inspectable rows, written only when a Member volunteers
something durable. Outdated notes are soft-retired, never deleted.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.models import MemberNote

NOTE_KINDS = frozenset({"injury", "preference", "goal", "constraint", "safety", "other"})
MAX_NOTE_LENGTH = 400

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NotesStore:
    def __init__(self, engine: AsyncEngine, clock: Clock = _utcnow) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

    async def remember(self, member_id: int, gym_id: int, kind: str, text: str) -> MemberNote:
        # "safety" is reserved for the flag_to_coach referral path
        # (remember_safety): written any other way it would mark the roster
        # and the banner without any coach ever being pinged. Remap it like
        # any unknown kind.
        if kind == "safety":
            kind = "other"
        return await self._write(member_id, gym_id, kind, text)

    async def remember_safety(self, member_id: int, gym_id: int, text: str) -> MemberNote:
        """The flag_to_coach path's safety Note — the only way a live
        ``safety`` kind is ever written, so a roster marker always means a
        coach was pinged."""
        return await self._write(member_id, gym_id, "safety", text)

    async def _write(self, member_id: int, gym_id: int, kind: str, text: str) -> MemberNote:
        note = MemberNote(
            gym_id=gym_id,
            member_id=member_id,
            kind=kind if kind in NOTE_KINDS else "other",
            text=" ".join(text.split())[:MAX_NOTE_LENGTH],
            created_at=self._clock(),
        )
        async with self._sessions() as db:
            db.add(note)
            await db.commit()
            return note

    async def retire(self, member_id: int, note_id: int) -> MemberNote:
        """Soft-retire: the row stays for the Coach, dated, out of recall."""
        async with self._sessions() as db:
            note = await db.get(MemberNote, note_id)
            if note is None or note.member_id != member_id:
                raise ValueError(
                    f"no note #{note_id} for this member — check the note ids "
                    "in your snapshot, or ask the Member which one they mean"
                )
            note.retired_at = self._clock()
            await db.commit()
            return note

    async def active(self, member_id: int) -> list[MemberNote]:
        async with self._sessions() as db:
            return list(
                await db.scalars(
                    select(MemberNote)
                    .where(MemberNote.member_id == member_id, MemberNote.retired_at.is_(None))
                    .order_by(MemberNote.created_at, MemberNote.id)
                )
            )
