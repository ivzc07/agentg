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

from agents import Agent, RunConfig, Runner
from agents.extensions.memory import SQLAlchemySession
from agents.run_config import CallModelData, ModelInputData
from sqlalchemy.ext.asyncio import AsyncEngine

from agentg.checkin_sweep import Notifier
from agentg.compaction import Summarizer, maybe_compact
from agentg.dashboard import DashboardDoor, is_dashboard_command
from agentg.demo_media import DemoSender, serve_demo
from agentg.messages import IncomingMessage, Reply
from agentg.linking import Linking
from agentg.linking_store import LinkedIdentity
from agentg.context import MemberContext
from agentg.snapshot import member_snapshot
from agentg.stores import Stores

logger = logging.getLogger(__name__)


async def _inject_snapshot(data: CallModelData[MemberContext]) -> ModelInputData:
    """call_model_input_filter: append the per-turn member snapshot as a
    developer message at the end of the input so the static system prompt
    stays identical across turns and the prompt prefix is cacheable (#175).

    The snapshot is injected here, not stored in the session history, so
    stale snapshots can never accumulate.
    """
    ctx = data.context
    if ctx is None:
        return data.model_data
    snapshot = await member_snapshot(ctx)
    snapshot_item: dict = {
        "role": "developer",
        "content": snapshot,
        "type": "message",
    }
    return ModelInputData(
        input=list(data.model_data.input) + [snapshot_item],
        instructions=data.model_data.instructions,
    )


_SNAPSHOT_RUN_CONFIG = RunConfig(call_model_input_filter=_inject_snapshot)


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
            session = self.session_for_member(linked.member.id)
            await maybe_compact(
                session, self.summarizer, self.stores.notes, linked.member.id, linked.gym.id
            )
            context = self.member_context(linked)
            result = await Runner.run(
                self.agent,
                msg.text,
                session=session,
                context=context,
                run_config=_SNAPSHOT_RUN_CONFIG,
            )
            text = str(result.final_output)
            # The check-in rhythm reset is deferred past the reply so it never
            # blocks the LLM call; it still revives lapsed Members (#169).
            sender = self.demo_sender
            member_id = linked.member.id
            requests = list(context.demo_requests) if sender is not None else []
            gym_id = context.gym_id
            channel, user_id = msg.channel, msg.channel_user_id

            # after_send is a best-effort hook: the channel adapter fires it
            # after the reply is delivered, so if the send itself fails this
            # never runs and the rhythm reset + demos are silently skipped.
            async def after_send() -> None:
                # reset_rhythm is isolated from demo sends so a failure in
                # one doesn't block the other.
                try:
                    await self.stores.checkins.reset_rhythm(member_id)
                except Exception:
                    logger.exception("reset_rhythm failed for %d", member_id)
                for exercise in requests:
                    try:
                        await serve_demo(
                            self.stores.demos, sender, exercise, gym_id, channel, user_id
                        )
                    except Exception:
                        logger.exception("failed to serve demo %r to %s", exercise, user_id)

            return Reply(text, after_send=after_send)
