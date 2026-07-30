"""The tenant Settings screen (spec-dashboard §Settings), end to end over a
real aiohttp server.

Covers the acceptance flow: any Coach of the gym opens Settings and copies
both links (member link with a QR, coach link without); regenerating either
code requires the typed confirm and invalidates the old code; renaming the
gym takes effect where Members see it (the Linking read path).
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
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()  # one shared clock, like test_dashboard_web.py
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

    async def settings_page(self) -> str:
        response = await self.client.get("/settings")
        assert response.status == 200
        return await response.text()


async def test_a_coach_sees_both_links_and_exactly_one_qr(env):
    page = await env.settings_page()

    member_url = f"https://t.me/{BOT_USERNAME}?start={env.gym.invite_code}"
    coach_url = f"https://t.me/{BOT_USERNAME}?start={env.gym.coach_invite_code}"
    assert member_url in page
    assert coach_url in page

    # The member link carries a QR (a printable poster); the coach link does
    # not — it is forwarded privately, never posted.
    invite_section = page.split('id="invite"', 1)[1].split('id="coach-link"', 1)[0]
    coach_section = page.split('id="coach-link"', 1)[1].split('id="gym-name"', 1)[0]
    assert "<svg" in invite_section
    assert "<svg" not in coach_section

    # Both regenerates sit behind the typed confirm, and the gym name is
    # editable — nothing else is on the screen: exactly three forms, whose
    # only inputs are the two confirms and the name. (A raw substring check
    # for "UTC"/"kg" would be flaky — random invite codes can contain them.)
    assert page.count(f'data-confirm="{REGENERATE_CONFIRM}"') == 2
    assert 'value="Iron Temple"' in page
    assert page.count("<form") == 3
    assert page.count('name="confirm"') == 2
    assert page.count('name="name"') == 1
    assert 'name="timezone"' not in page
    assert 'name="weight_unit"' not in page


async def test_the_shell_links_to_settings(env):
    page = await (await env.client.get("/")).text()
    assert 'href="/settings"' in page


async def test_regenerating_the_member_link_requires_the_typed_confirm(env):
    old_code = env.gym.invite_code

    # No confirm at all, and a wrong confirm: nothing changes.
    for data in ({}, {"confirm": "no"}):
        response = await env.client.post("/settings/regenerate-invite", data=data)
        assert REGENERATE_CONFIRM in await response.text()
        assert await env.linking.gym_by_invite_code(old_code) is not None

    response = await env.client.post(
        "/settings/regenerate-invite", data={"confirm": REGENERATE_CONFIRM}
    )
    assert response.status == 200  # redirected back to /settings

    # The old code is dead — including linking flows it started — and the
    # new one resolves and shows on the screen.
    assert await env.linking.gym_by_invite_code(old_code) is None
    page = await env.settings_page()
    assert old_code not in page
    new_code = page.split(f"<code>https://t.me/{BOT_USERNAME}?start=", 1)[1].split("<", 1)[0]
    assert new_code != old_code
    assert await env.linking.gym_by_invite_code(new_code) is not None


async def test_regenerating_the_coach_link_requires_the_typed_confirm(env):
    old_code = env.gym.coach_invite_code

    response = await env.client.post("/settings/regenerate-coach", data={})
    assert REGENERATE_CONFIRM in await response.text()
    assert await env.linking.gym_by_coach_invite_code(old_code) is not None

    await env.client.post(
        "/settings/regenerate-coach", data={"confirm": REGENERATE_CONFIRM}
    )

    assert await env.linking.gym_by_coach_invite_code(old_code) is None
    page = await env.settings_page()
    assert old_code not in page
    assert "coach-" in page


async def test_regenerating_the_member_link_leaves_the_coach_link_alone(env):
    old_coach_code = env.gym.coach_invite_code
    await env.client.post(
        "/settings/regenerate-invite", data={"confirm": REGENERATE_CONFIRM}
    )
    assert await env.linking.gym_by_coach_invite_code(old_coach_code) is not None


async def test_renaming_the_gym_takes_effect_where_members_see_it(env):
    response = await env.client.post(
        "/settings/gym-name", data={"name": "  Templo de Hierro  "}
    )
    assert response.status == 200  # redirected back to /settings

    # Members meet the gym name on the linking read path — it resolves fresh.
    gym = await env.linking.gym_by_invite_code(env.gym.invite_code)
    assert gym.name == "Templo de Hierro"
    identity = await env.linking.identity_for("telegram", "42")
    assert identity.gym.name == "Templo de Hierro"

    page = await env.settings_page()
    assert 'value="Templo de Hierro"' in page
    assert "Iron Temple" not in page

    shell = await (await env.client.get("/")).text()
    assert "Templo de Hierro" in shell


async def test_an_empty_gym_name_is_refused(env):
    response = await env.client.post("/settings/gym-name", data={"name": "   "})
    assert "vacío" in await response.text()
    gym = await env.linking.gym_by_invite_code(env.gym.invite_code)
    assert gym.name == "Iron Temple"


async def test_an_overlong_gym_name_is_capped_at_the_column_width(env):
    """The form's maxlength is client-side only; the store caps at 200."""
    await env.client.post("/settings/gym-name", data={"name": "Gimnasio " + "x" * 300})
    gym = await env.linking.gym_by_invite_code(env.gym.invite_code)
    assert gym.name == ("Gimnasio " + "x" * 300)[:200]
    assert len(gym.name) == 200


async def test_the_copy_button_handles_clipboard_failures(env):
    """navigator.clipboard is undefined on plain-HTTP origins and writeText
    can reject; the success label must only appear after the promise
    resolves, with a visible failure state otherwise."""
    page = await env.settings_page()
    script = page.split("<script>", 1)[1]

    assert "if (!navigator.clipboard)" in script  # plain-HTTP guard
    # The labels ride on the button as data attributes (the page language
    # decides them); the success label is assigned only inside the
    # writeText().then() success path.
    assert 'data-failed="No se pudo copiar"' in page
    assert 'data-done="Copiado"' in page
    then_at = script.index("writeText(button.dataset.copy).then(")
    assert script.index("button.dataset.done") > then_at


async def test_the_settings_screen_is_coach_only(env):
    await env.linking.set_coach(env.member.id, False)  # demoted in chat

    assert BOUNCE_MARKER in await (await env.client.get("/settings")).text()
    for route, data in (
        ("/settings/regenerate-invite", {"confirm": REGENERATE_CONFIRM}),
        ("/settings/regenerate-coach", {"confirm": REGENERATE_CONFIRM}),
        ("/settings/gym-name", {"name": "Nope"}),
    ):
        page = await (await env.client.post(route, data=data)).text()
        assert BOUNCE_MARKER in page

    # Nothing changed behind the bounced POSTs.
    assert await env.linking.gym_by_invite_code(env.gym.invite_code) is not None
    gym = await env.linking.gym_by_invite_code(env.gym.invite_code)
    assert gym.name == "Iron Temple"


async def test_anonymous_visits_to_settings_bounce(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    linking = LinkingStore(engine)
    store = DashboardStore(engine)
    await linking.ensure_schema()
    app = build_app(
        store, linking, session_secret=SECRET, bot_username=BOT_USERNAME, secure_cookies=False
    )
    async with TestClient(TestServer(app)) as client:
        assert BOUNCE_MARKER in await (await client.get("/settings")).text()
        page = await (
            await client.post(
                "/settings/regenerate-invite", data={"confirm": REGENERATE_CONFIRM}
            )
        ).text()
        assert BOUNCE_MARKER in page
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
