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
