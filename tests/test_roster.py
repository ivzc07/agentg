"""The Table roster (issue #97, spec-dashboard §The roster).

Store level: exactly the Gym's live-channel, non-coach Members, Gap-sorted,
with lapsed Members folded into a most-recently-active tail. Page level:
the new/snoozed tags, the collapsed lapsed tail outside the counters, and
the live search box markup.
"""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentg.models import Member


@pytest.fixture
async def env(roster_env):
    return roster_env


async def test_the_roster_lists_exactly_the_live_channel_non_coach_members(env):
    member = await env.add_member("Luis")
    # A gym switch's ghost: the channel identity moved to a fresh Member at
    # the new Gym, leaving the old row with no live channel.
    ghost = await env.add_member("Ghost")
    ghost_uid = env.last_uid()
    other_gym = await env.linking.create_gym("Other Gym")
    await env.linking.link_member(other_gym.id, "Ghost", "telegram", ghost_uid)
    # A Member of another Gym entirely.
    await env.linking.link_member(other_gym.id, "Outsider", "telegram", "902")

    rows, lapsed = await env.store.roster(env.gym.id)

    assert [row.name for row in rows] == ["Luis"]
    assert lapsed == []
    assert ghost.id not in [row.member_id for row in rows]
    assert env.coach.id not in [row.member_id for row in rows]


async def test_rows_sort_by_gap_largest_first(env):
    for name, days in [("Ana", 1), ("Beto", 5), ("Cora", 3)]:
        member = await env.add_member(name)
        await env.train(member, days_ago=days)

    rows, _ = await env.store.roster(env.gym.id)

    assert [(row.name, row.gap_days) for row in rows] == [("Beto", 5), ("Cora", 3), ("Ana", 1)]


async def test_a_member_without_an_active_routine_is_new(env):
    new_member = await env.add_member("Novata")
    with_routine = await env.add_member("Veterano")
    await env.give_routine(with_routine)

    rows, _ = await env.store.roster(env.gym.id)

    by_name = {row.name: row for row in rows}
    assert by_name["Novata"].is_new
    assert not by_name["Veterano"].is_new


async def test_a_sessionless_member_gaps_from_signup(env):
    member = await env.add_member("Recién llegado")
    signup = env.clock.now - timedelta(days=4)
    async with async_sessionmaker(env.engine)() as db:
        row = await db.get(Member, member.id)
        row.created_at = signup
        await db.commit()

    rows, _ = await env.store.roster(env.gym.id)

    assert rows[0].gap_days == 4
    assert not rows[0].has_sessions


async def test_a_snoozed_member_keeps_their_place_with_the_date(env):
    for name, days in [("Lejos", 6), ("Pausado", 3), ("Cerca", 1)]:
        member = await env.add_member(name)
        await env.train(member, days_ago=days)
    rows_all, _ = await env.store.roster(env.gym.id)
    paused = next(row for row in rows_all if row.name == "Pausado")
    until = env.clock.now.date() + timedelta(days=5)
    await env.checkins.snooze_until(paused.member_id, until)

    rows, lapsed = await env.store.roster(env.gym.id)

    assert [row.name for row in rows] == ["Lejos", "Pausado", "Cerca"]
    assert rows[1].snoozed_until == until
    assert lapsed == []


async def test_an_expired_unswept_snooze_renders_as_a_normal_row(env):
    member = await env.add_member("Pausado")
    await env.train(member, days_ago=3)
    # Snoozed until yesterday; the sweep hasn't woken the row yet.
    yesterday = env.clock.now.date() - timedelta(days=1)
    await env.checkins.snooze_until(member.id, yesterday)

    rows, _ = await env.store.roster(env.gym.id)

    assert rows[0].snoozed_until is None


async def test_lapsed_members_fold_into_a_most_recently_active_tail(env):
    for name, days in [("Activo", 2), ("Perdido A", 10), ("Perdido B", 20)]:
        member = await env.add_member(name)
        await env.train(member, days_ago=days)
    rows_all, _ = await env.store.roster(env.gym.id)
    by_name = {row.name: row for row in rows_all}
    await env.checkins.lapse(by_name["Perdido A"].member_id)
    await env.checkins.lapse(by_name["Perdido B"].member_id)

    rows, lapsed = await env.store.roster(env.gym.id)

    assert [row.name for row in rows] == ["Activo"]
    assert [row.name for row in lapsed] == ["Perdido A", "Perdido B"]


async def test_the_page_renders_rows_tags_and_the_collapsed_tail(env):
    away = await env.add_member("Beto")
    await env.train(away, days_ago=5)
    await env.give_routine(away)
    new_member = await env.add_member("Novata")
    paused = await env.add_member("Pausado")
    await env.train(paused, days_ago=3)
    until = env.clock.now.date() + timedelta(days=5)
    await env.checkins.snooze_until(paused.id, until)
    lost = await env.add_member("Perdido")
    await env.train(lost, days_ago=30)
    await env.checkins.lapse(lost.id)

    text = await env.page()

    # Gap order holds in the markup; the lapsed row is not in the main list.
    # (Novata has no Session yet, so her Gap is smallest.)
    main = text.split('<details id="lapsed">')[0]
    assert main.index("Beto") < main.index("Pausado") < main.index("Novata")
    assert "Perdido" not in main
    # The counter excludes the lapsed tail.
    assert "Miembros (3)" in text
    # Tags and row text.
    assert "nuevo" in text
    assert f"en pausa hasta el {until.strftime('%d/%m/%Y')}" in text
    assert "5 días sin venir" in text
    assert "Aún sin sesiones" in text
    # The tail is collapsed by default and labelled with its size.
    assert '<details id="lapsed">' in text
    assert '<details id="lapsed" open>' not in text
    assert "Se perdieron (1)" in text
    # Search: live box, name-only filter hooks, accent-insensitive matching.
    assert 'id="search"' in text
    assert 'data-name="Beto"' in text
    assert 'normalize("NFD")' in text
