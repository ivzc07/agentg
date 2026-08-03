"""Fast-path set logging: unit tests for the decision logic (#177)."""

import pytest
from datetime import UTC, datetime, timedelta

from agentg.parsing import ParsedSetLine, parse_set_line
from agentg.training import TrainingStore


@pytest.fixture
def frozen_clock():
    """Clock pinned at a known time so stale-session math is predictable."""
    now = datetime(2025, 7, 15, 10, 0, 0, tzinfo=UTC)
    return lambda: now


@pytest.fixture
async def env(tmp_path, frozen_clock):
    """A TrainingStore wired to a temp DB with a gym and member ready."""
    from agentg.db import create_engine
    from agentg.models import Base
    from agentg.linking_store import LinkingStore

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'fast_log.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Dani", "telegram", "42")
    store = TrainingStore(engine, clock=frozen_clock)
    await store.ensure_seeded()
    yield store, gym.id, member.id, engine
    await engine.dispose()


@pytest.fixture
def bench_line():
    return ParsedSetLine(exercise="bench", weight=60.0, unit=None, reps=[8, 8, 8])


@pytest.fixture
def bodyweight_line():
    return ParsedSetLine(exercise="dips", weight=None, unit=None, reps=[10, 10, 9])


@pytest.fixture
def no_exercise_line():
    return ParsedSetLine(exercise=None, weight=60.0, unit=None, reps=[8, 8, 7])


# ---------------------------------------------------------------------------
# Fast path fires
# ---------------------------------------------------------------------------


async def test_fast_path_logs_when_session_is_open(env, bench_line):
    store, gym_id, member_id, _engine = env
    await store.open_session(member_id, gym_id)
    logged = await store.try_fast_log_sets(member_id, gym_id, bench_line)
    assert logged is not None
    assert logged.exercise == "bench press"  # alias resolved
    assert logged.weight == 60.0
    assert logged.reps == [8, 8, 8]
    assert logged.suspect is None


async def test_fast_path_logs_bodyweight_exercise(env, bodyweight_line):
    store, gym_id, member_id, _engine = env
    await store.open_session(member_id, gym_id)
    logged = await store.try_fast_log_sets(member_id, gym_id, bodyweight_line)
    assert logged is not None
    assert logged.exercise == "dips"
    assert logged.weight is None
    assert logged.reps == [10, 10, 9]


# ---------------------------------------------------------------------------
# Fall-through: no open session
# ---------------------------------------------------------------------------


async def test_fast_path_returns_none_without_open_session(env, bench_line):
    store, gym_id, member_id, _engine = env
    logged = await store.try_fast_log_sets(member_id, gym_id, bench_line)
    assert logged is None


async def test_fast_path_returns_none_when_session_is_stale(env, bench_line, frozen_clock):
    """A Session abandoned for >3h is auto-closed by _open_session_row."""
    from agentg.training import SESSION_AUTO_CLOSE

    store, gym_id, member_id, _engine = env
    await store.open_session(member_id, gym_id)
    late_clock = lambda: frozen_clock() + SESSION_AUTO_CLOSE + timedelta(hours=1)
    store2 = TrainingStore(_engine, clock=late_clock)
    logged = await store2.try_fast_log_sets(member_id, gym_id, bench_line)
    assert logged is None


# ---------------------------------------------------------------------------
# Fall-through: exercise not in catalog
# ---------------------------------------------------------------------------


async def test_fast_path_returns_none_for_unknown_exercise(env):
    store, gym_id, member_id, _engine = env
    await store.open_session(member_id, gym_id)
    parsed = ParsedSetLine(exercise="zzz-snatch", weight=40.0, unit=None, reps=[5, 5, 5])
    logged = await store.try_fast_log_sets(member_id, gym_id, parsed)
    assert logged is None  # fast path never creates exercises


# ---------------------------------------------------------------------------
# Fall-through: no exercise name
# ---------------------------------------------------------------------------


async def test_fast_path_returns_none_when_exercise_is_none(env, no_exercise_line):
    store, gym_id, member_id, _engine = env
    await store.open_session(member_id, gym_id)
    logged = await store.try_fast_log_sets(member_id, gym_id, no_exercise_line)
    assert logged is None  # needs conversational context


# ---------------------------------------------------------------------------
# Fall-through: suspect weight
# ---------------------------------------------------------------------------


async def test_fast_path_returns_none_when_weight_is_suspect(env, bench_line):
    store, gym_id, member_id, _engine = env
    # Seed a prior session with a much lower weight so the new one looks suspect.
    await store.open_session(member_id, gym_id)
    await store.log_sets(member_id, gym_id, "bench 10 8,8,8")
    await store.close_session(member_id)
    # Open a fresh session for the fast-path attempt.
    await store.open_session(member_id, gym_id)
    # bench_line has weight=60 which is >2× the previous 10.
    logged = await store.try_fast_log_sets(member_id, gym_id, bench_line)
    assert logged is None


# ---------------------------------------------------------------------------
# parse_set_line → purity: the parser is the gate
# ---------------------------------------------------------------------------

def test_parser_rejects_prose():
    assert parse_set_line("bench 60 8,8,8 felt heavy") is None


def test_parser_rejects_greeting():
    assert parse_set_line("I'm here") is None


def test_parser_rejects_done():
    assert parse_set_line("done") is None


def test_parser_rejects_correction():
    assert parse_set_line("actually bench was 62.5 not 60") is None


def test_parser_accepts_pure_shorthand_with_exercise():
    assert parse_set_line("bench 60 8,8,8") is not None


def test_parser_accepts_pure_shorthand_without_exercise():
    """No-exercise lines parse successfully — they just need context."""
    parsed = parse_set_line("60 8,8,7")
    assert parsed is not None
    assert parsed.exercise is None


def test_parser_accepts_bodyweight_shorthand():
    parsed = parse_set_line("dips 10,10,9")
    assert parsed is not None
    assert parsed.exercise == "dips"
    assert parsed.weight is None


# ---------------------------------------------------------------------------
# _format_fast_confirmation
# ---------------------------------------------------------------------------

from agentg.runtime import AgentRuntime


class _FakeRuntime(AgentRuntime):
    """Expose the formatter without a full AgentRuntime setup."""

    def __init__(self):
        pass


def test_confirmation_with_weight():
    rt = _FakeRuntime()
    from agentg.training import LoggedSets

    logged = LoggedSets(exercise="bench press", weight=60.0, reps=[8, 8, 8], previous=None)
    result = rt._format_fast_confirmation(logged, "kg")
    assert "bench press" in result
    assert "60kg" in result
    assert "8/8/8" in result
    assert "✅" in result
    assert "primera vez" in result


def test_confirmation_bodyweight():
    rt = _FakeRuntime()
    from agentg.training import LoggedSets

    logged = LoggedSets(exercise="dips", weight=None, reps=[10, 10, 9], previous=None)
    result = rt._format_fast_confirmation(logged, "kg")
    assert "dips" in result
    assert "10/10/9" in result
    assert "kg" not in result
    assert "primera vez" in result


def test_confirmation_decimal_weight():
    rt = _FakeRuntime()
    from agentg.training import LoggedSets

    logged = LoggedSets(exercise="bench press", weight=62.5, reps=[8, 8, 8], previous=None)
    result = rt._format_fast_confirmation(logged, "kg")
    assert "62.5kg" in result
    assert "primera vez" in result


def test_confirmation_no_previous_data():
    rt = _FakeRuntime()
    from agentg.training import LoggedSets

    logged = LoggedSets(exercise="squat", weight=100.0, reps=[5, 5, 5],
                        previous={"weight": None, "reps": []})
    result = rt._format_fast_confirmation(logged, "kg")
    assert "✅" in result
    assert "primera vez" not in result
    assert "supera" not in result


def test_confirmation_weight_went_up():
    rt = _FakeRuntime()
    from agentg.training import LoggedSets

    logged = LoggedSets(exercise="bench press", weight=62.5, reps=[8, 8, 8],
                        previous={"weight": 60.0, "reps": [8, 8, 7]})
    result = rt._format_fast_confirmation(logged, "kg")
    assert "subiste de 60kg" in result
    assert "supera" in result


def test_confirmation_weight_went_down():
    rt = _FakeRuntime()
    from agentg.training import LoggedSets

    logged = LoggedSets(exercise="bench press", weight=55.0, reps=[8, 8, 8],
                        previous={"weight": 60.0, "reps": [8, 8, 7]})
    result = rt._format_fast_confirmation(logged, "kg")
    assert "bajó de 60kg" in result


def test_confirmation_reps_went_up():
    rt = _FakeRuntime()
    from agentg.training import LoggedSets

    logged = LoggedSets(exercise="bench press", weight=60.0, reps=[8, 8, 8],
                        previous={"weight": 60.0, "reps": [8, 8, 7]})
    result = rt._format_fast_confirmation(logged, "kg")
    assert "24 reps total superan 23" in result


def test_confirmation_same_numbers_no_celebration():
    rt = _FakeRuntime()
    from agentg.training import LoggedSets

    logged = LoggedSets(exercise="bench press", weight=60.0, reps=[8, 8, 8],
                        previous={"weight": 60.0, "reps": [8, 8, 8]})
    result = rt._format_fast_confirmation(logged, "kg")
    assert "✅" in result
    assert "supera" not in result
    assert "subiste" not in result
