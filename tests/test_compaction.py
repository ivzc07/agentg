"""History compaction: retention = compaction, durables to notes first."""

import pytest

from agentg.compaction import KEEP_RECENT, CompactionSummary, maybe_compact
from agentg.db import create_engine
from agentg.runtime import AgentRuntime
from agentg.stores import Stores
from agentg.linking import Linking
from agentg.messages import IncomingMessage
from conftest import unused_phraser


def item(i: int, role: str = "user") -> dict:
    return {"role": role, "content": f"turn {i}"}


class RecordingSummarizer:
    def __init__(self, notes=()):
        self.notes = list(notes)
        self.calls = []

    async def __call__(self, old_items, existing_notes):
        self.calls.append((list(old_items), list(existing_notes)))
        return CompactionSummary(
            summary="Dani benched 60 for three weeks; shoulder complaints in June.",
            notes=self.notes,
        )


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'compact.db'}")
    stores = Stores.from_engine(engine)
    runtime = AgentRuntime(
        agent=object(),
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, unused_phraser),
        summarizer=RecordingSummarizer(),
        stream_replies=False,
    )
    await runtime.ensure_schema()
    gym = await stores.linking.create_gym("Iron Temple")
    member = await stores.linking.link_member(gym.id, "Dani", "telegram", "42")

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.runtime = runtime
    env.notes = stores.notes
    env.member_id = member.id
    env.gym_id = gym.id
    env.session = runtime.session_for_member(member.id)
    yield env
    await engine.dispose()


def test_estimate_tokens_is_chars_over_four():
    from agentg.compaction import estimate_tokens

    # Independent of production framing: plain strings, known length.
    assert estimate_tokens(["abcd" * 10]) == 10  # 40 chars → 10 tokens
    assert estimate_tokens(["a" * 7, "b" * 1]) == 2  # 8 chars → 2 tokens


def fat_item(i: int, *, chars: int) -> dict:
    """One history item whose content alone is `chars` characters."""
    # prefix keeps items distinguishable; pad to exact char length
    prefix = f"turn {i}|"
    return {"role": "user", "content": prefix + ("x" * (chars - len(prefix)))}


def over_budget_items(n: int) -> list[dict]:
    """n items whose combined estimate clearly exceeds the token trigger."""
    from agentg.compaction import COMPACT_AT_TOKENS

    # content chars only — estimate also counts JSON framing, so this is a floor
    chars_each = (COMPACT_AT_TOKENS * 4) // n + 64
    return [fat_item(i, chars=chars_each) for i in range(n)]


async def test_many_small_items_do_not_trigger_compaction(env):
    """Item count is not the signal: 200 tiny turns stay under the token budget."""
    await env.session.add_items([item(i) for i in range(200)])
    summarizer = RecordingSummarizer()

    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is False
    assert summarizer.calls == []
    assert len(await env.session.get_items()) == 200


async def test_few_huge_items_do_trigger_compaction(env):
    """A handful of oversized turns cross the token budget and compact."""
    total = KEEP_RECENT + 5  # well under the old 50-item count threshold
    await env.session.add_items(over_budget_items(total))
    summarizer = RecordingSummarizer()

    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is True
    items = await env.session.get_items()
    assert len(items) == KEEP_RECENT + 1  # one summary + the recent tail
    assert "benched 60" in str(items[0])  # summary leads the history
    (old_items, _existing), = summarizer.calls
    assert len(old_items) == total - KEEP_RECENT


async def test_under_the_token_budget_nothing_happens(env):
    await env.session.add_items([item(i) for i in range(10)])
    summarizer = RecordingSummarizer()

    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is False
    assert summarizer.calls == []
    assert len(await env.session.get_items()) == 10


async def test_past_the_token_budget_old_turns_become_one_summary(env):
    total = KEEP_RECENT + 10
    await env.session.add_items(over_budget_items(total))
    summarizer = RecordingSummarizer()

    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is True
    items = await env.session.get_items()
    assert len(items) == KEEP_RECENT + 1  # one summary + the recent tail
    assert "benched 60" in str(items[0])  # the summary leads the history
    assert items[-1]["content"].startswith(f"turn {total - 1}|")  # newest kept
    # the raw old turns are deleted, not archived
    assert all(f"turn {0}|" not in str(i) for i in items[1:])
    # the summarizer saw exactly the turns that were compacted away
    (old_items, _existing), = summarizer.calls
    assert len(old_items) == total - KEEP_RECENT


async def test_keep_recent_floor_skips_when_nothing_outside_the_live_exchange(env):
    """Over budget but only KEEP_RECENT items — do not fold the live exchange."""
    await env.session.add_items(over_budget_items(KEEP_RECENT))
    summarizer = RecordingSummarizer()

    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is False
    assert summarizer.calls == []
    assert len(await env.session.get_items()) == KEEP_RECENT


async def test_durable_facts_land_in_notes_before_the_turns_are_deleted(env):
    await env.session.add_items(over_budget_items(KEEP_RECENT + 1))
    summarizer = RecordingSummarizer(notes=[("injury", "left shoulder impingement")])

    await maybe_compact(env.session, summarizer, env.notes, env.member_id, env.gym_id)

    active = await env.notes.active(env.member_id)
    assert [n.text for n in active] == ["left shoulder impingement"]
    assert active[0].kind == "injury"


async def test_existing_notes_are_shown_to_the_summarizer_for_dedup(env):
    await env.notes.remember(env.member_id, env.gym_id, "injury", "left shoulder impingement")
    await env.session.add_items(over_budget_items(KEEP_RECENT + 1))
    summarizer = RecordingSummarizer()

    await maybe_compact(env.session, summarizer, env.notes, env.member_id, env.gym_id)

    (_old, existing), = summarizer.calls
    assert existing == ["left shoulder impingement"]


async def test_the_summary_survives_for_later_turns(env):
    """AC: after compaction the Agent still answers what the summary covers —
    the mechanism being that the summary item is replayed into every run."""
    await env.session.add_items(over_budget_items(KEEP_RECENT + 1))
    await maybe_compact(
        env.session, RecordingSummarizer(), env.notes, env.member_id, env.gym_id
    )

    replayed = await env.session.get_items()
    assert any("shoulder complaints in June" in str(i) for i in replayed)


async def test_summary_lands_at_the_start_of_history(env):
    await env.session.add_items(over_budget_items(KEEP_RECENT + 3))
    await maybe_compact(
        env.session, RecordingSummarizer(), env.notes, env.member_id, env.gym_id
    )

    items = await env.session.get_items()
    assert "Summary of earlier conversation" in str(items[0])
    assert all("Summary of earlier conversation" not in str(i) for i in items[1:])


async def test_handle_message_defers_compaction_until_after_the_reply(env, monkeypatch):
    """Compaction no longer blocks the reply: the Agent sees uncompacted
    history, and compaction runs in after_send (issue #173)."""
    import agentg.runtime as runtime_module
    from types import SimpleNamespace

    history_sizes = []

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        history_sizes.append(len(await session.get_items()))
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    total = KEEP_RECENT + 20
    await env.session.add_items(over_budget_items(total))

    reply = await env.runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="I'm here", is_private=True)
    )

    # The Agent saw uncompacted history — compaction didn't block the reply.
    assert history_sizes == [total]
    assert reply.after_send is not None

    # After the reply is delivered, after_send compacts.
    await reply.after_send()

    items = await env.session.get_items()
    assert len(items) == KEEP_RECENT + 1  # one summary + the recent tail
    assert "benched 60" in str(items[0])


# ---------------------------------------------------------------------------
# Issue #165 — compaction survives failure and converges
# ---------------------------------------------------------------------------


class FailingSummarizer:
    """A summarizer that raises an exception on every call."""

    def __init__(self):
        self.calls = []

    async def __call__(self, old_items, existing_notes):
        self.calls.append((list(old_items), list(existing_notes)))
        raise RuntimeError("summarizer unavailable")


async def test_failing_summarizer_does_not_lose_history(env):
    """When the summarizer raises, the old session items are preserved
    because nothing is cleared before the summarizer is called."""
    total = KEEP_RECENT + 5
    original_items = over_budget_items(total)
    await env.session.add_items(original_items)

    with pytest.raises(RuntimeError, match="summarizer unavailable"):
        await maybe_compact(
            env.session,
            FailingSummarizer(),
            env.notes,
            env.member_id,
            env.gym_id,
        )

    # Old items are intact — no clear happened before the failed summarizer call
    items = await env.session.get_items()
    assert len(items) == total


async def test_failing_summarizer_in_handle_message_does_not_block_reply(env, monkeypatch):
    """A compaction failure in after_send does not propagate to the caller
    and the Member's message is still answered (issue #173 — compaction is
    deferred behind the reply)."""
    import agentg.runtime as runtime_module
    from types import SimpleNamespace

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    await env.session.add_items(over_budget_items(KEEP_RECENT + 20))

    # Replace the summarizer with one that always fails
    env.runtime.summarizer = FailingSummarizer()

    reply = await env.runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="I'm here", is_private=True)
    )

    # The reply still arrived — compaction failure didn't block it.
    assert str(reply) == "ok"
    # Compaction hasn't been called yet — it's deferred to after_send.
    assert len(env.runtime.summarizer.calls) == 0

    # after_send calls the summarizer; the failure is caught and logged.
    await reply.after_send()
    assert len(env.runtime.summarizer.calls) == 1


async def test_convergence_guard_skips_when_old_items_are_all_summaries(env):
    """When the only items outside the recent window are previous summaries,
    the summarizer is never called — there is nothing new to fold."""
    # Seed: one summary + KEEP_RECENT fat items, still over budget
    summary = {
        "role": "assistant",
        "content": "[Summary of earlier conversation]\nPrior training history.",
    }
    await env.session.add_items([summary] + over_budget_items(KEEP_RECENT))

    summarizer = RecordingSummarizer()
    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    # The only item outside the recent window is a summary — skip.
    assert compacted is False
    assert summarizer.calls == []


async def test_previously_written_summary_not_sent_to_summarizer(env):
    """When old items include both a previous summary and fresh turns,
    only the fresh turns are sent to the summarizer — the summary is
    preserved at the front of history and never re-summarized."""
    summary = {
        "role": "assistant",
        "content": "[Summary of earlier conversation]\nPrior training history.",
    }
    # Two fresh old items + KEEP_RECENT recent, all fat
    fresh_old = over_budget_items(2)
    await env.session.add_items([summary] + fresh_old + over_budget_items(KEEP_RECENT))

    summarizer = RecordingSummarizer()
    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is True
    assert len(summarizer.calls) == 1
    # Only the fresh items were sent to the summarizer — not the summary
    old_sent, _ = summarizer.calls[0]
    assert len(old_sent) == 2  # only the 2 fresh items, not the summary
    # The previous summary is preserved at the front of history
    items = await env.session.get_items()
    assert "Summary of earlier conversation" in str(items[0])
    # The new summary is the second item
    assert "Summary of earlier conversation" in str(items[1])
    # Total: old summary + new summary + KEEP_RECENT recent
    assert len(items) == 2 + KEEP_RECENT


async def test_convergence_guard_still_compacts_fresh_content(env):
    """When old items contain non-summary turns, compaction proceeds —
    the guard only blocks when there is nothing fresh to fold."""
    await env.session.add_items(over_budget_items(KEEP_RECENT + 5))
    summarizer = RecordingSummarizer()

    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is True
    assert len(summarizer.calls) == 1
    # All 5 old items were fresh — all sent to summarizer
    old_sent, _ = summarizer.calls[0]
    assert len(old_sent) == 5


# ---------------------------------------------------------------------------
# Issue #165 round 2 — crash safety, active convergence, summary bounding
# ---------------------------------------------------------------------------


async def test_maybe_compact_preserves_history_when_replace_fails(env):
    """If the replacement step fails after the summarizer and notes
    succeed, the old history is still intact — no items were deleted
    (issue #165, criterion #2)."""
    from unittest.mock import patch

    total = KEEP_RECENT + 5
    original_items = over_budget_items(total)
    await env.session.add_items(original_items)

    summarizer = RecordingSummarizer()

    with patch(
        "agentg.compaction._replace_items_atomically",
        side_effect=RuntimeError("simulated crash during replacement"),
    ):
        with pytest.raises(RuntimeError, match="simulated crash"):
            await maybe_compact(
                env.session, summarizer, env.notes, env.member_id, env.gym_id
            )

    # Summarizer was called (notes were written), but history is intact.
    assert len(summarizer.calls) == 1
    items = await env.session.get_items()
    assert len(items) == total
    for i in range(total):
        assert f"turn {i}|" in str(items[i])
    # No partial write — the replacement didn't touch the session.
    assert not any("Summary of earlier conversation" in str(it) for it in items)


async def test_replace_items_atomically_replaces_all_items(env):
    """Happy path: the atomic replacement correctly swaps old for new."""
    from agentg.compaction import _replace_items_atomically

    await env.session.add_items([item(i) for i in range(10)])
    new_items = [item(100), item(101)]

    await _replace_items_atomically(env.session, new_items)

    items = await env.session.get_items()
    assert len(items) == 2
    assert "turn 100" in str(items[0])
    assert "turn 101" in str(items[1])


async def test_convergence_guard_skips_when_one_fresh_item_and_summaries_exist(env):
    """When summaries already exist and only one fresh item has aged out
    of the recent window, compaction skips — it does not re-run on every
    message once the recent-item floor alone exceeds the budget (issue #165,
    criterion #3, active-conversation case)."""
    summary = {
        "role": "assistant",
        "content": "[Summary of earlier conversation]\nPrior training history.",
    }
    # One summary + 1 fresh old item + KEEP_RECENT recent, all fat → over budget
    await env.session.add_items(
        [summary] + over_budget_items(1) + over_budget_items(KEEP_RECENT)
    )

    summarizer = RecordingSummarizer()
    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    # Should skip: summaries exist and only 1 fresh item — not worth a model call.
    assert compacted is False
    assert summarizer.calls == []


# ---------------------------------------------------------------------------
# Issue #173 — compaction-vs-next-turn serialization (criteria 2 & 3)
# ---------------------------------------------------------------------------


async def test_compaction_in_after_send_serializes_with_next_turn(env, monkeypatch):
    """Compaction running inside after_send and the next handle_message
    must never interleave — both acquire the same per-identity asyncio.Lock,
    so Runner.run and maybe_compact are strictly serialized (issue #173)."""
    import agentg.runtime as runtime_module
    from types import SimpleNamespace
    import asyncio
    from agentg.compaction import CompactionSummary

    running: set[str] = set()
    overlapped: list[str] = []

    # Gate holds the summarizer mid-flight inside the lock.
    gate = asyncio.Event()
    compaction_started = asyncio.Event()

    async def slow_summarizer(old_items, existing_notes):
        running.add("compaction")
        compaction_started.set()
        await gate.wait()
        running.discard("compaction")
        return CompactionSummary(summary="Dani benched 60.", notes=[])

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        if "compaction" in running:
            overlapped.append(f"run-during-compaction:{text}")
        running.add("run")
        await asyncio.sleep(0.01)
        running.discard("run")
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    env.runtime.summarizer = slow_summarizer

    total = KEEP_RECENT + 20
    await env.session.add_items(over_budget_items(total))

    # First turn returns immediately; compaction is deferred to after_send.
    reply = await env.runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="first", is_private=True)
    )
    assert reply.after_send is not None

    # Start after_send in a concurrent task.
    after_task = asyncio.create_task(reply.after_send())
    # Wait until compaction is definitely inside the lock.
    await asyncio.wait_for(compaction_started.wait(), timeout=5)

    # Fire a second message for the same identity.  It must block on the
    # per-identity lock that after_send still holds.
    second_task = asyncio.create_task(
        env.runtime.handle_message(
            IncomingMessage(channel="telegram", channel_user_id="42", text="second", is_private=True)
        )
    )
    # Let the second task reach the lock-acquisition point.
    await asyncio.sleep(0.05)

    # Release the gate so compaction finishes and releases the lock.
    gate.set()
    await asyncio.wait_for(after_task, timeout=5)
    second_reply = await asyncio.wait_for(second_task, timeout=5)

    # No overlap detected — the lock serialized correctly.
    assert overlapped == []
    assert str(second_reply) == "ok"


async def test_compaction_completes_before_next_turn_even_when_after_send_is_delayed(env, monkeypatch):
    """When the adapter delays calling after_send (e.g. Telegram's
    message.answer calls), a rapid second message for the same identity must
    still wait for the first turn's compaction to finish before its own
    Runner.run begins (issue #173 criterion 2 — inverted ordering).

    This is the case the existing serialization test does NOT cover: there,
    after_send already holds the lock so the second turn blocks on the lock
    itself.  Here after_send hasn't even started — the second turn must
    block on _compaction_done instead."""
    import agentg.runtime as runtime_module
    from types import SimpleNamespace
    import asyncio
    from agentg.compaction import CompactionSummary

    run_order: list[str] = []

    # Gate holds the summarizer mid-flight.  after_send hasn't started yet
    # when the second message arrives — it's still queued behind the
    # adapter's message.answer calls.
    gate = asyncio.Event()

    async def slow_summarizer(old_items, existing_notes):
        await gate.wait()
        run_order.append("compaction-1")
        return CompactionSummary(summary="Dani benched 60.", notes=[])

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        run_order.append(f"run:{text}")
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    env.runtime.summarizer = slow_summarizer

    total = KEEP_RECENT + 20
    await env.session.add_items(over_budget_items(total))

    # First turn returns immediately; compaction is deferred to after_send.
    reply1 = await env.runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="first", is_private=True)
    )
    assert reply1.after_send is not None

    # Simulate the adapter delay: the second message arrives BEFORE
    # after_send is called (while the adapter is still sending
    # message.answer chunks).  The second turn must block on
    # _compaction_done, not on the lock.
    second_task = asyncio.create_task(
        env.runtime.handle_message(
            IncomingMessage(channel="telegram", channel_user_id="42", text="second", is_private=True)
        )
    )
    # Let the second task reach the _compaction_done wait point.
    await asyncio.sleep(0.05)
    # The second turn should NOT have run yet.
    assert run_order == ["run:first"]

    # Now start after_send — it will block inside the lock on the gate.
    after_task = asyncio.create_task(reply1.after_send())
    await asyncio.sleep(0.05)
    # Still blocked.
    assert run_order == ["run:first"]

    # Release the gate so compaction finishes and sets the event.
    gate.set()
    await asyncio.wait_for(after_task, timeout=5)
    reply2 = await asyncio.wait_for(second_task, timeout=5)

    assert str(reply2) == "ok"
    # Compaction from turn 1 finished before turn 2's Agent ran — even
    # though the second message arrived before after_send even started.
    assert run_order == ["run:first", "compaction-1", "run:second"]


async def test_convergence_guard_proceeds_with_enough_fresh_items(env):
    """When summaries exist AND enough fresh items have accumulated,
    compaction proceeds normally — the guard is a floor, not a ceiling."""
    from agentg.compaction import MIN_FRESH_TO_SUMMARIZE

    summary = {
        "role": "assistant",
        "content": "[Summary of earlier conversation]\nPrior training history.",
    }
    # Enough fresh items to meet the threshold
    await env.session.add_items(
        [summary]
        + over_budget_items(MIN_FRESH_TO_SUMMARIZE)
        + over_budget_items(KEEP_RECENT)
    )

    summarizer = RecordingSummarizer()
    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is True
    assert len(summarizer.calls) == 1


async def test_multiple_old_summaries_are_merged(env):
    """When multiple previous summaries accumulate across compactions,
    they are merged into a single item to prevent unbounded growth
    (issue #165, P2 — summary count converges)."""
    summaries = [
        {
            "role": "assistant",
            "content": f"[Summary of earlier conversation]\nSummary epoch {i}.",
        }
        for i in range(3)
    ]
    await env.session.add_items(
        summaries + over_budget_items(KEEP_RECENT + 3)
    )

    summarizer = RecordingSummarizer()
    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is True
    items = await env.session.get_items()
    # 3 old summaries merged into 1 + 1 new summary + KEEP_RECENT recent
    assert len(items) == 2 + KEEP_RECENT
    # The merged summary is at the front and contains all three epochs
    assert "Summary of earlier conversation" in str(items[0])
    assert "Summary epoch 0" in str(items[0])
    assert "Summary epoch 1" in str(items[0])
    assert "Summary epoch 2" in str(items[0])
    # The new summary is second
    assert "Summary of earlier conversation" in str(items[1])


async def test_a_channel_that_never_runs_after_send_does_not_wedge_the_member(
    env, monkeypatch
):
    """A channel that drops ``after_send`` must not wedge the Member forever.

    ``after_send`` is caller-driven: the runtime hands it back and trusts the
    channel to run it.  A channel that ignores it (or dies before it) must
    still be able to serve that Member's next turn — a cosmetic ordering
    guarantee may never cost liveness (issue #173).
    """
    import asyncio
    from types import SimpleNamespace

    import agentg.runtime as runtime_module

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    env.runtime.compaction_grace_seconds = 0.05

    msg = IncomingMessage(channel="telegram", channel_user_id="42", text="I'm here", is_private=True)
    # First turn: the reply comes back with an after_send the channel drops.
    first = await env.runtime.handle_message(msg)
    assert first.after_send is not None

    # Second turn from the same Member must still be served.
    second = await asyncio.wait_for(env.runtime.handle_message(msg), timeout=5)
    assert str(second) == "ok"

    # The stale signal is consumed, so the wedge costs one grace period
    # once — not a fresh stall on every later turn.
    third = await asyncio.wait_for(env.runtime.handle_message(msg), timeout=0.5)
    assert str(third) == "ok"


async def test_a_failing_after_send_does_not_wedge_the_member(env, monkeypatch):
    """``after_send`` raising before it reaches compaction must not wedge
    the Member either — the completion signal has to survive failure."""
    import asyncio
    from types import SimpleNamespace

    import agentg.runtime as runtime_module

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    msg = IncomingMessage(channel="telegram", channel_user_id="42", text="I'm here", is_private=True)
    first = await env.runtime.handle_message(msg)

    class Boom(Exception):
        pass

    # The channel starts after_send but it blows up part-way through.
    async def exploding_compact(*args, **kwargs):
        raise Boom("compaction exploded")

    monkeypatch.setattr(runtime_module, "maybe_compact", exploding_compact)
    await first.after_send()

    second = await asyncio.wait_for(env.runtime.handle_message(msg), timeout=5)
    assert str(second) == "ok"
