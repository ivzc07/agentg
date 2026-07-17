"""Telegram adapter glue: handler wiring, reply chunking, failure fallback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from agentg.channels.telegram import (
    ERROR_REPLY,
    MAX_MESSAGE_LENGTH,
    create_dispatcher,
    make_message_handler,
    split_reply,
)


class FakeMessage:
    def __init__(self, user_id=42, text="hi"):
        self.from_user = SimpleNamespace(id=user_id) if user_id is not None else None
        self.text = text
        self.answer = AsyncMock()


async def test_handler_passes_channel_identity_and_sends_the_reply():
    calls = {}

    async def reply_fn(channel, channel_user_id, text):
        calls["args"] = (channel, channel_user_id, text)
        return "welcome back!"

    message = FakeMessage(user_id=42, text="I'm here")
    await make_message_handler(reply_fn)(message)

    assert calls["args"] == ("telegram", "42", "I'm here")
    message.answer.assert_awaited_once_with("welcome back!")


async def test_handler_ignores_messages_without_a_sender():
    reply_fn = AsyncMock()
    await make_message_handler(reply_fn)(FakeMessage(user_id=None))
    reply_fn.assert_not_awaited()


async def test_handler_answers_even_when_the_agent_loop_fails():
    async def reply_fn(channel, channel_user_id, text):
        raise RuntimeError("model unavailable")

    message = FakeMessage()
    await make_message_handler(reply_fn)(message)
    message.answer.assert_awaited_once_with(ERROR_REPLY)


async def test_empty_reply_still_sends_something():
    async def reply_fn(channel, channel_user_id, text):
        return ""

    message = FakeMessage()
    await make_message_handler(reply_fn)(message)
    assert message.answer.await_count == 1


def test_short_reply_is_a_single_chunk():
    assert split_reply("hello") == ["hello"]


def test_long_replies_are_split_within_the_telegram_limit():
    text = "x" * (MAX_MESSAGE_LENGTH * 2 + 5)
    chunks = split_reply(text)
    assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "".join(chunks) == text


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_split_counts_utf16_units_the_way_telegram_does():
    text = "💪" * (MAX_MESSAGE_LENGTH + 10)  # each emoji is 2 UTF-16 units
    chunks = split_reply(text)
    assert all(utf16_units(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "".join(chunks) == text


def test_dispatcher_registers_one_message_handler():
    dispatcher = create_dispatcher(AsyncMock())
    assert len(dispatcher.message.handlers) == 1
