"""The Member page (issue #99, spec-dashboard §The Member page).

Store level: header facts, the active Routine, paginated Sessions, last
weight per Exercise read off the sets table, and Notes with their retired
tail. Page level: the shared bare 404 — a departed, forgotten, or mistyped
id all land on the same dead end — and the roster's click-through.
"""

from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentg.checkin_store import CheckinStore
from agentg.dashboard_i18n import fmt_date
from agentg.dashboard_store import DashboardStore
from agentg.dashboard_web import SESSION_COOKIE, build_app, sign_session
from agentg.db import create_engine
from agentg.forget import ForgetStore
from agentg.linking_store import LinkingStore
from agentg.models import Member
from agentg.notes import NotesStore
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.training import TrainingStore
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
    # The SDK's conversation tables, so ForgetStore's history wipe has them.
    from agents.extensions.memory import SQLAlchemySession

    await SQLAlchemySession("startup:schema", engine=engine, create_tables=True).get_items(
        limit=1
    )
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

    async def train(self, member: Member, days_ago: int, *set_lines: str) -> None:
        """A Session ``days_ago`` before the clock's now, logging set_lines."""
        now = self.clock.now
        self.clock.now = now - timedelta(days=days_ago)
        await self.training.open_session(member.id, self.gym.id)
        for line in set_lines:
            await self.training.log_sets(member.id, self.gym.id, line)
        await self.training.close_session(member.id)
        self.clock.now = now

    async def give_routine(self, member: Member) -> None:
        await self.training.ensure_seeded()  # save_routine draws from the catalog
        await self.routines.save_routine(
            member.id,
            self.gym.id,
            [
                WorkoutSpec(
                    weekday=2,
                    name="Piernas",
                    exercises=[ExerciseSpec("squat", 4, "8-10")],
                ),
                WorkoutSpec(
                    weekday=4,
                    name="Empuje",
                    exercises=[ExerciseSpec("bench press", 3, "10")],
                ),
            ],
        )

    async def page(self, member_id: int, query: str = "") -> tuple[int, str]:
        cookie = sign_session(self.coach.id, self.gym.id, SECRET, self.clock())
        response = await self.client.get(
            f"/members/{member_id}{query}", cookies={SESSION_COOKIE: cookie}
        )
        return response.status, await response.text()


async def test_the_page_shows_header_routine_sessions_weights_and_notes(env):
    member = await env.add_member("Luis")
    await env.give_routine(member)
    await env.train(member, 5, "squat 60 8,8,8", "bench press 40 10,10")
    await env.train(member, 2, "squat 65 8,8,6")
    await env.notes.remember(member.id, env.gym.id, "injury", "Rodilla izquierda molesta")
    retired = await env.notes.remember(member.id, env.gym.id, "goal", "Correr un maratón")
    await env.notes.retire(member.id, retired.id)

    status, text = await env.page(member.id)

    assert status == 200
    # Header: name, member-since, Session count, Gap, last Session.
    assert "Luis" in text
    assert f"Miembro desde {fmt_date(member.created_at.date(), 'es')}" in text
    assert "2 sesiones" in text
    assert "2 días sin venir" in text
    assert "última sesión" in text
    # Routine: pinned weekday names and the set/rep scheme.
    assert "Rutina" in text
    assert "miércoles" in text and "Piernas" in text
    assert "squat — 4 × 8-10" in text
    # Sessions: per-Exercise sets with the gym's weight unit.
    assert "Sesiones" in text
    assert "squat 65 kg × 8,8,6" in text
    # Last weight per Exercise: the newest Session's top set.
    assert "Últimos pesos" in text
    assert "squat" in text and "65 kg" in text
    assert "bench press" in text and "40 kg" in text
    # Notes, with the retired tail collapsed and dated.
    assert "Rodilla izquierda molesta" in text
    assert "Retiradas (1)" in text
    assert "Correr un maratón" in text
    assert "retirada el" in text


async def test_the_header_tags_lapsed_and_snoozed_members(env):
    lapsed = await env.add_member("Perdido")
    await env.train(lapsed, 30, "squat 60 8")
    await env.checkins.lapse(lapsed.id)
    snoozed = await env.add_member("Pausado")
    until = env.clock.now.date() + timedelta(days=5)
    await env.checkins.snooze_until(snoozed.id, until)

    _, lapsed_text = await env.page(lapsed.id)
    _, snoozed_text = await env.page(snoozed.id)

    assert "se perdió" in lapsed_text
    assert f"en pausa hasta el {fmt_date(until, 'es')}" in snoozed_text


async def test_sessions_paginate_ten_at_a_time(env):
    member = await env.add_member("Constante")
    for days_ago in range(12):
        await env.train(member, days_ago, "squat 60 8")

    status, first = await env.page(member.id)
    assert status == 200
    assert "12 sesiones" in first
    assert "página 1 de 2" in first
    assert "más antiguas ›" in first

    status, second = await env.page(member.id, "?page=2")
    assert status == 200
    assert "página 2 de 2" in second
    assert "‹ más recientes" in second
    # The two pages show different Sessions (the oldest only on page 2).
    # Anchored to the markup: a bare "4 jul 2026" would also match the
    # "14 jul 2026" that legitimately sits on page 1.
    oldest = f"<b>{fmt_date(env.clock.now.date() - timedelta(days=11), 'es')}</b>"
    assert oldest in second and oldest not in first


async def test_last_weight_reads_the_latest_sessions_top_set(env):
    member = await env.add_member("Progreso")
    await env.train(member, 5, "squat 50 8,8,8")
    await env.train(member, 2, "squat 40 10", "squat 55 8,8,6")

    view = await env.store.member_page(env.gym.id, member.id)

    assert view is not None
    squat = next(w for w in view.weights if w.exercise == "squat")
    assert squat.weight == 55  # the top set, not the warm-up
    assert squat.reps == [8, 8, 6]


async def test_a_coach_member_ghost_or_other_gyms_member_is_the_same_404(env):
    visible = await env.add_member("Visible")
    await env.train(visible, 1, "squat 60 8")
    # A gym switch's ghost: the channel identity moved to a fresh Member.
    ghost = await env.add_member("Ghost")
    ghost_uid = str(env._uid)
    other_gym = await env.linking.create_gym("Other Gym")
    outsider = await env.linking.link_member(other_gym.id, "Outsider", "telegram", ghost_uid)

    baseline = (await env.page(99999))[1]
    for url_id in (99999, outsider.id, ghost.id, env.coach.id, "abc"):
        status, text = await env.page(url_id)
        assert status == 404
        assert text == baseline
        assert "Visible" not in text and BOUNCE_MARKER not in text


async def test_a_forgotten_members_url_is_the_same_bare_404(env):
    member = await env.add_member("Olvidado")
    await env.train(member, 1, "squat 60 8")
    member_id = member.id
    await ForgetStore(env.engine).forget_member(member_id)

    status, text = await env.page(member_id)

    assert status == 404
    assert text == (await env.page(99999))[1]
    assert "Olvidado" not in text


async def test_the_member_page_bounces_anonymous_and_demoted_coaches(env):
    member = await env.add_member("Luis")

    response = await env.client.get(f"/members/{member.id}")
    assert response.status == 200
    assert BOUNCE_MARKER in await response.text()

    cookie = sign_session(env.coach.id, env.gym.id, SECRET, env.clock())
    await env.linking.set_coach(env.coach.id, False)
    response = await env.client.get(
        f"/members/{member.id}", cookies={SESSION_COOKIE: cookie}
    )
    assert BOUNCE_MARKER in await response.text()


async def test_roster_rows_link_to_the_member_page(env):
    member = await env.add_member("Beto")

    cookie = sign_session(env.coach.id, env.gym.id, SECRET, env.clock())
    response = await env.client.get("/", cookies={SESSION_COOKIE: cookie})
    text = await response.text()

    assert f'href="/members/{member.id}?view=table"' in text


async def test_the_gap_wording_matches_the_roster(env):
    member = await env.add_member("Hoy")
    await env.train(member, 0, "squat 60 8")

    _, member_text = await env.page(member.id)
    cookie = sign_session(env.coach.id, env.gym.id, SECRET, env.clock())
    roster_text = await (
        await env.client.get("/", cookies={SESSION_COOKIE: cookie})
    ).text()

    assert "entrenó hoy" in member_text
    assert "entrenó hoy" in roster_text
    assert "0 días sin venir" not in roster_text


async def test_a_sets_only_prescription_renders_its_set_count(env):
    member = await env.add_member("Fuerza")
    await env.training.ensure_seeded()
    await env.routines.save_routine(
        member.id,
        env.gym.id,
        [WorkoutSpec(weekday=0, name="Pesado", exercises=[ExerciseSpec("squat", 5, None)])],
    )

    status, text = await env.page(member.id)

    assert status == 200
    assert "squat — 5" in text
