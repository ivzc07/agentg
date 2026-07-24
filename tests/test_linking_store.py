"""LinkingStore: gyms, invite codes, Members, channel identity (spec §Data model)."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentg.db import create_engine
from agentg.models import Member, MemberChannel
from agentg.linking_store import INVITE_CODE_LENGTH, LinkingStore, new_invite_code


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
