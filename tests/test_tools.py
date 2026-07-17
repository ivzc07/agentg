"""Wiring: the Agent carries the training tools; the runtime passes context."""

from types import SimpleNamespace

import agentg.runtime as runtime_module
from agentg.agent import build_agent
from agentg.config import Settings
from agentg.db import create_engine
from agentg.messages import IncomingMessage
from agentg.onboarding import Onboarding
from agentg.runtime import AgentRuntime
from agentg.store import LinkingStore
from agentg.tools import MemberContext
from agentg.training import TrainingStore

EXPECTED_TOOLS = {
    "open_session",
    "log_sets",
    "copy_last_sets",
    "edit_logged_sets",
    "get_last_sets",
    "close_session",
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


async def test_the_runtime_hands_tools_the_members_context(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(agent, text, *, session, context=None):
        seen["context"] = context
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    store = LinkingStore(engine)
    runtime = AgentRuntime(
        agent=object(),
        engine=engine,
        store=store,
        onboarding=Onboarding(store),
        training=TrainingStore(engine),
    )
    await runtime.ensure_schema()
    gym = await store.create_gym("Iron Temple")
    member = await store.link_member(gym.id, "Dani", "telegram", "42")

    await runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="I'm here")
    )

    context = seen["context"]
    assert isinstance(context, MemberContext)
    assert context.member_id == member.id
    assert context.gym_id == gym.id
    assert context.weight_unit == "kg"
    assert context.training is runtime.training
    await engine.dispose()
