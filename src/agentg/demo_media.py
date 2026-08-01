"""Serving a demo animation, channel-agnostically (spec §Exercise demo media).

The Agent asks to show an Exercise; this resolves the demo (Gym override or
default), reuses a cached Telegram file_id when one exists, and otherwise has
the channel upload the MP4 and caches the file_id it returns. The channel
adapter supplies the ``DemoSender`` (ADR 0001); this module imports no aiogram.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agentg.demos import DemoRef, DemoStore


@dataclass(frozen=True)
class SentAnimation:
    file_id: str
    file_unique_id: str | None = None


class DemoSender(Protocol):
    @property
    def cache_namespace(self) -> str:
        """A per-bot cache key; a bot migration changes it and misses the cache."""
        ...

    async def send_animation(
        self, channel: str, channel_user_id: str, slug: str, cached_file_id: str | None
    ) -> SentAnimation | None:
        """Send the demo (by file_id if given, else by uploading ``slug``) and
        return the file_id it now has, or None on failure."""
        ...


async def _send_resolved_demo(
    demos: DemoStore,
    sender: DemoSender,
    ref: DemoRef,
    channel: str,
    channel_user_id: str,
) -> str:
    """Send an already-resolved DemoRef. Returns 'sent' / 'send_failed'."""
    bot = sender.cache_namespace
    cached = await demos.cached_file_id(ref.exercise_id, ref.gym_id, bot)
    result = await sender.send_animation(channel, channel_user_id, ref.slug, cached)
    if result is None:
        return "send_failed"
    if result.file_id != cached:  # first send, or a refreshed id — seed the cache
        await demos.cache_file_id(
            ref.exercise_id, ref.gym_id, bot, result.file_id, result.file_unique_id
        )
    return "sent"


async def serve_demo(
    demos: DemoStore,
    sender: DemoSender,
    exercise_name: str,
    gym_id: int,
    channel: str,
    channel_user_id: str,
) -> str:
    """Send an Exercise's demo to a Member. Returns a status:
    'sent' / 'no_demo' / 'send_failed'."""
    ref = await demos.resolve(exercise_name, gym_id)
    if ref is None:
        return "no_demo"
    return await _send_resolved_demo(demos, sender, ref, channel, channel_user_id)
