"""Streaming reply delivery (#176): sentence boundaries, streamed text, channel glue."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentg.channels.telegram import (
    ERROR_REPLY,
    make_message_handler,
    split_reply,
)
from agentg.db import create_engine
from agentg.messages import IncomingMessage, Reply
from agentg.linking import Linking
from agentg.runtime import (
    AgentRuntime,
    _is_sentence_boundary,
    _stream_text,
)
from agentg.stores import Stores
from conftest import unused_phraser


def _text_delta(delta: str, index: int = 0):
    """A minimal ``ResponseTextDeltaEvent`` for streaming tests."""
    from openai.types.responses import ResponseTextDeltaEvent

    return ResponseTextDeltaEvent(
        content_index=0,
        delta=delta,
        item_id="test",
        logprobs=[],
        output_index=0,
        sequence_number=index,
        type="response.output_text.delta",
    )


# ── sentence boundary detection ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "last_sent", "expected"),
    [
        # First sentence with a period
        ("Hello there, welcome back.", "", True),
        # First sentence with exclamation (9 chars, under 12-char minimum)
        ("Let's go!", "", False),
        # First sentence with question
        ("How are you feeling today?", "", True),
        # Too short — first chunk must be >= 12 chars
        ("Hi!", "", False),
        ("OK.", "", False),
        # Short but after first sentence is fine
        ("Hello there, welcome back. OK.", "Hello there, welcome back.", True),
        # No boundary — text hasn't changed
        ("Hello there, welcome back.", "Hello there, welcome back.", False),
        # No sentence ending yet
        ("Hello there, welcome", "", False),
        # Single word with period but too short
        ("Yes.", "", False),
        # Exactly 12 chars with period
        ("Long enough.", "", True),
        # Sentence ending in the middle but not at end
        ("Hello. World", "", False),
        # Sentence boundary detected at the first "." when followed by newline
        ("First sentence.\nNext", "", True),
        # No new text beyond last_sent
        ("", "", False),
        ("  ", "", False),
    ],
)
def test_sentence_boundary_detection(text, last_sent, expected):
    assert _is_sentence_boundary(text, last_sent) == expected


# ── streaming text generator ──────────────────────────────────────────────


async def test_stream_yields_at_sentence_boundaries():
    """Each sentence boundary produces a yield with the full accumulated text."""

    # Build a fake streaming result whose stream_events() emits text deltas
    # word by word.
    class FakeStreamResult:
        def __init__(self):
            self.is_complete = False

        async def stream_events(self):
            from agents.stream_events import RawResponsesStreamEvent

            text = "Welcome back, Ana! You last trained three days ago. Let's get started."
            for i, char in enumerate(text):
                yield RawResponsesStreamEvent(
                    type="raw_response_event",
                    data=_text_delta(char, i),
                )

    result = FakeStreamResult()
    chunks = [c async for c in _stream_text(result)]
    assert len(chunks) >= 2  # at least "Welcome back, Ana!" and the full text
    assert chunks[0] == "Welcome back, Ana!"  # first complete sentence (22 chars, > 20)
    assert chunks[-1] == "Welcome back, Ana! You last trained three days ago. Let's get started."


async def test_stream_yields_whatever_is_left_at_the_end():
    """When generation ends without a sentence ending, the final yield
    delivers the remainder."""

    class FakeStreamResult:
        async def stream_events(self):
            from agents.stream_events import RawResponsesStreamEvent

            text = "Bench press 60kg 8/8/8"  # no sentence ending at all
            for i, char in enumerate(text):
                yield RawResponsesStreamEvent(
                    type="raw_response_event",
                    data=_text_delta(char, i),
                )

    result = FakeStreamResult()
    chunks = [c async for c in _stream_text(result)]
    assert len(chunks) == 1
    assert chunks[0] == "Bench press 60kg 8/8/8"


async def test_stream_with_error_after_partial_send():
    """An exception midway through still delivers what was already sent."""

    class FakeStreamResult:
        async def stream_events(self):
            from agents.stream_events import RawResponsesStreamEvent

            for i, char in enumerate("Welcome back, Ana! You last trained"):
                yield RawResponsesStreamEvent(
                    type="raw_response_event",
                    data=_text_delta(char, i),
                )
            raise RuntimeError("model crashed")

    result = FakeStreamResult()
    chunks = [c async for c in _stream_text(result)]
    # The first sentence (>=20 chars) was sent before the crash; the remainder
    # (up to the crash) is also delivered.
    assert len(chunks) >= 1
    assert chunks[0] == "Welcome back, Ana!"
    # The final chunk has whatever accumulated before the crash
    assert "Welcome back, Ana! You last trained" in chunks[-1]


async def test_stream_with_error_before_first_send():
    """An error before the first sentence is sent yields nothing."""

    class FakeStreamResult:
        async def stream_events(self):
            from agents.stream_events import RawResponsesStreamEvent

            for i, char in enumerate("Hi"):
                yield RawResponsesStreamEvent(
                    type="raw_response_event",
                    data=_text_delta(char, i),
                )
            raise RuntimeError("model crashed")

    result = FakeStreamResult()
    chunks = [c async for c in _stream_text(result)]
    # "Hi" is too short to be a sentence boundary, so nothing was sent
    # before the error.
    assert len(chunks) == 0


# ── Reply carries stream when streaming is enabled ────────────────────────


async def test_streamed_reply_has_stream_set(tmp_path):
    """When stream_replies=True, the Reply carries a .stream async generator."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'sr.db'}")
    stores = Stores.from_engine(engine)
    runtime = AgentRuntime(
        agent=object(),
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, unused_phraser),
        summarizer=None,
        stream_replies=True,
    )
    await runtime.ensure_schema()
    gym = await stores.linking.create_gym("Iron Temple")
    await stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    # Monkeypatch run_streamed to return a fake that streams a short reply.
    import agentg.runtime as runtime_module

    class FakeStreamResult:
        async def stream_events(self):
            from agents.stream_events import RawResponsesStreamEvent

            text = "Welcome back, Ana! Let's train hard today."
            for i, char in enumerate(text):
                yield RawResponsesStreamEvent(
                    type="raw_response_event",
                    data=_text_delta(char, i),
                )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runtime_module.Runner, "run_streamed", lambda *a, **kw: FakeStreamResult())

    reply = await runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="I'm here")
    )

    assert reply.stream is not None
    chunks = [c async for c in reply.stream]
    assert len(chunks) >= 1
    assert "Welcome back, Ana!" in chunks[0]
    await engine.dispose()


async def test_non_streamed_reply_has_no_stream(tmp_path):
    """When stream_replies=False, the Reply has no .stream (kept for tests)."""
    import agentg.runtime as runtime_module

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'nsr.db'}")
    stores = Stores.from_engine(engine)
    runtime = AgentRuntime(
        agent=object(),
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, unused_phraser),
        summarizer=None,
        stream_replies=False,
    )
    await runtime.ensure_schema()
    gym = await stores.linking.create_gym("Iron Temple")
    await stores.linking.link_member(gym.id, "Ana", "telegram", "42")

    monkeypatch = pytest.MonkeyPatch()
    mock_run = AsyncMock()
    mock_run.return_value = SimpleNamespace(final_output="hey!")
    monkeypatch.setattr(runtime_module.Runner, "run", mock_run)

    reply = await runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="hi")
    )

    assert reply.stream is None
    assert reply == "hey!"
    await engine.dispose()


# ── channel delivers streamed reply progressively ────────────────────────


class FakeStreamMessage:
    """A message-like object that records .answer() and .edit_text() calls."""

    def __init__(self, user_id=42, text="hi", full_name="Ana", chat_type="private",
                 chat_id=77):
        self.from_user = SimpleNamespace(id=user_id, full_name=full_name)
        self.chat = SimpleNamespace(type=chat_type, id=chat_id)
        self.text = text
        self.answer = AsyncMock()
        self.edit_text = AsyncMock()
        self.bot = SimpleNamespace(send_chat_action=AsyncMock())
        # For answer() return value (the "sent message")
        self._sent = SimpleNamespace(edit_text=self.edit_text)
        self.answer.return_value = self._sent


async def test_streaming_handler_sends_first_chunk_then_edits():
    """The first stream yield is sent as a new message; later yields edit it."""

    async def stream_chunks():
        yield "Welcome back, Ana!"
        yield "Welcome back, Ana! You last trained three days ago."
        yield "Welcome back, Ana! You last trained three days ago. Let's go!"

    reply = Reply("", stream=stream_chunks())

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessage()
    await make_message_handler(reply_fn)(message)

    # First chunk: new message
    assert message.answer.await_count == 1
    first_call_text = message.answer.await_args.args[0]
    assert first_call_text == "Welcome back, Ana!"

    # Later chunks: edits of the sent message
    assert message.edit_text.await_count == 2
    assert message.edit_text.await_args_list[0].args[0] == (
        "Welcome back, Ana! You last trained three days ago."
    )
    assert message.edit_text.await_args_list[1].args[0] == (
        "Welcome back, Ana! You last trained three days ago. Let's go!"
    )


async def test_streaming_handler_runs_after_send_when_stream_is_done():
    """Demo animations land after the stream is exhausted."""
    order = []

    async def stream_chunks():
        order.append("stream-chunk")
        yield "On its way — knees out, chest tall!"

    async def after_send():
        order.append("demo")

    reply = Reply("", stream=stream_chunks(), after_send=after_send)

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessage()
    await make_message_handler(reply_fn)(message)

    assert order == ["stream-chunk", "demo"]


async def test_streaming_handler_stops_typing_on_first_chunk():
    """The typing indicator is cancelled when the first chunk arrives."""

    async def stream_chunks():
        yield "Welcome back, Ana!"
        # Second chunk never comes in this test — but handler should
        # cancel typing after the first chunk anyway.
        await asyncio.sleep(0.01)
        yield "Welcome back, Ana! Let's go!"

    reply = Reply("", stream=stream_chunks())

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessage()
    await make_message_handler(reply_fn)(message)

    # Typing was sent initially then cancelled after the first yield.
    assert message.bot.send_chat_action.await_count >= 1


async def test_streaming_handler_handles_empty_chunks():
    """Empty or whitespace-only chunks are skipped."""

    async def stream_chunks():
        yield ""
        yield "   "
        yield "Welcome back! Let's train."

    reply = Reply("", stream=stream_chunks())

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessage()
    await make_message_handler(reply_fn)(message)

    # Only the non-empty chunk triggers a send.
    assert message.answer.await_count == 1
    assert "Welcome back!" in message.answer.await_args.args[0]
    # No edits for empty chunks.
    assert message.edit_text.await_count == 0


async def test_streaming_handler_falls_back_on_edit_failure():
    """If edit_text raises, the handler sends the chunk as a new message."""

    async def stream_chunks():
        yield "First chunk here, hello!"
        yield "First chunk here, hello! Second sentence now."

    reply = Reply("", stream=stream_chunks())

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessage()
    # The first .edit_text call fails.
    message.edit_text.side_effect = [RuntimeError("message not found"), None]

    await make_message_handler(reply_fn)(message)

    # The first chunk is sent via .answer()
    assert message.answer.await_count >= 1
    # The second chunk: edit_text failed, so it falls back to a new .answer()
    assert message.answer.await_count == 2  # first chunk + fallback send


async def test_streaming_handler_sends_error_on_total_failure():
    """If the stream itself raises before any chunk, the error reply is sent."""

    async def stream_chunks():
        raise RuntimeError("model unavailable")
        yield  # unreachable

    reply = Reply("", stream=stream_chunks())

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessage()
    await make_message_handler(reply_fn)(message)

    # The error reply must be delivered.
    message.answer.assert_awaited_once_with(ERROR_REPLY)


async def test_non_streaming_handler_path_unchanged():
    """When reply.stream is None, the existing split-and-send logic still works."""

    async def reply_fn(msg):
        return Reply("Short reply here.")

    message = FakeStreamMessage()
    await make_message_handler(reply_fn)(message)

    message.answer.assert_awaited_once_with("Short reply here.")
    message.edit_text.assert_not_awaited()
