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
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from agents import Agent, RunConfig, Runner
from agents.stream_events import RawResponsesStreamEvent
from openai.types.responses import ResponseTextDeltaEvent
from agents.extensions.memory import SQLAlchemySession
from agents.run_config import CallModelData, ModelInputData
from sqlalchemy.ext.asyncio import AsyncEngine

from agentg.checkin_sweep import Notifier
from agentg.compaction import Summarizer, maybe_compact
from agentg.dashboard import DashboardDoor, is_dashboard_command
from agentg.demo_media import DemoSender, _send_resolved_demo
from agentg.messages import IncomingMessage, Reply
from agentg.linking import Linking
from agentg.linking_store import LinkedIdentity
from agentg.context import MemberContext
from agentg.instrument import TurnContext
from agentg.snapshot import member_snapshot
from agentg.stores import Stores

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agents.result import RunResultStreaming

logger = logging.getLogger(__name__)


async def _drain_coach_pings(pings):
    """Best-effort drain of accumulated coach pings after a Runner failure."""
    for ping in pings:
        try:
            await ping()
        except Exception:
            logger.exception("deferred coach ping failed after Runner exception")


# How long a turn waits for the previous turn's compaction to signal before
# giving up and proceeding.  This only covers the window before after_send
# starts; once compaction runs it holds the per-identity lock, which
# serialises the turns regardless.
COMPACTION_SIGNAL_GRACE_SECONDS = 5.0

# The first chunk must be at least this many characters before we send it
# (avoids sending "Hi!" or "OK." as the first sentence).
_MIN_FIRST_SENTENCE_LENGTH = 12
# Characters that mark the end of a complete sentence.
_SENTENCE_ENDINGS = (".", "!", "?")


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


# Per-language forget-me response templates (issue #212).  The language is
# detected from the triggering raw text and persisted with the pending
# intent so the goodbye mirrors the Member (ADR-0002: mirror, default
# Spanish when no signal).
_FORGET_GOODBYE: dict[str, str] = {
    "en": "Your data has been permanently deleted. Goodbye!",
    "es": (
        "Tus datos han sido eliminados permanentemente. \u00a1Adi\u00f3s!"
    ),
}

_FORGET_WARNING: dict[str, str] = {
    "en": (
        "\u26a0\ufe0f This will PERMANENTLY DELETE ALL your data \u2014 "
        "every session, routine, note, and all chat history. "
        "This cannot be undone.\n\n"
        "To confirm, reply with this exact phrase:\n\n"
        "{phrase}\n\n"
        "Any other response will cancel the request. "
        "This confirmation expires in {minutes} minute{s}."
    ),
    "es": (
        "\u26a0\ufe0f Esto borrar\u00e1 PERMANENTEMENTE TODOS tus datos \u2014 "
        "cada sesi\u00f3n, rutina, nota y todo el historial de chat. "
        "No se puede deshacer.\n\n"
        "Para confirmar, responde con esta frase exacta:\n\n"
        "{phrase}\n\n"
        "Cualquier otra respuesta cancelar\u00e1 la solicitud. "
        "Esta confirmaci\u00f3n expira en {minutes} minuto{s}."
    ),
}

# Private-message redirect when a Member tries to confirm or interact with
# a deletion from a group chat (issue #212, fix-r7 P1).
_FORGET_PRIVATE_REDIRECT: dict[str, str] = {
    "en": (
        "Your data deletion is in progress. "
        "Please send the confirmation phrase as a private message to complete it."
    ),
    "es": (
        "La eliminaci\u00f3n de tus datos est\u00e1 en curso. "
        "Env\u00eda la frase de confirmaci\u00f3n como mensaje privado para completarla."
    ),
}


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
    # Stream replies by default; set False when no live model backs the Agent
    # (tests that mock Runner).  Kept deliberately per #176.
    stream_replies: bool = True
    # How long a forget-me confirmation phrase stays valid (issue #212).
    forget_me_confirmation_seconds: int = 300
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
        # Streaming hands lock ownership to the stream wrapper, so the lock is
        # acquired manually rather than with ``async with``: it is released
        # when the stream is exhausted (or errors), not when this returns.
        lock = self._locks[key]
        await lock.acquire()
        lock_transferred = False
        try:
            # The streaming path defers this turn's log line until the stream
            # is consumed -- otherwise #161 would measure setup only and report
            # zero model calls on every production turn (issue #161 + #176).
            turn = TurnContext()
            with turn:
                linked = await self.stores.linking.identity_for(msg.channel, msg.channel_user_id)
                reply = await self.linking.handle(msg, linked)
                if reply is not None:
                    # P1 (fix-r8): Cancel pending Forget-me intent before linking
                    # early return so a /start code or linking/switch reply can't
                    # leave a pending request active.  Only linked Members can have
                    # a pending Forget-me request — unlinked identities have no row.
                    if linked is not None:
                        pending_fm = await self.stores.forget.get_pending_request(
                            linked.member.id
                        )
                        if pending_fm is not None:
                            await self.stores.forget.cancel_forget_me(
                                linked.member.id
                            )
                    return Reply(reply)
                if linked is None:  # linking always replies for unlinked identities
                    raise RuntimeError("unlinked message reached the agent loop")
                # Pre-model forget-me check (issue #212): handles both
                # initiating the two-turn flow and confirming deletion.
                # Must run before `/dashboard` dispatch so an ordinary
                # message (including /dashboard) cancels a pending intent.
                forget_reply = await self._handle_forget_me(msg, linked)
                if forget_reply is not None:
                    return forget_reply
                # `/dashboard` is a deterministic door, not Agent chat: it never
                # touches the check-in rhythm, compaction, or history.
                if self.dashboard is not None and is_dashboard_command(msg.text):
                    return await self.dashboard.handle(linked, is_group=msg.is_group)
                session = self.session_for_member(linked.member.id)
                # Awaited: the tool set is scoped to the caller's role, which
                # needs a Routine lookup (issue #174).
                context = await self.member_context(linked)
                # Any reply resets the check-in rhythm and revives a lapsed
                # Member.  Fired concurrently with the model call rather than
                # in front of it, so the DB write overlaps the LLM round-trip
                # and never adds to the Member's wait (issue #169).  It is
                # settled on every exit path, including failure.
                member_id = linked.member.id
                reset_task = asyncio.create_task(
                    self.stores.checkins.reset_rhythm(member_id)
                )

                async def _await_reset() -> None:
                    try:
                        await reset_task
                    except Exception:
                        logger.exception("reset_rhythm failed for %d", member_id)

                # Coach pings accumulated during the turn must be drained even
                # if the run raises (a later tool error, provider timeout,
                # MaxTurnsExceeded) -- the safety note was already committed
                # and silence is not an option (issue #172).
                try:
                    if self.stream_replies:
                        # Transfer lock ownership to the stream wrapper: it
                        # releases the lock when the stream is exhausted or
                        # errors, so concurrent messages from the same identity
                        # cannot race the session or interleave chunks (#176).
                        streamed = self._streamed_reply(
                            msg, context, session, key, member_id, _await_reset,
                            _lock=lock, _turn=turn,
                        )
                        lock_transferred = True
                        return streamed
                    return await self._blocking_reply(
                        msg, context, session, key, member_id, _await_reset
                    )
                except BaseException:
                    # On failure the reset must still land, or a lapsed Member
                    # is never revived (issue #169).
                    await _await_reset()
                    if context.coach_pings:
                        pings = list(context.coach_pings)
                        asyncio.create_task(_drain_coach_pings(pings))
                    raise
        finally:
            if not lock_transferred:
                lock.release()

    async def _blocking_reply(
        self,
        msg: IncomingMessage,
        context: MemberContext,
        session: SQLAlchemySession,
        key: tuple[str, str],
        member_id: int,
        await_reset,
    ) -> Reply:
        """Non-streaming path kept deliberately for tests (#176)."""
        result = await Runner.run(
            self.agent,
            msg.text,
            session=session,
            context=context,
            run_config=_SNAPSHOT_RUN_CONFIG,
        )
        text = str(result.final_output)
        return Reply(
            text,
            after_send=self._post_turn(msg, context, session, key, member_id, await_reset),
        )

    def _streamed_reply(
        self,
        msg: IncomingMessage,
        context: MemberContext,
        session: SQLAlchemySession,
        key: tuple[str, str],
        member_id: int,
        await_reset,
        _lock: asyncio.Lock | None = None,
        _turn: TurnContext | None = None,
    ) -> Reply:
        """Streaming path: a Reply whose ``.stream`` yields the accumulated
        text at sentence boundaries as the Agent generates it (#176)."""
        result = Runner.run_streamed(
            self.agent,
            msg.text,
            session=session,
            context=context,
            run_config=_SNAPSHOT_RUN_CONFIG,
        )
        stream = _stream_text(result)
        # Innermost first: release the lock (#176), then close the turn's
        # instrument last so its duration and counts cover the whole
        # generation, not just setup (#161).
        if _lock is not None:
            stream = _hold_lock(stream, _lock)
        if _turn is not None:
            _turn.defer_logging = True
            stream = _finish_turn(stream, _turn)
        return Reply(
            "",
            stream=stream,
            after_send=self._post_turn(msg, context, session, key, member_id, await_reset),
        )

    def _post_turn(
        self,
        msg: IncomingMessage,
        context: MemberContext,
        session: SQLAlchemySession,
        key: tuple[str, str],
        member_id: int,
        await_reset,
    ):
        """Build the ``after_send`` hook both reply paths share.

        Everything deferred past the reply lives here: the rhythm reset
        (#169), demo animations (#179), coach pings (#172) and compaction
        (#173).  The context lists are read when the hook *runs*, not when it
        is built, because on the streaming path the tools populate them after
        this returns.
        """
        # Compaction only affects the *next* turn's prompt, so it runs here
        # instead of in front of the reply -- that takes a model call off the
        # critical path (#173).  The next turn waits on this signal (bounded)
        # before it takes the lock.
        compaction_done = asyncio.Event()
        self._compaction_done[key] = compaction_done
        sender = self.demo_sender
        summarizer = self.summarizer
        notes_store = self.stores.notes
        lock = self._locks[key]
        gym_id = context.gym_id
        channel, user_id = msg.channel, msg.channel_user_id

        async def after_send(*, deliver_media: bool = True) -> None:
            """``deliver_media=False`` suppresses only the demo animations.

            The channel passes it when a streamed delivery errored, so an
            animation does not land beneath an error message (#176) -- but the
            safety pings, rhythm reset and compaction signal still run.
            """

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

            # Whatever happens below, the next turn must be released: the
            # signal is set in a finally covering the whole body, not just the
            # compaction call (#173).
            try:
                # The rhythm reset is settled here too, isolated from the
                # demo/ping fan-out so one failure cannot block it.
                await await_reset()
                # Read now, not at build time: on the streaming path the tools
                # populate these while the stream is being consumed.
                deliver_demos = sender is not None and deliver_media
                demo_refs = list(context.demo_requests) if deliver_demos else []
                coach_pings = list(context.coach_pings)
                # Demos and pings go first -- they must not wait on the
                # summarizer (timeout=60, num_retries=1 -> up to ~2 min).
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
                        logger.exception("compaction failed for member %d", member_id)
            finally:
                compaction_done.set()

        return after_send

    # -- Pre-model forget-me (issue #212) ---------------------------------

    async def _handle_forget_me(
        self, msg: IncomingMessage, linked: LinkedIdentity
    ) -> Reply | None:
        """Handle the two-turn forget-me flow before the model ever runs.

        Returns a Reply when the message was fully handled (request or
        confirmation), or None to let normal processing continue.
        """
        from agentg.forget import (
            detect_conversation_language,
            detect_forget_me_language,
            is_forget_me_request,
            normalize_confirmation,
        )

        now = datetime.now(timezone.utc)

        # P1: Group messages must NEVER execute Forget-me deletion or post
        # goodbye publicly, even when a deleting/consumed row exists.
        # Preserve durable deleting state and return only the required
        # private-message redirect; recovery occurs on a later private
        # turn (issue #212, fix-r7 P1).
        if msg.is_group:
            # Check for deleting (in-progress or interrupted) deletion
            # first — preserve the durable state, redirect to private.
            deleting_req = await self.stores.forget.get_deleting_request(
                linked.member.id
            )
            if deleting_req is not None:
                return Reply(
                    _FORGET_PRIVATE_REDIRECT[deleting_req.language or "es"]
                )
            # Cancel any pending request — group messages never confirm
            # deletion.
            pending = await self.stores.forget.get_pending_request(
                linked.member.id
            )
            if pending is not None:
                await self.stores.forget.cancel_forget_me(linked.member.id)
            return None

        # P1: Check for a deleting (in-progress or interrupted) deletion
        # FIRST — before anything else.  A deleting row is the durable
        # signal that deletion was already confirmed.
        #
        # When the exact confirmation phrase is repeated, resume deletion
        # deterministically (partial-failure recovery, issue #212, fix-3).
        # Any other message from a deleting state falls through — the
        # model can respond while the user waits for recovery.
        deleting_req = await self.stores.forget.get_deleting_by_phrase(
            linked.member.id, normalize_confirmation(msg.text), now
        )
        if deleting_req is not None:
            await self.stores.forget.forget_member(linked.member.id)
            return Reply(_FORGET_GOODBYE[deleting_req.language or "es"])

        pending = await self.stores.forget.get_pending_request(
            linked.member.id
        )

        if pending is not None:
            # Expired — cancel silently and fall through (P2: <= not <).
            if pending.expires_at <= now:
                await self.stores.forget.cancel_forget_me(linked.member.id)
            else:
                normalized = normalize_confirmation(msg.text)
                if normalized == pending.confirmation_phrase:
                    # P1: atomic compare-and-claim so concurrent
                    # confirmations across runtimes have exactly one
                    # winner and a stale confirmation cannot delete.
                    claimed = await self.stores.forget.claim_forget_me_request(
                        linked.member.id, normalized, now
                    )
                    if claimed is not None:
                        await self.stores.forget.forget_member(
                            linked.member.id
                        )
                        lang = claimed.language or "es"
                        return Reply(_FORGET_GOODBYE[lang])
                    # P1: Lost the race — another runtime already claimed
                    # this request and may be deleting the Member right
                    # now. The row now has status "deleting" (not deleted),
                    # so check for it to prevent falling through to the
                    # model while deletion is still in progress.
                    deleting_req = await self.stores.forget.get_deleting_request(
                        linked.member.id
                    )
                    if deleting_req is not None:
                        return Reply(
                            _FORGET_GOODBYE[deleting_req.language or "es"]
                        )
                    # The pending was cancelled (not claimed) — fall
                    # through to normal processing.
                else:
                    # Wrong phrase — cancel the pending request.  A new
                    # forget-me trigger below will create a fresh one.
                    await self.stores.forget.cancel_forget_me(
                        linked.member.id
                    )

        # Check if this message looks like a new forget-me request
        # (handles both "no pending" and "wrong phrase, re-asking").
        if is_forget_me_request(msg.text):
            # ADR-0002: language from the whole conversation, not just the
            # trigger text.  A Spanish-conversation Member typing "forget me"
            # in English must still receive Spanish warning and goodbye.
            session = self.session_for_member(linked.member.id)
            conv_lang = await detect_conversation_language(session)
            lang = conv_lang or detect_forget_me_language(msg.text) or "es"
            phrase = await self.stores.forget.request_forget_me(
                linked.member.id,
                linked.gym.id,
                now,
                self.forget_me_confirmation_seconds,
                lang,
            )
            # Empty string is the sentinel: a deleting row was
            # detected between the fast-path read and the conditional
            # upsert — another runtime claimed this Member's deletion.
            # Complete it deterministically (issue #212, fix-r6).
            if not phrase:
                await self.stores.forget.forget_member(linked.member.id)
                return Reply(_FORGET_GOODBYE[lang])
            minutes = max(1, self.forget_me_confirmation_seconds // 60)
            warning = _FORGET_WARNING[lang].format(
                phrase=phrase,
                minutes=minutes,
                s="s" if minutes != 1 else "",
            )
            # P2 (fix-r8): A forget-me warning is an actual Member reply
            # so reset the proactive check-in rhythm here — the normal
            # Agent path fires reset_rhythm but the pre-model forget-me
            # path does not, leaving the cadence to degrade across the
            # two-turn flow.  This is the inline equivalent.
            await self.stores.checkins.reset_rhythm(linked.member.id)
            return Reply(warning)

        # P1 safety net: before falling through to the model, re-verify
        # the Member still exists AND that no deleting request appeared
        # since our initial check.  A concurrent runtime may have claimed
        # the pending request and begun deletion while we processed this
        # ordinary message — the deleting row is the durable signal that
        # the model must never see this message.
        deleting_now = await self.stores.forget.get_deleting_request(
            linked.member.id
        )
        if deleting_now is not None:
            return Reply(_FORGET_GOODBYE[deleting_now.language or "es"])
        identity = await self.stores.linking.identity_for(
            msg.channel, msg.channel_user_id
        )
        if identity is None:
            return Reply(_FORGET_GOODBYE["es"])

        return None



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


async def _finish_turn(
    inner: AsyncIterator[str], turn: TurnContext,
) -> AsyncIterator[str]:
    """Yield every chunk from ``inner``, then close out the turn's instrument.

    A streaming turn is not over when ``handle_message`` returns -- the model
    is still generating.  Logging there would report setup-only duration and
    zero model calls on every production turn, so the log line is emitted here
    instead, in a ``finally`` so an aborted stream is still accounted for
    (issue #161 + #176).
    """
    aborted = False
    try:
        async for chunk in inner:
            yield chunk
    except BaseException:
        # Errored or abandoned mid-generation: not a completed turn, so it is
        # not logged -- #161 measures completed turns only, and a half-turn
        # would pollute the latency baseline.
        aborted = True
        raise
    finally:
        # ``async for ... yield`` does NOT propagate aclose() to the delegated
        # generator, so close it explicitly: the channel's aclose() on the
        # outermost wrapper must still drive the #166 wipe and the lock release
        # deterministically, not leave them to async-gen GC.  In a try/finally
        # so a failure down the chain cannot swallow the log.
        try:
            await inner.aclose()
        finally:
            if not aborted:
                turn.finish()


async def _hold_lock(
    inner: AsyncIterator[str], lock: asyncio.Lock,
) -> AsyncIterator[str]:
    """Yield every chunk from ``inner``, then release ``lock``.

    If the inner stream raises, the lock is released before the exception
    propagates — the same guarantee ``async with lock`` would give.

    The outer ``GeneratorExit`` handler covers ``aclose()`` arriving before
    the body has started.  Note that CPython does not run a never-started
    async generator's body at all, so that path is belt-and-braces rather
    than a guarantee to rely on; the channel always starts the stream before
    closing it (``_deliver_streamed``)."""
    released = False
    try:
        try:
            async for chunk in inner:
                yield chunk
        finally:
            # Propagate the close inward before releasing (see _finish_turn),
            # but never at the cost of the release itself: clear_session() down
            # the chain can raise, and a lost release wedges this Member
            # forever (handle_message acquires with no timeout).
            try:
                await inner.aclose()
            finally:
                if not released:
                    lock.release()
                    released = True
    except GeneratorExit:
        if not released:
            lock.release()
        raise


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
