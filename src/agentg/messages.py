"""The channel-agnostic shape of an incoming chat message."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass


class Reply(str):
    """The reply text, plus optional follow-up work to run *after* the text is
    sent (e.g. sending demo animations, so they land beneath the reply).

    A ``str`` subclass so every existing caller keeps treating a reply as
    plain text; the channel adapter awaits ``after_send`` once the text is out.

    When ``stream`` is set the channel delivers chunks progressively: the
    first yield is sent as a new message, later yields edit that message.
    Each yield is the full accumulated text so far, growing monotonically.
    """

    after_send: Callable[[], Awaitable[None]] | None
    # Suppress the channel's link preview (magic links: a preview fetch could
    # spend a one-time token before the human taps it).
    disable_preview: bool
    # When set, the channel streams chunks as they arrive (issue #176).
    stream: AsyncIterator[str] | None

    def __new__(
        cls,
        text: str,
        after_send: Callable[[], Awaitable[None]] | None = None,
        disable_preview: bool = False,
        stream: AsyncIterator[str] | None = None,
    ) -> "Reply":
        obj = super().__new__(cls, text)
        obj.after_send = after_send
        obj.disable_preview = disable_preview
        obj.stream = stream
        return obj


@dataclass(frozen=True)
class IncomingMessage:
    """One message from a person, as every channel adapter reports it.

    ``channel_user_id`` is the channel's stable numeric id as a string (for
    Telegram: the numeric user id, never the mutable ``@username``).
    ``link_code`` is a deep-link payload when the channel carries one:
    ``None`` for an ordinary message, ``""`` for a link tap with no code.
    ``is_group`` marks a shared chat (a Telegram group): anything secret —
    a dashboard magic link — must never be replied there.
    """

    channel: str
    channel_user_id: str
    text: str
    display_name: str = ""
    link_code: str | None = None
    is_group: bool = False
