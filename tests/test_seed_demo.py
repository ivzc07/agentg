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
    finally:
        await engine.dispose()
