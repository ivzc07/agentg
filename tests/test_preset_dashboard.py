"""Coach dashboard Preset flows (issue #102)."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentg.dashboard_store import DashboardStore
from agentg.dashboard_web import SESSION_COOKIE, build_app, sign_session
from agentg.dashboard_web import CONSEQUENCE_LINE, STALE_ERROR
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
        spa_enabled=True,
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


async def test_preset_master_editor_shows_its_own_consequence_not_the_member_warning(env):
    preset_id = await create_master(env)

    response = await env.client.get(
        f"/presets/{preset_id}/routine", cookies=cookies(env)
    )

    assert response.status == 200
    body = await response.text()
    assert "Guardar actualiza a todos los miembros que siguen este Preset." in body
    assert CONSEQUENCE_LINE not in body

    # The rejection re-render is the same page: a refused master save must
    # not fall back to the Member's fork warning either.
    refused = await env.client.post(
        f"/presets/{preset_id}/routine",
        data={
            "base_routine_id": "999999",
            "weekday": "0",
            "workout_name": "X",
            "exercises": "squat",
        },
        cookies=cookies(env),
    )

    assert refused.status == 409
    body = await refused.text()
    assert "Guardar actualiza a todos los miembros que siguen este Preset." in body
    assert CONSEQUENCE_LINE not in body


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
            f"/presets/{preset_id}/apply",
            data={"member_ids": str(member_id)},
            cookies=cookies(env),
        )
        assert response.status == 404
        assert await env.routines.active_routine(member_id) is None


async def test_apply_without_a_master_rerenders_presets_with_a_coach_error(env):
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    member = await add_member(env, "Luis", "2")
    response = await env.client.post(
        f"/presets/{preset.id}/apply",
        data={"member_ids": str(member.id)},
        cookies=cookies(env),
    )
    assert response.status == 400
    assert "Escribe el plan del Preset antes de aplicarlo." in await response.text()


async def test_apply_without_selection_rerenders_presets_with_a_coach_error(env):
    preset_id = await create_master(env)
    response = await env.client.post(
        f"/presets/{preset_id}/apply", data={}, cookies=cookies(env)
    )
    assert response.status == 400
    assert "Elige al menos un miembro." in await response.text()


async def test_stale_apply_rerenders_presets_with_the_stale_error(env, monkeypatch):
    preset_id = await create_master(env)
    member = await add_member(env, "Luis", "2")

    async def stale(*args, **kwargs):
        from agentg.routines import StaleRoutineError

        raise StaleRoutineError("concurrent apply")

    monkeypatch.setattr(env.store, "apply_preset", stale)
    response = await env.client.post(
        f"/presets/{preset_id}/apply",
        data={"member_ids": str(member.id)},
        cookies=cookies(env),
    )
    assert response.status == 409
    assert STALE_ERROR in await response.text()


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
    master = await env.routines.preset_master(preset_id)

    response = await env.client.post(
        f"/presets/{preset_id}/routine",
        data=[
            ("base_routine_id", str(master["routine_id"])),
            ("weekday", "0"),
            ("workout_name", "Refreshed"),
            ("exercises", "squat, 3, 8"),
        ],
        cookies=cookies(env),
        allow_redirects=False,
    )

    assert response.status == 302
    assert [sent[1] for sent in env.notifier.sent] == ["2"]
    assert "Coach Ana" in env.notifier.sent[0][2]
    assert (await env.routines.active_routine(linked.id))["workouts"][0]["name"] == "Refreshed"
    assert (await env.routines.active_routine(forked.id))["workouts"][0]["name"] == "Mara fork"

    env.notifier.fail_ids.add("2")
    master = await env.routines.preset_master(preset_id)
    response = await env.client.post(
        f"/presets/{preset_id}/routine",
        data=[
            ("base_routine_id", str(master["routine_id"])),
            ("weekday", "0"),
            ("workout_name", "Refreshed again"),
            ("exercises", "squat, 3, 8"),
        ],
        cookies=cookies(env),
        allow_redirects=False,
    )
    assert response.status == 302
    assert "failed to notify member" in caplog.text

    original_member_channel = env.store.member_channel

    async def no_channel(member_id):
        if member_id == linked.id:
            return None
        return await original_member_channel(member_id)

    monkeypatch.setattr(env.store, "member_channel", no_channel)
    master = await env.routines.preset_master(preset_id)
    response = await env.client.post(
        f"/presets/{preset_id}/routine",
        data=[
            ("base_routine_id", str(master["routine_id"])),
            ("weekday", "0"),
            ("workout_name", "Refreshed without channel"),
            ("exercises", "squat, 3, 8"),
        ],
        cookies=cookies(env),
        allow_redirects=False,
    )
    assert response.status == 302
    assert "no channel" in caplog.text


async def test_presets_can_move_the_default_slot_and_retire_it(env):
    first = await env.routines.create_preset(env.gym.id, "Beginner")
    second = await env.routines.create_preset(env.gym.id, "Advanced")

    response = await env.client.post(
        f"/presets/{first.id}/default", cookies=cookies(env), allow_redirects=False
    )
    assert response.status == 302
    assert await env.store.default_preset_id(env.gym.id) == first.id
    response = await env.client.post(
        f"/presets/{second.id}/default", cookies=cookies(env), allow_redirects=False
    )
    assert response.status == 302
    assert await env.store.default_preset_id(env.gym.id) == second.id

    response = await env.client.post(
        f"/presets/{second.id}/default", cookies=cookies(env), allow_redirects=False
    )
    assert response.status == 302
    assert await env.store.default_preset_id(env.gym.id) is None

    response = await env.client.post(
        f"/presets/{second.id}/retire", cookies=cookies(env), allow_redirects=False
    )
    assert response.status == 302
    assert await env.store.default_preset_id(env.gym.id) is None
    assert (await env.client.get(f"/presets/{second.id}/routine", cookies=cookies(env))).status == 404


async def test_default_and_retire_reject_a_foreign_or_missing_preset(env):
    foreign_gym = await env.linking.create_gym("Other Gym")
    foreign = await env.routines.create_preset(foreign_gym.id, "Foreign")
    for preset_id in (foreign.id, 999999):
        default = await env.client.post(
            f"/presets/{preset_id}/default", cookies=cookies(env)
        )
        retire = await env.client.post(
            f"/presets/{preset_id}/retire", cookies=cookies(env)
        )
        assert default.status == 404
        assert retire.status == 404
    assert await env.store.default_preset_id(env.gym.id) is None
    assert [item.id for item in await env.routines.presets(foreign_gym.id)] == [foreign.id]


async def test_an_htmx_master_save_returns_the_editor_in_place(env):
    preset_id = await create_master(env)
    master = await env.store.preset_master(preset_id)

    response = await env.client.post(
        f"/presets/{preset_id}/routine",
        data=[
            ("base_routine_id", str(master["routine_id"])),
            ("weekday", "1"),
            ("workout_name", "Full body B"),
            ("exercises", "bench press, 3, 10"),
        ],
        cookies=cookies(env),
        headers={"HX-Request": "true"},
        allow_redirects=False,
    )

    assert response.status == 200
    text = await response.text()
    assert "<!DOCTYPE" not in text
    assert text.lstrip().startswith('<div id="editor-root"')
    assert "Preset guardado" in text


async def test_preset_writes_redirect_with_their_done_keys(env):
    """Issue #129: every redirect write carries its confirmation key."""
    preset_id = await create_master(env)
    member = await add_member(env, "Marta", "201")

    created = await env.client.post(
        "/presets", data={"name": "Cutting"}, cookies=cookies(env), allow_redirects=False
    )
    assert created.headers["Location"] == "/presets?done=preset_created"

    applied = await env.client.post(
        f"/presets/{preset_id}/apply",
        data={"member_ids": str(member.id)},
        cookies=cookies(env),
        allow_redirects=False,
    )
    assert applied.headers["Location"] == "/presets?done=preset_applied"

    default_set = await env.client.post(
        f"/presets/{preset_id}/default", cookies=cookies(env), allow_redirects=False
    )
    assert default_set.headers["Location"] == "/presets?done=default_set"
    cleared = await env.client.post(
        f"/presets/{preset_id}/default", cookies=cookies(env), allow_redirects=False
    )
    assert cleared.headers["Location"] == "/presets?done=default_cleared"

    retired = await env.client.post(
        f"/presets/{preset_id}/retire", cookies=cookies(env), allow_redirects=False
    )
    assert retired.headers["Location"] == "/presets?done=preset_retired"


async def test_the_presets_page_renders_a_known_done_notice_and_ignores_garbage(env):
    await create_master(env)

    page = await env.client.get("/presets?done=preset_applied", cookies=cookies(env))
    text = await page.text()
    assert "notice-ok" in text and "Preset aplicado." in text

    garbage = await env.client.get(
        "/presets?done=%3Cscript%3Ealert(1)%3C/script%3E", cookies=cookies(env)
    )
    text = await garbage.text()
    assert "notice-ok" not in text and "<script>alert(1)" not in text
