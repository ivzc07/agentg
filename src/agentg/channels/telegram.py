"""Telegram adapter — the only module allowed to import aiogram (ADR 0001).

Delivery is long polling: no public endpoint, webhook, or domain, and exactly
one process may poll a given bot token at a time (single replica).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.types import FSInputFile, LinkPreviewOptions, Message

from agentg.demo_media import SentAnimation
from agentg.messages import IncomingMessage, Reply

logger = logging.getLogger(__name__)

CHANNEL = "telegram"
MAX_MESSAGE_LENGTH = 4096  # Telegram's hard cap per message
ERROR_REPLY = "Uy — algo falló de mi lado. Inténtalo de nuevo en un momento."
EMPTY_REPLY_FALLBACK = "Mmm, me quedé en blanco — ¿lo intentas de nuevo?"

ReplyFn = Callable[[IncomingMessage], Awaitable[Reply]]


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
    """Strip model bold markup, then split by Telegram's UTF-16 limit.

    Telegram's 4096 cap counts UTF-16 units, not code points — an emoji
    weighs 2. Splitting per character keeps surrogate pairs intact.
    """
    text = text.replace("**", "")
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


# Module-level so tests can shorten the refresh interval without waiting
# for a full production cycle (Telegram expires the action after ~5 s).
_TYPING_REFRESH_INTERVAL: float = 4.5


async def _keep_typing(bot: Bot, chat_id: int) -> None:
    """Send the typing action repeatedly so Telegram never expires it.

    Telegram's typing indicator lasts ~5 seconds; this coroutine refreshes it
    every ``_TYPING_REFRESH_INTERVAL`` seconds until cancelled.  The caller
    must cancel the task (and await the cancellation) to stop the indicator.
    """
    while True:
        await asyncio.sleep(_TYPING_REFRESH_INTERVAL)
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            logger.debug("typing refresh failed", exc_info=True)


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
            is_group=message.chat.type != "private",
        )
        # Send the first typing action synchronously so it lands before the
        # Agent starts work, then keep refreshing in the background (Telegram
        # expires the action after ~5 s).  The initial send is failure-tolerant
        # — a transient failure must never gate the reply.
        try:
            await message.bot.send_chat_action(
                chat_id=message.chat.id, action=ChatAction.TYPING
            )
        except Exception:
            logger.warning("first typing send failed", exc_info=True)
        typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
        try:
            try:
                reply = await reply_fn(incoming)
            except Exception:
                logger.exception("agent loop failed for sender %s", message.from_user.id)
                await message.answer(ERROR_REPLY)
                return
            # Deliver the reply text; follow-up actions always run in a finally
            # so a Telegram 403 / 429 / network error on the reply text does not
            # silently drop deferred coach pings or demo animations (issue #172).
            try:
                for chunk in split_reply(reply) or [EMPTY_REPLY_FALLBACK]:
                    if reply.disable_preview:  # keep the call shape unchanged otherwise
                        await message.answer(
                            chunk, link_preview_options=LinkPreviewOptions(is_disabled=True)
                        )
                    else:
                        await message.answer(chunk)
            finally:
                # Follow-up media (demo animations) lands beneath the reply text.
                if reply.after_send is not None:
                    try:
                        await reply.after_send()
                    except Exception:
                        logger.exception("post-reply delivery failed for sender %s", message.from_user.id)
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("typing task teardown raised", exc_info=True)

    return on_text


def create_dispatcher(reply_fn: ReplyFn) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.message.register(make_message_handler(reply_fn), F.text)
    return dispatcher


class TelegramNotifier:
    """Sends proactive messages (check-in nudges) outside the polling loop.

    Channel-agnostic callers (the check-in sweep) hold this as a ``Notifier``;
    it ignores anything not addressed to Telegram (ADR 0001).
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(
        self,
        channel: str,
        channel_user_id: str,
        text: str,
        disable_preview: bool = False,
        protect_content: bool = False,
    ) -> None:
        if channel != CHANNEL:
            return
        for chunk in split_reply(text):
            # Magic links: a preview fetch could spend a one-time token
            # before the human taps it (same rule as the /dashboard reply),
            # and protect_content keeps the token message unforwardable.
            kwargs: dict = {}
            if disable_preview:
                kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
            if protect_content:
                kwargs["protect_content"] = True
            await self._bot.send_message(chat_id=int(channel_user_id), text=chunk, **kwargs)


class TelegramDemoSender:
    """Sends exercise demos via ``sendAnimation`` — autoplaying, looping, muted.

    Resends by cached ``file_id`` when one exists (no bytes re-transferred);
    otherwise uploads the canonical MP4 from the media store and returns the
    fresh file_id for the core to cache. The cache namespace is the bot id, so
    a token migration simply misses and re-uploads (ADR 0001; research doc)."""

    def __init__(self, bot: Bot, media_root: str, bot_id: str) -> None:
        self._bot = bot
        self._media_root = Path(media_root)
        self._bot_id = bot_id

    @property
    def cache_namespace(self) -> str:
        return self._bot_id

    async def send_animation(
        self, channel: str, channel_user_id: str, slug: str, cached_file_id: str | None
    ) -> SentAnimation | None:
        if channel != CHANNEL:
            return None
        animation: str | FSInputFile = cached_file_id or FSInputFile(self._media_root / slug)
        try:
            message = await self._bot.send_animation(
                chat_id=int(channel_user_id), animation=animation
            )
        except Exception:
            logger.exception("send_animation failed for %s", slug)
            return None
        if message.animation is None:  # Telegram didn't render it as an animation
            return SentAnimation(file_id=cached_file_id) if cached_file_id else None
        return SentAnimation(
            file_id=message.animation.file_id,
            file_unique_id=message.animation.file_unique_id,
        )


def build_bot(token: str) -> Bot:
    return Bot(token=token)


def bot_id(token: str) -> str:
    """The numeric bot id embedded in the token (before the colon) — a stable
    per-bot cache namespace that changes when the token does."""
    return token.split(":", 1)[0]


async def run_polling(bot: Bot, reply_fn: ReplyFn) -> None:
    dispatcher = create_dispatcher(reply_fn)
    logger.info("starting Telegram long polling")
    await dispatcher.start_polling(bot)
