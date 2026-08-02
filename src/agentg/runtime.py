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
from agentg.demo_media import DemoSender, _send_resolved_demo


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
from agentg.instrument import TurnContext
from agentg.snapshot import member_snapshot
from agentg.stores import Stores

logger = logging.getLogger(__name__)

# How long a turn waits for the previous turn's compaction to signal before
# giving up and proceeding.  This only covers the window before after_send
# starts; once compaction runs it holds the per-identity lock, which
# serialises the turns regardless.
COMPACTION_SIGNAL_GRACE_SECONDS = 5.0


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
    # The "developer" role works with the default openai/gpt-4o-mini (the
    # SDK's chatcmpl converter supports it).  Via LiteLLM, some providers
    # (e.g. Anthropic) hoist developer/system messages into the top-level
    # system param — which would silently move the snapshot back to the
    # front and defeat the caching goal of #175.  If MODEL is set to a
    # provider that does not support developer-role messages as trailing
    # input items, this mechanism needs a per-provider adaptation.
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

    async def member_context(self, linked: LinkedIdentity) -> MemberContext:
        """Build the per-turn context with conversation-stable gating flags
        precomputed (issue #174)."""
        can_author_routine = True
        if not linked.member.is_coach:
            routine = await self.stores.routines.active_routine(linked.member.id)
            # Routine-authoring tools are usable when the Member has no routine
            # at all (intake) OR has an agent-generated one (can replace it).
            # A coach-authored routine blocks them — the Agent never restructures
            # those (issue #174).
            can_author_routine = (
                routine is None or not routine.get("coach_authored", False)
            )
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
            can_author_routine=can_author_routine,
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
            with TurnContext():
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
                # Awaited: the tool set is scoped to the caller's role, which
                # needs a Routine lookup (issue #174).
                context = await self.member_context(linked)
                # Coach pings accumulated during the turn must be drained even
                # if Runner.run raises (a later tool error, provider timeout,
                # MaxTurnsExceeded) -- the safety note was already committed
                # and silence is not an option (issue #172).
                result = None
                # Any reply resets the check-in rhythm and revives a lapsed
                # Member.  Fired concurrently with the model call rather than
                # in front of it, so the DB write overlaps the LLM round-trip
                # and never adds to the Member's wait (issue #169).  It is
                # awaited on every exit path below, including failure.
                member_id = linked.member.id
                reset_task = asyncio.create_task(
                    self.stores.checkins.reset_rhythm(member_id)
                )

                async def _await_reset() -> None:
                    try:
                        await reset_task
                    except Exception:
                        logger.exception("reset_rhythm failed for %d", member_id)

                try:
                    try:
                        result = await Runner.run(
                            self.agent,
                            msg.text,
                            session=session,
                            context=context,
                            run_config=_SNAPSHOT_RUN_CONFIG,
                        )
                    finally:
                        # Issue #166: delete_my_data clears the session during
                        # the turn, but the runner persists this turn's items
                        # afterwards -- the tool call and goodbye survive the
                        # wipe.  Clear again so nothing remains.  In a finally
                        # so a mid-turn error doesn't skip the clear after the
                        # domain wipe has already committed.
                        if context.forgotten:
                            await session.clear_session()
                    text = str(result.final_output)
                    sender = self.demo_sender
                    # after_send is always attached now: even with no demos and
                    # no pings it is what settles the deferred rhythm reset
                    # after the reply is delivered (issue #169).
                    # Already-resolved DemoRefs -- no second Catalog lookup (#179).
                    demo_refs = list(context.demo_requests) if sender is not None else []
                    coach_pings = list(context.coach_pings)
                    channel, user_id = msg.channel, msg.channel_user_id

                    # Compaction only affects the *next* turn's prompt, so it
                    # runs in after_send instead of in front of the reply --
                    # that takes a model call off the critical path (#173).
                    # The next turn waits on this signal (bounded) before it
                    # takes the lock.  Registered only on the success path, so
                    # a failed turn leaves no signal for anyone to wait on.
                    compaction_done = asyncio.Event()
                    self._compaction_done[key] = compaction_done
                    summarizer = self.summarizer
                    notes_store = self.stores.notes
                    lock = self._locks[key]
                    gym_id = context.gym_id

                    async def after_send() -> None:
                        async def _send_demo(ref) -> None:
                            try:
                                # Narrow sender for mypy (P2 #5153516992).
                                assert sender is not None
                                await _send_resolved_demo(
                                    self.stores.demos, sender, ref, channel, user_id
                                )
                            except Exception:
                                logger.exception(
                                    "failed to serve demo %r to %s", ref.exercise_name, user_id
                                )

                        async def _run_ping(ping):
                            try:
                                await ping()
                            except Exception:
                                logger.exception("deferred coach ping failed")

                        # Whatever happens below, the next turn must be
                        # released: the signal is set in a finally covering the
                        # whole body, not just the compaction call (#173).
                        try:
                            # The rhythm reset is settled here too, isolated
                            # from the demo/ping fan-out so one failure cannot
                            # block it.
                            await _await_reset()
                            # Demos and pings go first -- they must not wait on
                            # the summarizer (timeout=60, num_retries=1 -> up
                            # to ~2 min).
                            tasks = [_send_demo(ref) for ref in demo_refs] + [
                                _run_ping(p) for p in coach_pings
                            ]
                            if tasks:
                                await asyncio.gather(*tasks, return_exceptions=True)
                            async with lock:
                                try:
                                    await maybe_compact(
                                        session, summarizer, notes_store, member_id, gym_id
                                    )
                                except Exception:
                                    logger.exception(
                                        "compaction failed for member %d", member_id
                                    )
                        finally:
                            compaction_done.set()

                    return Reply(text, after_send=after_send)
                except BaseException:
                    # On model failure the reset must still land, or a lapsed
                    # Member is never revived (issue #169).
                    await _await_reset()
                    # Runner failed after the safety tool already ran -- drain
                    # the accumulated pings so no Coach notification is lost.
                    if context.coach_pings:
                        pings = list(context.coach_pings)
                        asyncio.create_task(_drain_coach_pings(pings))
                    raise
