"""Coach dashboard Preset flows (issue #102)."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentg.dashboard_store import DashboardStore
from agentg.dashboard_web import SESSION_COOKIE, build_app, sign_session
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from agentg.models import Member
from agentg.routines import RoutineStore
from agentg.training import TrainingStore
from conftest import FakeClock

SECRET = "preset-secret"


class Notifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.fail_ids: set[str] = set()

    async def send(self, channel: str, channel_user_id: str, text: str) -> None:
        if channel_user_id in self.fail_ids:
            raise RuntimeError("channel down")
        self.sent.append((channel, channel_user_id, text))


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'dashboard-presets.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    notifier = Notifier()
    await linking.ensure_schema()
    await TrainingStore(engine, clock=clock).ensure_seeded()
    gym = await linking.create_gym("Iron Temple")
    coach = await linking.link_member(gym.id, "Coach Ana", "telegram", "1")
    await linking.set_coach(coach.id, True)
    app = build_app(
        store,
        linking,
        session_secret=SECRET,
        bot_username="bot",
        secure_cookies=False,
        clock=clock,
        notifier=notifier,
    )
    async with TestClient(TestServer(app)) as client:
        class Env:
            pass

        result = Env()
        result.engine = engine
        result.clock = clock
        result.linking = linking
        result.store = store
        result.routines = RoutineStore(engine, clock=clock)
        result.gym = gym
        result.coach = coach
        result.notifier = notifier
        result.client = client
        yield result
    await engine.dispose()


def cookies(env) -> dict[str, str]:
    return {
        SESSION_COOKIE: sign_session(env.coach.id, env.gym.id, SECRET, env.clock())
    }


async def add_member(env, name: str, uid: str) -> Member:
    return await env.linking.link_member(env.gym.id, name, "telegram", uid)


async def create_master(env, name: str = "Beginner") -> int:
    preset = await env.routines.create_preset(env.gym.id, name)
    response = await env.client.post(
        f"/presets/{preset.id}/routine",
        data=[
            ("base_routine_id", ""),
            ("weekday", "0"),
            ("workout_name", "Full body"),
            ("exercises", "squat, 3, 8-10"),
        ],
        cookies=cookies(env),
        allow_redirects=False,
    )
    assert response.status == 302
    return preset.id


async def test_presets_index_create_duplicate_and_editor_share_the_coach_gate(env):
    response = await env.client.get("/presets", cookies=cookies(env))
    assert response.status == 200
    assert "Crear Preset" in await response.text()
    assert "/presets" in await response.text()

    response = await env.client.post(
        "/presets", data={"name": " Beginner "}, cookies=cookies(env), allow_redirects=False
    )
    assert response.status == 302
    response = await env.client.post(
        "/presets", data={"name": "Beginner"}, cookies=cookies(env)
    )
    assert response.status == 400
    assert "Ya existe un Preset" in await response.text()


async def test_preset_editor_reuses_validation_and_unknown_ids_are_404(env):
    preset_id = await create_master(env)
    response = await env.client.get(
        f"/presets/{preset_id}/routine", cookies=cookies(env)
    )
    assert response.status == 200
    assert "Preset: Beginner" in await response.text()
    response = await env.client.post(
        f"/presets/{preset_id}/routine",
        data={"base_routine_id": "999999", "weekday": "0", "workout_name": "X", "exercises": "squat"},
        cookies=cookies(env),
    )
    assert response.status == 409
    response = await env.client.get("/presets/not-an-id/routine", cookies=cookies(env))
    assert response.status == 404


async def test_apply_multi_and_all_notifies_each_member_and_never_coach(env, caplog):
    preset_id = await create_master(env)
    first = await add_member(env, "Luis", "2")
    second = await add_member(env, "Mara", "3")
    response = await env.client.post(
        f"/presets/{preset_id}/apply",
        data=[("member_ids", str(first.id)), ("member_ids", str(second.id))],
        cookies=cookies(env),
        allow_redirects=False,
    )
    assert response.status == 302
    assert {sent[1] for sent in env.notifier.sent} == {"2", "3"}
    assert await env.routines.active_routine(first.id)

    env.notifier.fail_ids.add("2")
    response = await env.client.post(
        f"/presets/{preset_id}/apply",
        data={"apply_all": "1"},
        cookies=cookies(env),
        allow_redirects=False,
    )
    assert response.status == 302
    assert "failed to notify member" in caplog.text
    assert await env.routines.active_routine(second.id)
    assert await env.routines.active_routine(env.coach.id) is None


async def test_member_chip_prefers_preset_name_over_coach_authored(env):
    preset_id = await create_master(env)
    member = await add_member(env, "Luis", "2")
    await env.routines.apply_preset(preset_id, env.gym.id, env.coach.id, [member.id])
    response = await env.client.get(f"/members/{member.id}/routine", cookies=cookies(env))
    assert response.status == 200
    body = await response.text()
    assert "Preset: Beginner" in body
    assert "Escrita por un coach" not in body
