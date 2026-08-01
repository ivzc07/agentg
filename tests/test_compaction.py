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


async def test_handle_message_compacts_before_running_the_agent(env, monkeypatch):
    import agentg.runtime as runtime_module
    from types import SimpleNamespace

    history_sizes = []

    async def fake_run(agent, text, *, session, context=None):
        history_sizes.append(len(await session.get_items()))
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    await env.session.add_items(over_budget_items(KEEP_RECENT + 20))

    await env.runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="I'm here")
    )

    assert history_sizes == [KEEP_RECENT + 1]  # compacted before the run
