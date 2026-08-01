"""The Routine editor store-level tests (issue #100, spec-dashboard
§Routines & Presets).

Store level: a Coach's web save goes through the supersession machinery
stamped coach-authored and actor-stamped, and a stale save (the active
Routine changed since the editor loaded) is refused.

Web-layer tests for the Routine editor moved to test_routine_api.py
(issue #151 — JSON API replaces the htmx screen).
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentg.dashboard_store import DashboardStore
from agentg.dashboard_web import (
    SESSION_COOKIE,
    build_app,
    sign_session,
)
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from agentg.models import Member, Routine
from agentg.routines import ExerciseSpec, RoutineStore, StaleRoutineError, WorkoutSpec
from agentg.training import TrainingStore
from conftest import FakeClock

SECRET = "test-secret"
BOUNCE_MARKER = "/dashboard"  # the bounce page tells you to send /dashboard


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    async def send(self, channel: str, channel_user_id: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append((channel, channel_user_id, text))


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    notifier = FakeNotifier()
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    coach = await linking.link_member(gym.id, "Coach Ana", "telegram", "1")
    await linking.set_coach(coach.id, True)
    app = build_app(
        store,
        linking,
        session_secret=SECRET,
        bot_username="testbot",
        secure_cookies=False,
        clock=clock,
        notifier=notifier,
    )
    async with TestClient(TestServer(app)) as client:
        yield Env(clock, engine, linking, store, notifier, client, gym, coach)
    await engine.dispose()


class Env:
    def __init__(self, clock, engine, linking, store, notifier, client, gym, coach):
        self.clock = clock
        self.engine = engine
        self.linking = linking
        self.store = store
        self.notifier = notifier
        self.client = client
        self.gym = gym
        self.coach = coach
        self.training = TrainingStore(engine, clock=clock)
        self.routines = RoutineStore(engine, clock=clock)
        self._uid = 100

    async def add_member(self, name: str) -> Member:
        self._uid += 1
        return await self.linking.link_member(
            self.gym.id, name, "telegram", str(self._uid)
        )

    async def give_routine(self, member: Member) -> Routine:
        """An agent-generated Routine: Wednesday legs, Friday push."""
        await self.training.ensure_seeded()  # save_routine draws from the catalog
        return await self.routines.save_routine(
            member.id,
            self.gym.id,
            [
                WorkoutSpec(
                    weekday=2,
                    name="Piernas",
                    exercises=[ExerciseSpec("squat", 4, "8-10")],
                ),
                WorkoutSpec(
                    weekday=4,
                    name="Empuje",
                    exercises=[ExerciseSpec("bench press", 3, "10")],
                ),
            ],
        )

    def cookies(self) -> dict[str, str]:
        return {
            SESSION_COOKIE: sign_session(
                self.coach.id, self.gym.id, SECRET, self.clock()
            )
        }

    async def get(self, path: str) -> tuple[int, str]:
        response = await self.client.get(path, cookies=self.cookies())
        return response.status, await response.text()

    async def save_via_web(
        self,
        member_id: int,
        base_routine_id: int | None,
        *days: tuple[str, str, str],
        headers: dict[str, str] | None = None,
    ):
        """POST the editor form; ``days`` are (weekday, workout name, exercises
        textarea body) triples."""
        data: list[tuple[str, str]] = [
            ("base_routine_id", "" if base_routine_id is None else str(base_routine_id))
        ]
        for weekday, name, exercises in days:
            data += [("weekday", weekday), ("workout_name", name), ("exercises", exercises)]
        return await self.client.post(
            f"/members/{member_id}/routine",
            data=data,
            cookies=self.cookies(),
            allow_redirects=False,
            headers=headers or {},
        )


# --- Store level: the stamped save and the stale refusal ---


async def test_a_coach_save_supersedes_stamped_and_locks_out_the_agent(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    saved = await env.routines.save_coach_routine(
        member.id,
        env.gym.id,
        env.coach.id,
        [WorkoutSpec(weekday=0, name="Full body", exercises=[ExerciseSpec("squat", 3, "8")])],
        base_routine_id=old.id,
    )

    assert saved.coach_authored is True
    assert saved.created_by_member_id == env.coach.id
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == saved.id
    assert active["coach_authored"] is True
    assert active["created_by_name"] == "Coach Ana"
    async with env.routines._sessions() as db:
        assert (await db.get(Routine, old.id)).is_active is False

    # The Agent never restructures a coach-authored Routine.
    with pytest.raises(ValueError):
        await env.routines.save_routine(
            member.id,
            env.gym.id,
            [WorkoutSpec(weekday=1, name="X", exercises=[ExerciseSpec("squat")])],
        )


async def test_an_agent_save_stays_unstamped(env):
    member = await env.add_member("Luis")
    routine = await env.give_routine(member)
    assert routine.created_by_member_id is None
    assert (await env.routines.active_routine(member.id))["created_by_name"] is None


async def test_a_stale_save_is_refused_and_changes_nothing(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)
    # The Agent replaced the Routine after the editor loaded.
    newer = await env.routines.save_routine(
        member.id,
        env.gym.id,
        [WorkoutSpec(weekday=1, name="Nuevo", exercises=[ExerciseSpec("squat")])],
    )

    with pytest.raises(StaleRoutineError):
        await env.routines.save_coach_routine(
            member.id,
            env.gym.id,
            env.coach.id,
            [WorkoutSpec(weekday=0, name="Full body", exercises=[ExerciseSpec("squat")])],
            base_routine_id=old.id,
        )

    assert (await env.routines.active_routine(member.id))["routine_id"] == newer.id


# --- Store level: FK blanking, chat path, DB-level guards, index healing ---


async def test_forgetting_the_coach_blanks_the_actor_stamp(tmp_path):
    """Forget-me on the Coach must not trip the created_by_member_id FK
    (Postgres would abort the wipe): the Routines they wrote survive with
    the stamp blanked, and the chip degrades to plain Coach-authored."""
    from agents.extensions.memory import SQLAlchemySession
    from sqlalchemy import event

    from agentg.forget import ForgetStore

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys_on(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    clock = FakeClock()
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    # The SDK's conversation tables, so ForgetStore's history wipe has them.
    await SQLAlchemySession("startup:schema", engine=engine, create_tables=True).get_items(
        limit=1
    )
    gym = await linking.create_gym("Iron Temple")
    coach = await linking.link_member(gym.id, "Coach Ana", "telegram", "1")
    await linking.set_coach(coach.id, True)
    member = await linking.link_member(gym.id, "Luis", "telegram", "2")
    training = TrainingStore(engine, clock=clock)
    routines = RoutineStore(engine, clock=clock)
    await training.ensure_seeded()
    routine = await routines.save_coach_routine(
        member.id,
        gym.id,
        coach.id,
        [WorkoutSpec(weekday=0, name="Full body", exercises=[ExerciseSpec("squat", 3, "8")])],
        base_routine_id=None,
    )

    await ForgetStore(engine).forget_member(coach.id)

    async with routines._sessions() as db:
        kept = await db.get(Routine, routine.id)
        assert kept is not None
        assert kept.created_by_member_id is None
    active = await routines.active_routine(member.id)
    assert active["coach_authored"] is True
    assert active["created_by_name"] is None
    await engine.dispose()


# --- Web-layer tests moved to test_routine_api.py (issue #151) ---
# The htmx Routine editor routes (GET/POST /members/{id}/routine) were retired
# in favour of the JSON API (GET/PUT /api/members/{id}/routine).
# Equivalent coverage lives in test_routine_api.py.


async def test_a_base_routine_of_another_member_is_stale_and_untouched(env):
    """The stale gate's conditional deactivation is member-scoped: a base id
    pointing at ANOTHER Member's active Routine refuses the save and must
    not deactivate that Routine."""
    member = await env.add_member("Luis")
    other = await env.add_member("Mara")
    await env.give_routine(member)
    other_routine = await env.give_routine(other)

    with pytest.raises(StaleRoutineError):
        await env.routines.save_coach_routine(
            member.id,
            env.gym.id,
            env.coach.id,
            [WorkoutSpec(weekday=0, name="X", exercises=[ExerciseSpec("squat")])],
            base_routine_id=other_routine.id,
        )

    active = await env.routines.active_routine(other.id)
    assert active["routine_id"] == other_routine.id


async def test_a_chat_coach_write_is_actor_stamped(env):
    """The chat path keeps this PR's invariant (NULL = written by the
    Agent): a Coach's write_routine from chat carries their stamp, so the
    chip names them like a web save would."""
    from agentg.coaching import write_routine_action
    from agentg.context import MemberContext
    from agentg.stores import Stores

    member = await env.add_member("Luis")
    await env.training.ensure_seeded()
    ctx = MemberContext(
        stores=Stores.from_engine(env.engine, clock=env.clock),
        member_id=env.coach.id,
        gym_id=env.gym.id,
        member_name=env.coach.name,
        gym_name=env.gym.name,
        weight_unit="kg",
        is_coach=True,
    )

    result = await write_routine_action(
        ctx,
        "Luis",
        None,
        [WorkoutSpec(weekday=0, name="Chat plan", exercises=[ExerciseSpec("squat", 3, "8")])],
    )

    assert result.get("routine_id") is not None
    active = await env.routines.active_routine(member.id)
    assert active["coach_authored"] is True
    assert active["created_by_name"] == "Coach Ana"


async def test_two_active_routines_cannot_coexist(env):
    """The DB-level backstop for the base=None race: a second active Routine
    for one Member violates the partial unique index, so an overlap of two
    no-base saves can never produce two active rows."""
    from sqlalchemy.exc import IntegrityError

    member = await env.add_member("Luis")
    await env.give_routine(member)

    async with env.routines._sessions() as db:
        db.add(
            Routine(
                gym_id=env.gym.id,
                member_id=member.id,
                is_active=True,
                created_at=env.clock(),
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_a_competing_save_during_a_no_base_coach_save_is_refused(env, monkeypatch):
    """The no-base gate must hold at WRITE time, not just at check time: an
    active Routine that appears mid-save (here: committed during the catalog
    validation) is refused, never silently deactivated by _save."""
    from agentg import routines as routines_module

    member = await env.add_member("Luis")
    await env.training.ensure_seeded()

    original_find = routines_module.find_exercise
    fired = False

    async def interleaved_find(db, norm):
        nonlocal fired
        if not fired:
            fired = True
            # A competing save lands mid-validation (the Agent, from chat).
            await env.routines.save_routine(
                member.id,
                env.gym.id,
                [WorkoutSpec(weekday=1, name="Del agente", exercises=[ExerciseSpec("squat")])],
            )
        return await original_find(db, norm)

    monkeypatch.setattr(routines_module, "find_exercise", interleaved_find)

    with pytest.raises(StaleRoutineError):
        await env.routines.save_coach_routine(
            member.id,
            env.gym.id,
            env.coach.id,
            [WorkoutSpec(weekday=0, name="Full body", exercises=[ExerciseSpec("squat", 3, "8")])],
            base_routine_id=None,
        )

    # The competing Routine survives, active and untouched.
    active = await env.routines.active_routine(member.id)
    assert [w["name"] for w in active["workouts"]] == ["Del agente"]
    assert active["coach_authored"] is False


async def test_a_conflicting_agent_save_surfaces_a_structured_error(env, monkeypatch):
    """The one-active-per-Member index can fire on the agent/chat path too
    (a web save committing mid-turn): it must surface as a structured
    StaleRoutineError — which write_routine_action turns into a clean
    {"error": ...} payload — never a raw IntegrityError crash."""
    from sqlalchemy.exc import IntegrityError

    from agentg.coaching import write_routine_action
    from agentg.context import MemberContext
    from agentg.stores import Stores

    member = await env.add_member("Luis")
    await env.training.ensure_seeded()

    async def conflicting_save(self, db, *args, **kwargs):
        raise IntegrityError(
            "INSERT INTO routines", {}, Exception("UNIQUE constraint failed")
        )

    monkeypatch.setattr(RoutineStore, "_save", conflicting_save)

    with pytest.raises(StaleRoutineError):
        await env.routines.save_routine(
            member.id,
            env.gym.id,
            [WorkoutSpec(weekday=0, name="X", exercises=[ExerciseSpec("squat")])],
        )

    ctx = MemberContext(
        stores=Stores.from_engine(env.engine, clock=env.clock),
        member_id=env.coach.id,
        gym_id=env.gym.id,
        member_name=env.coach.name,
        gym_name=env.gym.name,
        weight_unit="kg",
        is_coach=True,
    )
    result = await write_routine_action(
        ctx, "Luis", None, [WorkoutSpec(weekday=0, name="X", exercises=[ExerciseSpec("squat")])]
    )
    assert "error" in result


async def test_ensure_schema_heals_dual_active_routines_before_the_index(tmp_path):
    """A legacy database (no unique index yet) may hold a Member with two
    active Routines — pre-PR saves could interleave and commit both.
    ensure_schema must deactivate the extras (the newest survives, the same
    "most recent governs" rule as everywhere else) instead of aborting
    boot on the CREATE UNIQUE INDEX."""
    from sqlalchemy import select, text

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    # Roll back to the legacy schema: no one-active index.
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX uq_routines_one_active_per_member"))
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Luis", "telegram", "2")
    training = TrainingStore(engine, clock=clock)
    routines = RoutineStore(engine, clock=clock)
    await training.ensure_seeded()
    older = await routines.save_routine(
        member.id,
        gym.id,
        [WorkoutSpec(weekday=2, name="Piernas", exercises=[ExerciseSpec("squat")])],
    )
    # A second active row, planted the way a lost pre-index race would.
    async with routines._sessions() as db:
        newer = Routine(
            gym_id=gym.id, member_id=member.id, is_active=True, created_at=clock()
        )
        db.add(newer)
        await db.commit()

    await linking.ensure_schema()  # must not abort

    async with routines._sessions() as db:
        actives = list(
            await db.scalars(
                select(Routine).where(
                    Routine.member_id == member.id, Routine.is_active.is_(True)
                )
            )
        )
        assert [routine.id for routine in actives] == [newer.id]
        assert (await db.get(Routine, older.id)).is_active is False
        # And the index now guards the member.
        db.add(
            Routine(gym_id=gym.id, member_id=member.id, is_active=True, created_at=clock())
        )
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await db.flush()
    await engine.dispose()


# Remaining web-layer tests (notification, validation, htmx in-place) moved
# to test_routine_api.py — the JSON PUT endpoint covers the same scenarios.


# Remaining web-layer tests (validation errors, notification failures, htmx
# in-place saves) moved to test_routine_api.py — the JSON PUT endpoint
# covers the same scenarios.


# --- Flag-off regression: the member page Edit link must work ---


async def test_member_page_edit_link_is_200_with_flag_off(env):
    """The member page renders an Edit link to the server-HTML Routine
    editor.  When spa_enabled is False (production today) that link must
    answer 200 — the SPA cutover is #154's job, not #151's."""
    member = await env.add_member("Luis")
    await env.give_routine(member)

    # Fetch the member page and confirm its Edit link.
    status, body = await env.get("/members/{}".format(member.id))
    assert status == 200
    assert 'href="/members/{}/routine'.format(member.id) in body

    # Follow the Edit link.
    status, _ = await env.get(
        "/members/{}/routine?view=table".format(member.id)
    )
    assert status == 200
