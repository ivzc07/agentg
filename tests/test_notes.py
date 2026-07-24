"""NotesStore: volunteered durable facts as plain, coach-inspectable rows."""

from datetime import timedelta

import pytest
from sqlalchemy import text

from conftest import FakeClock

from agentg.db import create_engine
from agentg.notes import NOTE_KINDS, NotesStore
from agentg.linking_store import LinkingStore


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'notes.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Dani", "telegram", "42")
    other = await linking.link_member(gym.id, "Ben", "telegram", "7")

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.clock = FakeClock()
    env.notes = NotesStore(engine, clock=env.clock)
    env.member_id = member.id
    env.other_member_id = other.id
    env.gym_id = gym.id
    yield env
    await engine.dispose()


async def test_a_volunteered_fact_lands_as_an_active_note(env):
    note = await env.notes.remember(env.member_id, env.gym_id, "injury", "shoulder's been hurting")

    active = await env.notes.active(env.member_id)
    assert [n.id for n in active] == [note.id]
    assert active[0].kind == "injury"
    assert active[0].text == "shoulder's been hurting"


async def test_retiring_is_soft_the_row_stays(env):
    note = await env.notes.remember(env.member_id, env.gym_id, "injury", "shoulder pain")
    env.clock.advance(timedelta(days=14))

    await env.notes.retire(env.member_id, note.id)

    assert await env.notes.active(env.member_id) == []
    async with env.engine.connect() as conn:  # the row is still there, dated
        rows = (await conn.execute(text("SELECT id, retired_at FROM member_notes"))).all()
    assert len(rows) == 1
    assert rows[0].retired_at is not None


async def test_retiring_another_members_note_is_refused(env):
    note = await env.notes.remember(env.member_id, env.gym_id, "goal", "half marathon")
    with pytest.raises(ValueError, match="note"):
        await env.notes.retire(env.other_member_id, note.id)
    assert len(await env.notes.active(env.member_id)) == 1


async def test_unknown_kinds_fall_back_to_other(env):
    note = await env.notes.remember(env.member_id, env.gym_id, "vibes", "prefers morning sessions")
    assert note.kind == "other"
    assert "injury" in NOTE_KINDS and "preference" in NOTE_KINDS and "goal" in NOTE_KINDS


async def test_notes_are_plain_rows_a_human_can_inspect(env):
    await env.notes.remember(env.member_id, env.gym_id, "preference", "hates burpees")
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT gym_id, member_id, kind, text FROM member_notes")
            )
        ).all()
    assert rows == [(env.gym_id, env.member_id, "preference", "hates burpees")]


async def test_active_notes_are_ordered_oldest_first(env):
    first = await env.notes.remember(env.member_id, env.gym_id, "goal", "half marathon")
    env.clock.advance(timedelta(days=1))
    second = await env.notes.remember(env.member_id, env.gym_id, "preference", "hates burpees")

    active = await env.notes.active(env.member_id)
    assert [n.id for n in active] == [first.id, second.id]


async def test_note_text_is_capped_not_rejected(env):
    note = await env.notes.remember(env.member_id, env.gym_id, "other", "x" * 1000)
    assert len(note.text) <= 400
