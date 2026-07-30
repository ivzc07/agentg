"""Data access for gym linking (docs/spec.md §Onboarding & gym linking).

Gym provisioning is an operational update in v1 — no admin UI calls it
besides ops scripts and tests. Invite-code regeneration and the gym rename
have their first production caller in the tenant Settings screen
(docs/spec-dashboard.md §Settings); coach flagging has its own: the coach
invite link (docs/spec-dashboard.md §Access & identity).
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass

from sqlalchemy import inspect, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.models import Base, Gym, Member, MemberChannel

INVITE_CODE_ALPHABET = string.ascii_lowercase + string.digits
INVITE_CODE_LENGTH = 8
COACH_CODE_PREFIX = "coach-"
GYM_NAME_MAX_LENGTH = 200  # Gym.name is String(200)


def new_invite_code() -> str:
    """A short random slug, safe inside a t.me deep link and easy to type.

    Always carries at least one digit: the near-miss shape test in linking
    (``_looks_like_invite_code``) uses a digit to tell typed codes from
    ordinary short words, so a digitless code would dead-end its own typos.
    """
    while True:
        code = "".join(secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))
        if any(ch.isdigit() for ch in code):
            return code


def new_coach_invite_code() -> str:
    """The coach invite code: a visibly-prefixed slug of its own namespace.

    Member codes never contain "-", so the two namespaces can't collide.
    """
    return COACH_CODE_PREFIX + new_invite_code()


def normalize_invite_code(text: str) -> str:
    return text.strip().lower()


def _add_missing_columns(conn: Connection) -> None:
    """Schema evolution for deployed databases: ``create_all`` never alters
    existing tables, so columns and indexes added after first deploy are
    applied here, idempotently. (No migration framework — the repo's
    mechanism is this list; add one entry per new column or index on an
    existing table.)"""
    gym_columns = {c["name"] for c in inspect(conn).get_columns("gyms")}
    if "coach_invite_code" not in gym_columns:
        conn.execute(text("ALTER TABLE gyms ADD COLUMN coach_invite_code VARCHAR(64)"))
    gym_indexes = {i["name"] for i in inspect(conn).get_indexes("gyms")}
    if "ix_gyms_coach_invite_code" not in gym_indexes:
        conn.execute(
            text("CREATE UNIQUE INDEX ix_gyms_coach_invite_code ON gyms (coach_invite_code)")
        )
    # Per-Exercise weight reads (issue #99) must not keep scanning on
    # databases that already have a sets table.
    sets_indexes = {i["name"] for i in inspect(conn).get_indexes("sets")}
    if "ix_sets_exercise_id" not in sets_indexes:
        conn.execute(text("CREATE INDEX ix_sets_exercise_id ON sets (exercise_id)"))


@dataclass(frozen=True)
class LinkedIdentity:
    """A resolved channel identity: the Member it points at, and their Gym."""

    member: Member
    gym: Gym


async def _link_member_in_session(
    db, gym_id: int, name: str, channel: str, channel_user_id: str, *, is_coach: bool = False
) -> Member:
    """The writes of ``link_member`` inside an already-open transaction."""
    member = Member(gym_id=gym_id, name=name, is_coach=is_coach)
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
    return member


async def _redeem_coach_code(db, gym_id: int, coach_code: str) -> bool:
    """Confirm the coach code is still active while locking the Gym row.

    The no-op UPDATE takes the Gym row's write lock until commit, so a
    concurrent ``regenerate_coach_invite_code`` either landed first (this
    returns ``False``) or waits for this transaction — a revoked code can
    never slip a grant through between check and commit.
    """
    code = normalize_invite_code(coach_code)
    if not code:
        return False
    result = await db.execute(
        update(Gym)
        .where(Gym.id == gym_id, Gym.coach_invite_code == code)
        .values(coach_invite_code=code)
    )
    return result.rowcount > 0


class LinkingStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def ensure_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)
        # Gyms provisioned before the coach link get their code at startup;
        # fresh schemas have no NULL codes, so this is a no-op there.
        async with self._sessions() as db:
            legacy = (
                await db.scalars(select(Gym).where(Gym.coach_invite_code.is_(None)))
            ).all()
            for gym in legacy:
                gym.coach_invite_code = new_coach_invite_code()
            await db.commit()

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
            member = await _link_member_in_session(db, gym_id, name, channel, channel_user_id)
            await db.commit()
            return member

    async def link_member_as_coach(
        self, gym_id: int, name: str, channel: str, channel_user_id: str, coach_code: str
    ) -> Member | None:
        """Redeem a coach code: link the joiner already coach-flagged.

        One transaction: the Member row is born coach-flagged (no partial
        plain-member state a retry could duplicate), and the grant is
        conditional on the code still being active — a code regenerated
        mid-flow revokes the whole link. Returns ``None`` when the code is
        no longer active; nothing is written then.
        """
        async with self._sessions() as db:
            if not await _redeem_coach_code(db, gym_id, coach_code):
                await db.rollback()
                return None
            member = await _link_member_in_session(
                db, gym_id, name, channel, channel_user_id, is_coach=True
            )
            await db.commit()
            return member

    async def promote_to_coach(self, gym_id: int, member_id: int, coach_code: str) -> bool:
        """Redeem a coach code: flag an existing Member of the Gym as Coach.

        Atomic with the code check, so a code regenerated first revokes the
        promotion instead of racing through. Returns ``False`` when the code
        is no longer active.
        """
        async with self._sessions() as db:
            if not await _redeem_coach_code(db, gym_id, coach_code):
                await db.rollback()
                return False
            await db.execute(
                update(Member)
                .where(Member.id == member_id, Member.gym_id == gym_id)
                .values(is_coach=True)
            )
            await db.commit()
            return True

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

    async def rename_gym(self, gym_id: int, name: str) -> str:
        """Rename a Gym; the new name is what Members see when they join.

        Whitespace is collapsed and the result capped at the column's
        ``String(200)`` — the form's ``maxlength`` is client-side only, so
        the cap has to hold here. Every reader resolves the Gym row fresh,
        so the rename takes effect everywhere on commit — no cache to
        invalidate.
        """
        name = " ".join(name.split())[:GYM_NAME_MAX_LENGTH]
        async with self._sessions() as db:
            await db.execute(update(Gym).where(Gym.id == gym_id).values(name=name))
            await db.commit()
        return name
