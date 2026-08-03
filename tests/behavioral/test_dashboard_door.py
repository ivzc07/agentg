"""Dashboard door evals (#96): the /dashboard magic-link flow, end to end.

The door is fully deterministic — no model steps are ever enqueued, so the
harness's leftover-step check also proves the Agent never sees the command.
Chat goes through ``AgentRuntime.handle_message``; HTTP goes through the
real aiohttp app in-process (``build_app`` + TestClient); assertions land
on Stores end-state, same as the rest of the suite.
"""

from __future__ import annotations

from datetime import timedelta

from aiohttp.test_utils import TestClient, TestServer

from agentg.dashboard_store import TOKEN_TTL
from agentg.dashboard_web import SESSION_COOKIE, build_app, sign_session
from agentg.messages import IncomingMessage
from behavioral.harness import ConversationHarness
from conftest import FakeClock

BASE_URL = "https://dash.test"
SECRET = "behavioral-secret"
BOUNCE_MARKER = "/dashboard"  # the bounce page tells you to send /dashboard


def link_token(reply: str) -> str:
    """The one-time token out of a magic-link reply's URL."""
    prefix = f"{BASE_URL}/login/"
    assert prefix in reply, f"no magic link in reply: {reply!r}"
    return reply.split(prefix, 1)[1].split()[0]


def web_client(h: ConversationHarness, clock: FakeClock, tmp_path) -> TestClient:
    # A stub bundle so the SPA shell serves deterministically, without
    # depending on the real frontend/dist build state.
    stub_dist = tmp_path / "stub-dist"
    if not stub_dist.exists():
        stub_dist.mkdir()
        (stub_dist / "index.html").write_text(
            '<!DOCTYPE html><html><head><title>SPA</title></head>'
            '<body><div id="root"></div></body></html>',
            encoding="utf-8",
        )
        (stub_dist / "assets").mkdir()
    app = build_app(
        h.stores.dashboard,
        h.stores.linking,
        session_secret=SECRET,
        bot_username="testbot",
        secure_cookies=False,
        clock=clock,
        spa_dist=stub_dist,
    )
    return TestClient(TestServer(app))


async def test_coachs_dashboard_link_signs_them_in_for_revisits(tmp_path):
    clock = FakeClock()
    async with ConversationHarness.create(
        tmp_path, dashboard_base_url=BASE_URL, dashboard_clock=clock
    ) as h:
        await h.linked_member(is_coach=True)

        reply = await h.say("/dashboard")
        token = link_token(reply)

        async with web_client(h, clock, tmp_path) as client:
            # The SPA login shell never spends the token: a link-preview
            # fetch (GET) can't burn the one-time link before the human
            # taps it — the React interstitial validates via the peek API.
            page = await (await client.get(f"/login/{token}")).text()
            assert 'id="root"' in page
            assert await h.stores.dashboard.peek_login_token(token) is not None

            # The real redemption is the POST: redirect home, session cookie.
            response = await client.post(f"/login/{token}", allow_redirects=False)
            assert response.status == 302
            assert response.headers["Location"] == "/"
            assert SESSION_COOKIE in response.cookies

            # The cookie opens the gym's shell and survives revisits — the
            # gym itself comes from the JSON API the shell renders from.
            for _ in range(2):
                text = await (await client.get("/")).text()
                assert 'id="root"' in text and "window.__I18N__" in text
                session = await (await client.get("/api/session")).json()
                assert session["gym"] == "Iron Temple"

        # End-state: exactly one token row, spent, bound to this coach + gym.
        [row] = await h.login_tokens()
        assert row.member_id == h.member_id and row.gym_id == h.gym_id
        assert row.used_at is not None


async def test_the_magic_link_reply_suppresses_the_link_preview(tmp_path):
    async with ConversationHarness.create(tmp_path, dashboard_base_url=BASE_URL) as h:
        await h.linked_member(is_coach=True)
        reply = await h.runtime.handle_message(
            IncomingMessage(channel="telegram", channel_user_id="42", text="/dashboard", is_private=True)
        )
        assert f"{BASE_URL}/login/" in reply
        assert reply.disable_preview is True  # Telegram must not pre-fetch it


async def test_a_magic_link_redeems_exactly_once(tmp_path):
    clock = FakeClock()
    async with ConversationHarness.create(
        tmp_path, dashboard_base_url=BASE_URL, dashboard_clock=clock
    ) as h:
        await h.linked_member(is_coach=True)
        token = link_token(await h.say("/dashboard"))

        async with web_client(h, clock, tmp_path) as client:
            first = await client.post(f"/login/{token}", allow_redirects=False)
            assert first.status == 302

            # A second redemption bounces, and no fresh session cookie is
            # issued; the peek API reports the token dead so the React
            # interstitial shows the bounce too.
            second = await client.post(f"/login/{token}", allow_redirects=False)
            assert second.status == 200
            assert BOUNCE_MARKER in await second.text()
            assert SESSION_COOKIE not in second.cookies
            peek = await (await client.get(f"/api/login/{token}")).json()
            assert peek["valid"] is False


async def test_an_expired_bookmark_bounces_instead_of_erroring(tmp_path):
    clock = FakeClock()
    async with ConversationHarness.create(
        tmp_path, dashboard_base_url=BASE_URL, dashboard_clock=clock
    ) as h:
        await h.linked_member(is_coach=True)
        token = link_token(await h.say("/dashboard"))

        clock.advance(TOKEN_TTL + timedelta(seconds=1))  # link dies in chat

        async with web_client(h, clock, tmp_path) as client:
            peek = await (await client.get(f"/api/login/{token}")).json()
            assert peek["valid"] is False
            assert BOUNCE_MARKER in await (await client.post(f"/login/{token}")).text()
            # And no session ever materialized from it: the anonymous visit
            # still bounces instead of opening the shell.
            home = await (await client.get("/")).text()
            assert BOUNCE_MARKER in home and 'id="root"' not in home

        [row] = await h.login_tokens()
        assert row.used_at is None  # expired, never spent


async def test_a_non_coach_is_refused_in_chat_and_no_link_is_issued(tmp_path):
    async with ConversationHarness.create(tmp_path, dashboard_base_url=BASE_URL) as h:
        await h.linked_member(is_coach=False)

        reply = await h.say("/dashboard")

        assert "/login/" not in reply
        assert "coach" in reply
        assert await h.login_tokens() == []


async def test_the_http_door_enforces_the_session(tmp_path):
    clock = FakeClock()
    async with ConversationHarness.create(
        tmp_path, dashboard_base_url=BASE_URL, dashboard_clock=clock
    ) as h:
        await h.linked_member(is_coach=True)
        async with web_client(h, clock, tmp_path) as client:
            # Anonymous and forged-cookie visits both bounce.
            assert BOUNCE_MARKER in await (await client.get("/")).text()
            forged = sign_session(h.member_id, h.gym_id, "wrong-secret", clock())
            text = await (await client.get("/", cookies={SESSION_COOKIE: forged})).text()
            assert BOUNCE_MARKER in text

            # A real sign-in opens the shell…
            token = link_token(await h.say("/dashboard"))
            await client.post(f"/login/{token}")
            assert 'id="root"' in await (await client.get("/")).text()
            session = await (await client.get("/api/session")).json()
            assert session["gym"] == "Iron Temple"

            # …but demotion is checked per request, not per session: the
            # live cookie stops working on the very next click.
            await h.stores.linking.set_coach(h.member_id, False)
            text = await (await client.get("/")).text()
            assert BOUNCE_MARKER in text
            assert 'id="root"' not in text
