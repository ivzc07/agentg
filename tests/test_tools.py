"""Wiring: the Agent carries the training tools; the runtime passes context."""

from types import SimpleNamespace

import agentg.runtime as runtime_module
from agentg.agent import build_agent
from agentg.config import Settings
from agentg.db import create_engine
from agentg.messages import IncomingMessage
from agentg.linking import Linking
from agentg.runtime import AgentRuntime
from agentg.context import MemberContext
from agentg.stores import Stores
from agentg.tools import _logged
from agentg.training import LoggedSets
from conftest import unused_phraser


async def null_summarizer(old_items, existing_notes):
    raise AssertionError("compaction should not trigger in this test")

EXPECTED_TOOLS = {
    "open_session",
    "log_sets",
    "copy_last_sets",
    "edit_logged_sets",
    "get_last_sets",
    "close_session",
    "remember_note",
    "retire_note",
    "get_rules_doc",
    "list_exercises",
    "save_routine",
    "get_routine",
    "suggest_weights",
    "update_rules_doc",
    "write_routine",
    "stop_checkins",
    "snooze_checkins",
    "resume_checkins",
    "show_demo",
    "flag_to_coach",
}


def test_the_agent_carries_the_session_loop_tools():
    settings = Settings(
        telegram_bot_token="123:abc",
        model="openai/gpt-4o-mini",
        model_api_key="sk-test",
        database_url="sqlite+aiosqlite://",
    )
    agent = build_agent(settings)
    assert {tool.name for tool in agent.tools} == EXPECTED_TOOLS


def test_log_sets_payload_surfaces_a_suspect_hint():
    # The Agent only sees the tool payload — suspect must ride along there.
    payload = LoggedSets(
        exercise="bench press",
        weight=121.0,
        reps=[5, 5, 5],
        previous={"weight": 60.0, "reps": [8, 8, 8]},
        suspect="121 is more than 2× last time's 60 — double-check with the Member",
    )
    assert _logged(payload, "kg")["suspect"] == payload.suspect


def test_log_sets_payload_omits_suspect_when_the_jump_is_plausible():
    payload = LoggedSets(
        exercise="bench press",
        weight=62.5,
        reps=[8, 8, 8],
        previous={"weight": 60.0, "reps": [8, 8, 7]},
    )
    assert "suspect" not in _logged(payload, "kg")


async def test_the_runtime_hands_tools_the_members_context(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        seen["context"] = context
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    stores = Stores.from_engine(engine)
    runtime = AgentRuntime(
        agent=object(),
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, unused_phraser),
        summarizer=null_summarizer,
        stream_replies=False,
    )
    await runtime.ensure_schema()
    gym = await stores.linking.create_gym("Iron Temple")
    member = await stores.linking.link_member(gym.id, "Dani", "telegram", "42")

    await runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="I'm here", is_private=True)
    )

    context = seen["context"]
    assert isinstance(context, MemberContext)
    assert context.member_id == member.id
    assert context.gym_id == gym.id
    assert context.weight_unit == "kg"
    assert context.stores is runtime.stores
    await engine.dispose()
