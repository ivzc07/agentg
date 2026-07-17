"""Telegram adapter glue: handler wiring, /start payloads, chunking, fallback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentg.channels.telegram import (
    ERROR_REPLY,
    MAX_MESSAGE_LENGTH,
    create_dispatcher,
    make_message_handler,
    parse_start_payload,
    split_reply,
)


class FakeMessage:
    def __init__(self, user_id=42, text="hi", full_name="Ana García"):
        self.from_user = (
            SimpleNamespace(id=user_id, full_name=full_name) if user_id is not None else None
        )
        self.text = text
        self.answer = AsyncMock()


@pytest.mark.parametrize(
    ("text", "payload"),
    [
        ("/start abc123", "abc123"),
        ("/start", ""),
        ("/start@GymCoachBot abc123", "abc123"),
        ("/start   abc123  ", "abc123"),
        ("bench 60 8,8,8", None),
        ("/started nope", None),
    ],
)
def test_start_payload_parsing(text, payload):
    assert parse_start_payload(text) == payload


async def test_handler_passes_the_incoming_message_and_sends_the_reply():
    calls = {}

    async def reply_fn(msg):
        calls["msg"] = msg
        return "welcome back!"

    message = FakeMessage(user_id=42, text="I'm here")
    await make_message_handler(reply_fn)(message)

    msg = calls["msg"]
    assert msg.channel == "telegram"
    assert msg.channel_user_id == "42"  # the numeric id, never the @username
    assert msg.text == "I'm here"
    assert msg.display_name == "Ana García"
    assert msg.link_code is None
    message.answer.assert_awaited_once_with("welcome back!")


async def test_handler_extracts_the_deep_link_payload():
    calls = {}

    async def reply_fn(msg):
        calls["msg"] = msg
        return "hi"

    await make_message_handler(reply_fn)(FakeMessage(text="/start gym-code"))

    assert calls["msg"].link_code == "gym-code"


async def test_handler_ignores_messages_without_a_sender():
    reply_fn = AsyncMock()
    await make_message_handler(reply_fn)(FakeMessage(user_id=None))
    reply_fn.assert_not_awaited()


async def test_handler_answers_even_when_the_agent_loop_fails():
    async def reply_fn(msg):
        raise RuntimeError("model unavailable")

    message = FakeMessage()
    await make_message_handler(reply_fn)(message)
    message.answer.assert_awaited_once_with(ERROR_REPLY)


async def test_empty_reply_still_sends_something():
    async def reply_fn(msg):
        return ""

    message = FakeMessage()
    await make_message_handler(reply_fn)(message)
    assert message.answer.await_count == 1


async def test_after_send_runs_only_once_the_reply_text_is_out():
    from agentg.messages import Reply

    order = []

    async def after_send():
        order.append("demo")

    async def reply_fn(msg):
        return Reply("on its way!", after_send=after_send)

    message = FakeMessage()
    message.answer.side_effect = lambda *a, **k: order.append("text")
    await make_message_handler(reply_fn)(message)

    assert order == ["text", "demo"]  # the animation lands beneath the reply


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
