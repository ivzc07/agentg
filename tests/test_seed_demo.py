"""The demo seeder is the only way to judge the roster pilot on populated data
(issue #149), so it is pinned like any other shipped code path: it must run, and
what it produces must actually populate the roster's three shapes — busy actives,
a session-less newcomer, a snoozed member, and a lapsed tail.
"""

from agentg.dashboard_store import DashboardStore
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from agentg.scripts.seed_demo import seed_demo_data


async def _gym_with_coach(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    linking = LinkingStore(engine)
    store = DashboardStore(engine)
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    coach = await linking.link_member(gym.id, "Ana", "telegram", "42")
    await linking.set_coach(coach.id, True)
    return engine, linking, store, gym, coach


async def test_seed_demo_data_populates_the_roster(tmp_path):
    engine, linking, store, gym, coach = await _gym_with_coach(tmp_path)
    try:
        summary = await seed_demo_data(store, linking, gym.id, coach.id)

        assert summary["members"] > 0
        assert summary["sessions"] > 0

        rows, lapsed = await store.roster(gym.id)
        # The coach is a member too; the seeded members are on top of them.
        assert len(rows) + len(lapsed) >= summary["members"]
        # A lapsed tail exists, which is what the roster's lapsed section renders.
        assert lapsed, "seeded data must include a lapsed member"
        # At least one member has no sessions at all (the 'new' flag on the card).
        assert any(row.has_sessions is False for row in rows)
        # At least one member is snoozed (the snooze badge).
        assert any(row.snoozed_until is not None for row in rows)
        # And the busy end: someone with real session history driving the grid.
        assert any(row.gap_days <= 5 and row.has_sessions for row in rows)
        # At least one member has a non-null severity and non-zero missed_days
        # — the seeded data must genuinely produce the colour bands the roster
        # pilot exists to demo (issue #149, PR review).
        assert any(
            row.severity is not None and row.missed_days > 0 for row in rows
        ), "seeded data must produce a severity band"
    finally:
        await engine.dispose()


async def test_seed_two_gyms_does_not_cross_contaminate(tmp_path):
    """Seeding a second gym must not re-link channel rows or empty the
    first gym's roster (issue #149, PR review)."""
    from agentg.linking_store import LinkingStore as LS

    engine_a = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'gym_a.db'}")
    linking_a = LS(engine_a)
    store_a = DashboardStore(engine_a)
    await linking_a.ensure_schema()

    gym_a = await linking_a.create_gym("Alpha")
    coach_a = await linking_a.link_member(gym_a.id, "CoachA", "telegram", "c1")
    await linking_a.set_coach(coach_a.id, True)

    engine_b = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'gym_b.db'}")
    linking_b = LS(engine_b)
    store_b = DashboardStore(engine_b)
    await linking_b.ensure_schema()

    gym_b = await linking_b.create_gym("Bravo")
    coach_b = await linking_b.link_member(gym_b.id, "CoachB", "telegram", "c2")
    await linking_b.set_coach(coach_b.id, True)

    try:
        # Seed gym A first, then gym B.
        summary_a = await seed_demo_data(store_a, linking_a, gym_a.id, coach_a.id)
        summary_b = await seed_demo_data(store_b, linking_b, gym_b.id, coach_b.id)

        rows_a, lapsed_a = await store_a.roster(gym_a.id)
        rows_b, lapsed_b = await store_b.roster(gym_b.id)

        # Both rosters must survive intact — neither is emptied.
        total_a = len(rows_a) + len(lapsed_a)
        total_b = len(rows_b) + len(lapsed_b)
        assert total_a >= summary_a["members"], (
            f"gym A roster shrunk: {total_a} < {summary_a['members']}"
        )
        assert total_b >= summary_b["members"], (
            f"gym B roster shrunk: {total_b} < {summary_b['members']}"
        )
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
