"""Telegram adapter glue: handler wiring, /start payloads, chunking, fallback."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentg.channels.telegram import (
    ERROR_REPLY,
    MAX_MESSAGE_LENGTH,
    TelegramNotifier,
    create_dispatcher,
    make_message_handler,
    parse_start_payload,
    split_reply,
)
from agentg.messages import Reply


class FakeMessage:
    def __init__(self, user_id=42, text="hi", full_name="Ana García", chat_type="private",
                 chat_id=77, bot_send_chat_action=None):
        self.from_user = (
            SimpleNamespace(id=user_id, full_name=full_name) if user_id is not None else None
        )
        self.chat = SimpleNamespace(type=chat_type, id=chat_id)
        self.text = text
        self.answer = AsyncMock()
        self.bot = SimpleNamespace(
            send_chat_action=bot_send_chat_action or AsyncMock()
        )


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
        return Reply("welcome back!")

    message = FakeMessage(user_id=42, text="I'm here")
    await make_message_handler(reply_fn)(message)

    msg = calls["msg"]
    assert msg.channel == "telegram"
    assert msg.channel_user_id == "42"  # the numeric id, never the @username
    assert msg.text == "I'm here"
    assert msg.display_name == "Ana García"
    assert msg.link_code is None
    assert msg.is_private is True
    message.answer.assert_awaited_once_with("welcome back!")


async def test_group_messages_are_rejected_before_agent_work():
    """Shared chats get the deterministic Spanish rejection; the Agent
    (reply_fn) is never invoked — no identity resolution, no typing, no
    model call (#211)."""
    reply_fn_calls = []

    async def reply_fn(msg):
        reply_fn_calls.append(msg)
        return Reply("ok")

    message = FakeMessage(text="/dashboard", chat_type="supergroup")
    await make_message_handler(reply_fn)(message)

    # The Agent must never run for a group message.
    assert reply_fn_calls == []
    # The rejection reply must be sent exactly once.
    message.answer.assert_awaited_once()
    rejection_text = message.answer.await_args[0][0]
    assert rejection_text == (
        "👋 ¡Hola! Solo puedo entrenarte por chat directo, no en grupos. "
        "Envíame un mensaje privado y empezamos."
    )


async def test_group_messages_get_no_typing_indicator():
    """Rejection must happen before any typing activity (#211)."""
    bot_send_chat_action = AsyncMock()
    reply_fn = AsyncMock()

    message = FakeMessage(
        text="hello", chat_type="group",
        bot_send_chat_action=bot_send_chat_action,
    )
    await make_message_handler(reply_fn)(message)

    # No typing sent.
    bot_send_chat_action.assert_not_awaited()
    # Agent never called.
    reply_fn.assert_not_awaited()


async def test_handler_sends_model_markdown_as_plain_text():
    async def reply_fn(msg):
        return Reply("Do **bench** today 💪")

    message = FakeMessage()
    await make_message_handler(reply_fn)(message)

    message.answer.assert_awaited_once_with("Do bench today 💪")


async def test_handler_extracts_the_deep_link_payload():
    calls = {}

    async def reply_fn(msg):
        calls["msg"] = msg
        return Reply("hi")

    await make_message_handler(reply_fn)(FakeMessage(text="/start gym-code"))

    assert calls["msg"].link_code == "gym-code"


async def test_handler_ignores_messages_without_a_sender():
    reply_fn = AsyncMock()
    await make_message_handler(reply_fn)(FakeMessage(user_id=None))
    reply_fn.assert_not_awaited()


async def test_channel_post_message_gets_rejection():
    """Channel posts (chat type 'channel') arrive as channel_post updates,
    not message updates. The handler must reject them with the same
    non-private rejection (#211)."""
    async def reply_fn(msg):
        return Reply("should not reach agent")

    # A channel post still carries a Message object; the chat.type is 'channel'.
    message = FakeMessage(
        user_id=123, text="post to channel", chat_type="channel",
    )
    await make_message_handler(reply_fn)(message)

    message.answer.assert_awaited_once()
    rejection_text = message.answer.await_args[0][0]
    assert rejection_text == (
        "👋 ¡Hola! Solo puedo entrenarte por chat directo, no en grupos. "
        "Envíame un mensaje privado y empezamos."
    )


async def test_edited_group_message_gets_rejection():
    """When a member edits their text in a shared chat the edit arrives
    as an edited_message update.  The handler must still reject it with
    the deterministic Spanish rejection — no Agent invocation, no typing,
    no model call (#211)."""
    reply_fn = AsyncMock()

    # An edited group message has chat_type != 'private' and carries text.
    message = FakeMessage(
        user_id=99, text="edited in group", chat_type="supergroup",
    )
    await make_message_handler(reply_fn)(message)

    # The Agent must never run for an edited shared-chat message.
    reply_fn.assert_not_awaited()
    # The rejection reply must be sent exactly once.
    message.answer.assert_awaited_once()
    rejection_text = message.answer.await_args[0][0]
    assert rejection_text == (
        "👋 ¡Hola! Solo puedo entrenarte por chat directo, no en grupos. "
        "Envíame un mensaje privado y empezamos."
    )


async def test_edited_channel_post_gets_rejection():
    """An edited channel post arrives as an edited_channel_post update.
    The handler must reject it the same way as a fresh channel post (#211)."""
    async def reply_fn(msg):
        return Reply("should not reach agent")

    message = FakeMessage(
        user_id=123, text="edited channel post", chat_type="channel",
    )
    await make_message_handler(reply_fn)(message)

    message.answer.assert_awaited_once()
    rejection_text = message.answer.await_args[0][0]
    assert rejection_text == (
        "👋 ¡Hola! Solo puedo entrenarte por chat directo, no en grupos. "
        "Envíame un mensaje privado y empezamos."
    )


async def test_anonymous_admin_group_message_gets_rejection():
    """Messages with no from_user (e.g. anonymous admin / sender-chat)
    in non-private chats must still receive the rejection reply (#211)."""
    reply_fn = AsyncMock()
    message = FakeMessage(user_id=None, chat_type="supergroup", text="admin broadcast")
    await make_message_handler(reply_fn)(message)

    # Agent must never run.
    reply_fn.assert_not_awaited()
    # Rejection must be sent.
    message.answer.assert_awaited_once()
    rejection_text = message.answer.await_args[0][0]
    assert rejection_text == (
        "👋 ¡Hola! Solo puedo entrenarte por chat directo, no en grupos. "
        "Envíame un mensaje privado y empezamos."
    )


async def test_handler_answers_even_when_the_agent_loop_fails():
    async def reply_fn(msg):
        raise RuntimeError("model unavailable")

    message = FakeMessage()
    await make_message_handler(reply_fn)(message)
    message.answer.assert_awaited_once_with(ERROR_REPLY)


async def test_empty_reply_still_sends_something():
    async def reply_fn(msg):
        return Reply("")

    message = FakeMessage()
    await make_message_handler(reply_fn)(message)
    assert message.answer.await_count == 1


async def test_disable_preview_reply_sends_with_preview_off():
    async def reply_fn(msg):
        return Reply("https://dash.example.com/login/abc", disable_preview=True)

    message = FakeMessage()
    await make_message_handler(reply_fn)(message)

    _, kwargs = message.answer.await_args
    assert kwargs["link_preview_options"].is_disabled is True


async def test_notifier_with_disable_preview_sends_with_preview_off():
    # Safety-flag pings carry a one-time magic link: Telegram's preview
    # fetcher must never GET it (same rule as the /dashboard reply).
    bot = SimpleNamespace(send_message=AsyncMock())
    await TelegramNotifier(bot).send(
        "telegram", "7", "Heads-up…\nhttps://dash.example.com/login/abc",
        disable_preview=True,
    )

    _, kwargs = bot.send_message.await_args
    assert kwargs["link_preview_options"].is_disabled is True


async def test_notifier_with_protect_content_marks_the_message_unforwardable():
    # The one-time login token must not be forwardable (review on PR #120).
    bot = SimpleNamespace(send_message=AsyncMock())
    await TelegramNotifier(bot).send(
        "telegram", "7", "https://dash.example.com/login/abc",
        disable_preview=True,
        protect_content=True,
    )

    _, kwargs = bot.send_message.await_args
    assert kwargs["protect_content"] is True
    assert kwargs["link_preview_options"].is_disabled is True


async def test_notifier_leaves_previews_alone_by_default():
    bot = SimpleNamespace(send_message=AsyncMock())
    await TelegramNotifier(bot).send("telegram", "7", "missed legs Monday")

    _, kwargs = bot.send_message.await_args
    assert "link_preview_options" not in kwargs
    assert "protect_content" not in kwargs


async def test_after_send_runs_only_once_the_reply_text_is_out():
    order = []

    async def after_send(**_):
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


def test_dispatcher_registers_handler_on_all_non_private_observers():
    """The handler must be registered on all four observers that can carry
    non-private text: message, channel_post, edited_message, and
    edited_channel_post.  Missing any of them would silently bypass the
    non-private rejection (#211)."""
    dispatcher = create_dispatcher(AsyncMock())
    assert len(dispatcher.message.handlers) == 1
    assert len(dispatcher.channel_post.handlers) == 1, (
        "channel_post handler is missing — channel posts would silently "
        "bypass the non-private rejection"
    )
    assert len(dispatcher.edited_message.handlers) == 1, (
        "edited_message handler is missing — edited group messages would "
        "silently bypass the non-private rejection"
    )
    assert len(dispatcher.edited_channel_post.handlers) == 1, (
        "edited_channel_post handler is missing — edited channel posts "
        "would silently bypass the non-private rejection"
    )


# ── typing indicator ────────────────────────────────────────────────────


async def test_typing_indicator_sent_before_agent_work():
    """The typing action must be the first thing sent — before reply_fn runs."""
    order = []

    bot_send_chat_action = AsyncMock(
        side_effect=lambda *a, **k: order.append("typing")
    )

    async def reply_fn(msg):
        order.append("agent")
        return Reply("ok")

    message = FakeMessage(bot_send_chat_action=bot_send_chat_action)
    await make_message_handler(reply_fn)(message)

    # Typing must appear before the Agent does any work.
    assert order[0] == "typing"
    assert "typing" in order
    assert "agent" in order


async def test_typing_indicator_refreshes_during_turn():
    """Telegram expires the typing action after ~5 s; the handler must
    refresh it while the Agent is still working."""
    from agentg.channels import telegram as tmod

    async def slow_agent(msg):
        await asyncio.sleep(0.15)
        return Reply("done")

    bot_send_chat_action = AsyncMock()
    message = FakeMessage(bot_send_chat_action=bot_send_chat_action)

    # Shorten the refresh interval so the test sees refreshes without
    # waiting for a full production cycle.
    saved = tmod._TYPING_REFRESH_INTERVAL
    tmod._TYPING_REFRESH_INTERVAL = 0.04
    try:
        await make_message_handler(slow_agent)(message)
    finally:
        tmod._TYPING_REFRESH_INTERVAL = saved

    # The agent sleeps 0.15 s with a 0.04 s refresh interval, so the loop
    # should fire at least two refreshes on top of the initial send.
    assert bot_send_chat_action.await_count >= 3, (
        f"expected >= 3 typing sends, got {bot_send_chat_action.await_count}"
    )

    # Every call must use the action='typing' parameter.
    for call_args in bot_send_chat_action.await_args_list:
        assert call_args.kwargs.get("action") == "typing"


async def test_typing_indicator_stops_after_reply():
    """Once the reply is sent the typing indicator must not fire again."""
    from agentg.channels import telegram as tmod

    bot_send_chat_action = AsyncMock()

    async def reply_fn(msg):
        # Yield long enough for the typing task to wake from its first sleep,
        # send a refresh, and enter its loop body — so cancellation actually
        # interrupts a running task.
        await asyncio.sleep(0.03)
        return Reply("all good")

    message = FakeMessage(bot_send_chat_action=bot_send_chat_action)

    saved = tmod._TYPING_REFRESH_INTERVAL
    tmod._TYPING_REFRESH_INTERVAL = 0.01
    try:
        await make_message_handler(reply_fn)(message)
    finally:
        tmod._TYPING_REFRESH_INTERVAL = saved

    final_count = bot_send_chat_action.await_count

    # The typing task must have run its loop at least once before cancellation.
    assert final_count >= 2, (
        f"typing task never entered its refresh loop, got {final_count} calls"
    )

    # Wait a bit — no more typing actions should arrive.
    await asyncio.sleep(0.15)

    assert bot_send_chat_action.await_count == final_count, (
        "typing indicator leaked after reply was sent"
    )


async def test_typing_indicator_stops_on_agent_failure():
    """A failure inside the Agent still clears the typing indicator."""
    from agentg.channels import telegram as tmod

    bot_send_chat_action = AsyncMock()

    async def reply_fn(msg):
        # Yield long enough for the typing task to wake from its first sleep,
        # send a refresh, and enter its loop body — so cancellation actually
        # interrupts a running task.
        await asyncio.sleep(0.03)
        raise RuntimeError("model unavailable")

    message = FakeMessage(bot_send_chat_action=bot_send_chat_action)

    saved = tmod._TYPING_REFRESH_INTERVAL
    tmod._TYPING_REFRESH_INTERVAL = 0.01
    try:
        await make_message_handler(reply_fn)(message)
    finally:
        tmod._TYPING_REFRESH_INTERVAL = saved

    final_count = bot_send_chat_action.await_count

    # The typing task must have run its loop at least once before cancellation.
    assert final_count >= 2, (
        f"typing task never entered its refresh loop, got {final_count} calls"
    )

    # The typing indicator must stop after the error.
    await asyncio.sleep(0.15)

    assert bot_send_chat_action.await_count == final_count, (
        "typing indicator leaked after agent failure"
    )
    # The error reply must still be delivered.
    message.answer.assert_awaited_once_with(ERROR_REPLY)
