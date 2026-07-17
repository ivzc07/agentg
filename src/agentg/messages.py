"""The channel-agnostic shape of an incoming chat message."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IncomingMessage:
    """One message from a person, as every channel adapter reports it.

    ``channel_user_id`` is the channel's stable numeric id as a string (for
    Telegram: the numeric user id, never the mutable ``@username``).
    ``link_code`` is a deep-link payload when the channel carries one:
    ``None`` for an ordinary message, ``""`` for a link tap with no code.
    """

    channel: str
    channel_user_id: str
    text: str
    display_name: str = ""
    link_code: str | None = None
