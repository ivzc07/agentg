"""The channel-agnostic agent loop: incoming message in, reply text out.

Imports nothing from aiogram (ADR 0001); channel adapters call
``handle_message``. Linking runs first (deterministic); the Agent only runs
for linked Members, with history keyed ``member:{member_id}`` per
docs/design/memory.md. Walking-skeleton history under the old
``telegram:{user_id}`` keys was dev-only and is left behind (issue #25).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from agents import Agent, Runner
from agents.extensions.memory import SQLAlchemySession
from sqlalchemy.ext.asyncio import AsyncEngine

from agentg.checkin_store import CheckinStore
from agentg.checkin_sweep import Notifier
from agentg.compaction import Summarizer, maybe_compact
from agentg.demo_media import DemoSender, serve_demo
from agentg.demos import DemoStore
from agentg.messages import IncomingMessage, Reply
from agentg.notes import NotesStore
from agentg.routines import RoutineStore
from agentg.onboarding import Onboarding
from agentg.store import LinkedIdentity, LinkingStore
from agentg.tools import MemberContext
from agentg.training import TrainingStore

logger = logging.getLogger(__name__)


@dataclass
class AgentRuntime:
    agent: Agent
    engine: AsyncEngine
    store: LinkingStore
    onboarding: Onboarding
    training: TrainingStore
    notes: NotesStore
    routines: RoutineStore
    checkins: CheckinStore
    demos: DemoStore
    summarizer: Summarizer
    # The channel's demo-animation sender; None disables demo delivery (tests
    # that don't exercise demos leave it unset).
    demo_sender: DemoSender | None = None
    # Channel notifier for consented safety referrals (pinging a Gym's Coach).
    notifier: Notifier | None = None
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
            notes=self.notes,
            routines=self.routines,
            linking=self.store,
            checkins=self.checkins,
            demos=self.demos,
            notifier=self.notifier,
            member_id=linked.member.id,
            gym_id=linked.gym.id,
            member_name=linked.member.name,
            gym_name=linked.gym.name,
            weight_unit=linked.gym.weight_unit,
            is_coach=linked.member.is_coach,
        )

    async def handle_message(self, msg: IncomingMessage) -> str:
        async with self._locks[(msg.channel, msg.channel_user_id)]:
            linked = await self.store.identity_for(msg.channel, msg.channel_user_id)
            reply = await self.onboarding.handle(msg, linked)
            if reply is not None:
                return reply
            if linked is None:  # onboarding always replies for unlinked identities
                raise RuntimeError("unlinked message reached the agent loop")
            # Any reply resets the check-in rhythm and revives a lapsed Member.
            await self.checkins.reset_rhythm(linked.member.id)
            session = self.session_for_member(linked.member.id)
            await maybe_compact(
                session, self.summarizer, self.notes, linked.member.id, linked.gym.id
            )
            context = self.member_context(linked)
            result = await Runner.run(
                self.agent,
                msg.text,
                session=session,
                context=context,
            )
            text = str(result.final_output)
            sender = self.demo_sender
            if sender is None or not context.demo_requests:
                return Reply(text)
            # Defer the demo sends so the channel delivers the reply text first,
            # then the animations land beneath it.
            requests = list(context.demo_requests)
            gym_id = context.gym_id
            channel, user_id = msg.channel, msg.channel_user_id

            async def after_send() -> None:
                for exercise in requests:
                    try:
                        await serve_demo(self.demos, sender, exercise, gym_id, channel, user_id)
                    except Exception:
                        logger.exception("failed to serve demo %r to %s", exercise, user_id)

            return Reply(text, after_send=after_send)
