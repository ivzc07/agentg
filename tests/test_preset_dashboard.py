"""Coach dashboard Preset flows (issue #102), on the JSON API since the
React cutover (#154).

The Presets screen itself is React (PresetsPage RTL covers the chrome,
notices, and forms); what lives here is the web-layer contract the screen
runs on: create/apply/default/retire plus the master editor's save,
notification, and scoping semantics — the parts a Coach's data depends on.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentg.dashboard_store import DashboardStore
from agentg.dashboard_web import SESSION_COOKIE, build_app, sign_session
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from agentg.models import Member
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
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
    """A Preset with a master written through the JSON editor endpoint."""
    preset = await env.routines.create_preset(env.gym.id, name)
    response = await env.client.put(
        f"/api/presets/{preset.id}/routine",
        json={
            "base_routine_id": None,
            "workouts": [
                {
                    "weekday": 0,
                    "name": "Full body",
                    "exercises": [{"exercise": "squat", "sets": 3, "reps": "8-10"}],
                },
            ],
        },
        cookies=cookies(env),
        allow_redirects=False,
    )
    assert response.status == 200
    return preset.id


async def test_presets_screen_and_create_share_the_coach_gate(env):
    # The screen URL never hands the shell to an anonymous visitor…
    anonymous = await env.client.get("/presets")
    assert 'id="root"' not in await anonymous.text()
    # …and serves it to a signed-in coach.  (Order matters: the aiohttp
    # test client keeps cookies, and the authed visit refreshes a session
    # cookie into its jar.)
    response = await env.client.get("/presets", cookies=cookies(env))
    assert response.status == 200
    assert 'id="root"' in await response.text()

    # Creation and the duplicate refusal live on the API.
    response = await env.client.post(
        "/api/presets", json={"name": " Beginner "}, cookies=cookies(env)
    )
    assert response.status == 201
    response = await env.client.post(
        "/api/presets", json={"name": "Beginner"}, cookies=cookies(env)
    )
    assert response.status == 400
    assert (await response.json())["error"] == "duplicate_preset_name"


async def test_preset_editor_reuses_validation_and_unknown_ids_are_404(env):
    preset_id = await create_master(env)
    response = await env.client.get(
        f"/api/presets/{preset_id}/routine", cookies=cookies(env)
    )
    assert response.status == 200
    assert (await response.json())["name"] == "Beginner"
    response = await env.client.put(
        f"/api/presets/{preset_id}/routine",
        json={
            "base_routine_id": 999999,
            "workouts": [
                {"weekday": 0, "name": "X", "exercises": [{"exercise": "squat"}]},
            ],
        },
        cookies=cookies(env),
    )
    assert response.status == 409
    response = await env.client.get(
        "/api/presets/not-an-id/routine", cookies=cookies(env)
    )
    assert response.status == 404


async def test_apply_multi_and_all_notifies_each_member_and_never_coach(env, caplog):
    preset_id = await create_master(env)
    first = await add_member(env, "Luis", "2")
    second = await add_member(env, "Mara", "3")
    response = await env.client.post(
        f"/api/presets/{preset_id}/apply",
        json={"member_ids": [first.id, second.id]},
        cookies=cookies(env),
        allow_redirects=False,
    )
    assert response.status == 200
    assert (await response.json())["applied"] == 2
    assert {sent[1] for sent in env.notifier.sent} == {"2", "3"}
    assert await env.routines.active_routine(first.id)

    env.notifier.fail_ids.add("2")
    response = await env.client.post(
        f"/api/presets/{preset_id}/apply",
        json={"apply_all": True},
        cookies=cookies(env),
        allow_redirects=False,
    )
    assert response.status == 200
    assert "failed to notify member" in caplog.text
    assert await env.routines.active_routine(second.id)
    assert await env.routines.active_routine(env.coach.id) is None


async def test_member_chip_prefers_preset_name_over_coach_authored(env):
    preset_id = await create_master(env)
    member = await add_member(env, "Luis", "2")
    await env.routines.apply_preset(preset_id, env.gym.id, env.coach.id, [member.id])
    response = await env.client.get(
        f"/api/members/{member.id}/routine", cookies=cookies(env)
    )
    assert response.status == 200
    body = await response.json()
    assert body["routine_preset_name"] == "Beginner"
    assert body["coach_authored"] is True
    assert body["routine_author"] == "Coach Ana"


async def test_apply_rejects_a_foreign_or_coach_member_without_writing(env):
    preset_id = await create_master(env)
    foreign_gym = await env.linking.create_gym("Other Gym")
    foreign_member = await env.linking.link_member(foreign_gym.id, "Mara", "telegram", "7")
    for member_id in (foreign_member.id, env.coach.id):
        response = await env.client.post(
            f"/api/presets/{preset_id}/apply",
            json={"member_ids": [member_id]},
            cookies=cookies(env),
        )
        assert response.status == 404
        assert await env.routines.active_routine(member_id) is None


async def test_apply_without_a_master_answers_a_structured_400(env):
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    member = await add_member(env, "Luis", "2")
    response = await env.client.post(
        f"/api/presets/{preset.id}/apply",
        json={"member_ids": [member.id]},
        cookies=cookies(env),
    )
    assert response.status == 400
    assert (await response.json())["error"] == "preset_no_master"


async def test_apply_without_selection_answers_a_structured_400(env):
    preset_id = await create_master(env)
    response = await env.client.post(
        f"/api/presets/{preset_id}/apply", json={}, cookies=cookies(env)
    )
    assert response.status == 400
    assert (await response.json())["error"] == "preset_no_selection"


async def test_stale_apply_answers_409(env, monkeypatch):
    preset_id = await create_master(env)
    member = await add_member(env, "Luis", "2")

    async def stale(*args, **kwargs):
        from agentg.routines import StaleRoutineError

        raise StaleRoutineError("concurrent apply")

    monkeypatch.setattr(env.store, "apply_preset", stale)
    response = await env.client.post(
        f"/api/presets/{preset_id}/apply",
        json={"member_ids": [member.id]},
        cookies=cookies(env),
    )
    assert response.status == 409
    assert (await response.json())["error"] == "stale_error"


async def test_editing_a_preset_notifies_only_members_still_linked_to_it(env, monkeypatch, caplog):
    preset_id = await create_master(env)
    linked = await add_member(env, "Luis", "2")
    forked = await add_member(env, "Mara", "3")
    await env.routines.apply_preset(preset_id, env.gym.id, env.coach.id, [linked.id, forked.id])
    forked_active = await env.routines.active_routine(forked.id)
    await env.routines.save_coach_routine(
        forked.id,
        env.gym.id,
        env.coach.id,
        [WorkoutSpec(weekday=1, name="Mara fork", exercises=[ExerciseSpec("bench press")])],
        base_routine_id=forked_active["routine_id"],
    )
    env.notifier.sent.clear()

    async def save_master(name: str) -> None:
        master = await env.routines.preset_master(preset_id)
        response = await env.client.put(
            f"/api/presets/{preset_id}/routine",
            json={
                "base_routine_id": master["routine_id"],
                "workouts": [
                    {
                        "weekday": 0,
                        "name": name,
                        "exercises": [{"exercise": "squat", "sets": 3, "reps": "8"}],
                    },
                ],
            },
            cookies=cookies(env),
            allow_redirects=False,
        )
        assert response.status == 200

    await save_master("Refreshed")
    assert [sent[1] for sent in env.notifier.sent] == ["2"]
    assert "Coach Ana" in env.notifier.sent[0][2]
    assert (await env.routines.active_routine(linked.id))["workouts"][0]["name"] == "Refreshed"
    assert (await env.routines.active_routine(forked.id))["workouts"][0]["name"] == "Mara fork"

    env.notifier.fail_ids.add("2")
    await save_master("Refreshed again")
    assert "failed to notify member" in caplog.text

    original_member_channel = env.store.member_channel

    async def no_channel(member_id):
        if member_id == linked.id:
            return None
        return await original_member_channel(member_id)

    monkeypatch.setattr(env.store, "member_channel", no_channel)
    await save_master("Refreshed without channel")
    assert "no channel" in caplog.text


async def test_presets_can_move_the_default_slot_and_retire_it(env):
    first = await env.routines.create_preset(env.gym.id, "Beginner")
    second = await env.routines.create_preset(env.gym.id, "Advanced")

    response = await env.client.post(
        f"/api/presets/{first.id}/default", json={}, cookies=cookies(env)
    )
    assert response.status == 200
    assert await env.store.default_preset_id(env.gym.id) == first.id
    response = await env.client.post(
        f"/api/presets/{second.id}/default", json={}, cookies=cookies(env)
    )
    assert response.status == 200
    assert await env.store.default_preset_id(env.gym.id) == second.id

    # A second set on the same Preset clears the slot (the toggle).
    response = await env.client.post(
        f"/api/presets/{second.id}/default", json={}, cookies=cookies(env)
    )
    assert response.status == 200
    assert await env.store.default_preset_id(env.gym.id) is None

    response = await env.client.post(
        f"/api/presets/{second.id}/retire", json={}, cookies=cookies(env)
    )
    assert response.status == 200
    assert await env.store.default_preset_id(env.gym.id) is None
    # A retired Preset's master editor is gone with it.
    gone = await env.client.get(
        f"/api/presets/{second.id}/routine", cookies=cookies(env)
    )
    assert gone.status == 404


async def test_default_and_retire_reject_a_foreign_or_missing_preset(env):
    foreign_gym = await env.linking.create_gym("Other Gym")
    foreign = await env.routines.create_preset(foreign_gym.id, "Foreign")
    for preset_id in (foreign.id, 999999):
        default = await env.client.post(
            f"/api/presets/{preset_id}/default", json={}, cookies=cookies(env)
        )
        retire = await env.client.post(
            f"/api/presets/{preset_id}/retire", json={}, cookies=cookies(env)
        )
        assert default.status == 404
        assert retire.status == 404
    assert await env.store.default_preset_id(env.gym.id) is None
    assert [item.id for item in await env.routines.presets(foreign_gym.id)] == [foreign.id]
