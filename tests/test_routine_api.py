"""The Routine editor JSON API (issue #151).

GET /api/members/{id}/routine — returns routine data + catalog.
PUT /api/members/{id}/routine — saves a coach-authored Routine through JSON.
Both endpoints reuse require_coach auth.
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
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.training import TrainingStore
from conftest import FakeClock

SECRET = "test-secret"


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
        await self.training.ensure_seeded()
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


# --- GET /api/members/{id}/routine ---


async def test_get_routine_returns_structure_and_catalog(env):
    member = await env.add_member("Luis")
    await env.give_routine(member)

    response = await env.client.get(
        f"/api/members/{member.id}/routine", cookies=env.cookies()
    )

    assert response.status == 200
    body = await response.json()
    assert body["member_id"] == member.id
    assert body["name"] == "Luis"
    assert body["coach_authored"] is False
    assert body["routine_author"] is None
    assert body["routine_preset_name"] is None
    assert body["routine_id"] is not None
    assert len(body["routine"]) == 2
    assert body["routine"][0] == {
        "weekday": 2,
        "name": "Piernas",
        "exercises": [{"exercise": "squat", "sets": 4, "reps": "8-10"}],
    }
    assert body["routine"][1] == {
        "weekday": 4,
        "name": "Empuje",
        "exercises": [{"exercise": "bench press", "sets": 3, "reps": "10"}],
    }
    assert "squat" in body["catalog"]
    assert "bench press" in body["catalog"]


async def test_get_routine_empty_when_no_routine(env):
    member = await env.add_member("Luis")

    response = await env.client.get(
        f"/api/members/{member.id}/routine", cookies=env.cookies()
    )

    assert response.status == 200
    body = await response.json()
    assert body["routine"] == []
    assert body["routine_id"] is None
    assert body["catalog"] == []


async def test_get_routine_requires_auth(env):
    member = await env.add_member("Luis")
    await env.give_routine(member)

    response = await env.client.get(f"/api/members/{member.id}/routine")
    assert response.status == 401


async def test_get_routine_404_for_unknown_member(env):
    response = await env.client.get(
        "/api/members/99999/routine", cookies=env.cookies()
    )
    assert response.status == 404


async def test_get_routine_404_for_foreign_member(env):
    other_gym = await env.linking.create_gym("Other Gym")
    outsider = await env.linking.link_member(other_gym.id, "Mara", "telegram", "7")

    response = await env.client.get(
        f"/api/members/{outsider.id}/routine", cookies=env.cookies()
    )
    assert response.status == 404


async def test_get_routine_404_for_coach(env):
    response = await env.client.get(
        f"/api/members/{env.coach.id}/routine", cookies=env.cookies()
    )
    assert response.status == 404


# --- PUT /api/members/{id}/routine ---


async def test_put_saves_a_new_routine(env):
    member = await env.add_member("Luis")
    await env.training.ensure_seeded()

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": None,
            "workouts": [
                {
                    "weekday": 0,
                    "name": "Full body",
                    "exercises": [
                        {"exercise": "squat", "sets": 3, "reps": "8"},
                        {"exercise": "bench press", "sets": 3, "reps": "10"},
                    ],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True
    assert body["routine_id"] is not None
    assert body["coach_authored"] is True
    assert body["routine_author"] == "Coach Ana"
    assert body["routine_preset_name"] is None
    assert len(body["routine"]) == 1
    assert body["routine"][0]["name"] == "Full body"
    assert body["notified"] is True
    assert len(env.notifier.sent) == 1
    assert "Full body" in env.notifier.sent[0][2]


async def test_put_supersedes_existing_routine(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": old.id,
            "workouts": [
                {
                    "weekday": 0,
                    "name": "Full body",
                    "exercises": [{"exercise": "squat", "sets": 3, "reps": "8"}],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 200
    body = await response.json()
    assert body["coach_authored"] is True
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == body["routine_id"]
    assert active["coach_authored"] is True
    assert active["created_by_name"] == "Coach Ana"


async def test_put_refuses_stale_save(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)
    # The Agent replaces the Routine after the editor loaded.
    await env.routines.save_routine(
        member.id,
        env.gym.id,
        [WorkoutSpec(weekday=1, name="Nuevo plan", exercises=[ExerciseSpec("squat")])],
    )

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": old.id,
            "workouts": [
                {
                    "weekday": 0,
                    "name": "Full body",
                    "exercises": [{"exercise": "squat", "sets": 3, "reps": "8"}],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 409
    body = await response.json()
    assert "error" in body
    assert "fresh_routine" in body
    assert body["fresh_routine_id"] is not None
    assert body["fresh_routine"][0]["name"] == "Nuevo plan"
    # Nothing was written.
    active = await env.routines.active_routine(member.id)
    assert [w["name"] for w in active["workouts"]] == ["Nuevo plan"]
    assert env.notifier.sent == []


async def test_put_refuses_stale_save_from_empty(env):
    """The editor loaded with NO active Routine (base None): if the Agent
    saved one in the meantime, the PUT must refuse with the fresh plan."""
    member = await env.add_member("Luis")
    await env.training.ensure_seeded()
    await env.routines.save_routine(
        member.id,
        env.gym.id,
        [WorkoutSpec(weekday=1, name="Del agente", exercises=[ExerciseSpec("squat")])],
    )

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": None,
            "workouts": [
                {
                    "weekday": 0,
                    "name": "Full body",
                    "exercises": [{"exercise": "squat", "sets": 3, "reps": "8"}],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 409
    body = await response.json()
    assert body["fresh_routine"][0]["name"] == "Del agente"
    active = await env.routines.active_routine(member.id)
    assert [w["name"] for w in active["workouts"]] == ["Del agente"]
    assert env.notifier.sent == []


async def test_put_refuses_unknown_exercises(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": old.id,
            "workouts": [
                {
                    "weekday": 0,
                    "name": "Full body",
                    "exercises": [
                        {"exercise": "sentadilla mágica", "sets": 3, "reps": "8"},
                    ],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 400
    body = await response.json()
    assert "no están en el catálogo" in body["error"]
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id
    assert env.notifier.sent == []


async def test_put_refuses_duplicate_weekdays(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": old.id,
            "workouts": [
                {
                    "weekday": 0,
                    "name": "Piernas",
                    "exercises": [{"exercise": "squat", "sets": 4, "reps": "8-10"}],
                },
                {
                    "weekday": 0,
                    "name": "Empuje",
                    "exercises": [{"exercise": "bench press", "sets": 3, "reps": "10"}],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 400
    body = await response.json()
    assert "solo puede aparecer una vez" in body["error"]
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id


async def test_put_refuses_empty_workout(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": old.id,
            "workouts": [
                {"weekday": 0, "name": "Full body", "exercises": []},
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 400
    assert "necesita al menos un ejercicio" in (await response.json())["error"]


async def test_put_refuses_empty_routine(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={"base_routine_id": old.id, "workouts": []},
        cookies=env.cookies(),
    )

    assert response.status == 400
    body = await response.json()
    assert "necesita al menos un día" in body["error"]


async def test_put_refuses_bad_weekday(env):
    member = await env.add_member("Luis")

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": None,
            "workouts": [
                {"weekday": 7, "name": "X", "exercises": [{"exercise": "squat"}]},
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 400
    assert "no es válido" in (await response.json())["error"]


async def test_put_refuses_bad_sets(env):
    member = await env.add_member("Luis")

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": None,
            "workouts": [
                {
                    "weekday": 0,
                    "name": "X",
                    "exercises": [{"exercise": "squat", "sets": 0, "reps": "8"}],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 400
    body = await response.json()
    assert "entre 1 y 99" in body["error"]


async def test_put_refuses_overlong_workout_name(env):
    member = await env.add_member("Luis")

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": None,
            "workouts": [
                {
                    "weekday": 0,
                    "name": "x" * 110,
                    "exercises": [{"exercise": "squat"}],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 400
    assert "100 caracteres" in (await response.json())["error"]


async def test_put_requires_auth(env):
    member = await env.add_member("Luis")

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={"base_routine_id": None, "workouts": []},
    )
    assert response.status == 401


async def test_put_404_for_foreign_member(env):
    other_gym = await env.linking.create_gym("Other Gym")
    outsider = await env.linking.link_member(other_gym.id, "Mara", "telegram", "7")

    response = await env.client.put(
        f"/api/members/{outsider.id}/routine",
        json={
            "base_routine_id": None,
            "workouts": [
                {"weekday": 0, "name": "X", "exercises": [{"exercise": "squat"}]},
            ],
        },
        cookies=env.cookies(),
    )
    assert response.status == 404
    assert await env.routines.active_routine(outsider.id) is None


async def test_put_notifier_failure_does_not_block_save(env):
    member = await env.add_member("Luis")
    await env.training.ensure_seeded()
    env.notifier.fail = True

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": None,
            "workouts": [
                {
                    "weekday": 0,
                    "name": "Full body",
                    "exercises": [{"exercise": "squat", "sets": 3, "reps": "8"}],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True
    assert body["notified"] is False
    active = await env.routines.active_routine(member.id)
    assert active["coach_authored"] is True


async def test_put_editing_linked_member_forks_and_reports_detached(env):
    """Editing a Preset-linked Member silently forks: the save succeeds,
    preset_name goes to None, and consequence follows."""
    await env.training.ensure_seeded()
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    await env.routines.save_preset_master(
        preset.id,
        env.gym.id,
        env.coach.id,
        [WorkoutSpec(weekday=0, name="Preset day", exercises=[ExerciseSpec("squat")])],
        base_routine_id=None,
    )
    member = await env.add_member("Luis")
    await env.routines.apply_preset(preset.id, env.gym.id, env.coach.id, [member.id])
    old = await env.routines.active_routine(member.id)

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": old["routine_id"],
            "workouts": [
                {
                    "weekday": 1,
                    "name": "Forked day",
                    "exercises": [{"exercise": "bench press", "sets": 3, "reps": "8"}],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 200
    body = await response.json()
    assert body["coach_authored"] is True
    assert body["routine_preset_name"] is None  # detached from preset
    active = await env.routines.active_routine(member.id)
    assert active["preset_id"] is None


# --- Language: errors follow the Accept-Language header (cookieless first visit) ---


async def test_put_errors_follow_language(env):
    """A PUT refusal uses the browser's Accept-Language when no language
    cookie is set."""
    member = await env.add_member("Luis")

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={"base_routine_id": None, "workouts": []},
        cookies=env.cookies(),
        headers={"Accept-Language": "en"},
    )

    assert response.status == 400
    body = await response.json()
    assert "needs at least one day" in body["error"]


async def test_first_routine_write_via_api_is_coach_stamped(env):
    """A first Routine written via PUT from an empty editor is stamped
    coach-authored, like the htmx path."""
    member = await env.add_member("Luis")
    await env.training.ensure_seeded()

    response = await env.client.put(
        f"/api/members/{member.id}/routine",
        json={
            "base_routine_id": None,
            "workouts": [
                {
                    "weekday": 2,
                    "name": "Piernas",
                    "exercises": [{"exercise": "squat", "sets": 4, "reps": "8-10"}],
                },
            ],
        },
        cookies=env.cookies(),
    )

    assert response.status == 200
    body = await response.json()
    assert body["coach_authored"] is True
    assert body["routine_author"] == "Coach Ana"
    active = await env.routines.active_routine(member.id)
    assert active is not None
    assert active["coach_authored"] is True
    assert active["created_by_name"] == "Coach Ana"
