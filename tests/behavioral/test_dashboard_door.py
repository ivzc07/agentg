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


def web_client(h: ConversationHarness, clock: FakeClock) -> TestClient:
    app = build_app(
        h.stores.dashboard, session_secret=SECRET, secure_cookies=False, clock=clock
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

        async with web_client(h, clock) as client:
            # The interstitial never spends the token: a link-preview fetch
            # (GET) can't burn the one-time link before the human taps it.
            page = await (await client.get(f"/login/{token}")).text()
            assert 'method="post"' in page
            assert await h.stores.dashboard.peek_login_token(token) is not None

            # The real redemption is the POST: redirect home, session cookie.
            response = await client.post(f"/login/{token}", allow_redirects=False)
            assert response.status == 302
            assert response.headers["Location"] == "/"
            assert SESSION_COOKIE in response.cookies

            # The cookie opens the gym's shell and survives revisits.
            for _ in range(2):
                text = await (await client.get("/")).text()
                assert "Iron Temple" in text

        # End-state: exactly one token row, spent, bound to this coach + gym.
        [row] = await h.login_tokens()
        assert row.member_id == h.member_id and row.gym_id == h.gym_id
        assert row.used_at is not None


async def test_the_magic_link_reply_suppresses_the_link_preview(tmp_path):
    async with ConversationHarness.create(tmp_path, dashboard_base_url=BASE_URL) as h:
        await h.linked_member(is_coach=True)
        reply = await h.runtime.handle_message(
            IncomingMessage(channel="telegram", channel_user_id="42", text="/dashboard")
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

        async with web_client(h, clock) as client:
            first = await client.post(f"/login/{token}", allow_redirects=False)
            assert first.status == 302

            # A second redemption — or its interstitial — bounces, and no
            # fresh session cookie is issued.
            second = await client.post(f"/login/{token}", allow_redirects=False)
            assert second.status == 200
            assert BOUNCE_MARKER in await second.text()
            assert SESSION_COOKIE not in second.cookies
            assert BOUNCE_MARKER in await (await client.get(f"/login/{token}")).text()


async def test_an_expired_bookmark_bounces_instead_of_erroring(tmp_path):
    clock = FakeClock()
    async with ConversationHarness.create(
        tmp_path, dashboard_base_url=BASE_URL, dashboard_clock=clock
    ) as h:
        await h.linked_member(is_coach=True)
        token = link_token(await h.say("/dashboard"))

        clock.advance(TOKEN_TTL + timedelta(seconds=1))  # link dies in chat

        async with web_client(h, clock) as client:
            assert BOUNCE_MARKER in await (await client.get(f"/login/{token}")).text()
            assert BOUNCE_MARKER in await (await client.post(f"/login/{token}")).text()
            # And no session ever materialized from it.
            assert "Iron Temple" not in await (await client.get("/")).text()

        [row] = await h.login_tokens()
        assert row.used_at is None  # expired, never spent


async def test_a_non_coach_is_refused_in_chat_and_no_link_is_issued(tmp_path):
    async with ConversationHarness.create(tmp_path, dashboard_base_url=BASE_URL) as h:
        await h.linked_member(is_coach=False)

        reply = await h.say("/dashboard")

        assert "/login/" not in reply
        assert "coach" in reply
        assert await h.login_tokens() == []


async def test_a_dashboard_command_in_a_group_never_yields_a_link(tmp_path):
    """The bearer URL must not land where anyone can tap it first: a group
    /dashboard gets a "come to DM" reply — even from a coach — and no token
    is minted."""
    async with ConversationHarness.create(tmp_path, dashboard_base_url=BASE_URL) as h:
        await h.linked_member(is_coach=True)

        reply = await h.say("/dashboard", is_group=True)

        assert "/login/" not in reply
        assert await h.login_tokens() == []

        # The same coach in the DM gets the real link immediately after.
        reply = await h.say("/dashboard")
        assert f"{BASE_URL}/login/" in reply


async def test_the_http_door_enforces_the_session(tmp_path):
    clock = FakeClock()
    async with ConversationHarness.create(
        tmp_path, dashboard_base_url=BASE_URL, dashboard_clock=clock
    ) as h:
        await h.linked_member(is_coach=True)
        async with web_client(h, clock) as client:
            # Anonymous and forged-cookie visits both bounce.
            assert BOUNCE_MARKER in await (await client.get("/")).text()
            forged = sign_session(h.member_id, h.gym_id, "wrong-secret", clock())
            text = await (await client.get("/", cookies={SESSION_COOKIE: forged})).text()
            assert BOUNCE_MARKER in text

            # A real sign-in opens the shell…
            token = link_token(await h.say("/dashboard"))
            await client.post(f"/login/{token}")
            assert "Iron Temple" in await (await client.get("/")).text()

            # …but demotion is checked per request, not per session: the
            # live cookie stops working on the very next click.
            await h.stores.linking.set_coach(h.member_id, False)
            text = await (await client.get("/")).text()
            assert BOUNCE_MARKER in text
            assert "Iron Temple" not in text
