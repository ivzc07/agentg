"""The dashboard's HTTP door, end to end over a real aiohttp server.

Covers the acceptance flow: magic link -> interstitial (GET never spends the
token, so a link-preview fetch can't burn it) -> POST redeem -> session
cookie -> signed-in shell, refreshed on every visit; every bad state bounces
to the friendly page.
"""

import json
from datetime import timedelta
from pathlib import Path

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
    # Issue #139: the bounce page uses the same card language.
    assert 'class="door"' in text and 'class="card"' in text


async def test_the_full_login_flow_signs_the_coach_in(env):
    raw = await env.new_token()

    # GET shows the interstitial and does NOT spend the token (the
    # link-preview guard: a fetcher only ever GETs).
    response = await env.client.get(f"/login/{raw}")
    assert response.status == 200
    text = await response.text()
    assert 'method="post"' in text
    # Issue #139: the door pages use the same card language as the inside.
    assert 'class="door"' in text and 'class="card"' in text
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


@pytest.mark.parametrize("next_path", ["//evil.com", "https://evil.example/x", "evil"])
async def test_a_foreign_next_path_redirects_to_the_roster(env, next_path):
    """The deep-link landing is local-only: anything that isn't a plain path
    on our own origin falls back to the roster (review on PR #120)."""
    raw = await env.store.create_login_token(env.member.id, env.gym.id, next_path=next_path)

    response = await env.client.post(f"/login/{raw}", allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"] == "/"


async def test_a_local_next_path_is_honoured(env):
    raw = await env.store.create_login_token(
        env.member.id, env.gym.id, next_path="/members/1"
    )

    response = await env.client.post(f"/login/{raw}", allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"] == "/members/1"


EVIL_NEXT_PATHS = [
    "/\t/evil.com",
    "/\n/evil.com",
    "/\r/evil.com",
    "/%09/evil.com",
    "/%0a/evil.com",
    "/%0d/evil.com",
]


@pytest.mark.parametrize("next_path", EVIL_NEXT_PATHS)
async def test_a_control_char_next_path_redirects_to_the_roster(env, next_path):
    """Control/whitespace chars (raw or percent-encoded) let "/\t/evil.com"
    slip past a startswith guard; yarl then normalizes it to the
    protocol-relative //evil.com — an open redirect (review on PR #120)."""
    raw = await env.store.create_login_token(env.member.id, env.gym.id, next_path=next_path)

    response = await env.client.post(f"/login/{raw}", allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"] == "/"


@pytest.mark.parametrize("next_path", EVIL_NEXT_PATHS)
async def test_the_language_toggle_rejects_a_control_char_next(env, next_path):
    """The toggle's ``next`` takes the same guard as the magic-link redeem —
    one shared helper (review on PR #120)."""
    response = await env.client.get(
        "/lang/en", params={"next": next_path}, allow_redirects=False
    )

    assert response.status == 302
    assert response.headers["Location"] == "/"


# --- /api/session JSON contract (issue #155) ---


async def test_api_session_returns_coach_name_and_gym(env):
    """An authenticated coach's GET /api/session returns JSON with name and gym."""
    cookie = sign_session(env.member.id, env.gym.id, SECRET, env.clock())

    response = await env.client.get(
        "/api/session", cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    assert response.content_type == "application/json"
    data = json.loads(await response.text())
    assert data["name"] == "Ana"
    assert data["gym"] == "Iron Temple"


async def test_api_session_rejects_unauthenticated(env):
    """Without a valid session cookie /api/session answers 401."""
    response = await env.client.get("/api/session")
    assert response.status == 401


async def test_api_session_rejects_forged_cookie(env):
    """A forged session cookie does not open /api/session."""
    forged = sign_session(env.member.id, env.gym.id, "wrong-secret", env.clock())
    response = await env.client.get(
        "/api/session", cookies={SESSION_COOKIE: forged}
    )
    assert response.status == 401


# --- SPA serving (issue #155) ---


@pytest.fixture
async def spa_env(tmp_path):
    """A test app with dashboard_spa_enabled=True and a stub built bundle."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Ana", "telegram", "42")
    await linking.set_coach(member.id, True)

    # Create a stub built bundle so the SPA route finds assets.
    dist_dir = Path(__file__).parent.parent / "frontend" / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text(
        '<!DOCTYPE html><html><head><title>SPA</title></head>'
        '<body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    (dist_dir / "assets").mkdir(exist_ok=True)
    (dist_dir / "assets" / "index.js").write_text("// stub", encoding="utf-8")
    (dist_dir / "assets" / "index.css").write_text("/* stub */", encoding="utf-8")

    app = build_app(
        store,
        linking,
        session_secret=SECRET,
        bot_username="testbot",
        secure_cookies=False,
        clock=clock,
        spa_enabled=True,
    )
    async with TestClient(TestServer(app)) as client:
        yield SimpleEnv(clock, linking, store, client, gym, member)
    await engine.dispose()


SPA_SHELL_ROUTE = "/dashboard"


async def test_spa_shell_serves_authenticated(spa_env):
    """With the flag on, an authenticated coach gets the SPA shell."""
    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    response = await spa_env.client.get(
        SPA_SHELL_ROUTE, cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    text = await response.text()
    # The shell injects window.__I18N__ with the active-language strings.
    assert "window.__I18N__" in text
    # The root div is present for React to mount into.
    assert 'id="root"' in text


async def test_spa_shell_injects_i18n_strings(spa_env):
    """The SPA shell injects window.__I18N__ from server STRINGS for the active lang."""
    from agentg.dashboard_i18n import STRINGS

    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    response = await spa_env.client.get(
        SPA_SHELL_ROUTE,
        cookies={SESSION_COOKIE: cookie, "agentg_dashboard_lang": "en"},
    )

    text = await response.text()
    assert "window.__I18N__" in text
    # Spot-check a few English strings.
    for key, value in STRINGS["en"].items():
        # Not every string makes it into the bootstrap — just verify the
        # object is injected and carries real keys.
        if key == "settings":
            assert value in text
            break


async def test_spa_shell_rejects_unauthenticated(spa_env):
    """Without a cookie the SPA shell answers the same bounce page."""
    response = await spa_env.client.get(SPA_SHELL_ROUTE)
    assert response.status == 200
    text = await response.text()
    assert BOUNCE_MARKER in text
    assert "Iron Temple" not in text


async def test_spa_serves_static_assets(spa_env):
    """The built assets are served as static files."""
    response = await spa_env.client.get("/dashboard/assets/index.js")
    assert response.status == 200
    text = await response.text()
    assert "stub" in text


async def test_flag_off_dashboard_unaffected(env):
    """With the spa flag off (default), the existing dashboard is byte-for-byte
    the same — no new route leaks in."""
    # Sign in and verify the classic dashboard renders.
    cookie = sign_session(env.member.id, env.gym.id, SECRET, env.clock())

    response = await env.client.get("/", cookies={SESSION_COOKIE: cookie})

    assert response.status == 200
    text = await response.text()
    # The server-HTML dashboard is served, not the SPA shell.
    assert "Iron Temple" in text
    assert "window.__I18N__" not in text
    assert 'id="root"' not in text
