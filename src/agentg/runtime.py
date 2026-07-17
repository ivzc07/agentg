"""The channel-agnostic agent loop: incoming message in, reply text out.

Imports nothing from aiogram (ADR 0001); channel adapters call
``handle_message``. Linking runs first (deterministic); the Agent only runs
for linked Members, with history keyed ``member:{member_id}`` per
docs/design/memory.md. Walking-skeleton history under the old
``telegram:{user_id}`` keys was dev-only and is left behind (issue #25).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field

from agents import Agent, Runner
from agents.extensions.memory import SQLAlchemySession
from sqlalchemy.ext.asyncio import AsyncEngine

from agentg.messages import IncomingMessage
from agentg.onboarding import Onboarding
from agentg.store import LinkedIdentity, LinkingStore
from agentg.tools import MemberContext
from agentg.training import TrainingStore


@dataclass
class AgentRuntime:
    agent: Agent
    engine: AsyncEngine
    store: LinkingStore
    onboarding: Onboarding
    training: TrainingStore
    # One lock per channel identity so a rapid double message can't interleave
    # turns (or onboarding steps). Unbounded, but one entry per person who
    # ever messaged this process — fine at this scale.
    _locks: defaultdict[tuple[str, str], asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )

    async def ensure_schema(self) -> None:
        """Create the domain and SDK session tables once at startup."""
        await self.store.ensure_schema()
        await self.training.ensure_seeded()
        session = SQLAlchemySession("startup:schema", engine=self.engine, create_tables=True)
        await session.get_items(limit=1)  # table creation happens on first use

    def session_for_member(self, member_id: int) -> SQLAlchemySession:
        return SQLAlchemySession(f"member:{member_id}", engine=self.engine)

    def member_context(self, linked: LinkedIdentity) -> MemberContext:
        return MemberContext(
            training=self.training,
            member_id=linked.member.id,
            gym_id=linked.gym.id,
            member_name=linked.member.name,
            gym_name=linked.gym.name,
            weight_unit=linked.gym.weight_unit,
        )

    async def handle_message(self, msg: IncomingMessage) -> str:
        async with self._locks[(msg.channel, msg.channel_user_id)]:
            linked = await self.store.identity_for(msg.channel, msg.channel_user_id)
            reply = await self.onboarding.handle(msg, linked)
            if reply is not None:
                return reply
            if linked is None:  # onboarding always replies for unlinked identities
                raise RuntimeError("unlinked message reached the agent loop")
            result = await Runner.run(
                self.agent,
                msg.text,
                session=self.session_for_member(linked.member.id),
                context=self.member_context(linked),
            )
            return str(result.final_output)
