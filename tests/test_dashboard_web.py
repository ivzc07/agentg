"""The dashboard's HTTP door, end to end over a real aiohttp server.

Covers the acceptance flow: magic link -> interstitial (GET never spends the
token, so a link-preview fetch can't burn it) -> POST redeem -> session
cookie -> signed-in shell, refreshed on every visit; every bad state bounces
to the friendly page.
"""

from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentg.dashboard_store import DashboardStore
from agentg.dashboard_web import SESSION_COOKIE, build_app, sign_session
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from conftest import FakeClock

SECRET = "test-secret"
BOUNCE_MARKER = "/dashboard"  # the bounce page tells you to send /dashboard


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Ana", "telegram", "42")
    await linking.set_coach(member.id, True)
    app = build_app(
        store,
        linking,
        session_secret=SECRET,
        bot_username="testbot",
        secure_cookies=False,
        clock=clock,
    )
    async with TestClient(TestServer(app)) as client:
        yield SimpleEnv(clock, linking, store, client, gym, member)
    await engine.dispose()


class SimpleEnv:
    def __init__(self, clock, linking, store, client, gym, member):
        self.clock = clock
        self.linking = linking
        self.store = store
        self.client = client
        self.gym = gym
        self.member = member

    async def new_token(self) -> str:
        return await self.store.create_login_token(self.member.id, self.gym.id)


async def test_anonymous_visit_bounces_to_the_friendly_page(env):
    response = await env.client.get("/")
    assert response.status == 200
    text = await response.text()
    assert BOUNCE_MARKER in text and "Iron Temple" not in text


async def test_the_full_login_flow_signs_the_coach_in(env):
    raw = await env.new_token()

    # GET shows the interstitial and does NOT spend the token (the
    # link-preview guard: a fetcher only ever GETs).
    response = await env.client.get(f"/login/{raw}")
    assert response.status == 200
    assert 'method="post"' in (await response.text())
    assert await env.store.peek_login_token(raw) is not None

    # POST redeems: one redirect home, one session cookie.
    response = await env.client.post(f"/login/{raw}", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/"
    assert SESSION_COOKIE in response.cookies

    # The cookie opens the shell, and every visit slides the 90-day window.
    response = await env.client.get("/")
    text = await response.text()
    assert response.status == 200
    assert "Iron Temple" in text
    assert SESSION_COOKIE in response.cookies  # refreshed on the visit


async def test_a_token_is_single_use(env):
    raw = await env.new_token()
    assert (await env.client.post(f"/login/{raw}", allow_redirects=False)).status == 302

    # Second redemption (or its interstitial) bounces.
    assert BOUNCE_MARKER in await (await env.client.post(f"/login/{raw}")).text()
    assert BOUNCE_MARKER in await (await env.client.get(f"/login/{raw}")).text()


async def test_unknown_and_expired_links_bounce(env):
    assert BOUNCE_MARKER in await (await env.client.get("/login/no-such-token")).text()
    assert BOUNCE_MARKER in await (await env.client.post("/login/no-such-token")).text()

    raw = await env.new_token()
    env.clock.advance(timedelta(days=1))  # well past the token TTL
    assert BOUNCE_MARKER in await (await env.client.get(f"/login/{raw}")).text()


async def test_a_demoted_coach_is_locked_out_with_a_live_cookie(env):
    raw = await env.new_token()
    await env.client.post(f"/login/{raw}")  # sign in (redirect followed)
    assert "Iron Temple" in await (await env.client.get("/")).text()

    await env.linking.set_coach(env.member.id, False)  # demoted in chat

    text = await (await env.client.get("/")).text()
    assert BOUNCE_MARKER in text
    assert "Iron Temple" not in text


async def test_a_forged_cookie_does_not_open_the_door(env):
    forged = sign_session(env.member.id, env.gym.id, "wrong-secret", env.clock())
    response = await env.client.get("/", cookies={SESSION_COOKIE: forged})
    assert BOUNCE_MARKER in await response.text()
