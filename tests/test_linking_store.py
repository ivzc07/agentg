"""LinkingStore: gyms, invite codes, Members, channel identity (spec §Data model)."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentg.db import create_engine
from agentg.models import Member, MemberChannel
from agentg.linking_store import COACH_CODE_PREFIX, INVITE_CODE_LENGTH, LinkingStore, new_invite_code


@pytest.fixture
async def store(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    store = LinkingStore(engine)
    await store.ensure_schema()
    yield store
    await engine.dispose()


def test_invite_codes_are_deep_link_safe_slugs():
    code = new_invite_code()
    assert len(code) == INVITE_CODE_LENGTH
    assert code.isalnum() and code == code.lower()


def test_generated_invite_codes_always_carry_a_digit():
    # The near-miss shape test (_looks_like_invite_code) requires a digit to
    # tell typed codes from ordinary words; generation must guarantee one or
    # ~7% of real codes would dead-end their own typos.
    for _ in range(1000):
        assert any(ch.isdigit() for ch in new_invite_code())


async def test_created_gym_is_found_by_its_invite_code(store):
    gym = await store.create_gym("Iron Temple")
    found = await store.gym_by_invite_code(gym.invite_code)
    assert found is not None and found.id == gym.id
    assert found.timezone and found.weight_unit  # spec: gym carries defaults


async def test_invite_code_lookup_forgives_case_and_whitespace(store):
    gym = await store.create_gym("Iron Temple")
    assert (await store.gym_by_invite_code(f"  {gym.invite_code.upper()} ")) is not None
    assert (await store.gym_by_invite_code("no-such-code")) is None
    assert (await store.gym_by_invite_code("   ")) is None


async def test_link_member_creates_the_member_under_the_right_gym(store):
    gym = await store.create_gym("Iron Temple")
    member = await store.link_member(gym.id, "Ana", "telegram", "42")

    linked = await store.identity_for("telegram", "42")
    assert linked is not None
    assert linked.member.id == member.id
    assert linked.member.name == "Ana"
    assert linked.member.is_coach is False
    assert linked.gym.id == gym.id


async def test_unknown_identity_is_not_linked(store):
    assert await store.identity_for("telegram", "999") is None


async def test_relinking_repoints_the_identity_without_duplicating_it(store):
    old_gym = await store.create_gym("Iron Temple")
    new_gym = await store.create_gym("Steel Yard")
    old_member = await store.link_member(old_gym.id, "Ana", "telegram", "42")
    new_member = await store.link_member(new_gym.id, "Ana", "telegram", "42")

    linked = await store.identity_for("telegram", "42")
    assert linked is not None and linked.member.id == new_member.id
    assert linked.gym.id == new_gym.id

    sessions = async_sessionmaker(store.engine)
    async with sessions() as db:
        # exactly one channel row for the identity; the old Member row untouched
        assert await db.scalar(select(func.count()).select_from(MemberChannel)) == 1
        old_row = await db.get(Member, old_member.id)
        assert old_row is not None and old_row.gym_id == old_gym.id


async def test_channel_identity_is_unique_per_channel(store):
    gym = await store.create_gym("Iron Temple")
    member = await store.link_member(gym.id, "Ana", "telegram", "42")

    sessions = async_sessionmaker(store.engine)
    async with sessions() as db:
        db.add(
            MemberChannel(
                gym_id=gym.id, member_id=member.id, channel="telegram", channel_user_id="42"
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_same_numeric_id_on_another_channel_is_a_distinct_identity(store):
    gym = await store.create_gym("Iron Temple")
    await store.link_member(gym.id, "Ana", "telegram", "42")
    assert await store.identity_for("whatsapp", "42") is None


async def test_regenerating_the_invite_code_stops_the_old_one_matching(store):
    gym = await store.create_gym("Iron Temple")
    old_code = gym.invite_code

    new_code = await store.regenerate_invite_code(gym.id)

    assert new_code != old_code
    assert await store.gym_by_invite_code(old_code) is None
    found = await store.gym_by_invite_code(new_code)
    assert found is not None and found.id == gym.id


async def test_a_member_can_be_flagged_as_coach(store):
    gym = await store.create_gym("Iron Temple")
    member = await store.link_member(gym.id, "Sam", "telegram", "7")

    await store.set_coach(member.id)

    linked = await store.identity_for("telegram", "7")
    assert linked is not None and linked.member.is_coach is True


# --- the coach invite code (issue #104) ---


async def test_provisioning_creates_a_coach_code_alongside_the_member_code(store):
    gym = await store.create_gym("Iron Temple")

    assert gym.coach_invite_code is not None
    assert gym.coach_invite_code.startswith(COACH_CODE_PREFIX)
    assert gym.coach_invite_code != gym.invite_code

    found = await store.gym_by_coach_invite_code(gym.coach_invite_code)
    assert found is not None and found.id == gym.id


async def test_coach_code_lookup_forgives_case_and_whitespace(store):
    gym = await store.create_gym("Iron Temple")
    assert (await store.gym_by_coach_invite_code(f"  {gym.coach_invite_code.upper()} ")) is not None
    assert (await store.gym_by_coach_invite_code("coach-nope")) is None
    assert (await store.gym_by_coach_invite_code("   ")) is None


async def test_the_two_code_namespaces_never_cross_match(store):
    gym = await store.create_gym("Iron Temple")
    assert (await store.gym_by_coach_invite_code(gym.invite_code)) is None
    assert (await store.gym_by_invite_code(gym.coach_invite_code)) is None


async def test_regenerating_the_coach_code_stops_the_old_one_matching(store):
    gym = await store.create_gym("Iron Temple")
    old_code = gym.coach_invite_code

    new_code = await store.regenerate_coach_invite_code(gym.id)

    assert new_code != old_code
    assert await store.gym_by_coach_invite_code(old_code) is None
    found = await store.gym_by_coach_invite_code(new_code)
    assert found is not None and found.id == gym.id
    # the member Invite code is untouched
    found = await store.gym_by_invite_code(gym.invite_code)
    assert found is not None and found.id == gym.id


async def test_regenerating_the_coach_code_never_unflags_a_coach(store):
    gym = await store.create_gym("Iron Temple")
    member = await store.link_member(gym.id, "Sam", "telegram", "7")
    await store.set_coach(member.id)

    await store.regenerate_coach_invite_code(gym.id)

    linked = await store.identity_for("telegram", "7")
    assert linked is not None and linked.member.is_coach is True


# --- atomic coach-code redemption: a revoked code cannot grant (PR #109) ---


async def test_promote_to_coach_flags_only_while_the_code_is_active(store):
    gym = await store.create_gym("Iron Temple")
    member = await store.link_member(gym.id, "Sam", "telegram", "7")
    stale_code = gym.coach_invite_code
    await store.regenerate_coach_invite_code(gym.id)

    # A code regenerated before redemption revokes the promotion.
    assert await store.promote_to_coach(gym.id, member.id, stale_code) is False
    linked = await store.identity_for("telegram", "7")
    assert linked is not None and linked.member.is_coach is False

    current = (await store.gym_by_invite_code(gym.invite_code)).coach_invite_code
    assert await store.promote_to_coach(gym.id, member.id, current) is True
    linked = await store.identity_for("telegram", "7")
    assert linked is not None and linked.member.is_coach is True


async def test_promote_to_coach_cannot_reach_across_gyms(store):
    gym = await store.create_gym("Iron Temple")
    other = await store.create_gym("Steel Yard")
    member = await store.link_member(gym.id, "Sam", "telegram", "7")

    # The code is valid but the Member belongs to another Gym: no grant.
    assert await store.promote_to_coach(other.id, member.id, other.coach_invite_code) is True
    linked = await store.identity_for("telegram", "7")
    assert linked is not None and linked.member.is_coach is False


async def test_link_member_as_coach_births_a_coach_flagged_member(store):
    gym = await store.create_gym("Iron Temple")

    member = await store.link_member_as_coach(
        gym.id, "Sam", "telegram", "7", gym.coach_invite_code
    )

    assert member is not None and member.is_coach is True
    linked = await store.identity_for("telegram", "7")
    assert linked is not None and linked.member.is_coach is True


async def test_link_member_as_coach_with_a_revoked_code_writes_nothing(store):
    gym = await store.create_gym("Iron Temple")
    stale_code = gym.coach_invite_code
    await store.regenerate_coach_invite_code(gym.id)

    # No partial state: no Member row, no channel pointer, nothing to retry into.
    assert await store.link_member_as_coach(gym.id, "Sam", "telegram", "7", stale_code) is None
    sessions = async_sessionmaker(store.engine)
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Member)) == 0
        assert await db.scalar(select(func.count()).select_from(MemberChannel)) == 0

    # A retry with the current code links exactly one coach-flagged Member.
    current = (await store.gym_by_invite_code(gym.invite_code)).coach_invite_code
    member = await store.link_member_as_coach(gym.id, "Sam", "telegram", "7", current)
    assert member is not None and member.is_coach is True
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Member)) == 1


# --- schema evolution for deployed databases (PR #109) ---


async def test_ensure_schema_adds_the_coach_code_column_to_a_legacy_db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    store = LinkingStore(engine)
    await store.ensure_schema()
    # Simulate a database that predates the column: drop it, then insert a
    # Gym the old way (no coach code).
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX ix_gyms_coach_invite_code"))
        await conn.execute(text("ALTER TABLE gyms DROP COLUMN coach_invite_code"))
        await conn.execute(
            text(
                "INSERT INTO gyms (name, invite_code, timezone, weight_unit)"
                " VALUES ('Old Gym', 'legacy1', 'UTC', 'kg')"
            )
        )

    # Startup against the legacy schema: column added, code backfilled.
    await store.ensure_schema()

    gym = await store.gym_by_invite_code("legacy1")
    assert gym is not None
    assert gym.coach_invite_code is not None
    assert gym.coach_invite_code.startswith(COACH_CODE_PREFIX)
    found = await store.gym_by_coach_invite_code(gym.coach_invite_code)
    assert found is not None and found.id == gym.id
    async with engine.begin() as conn:
        index = (
            await conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND name = 'ix_gyms_coach_invite_code'"
                )
            )
        ).first()
    assert index is not None

    # Idempotent: a later startup keeps the backfilled code.
    await store.ensure_schema()
    again = await store.gym_by_invite_code("legacy1")
    assert again is not None and again.coach_invite_code == gym.coach_invite_code
    await engine.dispose()


async def test_ensure_schema_adds_the_sets_exercise_index_to_a_legacy_db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    store = LinkingStore(engine)
    await store.ensure_schema()
    # Simulate a database that predates the index (issue #99).
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX ix_sets_exercise_id"))

    await store.ensure_schema()

    async with engine.begin() as conn:
        index = (
            await conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                    " AND name = 'ix_sets_exercise_id'"
                )
            )
        ).first()
    assert index is not None
    await engine.dispose()


def test_the_safety_flag_migration_columns_compile_on_postgres():
    """The hand-written ALTER TABLEs must use types Postgres accepts.

    SQLite happily takes DATETIME; Postgres has no such type — on deploy the
    whole ensure_schema transaction would roll back and the process would
    fail to boot (review on PR #120). Pin the DDL against the model's column
    type as the PostgreSQL dialect compiles it."""
    from sqlalchemy.dialects import postgresql

    from agentg.linking_store import (
        ADD_ACKNOWLEDGED_AT_DDL,
        ADD_ACKNOWLEDGED_BY_DDL,
        ADD_NEXT_PATH_DDL,
    )
    from agentg.models import MemberNote

    dialect = postgresql.dialect()
    model_type = MemberNote.__table__.c.acknowledged_at.type.compile(dialect=dialect)
    assert model_type.startswith("TIMESTAMP")
    assert model_type.split()[0] in ADD_ACKNOWLEDGED_AT_DDL
    for ddl in (ADD_ACKNOWLEDGED_AT_DDL, ADD_ACKNOWLEDGED_BY_DDL, ADD_NEXT_PATH_DDL):
        assert "DATETIME" not in ddl


async def test_ensure_schema_adds_the_safety_flag_columns_to_a_legacy_db(tmp_path):
    """A database that predates issue #101 gets the tick-off stamps and the
    token next_path at startup, and the flag/tick-off queries work against
    the upgraded schema (review on PR #120)."""
    import sqlite3

    from agentg.dashboard_store import DashboardStore
    from agentg.notes import NotesStore

    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    await LinkingStore(engine).ensure_schema()
    await engine.dispose()

    # Simulate the legacy schema. Done on a raw sync connection (FK
    # enforcement off — our async engine runs with PRAGMA foreign_keys=ON).
    # member_notes is rebuilt the old way: SQLite 3.50 refuses to DROP a
    # column its own table-level FOREIGN KEY clause names, and the table is
    # still empty here.
    raw = sqlite3.connect(str(db_path))
    raw.execute("DROP TABLE member_notes")
    raw.execute(
        """CREATE TABLE member_notes (
            id INTEGER NOT NULL PRIMARY KEY,
            gym_id INTEGER NOT NULL REFERENCES gyms (id),
            member_id INTEGER NOT NULL REFERENCES members (id),
            kind VARCHAR(20) NOT NULL,
            text VARCHAR(400) NOT NULL,
            created_at DATETIME NOT NULL,
            retired_at DATETIME
        )"""
    )
    raw.execute("CREATE INDEX ix_member_notes_member_id ON member_notes (member_id)")
    raw.execute("ALTER TABLE dashboard_login_tokens DROP COLUMN next_path")
    raw.commit()
    raw.close()

    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    store = LinkingStore(engine)
    await store.ensure_schema()  # startup against the legacy schema

    async with engine.begin() as conn:
        note_columns = {
            row[1] for row in await conn.execute(text("PRAGMA table_info(member_notes)"))
        }
        token_columns = {
            row[1]
            for row in await conn.execute(text("PRAGMA table_info(dashboard_login_tokens)"))
        }
    assert {"acknowledged_at", "acknowledged_by_member_id"} <= note_columns
    assert "next_path" in token_columns

    # The flag end-to-end against the upgraded schema: write, mark, tick.
    gym = await store.create_gym("Iron Temple")
    coach = await store.link_member(gym.id, "Coach Ana", "telegram", "1")
    await store.set_coach(coach.id)
    member = await store.link_member(gym.id, "Ana", "telegram", "42")
    notes = NotesStore(engine)
    dashboard = DashboardStore(engine)
    flag = await notes.remember_safety(member.id, gym.id, "sharp knee pain")
    rows, _ = await dashboard.roster(gym.id)
    assert rows[0].has_safety_flag
    ticked = await dashboard.acknowledge_flag(gym.id, member.id, flag.id, coach.id)
    assert ticked is not None and ticked.acknowledged_by_member_id == coach.id
    token = await dashboard.create_login_token(coach.id, gym.id, next_path="/members/2")
    peeked = await dashboard.peek_login_token(token)
    assert peeked is not None and peeked.next_path == "/members/2"

    # Idempotent: a later startup changes nothing.
    await store.ensure_schema()
    rows, _ = await dashboard.roster(gym.id)
    assert not rows[0].has_safety_flag
    await engine.dispose()


async def test_ensure_schema_rebuilds_legacy_routines_for_memberless_masters(tmp_path):
    """The issue #102 SQLite upgrade keeps old rows while dropping the old
    NOT NULL member_id constraint so a Preset master can be inserted."""
    import sqlite3

    from agentg.models import Routine, Workout

    db_path = tmp_path / "legacy-routines.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    store = LinkingStore(engine)
    await store.ensure_schema()
    gym = await store.create_gym("Iron Temple")
    member = await store.link_member(gym.id, "Luis", "telegram", "2")
    await engine.dispose()
    raw = sqlite3.connect(str(db_path))
    raw.execute("DROP TABLE routines")
    raw.execute(
        "INSERT INTO routine_presets (id, gym_id, name, retired_at, created_at) "
        "VALUES (1, ?, 'Legacy', NULL, '2026-01-01 00:00:00')",
        (gym.id,),
    )
    raw.execute(
        """CREATE TABLE routines (
            id INTEGER NOT NULL PRIMARY KEY,
            gym_id INTEGER NOT NULL REFERENCES gyms (id),
            member_id INTEGER NOT NULL REFERENCES members (id),
            is_active BOOLEAN NOT NULL,
            coach_authored BOOLEAN NOT NULL,
            created_by_member_id INTEGER REFERENCES members (id),
            created_at DATETIME NOT NULL
        )"""
    )
    raw.execute(
        "INSERT INTO routines VALUES (1, ?, ?, 1, 0, NULL, '2026-01-01 00:00:00')",
        (gym.id, member.id),
    )
    raw.execute(
        "INSERT INTO workouts (id, gym_id, routine_id, weekday, name) "
        "VALUES (1, ?, 1, 0, 'Legacy day')",
        (gym.id,),
    )
    raw.commit()
    raw.close()

    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    store = LinkingStore(engine)
    await store.ensure_schema()
    async with store._sessions() as db:
        old = await db.get(Routine, 1)
        assert old is not None and old.member_id == member.id
        assert (await db.get(Workout, 1)).name == "Legacy day"
        db.add(
            Routine(
                gym_id=gym.id,
                member_id=None,
                preset_id=1,
                is_active=True,
                created_at=old.created_at,
            )
        )
        await db.flush()
    await store.ensure_schema()
    await engine.dispose()


async def test_ensure_schema_adds_default_preset_to_a_legacy_gym(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy-gym.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    store = LinkingStore(engine)
    await store.ensure_schema()
    await engine.dispose()
    raw = sqlite3.connect(str(db_path))
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("DROP INDEX ix_gyms_coach_invite_code")
    raw.execute("ALTER TABLE gyms RENAME TO gyms_legacy")
    raw.execute(
        """CREATE TABLE gyms (
            id INTEGER NOT NULL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            invite_code VARCHAR(64) NOT NULL,
            coach_invite_code VARCHAR(64),
            timezone VARCHAR(64) NOT NULL,
            weight_unit VARCHAR(8) NOT NULL,
            rules_doc TEXT,
            created_at DATETIME NOT NULL
        )"""
    )
    raw.execute(
        "INSERT INTO gyms SELECT id, name, invite_code, coach_invite_code, timezone, "
        "weight_unit, rules_doc, created_at FROM gyms_legacy"
    )
    raw.execute("DROP TABLE gyms_legacy")
    raw.commit()
    raw.close()

    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    store = LinkingStore(engine)
    await store.ensure_schema()

    async with engine.begin() as conn:
        columns = {row[1] for row in await conn.execute(text("PRAGMA table_info(gyms)"))}
    assert "default_preset_id" in columns
    await engine.dispose()


async def test_ensure_schema_adds_gym_scoped_fk_indexes_to_a_legacy_db(tmp_path):
    """A database that predates issue #178 gets indexes on members.gym_id
    and member_channels.member_id / gym_id at startup."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-fk.db'}")
    store = LinkingStore(engine)
    await store.ensure_schema()

    # Drop the FK indexes to simulate a pre-#178 database.
    async with engine.begin() as conn:
        for name in ("ix_members_gym_id", "ix_member_channels_member_id",
                     "ix_member_channels_gym_id"):
            await conn.execute(text(f"DROP INDEX IF EXISTS {name}"))

    await store.ensure_schema()

    async with engine.begin() as conn:
        indexes = {
            row[1]
            for row in await conn.execute(
                text("SELECT type, name FROM sqlite_master WHERE type = 'index'")
            )
        }
    for name in ("ix_members_gym_id", "ix_member_channels_member_id",
                 "ix_member_channels_gym_id"):
        assert name in indexes, f"{name} should have been created by ensure_schema"

    # Idempotent: a later startup changes nothing.
    await store.ensure_schema()
    await engine.dispose()


async def test_gym_scoped_fk_indexes_are_present_on_a_fresh_schema(tmp_path):
    """A brand-new database gets the FK indexes from create_all (model defs)."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    store = LinkingStore(engine)
    await store.ensure_schema()

    async with engine.begin() as conn:
        indexes = {
            row[1]
            for row in await conn.execute(
                text("SELECT type, name FROM sqlite_master WHERE type = 'index'")
            )
        }
    for name in ("ix_members_gym_id", "ix_member_channels_member_id",
                 "ix_member_channels_gym_id"):
        assert name in indexes, f"{name} should be present on a fresh schema"
    await engine.dispose()
