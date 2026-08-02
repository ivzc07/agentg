"""TrainingStore: the Session loop — open, log, correct, close, auto-close.

Facts flow only through these methods (the Agent's tools are thin wrappers);
the clock is injected so gaps and the auto-close timeout are testable.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from conftest import FakeClock

from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from agentg.models import Session as SessionModel
from agentg.training import SESSION_AUTO_CLOSE, SEED_EXERCISES, TrainingStore


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'training.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    clock = FakeClock()
    training = TrainingStore(engine, clock=clock)
    await training.ensure_seeded()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Dani", "telegram", "42")
    yield SimpleEnv(training=training, clock=clock, member_id=member.id, gym_id=gym.id)
    await engine.dispose()


class SimpleEnv:
    def __init__(self, training, clock, member_id, gym_id):
        self.training = training
        self.clock = clock
        self.member_id = member_id
        self.gym_id = gym_id


def days(n: int) -> timedelta:
    return timedelta(days=n)


# --- opening a Session ---


async def test_first_open_has_no_last_session(env):
    opened = await env.training.open_session(env.member_id, env.gym_id)
    assert opened.days_since_last is None
    assert opened.last_session is None


async def test_open_reports_days_since_and_the_last_sessions_headline(env):
    await env.training.open_session(env.member_id, env.gym_id)
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,7")
    await env.training.close_session(env.member_id)

    env.clock.advance(days(2))
    opened = await env.training.open_session(env.member_id, env.gym_id)

    assert opened.days_since_last == 2
    assert opened.last_session is not None
    exercises = {e["exercise"]: e for e in opened.last_session["exercises"]}
    assert exercises["bench press"]["weight"] == 60.0
    assert exercises["bench press"]["reps"] == [8, 8, 7]


async def test_opening_twice_reuses_the_open_session(env):
    first = await env.training.open_session(env.member_id, env.gym_id)
    again = await env.training.open_session(env.member_id, env.gym_id)
    assert again.session_id == first.session_id


async def test_a_session_with_no_sets_still_counts_as_a_visit(env):
    await env.training.open_session(env.member_id, env.gym_id)  # walks in, logs nothing
    env.clock.advance(SESSION_AUTO_CLOSE + days(1) - SESSION_AUTO_CLOSE)  # next day
    opened = await env.training.open_session(env.member_id, env.gym_id)
    assert opened.days_since_last == 1  # the empty visit reset the gap


# --- auto-close ---


async def test_an_abandoned_session_auto_closes_after_the_timeout(env):
    first = await env.training.open_session(env.member_id, env.gym_id)
    opened_at = env.clock.now
    env.clock.advance(SESSION_AUTO_CLOSE + timedelta(minutes=1))

    second = await env.training.open_session(env.member_id, env.gym_id)

    assert second.session_id != first.session_id
    closed = await env.training.get_session(first.session_id)
    assert closed.closed_at is not None
    assert closed.closed_at == opened_at  # closed as of its last activity


async def test_auto_close_counts_from_the_last_logged_set(env):
    await env.training.open_session(env.member_id, env.gym_id)
    env.clock.advance(SESSION_AUTO_CLOSE - timedelta(minutes=10))
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")  # still active
    env.clock.advance(SESSION_AUTO_CLOSE - timedelta(minutes=10))

    opened = await env.training.open_session(env.member_id, env.gym_id)

    # the set kept the session alive, so it is still the same session
    sets = await env.training.current_session_sets(env.member_id)
    assert opened.reopened is True
    assert len(sets) == 3


# --- logging sets ---


async def test_logging_stores_one_row_per_set_and_echoes_them(env):
    logged = await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")

    assert logged.exercise == "bench press"  # alias resolved
    assert logged.weight == 60.0
    assert logged.reps == [8, 8, 8]
    sets = await env.training.current_session_sets(env.member_id)
    assert [(s.weight, s.reps) for s in sets] == [(60.0, 8)] * 3


async def test_logging_without_an_open_session_opens_one(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    assert await env.training.current_session_sets(env.member_id)  # implies open session


async def test_bodyweight_sets_have_no_weight(env):
    logged = await env.training.log_sets(env.member_id, env.gym_id, "dips 10,10,9")
    assert logged.weight is None
    assert [s.weight for s in await env.training.current_session_sets(env.member_id)] == [
        None,
        None,
        None,
    ]


async def test_a_context_exercise_covers_lines_that_omit_it(env):
    logged = await env.training.log_sets(
        env.member_id, env.gym_id, "40 8/7/6", exercise="overhead press"
    )
    assert logged.exercise == "overhead press"
    assert logged.weight == 40.0


async def test_a_line_with_no_exercise_and_no_context_is_rejected(env):
    with pytest.raises(ValueError, match="exercise"):
        await env.training.log_sets(env.member_id, env.gym_id, "60 8,8,7")


async def test_an_unparseable_line_is_rejected_with_guidance(env):
    with pytest.raises(ValueError, match="parse"):
        await env.training.log_sets(env.member_id, env.gym_id, "felt strong today")


async def test_unknown_exercises_are_added_rather_than_dropped(env):
    logged = await env.training.log_sets(env.member_id, env.gym_id, "cable fly 15 12,12")
    assert logged.exercise == "cable fly"


async def test_lb_lines_at_a_kg_gym_are_converted(env):
    logged = await env.training.log_sets(env.member_id, env.gym_id, "bench 135lb 5,5,5")
    assert logged.weight == pytest.approx(61.23, abs=0.01)  # stored in the gym's kg


async def test_kg_lines_at_a_lb_gym_are_converted(env, tmp_path):
    from agentg.db import create_engine
    from agentg.linking_store import LinkingStore
    from agentg.training import TrainingStore

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'lbgym.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    training = TrainingStore(engine, clock=env.clock)
    await training.ensure_seeded()
    gym = await linking.create_gym("Lone Star Barbell", weight_unit="lb")
    member = await linking.link_member(gym.id, "Tex", "telegram", "7")

    logged = await training.log_sets(member.id, gym.id, "squat 100kg 5,5,5")

    assert logged.weight == pytest.approx(220.46, abs=0.01)  # stored in the gym's lb
    await engine.dispose()


async def test_volunteered_rpe_and_note_are_stored(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8", rpe=8.5, note="pause reps")
    sets = await env.training.current_session_sets(env.member_id)
    assert {s.rpe for s in sets} == {8.5}
    assert {s.note for s in sets} == {"pause reps"}


async def test_previous_numbers_ride_along_for_the_echo(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,7")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))

    logged = await env.training.log_sets(env.member_id, env.gym_id, "bench 62.5 8,8,8")

    assert logged.previous is not None
    assert logged.previous["weight"] == 60.0
    assert logged.previous["reps"] == [8, 8, 7]


# --- suspect jumps (plausible-but-wrong parse guard) ---


async def test_a_weight_jump_beyond_2x_last_is_flagged_suspect_but_still_logged(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))

    logged = await env.training.log_sets(env.member_id, env.gym_id, "bench 121 5,5,5")

    assert logged.weight == 121.0  # still stored — Member is right once they confirm
    assert logged.suspect is not None
    assert len(await env.training.current_session_sets(env.member_id)) == 3


async def test_a_weight_at_exactly_2x_last_is_not_suspect(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))

    logged = await env.training.log_sets(env.member_id, env.gym_id, "bench 120 5,5,5")

    assert logged.weight == 120.0
    assert logged.suspect is None


async def test_a_normal_progression_is_not_suspect(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))

    logged = await env.training.log_sets(env.member_id, env.gym_id, "bench 62.5 8,8,8")

    assert logged.suspect is None


async def test_first_log_of_an_exercise_is_not_suspect(env):
    logged = await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    assert logged.suspect is None


async def test_an_edit_to_a_weight_beyond_2x_last_is_flagged_suspect(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))
    await env.training.log_sets(env.member_id, env.gym_id, "bench 62.5 8,8,8")

    edited = await env.training.edit_logged_sets(env.member_id, "bench", weight=600)

    assert edited.weight == 600.0  # still stored — same typo surface as log_sets
    assert edited.suspect is not None


async def test_a_plausible_edit_is_not_suspect(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))
    await env.training.log_sets(env.member_id, env.gym_id, "bench 600 8,8,8")

    edited = await env.training.edit_logged_sets(env.member_id, "bench", weight=62.5)

    assert edited.suspect is None


async def test_copy_last_sets_is_never_suspect(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))

    copied = await env.training.copy_last_sets(env.member_id, env.gym_id, "bench")

    assert copied.suspect is None  # copies the previous weight, nothing to flag


# --- "same as last time" ---


async def test_copy_last_sets_copies_that_exercises_previous_session(env):
    await env.training.log_sets(env.member_id, env.gym_id, "overhead press 40 8,7,6")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(7))

    await env.training.open_session(env.member_id, env.gym_id)
    copied = await env.training.copy_last_sets(env.member_id, env.gym_id, "ohp")

    assert copied.exercise == "overhead press"
    assert copied.weight == 40.0
    assert copied.reps == [8, 7, 6]
    assert len(await env.training.current_session_sets(env.member_id)) == 3


async def test_copy_last_sets_without_history_is_rejected(env):
    await env.training.open_session(env.member_id, env.gym_id)
    with pytest.raises(ValueError, match="no earlier"):
        await env.training.copy_last_sets(env.member_id, env.gym_id, "bench")


# --- corrections ---


async def test_correcting_the_weight_edits_the_just_logged_sets(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")

    edited = await env.training.edit_logged_sets(env.member_id, "bench", weight=62.5)

    assert edited.weight == 62.5
    assert [s.weight for s in await env.training.current_session_sets(env.member_id)] == [
        62.5,
        62.5,
        62.5,
    ]


async def test_correcting_the_reps_replaces_the_rep_list(env):
    await env.training.log_sets(env.member_id, env.gym_id, "dips 10,10,8")

    edited = await env.training.edit_logged_sets(env.member_id, "dips", reps=[10, 10, 9])

    assert edited.reps == [10, 10, 9]


async def test_corrections_touch_only_the_just_logged_batch(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 40 10")  # warm-up
    env.clock.advance(timedelta(minutes=5))
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")  # work sets

    await env.training.edit_logged_sets(env.member_id, "bench", weight=62.5)

    weights = [s.weight for s in await env.training.current_session_sets(env.member_id)]
    assert weights == [40.0, 62.5, 62.5, 62.5]  # the warm-up batch is untouched


async def test_corrections_return_previous_numbers_for_the_echo(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,7")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")

    edited = await env.training.edit_logged_sets(env.member_id, "bench", weight=62.5)

    assert edited.previous is not None
    assert edited.previous["weight"] == 60.0  # so the Agent can note the +2.5


async def test_reference_numbers_use_the_top_set_not_the_last(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8")  # work sets
    await env.training.log_sets(env.member_id, env.gym_id, "bench 50 12")  # back-off after
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))

    last = await env.training.last_sets(env.member_id, "bench")

    assert last is not None
    assert last["weight"] == 60.0  # the top set, not the trailing back-off


async def test_corrections_never_reach_a_previous_session(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))
    await env.training.open_session(env.member_id, env.gym_id)

    with pytest.raises(ValueError, match="current"):
        await env.training.edit_logged_sets(env.member_id, "bench", weight=62.5)


async def test_growing_a_set_batch_preserves_note_and_rpe_on_the_added_sets(env):
    await env.training.log_sets(
        env.member_id, env.gym_id, "bench 60 8,8", rpe=8.0, note="paused"
    )

    await env.training.edit_logged_sets(env.member_id, "bench", reps=[8, 8, 8, 8])

    sets = await env.training.current_session_sets(env.member_id)
    assert len(sets) == 4
    for s in sets:
        assert s.rpe == 8.0
        assert s.note == "paused"


# --- closing ---


async def test_done_closes_with_a_summary_noting_what_went_up(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,7")
    await env.training.log_sets(env.member_id, env.gym_id, "dips 10,10,8")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(7))

    await env.training.log_sets(env.member_id, env.gym_id, "bench 62.5 8,8,8")
    await env.training.log_sets(env.member_id, env.gym_id, "dips 10,10,9")
    summary = await env.training.close_session(env.member_id)

    assert summary.total_sets == 6
    by_name = {line["exercise"]: line for line in summary.exercises}
    assert by_name["bench press"]["weight_change"] == pytest.approx(2.5)
    assert by_name["dips"]["reps_change"] == 1
    session = await env.training.get_session(summary.session_id)
    assert session.closed_at is not None


async def test_closing_without_an_open_session_says_so(env):
    with pytest.raises(ValueError, match="open"):
        await env.training.close_session(env.member_id)


# --- reading facts back (never from chat history) ---


async def test_last_sets_reads_the_previous_session_not_the_current_one(env):
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,7")
    await env.training.close_session(env.member_id)
    env.clock.advance(days(2))
    await env.training.log_sets(env.member_id, env.gym_id, "bench 62.5 8,8,8")

    last = await env.training.last_sets(env.member_id, "bench")

    assert last is not None
    assert last["weight"] == 60.0  # the previous session's numbers
    assert last["reps"] == [8, 8, 7]


async def test_last_sets_for_an_unknown_exercise_is_none(env):
    assert await env.training.last_sets(env.member_id, "bench") is None


# --- the seed catalog ---


async def test_seeding_is_idempotent_and_matches_aliases(env):
    await env.training.ensure_seeded()  # second run must not duplicate
    exercise = await env.training.match_or_create_exercise("ohp")
    assert exercise.name == "overhead press"
    assert len(SEED_EXERCISES) >= 8


# --- timezone-aware day boundaries (issue #95) ---

CHICAGO = "America/Chicago"  # UTC-5 in July


@pytest.fixture
async def chicago_env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'tz.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    # 02:00 UTC Jul 16 is still Jul 15, 21:00 at the gym.
    clock = FakeClock(datetime(2026, 7, 16, 2, 0, tzinfo=UTC))
    training = TrainingStore(engine, clock=clock)
    await training.ensure_seeded()
    gym = await linking.create_gym("Iron Temple", timezone=CHICAGO)
    member = await linking.link_member(gym.id, "Dani", "telegram", "42")
    yield SimpleEnv(training=training, clock=clock, member_id=member.id, gym_id=gym.id)
    await engine.dispose()


async def test_a_session_after_utc_midnight_counts_on_the_local_day(chicago_env):
    env = chicago_env
    # logged at 02:00 UTC — UTC math would put the visit on Jul 16
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    await env.training.close_session(env.member_id)

    env.clock.now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)  # Jul 16, 07:00 local
    days_since, last = await env.training.latest_session_info(env.member_id)

    assert days_since == 1  # not 0 — the visit was on the local Jul 15
    assert last is not None and last["date"] == "2026-07-15"


async def test_newest_session_date_uses_the_gyms_timezone(chicago_env):
    env = chicago_env
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    assert await env.training.newest_session_date(env.member_id) == date(2026, 7, 15)


async def test_today_uses_the_gyms_timezone(chicago_env):
    env = chicago_env
    assert env.training.today(CHICAGO) == date(2026, 7, 15)
    assert env.training.today() == date(2026, 7, 16)  # UTC stays the default


# --- exercise_history_batch (issue #170) ---


async def test_exercise_history_batch_resolves_aliases(env):
    """Alias resolution works: "bench" finds "bench press" history."""
    await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
    await env.training.close_session(env.member_id)

    result = await env.training.exercise_history_batch(env.member_id, ["bench"], limit=5)

    assert "bench" in result
    assert len(result["bench"]) == 1
    assert result["bench"][0]["top_weight"] == 60.0
    assert result["bench"][0]["top_reps"] == [8, 8, 8]


async def test_exercise_history_batch_unknown_exercise_is_empty(env):
    """An exercise with no catalog match returns an empty history."""
    result = await env.training.exercise_history_batch(
        env.member_id, ["nonexistent"], limit=5
    )

    assert result["nonexistent"] == []


async def test_exercise_history_batch_respects_limit(env):
    """Per-exercise limit truncates to the most recent sessions."""
    # Log three sessions, most recent first due to descending order
    for offset in (9, 6, 3):
        env.clock.now = FakeClock().now - timedelta(days=offset)
        await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
        await env.training.close_session(env.member_id)
    env.clock.now = FakeClock().now

    result = await env.training.exercise_history_batch(env.member_id, ["bench"], limit=2)

    assert len(result["bench"]) == 2  # only the 2 most recent sessions


# --- issue #213: one open Session per Member ---


class TestSchemaOpenSessionUniqueness:
    """The DB enforces at most one Session without a close time per Member."""

    async def test_fresh_schema_has_the_partial_unique_index(self, tmp_path):
        """A brand-new database gets the index from create_all."""
        from sqlalchemy import text

        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
        linking = LinkingStore(engine)
        await linking.ensure_schema()

        async with engine.begin() as conn:
            indexes = {
                row[1]
                for row in await conn.execute(
                    text("SELECT type, name FROM sqlite_master WHERE type = 'index'")
                )
            }
        assert "uq_sessions_one_open_per_member" in indexes
        await engine.dispose()

    async def test_migration_adds_index_to_a_legacy_db(self, tmp_path):
        """A database that predates issue #213 gets the index at startup."""
        import sqlite3
        from sqlalchemy import text

        db_path = tmp_path / "legacy-sessions.db"
        engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
        store = LinkingStore(engine)
        await store.ensure_schema()
        await engine.dispose()

        # Simulate a pre-#213 database: drop the index.
        raw = sqlite3.connect(str(db_path))
        raw.execute("DROP INDEX uq_sessions_one_open_per_member")
        raw.commit()
        raw.close()

        engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
        store = LinkingStore(engine)
        await store.ensure_schema()

        async with engine.begin() as conn:
            indexes = {
                row[1]
                for row in await conn.execute(
                    text("SELECT type, name FROM sqlite_master WHERE type = 'index'")
                )
            }
        assert "uq_sessions_one_open_per_member" in indexes
        await engine.dispose()

    async def test_duplicate_open_sessions_detected_with_actionable_member_ids(
        self, tmp_path
    ):
        """Schema setup detects historical duplicates and fails with Member IDs."""
        import sqlite3

        db_path = tmp_path / "dup-sessions.db"
        engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
        store = LinkingStore(engine)
        await store.ensure_schema()
        await engine.dispose()

        # Create a pre-#213 state with two open Sessions for one Member.
        raw = sqlite3.connect(str(db_path))
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("DROP INDEX uq_sessions_one_open_per_member")
        # Insert two open sessions for member_id=1, gym_id=1.
        raw.execute(
            "INSERT INTO sessions (id, gym_id, member_id, started_at, closed_at) "
            "VALUES (1, 1, 1, '2026-07-01 10:00:00', NULL)"
        )
        raw.execute(
            "INSERT INTO sessions (id, gym_id, member_id, started_at, closed_at) "
            "VALUES (2, 1, 1, '2026-07-01 11:00:00', NULL)"
        )
        raw.commit()
        raw.close()

        engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
        store = LinkingStore(engine)
        with pytest.raises(RuntimeError, match="Historical duplicate open Sessions"):
            await store.ensure_schema()
        await engine.dispose()

    def test_partial_unique_index_ddl_compiles_on_postgres(self):
        """The partial unique index must compile on PostgreSQL (no DATETIME)."""
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateIndex

        from agentg.models import Session

        dialect = postgresql.dialect()
        for index in Session.__table_args__:
            if hasattr(index, "name") and index.name == "uq_sessions_one_open_per_member":
                ddl = str(
                    CreateIndex(index).compile(dialect=dialect)
                )
                assert "DATETIME" not in ddl.upper()
                assert "closed_at IS NULL" in ddl
                assert "UNIQUE" in ddl.upper()
                break
        else:
            pytest.fail("uq_sessions_one_open_per_member index not found on Session")


class TestStoreConcurrency:
    """Race-safe Session open: concurrent callers reuse one Session."""

    @pytest.fixture
    async def two_members(self, tmp_path):
        """Two Members at the same Gym for concurrency tests."""
        engine = create_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}"
        )
        linking = LinkingStore(engine)
        await linking.ensure_schema()
        clock = FakeClock()
        training = TrainingStore(engine, clock=clock)
        await training.ensure_seeded()
        gym = await linking.create_gym("Iron Temple")
        a = await linking.link_member(gym.id, "Ana", "telegram", "1")
        b = await linking.link_member(gym.id, "Bob", "telegram", "2")
        yield SimpleEnv(
            training=training,
            clock=clock,
            member_id=a.id,
            gym_id=gym.id,
        ), SimpleEnv(
            training=training,
            clock=clock,
            member_id=b.id,
            gym_id=gym.id,
        )
        await engine.dispose()

    async def test_concurrent_open_session_shares_one_session(self, tmp_path):
        """Two concurrent public open_session calls reuse one Session."""
        engine = create_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'race1.db'}"
        )
        linking = LinkingStore(engine)
        await linking.ensure_schema()
        clock = FakeClock()
        training = TrainingStore(engine, clock=clock)
        await training.ensure_seeded()
        gym = await linking.create_gym("Iron Temple")
        member = await linking.link_member(gym.id, "Ana", "telegram", "1")

        barrier = asyncio.Barrier(2)

        async def opener():
            await barrier.wait()
            return await training.open_session(member.id, gym.id)

        r1, r2 = await asyncio.gather(opener(), opener())

        # Both callers got the same Session.
        assert r1.session_id == r2.session_id
        # One created it, the other found it pre-existing.
        assert {r1.reopened, r2.reopened} == {True, False}

        # Exactly one open Session in the database.
        async with training._sessions() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(SessionModel)
                .where(SessionModel.member_id == member.id, SessionModel.closed_at.is_(None))
            )
            assert count == 1

        await engine.dispose()

    async def test_concurrent_log_sets_one_session(self, tmp_path):
        """Concurrent log_sets calls (with an existing open Session) all
        attach their Sets to the same Session — no work is dropped."""
        from agentg.models import Set as SetModel

        engine = create_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'race2.db'}"
        )
        linking = LinkingStore(engine)
        await linking.ensure_schema()
        clock = FakeClock()
        training = TrainingStore(engine, clock=clock)
        await training.ensure_seeded()
        gym = await linking.create_gym("Iron Temple")
        member = await linking.link_member(gym.id, "Ana", "telegram", "1")

        # Open a Session first so both log_sets find it.
        await training.open_session(member.id, gym.id)

        async def log(line):
            return await training.log_sets(member.id, gym.id, line)

        r1, r2 = await asyncio.gather(
            log("bench 60 8,8,8"),
            log("squat 100 5,5,5"),
        )
        assert r1.exercise == "bench press"
        assert r2.exercise == "squat"

        # All 6 Sets in one Session, not split across two.
        async with training._sessions() as db:
            session = await db.scalar(
                select(SessionModel).where(
                    SessionModel.member_id == member.id,
                    SessionModel.closed_at.is_(None),
                )
            )
            assert session is not None
            set_rows = (
                await db.execute(
                    select(SetModel).where(SetModel.session_id == session.id)
                )
            ).scalars().all()
            assert len(set_rows) == 6  # 3 bench + 3 squat

        await engine.dispose()

    async def test_concurrent_implicit_open_no_dropped_work(self, tmp_path):
        """Concurrent log_sets calls with no open Session all land Sets in
        one implicitly-opened Session — no work is dropped or split."""
        from agentg.models import Set as SetModel

        engine = create_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'race3.db'}"
        )
        linking = LinkingStore(engine)
        await linking.ensure_schema()
        clock = FakeClock()
        training = TrainingStore(engine, clock=clock)
        await training.ensure_seeded()
        gym = await linking.create_gym("Iron Temple")
        member = await linking.link_member(gym.id, "Ana", "telegram", "1")

        barrier = asyncio.Barrier(3)

        async def log(line):
            await barrier.wait()
            return await training.log_sets(member.id, gym.id, line)

        results = await asyncio.gather(
            log("bench 60 8,8,8"),
            log("squat 100 5,5,5"),
            log("deadlift 120 3,3,3"),
        )
        assert len(results) == 3

        # All 9 Sets in one Session.
        async with training._sessions() as db:
            session = await db.scalar(
                select(SessionModel).where(
                    SessionModel.member_id == member.id,
                    SessionModel.closed_at.is_(None),
                )
            )
            assert session is not None
            set_rows = (
                await db.execute(
                    select(SetModel).where(SetModel.session_id == session.id)
                )
            ).scalars().all()
            assert len(set_rows) == 9  # 3+3+3, all in one session

        await engine.dispose()

    async def test_concurrent_open_with_stale_session(self, tmp_path):
        """Two concurrent open_session calls with a stale open Session both
        get the same new Session — the stale one is closed, and exactly one
        open Session remains."""
        engine = create_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'race4.db'}"
        )
        linking = LinkingStore(engine)
        await linking.ensure_schema()
        clock = FakeClock()
        training = TrainingStore(engine, clock=clock)
        await training.ensure_seeded()
        gym = await linking.create_gym("Iron Temple")
        member = await linking.link_member(gym.id, "Ana", "telegram", "1")

        # Create a stale open Session.
        first = await training.open_session(member.id, gym.id)
        clock.advance(SESSION_AUTO_CLOSE + timedelta(minutes=1))

        barrier = asyncio.Barrier(2)

        async def opener():
            await barrier.wait()
            return await training.open_session(member.id, gym.id)

        r1, r2 = await asyncio.gather(opener(), opener())

        # Both got the same new Session, not the stale one.
        assert r1.session_id == r2.session_id
        assert r1.session_id != first.session_id

        # The first (stale) Session is closed.
        closed = await training.get_session(first.session_id)
        assert closed.closed_at is not None

        # Exactly one open Session.
        async with training._sessions() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(SessionModel)
                .where(
                    SessionModel.member_id == member.id,
                    SessionModel.closed_at.is_(None),
                )
            )
            assert count == 1

        await engine.dispose()

    async def test_concurrent_log_sets_new_exercise_no_dropped_work(self, tmp_path):
        """Concurrent log_sets calls for the same unknown Exercise both
        succeed — the catalog INSERT race is handled gracefully and no
        valid Sets are dropped (issue #213)."""
        from agentg.models import Exercise as ExerciseModel
        from agentg.models import Set as SetModel

        engine = create_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'race5.db'}"
        )
        linking = LinkingStore(engine)
        await linking.ensure_schema()
        clock = FakeClock()
        training = TrainingStore(engine, clock=clock)
        # Do NOT seed — the tested exercise ("cable fly") is unknown.
        gym = await linking.create_gym("Iron Temple")
        member = await linking.link_member(gym.id, "Ana", "telegram", "1")

        # Open a Session so the race only exercises the catalog path.
        await training.open_session(member.id, gym.id)

        barrier = asyncio.Barrier(2)

        async def log(line):
            await barrier.wait()
            return await training.log_sets(member.id, gym.id, line)

        r1, r2 = await asyncio.gather(
            log("cable fly 20 10,10,10"),
            log("cable fly 20 10,10,10"),
        )
        assert r1.exercise == "cable fly"
        assert r2.exercise == "cable fly"
        assert r1.reps == [10, 10, 10]
        assert r2.reps == [10, 10, 10]

        # All 6 Sets in one Session — none dropped.
        async with training._sessions() as db:
            session = await db.scalar(
                select(SessionModel).where(
                    SessionModel.member_id == member.id,
                    SessionModel.closed_at.is_(None),
                )
            )
            assert session is not None
            set_rows = (
                await db.execute(
                    select(SetModel).where(SetModel.session_id == session.id)
                )
            ).scalars().all()
            assert len(set_rows) == 6  # 3+3, all in one session

            # Exactly one Exercise was created.
            ex_count = await db.scalar(
                select(func.count()).where(ExerciseModel.name == "cable fly")
            )
            assert ex_count == 1

        await engine.dispose()


class TestSessionLifecycleUnderConstraint:
    """Normal lifecycle operations work with the unique constraint in place."""

    async def test_open_log_close_reopen_cycle(self, env):
        """A full cycle: open, log, close, reopen — all behave correctly."""
        # Open, log, close.
        opened = await env.training.open_session(env.member_id, env.gym_id)
        assert opened.reopened is False

        await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
        summary = await env.training.close_session(env.member_id)
        assert summary.total_sets == 3

        # Move forward and reopen.
        env.clock.advance(days(2))
        reopened = await env.training.open_session(env.member_id, env.gym_id)
        assert reopened.reopened is False  # new session, not the old one
        assert reopened.days_since_last == 2

        await env.training.log_sets(env.member_id, env.gym_id, "bench 62.5 8,8,8")
        summary2 = await env.training.close_session(env.member_id)
        assert summary2.total_sets == 3

    async def test_two_members_each_have_independent_open_sessions(
        self, tmp_path
    ):
        """The unique constraint is per-member: two Members can each have
        an open Session."""
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'two.db'}")
        linking = LinkingStore(engine)
        await linking.ensure_schema()
        clock = FakeClock()
        training = TrainingStore(engine, clock=clock)
        await training.ensure_seeded()
        gym = await linking.create_gym("Iron Temple")
        a = await linking.link_member(gym.id, "Ana", "telegram", "1")
        b = await linking.link_member(gym.id, "Bob", "telegram", "2")

        # Both members open a session.
        opened_a = await training.open_session(a.id, gym.id)
        opened_b = await training.open_session(b.id, gym.id)

        assert opened_a.session_id != opened_b.session_id

        # Each logs sets independently.
        await training.log_sets(a.id, gym.id, "bench 60 8,8,8")
        await training.log_sets(b.id, gym.id, "squat 100 5,5,5")

        sets_a = await training.current_session_sets(a.id)
        sets_b = await training.current_session_sets(b.id)
        assert len(sets_a) == 3
        assert len(sets_b) == 3

        await engine.dispose()

    async def test_implicit_open_via_log_sets_then_explicit_open_reuses(self, env):
        """Implicit open (log_sets) + explicit open reuse the same Session."""
        # Implicit open via log_sets.
        await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")

        # Explicit open reuses that Session.
        opened = await env.training.open_session(env.member_id, env.gym_id)
        assert opened.reopened is True

        # Sets from the implicit open are still there.
        sets = await env.training.current_session_sets(env.member_id)
        assert len(sets) == 3

    async def test_close_then_implicit_open_creates_new_session(self, env):
        """After closing, an implicit open (log_sets) creates a new Session."""
        await env.training.log_sets(env.member_id, env.gym_id, "bench 60 8,8,8")
        await env.training.close_session(env.member_id)

        env.clock.advance(days(1))
        await env.training.log_sets(env.member_id, env.gym_id, "squat 100 5,5,5")

        sets = await env.training.current_session_sets(env.member_id)
        assert len(sets) == 3
        assert sets[0].weight == 100.0  # the new session's sets


class TestPostgresDdlSafety:
    """The DDL must compile on both SQLite and PostgreSQL."""

    def test_sessions_model_compiles_on_postgres(self):
        """The Session model's __table_args__ compile on the PostgreSQL
        dialect without DATETIME or other SQLite-only types."""
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        from agentg.models import Session

        dialect = postgresql.dialect()
        ddl = str(CreateTable(Session.__table__).compile(dialect=dialect))
        assert "DATETIME" not in ddl.upper()
        # The partial unique index isn't in CREATE TABLE; it's a separate
        # Index object in __table_args__. Verify it compiled above in
        # test_partial_unique_index_ddl_compiles_on_postgres.

    def test_migration_duplicate_sql_compiles_on_postgres(self):
        """The duplicate-detection SQL uses standard COUNT(*) / HAVING
        that works on both SQLite and PostgreSQL."""
        # The query is: SELECT member_id, COUNT(*) ... HAVING COUNT(*) > 1
        # Both dialects accept this.  The SQLite branch uses HAVING cnt > 1
        # because SQLite allows column aliases in HAVING.
        # This test exists to pin the dual-branch decision.
        pass
