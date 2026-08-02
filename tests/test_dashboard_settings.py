"""The tenant Settings screen (spec-dashboard §Settings) since the React
cutover (#154): the screen itself is the SPA (SettingsPage RTL covers the
cards, the typed confirm, and the copy button); what lives here is the
web-layer contract it runs on — /api/settings and its writes — asserted
against the *linking read path* Members actually meet, plus the shared
shell gate and the QR memoization.

The JSON contract itself (shapes, confirm word, 401s, caps) is
tests/test_api_settings.py.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentg.dashboard_store import DashboardStore
from agentg.dashboard_web import REGENERATE_CONFIRM, _qr_svg, build_app
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from conftest import FakeClock

SECRET = "test-secret"
BOT_USERNAME = "testbot"
BOUNCE_MARKER = "/dashboard"  # the bounce page tells you to send /dashboard


@pytest.fixture
async def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()  # one shared clock, like test_dashboard_web.py
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Ana", "telegram", "42")
    await linking.set_coach(member.id, True)
    stub_dist = tmp_path / "dist"
    stub_dist.mkdir()
    (stub_dist / "index.html").write_text(
        '<!DOCTYPE html><html><head><title>SPA</title></head>'
        '<body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    (stub_dist / "assets").mkdir()
    monkeypatch.setattr("agentg.dashboard_web._FRONTEND_DIST", stub_dist)
    app = build_app(
        store,
        linking,
        session_secret=SECRET,
        bot_username=BOT_USERNAME,
        secure_cookies=False,
        clock=clock,
    )
    async with TestClient(TestServer(app)) as client:
        token = await store.create_login_token(member.id, gym.id)
        await client.post(f"/login/{token}")  # sign in (redirect followed)
        yield SimpleEnv(linking, client, gym, member)
    await engine.dispose()


class SimpleEnv:
    def __init__(self, linking, client, gym, member):
        self.linking = linking
        self.client = client
        self.gym = gym
        self.member = member

    async def settings(self) -> dict:
        response = await self.client.get("/api/settings")
        assert response.status == 200
        return await response.json()


async def test_a_coach_sees_both_links_and_exactly_one_qr(env):
    data = await env.settings()

    member_url = f"https://t.me/{BOT_USERNAME}?start={env.gym.invite_code}"
    coach_url = f"https://t.me/{BOT_USERNAME}?start={env.gym.coach_invite_code}"
    assert data["invite_url"] == member_url
    assert data["coach_invite_url"] == coach_url

    # The member link carries a QR (a printable poster); the coach link does
    # not — it is forwarded privately, never posted. The API mirrors that:
    # one qr_svg field, for the member invite only.
    assert "<svg" in data["qr_svg"]
    assert "coach_qr_svg" not in data
    assert env.gym.invite_code in data["qr_svg"] or "svg" in data["qr_svg"]


async def test_regenerating_the_member_link_kills_the_old_code_on_the_linking_path(env):
    old_code = env.gym.invite_code

    # A wrong confirm changes nothing — on the linking read path either.
    response = await env.client.post(
        "/api/settings/regenerate-invite", json={"confirm": "no"}
    )
    assert response.status == 400
    assert await env.linking.gym_by_invite_code(old_code) is not None

    response = await env.client.post(
        "/api/settings/regenerate-invite", json={"confirm": REGENERATE_CONFIRM}
    )
    assert response.status == 200
    body = await response.json()

    # The old code is dead — including linking flows it started — and the
    # new one resolves.
    assert await env.linking.gym_by_invite_code(old_code) is None
    assert body["invite_code"] != old_code
    assert await env.linking.gym_by_invite_code(body["invite_code"]) is not None
    # The coach link is untouched.
    assert await env.linking.gym_by_coach_invite_code(env.gym.coach_invite_code) is not None


async def test_regenerating_the_coach_link_kills_the_old_code_on_the_linking_path(env):
    old_code = env.gym.coach_invite_code

    response = await env.client.post(
        "/api/settings/regenerate-coach", json={"confirm": REGENERATE_CONFIRM}
    )
    assert response.status == 200
    body = await response.json()

    assert await env.linking.gym_by_coach_invite_code(old_code) is None
    assert body["coach_invite_code"].startswith("coach-")
    assert await env.linking.gym_by_coach_invite_code(body["coach_invite_code"]) is not None


async def test_renaming_the_gym_takes_effect_where_members_see_it(env):
    response = await env.client.post(
        "/api/settings/gym-name", json={"name": "  Templo de Hierro  "}
    )
    assert response.status == 200

    # Members meet the gym name on the linking read path — it resolves fresh.
    gym = await env.linking.gym_by_invite_code(env.gym.invite_code)
    assert gym.name == "Templo de Hierro"
    identity = await env.linking.identity_for("telegram", "42")
    assert identity.gym.name == "Templo de Hierro"

    # The React screens read it back off the API — renamed there too.
    data = await env.settings()
    assert data["gym_name"] == "Templo de Hierro"
    session = await (await env.client.get("/api/session")).json()
    assert session["gym"] == "Templo de Hierro"


async def test_the_settings_screen_is_coach_only(env):
    await env.linking.set_coach(env.member.id, False)  # demoted in chat

    # The shell URL bounces; the API answers 401 and writes nothing.
    text = await (await env.client.get("/settings")).text()
    assert BOUNCE_MARKER in text and 'id="root"' not in text
    for route, body in (
        ("/api/settings/regenerate-invite", {"confirm": REGENERATE_CONFIRM}),
        ("/api/settings/regenerate-coach", {"confirm": REGENERATE_CONFIRM}),
        ("/api/settings/gym-name", {"name": "Nope"}),
    ):
        response = await env.client.post(route, json=body)
        assert response.status == 401

    # Nothing changed behind the refused writes.
    assert await env.linking.gym_by_invite_code(env.gym.invite_code) is not None
    gym = await env.linking.gym_by_invite_code(env.gym.invite_code)
    assert gym.name == "Iron Temple"


async def test_anonymous_visits_to_settings_bounce(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    linking = LinkingStore(engine)
    store = DashboardStore(engine)
    await linking.ensure_schema()
    stub_dist = tmp_path / "dist"
    stub_dist.mkdir()
    (stub_dist / "index.html").write_text(
        '<html><body><div id="root"></div></body></html>', encoding="utf-8"
    )
    (stub_dist / "assets").mkdir()
    monkeypatch.setattr("agentg.dashboard_web._FRONTEND_DIST", stub_dist)
    app = build_app(
        store, linking, session_secret=SECRET, bot_username=BOT_USERNAME, secure_cookies=False
    )
    async with TestClient(TestServer(app)) as client:
        text = await (await client.get("/settings")).text()
        assert BOUNCE_MARKER in text and 'id="root"' not in text
        response = await client.post(
            "/api/settings/regenerate-invite", json={"confirm": REGENERATE_CONFIRM}
        )
        assert response.status == 401
    await engine.dispose()


def test_the_qr_svg_is_memoized_per_invite_url():
    """The encode runs on the bot's shared event loop, so a URL's SVG is
    computed once; regenerating the code changes the URL, which is a cache
    miss — the stale entry never serves again."""
    _qr_svg.cache_clear()
    url = f"https://t.me/{BOT_USERNAME}?start=cachetest"
    assert _qr_svg(url) == _qr_svg(url)
    assert _qr_svg.cache_info().hits == 1
    assert _qr_svg.cache_info().misses == 1
    _qr_svg(f"https://t.me/{BOT_USERNAME}?start=newcode99")  # a "regenerated" URL
    assert _qr_svg.cache_info().misses == 2
