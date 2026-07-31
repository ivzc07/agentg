"""The Cards and Split roster views behind the view switcher (issue #106,
spec-dashboard §The roster).

Store level: the Cards attendance grid — 4 weeks as a 7-column Mon–Sun day
grid, one square per day, driven by the same day-grained Routine
reconstruction as the severity engine (a Session on an unplanned day shows
trained but never cancels a miss square; today never counts until it is
over). Page level: the segmented control switching Table / Cards / Split
with the search beside it, the severity bands, the shared lapsed tail, and
the switcher-visibility rules (hidden on a Member opened from Table or
Cards, always visible in Split).
"""

from datetime import date, timedelta

import pytest

from agentg.dashboard_store import DayCell

# The FakeClock starts at Wed 2026-07-15 18:00 UTC; the gym is UTC. The
# 4-week grid window therefore runs Mon 2026-06-22 through Sun 2026-07-19.
WINDOW_START = date(2026, 6, 22)
TODAY = date(2026, 7, 15)


@pytest.fixture
async def env(roster_env):
    return roster_env


def cells_by_date(cells: list[DayCell]) -> dict[date, str]:
    return {cell.on: cell.state for cell in cells}


async def test_the_grid_covers_four_monday_first_weeks(env):
    member = await env.add_member("Luis")

    grid = await env.store.attendance(env.gym.id, [member.id])

    cells = grid[member.id]
    assert len(cells) == 28
    assert cells[0].on == WINDOW_START and cells[0].on.weekday() == 0
    assert cells[-1].on == WINDOW_START + timedelta(days=27)
    assert cells[-1].on.weekday() == 6
    # No Routine: nothing is ever a miss; days past today are future.
    states = cells_by_date(cells)
    assert all(states[d] == "plain" for d in states if d <= TODAY)
    assert states[date(2026, 7, 16)] == "future"
    assert states[date(2026, 7, 19)] == "future"


async def test_the_grid_marks_hits_misses_and_today_against_the_governing_routine(env):
    member = await env.add_member("Luis")
    # Mon/Wed plan created Sun 2026-07-05; it governs from that day on.
    await env.give_planned_routine(member, weekdays=[0, 2], days_ago=10)
    # Sessions on two unplanned days (Tue 07-07, Sun 07-12).
    await env.train(member, days_ago=8)
    await env.train(member, days_ago=3)

    grid = await env.store.attendance(env.gym.id, [member.id])

    states = cells_by_date(grid[member.id])
    # Before the Routine existed: no misses.
    assert states[date(2026, 6, 29)] == "plain"  # a Monday
    assert states[date(2026, 7, 5)] == "plain"  # the creation Sunday
    # Planned days with no Session are misses — the unplanned-day Sessions
    # show trained but cancel none of them.
    assert states[date(2026, 7, 6)] == "miss"  # Mon
    assert states[date(2026, 7, 8)] == "miss"  # Wed
    assert states[date(2026, 7, 13)] == "miss"  # Mon
    assert states[date(2026, 7, 7)] == "hit"  # unplanned Tuesday Session
    assert states[date(2026, 7, 12)] == "hit"  # unplanned Sunday Session
    # Today is planned but never counts until it is over.
    assert states[TODAY] == "plain"
    assert TODAY.weekday() == 2


async def test_a_session_on_a_planned_day_is_a_hit_not_a_miss(env):
    member = await env.add_member("Luis")
    # Mon/Wed plan from 2026-07-05; the Member trains Mon 07-06 as planned.
    await env.give_planned_routine(member, weekdays=[0, 2], days_ago=10)
    await env.train(member, days_ago=9)

    grid = await env.store.attendance(env.gym.id, [member.id])

    states = cells_by_date(grid[member.id])
    assert states[date(2026, 7, 6)] == "hit"  # planned Monday, trained
    assert states[date(2026, 7, 8)] == "miss"  # planned Wednesday, not


async def test_a_session_today_renders_as_a_hit(env):
    member = await env.add_member("Luis")
    await env.give_planned_routine(member, weekdays=[TODAY.weekday()], days_ago=10)
    await env.train(member, days_ago=0)

    grid = await env.store.attendance(env.gym.id, [member.id])

    # Today never counts as a miss while it runs — and a Session already
    # logged today is a hit like any other day.
    assert cells_by_date(grid[member.id])[TODAY] == "hit"


async def test_a_mid_window_routine_change_rejudges_the_days_it_governs(env):
    member = await env.add_member("Luis")
    # Mondays only from 2026-06-25, then Fridays only from 2026-07-10.
    await env.give_planned_routine(member, weekdays=[0], days_ago=20)
    await env.give_planned_routine(member, weekdays=[4], days_ago=5)

    grid = await env.store.attendance(env.gym.id, [member.id])

    states = cells_by_date(grid[member.id])
    assert states[date(2026, 6, 29)] == "miss"  # Mon, first Routine
    assert states[date(2026, 7, 6)] == "miss"  # Mon, first Routine
    assert states[date(2026, 7, 3)] == "plain"  # Fri, but not planned yet
    assert states[date(2026, 7, 10)] == "miss"  # Fri, second Routine governs
    assert states[date(2026, 7, 13)] == "plain"  # Mon, no longer planned


async def test_the_switcher_offers_the_three_views_with_the_search_beside_it(env):
    await env.add_member("Luis")

    for view in ("table", "cards", "split"):
        text = await env.page(f"/?view={view}")
        assert 'class="seg"' in text
        assert 'href="/?view=table"' in text
        assert 'href="/?view=cards"' in text
        assert 'href="/?view=split"' in text
        assert 'id="search"' in text
        assert f'?view={view}" aria-current="true"' in text
        assert f'data-name="Luis"' in text


async def test_an_unknown_view_falls_back_to_the_table(env):
    await env.add_member("Luis")

    text = await env.page("/?view=mosaic")

    assert '?view=table" aria-current="true"' in text
    assert 'data-name="Luis"' in text


async def test_cards_group_members_into_severity_bands_with_day_grids(env):
    red = await env.add_member("Rojo")
    # Mon+Tue planned since 2026-07-05, never trained: 4 misses -> red.
    await env.give_planned_routine(red, weekdays=[0, 1], days_ago=10)
    amber = await env.add_member("Ambar")
    # Sundays planned since 2026-07-05, never trained: 2 misses -> amber.
    await env.give_planned_routine(amber, weekdays=[6], days_ago=10)
    fine = await env.add_member("Aldia")
    await env.train(fine, days_ago=0)
    new = await env.add_member("Novata")

    text = await env.page("/?view=cards")

    hot = text.split('id="band-hot"')[1].split('id="band-warm"')[0]
    warm = text.split('id="band-warm"')[1].split('id="band-cool"')[0]
    cool = text.split('id="band-cool"')[1]
    assert "Te necesitan ya" in hot and 'data-name="Rojo"' in hot
    assert "Aflojando" in warm and 'data-name="Ambar"' in warm
    assert "Al día" in cool
    assert 'data-name="Aldia"' in cool and 'data-name="Novata"' in cool
    # Severity colours and the new tag ride along from the Table view.
    assert "sev-red" in hot and "sev-amber" in warm
    assert "nuevo" in cool
    # Rojo's card carries the 4-week grid: 4 misses (07-06, 07-07, 07-13,
    # 07-14), 4 future days (07-16..07-19), no hits, Mon-first initials.
    card = hot.split('data-name="Rojo"')[1]
    assert card.count('class="miss"') == 4
    assert card.count('class="future"') == 4
    assert 'class="hit"' not in card
    assert '<span class="wd" aria-hidden="true">lu</span>' in card
    # A session on an unplanned day draws a hit square.
    assert "últimas 4 semanas" in card


async def test_a_cards_hit_square_marks_an_unplanned_session(env):
    member = await env.add_member("Luis")
    await env.give_planned_routine(member, weekdays=[0], days_ago=10)
    await env.train(member, days_ago=1)  # Tue 2026-07-14, unplanned

    text = await env.page("/?view=cards")

    card = text.split(f'data-name="Luis"')[1]
    assert card.count('class="hit"') == 1
    assert 'title="14 jul 2026"' in card
    assert card.count('class="miss"') == 2  # 07-06 and 07-13


async def test_all_views_share_the_gap_sort_and_the_lapsed_tail(env):
    away = await env.add_member("Lejos")
    await env.train(away, days_ago=9)
    near = await env.add_member("Cerca")
    await env.train(near, days_ago=1)
    lost = await env.add_member("Perdido")
    await env.train(lost, days_ago=30)
    await env.checkins.lapse(lost.id)

    for view in ("table", "cards", "split"):
        text = await env.page(f"/?view={view}")
        main = text.split('<details id="lapsed">')[0]
        assert main.index("Lejos") < main.index("Cerca")
        assert "Perdido" not in main
        assert "Se perdieron (1)" in text


async def test_split_keeps_the_roster_rail_and_a_pick_a_member_placeholder(env):
    member = await env.add_member("Luis")

    text = await env.page("/?view=split")

    assert 'class="split"' in text
    assert f'href="/members/{member.id}?view=split"' in text
    assert "Elige un miembro" in text


async def test_a_member_opened_in_split_keeps_the_rail_and_the_switcher(env):
    member = await env.add_member("Luis")
    other = await env.add_member("Otra")

    text = await env.page(f"/members/{member.id}?view=split")

    # The Member page fills the right pane…
    assert "Miembro desde" in text
    # …the rail never leaves…
    assert 'class="split"' in text
    assert f'data-name="Otra"' in text
    # …and the switcher stays visible, still on Split.
    assert 'class="seg"' in text
    assert '?view=split" aria-current="true"' in text
    # No back link: nothing was left.
    assert "Todos los miembros" not in text


async def test_a_member_opened_from_table_or_cards_hides_the_switcher(env):
    member = await env.add_member("Luis")

    for view in ("table", "cards"):
        text = await env.page(f"/members/{member.id}?view={view}")
        assert 'class="seg"' not in text
        assert 'id="search"' not in text
        assert f'href="/?view={view}"' in text  # the way back to the view

    # The roster's member links carry the view they were opened from.
    for view in ("table", "cards"):
        roster = await env.page(f"/?view={view}")
        assert f'href="/members/{member.id}?view={view}"' in roster
