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

from agentg.checkin_sweep import Notifier
from agentg.compaction import Summarizer, maybe_compact
from agentg.dashboard import DashboardDoor, is_dashboard_command
from agentg.demo_media import DemoSender, _send_resolved_demo
from agentg.messages import IncomingMessage, Reply
from agentg.linking import Linking
from agentg.linking_store import LinkedIdentity
from agentg.context import MemberContext
from agentg.stores import Stores

logger = logging.getLogger(__name__)


@dataclass
class AgentRuntime:
    agent: Agent
    engine: AsyncEngine
    stores: Stores
    linking: Linking
    summarizer: Summarizer
    # The channel's demo-animation sender; None disables demo delivery (tests
    # that don't exercise demos leave it unset).
    demo_sender: DemoSender | None = None
    # Channel notifier for safety-flag pings (the Gym's Coaches).
    notifier: Notifier | None = None
    # The dashboard door (`/dashboard` -> magic link); None in tests that
    # don't exercise the dashboard.
    dashboard: DashboardDoor | None = None
    # One lock per channel identity so a rapid double message can't interleave
    # turns (or linking steps). Unbounded, but one entry per person who
    # ever messaged this process — fine at this scale.
    _locks: defaultdict[tuple[str, str], asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )

    async def ensure_schema(self) -> None:
        """Create the domain and SDK session tables once at startup."""
        await self.stores.linking.ensure_schema()
        await self.stores.training.ensure_seeded()
        session = SQLAlchemySession("startup:schema", engine=self.engine, create_tables=True)
        await session.get_items(limit=1)  # table creation happens on first use

    def session_for_member(self, member_id: int) -> SQLAlchemySession:
        return SQLAlchemySession(f"member:{member_id}", engine=self.engine)

    def member_context(self, linked: LinkedIdentity) -> MemberContext:
        return MemberContext(
            stores=self.stores,
            notifier=self.notifier,
            member_id=linked.member.id,
            gym_id=linked.gym.id,
            member_name=linked.member.name,
            gym_name=linked.gym.name,
            weight_unit=linked.gym.weight_unit,
            timezone=linked.gym.timezone,
            is_coach=linked.member.is_coach,
            dashboard_base_url=self.dashboard.base_url if self.dashboard else None,
        )

    async def handle_message(self, msg: IncomingMessage) -> Reply:
        async with self._locks[(msg.channel, msg.channel_user_id)]:
            linked = await self.stores.linking.identity_for(msg.channel, msg.channel_user_id)
            reply = await self.linking.handle(msg, linked)
            if reply is not None:
                return Reply(reply)
            if linked is None:  # linking always replies for unlinked identities
                raise RuntimeError("unlinked message reached the agent loop")
            # `/dashboard` is a deterministic door, not Agent chat: it never
            # touches the check-in rhythm, compaction, or history.
            if self.dashboard is not None and is_dashboard_command(msg.text):
                return await self.dashboard.handle(linked, is_group=msg.is_group)
            # Any reply resets the check-in rhythm and revives a lapsed Member.
            await self.stores.checkins.reset_rhythm(linked.member.id)
            session = self.session_for_member(linked.member.id)
            try:
                await maybe_compact(
                    session, self.summarizer, self.stores.notes, linked.member.id, linked.gym.id
                )
            except Exception:
                logger.exception("compaction failed for member %d", linked.member.id)
            context = self.member_context(linked)
            try:
                result = await Runner.run(
                    self.agent,
                    msg.text,
                    session=session,
                    context=context,
                )
            finally:
                # Issue #166: delete_my_data clears the session during the
                # turn, but the runner persists this turn's items afterwards —
                # the tool call and goodbye survive the wipe.  Clear again so
                # nothing remains.  Run in finally so a mid-turn error (API,
                # MaxTurnsExceeded) doesn't skip the clear after the domain
                # wipe has already committed.
                if context.forgotten:
                    await session.clear_session()
            text = str(result.final_output)
            sender = self.demo_sender
            if sender is None or not context.demo_requests:
                return Reply(text)
            # Defer the demo sends so the channel delivers the reply text first,
            # then the animations land beneath it.
            refs = list(context.demo_requests)
            channel, user_id = msg.channel, msg.channel_user_id

            async def after_send() -> None:
                for ref in refs:
                    try:
                        await _send_resolved_demo(
                            self.stores.demos, sender, ref, channel, user_id
                        )
                    except Exception:
                        logger.exception("failed to serve demo %r to %s", ref.exercise_name, user_id)

            return Reply(text, after_send=after_send)
