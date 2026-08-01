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
from agentg.demo_media import DemoSender, serve_demo


async def _drain_coach_pings(pings):
    """Best-effort drain of accumulated coach pings after a Runner failure."""
    for ping in pings:
        try:
            await ping()
        except Exception:
            logger.exception("deferred coach ping failed after Runner exception")
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
            await maybe_compact(
                session, self.summarizer, self.stores.notes, linked.member.id, linked.gym.id
            )
            context = self.member_context(linked)
            # Coach pings accumulated during the turn must be drained even
            # if Runner.run raises (a later tool error, provider timeout,
            # MaxTurnsExceeded) — the safety note was already committed and
            # silence is not an option (P1 #5153515963).
            result = None
            try:
                result = await Runner.run(
                    self.agent,
                    msg.text,
                    session=session,
                    context=context,
                )
                text = str(result.final_output)
                sender = self.demo_sender
                has_demos = sender is not None and context.demo_requests
                has_pings = bool(context.coach_pings)
                if not has_demos and not has_pings:
                    return Reply(text)
                demo_requests = list(context.demo_requests) if sender is not None else []
                coach_pings = list(context.coach_pings)
                gym_id = context.gym_id
                channel, user_id = msg.channel, msg.channel_user_id

                async def after_send() -> None:
                    async def _send_demo(exercise: str) -> None:
                        try:
                            # Narrow sender for mypy (P2 #5153516992).
                            assert sender is not None
                            await serve_demo(
                                self.stores.demos, sender, exercise, gym_id, channel, user_id
                            )
                        except Exception:
                            logger.exception("failed to serve demo %r to %s", exercise, user_id)

                    async def _run_ping(ping):
                        try:
                            await ping()
                        except Exception:
                            logger.exception("deferred coach ping failed")

                    tasks = [_send_demo(ex) for ex in demo_requests] + [
                        _run_ping(p) for p in coach_pings
                    ]
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)

                return Reply(text, after_send=after_send)
            except BaseException:
                # Runner failed after the safety tool already ran — drain
                # the accumulated pings so no Coach notification is lost.
                if context.coach_pings:
                    pings = list(context.coach_pings)
                    asyncio.create_task(_drain_coach_pings(pings))
                raise
