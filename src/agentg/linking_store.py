"""Data access for gym linking (docs/spec.md §Onboarding & gym linking).

Gym provisioning and invite-code regeneration are operational updates in
v1 — no admin UI calls them besides ops scripts and tests. Coach flagging
has one production caller: the coach invite link
(docs/spec-dashboard.md §Access & identity).
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
COACH_CODE_PREFIX = "coach-"


def new_invite_code() -> str:
    """A short random slug, safe inside a t.me deep link and easy to type."""
    return "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))


def new_coach_invite_code() -> str:
    """The coach invite code: a visibly-prefixed slug of its own namespace.

    Member codes never contain "-", so the two namespaces can't collide.
    """
    return COACH_CODE_PREFIX + new_invite_code()


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
                coach_invite_code=new_coach_invite_code(),
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

    async def gym_by_coach_invite_code(self, text: str) -> Gym | None:
        code = normalize_invite_code(text)
        if not code:
            return None
        async with self._sessions() as db:
            return await db.scalar(select(Gym).where(Gym.coach_invite_code == code))

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

    async def members_by_name(self, gym_id: int, name: str) -> list[Member]:
        """Members of a Gym whose name matches (case-insensitive), oldest first.

        Lets a Coach address a Member by name; more than one match is the
        Coach's to disambiguate.
        """
        def norm(value: str) -> str:
            return " ".join(value.split()).lower()

        target = norm(name)
        async with self._sessions() as db:
            members = await db.scalars(
                select(Member).where(Member.gym_id == gym_id).order_by(Member.id)
            )
            return [m for m in members if norm(m.name) == target]

    async def member_in_gym(self, gym_id: int, member_id: int) -> Member | None:
        """A Member by id, scoped to a Gym so a Coach can't reach across gyms."""
        async with self._sessions() as db:
            member = await db.get(Member, member_id)
            return member if member is not None and member.gym_id == gym_id else None

    async def coaches_for_gym(
        self, gym_id: int, exclude_member_id: int | None = None
    ) -> list[tuple[int, str, str, str]]:
        """The Gym's Coaches reachable on a channel, as
        ``(member_id, name, channel, channel_user_id)`` — who a consented
        safety referral gets pinged to."""
        async with self._sessions() as db:
            rows = (
                await db.execute(
                    select(
                        Member.id,
                        Member.name,
                        MemberChannel.channel,
                        MemberChannel.channel_user_id,
                    )
                    .join(MemberChannel, MemberChannel.member_id == Member.id)
                    .where(Member.gym_id == gym_id, Member.is_coach.is_(True))
                )
            ).all()
        return [
            (member_id, name, channel, channel_user_id)
            for member_id, name, channel, channel_user_id in rows
            if member_id != exclude_member_id
        ]

    async def regenerate_invite_code(self, gym_id: int) -> str:
        """The old code stops matching the moment this commits."""
        code = new_invite_code()
        async with self._sessions() as db:
            await db.execute(update(Gym).where(Gym.id == gym_id).values(invite_code=code))
            await db.commit()
        return code

    async def regenerate_coach_invite_code(self, gym_id: int) -> str:
        """The old code stops matching the moment this commits. Coach flags
        live on Members, so regenerating never unflags anyone."""
        code = new_coach_invite_code()
        async with self._sessions() as db:
            await db.execute(update(Gym).where(Gym.id == gym_id).values(coach_invite_code=code))
            await db.commit()
        return code
