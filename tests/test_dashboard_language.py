"""The per-browser EN/ES language toggle (issue #106, spec-dashboard
§Language).

The toggle lives in the chrome, persisted in a long-lived cookie beside the
session cookie; a first visit defaults from ``Accept-Language``, falling
back to Spanish. Chrome, weekdays, months, relative time and the decimal
mark translate. Exercise names, Workout names and the Member's own words
(Notes, Set comments) never translate — the last carry a small
source-language tag when they differ from the language the Coach reads.
"""

from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentg.checkin_store import CheckinStore
from agentg.dashboard_i18n import fmt_date
from agentg.dashboard_store import DashboardStore
from agentg.dashboard_web import LANG_COOKIE, SESSION_COOKIE, build_app, sign_session
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from agentg.models import Member
from agentg.notes import NotesStore
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.training import TrainingStore
from conftest import FakeClock

SECRET = "test-secret"


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    coach = await linking.link_member(gym.id, "Coach Ana", "telegram", "1")
    await linking.set_coach(coach.id, True)
    app = build_app(
        store,
        linking,
        session_secret=SECRET,
        bot_username="testbot",
        secure_cookies=False,
        clock=clock,
    )
    async with TestClient(TestServer(app)) as client:
        yield Env(clock, engine, linking, store, client, gym, coach)
    await engine.dispose()


class Env:
    def __init__(self, clock, engine, linking, store, client, gym, coach):
        self.clock = clock
        self.engine = engine
        self.linking = linking
        self.store = store
        self.client = client
        self.gym = gym
        self.coach = coach
        self.checkins = CheckinStore(engine)
        self.training = TrainingStore(engine, clock=clock)
        self.routines = RoutineStore(engine, clock=clock)
        self.notes = NotesStore(engine, clock=clock)
        self._uid = 100

    async def add_member(self, name: str) -> Member:
        self._uid += 1
        return await self.linking.link_member(self.gym.id, name, "telegram", str(self._uid))

    def cookies(self, **extra: str) -> dict:
        cookie = sign_session(self.coach.id, self.gym.id, SECRET, self.clock())
        return {SESSION_COOKIE: cookie, **extra}

    async def page(self, path: str, headers: dict | None = None, **cookies: str) -> str:
        response = await self.client.get(
            path, cookies=self.cookies(**cookies), headers=headers or {}
        )
        assert response.status == 200
        return await response.text()

    async def give_routine(self, member: Member) -> None:
        await self.training.ensure_seeded()
        await self.routines.save_routine(
            member.id,
            self.gym.id,
            [
                WorkoutSpec(
                    weekday=2,
                    name="Piernas",
                    exercises=[ExerciseSpec("squat", 4, "8-10")],
                ),
            ],
        )

    async def train(self, member: Member, days_ago: int, *set_lines: str) -> None:
        now = self.clock.now
        self.clock.now = now - timedelta(days=days_ago)
        await self.training.open_session(member.id, self.gym.id)
        for line in set_lines:
            await self.training.log_sets(member.id, self.gym.id, line)
        await self.training.close_session(member.id)
        self.clock.now = now


# --- first visit: Accept-Language, Spanish fallback ---


async def test_first_visit_defaults_from_accept_language(env):
    text = await env.page("/", headers={"Accept-Language": "en-US,en;q=0.9"})

    assert '<html lang="en">' in text
    assert "Members (0)" in text
    assert "Search by name" in text
    assert ">Table<" in text and ">Cards<" in text and ">Split<" in text


async def test_spanish_and_unrelated_headers_fall_back_to_spanish(env):
    for header in ({"Accept-Language": "es-MX,es;q=0.9"}, {"Accept-Language": "fr-FR"}, {}):
        text = await env.page("/", headers=header)
        assert '<html lang="es">' in text
        assert "Miembros (0)" in text
        assert ">Tabla<" in text and ">Tarjetas<" in text and ">Dividida<" in text


# --- the toggle: a long-lived cookie that wins over the header ---


async def test_the_toggle_sets_a_long_lived_cookie_and_redirects_back(env):
    response = await env.client.get("/lang/en?next=/?view=cards", allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"] == "/?view=cards"
    cookie = response.cookies[LANG_COOKIE]
    assert cookie.value == "en"
    assert int(cookie["max-age"]) > 365 * 24 * 3600  # long-lived, not session-scoped

    # …and it wins over a Spanish Accept-Language on later visits.
    text = await env.page("/", headers={"Accept-Language": "es"}, **{LANG_COOKIE: "en"})
    assert '<html lang="en">' in text


async def test_an_unknown_language_sets_no_cookie_and_goes_home(env):
    response = await env.client.get("/lang/klingon?next=/", allow_redirects=False)

    assert response.status == 302
    assert LANG_COOKIE not in response.cookies


async def test_an_external_redirect_target_is_refused(env):
    response = await env.client.get("/lang/en?next=https://evil.example", allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"] == "/"


# --- what translates ---


async def test_the_member_page_translates_chrome_dates_relative_time_and_decimals(env):
    member = await env.add_member("Luis")
    await env.give_routine(member)
    await env.train(member, 2, "squat 62.5 8,8,6")

    en = await env.page(f"/members/{member.id}", headers={"Accept-Language": "en"})
    es = await env.page(f"/members/{member.id}")

    # Chrome and headings.
    assert "Routine" in en and "Sessions" in en and "Last weights" in en
    assert "Rutina" in es and "Sesiones" in es and "Últimos pesos" in es
    # Weekdays.
    assert "Wednesday" in en and "miércoles" in es
    # Months in the formatted dates (linking stamps created_at itself; the
    # gym is UTC, so the row's date is the gym-local one).
    joined = member.created_at.date()
    assert f"Member since {fmt_date(joined, 'en')}" in en
    assert f"Miembro desde {fmt_date(joined, 'es')}" in es
    # Relative time.
    assert "2 days away" in en and "2 días sin venir" in es
    # The decimal mark.
    assert "62.5 kg" in en and "62,5 kg" in es


async def test_the_settings_screen_translates(env):
    en = await env.page("/settings", headers={"Accept-Language": "en"})
    es = await env.page("/settings")

    assert "Settings" in en and "Invite link" in en and "Regenerate" in en
    assert "Ajustes" in es and "Enlace de invitación" in es


async def test_cards_and_split_translate_too(env):
    await env.add_member("Luis")

    en_cards = await env.page("/?view=cards", headers={"Accept-Language": "en"})
    en_split = await env.page("/?view=split", headers={"Accept-Language": "en"})

    assert "On track" in en_cards and "last 4 weeks" in en_cards
    assert '<span class="wd">Mo</span>' in en_cards
    assert "Pick a member" in en_split


# --- what never translates ---


async def test_exercise_and_workout_names_never_translate(env):
    member = await env.add_member("Luis")
    await env.give_routine(member)  # Workout "Piernas", Exercise "squat"

    en = await env.page(f"/members/{member.id}", headers={"Accept-Language": "en"})

    assert "Piernas" in en
    assert "squat" in en


async def test_notes_carry_a_source_language_tag_only_on_mismatch(env):
    member = await env.add_member("Luis")
    await env.notes.remember(member.id, env.gym.id, "preference", "Hates burpees, will not do them")
    await env.notes.remember(member.id, env.gym.id, "constraint", "Solo puede entrenar por la mañana")

    es = await env.page(f"/members/{member.id}")
    en = await env.page(f"/members/{member.id}", headers={"Accept-Language": "en"})

    # The words themselves never move…
    assert "Hates burpees, will not do them" in es
    assert "Solo puede entrenar por la mañana" in en
    # …and only a foreign-language quote carries the small tag.
    assert "EN · textual" in es
    assert "ES · textual" not in es
    assert "ES · as written" in en
    assert "EN · as written" not in en


async def test_set_comments_render_verbatim_with_the_same_tag(env):
    member = await env.add_member("Luis")
    await env.training.ensure_seeded()
    now = env.clock.now
    env.clock.now = now - timedelta(days=1)
    await env.training.open_session(member.id, env.gym.id)
    await env.training.log_sets(
        member.id, env.gym.id, "squat 60 8,8", note="my shoulder hurt, stopped early"
    )
    await env.training.close_session(member.id)
    env.clock.now = now

    es = await env.page(f"/members/{member.id}")
    en = await env.page(f"/members/{member.id}", headers={"Accept-Language": "en"})

    assert "my shoulder hurt, stopped early" in es
    assert "EN · textual" in es
    assert "my shoulder hurt, stopped early" in en
    assert "EN · as written" not in en


async def test_a_set_comment_renders_once_no_matter_the_rep_count(env):
    """log_sets stamps the same note on every rep Set; the collapsed
    (Exercise, weight) line must still quote it exactly once."""
    member = await env.add_member("Luis")
    await env.training.ensure_seeded()
    await env.training.open_session(member.id, env.gym.id)
    await env.training.log_sets(
        member.id, env.gym.id, "squat 60 8,8,8", note="my shoulder hurt, stopped early"
    )
    await env.training.close_session(member.id)

    text = await env.page(f"/members/{member.id}")

    assert text.count("my shoulder hurt, stopped early") == 1


async def test_the_language_toggle_sits_in_the_chrome_of_every_screen(env):
    member = await env.add_member("Luis")

    for path in ("/", "/?view=cards", "/?view=split", f"/members/{member.id}", "/settings"):
        text = await env.page(path)
        assert 'href="/lang/en?' in text and 'href="/lang/es?' in text
