"""The Routine editor on the Member page (issue #100, spec-dashboard
§Routines & Presets).

Store level: a Coach's web save goes through the supersession machinery
stamped coach-authored and actor-stamped, and a stale save (the active
Routine changed since the editor loaded) is refused. Page level: the
ownership chip (Agent-managed with its consequence line before the first
save, named Coach-authored after), the fresh version on a refused save,
and the chat notice the Member gets — their coach, named, plus the new
plan.

Web-layer tests for the JSON API replacement live in test_routine_api.py
(issue #151); the server-HTML editor covered here still ships until the
SPA cutover in #154.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentg.dashboard_store import DashboardStore
from agentg.dashboard_web import (
    AGENT_CHIP,
    BAD_SETS_ERROR,
    CONSEQUENCE_LINE,
    DUPLICATE_WEEKDAY_ERROR,
    EMPTY_ROUTINE_ERROR,
    EMPTY_WORKOUT_ERROR,
    NAME_TOO_LONG_ERROR,
    REPS_TOO_LONG_ERROR,
    SESSION_COOKIE,
    SETS_RANGE_ERROR,
    STALE_ERROR,
    UNDATED_BLOCK_ERROR,
    build_app,
    sign_session,
)
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from agentg.models import Member, Routine
from agentg.routines import ExerciseSpec, RoutineStore, StaleRoutineError, WorkoutSpec
from agentg.training import TrainingStore
from conftest import FakeClock

SECRET = "test-secret"
BOUNCE_MARKER = "/dashboard"  # the bounce page tells you to send /dashboard


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    async def send(self, channel: str, channel_user_id: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram is down")
        self.sent.append((channel, channel_user_id, text))


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    notifier = FakeNotifier()
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
        notifier=notifier,
    )
    async with TestClient(TestServer(app)) as client:
        yield Env(clock, engine, linking, store, notifier, client, gym, coach)
    await engine.dispose()


class Env:
    def __init__(self, clock, engine, linking, store, notifier, client, gym, coach):
        self.clock = clock
        self.engine = engine
        self.linking = linking
        self.store = store
        self.notifier = notifier
        self.client = client
        self.gym = gym
        self.coach = coach
        self.training = TrainingStore(engine, clock=clock)
        self.routines = RoutineStore(engine, clock=clock)
        self._uid = 100

    async def add_member(self, name: str) -> Member:
        self._uid += 1
        return await self.linking.link_member(
            self.gym.id, name, "telegram", str(self._uid)
        )

    async def give_routine(self, member: Member) -> Routine:
        """An agent-generated Routine: Wednesday legs, Friday push."""
        await self.training.ensure_seeded()  # save_routine draws from the catalog
        return await self.routines.save_routine(
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

    def cookies(self) -> dict[str, str]:
        return {
            SESSION_COOKIE: sign_session(
                self.coach.id, self.gym.id, SECRET, self.clock()
            )
        }

    async def get(self, path: str) -> tuple[int, str]:
        response = await self.client.get(path, cookies=self.cookies())
        return response.status, await response.text()

    async def save_via_web(
        self,
        member_id: int,
        base_routine_id: int | None,
        *days: tuple[str, str, str],
        headers: dict[str, str] | None = None,
    ):
        """POST the editor form; ``days`` are (weekday, workout name, exercises
        textarea body) triples."""
        data: list[tuple[str, str]] = [
            ("base_routine_id", "" if base_routine_id is None else str(base_routine_id))
        ]
        for weekday, name, exercises in days:
            data += [("weekday", weekday), ("workout_name", name), ("exercises", exercises)]
        return await self.client.post(
            f"/members/{member_id}/routine",
            data=data,
            cookies=self.cookies(),
            allow_redirects=False,
            headers=headers or {},
        )


# --- Store level: the stamped save and the stale refusal ---


async def test_a_coach_save_supersedes_stamped_and_locks_out_the_agent(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    saved = await env.routines.save_coach_routine(
        member.id,
        env.gym.id,
        env.coach.id,
        [WorkoutSpec(weekday=0, name="Full body", exercises=[ExerciseSpec("squat", 3, "8")])],
        base_routine_id=old.id,
    )

    assert saved.coach_authored is True
    assert saved.created_by_member_id == env.coach.id
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == saved.id
    assert active["coach_authored"] is True
    assert active["created_by_name"] == "Coach Ana"
    async with env.routines._sessions() as db:
        assert (await db.get(Routine, old.id)).is_active is False

    # The Agent never restructures a coach-authored Routine.
    with pytest.raises(ValueError):
        await env.routines.save_routine(
            member.id,
            env.gym.id,
            [WorkoutSpec(weekday=1, name="X", exercises=[ExerciseSpec("squat")])],
        )


async def test_an_agent_save_stays_unstamped(env):
    member = await env.add_member("Luis")
    routine = await env.give_routine(member)
    assert routine.created_by_member_id is None
    assert (await env.routines.active_routine(member.id))["created_by_name"] is None


async def test_a_stale_save_is_refused_and_changes_nothing(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)
    # The Agent replaced the Routine after the editor loaded.
    newer = await env.routines.save_routine(
        member.id,
        env.gym.id,
        [WorkoutSpec(weekday=1, name="Nuevo", exercises=[ExerciseSpec("squat")])],
    )

    with pytest.raises(StaleRoutineError):
        await env.routines.save_coach_routine(
            member.id,
            env.gym.id,
            env.coach.id,
            [WorkoutSpec(weekday=0, name="Full body", exercises=[ExerciseSpec("squat")])],
            base_routine_id=old.id,
        )

    assert (await env.routines.active_routine(member.id))["routine_id"] == newer.id


# --- Page level: the chip, the save flow, the stale refusal, the notice ---


async def test_the_member_api_carries_the_agent_chip_facts(env):
    """The React member page renders the Edit link and the ownership chip
    from these fields (MemberPage RTL covers the link and chip markup)."""
    import json

    member = await env.add_member("Luis")
    await env.give_routine(member)

    status, text = await env.get(f"/api/members/{member.id}")
    data = json.loads(text)

    assert status == 200
    assert data["coach_authored"] is False  # -> the Agent chip
    assert data["routine_author"] is None
    assert data["routine_preset_name"] is None


async def test_the_editor_shows_the_consequence_line_before_the_first_save(env):
    member = await env.add_member("Luis")
    await env.give_routine(member)

    status, text = await env.get(f"/members/{member.id}/routine")

    assert status == 200
    assert AGENT_CHIP in text
    assert CONSEQUENCE_LINE in text
    assert "Escrita por" not in text
    # The editor remembers which Routine it loaded, for the stale check.
    routine_id = (await env.routines.active_routine(member.id))["routine_id"]
    assert f'name="base_routine_id" value="{routine_id}"' in text
    # The current plan is pre-filled: weekday, workout name, exercises.
    assert "Piernas" in text
    assert "squat" in text


async def test_a_web_save_replaces_the_routine_stamps_it_and_notifies_the_member(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)
    # A running Session is never disturbed by a web save.
    opened = await env.training.open_session(member.id, env.gym.id)

    response = await env.save_via_web(
        member.id, old.id, ("0", "Full body", "squat, 3, 8\nbench press, 3, 10")
    )

    assert response.status == 302
    # Back to the Member page, in the view the editor journey started from.
    assert response.headers["Location"] == f"/members/{member.id}?view=table"
    active = await env.routines.active_routine(member.id)
    assert active["coach_authored"] is True
    assert active["created_by_name"] == "Coach Ana"
    assert [w["name"] for w in active["workouts"]] == ["Full body"]
    assert active["workouts"][0]["weekday"] == 0
    assert active["workouts"][0]["exercises"] == [
        {"exercise": "squat", "sets": 3, "reps": "8"},
        {"exercise": "bench press", "sets": 3, "reps": "10"},
    ]
    # The visit carries on as it started: the same Session is still open.
    session = await env.training.get_session(opened.session_id)
    assert session.closed_at is None

    # The Member hears it from the Agent: the coach, named, plus the new plan.
    assert len(env.notifier.sent) == 1
    channel, channel_user_id, text = env.notifier.sent[0]
    assert (channel, channel_user_id) == ("telegram", str(env._uid))
    assert "Coach Ana" in text
    assert "Full body" in text
    assert "squat" in text

    # After the first save the chip reads named Coach-authored, permanently.
    _, editor = await env.get(f"/members/{member.id}/routine")
    assert "Escrita por Coach Ana" in editor
    assert AGENT_CHIP not in editor
    assert CONSEQUENCE_LINE not in editor
    import json

    _, page = await env.get(f"/api/members/{member.id}")
    page_data = json.loads(page)
    assert page_data["coach_authored"] is True
    assert page_data["routine_author"] == "Coach Ana"


async def test_editing_a_linked_member_silently_forks_and_shows_the_consequence_before_save(env):
    await env.training.ensure_seeded()
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    await env.routines.save_preset_master(
        preset.id,
        env.gym.id,
        env.coach.id,
        [WorkoutSpec(weekday=0, name="Preset day", exercises=[ExerciseSpec("squat")])],
        base_routine_id=None,
    )
    member = await env.add_member("Luis")
    await env.routines.apply_preset(preset.id, env.gym.id, env.coach.id, [member.id])
    old = await env.routines.active_routine(member.id)

    status, editor = await env.get(f"/members/{member.id}/routine")
    assert status == 200
    assert "Preset: Beginner" in editor
    assert CONSEQUENCE_LINE in editor

    response = await env.save_via_web(
        member.id, old["routine_id"], ("1", "Forked day", "bench press, 3, 8")
    )

    assert response.status == 302
    active = await env.routines.active_routine(member.id)
    assert active["preset_id"] is None
    assert active["created_by_name"] == "Coach Ana"
    _, editor = await env.get(f"/members/{member.id}/routine")
    assert "Escrita por Coach Ana" in editor
    assert CONSEQUENCE_LINE not in editor


async def test_a_stale_web_save_is_refused_and_shows_the_fresh_version(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)
    # The Agent replaced the Routine after the editor loaded.
    await env.routines.save_routine(
        member.id,
        env.gym.id,
        [WorkoutSpec(weekday=1, name="Nuevo plan", exercises=[ExerciseSpec("squat")])],
    )

    response = await env.save_via_web(
        member.id, old.id, ("0", "Full body", "squat, 3, 8")
    )

    assert response.status == 409
    text = await response.text()
    assert STALE_ERROR in text
    # The fresh version is on the page read-only, and the Coach's own edits
    # survive in the form — a refused save never destroys typed work.
    assert "Nuevo plan" in text
    assert "squat, 3, 8" in text
    assert "Full body" in text
    # Nothing was written.
    active = await env.routines.active_routine(member.id)
    assert [w["name"] for w in active["workouts"]] == ["Nuevo plan"]
    assert env.notifier.sent == []
    # The stamp re-arms against the fresh Routine, so saving again applies
    # the kept edits on top of it, knowingly.
    assert f'name="base_routine_id" value="{active["routine_id"]}"' in text


async def test_unknown_exercises_are_rejected_with_nothing_saved(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.save_via_web(
        member.id, old.id, ("0", "Full body", "sentadilla mágica, 3, 8")
    )

    assert response.status == 400
    text = await response.text()
    # Spanish, coach-facing — never the raw English agent-tool message.
    assert "no están en el catálogo" in text
    assert "list_exercises" not in text
    assert "sentadilla mágica" in text
    # The Coach's edits survive on the page.
    assert "Full body" in text
    assert "sentadilla mágica, 3, 8" in text
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id
    assert env.notifier.sent == []


async def test_the_editor_is_coach_only_and_member_scoped(env):
    member = await env.add_member("Luis")
    await env.give_routine(member)

    # Anonymous: the friendly bounce, both routes.
    assert BOUNCE_MARKER in await (
        await env.client.get(f"/members/{member.id}/routine")
    ).text()
    assert BOUNCE_MARKER in await (
        await env.client.post(f"/members/{member.id}/routine", data={})
    ).text()

    # Unknown, departed, or another Gym's Member: the shared bare 404.
    status, _ = await env.get("/members/99999/routine")
    assert status == 404
    other_gym = await env.linking.create_gym("Other Gym")
    outsider = await env.linking.link_member(other_gym.id, "Mara", "telegram", "7")
    status, _ = await env.get(f"/members/{outsider.id}/routine")
    assert status == 404


async def test_a_first_routine_can_be_written_from_the_editor(env):
    member = await env.add_member("Luis")
    await env.training.ensure_seeded()

    response = await env.save_via_web(
        member.id, None, ("2", "Piernas", "squat, 4, 8-10")
    )

    assert response.status == 302
    active = await env.routines.active_routine(member.id)
    assert active is not None
    assert active["coach_authored"] is True
    assert active["created_by_name"] == "Coach Ana"


async def test_a_stale_save_after_an_empty_editor_is_refused(env):
    """The editor loaded with NO active Routine (base None): if the Agent
    saved one from chat in the meantime, the web save must refuse, show the
    fresh plan, and never overwrite it."""
    member = await env.add_member("Luis")
    await env.training.ensure_seeded()
    # The Agent writes a plan from chat after the empty editor loaded.
    await env.routines.save_routine(
        member.id,
        env.gym.id,
        [WorkoutSpec(weekday=1, name="Del agente", exercises=[ExerciseSpec("squat")])],
    )

    response = await env.save_via_web(
        member.id, None, ("0", "Full body", "squat, 3, 8")
    )

    assert response.status == 409
    text = await response.text()
    assert STALE_ERROR in text
    assert "Del agente" in text
    active = await env.routines.active_routine(member.id)
    assert [w["name"] for w in active["workouts"]] == ["Del agente"]
    assert active["coach_authored"] is False
    assert env.notifier.sent == []


async def test_a_day_without_exercises_is_rejected(env):
    """A picked weekday with zero exercise lines must not save as a real
    (empty) Workout — the Coach either fills it or drops the day."""
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.save_via_web(
        member.id, old.id, ("0", "Full body", ""), ("2", "Piernas", "squat, 4, 8-10")
    )

    assert response.status == 400
    assert EMPTY_WORKOUT_ERROR in await response.text()
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id
    assert env.notifier.sent == []


async def test_duplicate_weekdays_are_rejected(env):
    """Two blocks on the same weekday would save fine but downstream pickers
    would disagree about which Workout the day has — refuse the save."""
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.save_via_web(
        member.id,
        old.id,
        ("0", "Piernas", "squat, 4, 8-10"),
        ("0", "Empuje", "bench press, 3, 10"),
    )

    assert response.status == 400
    assert DUPLICATE_WEEKDAY_ERROR in await response.text()
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id
    assert env.notifier.sent == []


async def test_forgetting_the_coach_blanks_the_actor_stamp(tmp_path):
    """Forget-me on the Coach must not trip the created_by_member_id FK
    (Postgres would abort the wipe): the Routines they wrote survive with
    the stamp blanked, and the chip degrades to plain Coach-authored."""
    from agents.extensions.memory import SQLAlchemySession
    from sqlalchemy import event

    from agentg.forget import ForgetStore

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys_on(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    clock = FakeClock()
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    # The SDK's conversation tables, so ForgetStore's history wipe has them.
    await SQLAlchemySession("startup:schema", engine=engine, create_tables=True).get_items(
        limit=1
    )
    gym = await linking.create_gym("Iron Temple")
    coach = await linking.link_member(gym.id, "Coach Ana", "telegram", "1")
    await linking.set_coach(coach.id, True)
    member = await linking.link_member(gym.id, "Luis", "telegram", "2")
    training = TrainingStore(engine, clock=clock)
    routines = RoutineStore(engine, clock=clock)
    await training.ensure_seeded()
    routine = await routines.save_coach_routine(
        member.id,
        gym.id,
        coach.id,
        [WorkoutSpec(weekday=0, name="Full body", exercises=[ExerciseSpec("squat", 3, "8")])],
        base_routine_id=None,
    )

    await ForgetStore(engine).forget_member(coach.id)

    async with routines._sessions() as db:
        kept = await db.get(Routine, routine.id)
        assert kept is not None
        assert kept.created_by_member_id is None
    active = await routines.active_routine(member.id)
    assert active["coach_authored"] is True
    assert active["created_by_name"] is None
    await engine.dispose()


async def test_an_undated_block_with_content_is_rejected(env):
    """A block with exercise lines but the weekday left on «— día —» is a
    mistake, not a removal: refuse the save, don't silently drop the work.
    (Removing a day = clear its exercises AND unset the weekday.)"""
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.save_via_web(
        member.id, old.id, ("", "Suelto", "squat, 3, 8"), ("2", "Piernas", "squat, 4, 8-10")
    )

    assert response.status == 400
    assert UNDATED_BLOCK_ERROR in await response.text()
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id
    assert env.notifier.sent == []


async def test_a_rejected_save_keeps_the_coachs_edits_on_the_page(env):
    """Every 400 (here: non-numeric sets) re-renders the SUBMITTED form, not
    the stored plan — the Coach fixes the bad line, not retypes the day."""
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.save_via_web(
        member.id,
        old.id,
        ("0", "Tren inferior", "squat, cuatro, 8"),
        ("4", "Tren superior", "bench press, 3, 10"),
    )

    assert response.status == 400
    text = await response.text()
    assert BAD_SETS_ERROR in text
    # Both edited blocks are still on the page, invalid line included.
    assert "Tren inferior" in text
    assert "squat, cuatro, 8" in text
    assert "Tren superior" in text
    assert "bench press, 3, 10" in text
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id


async def test_a_base_routine_of_another_member_is_stale_and_untouched(env):
    """The stale gate's conditional deactivation is member-scoped: a base id
    pointing at ANOTHER Member's active Routine refuses the save and must
    not deactivate that Routine."""
    member = await env.add_member("Luis")
    other = await env.add_member("Mara")
    await env.give_routine(member)
    other_routine = await env.give_routine(other)

    with pytest.raises(StaleRoutineError):
        await env.routines.save_coach_routine(
            member.id,
            env.gym.id,
            env.coach.id,
            [WorkoutSpec(weekday=0, name="X", exercises=[ExerciseSpec("squat")])],
            base_routine_id=other_routine.id,
        )

    active = await env.routines.active_routine(other.id)
    assert active["routine_id"] == other_routine.id


async def test_a_chat_coach_write_is_actor_stamped(env):
    """The chat path keeps this PR's invariant (NULL = written by the
    Agent): a Coach's write_routine from chat carries their stamp, so the
    chip names them like a web save would."""
    from agentg.coaching import write_routine_action
    from agentg.context import MemberContext
    from agentg.stores import Stores

    member = await env.add_member("Luis")
    await env.training.ensure_seeded()
    ctx = MemberContext(
        stores=Stores.from_engine(env.engine, clock=env.clock),
        member_id=env.coach.id,
        gym_id=env.gym.id,
        member_name=env.coach.name,
        gym_name=env.gym.name,
        weight_unit="kg",
        is_coach=True,
    )

    result = await write_routine_action(
        ctx,
        "Luis",
        None,
        [WorkoutSpec(weekday=0, name="Chat plan", exercises=[ExerciseSpec("squat", 3, "8")])],
    )

    assert result.get("routine_id") is not None
    active = await env.routines.active_routine(member.id)
    assert active["coach_authored"] is True
    assert active["created_by_name"] == "Coach Ana"


async def test_a_400_keeps_the_submitted_base_so_a_retry_still_catches_staleness(env):
    """A parse-error page must carry the SUBMITTED base_routine_id, not the
    fresh view's — otherwise a retry writes over the Agent's newer Routine
    with no stale refusal."""
    member = await env.add_member("Luis")
    old = await env.give_routine(member)
    # The Agent supersedes after the editor loaded.
    await env.routines.save_routine(
        member.id,
        env.gym.id,
        [WorkoutSpec(weekday=1, name="Nuevo plan", exercises=[ExerciseSpec("squat")])],
    )

    # First POST: stale base AND a parse error. The 400 page keeps base A.
    response = await env.save_via_web(
        member.id, old.id, ("0", "Tren", "squat, cuatro, 8")
    )
    assert response.status == 400
    text = await response.text()
    assert f'name="base_routine_id" value="{old.id}"' in text

    # Retry with valid data and the same base -> refused, nothing overwritten.
    response = await env.save_via_web(
        member.id, old.id, ("0", "Tren", "squat, 4, 8")
    )
    assert response.status == 409
    assert STALE_ERROR in await response.text()
    active = await env.routines.active_routine(member.id)
    assert [w["name"] for w in active["workouts"]] == ["Nuevo plan"]
    assert active["coach_authored"] is False


async def test_two_active_routines_cannot_coexist(env):
    """The DB-level backstop for the base=None race: a second active Routine
    for one Member violates the partial unique index, so an overlap of two
    no-base saves can never produce two active rows."""
    from sqlalchemy.exc import IntegrityError

    member = await env.add_member("Luis")
    await env.give_routine(member)

    async with env.routines._sessions() as db:
        db.add(
            Routine(
                gym_id=env.gym.id,
                member_id=member.id,
                is_active=True,
                created_at=env.clock(),
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_a_competing_save_during_a_no_base_coach_save_is_refused(env, monkeypatch):
    """The no-base gate must hold at WRITE time, not just at check time: an
    active Routine that appears mid-save (here: committed during the catalog
    validation) is refused, never silently deactivated by _save."""
    from agentg import routines as routines_module

    member = await env.add_member("Luis")
    await env.training.ensure_seeded()

    original_find = routines_module.find_exercise
    fired = False

    async def interleaved_find(db, norm):
        nonlocal fired
        if not fired:
            fired = True
            # A competing save lands mid-validation (the Agent, from chat).
            await env.routines.save_routine(
                member.id,
                env.gym.id,
                [WorkoutSpec(weekday=1, name="Del agente", exercises=[ExerciseSpec("squat")])],
            )
        return await original_find(db, norm)

    monkeypatch.setattr(routines_module, "find_exercise", interleaved_find)

    with pytest.raises(StaleRoutineError):
        await env.routines.save_coach_routine(
            member.id,
            env.gym.id,
            env.coach.id,
            [WorkoutSpec(weekday=0, name="Full body", exercises=[ExerciseSpec("squat", 3, "8")])],
            base_routine_id=None,
        )

    # The competing Routine survives, active and untouched.
    active = await env.routines.active_routine(member.id)
    assert [w["name"] for w in active["workouts"]] == ["Del agente"]
    assert active["coach_authored"] is False


async def test_a_conflicting_agent_save_surfaces_a_structured_error(env, monkeypatch):
    """The one-active-per-Member index can fire on the agent/chat path too
    (a web save committing mid-turn): it must surface as a structured
    StaleRoutineError — which write_routine_action turns into a clean
    {"error": ...} payload — never a raw IntegrityError crash."""
    from sqlalchemy.exc import IntegrityError

    from agentg.coaching import write_routine_action
    from agentg.context import MemberContext
    from agentg.stores import Stores

    member = await env.add_member("Luis")
    await env.training.ensure_seeded()

    async def conflicting_save(self, db, *args, **kwargs):
        raise IntegrityError(
            "INSERT INTO routines", {}, Exception("UNIQUE constraint failed")
        )

    monkeypatch.setattr(RoutineStore, "_save", conflicting_save)

    with pytest.raises(StaleRoutineError):
        await env.routines.save_routine(
            member.id,
            env.gym.id,
            [WorkoutSpec(weekday=0, name="X", exercises=[ExerciseSpec("squat")])],
        )

    ctx = MemberContext(
        stores=Stores.from_engine(env.engine, clock=env.clock),
        member_id=env.coach.id,
        gym_id=env.gym.id,
        member_name=env.coach.name,
        gym_name=env.gym.name,
        weight_unit="kg",
        is_coach=True,
    )
    result = await write_routine_action(
        ctx, "Luis", None, [WorkoutSpec(weekday=0, name="X", exercises=[ExerciseSpec("squat")])]
    )
    assert "error" in result


async def test_the_editor_post_is_scoped_like_the_get(env):
    """POSTing another Gym's Member or a coach-flagged Member hits the same
    shared 404 as the GET — and writes nothing."""
    member = await env.add_member("Luis")
    await env.give_routine(member)
    other_gym = await env.linking.create_gym("Other Gym")
    outsider = await env.linking.link_member(other_gym.id, "Mara", "telegram", "7")

    response = await env.save_via_web(outsider.id, None, ("0", "X", "squat"))
    assert response.status == 404
    response = await env.save_via_web(env.coach.id, None, ("0", "X", "squat"))
    assert response.status == 404

    assert await env.routines.active_routine(outsider.id) is None
    assert await env.routines.active_routine(env.coach.id) is None
    assert env.notifier.sent == []


async def test_ensure_schema_heals_dual_active_routines_before_the_index(tmp_path):
    """A legacy database (no unique index yet) may hold a Member with two
    active Routines — pre-PR saves could interleave and commit both.
    ensure_schema must deactivate the extras (the newest survives, the same
    "most recent governs" rule as everywhere else) instead of aborting
    boot on the CREATE UNIQUE INDEX."""
    from sqlalchemy import select, text

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    clock = FakeClock()
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    # Roll back to the legacy schema: no one-active index.
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX uq_routines_one_active_per_member"))
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Luis", "telegram", "2")
    training = TrainingStore(engine, clock=clock)
    routines = RoutineStore(engine, clock=clock)
    await training.ensure_seeded()
    older = await routines.save_routine(
        member.id,
        gym.id,
        [WorkoutSpec(weekday=2, name="Piernas", exercises=[ExerciseSpec("squat")])],
    )
    # A second active row, planted the way a lost pre-index race would.
    async with routines._sessions() as db:
        newer = Routine(
            gym_id=gym.id, member_id=member.id, is_active=True, created_at=clock()
        )
        db.add(newer)
        await db.commit()

    await linking.ensure_schema()  # must not abort

    async with routines._sessions() as db:
        actives = list(
            await db.scalars(
                select(Routine).where(
                    Routine.member_id == member.id, Routine.is_active.is_(True)
                )
            )
        )
        assert [routine.id for routine in actives] == [newer.id]
        assert (await db.get(Routine, older.id)).is_active is False
        # And the index now guards the member.
        db.add(
            Routine(gym_id=gym.id, member_id=member.id, is_active=True, created_at=clock())
        )
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            await db.flush()
    await engine.dispose()


async def test_a_failing_notifier_never_turns_a_committed_save_into_a_500(env, caplog):
    """The save commits first; the notice is best-effort. A raising notifier
    (or a failed channel lookup) logs and still redirects — the acceptance
    rule is that the Member's plan changes even if the message is lost."""
    member = await env.add_member("Luis")
    old = await env.give_routine(member)
    env.notifier.fail = True

    response = await env.save_via_web(
        member.id, old.id, ("0", "Full body", "squat, 3, 8")
    )

    assert response.status == 302
    active = await env.routines.active_routine(member.id)
    assert active["coach_authored"] is True
    assert [w["name"] for w in active["workouts"]] == ["Full body"]
    assert "failed to notify member" in caplog.text


async def test_a_failing_channel_lookup_never_turns_a_committed_save_into_a_500(
    env, monkeypatch, caplog
):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    async def broken_channel(member_id):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(env.store, "member_channel", broken_channel)

    response = await env.save_via_web(
        member.id, old.id, ("0", "Full body", "squat, 3, 8")
    )

    assert response.status == 302
    active = await env.routines.active_routine(member.id)
    assert active["coach_authored"] is True
    assert "failed to notify member" in caplog.text


async def test_an_overlong_workout_name_is_rejected(env):
    """Workout.name is String(100): SQLite would hide an overflow, Postgres
    would 500 on a DataError — the editor validates first."""
    member = await env.add_member("Luis")
    old = await env.give_routine(member)
    long_name = "Piernas " + "x" * 100  # 108 chars

    response = await env.save_via_web(
        member.id, old.id, ("0", long_name, "squat, 4, 8-10")
    )

    assert response.status == 400
    text = await response.text()
    assert NAME_TOO_LONG_ERROR in text
    assert long_name in text  # the Coach's edit stays on the page
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id
    assert env.notifier.sent == []


async def test_an_overlong_reps_token_is_rejected(env):
    """WorkoutExercise.reps is String(40): same overflow guard."""
    member = await env.add_member("Luis")
    old = await env.give_routine(member)
    long_reps = "8-12 con una progresión muy larga y detallada"  # 46 chars

    response = await env.save_via_web(
        member.id, old.id, ("0", "Piernas", f"squat, 4, {long_reps}")
    )

    assert response.status == 400
    text = await response.text()
    assert REPS_TOO_LONG_ERROR in text
    assert long_reps in text
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id
    assert env.notifier.sent == []


async def test_out_of_range_sets_are_rejected(env):
    """Sets must be 1..99: 0 and negatives are nonsense, and an unbounded
    integer overflows at flush (OverflowError on SQLite, DataError on
    Postgres) — the editor validates with the same Spanish 400 pattern."""
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    for bad_sets in ("0", "-3", "9999999999999999999"):
        response = await env.save_via_web(
            member.id, old.id, ("0", "Piernas", f"squat, {bad_sets}, 8-10")
        )
        assert response.status == 400, bad_sets
        text = await response.text()
        assert SETS_RANGE_ERROR in text, bad_sets
        assert f"squat, {bad_sets}, 8-10" in text  # the edit stays on the page
        active = await env.routines.active_routine(member.id)
        assert active["routine_id"] == old.id
    assert env.notifier.sent == []


async def test_an_empty_form_is_rejected_with_the_submitted_base_kept(env):
    """No day-blocks at all: 400 with the empty-routine copy, nothing saved,
    and the re-rendered form still carries the submitted base stamp so a
    retry goes through the stale check like any other."""
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    data = [("base_routine_id", str(old.id))]
    response = await env.client.post(
        f"/members/{member.id}/routine",
        data=data,
        cookies=env.cookies(),
        allow_redirects=False,
    )

    assert response.status == 400
    text = await response.text()
    assert EMPTY_ROUTINE_ERROR in text
    assert f'name="base_routine_id" value="{old.id}"' in text
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id
    assert env.notifier.sent == []


# --- In-place saves (issue #128): the htmx branch of the editor POST ---

HTMX = {"HX-Request": "true"}


async def test_the_editor_page_loads_htmx_and_posts_in_place(env):
    member = await env.add_member("Marta")

    status, text = await env.get(f"/members/{member.id}/routine")

    assert status == 200
    assert "/static/htmx.min.js?v=" in text
    assert f'hx-post="/members/{member.id}/routine' in text
    assert 'hx-target="#editor-root"' in text
    # htmx preventDefaults the submit, which mutes the vanilla guard —
    # the double-submit protection must ride htmx's own machinery.
    assert 'hx-disabled-elt="find button[type=submit]"' in text
    assert 'id="editor-root"' in text


async def test_an_htmx_save_returns_the_editor_in_place_with_the_success_line(env):
    member = await env.add_member("Marta")
    await env.training.ensure_seeded()  # the Catalog the save validates against

    response = await env.save_via_web(
        member.id, None, ("0", "Full body", "squat, 3, 8"), headers=HTMX
    )

    assert response.status == 200
    assert "Location" not in response.headers
    text = await response.text()
    assert "<!DOCTYPE" not in text  # a fragment, not a document
    assert text.lstrip().startswith('<div id="editor-root"')
    assert "Rutina guardada." in text
    assert "Avisamos a Marta." in text
    assert env.notifier.sent  # the Telegram notice still went out


async def test_an_htmx_rejection_keeps_the_typed_work_and_answers_200(env):
    member = await env.add_member("Marta")

    response = await env.save_via_web(
        member.id, None, ("", "Huerfano", "squat, 3, 8"), headers=HTMX
    )

    assert response.status == 200
    text = await response.text()
    assert text.lstrip().startswith('<div id="editor-root"')
    assert UNDATED_BLOCK_ERROR in text
    assert "Huerfano" in text  # the typed work survives the refusal
    assert "Rutina guardada." not in text


async def test_an_htmx_stale_save_answers_200_with_the_fresh_version(env):
    member = await env.add_member("Marta")
    routine = await env.give_routine(member)
    await env.save_via_web(member.id, routine.id, ("0", "Nueva", "squat, 3, 8"))

    response = await env.save_via_web(
        member.id, routine.id, ("1", "Vieja", "squat, 3, 8"), headers=HTMX
    )

    assert response.status == 200
    text = await response.text()
    assert STALE_ERROR in text and 'id="editor-root"' in text
    assert "Vieja" in text  # kept work


async def test_the_success_line_follows_the_page_language(env):
    member = await env.add_member("Marta")
    await env.training.ensure_seeded()  # the Catalog the save validates against

    response = await env.save_via_web(
        member.id,
        None,
        ("0", "Full body", "squat, 3, 8"),
        headers={**HTMX, "Accept-Language": "en"},
    )

    text = await response.text()
    assert "Routine saved." in text and "We told Marta." in text


async def test_an_htmx_save_on_a_dead_session_redirects_the_whole_page(env):
    member = await env.add_member("Marta")

    response = await env.client.post(
        f"/members/{member.id}/routine",
        data=[("base_routine_id", "")],
        headers=HTMX,
        allow_redirects=False,
    )

    assert response.headers.get("HX-Redirect") == "/"


async def test_without_the_header_the_save_still_redirects(env):
    member = await env.add_member("Marta")
    await env.training.ensure_seeded()  # the Catalog the save validates against

    response = await env.save_via_web(member.id, None, ("0", "Full body", "squat, 3, 8"))

    assert response.status == 302
    assert response.headers["Location"] == f"/members/{member.id}?view=table"


# --- Regression: the member page's Edit link target must answer 200 ---


async def test_member_page_edit_link_target_answers_200(env):
    """The React member page links Edit at /members/{id}/routine (its RTL
    test asserts the href); a deleted or broken route here is the silent
    404-Edit-link regression class from #151's review."""
    member = await env.add_member("Luis")
    await env.give_routine(member)

    status, _ = await env.get("/members/{}/routine".format(member.id))
    assert status == 200
