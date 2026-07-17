"""Telegram adapter — the only module allowed to import aiogram (ADR 0001).

Delivery is long polling: no public endpoint, webhook, or domain, and exactly
one process may poll a given bot token at a time (single replica).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

from agentg.messages import IncomingMessage

logger = logging.getLogger(__name__)

CHANNEL = "telegram"
MAX_MESSAGE_LENGTH = 4096  # Telegram's hard cap per message
ERROR_REPLY = "Sorry — something went wrong on my end. Give it another try in a moment."
EMPTY_REPLY_FALLBACK = "Hmm, I came up empty — mind trying that again?"

ReplyFn = Callable[[IncomingMessage], Awaitable[str]]


def parse_start_payload(text: str) -> str | None:
    """The deep-link payload of a ``/start`` command, if this is one.

    A Gym's deep link ``t.me/<bot>?start=<code>`` arrives as the message
    ``/start <code>``; a bare ``/start`` (no payload) returns ``""`` and any
    other message returns ``None``.
    """
    parts = text.split(maxsplit=1)
    if not parts or parts[0].split("@")[0] != "/start":
        return None
    return parts[1].strip() if len(parts) > 1 else ""


def split_reply(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split into chunks of at most ``limit`` UTF-16 code units.

    Telegram's 4096 cap counts UTF-16 units, not code points — an emoji
    weighs 2. Splitting per character keeps surrogate pairs intact.
    """
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for char in text:
        weight = 2 if ord(char) > 0xFFFF else 1
        if units + weight > limit:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += weight
    if current:
        chunks.append("".join(current))
    return chunks


def make_message_handler(reply_fn: ReplyFn) -> Callable[[Message], Awaitable[None]]:
    async def on_text(message: Message) -> None:
        if message.from_user is None or message.text is None:
            return
        incoming = IncomingMessage(
            channel=CHANNEL,
            channel_user_id=str(message.from_user.id),  # numeric id, never @username
            text=message.text,
            display_name=message.from_user.full_name or "",
            link_code=parse_start_payload(message.text),
        )
        try:
            reply = await reply_fn(incoming)
        except Exception:
            logger.exception("agent loop failed for sender %s", message.from_user.id)
            await message.answer(ERROR_REPLY)
            return
        for chunk in split_reply(reply) or [EMPTY_REPLY_FALLBACK]:
            await message.answer(chunk)

    return on_text


def create_dispatcher(reply_fn: ReplyFn) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.message.register(make_message_handler(reply_fn), F.text)
    return dispatcher


async def run_polling(token: str, reply_fn: ReplyFn) -> None:
    bot = Bot(token=token)
    dispatcher = create_dispatcher(reply_fn)
    logger.info("starting Telegram long polling")
    await dispatcher.start_polling(bot)
