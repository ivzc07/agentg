"""Schedule-aware roster severity colouring (issue #98, spec-dashboard §The
roster / §Attendance).

Severity counts consecutive missed planned Workout days since the last
Session, each date judged against the Routine active on it (reconstructed at
read time, day-grained). Amber at one missed planned day, red at three; any
Session resets the colour; today never counts until it is over; no active
Routine means no colour; a running snooze shows none either. Gap stays the
sort key and row text — the colour never re-sorts.

The fixture clock starts on Wednesday 2026-07-15 18:00 UTC, so "yesterday"
is Tuesday the 14th throughout.
"""

import re
from datetime import timedelta

import pytest


@pytest.fixture
async def env(roster_env):
    return roster_env


# Weekday numbers (0=Monday) for the fixture week of 2026-07-13 .. 07-15.
SATURDAY, MONDAY, TUESDAY, WEDNESDAY = 5, 0, 1, 2


def row_html(text: str, name: str) -> str:
    match = re.search(rf'<li class="row" data-name="{name}">.*?</li>', text)
    assert match is not None, f"no roster row for {name}"
    return match.group(0)


async def test_one_missed_planned_day_is_amber(env):
    member = await env.add_member("Ambar")
    await env.give_planned_routine(member, [TUESDAY], days_ago=5)
    await env.train(member, days_ago=3)  # Sunday; Monday is unplanned

    row = await env.roster_row(member)

    assert row.missed_days == 1
    assert row.severity == "amber"


async def test_two_missed_planned_days_are_still_amber(env):
    member = await env.add_member("Ambar")
    await env.give_planned_routine(member, [MONDAY, TUESDAY], days_ago=10)
    await env.train(member, days_ago=5)  # Friday

    row = await env.roster_row(member)

    assert row.missed_days == 2
    assert row.severity == "amber"


async def test_three_missed_planned_days_are_red(env):
    member = await env.add_member("Roja")
    await env.give_planned_routine(member, [SATURDAY, MONDAY, TUESDAY], days_ago=10)
    await env.train(member, days_ago=5)  # Friday

    row = await env.roster_row(member)

    assert row.missed_days == 3
    assert row.severity == "red"


async def test_each_date_is_judged_against_the_routine_active_on_it(env):
    member = await env.add_member("Cambiado")
    # Mondays only until the plan changed on Monday, then Tuesdays only.
    await env.give_planned_routine(member, [MONDAY], days_ago=10)
    await env.give_planned_routine(member, [TUESDAY], days_ago=2)
    await env.train(member, days_ago=5)  # Friday

    row = await env.roster_row(member)

    # The replacement governs Monday itself (day-grained), so only Tuesday
    # misses — not Monday under the old plan.
    assert row.missed_days == 1
    assert row.severity == "amber"


async def test_same_instant_saves_before_the_session_pick_the_newest_routine(env):
    member = await env.add_member("Doble")
    # Two Routines stamped at the same instant; the second supersedes.
    await env.give_planned_routine(member, [MONDAY, TUESDAY], days_ago=10)
    await env.give_planned_routine(member, [TUESDAY], days_ago=10)
    await env.train(member, days_ago=5)  # Friday

    row = await env.roster_row(member)

    # The pre-Session governing Routine must be the newest row — the active
    # one — never the predecessor that happens to share its created_at.
    assert not row.is_new
    assert row.missed_days == 1  # Tuesday only, under the replacement
    assert row.severity == "amber"


async def test_a_session_on_an_unplanned_day_resets_the_colour(env):
    member = await env.add_member("Vino")
    await env.give_planned_routine(member, [MONDAY], days_ago=10)
    await env.train(member, days_ago=1)  # Tuesday — not a planned day

    row = await env.roster_row(member)

    assert row.missed_days == 0
    assert row.severity is None


async def test_a_routine_created_today_never_flags_the_running_day(env):
    member = await env.add_member("Estrenando")
    await env.give_planned_routine(member, [WEDNESDAY], days_ago=0)  # planned today

    row = await env.roster_row(member)

    assert row.missed_days == 0
    assert row.severity is None


async def test_a_member_without_an_active_routine_stays_uncoloured(env):
    member = await env.add_member("Novata")
    await env.train(member, days_ago=10)

    row = await env.roster_row(member)

    assert row.is_new
    assert row.missed_days == 0
    assert row.severity is None


async def test_a_sessionless_member_counts_from_the_first_routine(env):
    member = await env.add_member("SinSesiones")
    await env.give_planned_routine(member, [SATURDAY, MONDAY, TUESDAY], days_ago=4)

    row = await env.roster_row(member)

    # No grace period: all three planned days since Saturday count.
    assert row.missed_days == 3
    assert row.severity == "red"


async def test_a_snoozed_member_shows_no_colour_while_the_snooze_runs(env):
    member = await env.add_member("Pausado")
    await env.give_planned_routine(member, [SATURDAY, MONDAY, TUESDAY], days_ago=10)
    await env.train(member, days_ago=5)
    until = env.clock.now.date() + timedelta(days=5)
    await env.checkins.snooze_until(member.id, until)

    row = await env.roster_row(member)

    assert row.missed_days == 3  # the count is real…
    assert row.severity is None  # …but the snooze shows no colour


async def test_the_page_colours_rows_and_keeps_the_counters(env):
    amber = await env.add_member("Ambar")
    await env.give_planned_routine(amber, [TUESDAY], days_ago=5)
    await env.train(amber, days_ago=3)
    red = await env.add_member("Roja")
    await env.give_planned_routine(red, [SATURDAY, MONDAY, TUESDAY], days_ago=10)
    await env.train(red, days_ago=5)
    await env.add_member("Novata")
    paused = await env.add_member("Pausado")
    await env.give_planned_routine(paused, [SATURDAY, MONDAY, TUESDAY], days_ago=10)
    await env.train(paused, days_ago=5)
    until = env.clock.now.date() + timedelta(days=5)
    await env.checkins.snooze_until(paused.id, until)

    text = await env.page()

    assert "sev-amber" in row_html(text, "Ambar")
    assert "sev-red" in row_html(text, "Roja")
    assert "sev-" not in row_html(text, "Novata")
    assert "sev-" not in row_html(text, "Pausado")
    assert f"en pausa hasta el {until.strftime('%d/%m/%Y')}" in text
    # The counter still just counts Members — the lapsed tail stays out.
    assert "Miembros (4)" in text


async def test_the_colour_never_moves_the_gap_sort(env):
    red = await env.add_member("Roja")  # red, but the smaller Gap
    await env.give_planned_routine(red, [SATURDAY, MONDAY, TUESDAY], days_ago=10)
    await env.train(red, days_ago=5)
    amber = await env.add_member("Ambar")  # amber, but the larger Gap
    await env.give_planned_routine(amber, [MONDAY], days_ago=12)
    await env.train(amber, days_ago=10)

    rows, _ = await env.store.roster(env.gym.id)

    # Gap order holds even though the trailing row is the redder one.
    assert [(row.name, row.severity) for row in rows] == [
        ("Ambar", "amber"),
        ("Roja", "red"),
    ]

    text = await env.page()
    main = text.split('<details id="lapsed">')[0]
    assert main.index("Ambar") < main.index("Roja")
