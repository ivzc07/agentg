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
from agentg.messages import IncomingMessage, Reply
from agentg.linking import Linking
from agentg.linking_store import LinkedIdentity
from agentg.context import MemberContext
from agentg.stores import Stores

logger = logging.getLogger(__name__)

# How long a turn waits for the previous turn's compaction to signal before
# giving up and proceeding.  This only covers the window before after_send
# starts; once compaction runs it holds the per-identity lock, which
# serialises the turns regardless.
COMPACTION_SIGNAL_GRACE_SECONDS = 5.0


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
    # How long a turn waits for the previous turn's compaction to signal
    # before proceeding without it (see COMPACTION_SIGNAL_GRACE_SECONDS).
    compaction_grace_seconds: float = COMPACTION_SIGNAL_GRACE_SECONDS
    # One lock per channel identity so a rapid double message can't interleave
    # turns (or linking steps). Unbounded, but one entry per person who
    # ever messaged this process — fine at this scale.
    _locks: defaultdict[tuple[str, str], asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )
    # One pending-compaction event per identity.  Before processing a turn,
    # handle_message awaits the previous turn's compaction (if any) outside
    # the lock — after_send still acquires the lock to compact, so the
    # event-based wait avoids a deadlock (issue #173 criterion 2).
    #
    # after_send is caller-driven: the runtime hands it back and trusts the
    # channel to run it.  A channel that drops it (or dies first) must never
    # wedge that Member, so the wait is bounded and the entry is consumed on
    # read — ordering is a nicety, liveness is not negotiable.
    _compaction_done: dict[tuple[str, str], asyncio.Event] = field(default_factory=dict)

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
        key = (msg.channel, msg.channel_user_id)
        # Await the previous turn's compaction (if any) before acquiring the
        # lock.  This is done outside the lock so that after_send (which
        # also acquires the lock) can still make progress (issue #173).
        #
        # The wait is bounded: it only covers the window before after_send
        # starts.  Once compaction is actually running it holds the lock, so
        # the acquire below serialises us anyway.  If the signal never comes
        # (a channel that dropped after_send), we proceed with an uncompacted
        # prompt — the pre-#173 behaviour — rather than wedging the Member.
        # popping consumes the event so one dropped after_send costs one
        # grace period, not every turn thereafter, and the dict cannot grow
        # without bound.
        prev = self._compaction_done.pop(key, None)
        if prev is not None and not prev.is_set():
            try:
                await asyncio.wait_for(prev.wait(), self.compaction_grace_seconds)
            except TimeoutError:
                logger.warning(
                    "previous turn's compaction never signalled for %s:%s; "
                    "proceeding (the per-identity lock still serialises it)",
                    msg.channel,
                    msg.channel_user_id,
                )
        async with self._locks[key]:
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
            context = self.member_context(linked)
            result = await Runner.run(
                self.agent,
                msg.text,
                session=session,
                context=context,
            )
            text = str(result.final_output)
            # Build the after_send callback: demo animations land first
            # (they don't need the lock and shouldn't wait on the summarizer),
            # then compaction runs inside the per-identity lock.  Compaction
            # only affects the *next* turn's prompt, so deferring it behind
            # the reply removes a model call from the critical path (issue #173).
            # The next turn awaits compaction_done before acquiring the lock,
            # so criterion 2 (compaction completes before next turn) holds
            # even when the adapter delays calling after_send.
            member_id = linked.member.id
            gym_id = context.gym_id
            channel, user_id = msg.channel, msg.channel_user_id
            sender = self.demo_sender
            summarizer = self.summarizer
            notes_store = self.stores.notes
            lock = self._locks[(msg.channel, msg.channel_user_id)]
            demo_requests = list(context.demo_requests)
            compaction_done = asyncio.Event()
            self._compaction_done[key] = compaction_done

            async def after_send() -> None:
                # Whatever happens in here, the next turn must be released:
                # the signal is set in a finally covering the whole body, not
                # just the compaction call (issue #173).
                try:
                    # Serve demos first — they don't need the lock and
                    # shouldn't wait for the summarizer (timeout=60,
                    # num_retries=1 → up to ~2 min).
                    if sender is not None:
                        for exercise in demo_requests:
                            try:
                                await serve_demo(
                                    self.stores.demos, sender, exercise, gym_id, channel, user_id
                                )
                            except Exception:
                                logger.exception(
                                    "failed to serve demo %r to %s", exercise, user_id
                                )
                    async with lock:
                        try:
                            await maybe_compact(
                                session, summarizer, notes_store, member_id, gym_id
                            )
                        except Exception:
                            logger.exception("compaction failed for member %d", member_id)
                finally:
                    compaction_done.set()

            return Reply(text, after_send=after_send)
