"""AgentRuntime: schema startup, member-keyed history, serialized turns."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import event

import agentg.runtime as runtime_module
from agentg.db import create_engine
from agentg.messages import IncomingMessage
from agentg.linking import Linking
from agentg.linking_store import LinkedIdentity
from agentg.routines import ExerciseSpec, WorkoutSpec
from agentg.runtime import AgentRuntime
from agentg.stores import Stores
from conftest import unused_phraser


async def null_summarizer(old_items, existing_notes):
    raise AssertionError("compaction should not trigger in this test")


def sqlite_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}"


def make_runtime(url) -> AgentRuntime:
    engine = create_engine(url)
    stores = Stores.from_engine(engine)
    return AgentRuntime(
        agent=object(),
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, unused_phraser),
        summarizer=null_summarizer,
        stream_replies=False,
    )


def incoming(text, user_id):
    return IncomingMessage(
        channel="telegram", channel_user_id=user_id, text=text, display_name="Ana"
    )


@pytest.fixture
async def runtime(tmp_path):
    runtime = make_runtime(sqlite_url(tmp_path))
    await runtime.ensure_schema()
    yield runtime
    await runtime.engine.dispose()


async def test_history_survives_a_process_restart(tmp_path):
    url = sqlite_url(tmp_path)
    turn = [{"role": "user", "content": "bench was 60 today"}]

    runtime = make_runtime(url)
    await runtime.ensure_schema()
    await runtime.session_for_member(1).add_items(turn)
    await runtime.engine.dispose()  # the process dies

    runtime = make_runtime(url)  # ...and comes back
    await runtime.ensure_schema()
    assert await runtime.session_for_member(1).get_items() == turn
    await runtime.engine.dispose()


async def test_member_histories_are_isolated_from_each_other(runtime):
    await runtime.session_for_member(1).add_items([{"role": "user", "content": "my knee hurts"}])
    assert await runtime.session_for_member(2).get_items() == []


async def test_turns_in_one_conversation_never_interleave(runtime, monkeypatch):
    running: set[str] = set()
    overlapped = []

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        if session.session_id in running:
            overlapped.append(text)
        running.add(session.session_id)
        await asyncio.sleep(0.01)
        running.discard(session.session_id)
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    await runtime.stores.linking.link_member(gym.id, "Ben", "telegram", "7")

    await asyncio.gather(
        runtime.handle_message(incoming("first", "42")),
        runtime.handle_message(incoming("second", "42")),
        runtime.handle_message(incoming("other member", "7")),
    )

    assert overlapped == []


# --- AC: the rhythm reset no longer blocks the reply, and lapsed Members are still revived (#169) ---


async def test_reset_rhythm_is_deferred_past_the_reply(runtime, monkeypatch):
    """reset_rhythm must not block the LLM call — it fires after_send."""
    events: list[str] = []

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        events.append("llm")
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    original_reset = runtime.stores.checkins.reset_rhythm

    async def spy_reset(member_id: int) -> None:
        events.append("reset_rhythm")
        await original_reset(member_id)

    runtime.stores.checkins.reset_rhythm = spy_reset  # type: ignore[method-assign]

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    reply = await runtime.handle_message(incoming("I'm here", "42"))

    # The LLM ran before the reply was complete; reset_rhythm was only queued.
    assert "llm" in events
    # The reset_rhythm hasn't fired yet — it's deferred to after_send.
    assert "reset_rhythm" not in events

    # Now await after_send to simulate the channel adapter's delivery.
    if reply.after_send is not None:
        await reply.after_send()

    # After delivery, reset_rhythm fires.
    assert events.index("llm") < events.index("reset_rhythm")


async def test_deferred_reset_rhythm_still_revives_lapsed_members(runtime, monkeypatch):
    """A lapsed Member is revived after the reply, not before."""
    async def fake_run(agent, text, *, session, context=None, run_config=None):
        return SimpleNamespace(final_output="welcome back!")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    await runtime.stores.checkins.lapse(1)  # member id 1

    state_before, _ = await runtime.stores.checkins.get_state(1)
    assert state_before == "lapsed"

    reply = await runtime.handle_message(incoming("I'm back", "42"))
    assert reply == "welcome back!"

    # The lapsed state is still visible during the reply (reset not yet applied).
    # After after_send, the Member is revived.
    if reply.after_send is not None:
        await reply.after_send()

    state_after, _ = await runtime.stores.checkins.get_state(1)
    assert state_after == "on"


async def test_reset_rhythm_still_runs_on_llm_failure(runtime, monkeypatch):
    """A lapsed Member is revived even when the LLM call fails (#169).

    Before this PR, the rhythm reset only ran inside after_send, which the
    channel adapter never calls when the reply_fn raises — a lapsed Member
    whose model call failed would stay lapsed, feeding the give-up rule."""
    async def fake_run(agent, text, *, session, context=None, run_config=None):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    await runtime.stores.checkins.lapse(1)

    state_before, _ = await runtime.stores.checkins.get_state(1)
    assert state_before == "lapsed"

    with pytest.raises(RuntimeError, match="model unavailable"):
        await runtime.handle_message(incoming("I'm here", "42"))

    # The rhythm reset ran on the error path: the Member is revived.
    state_after, _ = await runtime.stores.checkins.get_state(1)
    assert state_after == "on"


# --- AC: the measured query count for a plain message drops (#169) ---


async def test_plain_linked_message_issues_few_queries(runtime, monkeypatch):
    """A plain message from a linked Member issues a bounded number of DB
    queries — the turn-level query count that issue #169 asks for."""
    async def fake_run(agent, text, *, session, context=None, run_config=None):
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    counts: list[int] = []

    @event.listens_for(runtime.engine.sync_engine, "before_cursor_execute")
    def count_query(conn, cursor, statement, parameters, context, executemany):
        counts.append(1)

    await runtime.handle_message(incoming("I'm here today", "42"))

    # The exact count depends on compaction internals, but it must be
    # bounded — a plain message should never trigger dozens of queries.
    assert len(counts) < 30, f"expected <30 queries for a plain message, got {len(counts)}"


# --- member_context gating flags (issue #174) ---


def bench_spec():
    return [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench press", sets=3, reps="5")])]


async def test_member_context_can_author_routine_no_routine(runtime):
    """A new Member with no routine: can_author_routine is True."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    member = await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "1")
    linked = LinkedIdentity(member=member, gym=gym)

    ctx = await runtime.member_context(linked)
    assert ctx.can_author_routine is True


async def test_member_context_can_author_routine_after_agent_routine(runtime):
    """A Member with an agent-generated routine: can_author_routine is True
    (the Agent can restructure it on request)."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    member = await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "1")
    # An agent-generated routine (coach_authored omitted / defaults to False).
    await runtime.stores.routines.save_routine(member.id, gym.id, bench_spec())
    linked = LinkedIdentity(member=member, gym=gym)

    ctx = await runtime.member_context(linked)
    assert ctx.can_author_routine is True


async def test_member_context_can_author_routine_after_coach_routine(runtime):
    """A Member with a coach-authored routine: can_author_routine is False
    (the Agent never restructures coach-authored routines)."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    member = await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "1")
    await runtime.stores.routines.save_routine(
        member.id, gym.id, bench_spec(), coach_authored=True
    )
    linked = LinkedIdentity(member=member, gym=gym)

    ctx = await runtime.member_context(linked)
    assert ctx.can_author_routine is False


async def test_member_context_can_author_routine_for_coach_with_own_routine(runtime):
    """A Coach always gets can_author_routine=True, even with their own
    coach-authored routine (is_coach dominates)."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    coach = await runtime.stores.linking.link_member(gym.id, "Coach Sam", "telegram", "2")
    await runtime.stores.linking.set_coach(coach.id)
    await runtime.stores.routines.save_routine(
        coach.id, gym.id, bench_spec(), coach_authored=True
    )
    # Re-fetch to get the fresh is_coach flag from the DB (set_coach writes
    # through SQL without refreshing the in-memory model).
    linked = await runtime.stores.linking.identity_for("telegram", "2")

    ctx = await runtime.member_context(linked)
    assert ctx.can_author_routine is True


# --- P2 (fix-r8): forget-me warning resets check-in rhythm ---


async def test_forget_me_warning_resets_checkin_rhythm(runtime):
    """A forget-me warning is an actual Member reply — the proactive
    check-in rhythm must be reset so the cadence doesn't degrade during
    the two-turn flow.  The model never runs on the warning path, so the
    async reset_task from the normal Agent path is never created; this
    test confirms the inline equivalent fires."""
    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    # Simulate a lapsed Member: ignored_nudges past the give-up threshold.
    await runtime.stores.checkins.lapse(1)
    state_before, _ = await runtime.stores.checkins.get_state(1)
    assert state_before == "lapsed"

    # Send a forget-me trigger — the runtime returns a warning without
    # calling the model.
    reply = await runtime.handle_message(incoming("forget me", "42"))
    assert "DELETE-ME-" in str(reply)
    assert "permanently" in str(reply).lower()

    # The rhythm must be reset: lapsed → on, ignored_nudges → 0.
    state_after, _ = await runtime.stores.checkins.get_state(1)
    assert state_after == "on", (
        "forget-me warning must reset check-in rhythm (lapsed → on)"
    )


async def test_forget_me_warning_does_not_crash_on_sentinel(runtime):
    """fix-r12 R4: when a deleting row exists (deletion already confirmed
    but not completed), a new generic "forget me" request must NOT trigger
    deletion.  The runtime's _handle_forget_me gates with get_deleting_request
    BEFORE is_forget_me_request, returning "deletion in progress."

    Only the exact stored confirmation phrase may retry deletion.  The
    sentinel (empty string) from request_forget_me is still handled in the
    true race case where a concurrent runtime claims between our deleting
    check and the request_forget_me call."""
    from datetime import datetime, timezone

    gym = await runtime.stores.linking.create_gym("Iron Temple")
    await runtime.stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    # Simulate: another runtime already claimed the request (status -> deleting).
    now = datetime.now(timezone.utc)
    phrase = await runtime.stores.forget.request_forget_me(1, gym.id, now, 300, "en")
    claimed = await runtime.stores.forget.claim_forget_me_request(1, phrase, now)
    assert claimed is not None

    # A new generic "forget me" arrives.  The runtime must gate with
    # get_deleting_request BEFORE is_forget_me_request and return
    # "deletion in progress" — the Member must NOT be deleted.
    reply = await runtime.handle_message(incoming("forget me", "42"))
    assert "deletion is in progress" in str(reply).lower(), (
        f"expected 'deletion in progress', got: {reply!r}"
    )

    # The Member must still exist — deletion was NOT triggered.
    identity = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity is not None, (
        "Member must NOT be deleted by a new generic 'forget me' in deleting state"
    )

    # Only the exact phrase can resume deletion.
    reply_exact = await runtime.handle_message(incoming(phrase, "42"))
    assert "permanently" in str(reply_exact).lower() or (
        "eliminados" in str(reply_exact).lower()
    )
    identity_after = await runtime.stores.linking.identity_for("telegram", "42")
    assert identity_after is None, (
        "exact phrase must still resume and complete deletion"
    )

