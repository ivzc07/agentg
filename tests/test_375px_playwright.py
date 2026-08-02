"""Real-browser 375px rendering checks for every dashboard screen (issue #157).

`test_redesign_acceptance_sweep.py::Test375pxBar` asserts the *stylesheet* carries
the right responsive rules. That is a structural proxy: it cannot see a screen
that still overflows because some element outside those rules is too wide, and it
cannot see a primary action pushed off-canvas. This module closes that gap by
rendering each screen in a real browser at 375x812 (iPhone SE) and asserting on
the layout the user actually gets.

## Why this skips rather than fails when Playwright is absent

Playwright and its browser binaries are a heavy, optional dev dependency the repo
does not install today, so by default every test here skips - the issue asked for
exactly that.

It is deliberately an *importorskip* rather than the `@pytest.mark.skip` the issue
suggested. An unconditional skip can never fail, so it would rot silently: the
selectors below would drift out of sync with the markup and nobody would learn
until someone finally installed Playwright and found the module broken. With
importorskip the moment the dependency exists - locally or in a future CI job -
these tests run and bite.

To run them:

    uv pip install ".[browser]" && uv run playwright install chromium
    uv run pytest tests/test_375px_playwright.py

Install through the `browser` extra, not a bare `pip install playwright`: the
extra carries the `>=1.40` pin, and bypassing it is how the two drift apart.

## Two deviations from the issue text, recorded rather than glossed

Step 2 says the test "signs in". It injects a pre-signed session cookie instead
of redeeming a login token. Login tokens are single-use, and the interstitial
submits itself, so redeeming one to reach the roster would both consume the
token the login screen needs and make every other screen depend on that
redirect landing. The cookie is the same credential the redemption would
issue, so the screens under test are identical - only the route in differs.

Step 3 says primary actions must be "within the viewport bounds". The check is
horizontal-only. A strict vertical check would fail every page taller than
812px, which is most of them, and scrolling down is normal on a phone while
scrolling sideways is the defect this module exists to catch.

Screenshots land in `docs/design/375px/` next to the sweep doc, per the issue.
They are regenerated on every run and are git-ignored: committing them would put
~320 KB of churning binaries in every diff for no review value.
"""

from __future__ import annotations

import pytest

# Skips the whole module when the optional dependency is missing. Must precede
# any playwright import.
pytest.importorskip(
    "playwright",
    reason="needs playwright + browser binaries: "
    'uv pip install ".[browser]" && uv run playwright install chromium',
)

from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest_asyncio  # noqa: E402

from aiohttp.test_utils import TestServer  # noqa: E402
from playwright.async_api import Error as PlaywrightError  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from agentg.dashboard_store import DashboardStore  # noqa: E402
from agentg.dashboard_web import SESSION_COOKIE, build_app, sign_session  # noqa: E402
from agentg.db import create_engine  # noqa: E402
from agentg.linking_store import LinkingStore  # noqa: E402
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec  # noqa: E402
from agentg.training import TrainingStore  # noqa: E402
from conftest import FakeClock  # noqa: E402

WIDTH = 375
VIEWPORT = {"width": WIDTH, "height": 812}
SECRET = "test-secret"
SHOT_DIR = Path(__file__).resolve().parents[1] / "docs" / "design" / "375px"

# Elements that must stay fully inside the viewport: a primary action the coach
# cannot reach is a broken screen even if nothing technically overflows.
REACHABLE = "button.btn-primary, button[type=submit], .seg a"

MISSING_BROWSER = (
    "playwright browsers not installed: run `uv run playwright install chromium`"
)


@dataclass
class Screen:
    """One dashboard screen to render.

    ``marker`` is a selector unique to this screen. Asserting it is present is
    what stops a screen from silently passing while showing something else - the
    login interstitial auto-submits itself, so without this the suite happily
    screenshotted the roster and reported the login screen as clean.

    ``javascript`` is off only for that interstitial, whose inline script posts
    the form immediately; with JS disabled the static page it renders for a
    no-JS client stays put and can actually be measured.
    """

    name: str
    path: str
    marker: str
    javascript: bool = True


@dataclass
class Rendered:
    """What one screen measured at 375px."""

    name: str
    scroll_width: int
    widest: str
    offenders: list[str] = field(default_factory=list)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def live_dashboard(tmp_path_factory):
    """A real HTTP server on a real port, seeded with a populated gym.

    Playwright drives a browser over the network, so the in-process aiohttp
    TestClient used elsewhere in the suite is not enough - this binds a socket.
    The gym is *populated* deliberately: empty screens collapse to a narrow
    column and would hide precisely the overflow this module exists to catch.
    """
    tmp_path = tmp_path_factory.mktemp("px375")
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    routines = RoutineStore(engine, clock=clock)
    await linking.ensure_schema()
    await TrainingStore(engine, clock=clock).ensure_seeded()

    gym = await linking.create_gym("Iron Temple")
    coach = await linking.link_member(gym.id, "Coach Ana", "telegram", "1")
    await linking.set_coach(coach.id, True)

    # Long names and a full week of exercises push the layout harder than a
    # minimal fixture would - narrow content passes 375px trivially.
    members = []
    for index, name in enumerate(
        ["Luis Fernando Martínez", "Ana", "Guadalupe Hernández de la Cruz"], start=2
    ):
        member = await linking.link_member(gym.id, name, "telegram", str(index))
        await routines.save_routine(
            member.id,
            gym.id,
            [
                WorkoutSpec(
                    weekday=weekday,
                    name=f"Día {weekday} — empuje y tirón",
                    exercises=[
                        ExerciseSpec("squat", sets=3, reps="5"),
                        ExerciseSpec("bench", sets=3, reps="8"),
                    ],
                )
                for weekday in range(5)
            ],
        )
        members.append(member)

    # A Preset with a master routine: the Presets screen renders its per-preset
    # `.actions` block only when presets exist, so without this that screen is
    # an empty page and its 375px result would prove nothing.
    preset = await routines.create_preset(gym.id, "Principiantes - cuerpo completo")
    await routines.save_preset_master(
        preset.id,
        gym.id,
        coach.id,
        [
            WorkoutSpec(
                weekday=0,
                name="Full body",
                exercises=[ExerciseSpec("squat", sets=3, reps="8-10")],
            )
        ],
        base_routine_id=None,
    )

    app = build_app(
        store,
        linking,
        session_secret=SECRET,
        bot_username="testbot",
        secure_cookies=False,
        clock=clock,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        yield {
            "base": str(server.make_url("/")).rstrip("/"),
            "cookie": sign_session(coach.id, gym.id, SECRET, clock()),
            "token": await store.create_login_token(coach.id, gym.id),
            "members": members,
            "preset": preset.id,
        }
    finally:
        await server.close()
        await engine.dispose()


def _screens(env) -> list[Screen]:
    """Every screen the issue lists, with the selector that proves we're on it."""
    member = env["members"][0].id
    return [
        Screen("table", "/?view=table", "#search"),
        Screen("cards", "/?view=cards", ".grid"),
        # Both Split variants: the roster-level one renders _split_placeholder
        # in the right pane, a different layout from the member-open variant.
        Screen("split-empty", "/?view=split", ".split"),
        Screen("split", f"/members/{member}?view=split", ".split"),
        Screen("member", f"/members/{member}", "header.mhead"),
        Screen("routine-editor", f"/members/{member}/routine", ".editor-wrap"),
        # The Preset master editor is a separate route and was the last
        # coach-facing screen with no 375px verification at all.
        Screen(
            "preset-routine-editor",
            f"/presets/{env['preset']}/routine",
            ".editor-wrap",
        ),
        Screen("presets", "/presets", ".actions"),
        Screen("settings", "/settings", ".setcard"),
        # JS off: the interstitial's inline script submits the form on load, so
        # with JS on this lands on the roster and measures the wrong page.
        Screen("login", f"/login/{env['token']}", "form#go", javascript=False),
    ]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def rendered(live_dashboard) -> list[Rendered]:
    """Render every screen once at 375px and collect the measurements.

    Module-scoped so this really is one browser launch for both assertions -
    function scope would silently re-run the whole pass per test, paying for
    nine page loads and a second set of screenshots to look at identical
    numbers. The tests only read from the result, so sharing it is safe.
    """
    env = live_dashboard
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[Rendered] = []

    async with async_playwright() as pw:
        # The package can be installed while the browser binaries are not -
        # `pip install ".[browser]"` without the `playwright install chromium`
        # half. importorskip cannot see that, and without this the module ERRORs
        # instead of skipping, the opposite of what this file promises.
        #
        # Checked via executable_path (an API) rather than only by matching the
        # error prose, which is not a stable interface: a Playwright release that
        # rephrases its message would otherwise silently turn this skip back into
        # an error. The prose match stays as a backstop for launch failures the
        # path check cannot foresee.
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip(MISSING_BROWSER)
        try:
            browser = await pw.chromium.launch()
        except PlaywrightError as exc:
            if "playwright install" not in str(
                exc
            ) and "Executable doesn't exist" not in str(exc):
                raise
            pytest.skip(f"{MISSING_BROWSER} ({exc.__class__.__name__})")
        try:
            for screen in _screens(env):
                context = await browser.new_context(
                    viewport=VIEWPORT, java_script_enabled=screen.javascript
                )
                await context.add_cookies(
                    [
                        {
                            "name": SESSION_COOKIE,
                            "value": env["cookie"],
                            "url": env["base"],
                        }
                    ]
                )
                page = await context.new_page()
                response = await page.goto(
                    f"{env['base']}{screen.path}",
                    wait_until="domcontentloaded"
                    if not screen.javascript
                    else "networkidle",
                )
                assert response is not None and response.ok, (
                    f"{screen.name}: HTTP {response.status if response else 'no response'}"
                )
                # Proves we are on the intended screen before measuring it.
                assert await page.query_selector(screen.marker) is not None, (
                    f"{screen.name}: marker {screen.marker!r} absent - the browser is not on "
                    f"this screen (redirected?), so its 375px result would be meaningless"
                )
                await page.screenshot(
                    path=str(SHOT_DIR / f"{screen.name}-375.png"), full_page=True
                )
                width = await page.evaluate("document.documentElement.scrollWidth")
                widest = await page.evaluate(
                    """() => {
                      let worst = null, max = 0;
                      for (const el of document.querySelectorAll('*')) {
                        const r = el.getBoundingClientRect();
                        if (r.right > max) { max = r.right; worst = el; }
                      }
                      return worst
                        ? `${worst.tagName.toLowerCase()}.${worst.className || '(no class)'}`
                          + ` -> right:${Math.round(max)}px`
                        : 'unknown';
                    }"""
                )
                offenders = await page.evaluate(
                    """(selector) => {
                      const out = [];
                      for (const el of document.querySelectorAll(selector)) {
                        const r = el.getBoundingClientRect();
                        // Skip anything the user cannot see. A zero-sized box is
                        // the common case, but visibility:hidden keeps a real
                        // rect, and reporting one of those as an unreachable
                        // action would be a phantom failure.
                        if (r.width === 0 && r.height === 0) continue;
                        const cs = getComputedStyle(el);
                        if (cs.visibility === 'hidden' || cs.display === 'none') continue;
                        // Horizontal only, deliberately - see the module
                        // docstring: pages are routinely taller than the
                        // viewport and scrolling down is not a defect.
                        if (r.left < 0 || r.right > window.innerWidth) {
                          out.push(`${el.tagName.toLowerCase()}.${el.className || '(no class)'}`
                                   + ` [${Math.round(r.left)}..${Math.round(r.right)}]`);
                        }
                      }
                      return out;
                    }""",
                    REACHABLE,
                )
                results.append(Rendered(screen.name, width, widest, offenders))
                await context.close()
        finally:
            await browser.close()

    assert len(results) == len(_screens(env)), "not every screen was rendered"
    return results


async def test_no_screen_overflows_horizontally_at_375px(rendered):
    """The check the CSS-level sweep cannot make: real layout, real widths."""
    failures = [
        # Name the widest offender: "something overflows" is not actionable, and
        # the screenshot alone rarely identifies which element did it.
        f"{r.name}: scrollWidth {r.scroll_width}px > {WIDTH}px; widest: {r.widest}"
        for r in rendered
        if r.scroll_width > WIDTH
    ]
    assert not failures, "horizontal overflow at 375px:\n  " + "\n  ".join(failures)


async def test_primary_actions_stay_inside_the_viewport_at_375px(rendered):
    """A submit button pushed off-canvas is unreachable on a phone even when the
    document itself reports no overflow."""
    failures = [
        f"{r.name}: {offender} outside 0..{WIDTH}"
        for r in rendered
        for offender in r.offenders
    ]
    assert not failures, "primary actions unreachable at 375px:\n  " + "\n  ".join(
        failures
    )
