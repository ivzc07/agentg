"""Data access for gym linking (docs/spec.md §Onboarding & gym linking).

Gym provisioning, invite-code regeneration, and coach flagging are
operational updates in v1 — no admin UI calls these besides ops scripts
and tests.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.models import Base, Gym, Member, MemberChannel

INVITE_CODE_ALPHABET = string.ascii_lowercase + string.digits
INVITE_CODE_LENGTH = 8


def new_invite_code() -> str:
    """A short random slug, safe inside a t.me deep link and easy to type."""
    return "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))


def normalize_invite_code(text: str) -> str:
    return text.strip().lower()


@dataclass(frozen=True)
class LinkedIdentity:
    """A resolved channel identity: the Member it points at, and their Gym."""

    member: Member
    gym: Gym


class LinkingStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def ensure_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def create_gym(
        self, name: str, *, timezone: str = "UTC", weight_unit: str = "kg"
    ) -> Gym:
        async with self._sessions() as db:
            gym = Gym(
                name=name,
                invite_code=new_invite_code(),
                timezone=timezone,
                weight_unit=weight_unit,
            )
            db.add(gym)
            await db.commit()
            return gym

    async def gym_by_invite_code(self, text: str) -> Gym | None:
        code = normalize_invite_code(text)
        if not code:
            return None
        async with self._sessions() as db:
            return await db.scalar(select(Gym).where(Gym.invite_code == code))

    async def identity_for(self, channel: str, channel_user_id: str) -> LinkedIdentity | None:
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(Member, Gym)
                    .join(MemberChannel, MemberChannel.member_id == Member.id)
                    .join(Gym, Member.gym_id == Gym.id)
                    .where(
                        MemberChannel.channel == channel,
                        MemberChannel.channel_user_id == channel_user_id,
                    )
                )
            ).first()
        if row is None:
            return None
        return LinkedIdentity(member=row[0], gym=row[1])

    async def link_member(
        self, gym_id: int, name: str, channel: str, channel_user_id: str
    ) -> Member:
        """Create a Member and point the channel identity at them.

        An identity that already points somewhere is re-pointed (the gym
        switch), leaving the old Member row untouched. The read-then-write on
        the pointer is race-free only because exactly one replica runs (spec
        §Hosting) and the runtime serializes turns per identity.
        """
        async with self._sessions() as db:
            member = Member(gym_id=gym_id, name=name)
            db.add(member)
            await db.flush()
            pointer = await db.scalar(
                select(MemberChannel).where(
                    MemberChannel.channel == channel,
                    MemberChannel.channel_user_id == channel_user_id,
                )
            )
            if pointer is None:
                db.add(
                    MemberChannel(
                        gym_id=gym_id,
                        member_id=member.id,
                        channel=channel,
                        channel_user_id=channel_user_id,
                    )
                )
            else:
                pointer.member_id = member.id
                pointer.gym_id = gym_id
            await db.commit()
            return member

    async def set_coach(self, member_id: int, is_coach: bool = True) -> None:
        async with self._sessions() as db:
            await db.execute(update(Member).where(Member.id == member_id).values(is_coach=is_coach))
            await db.commit()

    async def regenerate_invite_code(self, gym_id: int) -> str:
        """The old code stops matching the moment this commits."""
        code = new_invite_code()
        async with self._sessions() as db:
            await db.execute(update(Gym).where(Gym.id == gym_id).values(invite_code=code))
            await db.commit()
        return code
