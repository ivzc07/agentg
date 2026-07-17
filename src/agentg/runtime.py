"""The channel-agnostic agent loop: channel identity + text in, reply text out.

Imports nothing from aiogram (ADR 0001); channel adapters call ``handle_message``.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    def session_for(self, channel: str, channel_user_id: str) -> SQLAlchemySession:
        return SQLAlchemySession(
            conversation_key(channel, channel_user_id),
            engine=self.engine,
            create_tables=True,
        )

    async def handle_message(self, channel: str, channel_user_id: str, text: str) -> str:
        session = self.session_for(channel, channel_user_id)
        result = await Runner.run(self.agent, text, session=session)
        return str(result.final_output)
