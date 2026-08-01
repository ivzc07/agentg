"""The channel-agnostic agent loop: incoming message in, reply text out.

Imports nothing from aiogram (ADR 0001); channel adapters call
``handle_message``. Linking runs first (deterministic); the Agent only runs
for linked Members, with history keyed ``member:{member_id}`` per
docs/design/memory.md. Walking-skeleton history under the old
``telegram:{user_id}`` keys was dev-only and is left behind (issue #25).

Streaming (issue #176): the runtime uses ``Runner.run_streamed`` so the
first complete sentence reaches the channel before generation finishes.
When no live model is available (tests), ``stream_replies=False`` falls
back to ``Runner.run`` — the streaming path is the production default.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from agents import Agent, Runner
from agents.extensions.memory import SQLAlchemySession
from agents.stream_events import RawResponsesStreamEvent
from sqlalchemy.ext.asyncio import AsyncEngine

from openai.types.responses import ResponseTextDeltaEvent

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

# The first chunk must be at least this many characters before we send it
# (avoids sending "Hi!" or "OK." as the first sentence).
_MIN_FIRST_SENTENCE_LENGTH = 12
# Characters that mark the end of a complete sentence.
_SENTENCE_ENDINGS = (".", "!", "?")


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
    # Stream replies by default; set False when no live model backs the Agent
    # (tests that mock Runner).  Kept deliberately per #176.
    stream_replies: bool = True
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

            if self.stream_replies:
                return self._streamed_reply(msg, context, session)
            else:
                return await self._blocking_reply(msg, context, session)

    async def _blocking_reply(
        self, msg: IncomingMessage, context: MemberContext, session: SQLAlchemySession
    ) -> Reply:
        """Non-streaming path kept deliberately for tests (#176)."""
        result = await Runner.run(
            self.agent,
            msg.text,
            session=session,
            context=context,
        )
        text = str(result.final_output)
        return self._wrap_with_demos(text, context, msg.channel, msg.channel_user_id)

    def _streamed_reply(
        self, msg: IncomingMessage, context: MemberContext, session: SQLAlchemySession
    ) -> Reply:
        """Streaming path: returns a Reply whose ``.stream`` async generator
        yields accumulated text at sentence boundaries as the Agent generates."""
        result = Runner.run_streamed(
            self.agent,
            msg.text,
            session=session,
            context=context,
        )
        stream = _stream_text(result)
        after_send = self._after_send_for_demos(context, msg.channel, msg.channel_user_id)
        return Reply("", stream=stream, after_send=after_send)

    def _wrap_with_demos(
        self, text: str, context: MemberContext, channel: str, user_id: str
    ) -> Reply:
        """Return a Reply whose ``after_send`` sends deferred demos.

        For the blocking path demo_requests are already populated, so we check
        eagerly and only attach ``after_send`` when there is work to do."""
        if self.demo_sender is None or not context.demo_requests:
            return Reply(text)
        return Reply(
            text,
            after_send=self._after_send_for_demos(context, channel, user_id),
        )

    def _after_send_for_demos(
        self, context: MemberContext, channel: str, user_id: str
    ) -> Callable[[], Awaitable[None]] | None:
        """Return an ``after_send`` callback that delivers queued demos, or
        None when no demos were requested or the sender is absent."""
        sender = self.demo_sender
        if sender is None:
            return None
        # Capture nothing now — demo_requests is populated during the run
        # and read only when after_send fires (after the stream is exhausted).
        gym_id = context.gym_id

        async def after_send() -> None:
            for exercise in list(context.demo_requests):
                try:
                    await serve_demo(
                        self.stores.demos, sender, exercise, gym_id, channel, user_id
                    )
                except Exception:
                    logger.exception("failed to serve demo %r to %s", exercise, user_id)

        # Only attach the callback if there were demo requests; the list is
        # populated during generation, so check lazily via a wrapper.
        async def maybe_send() -> None:
            if context.demo_requests:
                await after_send()

        return maybe_send


def _is_sentence_boundary(text: str, last_sent: str) -> bool:
    """True when ``text`` has grown a complete sentence past ``last_sent``.

    A sentence ends with ``.``, ``!``, or ``?`` followed by a space, newline,
    or end-of-string.  The first chunk must be at least
    ``_MIN_FIRST_SENTENCE_LENGTH`` characters.  Subsequent chunks are sent
    at every sentence boundary.
    """
    new = text[len(last_sent):]
    if not new:
        return False
    # Scan new text for a sentence ending followed by whitespace or end.
    for i, ch in enumerate(new):
        if ch not in _SENTENCE_ENDINGS:
            continue
        after = new[i + 1:]
        if after != "" and not after[0].isspace():
            continue  # e.g. "Hello.World" — no boundary
        # The sentence candidate ends at this punctuation.
        candidate_len = len(last_sent) + i + 1
        if not last_sent and candidate_len < _MIN_FIRST_SENTENCE_LENGTH:
            continue  # first chunk too short; keep scanning
        return True
    return False


async def _stream_text(result: "RunResultStreaming") -> AsyncIterator[str]:
    """Yield the accumulated reply text at sentence boundaries.

    Wraps the SDK's ``stream_events()``: every
    ``ResponseTextDeltaEvent`` appends a text fragment; when a sentence
    boundary is detected the full accumulated text is yielded.  The final
    yield always sends whatever remains.

    An exception part-way through generation yields the text accumulated
    so far (if any was already sent) so the Member sees a coherent outcome.
    """
    accumulated = ""
    last_sent = ""
    try:
        async for event in result.stream_events():
            if isinstance(event, RawResponsesStreamEvent):
                data = event.data
                if isinstance(data, ResponseTextDeltaEvent):
                    accumulated += data.delta
                    if _is_sentence_boundary(accumulated, last_sent):
                        last_sent = accumulated
                        yield accumulated
        # Final yield: always send whatever is left.
        if accumulated != last_sent:
            yield accumulated
    except Exception:
        logger.exception("streaming generation failed")
        # When nothing has been delivered yet, re-raise so the channel can
        # send an error reply instead of leaving the Member in silence.
        if not last_sent:
            raise
        # Already sent a partial reply — yield the remainder so the Member
        # sees a coherent outcome rather than a truncated message.
        if accumulated != last_sent:
            yield accumulated
