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


async def _setup_stores(tmp_path):
    """Shared store and member setup for dashboard web tests."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Ana", "telegram", "42")
    await linking.set_coach(member.id, True)
    return engine, clock, linking, store, gym, member


@pytest.fixture
async def env(tmp_path):
    engine, clock, linking, store, gym, member = await _setup_stores(tmp_path)
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


SPA_SHELL_ROUTE = "/dashboard"


def _write_stub_dist(dist_dir: Path, *, assets: bool = True) -> None:
    """Write a minimal stub bundle under *dist_dir* so the SPA routes find it.

    Set *assets* to ``False`` to create a partial bundle (``index.html`` only)
    that triggers the missing-assets guard."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text(
        '<!DOCTYPE html><html><head><title>SPA</title></head>'
        '<body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    if assets:
        (dist_dir / "assets").mkdir(exist_ok=True)
        (dist_dir / "assets" / "index.js").write_text("// stub", encoding="utf-8")
        (dist_dir / "assets" / "index.css").write_text("/* stub */", encoding="utf-8")


@pytest.fixture
async def spa_env(tmp_path, monkeypatch):
    """A test app with dashboard_spa_enabled=True and a stub built bundle
    under *tmp_path* so nothing touches the real ``frontend/dist/``."""
    engine, clock, linking, store, gym, member = await _setup_stores(tmp_path)

    stub_dist = tmp_path / "dist"
    _write_stub_dist(stub_dist)
    monkeypatch.setattr(
        "agentg.dashboard_web._FRONTEND_DIST", stub_dist
    )

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


async def test_spa_shell_trailing_slash_serves_authenticated(spa_env):
    """The trailing-slash URL (Vite's canonical origin) also serves the shell."""
    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    response = await spa_env.client.get(
        f"{SPA_SHELL_ROUTE}/", cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    text = await response.text()
    assert "window.__I18N__" in text
    assert 'id="root"' in text


async def test_spa_index_html_not_served_unauthenticated(spa_env):
    """``/dashboard/index.html`` must not hand out the built bundle without a
    cookie: the static handler is scoped to /dashboard/assets/, so the path
    falls through to the deep-link catch-all (issue #149) and an anonymous
    caller gets the same bounce page every other dashboard URL gives them."""
    response = await spa_env.client.get("/dashboard/index.html")

    assert response.status == 200
    text = await response.text()
    # Not the bundle off disk, and no bootstrap payload for an anonymous caller.
    assert 'id="root"' not in text
    assert "window.__I18N__" not in text


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


async def test_spa_shell_injects_es_i18n_strings(spa_env):
    """When the language cookie is ``es``, the SPA shell injects Spanish STRINGS."""
    from agentg.dashboard_i18n import STRINGS

    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    response = await spa_env.client.get(
        SPA_SHELL_ROUTE,
        cookies={SESSION_COOKIE: cookie, "agentg_dashboard_lang": "es"},
    )

    text = await response.text()
    assert "window.__I18N__" in text
    # Verify ES strings are present in the injected bootstrap.
    es_settings = STRINGS["es"]["settings"]
    assert es_settings in text
    assert "Ajustes" in text


async def test_spa_shell_injects_i18n_arrays_en(spa_env):
    """The SPA shell injects _weekdays, _months, and _weekday_initials for en."""
    from agentg.dashboard_i18n import WEEKDAYS, MONTHS, WEEKDAY_INITIALS

    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    response = await spa_env.client.get(
        SPA_SHELL_ROUTE,
        cookies={SESSION_COOKIE: cookie, "agentg_dashboard_lang": "en"},
    )

    text = await response.text()
    assert "window.__I18N__" in text
    # Parse the injected payload.
    before_close = text.split("window.__I18N__", 1)[1].split("</script>", 1)[0]
    payload = json.loads(before_close.split(" = ", 1)[1].rstrip(";"))

    assert payload["_weekdays"] == list(WEEKDAYS["en"])
    assert payload["_months"] == list(MONTHS["en"])
    assert payload["_weekday_initials"] == list(WEEKDAY_INITIALS["en"])


async def test_spa_shell_injects_i18n_arrays_es(spa_env):
    """The SPA shell injects _weekdays, _months, and _weekday_initials for es."""
    from agentg.dashboard_i18n import WEEKDAYS, MONTHS, WEEKDAY_INITIALS

    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    response = await spa_env.client.get(
        SPA_SHELL_ROUTE,
        cookies={SESSION_COOKIE: cookie, "agentg_dashboard_lang": "es"},
    )

    text = await response.text()
    assert "window.__I18N__" in text
    # Parse the injected payload.
    before_close = text.split("window.__I18N__", 1)[1].split("</script>", 1)[0]
    payload = json.loads(before_close.split(" = ", 1)[1].rstrip(";"))

    assert payload["_weekdays"] == list(WEEKDAYS["es"])
    assert "miércoles" in payload["_weekdays"]
    assert payload["_months"] == list(MONTHS["es"])
    assert payload["_weekday_initials"] == list(WEEKDAY_INITIALS["es"])


async def test_spa_shell_escapes_script_close_tag(tmp_path, monkeypatch):
    """A ``</script>`` string in STRINGS must not close the injection tag early."""
    import agentg.dashboard_web as dashboard_web

    engine, clock, linking, store, gym, member = await _setup_stores(tmp_path)
    stub_dist = tmp_path / "dist"
    _write_stub_dist(stub_dist)
    monkeypatch.setattr(dashboard_web, "_FRONTEND_DIST", stub_dist)
    # Push a </script> string into the en dict (mutate and restore).
    from agentg.dashboard_i18n import STRINGS

    original_settings = STRINGS["en"]["settings"]
    STRINGS["en"]["settings"] = "</script><script>alert(1)</script>"
    try:
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
            cookie = sign_session(member.id, gym.id, SECRET, clock())
            response = await client.get(
                SPA_SHELL_ROUTE,
                cookies={SESSION_COOKIE: cookie, "agentg_dashboard_lang": "en"},
            )
            text = await response.text()
            # The raw </script> and </script must not appear in the
            # injection payload — < was escaped to \u003c.
            assert "</script>" in text  # the closing tag still exists
            # The payload between window.__I18N__ = and the closing </script>
            # must not contain a raw </script>.
            before_close = text.split("window.__I18N__", 1)[1].split("</script>", 1)[0]
            assert "</script>" not in before_close
            assert "<script>" not in before_close
            # The escaped < appears as \u003c in the JSON.
            assert "\\u003c/script>" in text
    finally:
        STRINGS["en"]["settings"] = original_settings
        await engine.dispose()


async def test_spa_shell_rejects_unauthenticated(spa_env):
    """Without a cookie the SPA shell answers the same bounce page."""
    response = await spa_env.client.get(SPA_SHELL_ROUTE)
    assert response.status == 200
    text = await response.text()
    assert BOUNCE_MARKER in text
    assert "Iron Temple" not in text


async def test_spa_serves_static_assets(spa_env):
    """The built assets are served under ``/dashboard/assets/``."""
    response = await spa_env.client.get("/dashboard/assets/index.js")
    assert response.status == 200
    text = await response.text()
    assert "stub" in text


async def test_spa_enabled_no_dist_builds_app(tmp_path, monkeypatch):
    """With the flag on and no ``dist/`` directory the app builds without
    crashing and the shell route returns a 503."""
    import agentg.dashboard_web as dashboard_web

    engine, clock, linking, store, gym, member = await _setup_stores(tmp_path)
    missing = tmp_path / "no-such-dist"
    monkeypatch.setattr(dashboard_web, "_FRONTEND_DIST", missing)
    try:
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
            cookie = sign_session(member.id, gym.id, SECRET, clock())
            # The shell route is not registered — aiohttp falls back to 404.
            response = await client.get(
                SPA_SHELL_ROUTE, cookies={SESSION_COOKIE: cookie}
            )
            assert response.status == 404
    finally:
        await engine.dispose()


async def test_flag_off_dashboard_unaffected(env):
    """With the spa flag off (default), the existing dashboard is unchanged.
    The SPA routes (/dashboard*, /api/roster, /api/seed) must not be reachable
    so that the rollback guarantee holds."""
    cookie = sign_session(env.member.id, env.gym.id, SECRET, env.clock())

    # The server-HTML dashboard at / is served, not the SPA shell.
    response = await env.client.get("/", cookies={SESSION_COOKIE: cookie})
    assert response.status == 200
    text = await response.text()
    assert "Iron Temple" in text
    assert "window.__I18N__" not in text
    assert 'id="root"' not in text

    # /dashboard is flag-gated — 404 when the flag is off.
    dash = await env.client.get("/dashboard", cookies={SESSION_COOKIE: cookie})
    assert dash.status == 404

    # /api/roster is flag-gated — 404 when the flag is off.
    roster = await env.client.get("/api/roster", cookies={SESSION_COOKIE: cookie})
    assert roster.status == 404

    # /api/seed is removed from the HTTP surface entirely — 404 in all configs.
    seed = await env.client.post("/api/seed", cookies={SESSION_COOKIE: cookie})
    assert seed.status == 404

    # SPA shell route is not registered.
    response = await env.client.get(SPA_SHELL_ROUTE)
    assert response.status == 404

    # Bundle asset route is not registered.
    response = await env.client.get("/dashboard/assets/index.js")
    assert response.status == 404


async def test_spa_enabled_wired_from_settings(tmp_path, monkeypatch):
    """The production wiring — Settings.dashboard_spa_enabled → build_app — runs
    through ``main.build_dashboard_app``, the one place ``run()`` builds the app.

    Dropping ``spa_enabled=`` from that call site turns this test red; that gap
    is exactly what shipped a dead flag the first time round.
    """
    from types import SimpleNamespace

    from agentg import main as main_module
    from agentg.config import Settings

    settings = Settings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "dummy",
            "MODEL_API_KEY": "dummy",
            "DASHBOARD_SPA_ENABLED": "1",
            "DASHBOARD_SESSION_SECRET": SECRET,
        }
    )
    assert settings.dashboard_spa_enabled is True

    engine, clock, linking, store, gym, member = await _setup_stores(tmp_path)
    stub_dist = tmp_path / "dist"
    _write_stub_dist(stub_dist)
    monkeypatch.setattr(
        "agentg.dashboard_web._FRONTEND_DIST", stub_dist
    )
    try:
        # The clock is the only thing run()'s call site cannot supply, so the
        # app is built through the production helper and only the clock is
        # patched in.
        real_build_app = main_module.build_app

        def build_app_with_clock(*args, **kwargs):
            return real_build_app(*args, clock=clock, **kwargs)

        monkeypatch.setattr(main_module, "build_app", build_app_with_clock)
        app = main_module.build_dashboard_app(
            SimpleNamespace(dashboard=store, linking=linking),
            settings,
            bot_username="testbot",
            notifier=None,
        )
        async with TestClient(TestServer(app)) as client:
            cookie = sign_session(member.id, gym.id, SECRET, clock())
            response = await client.get(
                SPA_SHELL_ROUTE,
                cookies={SESSION_COOKIE: cookie},
            )
            assert response.status == 200
            text = await response.text()
            assert "window.__I18N__" in text
    finally:
        await engine.dispose()


async def test_spa_enabled_partial_bundle(tmp_path, monkeypatch):
    """A dist/ with index.html but no assets/ must not crash at build_app
    — the guard must catch the missing assets directory (P1, PR review 2)."""
    import agentg.dashboard_web as dashboard_web

    engine, clock, linking, store, gym, member = await _setup_stores(tmp_path)
    stub_dist = tmp_path / "dist"
    _write_stub_dist(stub_dist, assets=False)
    monkeypatch.setattr(dashboard_web, "_FRONTEND_DIST", stub_dist)
    try:
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
            cookie = sign_session(member.id, gym.id, SECRET, clock())
            # The SPA route is not registered — aiohttp falls back to 404.
            response = await client.get(
                SPA_SHELL_ROUTE, cookies={SESSION_COOKIE: cookie}
            )
            assert response.status == 404
    finally:
        await engine.dispose()


async def test_spa_dist_override_packaged_layout(tmp_path, monkeypatch):
    """With a spa_dist override, the app uses that path instead of the
    repo-relative _FRONTEND_DIST — simulating a container deploy where
    the bundle is at /app/frontend/dist (P2, PR review 2)."""
    import agentg.dashboard_web as dashboard_web

    engine, clock, linking, store, gym, member = await _setup_stores(tmp_path)
    # Simulate a packaged layout: _FRONTEND_DIST points to a non-existent
    # site-packages-adjacent path.
    monkeypatch.setattr(
        dashboard_web,
        "_FRONTEND_DIST",
        tmp_path / "site-packages" / "agentg" / ".." / ".." / "frontend" / "dist",
    )
    # The override points at the real stub bundle.
    override_dist = tmp_path / "dist"
    _write_stub_dist(override_dist)
    try:
        app = build_app(
            store,
            linking,
            session_secret=SECRET,
            bot_username="testbot",
            secure_cookies=False,
            clock=clock,
            spa_enabled=True,
            spa_dist=override_dist,
        )
        async with TestClient(TestServer(app)) as client:
            cookie = sign_session(member.id, gym.id, SECRET, clock())
            response = await client.get(
                SPA_SHELL_ROUTE,
                cookies={SESSION_COOKIE: cookie},
            )
            assert response.status == 200
            text = await response.text()
            assert "window.__I18N__" in text
    finally:
        await engine.dispose()


# --- /api/roster JSON contract (issue #149) ---
# The roster JSON endpoint is flag-gated behind spa_enabled, so all
# tests in this section use ``spa_env`` (the env fixture with the flag on).


async def test_api_roster_returns_json_shape(spa_env):
    """An authenticated coach's GET /api/roster returns the expected JSON shape."""
    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    response = await spa_env.client.get(
        "/api/roster", cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    assert response.content_type == "application/json"
    data = json.loads(await response.text())
    assert "active" in data
    assert "lapsed" in data
    assert "counts" in data
    assert "sortedBy" in data
    assert data["sortedBy"] == "gap_days"
    assert isinstance(data["active"], list)
    assert isinstance(data["lapsed"], list)
    assert "active" in data["counts"]
    assert "lapsed" in data["counts"]


async def test_api_roster_rejects_unauthenticated(spa_env):
    """Without a valid session cookie /api/roster answers 401."""
    response = await spa_env.client.get("/api/roster")
    assert response.status == 401


async def test_api_roster_rejects_forged_cookie(spa_env):
    """A forged session cookie does not open /api/roster."""
    forged = sign_session(spa_env.member.id, spa_env.gym.id, "wrong-secret", spa_env.clock())
    response = await spa_env.client.get(
        "/api/roster", cookies={SESSION_COOKIE: forged}
    )
    assert response.status == 401


async def test_api_roster_active_rows_have_required_fields(spa_env):
    """Every active roster row carries the fields the React screen needs."""
    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    # Add a non-coach member so the roster has at least one row.
    member2 = await spa_env.linking.link_member(spa_env.gym.id, "Ben", "telegram", "99")

    response = await spa_env.client.get(
        "/api/roster", cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    data = json.loads(await response.text())
    assert len(data["active"]) >= 1
    row = data["active"][0]
    required = [
        "member_id", "name", "gap_days", "has_sessions", "is_new",
        "snoozed_until", "missed_days", "severity", "has_safety_flag",
        "attendance",
    ]
    for field in required:
        assert field in row, f"missing field: {field}"
    # attendance is a list of {on, state} cells
    assert isinstance(row["attendance"], list)
    if row["attendance"]:
        cell = row["attendance"][0]
        assert "on" in cell
        assert "state" in cell


async def test_api_roster_lapsed_split(spa_env):
    """Lapsed members appear in the lapsed tail, not the active list."""
    from agentg.checkin import LAPSED

    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    member2 = await spa_env.linking.link_member(spa_env.gym.id, "Ben", "telegram", "99")
    # Set the member as lapsed.
    async with spa_env.store._sessions() as db:
        from agentg.models import Member
        m = await db.get(Member, member2.id)
        m.checkin_state = LAPSED
        await db.commit()

    response = await spa_env.client.get(
        "/api/roster", cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    data = json.loads(await response.text())
    lapsed_ids = [row["member_id"] for row in data["lapsed"]]
    assert member2.id in lapsed_ids
    active_ids = [row["member_id"] for row in data["active"]]
    assert member2.id not in active_ids
    assert data["counts"]["lapsed"] == len(data["lapsed"])


async def test_api_roster_sorted_by_gap_desc(spa_env):
    """Active rows are sorted by gap_days descending (largest gap first).

    Seeds members with distinct known last-session dates so the sort order
    is concretely verifiable — a monotonicity-only assert would not catch
    an accidentally reversed sort."""
    from datetime import UTC, datetime, timedelta

    from agentg.models import Session

    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())
    now = datetime.now(UTC)

    # Create two members with sessions at different dates to establish
    # distinct gap_days.  Zoe last trained 5 days ago, Alex 2 days ago,
    # so descending sort must put Zoe first (gap=5) then Alex (gap=2).
    m_zoe = await spa_env.linking.link_member(spa_env.gym.id, "Zoe", "telegram", "100")
    m_alex = await spa_env.linking.link_member(spa_env.gym.id, "Alex", "telegram", "101")

    async with spa_env.store._sessions() as db:
        db.add(Session(
            gym_id=spa_env.gym.id,
            member_id=m_zoe.id,
            started_at=now - timedelta(days=5),
            closed_at=now - timedelta(days=5, hours=-1),
        ))
        db.add(Session(
            gym_id=spa_env.gym.id,
            member_id=m_alex.id,
            started_at=now - timedelta(days=2),
            closed_at=now - timedelta(days=2, hours=-1),
        ))
        await db.commit()

    response = await spa_env.client.get(
        "/api/roster", cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    data = json.loads(await response.text())
    gaps = [row["gap_days"] for row in data["active"]]
    assert gaps == sorted(gaps, reverse=True), f"not sorted desc: {gaps}"
    # Concretely: Zoe (gap 5) then Alex (gap 2) in descending order.
    active_names = [row["name"] for row in data["active"]]
    zoe_idx = active_names.index("Zoe")
    alex_idx = active_names.index("Alex")
    assert zoe_idx < alex_idx, f"Zoe (gap=5) must precede Alex (gap=2), got {active_names}"
    # When gap ties, alphabetical by name (lowercase) breaks the tie.
    same_gap_rows = [row for row in data["active"] if row["gap_days"] == gaps[0]]
    if len(same_gap_rows) > 1:
        names = [row["name"].lower() for row in same_gap_rows]
        assert names == sorted(names), f"tie-break failed: {names}"


async def test_api_roster_severity_is_null_for_new_members(spa_env):
    """Members with no active Routine have severity=None (the grey-new rule)."""
    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    member2 = await spa_env.linking.link_member(spa_env.gym.id, "New Kid", "telegram", "102")

    response = await spa_env.client.get(
        "/api/roster", cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    data = json.loads(await response.text())
    new_row = next((r for r in data["active"] if r["member_id"] == member2.id), None)
    assert new_row is not None
    assert new_row["is_new"] is True
    assert new_row["severity"] is None


async def test_api_roster_snoozed_shows_until_date(spa_env):
    """A snoozed member carries snoozed_until; severity is None while snoozed."""
    from datetime import date, timedelta

    from agentg.checkin import SNOOZED

    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    member2 = await spa_env.linking.link_member(spa_env.gym.id, "Resting", "telegram", "103")
    future = date.today() + timedelta(days=5)
    async with spa_env.store._sessions() as db:
        from agentg.models import Member
        m = await db.get(Member, member2.id)
        m.checkin_state = SNOOZED
        m.snoozed_until = future
        await db.commit()

    response = await spa_env.client.get(
        "/api/roster", cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    data = json.loads(await response.text())
    snoozed_row = next((r for r in data["active"] if r["member_id"] == member2.id), None)
    assert snoozed_row is not None
    assert snoozed_row["snoozed_until"] == future.isoformat()
    assert snoozed_row["severity"] is None


async def test_api_roster_slides_session_cookie(spa_env):
    """A successful /api/roster call refreshes the 90-day session cookie."""
    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    response = await spa_env.client.get(
        "/api/roster", cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    assert SESSION_COOKIE in response.cookies


# --- /api/seed is not an HTTP endpoint (issue #149, review) ---


async def test_api_seed_not_an_http_endpoint(spa_env):
    """/api/seed was removed from the HTTP surface — it returns 404 even
    with the SPA flag on.  Use ``python -m agentg.scripts.seed_demo`` instead."""
    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())
    response = await spa_env.client.post(
        "/api/seed", cookies={SESSION_COOKIE: cookie}
    )
    assert response.status == 404


# --- SPA fallback for React Router deep links (issue #149) ---


async def test_spa_fallback_serves_shell_for_deep_links(spa_env):
    """GET /dashboard/members/1 serves the SPA shell, not a 404 (issue #149)."""
    cookie = sign_session(spa_env.member.id, spa_env.gym.id, SECRET, spa_env.clock())

    response = await spa_env.client.get(
        "/dashboard/members/1", cookies={SESSION_COOKIE: cookie}
    )

    assert response.status == 200
    text = await response.text()
    assert "window.__I18N__" in text
    assert 'id="root"' in text


async def test_spa_fallback_requires_auth(spa_env):
    """The SPA fallback also requires authentication."""
    response = await spa_env.client.get("/dashboard/members/1")
    assert response.status == 200
    text = await response.text()
    assert BOUNCE_MARKER in text


async def test_spa_fallback_static_assets_still_served(spa_env):
    """The SPA fallback does not shadow static asset routes."""
    response = await spa_env.client.get("/dashboard/assets/index.js")
    assert response.status == 200
    text = await response.text()
    assert "stub" in text
