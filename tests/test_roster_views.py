"""The Cards and Split roster views behind the view switcher (issue #106,
spec-dashboard §The roster).

Store level: the Cards attendance grid — 4 weeks as a 7-column Mon–Sun day
grid, one square per day, driven by the same day-grained Routine
reconstruction as the severity engine (a Session on an unplanned day shows
trained but never cancels a miss square; today never counts until it is
over). API level: the grid's serialization on /api/roster (#154). The view
chrome — the Table/Cards/Split switcher, search, bands, rail — is React's
now and covered by the frontend RTL tests.
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


async def test_the_api_serves_the_attendance_grid_with_hit_squares(env):
    """The /api/roster attendance cells carry the store's day states —
    including a hit on an unplanned-day Session — so the React Cards view
    renders the same grid the server computed (#154; the view chrome
    itself is covered by the frontend RTL tests)."""
    import json

    member = await env.add_member("Luis")
    await env.give_planned_routine(member, weekdays=[0], days_ago=10)
    await env.train(member, days_ago=1)  # Tue 2026-07-14, unplanned

    data = json.loads(await env.page("/api/roster"))

    row = next(r for r in data["active"] if r["name"] == "Luis")
    states = {cell["on"]: cell["state"] for cell in row["attendance"]}
    assert states["2026-07-14"] == "hit"  # the unplanned Session
    assert states["2026-07-06"] == "miss" and states["2026-07-13"] == "miss"
    assert sum(1 for s in states.values() if s == "hit") == 1
    assert sum(1 for s in states.values() if s == "miss") == 2
