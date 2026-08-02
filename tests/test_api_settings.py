"""/api/settings and its write routes (issue #153), end to end over a real
aiohttp server.

Covers the JSON contract for the tenant Settings screen: gym name, both
invite URLs, the typed confirm for regenerations, and flag-off 404s.
"""

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import FakeClock

from agentg.dashboard_store import DashboardStore
from agentg.dashboard_web import SESSION_COOKIE, build_app, sign_session
from agentg.db import create_engine
from agentg.linking_store import LinkingStore

SECRET = "test-secret"
BOT_USERNAME = "testbot"

REGENERATE_CONFIRM_ES = "regenerar"
REGENERATE_CONFIRM_EN = "regenerate"


async def _setup_stores(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Ana", "telegram", "42")
    await linking.set_coach(member.id, True)
    return engine, clock, linking, store, gym, member


def _cookie(member_id, gym_id, clock):
    return {SESSION_COOKIE: sign_session(member_id, gym_id, SECRET, clock())}


@pytest.fixture
async def spa_env(tmp_path):
    """A test app with spa_enabled=True so /api/settings routes are wired."""
    engine, clock, linking, store, gym, member = await _setup_stores(tmp_path)

    stub_dist = tmp_path / "dist"
    stub_dist.mkdir(parents=True, exist_ok=True)
    (stub_dist / "index.html").write_text(
        '<!DOCTYPE html><html><head><title>SPA</title></head>'
        '<body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    (stub_dist / "assets").mkdir(exist_ok=True)
    (stub_dist / "assets" / "index.js").write_text("// stub", encoding="utf-8")



    # Patch _FRONTEND_DIST and basename to avoid the conditional
    app = build_app(
        store,
        linking,
        session_secret=SECRET,
        bot_username=BOT_USERNAME,
        secure_cookies=False,
        clock=clock,
        spa_enabled=True,
        spa_dist=stub_dist,
    )
    async with TestClient(TestServer(app)) as client:
        yield SimpleEnv(linking, store, client, gym, member, clock)
    await engine.dispose()


class SimpleEnv:
    def __init__(self, linking, store, client, gym, member, clock):
        self.linking = linking
        self.store = store
        self.client = client
        self.gym = gym
        self.member = member
        self.clock = clock

    def cookie(self):
        return _cookie(self.member.id, self.gym.id, self.clock)


# --- /api/settings GET ---


async def test_api_settings_returns_full_shape(spa_env):
    """GET /api/settings returns gym name, both invite codes, and the bot username."""
    response = await spa_env.client.get(
        "/api/settings", cookies=spa_env.cookie()
    )

    assert response.status == 200
    assert response.content_type == "application/json"
    data = json.loads(await response.text())

    assert data["gym_name"] == "Iron Temple"
    assert data["invite_code"] == spa_env.gym.invite_code
    assert data["coach_invite_code"] == spa_env.gym.coach_invite_code
    assert data["bot_username"] == BOT_USERNAME
    # The invite URL is composed from bot_username + invite_code
    assert "t.me" in data["invite_url"]
    assert data["invite_code"] in data["invite_url"]
    # QR SVG is included (generated server-side for the SPA)
    assert "<svg" in data["qr_svg"]
    assert data["coach_invite_url"] is not None
    assert data["coach_invite_code"] in data["coach_invite_url"]


async def test_api_settings_rejects_unauthenticated(spa_env):
    """Without a valid session cookie /api/settings answers 401."""
    response = await spa_env.client.get("/api/settings")
    assert response.status == 401


async def test_api_settings_rejects_forged_cookie(spa_env):
    """A forged session cookie does not open /api/settings."""
    forged = sign_session(spa_env.member.id, spa_env.gym.id, "wrong-secret", spa_env.clock())
    response = await spa_env.client.get(
        "/api/settings", cookies={SESSION_COOKIE: forged}
    )
    assert response.status == 401


async def test_api_settings_rejects_demoted_coach(spa_env):
    """A demoted coach with a live cookie gets 401."""
    await spa_env.linking.set_coach(spa_env.member.id, False)
    response = await spa_env.client.get(
        "/api/settings", cookies=spa_env.cookie()
    )
    assert response.status == 401


async def test_api_settings_slides_session_cookie(spa_env):
    """A successful /api/settings call refreshes the 90-day session cookie."""
    response = await spa_env.client.get(
        "/api/settings", cookies=spa_env.cookie()
    )
    assert response.status == 200
    assert SESSION_COOKIE in response.cookies


# --- /api/settings/regenerate-invite POST ---


async def test_api_regenerate_invite_requires_confirm_word(spa_env):
    """Without the correct confirm word, the invite code is not regenerated."""
    old_code = spa_env.gym.invite_code

    # Wrong confirm
    response = await spa_env.client.post(
        "/api/settings/regenerate-invite",
        json={"confirm": "no"},
        cookies=spa_env.cookie(),
    )
    assert response.status == 400
    data = json.loads(await response.text())
    assert "error" in data
    assert await spa_env.linking.gym_by_invite_code(old_code) is not None

    # Missing confirm
    response = await spa_env.client.post(
        "/api/settings/regenerate-invite",
        json={},
        cookies=spa_env.cookie(),
    )
    assert response.status == 400
    assert await spa_env.linking.gym_by_invite_code(old_code) is not None


async def test_api_regenerate_invite_succeeds_with_confirm(spa_env):
    """With the correct confirm word, the invite code is regenerated."""
    old_code = spa_env.gym.invite_code

    response = await spa_env.client.post(
        "/api/settings/regenerate-invite",
        json={"confirm": REGENERATE_CONFIRM_ES},
        cookies=spa_env.cookie(),
    )

    assert response.status == 200
    data = json.loads(await response.text())
    assert data["invite_code"] != old_code
    assert data["invite_url"].startswith("https://t.me/")

    # Old code no longer resolves
    assert await spa_env.linking.gym_by_invite_code(old_code) is None
    # New code resolves
    assert await spa_env.linking.gym_by_invite_code(data["invite_code"]) is not None


async def test_api_regenerate_invite_uses_page_language(spa_env):
    """The confirm word follows the page language (EN cookie → EN word)."""
    response = await spa_env.client.post(
        "/api/settings/regenerate-invite",
        json={"confirm": REGENERATE_CONFIRM_EN},
        cookies={
            **spa_env.cookie(),
            "agentg_dashboard_lang": "en",
        },
    )
    assert response.status == 200

    # Spanish word fails when the page is in English
    response = await spa_env.client.post(
        "/api/settings/regenerate-invite",
        json={"confirm": REGENERATE_CONFIRM_ES},
        cookies={
            **spa_env.cookie(),
            "agentg_dashboard_lang": "en",
        },
    )
    assert response.status == 400


async def test_api_regenerate_invite_rejects_unauthenticated(spa_env):
    """POST without a cookie answers 401."""
    response = await spa_env.client.post(
        "/api/settings/regenerate-invite",
        json={"confirm": REGENERATE_CONFIRM_ES},
    )
    assert response.status == 401


# --- /api/settings/regenerate-coach POST ---


async def test_api_regenerate_coach_requires_confirm_word(spa_env):
    """Without the correct confirm word, the coach code is not regenerated."""
    old_code = spa_env.gym.coach_invite_code

    response = await spa_env.client.post(
        "/api/settings/regenerate-coach",
        json={"confirm": "no"},
        cookies=spa_env.cookie(),
    )
    assert response.status == 400
    assert await spa_env.linking.gym_by_coach_invite_code(old_code) is not None


async def test_api_regenerate_coach_succeeds_with_confirm(spa_env):
    """With the correct confirm word, the coach code is regenerated."""
    old_code = spa_env.gym.coach_invite_code

    response = await spa_env.client.post(
        "/api/settings/regenerate-coach",
        json={"confirm": REGENERATE_CONFIRM_ES},
        cookies=spa_env.cookie(),
    )

    assert response.status == 200
    data = json.loads(await response.text())
    assert data["coach_invite_code"] != old_code
    assert "coach-" in data["coach_invite_code"]
    assert data["coach_invite_url"].startswith("https://t.me/")

    # Old code no longer resolves
    assert await spa_env.linking.gym_by_coach_invite_code(old_code) is None
    # New code resolves
    assert await spa_env.linking.gym_by_coach_invite_code(data["coach_invite_code"]) is not None


async def test_api_regenerate_member_leaves_coach_alone(spa_env):
    """Regenerating the member invite does not touch the coach code."""
    old_coach = spa_env.gym.coach_invite_code
    await spa_env.client.post(
        "/api/settings/regenerate-invite",
        json={"confirm": REGENERATE_CONFIRM_ES},
        cookies=spa_env.cookie(),
    )
    assert await spa_env.linking.gym_by_coach_invite_code(old_coach) is not None


# --- /api/settings/gym-name POST ---


async def test_api_gym_name_update_succeeds(spa_env):
    """Renaming the gym updates the name and returns it."""
    response = await spa_env.client.post(
        "/api/settings/gym-name",
        json={"name": "Templo de Hierro"},
        cookies=spa_env.cookie(),
    )

    assert response.status == 200
    data = json.loads(await response.text())
    assert data["gym_name"] == "Templo de Hierro"

    # Verify via the read path
    gym = await spa_env.linking.gym_by_invite_code(spa_env.gym.invite_code)
    assert gym.name == "Templo de Hierro"


async def test_api_gym_name_trims_whitespace(spa_env):
    """Extra whitespace is collapsed."""
    response = await spa_env.client.post(
        "/api/settings/gym-name",
        json={"name": "  Templo  de  Hierro  "},
        cookies=spa_env.cookie(),
    )

    assert response.status == 200
    data = json.loads(await response.text())
    assert data["gym_name"] == "Templo de Hierro"


async def test_api_gym_name_rejects_empty(spa_env):
    """An empty or whitespace-only name is refused with 400."""
    for bad in ("", "   "):
        response = await spa_env.client.post(
            "/api/settings/gym-name",
            json={"name": bad},
            cookies=spa_env.cookie(),
        )
        assert response.status == 400
        data = json.loads(await response.text())
        assert "error" in data

    # Gym name unchanged
    gym = await spa_env.linking.gym_by_invite_code(spa_env.gym.invite_code)
    assert gym.name == "Iron Temple"


async def test_api_gym_name_rejects_unauthenticated(spa_env):
    """POST without a cookie answers 401."""
    response = await spa_env.client.post(
        "/api/settings/gym-name",
        json={"name": "Nope"},
    )
    assert response.status == 401


async def test_api_gym_name_caps_at_max_length(spa_env):
    """The name is capped at 200 chars (the column limit)."""
    long_name = "Gym " + "x" * 300
    response = await spa_env.client.post(
        "/api/settings/gym-name",
        json={"name": long_name},
        cookies=spa_env.cookie(),
    )
    assert response.status == 200
    data = json.loads(await response.text())
    assert len(data["gym_name"]) == 200


# --- Flag-off ---


async def test_api_settings_flag_off_404(tmp_path):
    """With spa_enabled=False, none of the /api/settings routes are reachable."""
    engine, clock, linking, store, gym, member = await _setup_stores(tmp_path)
    try:
        app = build_app(
            store,
            linking,
            session_secret=SECRET,
            bot_username=BOT_USERNAME,
            secure_cookies=False,
            clock=clock,
            spa_enabled=False,
        )
        async with TestClient(TestServer(app)) as client:
            cookie = _cookie(member.id, gym.id, clock)
            for method, path in [
                ("get", "/api/settings"),
                ("post", "/api/settings/regenerate-invite"),
                ("post", "/api/settings/regenerate-coach"),
                ("post", "/api/settings/gym-name"),
            ]:
                if method == "get":
                    resp = await client.get(path, cookies=cookie)
                else:
                    resp = await client.post(path, json={}, cookies=cookie)
                assert resp.status == 404, f"{method} {path} should be 404, got {resp.status}"
    finally:
        await engine.dispose()
