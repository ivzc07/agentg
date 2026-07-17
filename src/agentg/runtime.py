"""The channel-agnostic agent loop: channel identity + text in, reply text out.

Imports nothing from aiogram (ADR 0001); channel adapters call ``handle_message``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field

from agents import Agent, Runner
from agents.extensions.memory import SQLAlchemySession
from sqlalchemy.ext.asyncio import AsyncEngine


def conversation_key(channel: str, channel_user_id: str) -> str:
    """One SDK session per person.

    Keyed by channel identity until Members and gym linking exist (ticket #25);
    docs/design/memory.md then switches this to ``member:{member_id}``.
    """
    return f"{channel}:{channel_user_id}"


@dataclass
class AgentRuntime:
    agent: Agent
    engine: AsyncEngine
    # One lock per conversation so a rapid double message can't interleave
    # turns in the same session history. Unbounded, but one entry per person
    # who ever messaged this process — fine at this scale.
    _locks: defaultdict[str, asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )

    async def ensure_schema(self) -> None:
        """Create the SDK session tables once at startup."""
        session = SQLAlchemySession("startup:schema", engine=self.engine, create_tables=True)
        await session.get_items(limit=1)  # table creation happens on first use

    def session_for(self, channel: str, channel_user_id: str) -> SQLAlchemySession:
        return SQLAlchemySession(
            conversation_key(channel, channel_user_id),
            engine=self.engine,
        )

    async def handle_message(self, channel: str, channel_user_id: str, text: str) -> str:
        key = conversation_key(channel, channel_user_id)
        async with self._locks[key]:
            result = await Runner.run(
                self.agent, text, session=self.session_for(channel, channel_user_id)
            )
            return str(result.final_output)
