"""The runtime serves queued demos after the turn (channel-agnostically)."""

from types import SimpleNamespace

import pytest

import agentg.runtime as runtime_module
from agentg.db import create_engine
from agentg.demo_media import SentAnimation
from agentg.messages import IncomingMessage
from agentg.linking import Linking
from agentg.runtime import AgentRuntime
from agentg.stores import Stores
from conftest import unused_phraser


class FakeSender:
    def __init__(self):
        self.sent = []

    @property
    def cache_namespace(self):
        return "bot-1"

    async def send_animation(self, channel, channel_user_id, slug, cached_file_id):
        self.sent.append((channel, channel_user_id, slug))
        return SentAnimation(file_id=f"id-{slug}")


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'rt.db'}")
    stores = Stores.from_engine(engine)
    sender = FakeSender()
    runtime = AgentRuntime(
        agent=object(),
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, unused_phraser),
        summarizer=None,
        demo_sender=sender,
    )
    await runtime.ensure_schema()
    gym = await stores.linking.create_gym("Iron Temple")
    await stores.linking.link_member(gym.id, "Dani", "telegram", "42")
    await stores.demos.set_default_demo("goblet squat", "goblet-squat.mp4")

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.runtime = runtime
    env.demos = stores.demos
    env.sender = sender
    yield env
    await engine.dispose()


async def test_a_queued_demo_is_sent_after_the_reply(env, monkeypatch):
    async def fake_run(agent, text, *, session, context=None):
        # the show_demo tool would append the resolved exercise name
        context.demo_requests.append("goblet squat")
        return SimpleNamespace(final_output="on its way — knees out, chest tall!")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    reply = await env.runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="how do I goblet squat?")
    )

    assert "way" in reply
    assert env.sender.sent == []  # not sent yet — deferred until after the text
    await reply.after_send()  # the channel runs this once the reply text is out
    assert env.sender.sent == [("telegram", "42", "goblet-squat.mp4")]


async def test_no_send_when_nothing_was_queued(env, monkeypatch):
    async def fake_run(agent, text, *, session, context=None):
        return SimpleNamespace(final_output="hey!")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)
    reply = await env.runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="hi")
    )
    assert getattr(reply, "after_send", None) is None  # nothing deferred
    assert env.sender.sent == []
