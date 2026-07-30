"""Shared test helpers."""

from datetime import UTC, datetime, timedelta


async def unused_phraser(instruction: str, member_text: str) -> str:
    """A Linking phraser for tests that don't exercise linking replies."""
    raise AssertionError("linking should not phrase anything in this test")


async def identity_phraser(instruction: str, member_text: str) -> str:
    """A Linking phraser for tests that exercise linking replies: no
    model, so a reply is exactly its instruction — assertions about facts
    (gym/name) exercise the real instruction text the production phraser
    would receive."""
    return instruction


class FakeClock:
    """An injectable clock: starts at a fixed instant, advances on demand."""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 7, 15, 18, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


# --- The dashboard roster test world (test_roster.py, test_roster_severity.py) ---

import pytest
from aiohttp.test_utils import TestClient, TestServer
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentg.checkin_store import CheckinStore
from agentg.dashboard_store import DashboardStore, RosterRow
from agentg.dashboard_web import SESSION_COOKIE, build_app, sign_session
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from agentg.models import Member, Routine
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.training import TrainingStore

ROSTER_SECRET = "test-secret"


class RosterEnv:
    """A wired roster world: one Gym, its Coach, the stores, an HTTP client."""

    def __init__(self, clock, engine, linking, store, client, gym, coach):
        self.clock = clock
        self.engine = engine
        self.linking = linking
        self.store = store
        self.client = client
        self.gym = gym
        self.coach = coach
        self.checkins = CheckinStore(engine)
        self.training = TrainingStore(engine, clock=clock)
        self.routines = RoutineStore(engine, clock=clock)
        self._uid = 100

    async def add_member(self, name: str) -> Member:
        self._uid += 1
        return await self.linking.link_member(self.gym.id, name, "telegram", str(self._uid))

    def last_uid(self) -> str:
        return str(self._uid)

    @asynccontextmanager
    async def _rewind(self, days: int):
        """The clock ``days`` back, restored on the way out even on failure."""
        now = self.clock.now
        self.clock.now = now - timedelta(days=days)
        try:
            yield
        finally:
            self.clock.now = now

    async def train(self, member: Member, days_ago: int) -> None:
        """A Session ending ``days_ago`` before the clock's now."""
        async with self._rewind(days_ago):
            await self.training.open_session(member.id, self.gym.id)

    async def give_routine(self, member: Member) -> None:
        """A bare active Routine row — marks the Member as not-new."""
        async with async_sessionmaker(self.engine)() as db:
            db.add(
                Routine(
                    gym_id=self.gym.id,
                    member_id=member.id,
                    is_active=True,
                    created_at=self.clock(),
                )
            )
            await db.commit()

    async def give_planned_routine(
        self, member: Member, weekdays: list[int], days_ago: int
    ) -> None:
        """A Routine created ``days_ago`` ago with a Workout on each weekday."""
        async with self._rewind(days_ago):
            await self.routines.save_routine(
                member.id,
                self.gym.id,
                [
                    WorkoutSpec(
                        weekday=weekday,
                        name=f"Día {weekday}",
                        exercises=[ExerciseSpec("squat", sets=3, reps="5")],
                    )
                    for weekday in weekdays
                ],
            )

    async def roster_row(self, member: Member) -> RosterRow:
        rows, _ = await self.store.roster(self.gym.id)
        return next(row for row in rows if row.member_id == member.id)

    async def page(self) -> str:
        cookie = sign_session(self.coach.id, self.gym.id, ROSTER_SECRET, self.clock())
        response = await self.client.get("/", cookies={SESSION_COOKIE: cookie})
        assert response.status == 200
        return await response.text()


@pytest.fixture
async def roster_env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    await linking.ensure_schema()
    await TrainingStore(engine, clock=clock).ensure_seeded()  # a catalog to draw from
    gym = await linking.create_gym("Iron Temple")
    coach = await linking.link_member(gym.id, "Coach Ana", "telegram", "1")
    await linking.set_coach(coach.id, True)
    app = build_app(
        store,
        linking,
        session_secret=ROSTER_SECRET,
        bot_username="testbot",
        secure_cookies=False,
        clock=clock,
    )
    async with TestClient(TestServer(app)) as client:
        yield RosterEnv(clock, engine, linking, store, client, gym, coach)
    await engine.dispose()
