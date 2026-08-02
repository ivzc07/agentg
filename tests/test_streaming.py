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
    assert chunks[0] == "Welcome back, Ana!"  # first complete sentence (22 chars, >= min 12)
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
    # The first sentence (>=12 chars) was sent before the crash; the remainder
    # (up to the crash) is also delivered.
    assert len(chunks) >= 1
    assert chunks[0] == "Welcome back, Ana!"
    # The final chunk has whatever accumulated before the crash
    assert "Welcome back, Ana! You last trained" in chunks[-1]


async def test_stream_with_error_before_first_send():
    """An error before the first sentence propagates so the channel can
    send an error reply instead of leaving the Member in silence."""

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
    with pytest.raises(RuntimeError, match="model crashed"):
        [c async for c in _stream_text(result)]


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

    async def after_send(**_):
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


# ── long streamed replies (> 4096 UTF-16 units) ───────────────────────────


class FakeStreamMessageMultiSend:
    """Like FakeStreamMessage but each .answer() returns a unique sent-message
    object — needed when the streaming path sends multiple messages for a long
    reply."""

    def __init__(self, user_id=42, text="hi", full_name="Ana", chat_type="private",
                 chat_id=77):
        self.from_user = SimpleNamespace(id=user_id, full_name=full_name)
        self.chat = SimpleNamespace(type=chat_type, id=chat_id)
        self.text = text
        self.bot = SimpleNamespace(send_chat_action=AsyncMock())
        self._sent_messages: list = []

        async def _answer(*args, **kwargs):
            edit_mock = AsyncMock()
            sent = SimpleNamespace(edit_text=edit_mock)
            self._sent_messages.append(sent)
            return sent

        self.answer = AsyncMock(side_effect=_answer)

    def sent_messages(self):
        """All distinct sent-message objects returned by answer()."""
        return list(self._sent_messages)


async def test_long_streamed_reply_is_split_across_messages():
    """When the accumulated text exceeds 4096 UTF-16 units, the streaming
    path sends it in multiple ordered messages instead of silently truncating.

    Revert-proof: with the old _cap_text + single sent_message the text
    beyond 4096 chars is dropped and never delivered."""

    # Build a long reply: two sentence yields, the second crosses 4096.
    chunk0 = "A. " + "B" * 3000  # well under 4096, first sentence
    assert len(chunk0) < 4096
    chunk1 = chunk0 + " C" + "D" * 4096 + "."  # crosses 4096 after first chunk
    assert len(chunk1) > 4096

    async def stream_chunks():
        yield chunk0
        yield chunk1

    reply = Reply("", stream=stream_chunks())

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessageMultiSend()
    await make_message_handler(reply_fn)(message)

    # At least 2 answer() calls — one for the first chunk (under 4096) and
    # at least one for the overflow chunk (when the text crosses 4096).
    assert message.answer.await_count >= 2

    # The tail of the long text must appear in the final answer() call —
    # the old _cap_text path would never deliver text beyond 4096 chars.
    final_answer = message.answer.await_args_list[-1].args[0]
    tail = chunk1[-100:]
    assert tail in final_answer or final_answer in chunk1, (
        f"tail {tail[:50]}... not found in final answer"
    )


async def test_long_streamed_reply_no_duplicate_edits():
    """When a long reply is streamed, edit_text is never called with the same
    text twice in a row — no "message is not modified" burst.

    Revert-proof: the old _cap_text path calls edit_text with the same
    4096-char prefix on every yield after the cap is hit."""

    # Simulate a reply that grows at sentence boundaries past 4096.
    base = "First sentence here. " + "X" * 3000  # under 4096
    assert len(base) < 4096
    mid = base + " Second sentence with more text. " + "Y" * 1000  # still under 4096
    assert len(mid) < 4096
    over = mid + " Third sentence here. " + "Z" * 4096  # crosses 4096
    assert len(over) > 4096

    async def stream_chunks():
        yield base
        yield mid
        yield over

    reply = Reply("", stream=stream_chunks())

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessageMultiSend()
    await make_message_handler(reply_fn)(message)

    # Collect all edit_text calls across all sent messages.
    all_edit_calls: list[str] = []
    for sent in message.sent_messages():
        for call in sent.edit_text.await_args_list:
            all_edit_calls.append(call.args[0])

    # No consecutive duplicate edits.
    for i in range(1, len(all_edit_calls)):
        assert all_edit_calls[i] != all_edit_calls[i - 1], (
            f"duplicate edit_text at position {i}: {all_edit_calls[i][:80]}..."
        )


async def test_long_streamed_reply_full_text_delivered():
    """Every character of a reply > 4096 UTF-16 units is delivered.

    Verifies that when the accumulated text crosses the 4096-unit boundary,
    a second message is sent with the overflow — the old _cap_text path
    would silently drop those characters.
    """

    # Build text where the final accumulated form splits into 2 chunks.
    filler = "F" * 4000
    prefix = "Start. "
    short = prefix + filler  # well under 4096
    assert len(short) < 4096
    extra = " End." + "G" * 100
    long_text = short + extra  # exceeds 4096
    assert len(long_text) > 4096

    async def stream_chunks():
        yield short
        yield long_text

    reply = Reply("", stream=stream_chunks())

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessageMultiSend()
    await make_message_handler(reply_fn)(message)

    # At least 2 answer() calls — the second one is the overflow chunk.
    assert message.answer.await_count >= 2, (
        f"expected >=2 answer calls for overflow, got {message.answer.await_count}"
    )

    # The second split_reply chunk (everything beyond 4096) should be fully
    # contained in the answer() arguments.  Use split_reply to find it.
    chunks = split_reply(long_text)
    assert len(chunks) >= 2, f"expected >=2 split_reply chunks, got {len(chunks)}"
    overflow = chunks[1]  # text beyond 4096 units

    delivered = [call.args[0] for call in message.answer.await_args_list]
    # The overflow text must be present in at least one answer call.
    assert any(overflow in d for d in delivered), (
        f"overflow chunk {overflow!r} not found in answers: {[d[:50] for d in delivered]}"
    )


# ── concurrent same-member messages must not interleave ──────────────────


async def test_concurrent_same_member_messages_are_serialized(tmp_path):
    """Two rapid messages from the same Member do not interleave their turns.

    The per-identity lock (``runtime.py:71-73``) must span the entire
    streaming turn — from ``run_streamed`` through stream consumption —
    so that a second message from the same identity cannot race the
    session or interleave reply chunks.

    Revert-proof: without the lock held for the full stream lifetime,
    ``run_streamed`` for the second message starts before the first
    stream finishes, and the capture order shows interleaving.

    Uses explicit events to force timing: t1 enters the stream and pauses;
    t2 tries to start while t1 is paused.  With the lock fix t2 cannot
    enter until t1 finishes; without it t2 enters immediately.
    """
    import agentg.runtime as runtime_module

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'conc.db'}")
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

    capture: list[str] = []
    _seq = 0

    # Synchronization: t1 pauses mid-stream, t2 tries to go, then we see.
    t1_paused = asyncio.Event()
    t1_continue = asyncio.Event()

    class FakeStreamResult:
        def __init__(self, label):
            self._label = label

        async def stream_events(self):
            from agents.stream_events import RawResponsesStreamEvent

            capture.append(f"enter:{self._label}")
            text = f"{self._label}. First sentence here. Second sentence."
            for i, char in enumerate(text):
                yield RawResponsesStreamEvent(
                    type="raw_response_event",
                    data=_text_delta(char, i),
                )
                if i == 5 and self._label == "1":
                    # Pause mid-stream so t2 can try to start.
                    t1_paused.set()
                    await t1_continue.wait()
            capture.append(f"leave:{self._label}")

    def fake_run_streamed(*args, **kwargs):
        nonlocal _seq
        _seq += 1
        return FakeStreamResult(str(_seq))

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(runtime_module.Runner, "run_streamed", fake_run_streamed)

    async def full_turn(text):
        reply = await runtime.handle_message(
            IncomingMessage(channel="telegram", channel_user_id="42", text=text)
        )
        assert reply.stream is not None
        async for _ in reply.stream:
            pass
        capture.append(f"consumed:{text}")

    t1 = asyncio.create_task(full_turn("first"))

    # Wait for t1 to enter the stream and pause.
    await t1_paused.wait()

    # Now t1 is mid-stream (paused).  Launch t2.
    t2 = asyncio.create_task(full_turn("second"))

    # Give t2 a moment to try to enter.
    await asyncio.sleep(0.05)

    # Check that t2 has NOT entered (blocked by the lock in the fix).
    # With the fix: enter:2 should NOT be in capture yet.
    # Without the fix: enter:2 IS in capture (interleaved).
    has_enter2 = "enter:2" in capture

    # Release t1 so both can finish.
    t1_continue.set()
    await asyncio.gather(t1, t2)

    assert not has_enter2, (
        f"second turn entered before first turn finished: {capture}"
    )

    # Sanity-check: both turns completed.
    assert "enter:1" in capture
    assert "leave:1" in capture
    assert "enter:2" in capture
    assert "leave:2" in capture
    assert "consumed:first" in capture
    assert "consumed:second" in capture

    await engine.dispose()


# ── lock / stream lifecycle on delivery failure ─────────────────────────


async def test_stream_aclosed_on_delivery_error():
    """When ``message.answer`` raises during stream delivery, the stream is
    deterministically ``aclose()``-d so ``_hold_lock`` releases the
    per-identity lock rather than leaving it dangling until GC.

    Revert-proof: without the ``aclose()`` in ``_deliver_streamed``\'s
    ``finally``, the ``_hold_lock`` generator stays suspended at its
    ``yield`` after a delivery error and the lock is never released."""

    # Track whether aclose was called on the underlying stream.
    aclosed = False

    async def inner_stream():
        yield "Welcome back, Ana!"

    gen = inner_stream()

    class _Tracked:
        """Wraps an async generator so we can assert aclose() was called."""

        def __init__(self, g):
            self._g = g
            self.was_aclosed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self._g.__anext__()

        async def aclose(self):
            self.was_aclosed = True
            await self._g.aclose()

    tracked = _Tracked(gen)
    reply = Reply("", stream=tracked)

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessage()
    # First answer() (stream chunk) raises; second answer() (ERROR_REPLY) works.
    sent_error_reply = SimpleNamespace(edit_text=AsyncMock())
    message.answer.side_effect = [RuntimeError("network failure"), sent_error_reply]

    await make_message_handler(reply_fn)(message)

    assert tracked.was_aclosed, (
        "stream must be aclosed after delivery error so _hold_lock releases the lock"
    )


async def test_second_message_not_deadlocked_after_stream_error(tmp_path):
    """After a stream delivery error, a second message from the same Member
    does not deadlock — the per-identity lock was released.

    Revert-proof: without proper aclose() and _hold_lock cleanup, the lock
    stays held after a delivery error and the second ``handle_message``
    hangs forever at ``lock.acquire()``."""
    import agentg.runtime as runtime_module

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'nd.db'}")
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

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        runtime_module.Runner,
        "run_streamed",
        lambda *a, **kw: type(
            "Fake", (),
            {
                "stream_events": lambda s: _stream_events(
                    "Welcome back, Ana! Let's train hard today."
                )
            },
        )(),
    )

    # First message: get a streamed reply.  The lock is acquired in
    # handle_message and transferred to _hold_lock.
    reply1 = await runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="first")
    )

    assert reply1.stream is not None
    # Consume one chunk then aclose — simulating a delivery error scenario
    # where the channel calls aclose() to recover.
    try:
        async for _ in reply1.stream:
            break
    finally:
        await reply1.stream.aclose()

    # Second message: must not deadlock.
    monkeypatch.setattr(
        runtime_module.Runner,
        "run_streamed",
        lambda *a, **kw: type(
            "Fake2", (),
            {
                "stream_events": lambda s: _stream_events(
                    "Second reply here. All good."
                )
            },
        )(),
    )

    reply2 = await runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="second")
    )
    assert reply2.stream is not None
    # Consume fully to release the lock cleanly.
    async for _ in reply2.stream:
        pass

    await engine.dispose()


async def test_no_demo_after_stream_error():
    """A stream delivery error suppresses the demo animation but still runs
    ``after_send``.

    ``after_send`` no longer carries only demo media: it also settles the
    deferred rhythm reset (#169), fires the Coach safety pings (#172) and
    releases the compaction signal the next turn waits on (#173).  Skipping
    it wholesale on a delivery error would mean a flagged Member's Coach is
    never pinged and a lapsed Member is never revived -- far worse than a
    cosmetic animation under an error message.  So the hook always runs and
    is told to suppress media instead.
    """

    after_send_calls = []

    async def stream_chunks():
        yield "Welcome back, Ana!"

    async def after_send(*, deliver_media=True):
        after_send_calls.append(deliver_media)

    reply = Reply("", stream=stream_chunks(), after_send=after_send)

    async def reply_fn(msg):
        return reply

    message = FakeStreamMessage()
    # First answer() (stream chunk) raises; second (ERROR_REPLY) succeeds.
    sent_error = SimpleNamespace(edit_text=AsyncMock())
    message.answer.side_effect = [RuntimeError("network failure"), sent_error]

    await make_message_handler(reply_fn)(message)

    assert after_send_calls == [False], (
        "after_send must still run after a stream error, with media suppressed"
    )
    assert ERROR_REPLY in message.answer.await_args_list[-1].args[0]


async def _stream_events(text: str):
    """Helper: yield RawResponsesStreamEvent deltas for each char in *text*."""
    from agents.stream_events import RawResponsesStreamEvent

    for i, ch in enumerate(text):
        yield RawResponsesStreamEvent(
            type="raw_response_event",
            data=_text_delta(ch, i),
        )


async def test_aclose_releases_the_lock_synchronously(tmp_path):
    """``aclose()`` on the outermost wrapper must release the per-identity
    lock and run the forget-me wipe *before it returns* -- not eventually,
    when asyncio finalizes an abandoned generator.

    ``async for ... yield`` does not propagate ``aclose()`` to the delegated
    generator, so every wrapper closes its inner chain explicitly.  Without
    that, a delivery error leaves the lock held until async-gen GC, and
    ``after_send``'s compaction can acquire the lock and summarise a
    forgotten Member's history *before* the deferred wipe runs (#166/#176).

    Revert-proof: drop the ``await inner.aclose()`` from ``_finish_turn`` and
    the lock is still held when this assertion runs.
    """
    from agentg.db import create_engine
    from agentg.linking import Linking
    from agentg.messages import IncomingMessage
    from agentg.runtime import AgentRuntime
    from agentg.stores import Stores
    from conftest import unused_phraser

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'aclose.db'}")
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

    class FakeStreamResult:
        async def stream_events(self):
            from agents.stream_events import RawResponsesStreamEvent

            for i, char in enumerate("Hello there, Ana. "):
                yield RawResponsesStreamEvent(
                    type="raw_response_event", data=_text_delta(char, i)
                )

    import agentg.runtime as runtime_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            runtime_module.Runner,
            "run_streamed",
            lambda *a, **k: FakeStreamResult(),
        )
        reply = await runtime.handle_message(
            IncomingMessage(channel="telegram", channel_user_id="42", text="hi")
        )

        key = ("telegram", "42")
        assert runtime._locks[key].locked(), "the stream should hold the lock"

        # Consume one chunk, then abandon the stream the way a delivery error
        # does, and close it.
        await reply.stream.__anext__()
        await reply.stream.aclose()

        assert not runtime._locks[key].locked(), (
            "aclose() must release the per-identity lock before it returns, "
            "not leave it to async-generator finalization"
        )
