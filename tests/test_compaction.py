"""History compaction: retention = compaction, durables to notes first."""

from datetime import UTC, datetime

import pytest

from agentg.compaction import COMPACT_THRESHOLD, KEEP_RECENT, CompactionSummary, maybe_compact
from agentg.db import create_engine
from agentg.notes import NotesStore
from agentg.runtime import AgentRuntime
from agentg.store import LinkingStore
from agentg.training import TrainingStore
from agentg.onboarding import Onboarding
from agentg.messages import IncomingMessage


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
    linking = LinkingStore(engine)
    training = TrainingStore(engine)
    notes = NotesStore(engine)
    runtime = AgentRuntime(
        agent=object(),
        engine=engine,
        store=linking,
        onboarding=Onboarding(linking),
        training=training,
        notes=notes,
        summarizer=RecordingSummarizer(),
    )
    await runtime.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Dani", "telegram", "42")

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.runtime = runtime
    env.notes = notes
    env.member_id = member.id
    env.gym_id = gym.id
    env.session = runtime.session_for_member(member.id)
    yield env
    await engine.dispose()


async def test_under_the_threshold_nothing_happens(env):
    await env.session.add_items([item(i) for i in range(10)])
    summarizer = RecordingSummarizer()

    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is False
    assert summarizer.calls == []
    assert len(await env.session.get_items()) == 10


async def test_past_the_threshold_old_turns_become_one_summary(env):
    total = COMPACT_THRESHOLD + 10
    await env.session.add_items([item(i) for i in range(total)])
    summarizer = RecordingSummarizer()

    compacted = await maybe_compact(
        env.session, summarizer, env.notes, env.member_id, env.gym_id
    )

    assert compacted is True
    items = await env.session.get_items()
    assert len(items) == KEEP_RECENT + 1  # one summary + the recent tail
    assert "benched 60" in str(items[0])  # the summary leads the history
    assert items[-1]["content"] == f"turn {total - 1}"  # newest raw turn kept
    # the raw old turns are deleted, not archived
    assert all(f"turn {0}" not in str(i) for i in items[1:])
    # the summarizer saw exactly the turns that were compacted away
    (old_items, _existing), = summarizer.calls
    assert len(old_items) == total - KEEP_RECENT


async def test_durable_facts_land_in_notes_before_the_turns_are_deleted(env):
    await env.session.add_items([item(i) for i in range(COMPACT_THRESHOLD + 1)])
    summarizer = RecordingSummarizer(notes=[("injury", "left shoulder impingement")])

    await maybe_compact(env.session, summarizer, env.notes, env.member_id, env.gym_id)

    active = await env.notes.active(env.member_id)
    assert [n.text for n in active] == ["left shoulder impingement"]
    assert active[0].kind == "injury"


async def test_existing_notes_are_shown_to_the_summarizer_for_dedup(env):
    await env.notes.remember(env.member_id, env.gym_id, "injury", "left shoulder impingement")
    await env.session.add_items([item(i) for i in range(COMPACT_THRESHOLD + 1)])
    summarizer = RecordingSummarizer()

    await maybe_compact(env.session, summarizer, env.notes, env.member_id, env.gym_id)

    (_old, existing), = summarizer.calls
    assert existing == ["left shoulder impingement"]


async def test_the_summary_survives_for_later_turns(env):
    """AC: after compaction the Agent still answers what the summary covers —
    the mechanism being that the summary item is replayed into every run."""
    await env.session.add_items([item(i) for i in range(COMPACT_THRESHOLD + 1)])
    await maybe_compact(
        env.session, RecordingSummarizer(), env.notes, env.member_id, env.gym_id
    )

    replayed = await env.session.get_items()
    assert any("shoulder complaints in June" in str(i) for i in replayed)


async def test_handle_message_compacts_before_running_the_agent(env, monkeypatch):
    import agentg.runtime as runtime_module
    from types import SimpleNamespace

    history_sizes = []

    async def fake_run(agent, text, *, session, context=None):
        history_sizes.append(len(await session.get_items()))
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    await env.session.add_items([item(i) for i in range(COMPACT_THRESHOLD + 20)])

    await env.runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="I'm here")
    )

    assert history_sizes == [KEEP_RECENT + 1]  # compacted before the run
