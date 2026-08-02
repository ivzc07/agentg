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
    NAME_TOO_LONG_ERROR,
    REPS_TOO_LONG_ERROR,
    SESSION_COOKIE,
    SETS_RANGE_ERROR,
    STALE_ERROR,
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
async def env(tmp_path, stub_spa_dist):
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
        spa_dist=stub_spa_dist,
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
        """PUT the editor's JSON API (#154); ``days`` keep the old helper's
        (weekday, workout name, "exercise, sets, reps" lines) triples."""

        def parse_line(line: str) -> dict:
            parts = [p.strip() for p in line.split(",")]
            exercise: dict = {"exercise": parts[0]}
            if len(parts) > 1 and parts[1]:
                try:
                    exercise["sets"] = int(parts[1])
                except ValueError:
                    exercise["sets"] = parts[1]  # deliberately bad input
            if len(parts) > 2 and parts[2]:
                exercise["reps"] = parts[2]
            return exercise

        workouts = [
            {
                "weekday": int(weekday),
                "name": name,
                "exercises": [
                    parse_line(line)
                    for line in exercises.splitlines()
                    if line.strip()
                ],
            }
            for weekday, name, exercises in days
        ]
        return await self.client.put(
            f"/api/members/{member_id}/routine",
            json={"base_routine_id": base_routine_id, "workouts": workouts},
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



async def test_a_web_save_replaces_the_routine_stamps_it_and_notifies_the_member(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)
    # A running Session is never disturbed by a web save.
    opened = await env.training.open_session(member.id, env.gym.id)

    response = await env.save_via_web(
        member.id, old.id, ("0", "Full body", "squat, 3, 8\nbench press, 3, 10")
    )

    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True and body["notified"] is True
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

    # After the first save the chip reads named Coach-authored, permanently
    # (the React editor and member page render it from these fields).
    import json

    _, editor = await env.get(f"/api/members/{member.id}/routine")
    editor_data = json.loads(editor)
    assert editor_data["coach_authored"] is True
    assert editor_data["routine_author"] == "Coach Ana"
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

    import json

    status, editor = await env.get(f"/api/members/{member.id}/routine")
    assert status == 200
    before = json.loads(editor)
    # The React editor shows the preset chip + consequence from this field.
    assert before["routine_preset_name"] == "Beginner"

    response = await env.save_via_web(
        member.id, old["routine_id"], ("1", "Forked day", "bench press, 3, 8")
    )

    assert response.status == 200
    active = await env.routines.active_routine(member.id)
    assert active["preset_id"] is None
    assert active["created_by_name"] == "Coach Ana"
    _, editor = await env.get(f"/api/members/{member.id}/routine")
    after = json.loads(editor)
    assert after["routine_preset_name"] is None  # silently forked
    assert after["routine_author"] == "Coach Ana"


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
    body = await response.json()
    assert STALE_ERROR in body["error"]
    # The fresh version rides along for the read-only block the React
    # editor shows (its RTL stale test covers keeping the typed work).
    assert body["fresh_routine"][0]["name"] == "Nuevo plan"
    # Nothing was written.
    active = await env.routines.active_routine(member.id)
    assert [w["name"] for w in active["workouts"]] == ["Nuevo plan"]
    assert env.notifier.sent == []
    # The stamp re-arms against the fresh Routine, so saving again applies
    # the kept edits on top of it, knowingly.
    assert body["fresh_routine_id"] == active["routine_id"]


async def test_unknown_exercises_are_rejected_with_nothing_saved(env):
    member = await env.add_member("Luis")
    old = await env.give_routine(member)

    response = await env.save_via_web(
        member.id, old.id, ("0", "Full body", "sentadilla mágica, 3, 8")
    )

    assert response.status == 400
    body = await response.json()
    # Spanish, coach-facing — never the raw English agent-tool message.
    assert "no están en el catálogo" in body["error"]
    assert "list_exercises" not in body["error"]
    assert "sentadilla mágica" in body["error"]
    active = await env.routines.active_routine(member.id)
    assert active["routine_id"] == old.id
    assert env.notifier.sent == []


async def test_the_editor_is_coach_only_and_member_scoped(env):
    member = await env.add_member("Luis")
    await env.give_routine(member)

    # Anonymous: the friendly bounce on the shell URL, 401 on the API.
    assert BOUNCE_MARKER in await (
        await env.client.get(f"/members/{member.id}/routine")
    ).text()
    api = await env.client.get(f"/api/members/{member.id}/routine")
    assert api.status == 401

    # Unknown or another Gym's Member: the shared bare 404 on the API (the
    # shell serves for any path; the React editor renders the 404 state).
    status, _ = await env.get("/api/members/99999/routine")
    assert status == 404
    other_gym = await env.linking.create_gym("Other Gym")
    outsider = await env.linking.link_member(other_gym.id, "Mara", "telegram", "7")
    status, _ = await env.get(f"/api/members/{outsider.id}/routine")
    assert status == 404






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


async def test_the_editor_put_is_scoped_like_the_get(env):
    """PUTting another Gym's Member or a coach-flagged Member hits the same
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

    assert response.status == 200
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
    body = await response.json()
    assert NAME_TOO_LONG_ERROR in body["error"]
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
    body = await response.json()
    assert REPS_TOO_LONG_ERROR in body["error"]
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
        body = await response.json()
        assert SETS_RANGE_ERROR in body["error"], bad_sets
        active = await env.routines.active_routine(member.id)
        assert active["routine_id"] == old.id
    assert env.notifier.sent == []



# --- Regression: the member page's Edit link target must answer 200 ---


async def test_member_page_edit_link_target_answers_200(env):
    """The React member page links Edit at /members/{id}/routine (its RTL
    test asserts the href); a deleted or broken route here is the silent
    404-Edit-link regression class from #151's review."""
    member = await env.add_member("Luis")
    await env.give_routine(member)

    status, _ = await env.get("/members/{}/routine".format(member.id))
    assert status == 200
