"""Real-browser 375px rendering checks for every dashboard screen (issue #157).

Since the React cutover (#154) every screen is the SPA, so a static analysis
of a stylesheet cannot see layout at all - only a real browser can. This
module renders each screen at 375x812 (iPhone SE) and asserts on the layout
the user actually gets: no horizontal overflow, and no primary action pushed
off-canvas.

The server serves the built bundle, so these tests need `npm run build` in
frontend/ first; they skip with a clear message when the bundle is absent.

## Why this skips rather than fails when Playwright is absent

Playwright and its browser binaries are a heavy, optional dev dependency the
default `pytest` run does not install, so locally these tests skip unless you
opt in. In CI they run for real: the `e2e-375px` job in tests.yml installs
Chromium and builds the bundle - since #154 made React the only dashboard,
this is the layout gate (it was pointless to pay 115 MB per run to guard a
dashboard scheduled for deletion; that reason expired with the cutover).

It is deliberately an *importorskip* rather than the `@pytest.mark.skip` the
issue suggested. An unconditional skip can never fail, so it would rot
silently: the selectors below would drift out of sync with the markup and
nobody would learn until someone finally installed Playwright and found the
module broken. With importorskip the moment the dependency exists these tests
run and bite.

To run them:

    uv pip install ".[browser]" && uv run playwright install chromium
    uv run pytest tests/test_375px_playwright.py

Install through the `browser` extra, not a bare `pip install playwright`: the
extra carries the `>=1.40` pin, and bypassing it is how the two drift apart.

## Two deviations from the issue text, recorded rather than glossed

Step 2 says the test "signs in". It injects a pre-signed session cookie instead
of redeeming a login token. Login tokens are single-use, so redeeming one to
reach the roster would consume the token the login screen needs and make every
other screen depend on that redirect landing. The cookie is the same credential
the redemption would issue, so the screens under test are identical - only the
route in differs.

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
REACHABLE = "button[type=submit], .seg button, nav.quick a"

# The Vite bundle the server serves; without it every screen answers 503.
FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
MISSING_BUNDLE = "SPA bundle not built: run `npm run build` in frontend/"

MISSING_BROWSER = (
    "playwright browsers not installed: run `uv run playwright install chromium`"
)


@dataclass
class Screen:
    """One dashboard screen to render.

    ``marker`` is a selector this screen renders and its siblings do not.
    Asserting it is present (after React has painted - the wait below is on
    this marker, not on document load) is what stops a screen from silently
    passing while showing something else: the first version of this file
    happily screenshotted the roster and reported the login screen as clean.
    Markers must discriminate against the screens most likely to be served by
    mistake, which for the roster views means each other: a selector from the
    shared chrome would pass on all three.

    ``click`` optionally names an element to click once the screen is up -
    the Split view opens a member client-side, with no URL of its own since
    #154, so that variant is reached by clicking the rail.
    """

    name: str
    path: str
    marker: str
    click: str | None = None


@dataclass
class Rendered:
    """What one screen measured at 375px, or why it could not be measured.

    A screen that fails to load is recorded rather than raised: the fixture is
    shared, so an exception mid-pass would abort every screen after it and
    surface as one ERROR on both tests, throwing away the diagnostics - and the
    screenshots - for everything still unrendered. `test_every_screen_renders`
    turns these into a normal failure listing all of them at once.
    """

    name: str
    scroll_width: int = 0
    widest: str = ""
    offenders: list[str] = field(default_factory=list)
    error: str | None = None


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
    """Every screen the issue lists, with the selector that proves we're on it.

    All selectors are the React DOM's (#154). Discrimination notes:
    - table vs split: both render ``ul#roster``, but only the table's rows
      are links (the split rail uses buttons);
    - cards is the only view with the attendance ``.legend``;
    - the standalone member page is the only screen whose header links back
      to the roster (its back link carries the view it was opened from);
    - the two editors share a component, told apart by their back links.
    """
    member = env["members"][0].id
    return [
        Screen("table", "/?view=table", "ul#roster > li > a"),
        Screen("cards", "/?view=cards", ".legend"),
        # Both Split variants: the empty pane, and a member opened from the
        # rail — a client-side selection with no URL of its own since #154.
        Screen("split-empty", "/?view=split", ".split .pane-empty"),
        Screen(
            "split",
            "/?view=split",
            ".split .pane section.card",
            click="ul#roster > li > button",
        ),
        Screen("member", f"/members/{member}", 'header a[href^="/?view="]'),
        Screen(
            "routine-editor",
            f"/members/{member}/routine",
            f'header a[href="/members/{member}"]',
        ),
        # The Preset master editor is a separate route and was the last
        # coach-facing screen with no 375px verification at all.
        Screen(
            "preset-routine-editor",
            f"/presets/{env['preset']}/routine",
            'header a[href="/presets"]',
        ),
        Screen("presets", "/presets", "#preset-name"),
        Screen("settings", "/settings", "#invite"),
        # The React interstitial waits for a click (no auto-submit), so the
        # login screen measures itself even with JS on.
        Screen("login", f"/login/{env['token']}", 'form[action^="/login/"]'),
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
    if not (FRONTEND_DIST / "index.html").exists():
        pytest.skip(MISSING_BUNDLE)
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
                context = await browser.new_context(viewport=VIEWPORT)
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
                # `load`, not `networkidle`: Playwright documents networkidle
                # as discouraged, and it is what hid the login redirect in the
                # first version of this file. React paints after load, so the
                # on-screen proof below waits on the marker instead.
                response = await page.goto(
                    f"{env['base']}{screen.path}", wait_until="load"
                )
                if response is None or not response.ok:
                    status = response.status if response else "no response"
                    results.append(Rendered(screen.name, error=f"HTTP {status}"))
                    await context.close()
                    continue
                try:
                    if screen.click is not None:
                        await page.click(screen.click, timeout=10_000)
                    # Proves we are on the intended screen - and that React
                    # has painted it - before measuring.
                    await page.wait_for_selector(screen.marker, timeout=10_000)
                except PlaywrightError:
                    results.append(
                        Rendered(
                            screen.name,
                            error=f"marker {screen.marker!r} absent - the browser is not on "
                            f"this screen (redirected? bundle stale?), so its "
                            f"375px result would be meaningless",
                        )
                    )
                    await context.close()
                    continue
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

    assert len(results) == len(_screens(env)), "not every screen was visited"
    return results


async def test_every_screen_renders(rendered):
    """Loaded, and showing the screen we asked for.

    Separate from the layout assertions so a screen that never rendered reads as
    a render failure rather than a 375px failure - and so all of them are
    reported together instead of the first one aborting the pass.
    """
    failures = [f"{r.name}: {r.error}" for r in rendered if r.error]
    assert not failures, "screens failed to render:\n  " + "\n  ".join(failures)


async def test_no_screen_overflows_horizontally_at_375px(rendered):
    """The check the CSS-level sweep cannot make: real layout, real widths."""
    failures = [
        # Name the widest offender: "something overflows" is not actionable, and
        # the screenshot alone rarely identifies which element did it.
        f"{r.name}: scrollWidth {r.scroll_width}px > {WIDTH}px; widest: {r.widest}"
        for r in rendered
        if not r.error and r.scroll_width > WIDTH
    ]
    assert not failures, "horizontal overflow at 375px:\n  " + "\n  ".join(failures)


async def test_primary_actions_stay_inside_the_viewport_at_375px(rendered):
    """A submit button pushed off-canvas is unreachable on a phone even when the
    document itself reports no overflow."""
    failures = [
        f"{r.name}: {offender} outside 0..{WIDTH}"
        for r in rendered
        if not r.error
        for offender in r.offenders
    ]
    assert not failures, "primary actions unreachable at 375px:\n  " + "\n  ".join(
        failures
    )
