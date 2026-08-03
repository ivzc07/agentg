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


# Safety flags (issue #101): the tick-off stamps on member_notes, and the
# deep-link target on the magic-link tokens. Module-level so a test can pin
# them against the model's column types as PostgreSQL compiles them —
# TIMESTAMP, never DATETIME (SQLite accepts both; Postgres has no DATETIME).
ADD_ACKNOWLEDGED_AT_DDL = (
    "ALTER TABLE member_notes ADD COLUMN acknowledged_at TIMESTAMP"
)
ADD_ACKNOWLEDGED_BY_DDL = (
    "ALTER TABLE member_notes ADD COLUMN acknowledged_by_member_id "
    "INTEGER REFERENCES members(id)"
)
ADD_NEXT_PATH_DDL = (
    "ALTER TABLE dashboard_login_tokens ADD COLUMN next_path VARCHAR(200)"
)


def _add_missing_columns(conn: Connection) -> None:
    """Schema evolution for deployed databases: ``create_all`` never alters
    existing tables, so columns and indexes added after first deploy are
    applied here, idempotently. (No migration framework — the repo's
    mechanism is this list; add one entry per new column or index on an
    existing table.)"""
    gym_columns = {c["name"] for c in inspect(conn).get_columns("gyms")}
    if "coach_invite_code" not in gym_columns:
        conn.execute(text("ALTER TABLE gyms ADD COLUMN coach_invite_code VARCHAR(64)"))
    if "default_preset_id" not in gym_columns:
        conn.execute(
            text(
                "ALTER TABLE gyms ADD COLUMN default_preset_id "
                "INTEGER REFERENCES routine_presets(id)"
            )
        )
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
    # The dashboard Routine editor's actor stamp (issue #100): which Member
    # (as Coach) wrote the Routine; NULL keeps meaning "the Agent via chat".
    routine_columns = {c["name"] for c in inspect(conn).get_columns("routines")}
    if "created_by_member_id" not in routine_columns:
        conn.execute(
            text("ALTER TABLE routines ADD COLUMN created_by_member_id INTEGER REFERENCES members(id)")
        )
    if "preset_id" not in routine_columns:
        conn.execute(
            text("ALTER TABLE routines ADD COLUMN preset_id INTEGER REFERENCES routine_presets(id)")
        )
    # SQLite has no ALTER COLUMN. Rebuild only legacy tables that still make
    # Member mandatory, preserving every row inside this startup transaction
    # (issue #102; the Postgres branch below can alter in place).
    routine_columns_info = inspect(conn).get_columns("routines")
    member_column = next(column for column in routine_columns_info if column["name"] == "member_id")
    if not member_column["nullable"]:
        if conn.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE routines ALTER COLUMN member_id DROP NOT NULL"))
        elif conn.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.exec_driver_sql("DROP TABLE IF EXISTS routines__preset_upgrade")
            conn.exec_driver_sql(
                """CREATE TABLE routines__preset_upgrade (
                    id INTEGER NOT NULL PRIMARY KEY,
                    gym_id INTEGER NOT NULL REFERENCES gyms(id),
                    member_id INTEGER REFERENCES members(id),
                    preset_id INTEGER REFERENCES routine_presets(id),
                    is_active BOOLEAN NOT NULL,
                    coach_authored BOOLEAN NOT NULL,
                    created_by_member_id INTEGER REFERENCES members(id),
                    created_at DATETIME NOT NULL
                )"""
            )
            conn.execute(
                text(
                    "INSERT INTO routines__preset_upgrade "
                    "(id, gym_id, member_id, preset_id, is_active, coach_authored, "
                    "created_by_member_id, created_at) "
                    "SELECT id, gym_id, member_id, preset_id, is_active, coach_authored, "
                    "created_by_member_id, created_at FROM routines"
                )
            )
            conn.exec_driver_sql("DROP TABLE routines")
            conn.exec_driver_sql("ALTER TABLE routines__preset_upgrade RENAME TO routines")
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    # One active Routine per Member, DB-enforced (issue #100 review): the
    # backstop behind the editor's no-base stale check.
    routine_indexes = {i["name"] for i in inspect(conn).get_indexes("routines")}
    if "uq_routines_one_active_per_member" not in routine_indexes:
        # Heal pre-index duplicates first: a legacy database may hold Members
        # with two active Routines (pre-PR saves could interleave and commit
        # both), and creating the unique index over them would abort boot.
        # The newest active survives (MAX(id) — same "most recent governs"
        # rule as the dashboard's per-date reconstruction), the rest are
        # deactivated.
        conn.execute(
            text(
                "UPDATE routines SET is_active = false "
                "WHERE is_active AND member_id IS NOT NULL AND id NOT IN ("
                "  SELECT MAX(id) FROM routines "
                "  WHERE is_active AND member_id IS NOT NULL GROUP BY member_id"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_routines_one_active_per_member "
                "ON routines (member_id) WHERE is_active"
            )
        )
    routine_indexes = {i["name"] for i in inspect(conn).get_indexes("routines")}
    if "ix_routines_member_active" not in routine_indexes:
        conn.execute(
            text("CREATE INDEX ix_routines_member_active ON routines (member_id, is_active)")
        )
    if "ix_routines_preset_id" not in routine_indexes:
        conn.execute(text("CREATE INDEX ix_routines_preset_id ON routines (preset_id)"))
    routine_indexes = {i["name"] for i in inspect(conn).get_indexes("routines")}
    if "uq_routines_one_active_master_per_preset" not in routine_indexes:
        conn.execute(
            text(
                "UPDATE routines SET is_active = false "
                "WHERE is_active AND member_id IS NULL AND preset_id IS NOT NULL "
                "AND id NOT IN ("
                "  SELECT MAX(id) FROM routines "
                "  WHERE is_active AND member_id IS NULL AND preset_id IS NOT NULL "
                "  GROUP BY preset_id"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_routines_one_active_master_per_preset "
                "ON routines (preset_id) WHERE is_active AND member_id IS NULL"
            )
        )
    # Safety flags (issue #101): the tick-off stamps on member_notes, and the
    # deep-link target on the magic-link tokens.
    note_columns = {c["name"] for c in inspect(conn).get_columns("member_notes")}
    if "acknowledged_at" not in note_columns:
        conn.execute(text(ADD_ACKNOWLEDGED_AT_DDL))
    if "acknowledged_by_member_id" not in note_columns:
        conn.execute(text(ADD_ACKNOWLEDGED_BY_DDL))
    token_columns = {c["name"] for c in inspect(conn).get_columns("dashboard_login_tokens")}
    if "next_path" not in token_columns:
        conn.execute(text(ADD_NEXT_PATH_DDL))
    # FK indexes for Gym-scoped reads (issue #178): Coach lookup, roster,
    # and the check-in sweep join on these columns.
    members_indexes = {i["name"] for i in inspect(conn).get_indexes("members")}
    if "ix_members_gym_id" not in members_indexes:
        conn.execute(text("CREATE INDEX ix_members_gym_id ON members (gym_id)"))
    channels_indexes = {i["name"] for i in inspect(conn).get_indexes("member_channels")}
    if "ix_member_channels_member_id" not in channels_indexes:
        conn.execute(
            text("CREATE INDEX ix_member_channels_member_id ON member_channels (member_id)")
        )
    if "ix_member_channels_gym_id" not in channels_indexes:
        conn.execute(
            text("CREATE INDEX ix_member_channels_gym_id ON member_channels (gym_id)")
        )
    # Safety-outbox columns (issue #216): transient failures are retried
    # before a job is permanently failed; claimed_at enables lease detection;
    # last_error captures the most recent transient failure reason.
    outbox_columns = {c["name"] for c in inspect(conn).get_columns("safety_outbox_jobs")}
    if "retry_count" not in outbox_columns:
        conn.execute(
            text("ALTER TABLE safety_outbox_jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        )
    if "claimed_at" not in outbox_columns:
        conn.execute(
            text("ALTER TABLE safety_outbox_jobs ADD COLUMN claimed_at TIMESTAMP")
        )
    if "last_error" not in outbox_columns:
        conn.execute(
            text("ALTER TABLE safety_outbox_jobs ADD COLUMN last_error VARCHAR(400)")
        )
    if "next_retry_at" not in outbox_columns:
        conn.execute(
            text("ALTER TABLE safety_outbox_jobs ADD COLUMN next_retry_at TIMESTAMP")
        )
    # P1 #1: tighten unique constraint from (gym_id, note_id, coach_member_id)
    # to (note_id, coach_member_id) — one job per Note/Coach regardless of
    # gym_id (the Note already owns the Gym scope; the gym_id column is
    # denormalised for convenience and must match the Note's gym_id).
    #
    # Use get_unique_constraints (not get_indexes) so the inspection works
    # on PostgreSQL (where unique constraints are not regular indexes) and
    # column_names are strings — iterate them directly (P1 #5 r5).
    outbox_uniques = {
        c["name"]: c
        for c in inspect(conn).get_unique_constraints("safety_outbox_jobs")
    }
    if "uq_outbox_job_note_coach" in outbox_uniques:
        existing_cols = list(
            outbox_uniques["uq_outbox_job_note_coach"]["column_names"]
        )
        # Recreate if the old three-column form is still present.
        if "gym_id" in existing_cols:
            if conn.dialect.name == "postgresql":
                conn.execute(
                    text(
                        "ALTER TABLE safety_outbox_jobs "
                        "DROP CONSTRAINT uq_outbox_job_note_coach"
                    )
                )
                conn.execute(
                    text(
                        "ALTER TABLE safety_outbox_jobs "
                        "ADD CONSTRAINT uq_outbox_job_note_coach "
                        "UNIQUE (note_id, coach_member_id)"
                    )
                )
            else:
                conn.execute(
                    text("DROP INDEX IF EXISTS uq_outbox_job_note_coach")
                )
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_outbox_job_note_coach "
                        "ON safety_outbox_jobs (note_id, coach_member_id)"
                    )
                )


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
        # Gym switch: lock the MemberChannel row to serialize with
        # _authorized_send so a concurrent safety-flag delivery sees
        # a consistent channel identity (P1 r8).  Also lock the old
        # Member row — if the old identity was a Coach, _authorized_send
        # may be holding it.
        await db.execute(
            select(MemberChannel)
            .where(MemberChannel.id == pointer.id)
            .with_for_update()
        )
        old_member_id = pointer.member_id
        if old_member_id is not None:
            await db.execute(
                select(Member)
                .where(Member.id == old_member_id)
                .with_for_update()
            )
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
            gyms = (await db.scalars(select(Gym))).all()
            for gym in gyms:
                if gym.coach_invite_code is None:
                    gym.coach_invite_code = new_coach_invite_code()
                # Gyms provisioned before the digit guarantee (c0a43fb) can
                # hold digitless invite codes; the near-miss gate would
                # dead-end them when typed (#169). Heal them at startup.
                if not any(ch.isdigit() for ch in gym.invite_code):
                    gym.invite_code = new_invite_code()
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
            # Lock the Member row to serialize with _authorized_send
            # (delivery path) so a concurrent safety-flag delivery
            # sees a consistent coach status (P1 r8).
            await db.execute(
                select(Member).where(Member.id == member_id).with_for_update()
            )
            # Lock the Gym row to serialize with coach eligibility queries
            # so a concurrent safety flag sees a consistent coach set (P1 #3).
            member = await db.get(Member, member_id)
            if member is not None:
                await db.execute(
                    select(Gym).where(Gym.id == member.gym_id).with_for_update()
                )
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
        """Exactly one Coach per Coach in the Gym, as
        ``(member_id, name, channel, channel_user_id)`` — who a safety flag
        gets pinged to.

        A Coach may have multiple channel identities (MemberChannel rows);
        exactly one is selected per Coach (deterministic: first by channel
        name, then by channel_user_id)."""
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
                    .order_by(
                        Member.id,
                        MemberChannel.channel,
                        MemberChannel.channel_user_id,
                    )
                )
            ).all()
        seen: set[int] = set()
        result: list[tuple[int, str, str, str]] = []
        for member_id, name, channel, channel_user_id in rows:
            if member_id == exclude_member_id or member_id in seen:
                continue
            seen.add(member_id)
            result.append((member_id, name, channel, channel_user_id))
        return result

    async def coach_channel_in_gym(
        self, member_id: int, gym_id: int
    ) -> tuple[str, str] | None:
        """Return ``(channel, channel_user_id)`` for *member_id* if they are
        still reachable in *gym_id* and still flagged as Coach, or ``None``
        when the channel was repointed (gym switch leaving no channel row for
        the old member) or the Coach flag was removed.

        When a Coach has multiple channels, the determination is
        deterministic (by channel name, then channel_user_id).

        This is called at delivery time so a coach who switched gyms or was
        demoted between job creation and delivery never receives a cross-gym
        or cross-role notification.
        """
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(
                        MemberChannel.channel,
                        MemberChannel.channel_user_id,
                    )
                    .join(Member, Member.id == MemberChannel.member_id)
                    .where(
                        MemberChannel.member_id == member_id,
                        MemberChannel.gym_id == gym_id,
                        Member.is_coach.is_(True),
                    )
                    .order_by(
                        MemberChannel.channel,
                        MemberChannel.channel_user_id,
                    )
                )
            ).first()
        if row is None:
            return None
        return (row.channel, row.channel_user_id)

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
