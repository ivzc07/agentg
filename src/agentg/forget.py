"""Forget-me: a Member's complete hard delete (spec §Privacy & data retention).

A Member asks in chat, the system persists an expiring confirmation, and
only the exact confirmation phrase in a later private message triggers the
wipe — deterministic, model-free, two-turn (issue #212).
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from agents.extensions.memory import SQLAlchemySession
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.models import (
    DashboardLoginToken,
    ForgetMeRequest,
    Member,
    MemberChannel,
    MemberNote,
    ModelTurnLease,
    Routine,
    Session,
    Set,
    Workout,
    WorkoutExercise,
)

# Forget-me trigger phrases grouped by language so the deterministic
# reply language can be derived from the raw text that started the flow
# (ADR-0002: mirror the Member, default Spanish when no signal).
_FORGET_ME_TRIGGERS_EN: tuple[str, ...] = (
    "forget me",
    "delete my data",
    "delete my account",
    "delete my info",
    "erase my data",
    "erase me",
)

_FORGET_ME_TRIGGERS_ES: tuple[str, ...] = (
    "olvídame",
    "bórrame",
    "elimíname",
    "borra mi cuenta",
    "borrar mi cuenta",
    "borra mis datos",
    "borrar mis datos",
    "elimina mis datos",
    "eliminar mis datos",
    "elimina mi cuenta",
    "eliminar mi cuenta",
    "borra mi información",
    "borrar mi información",
)

# Combined list for backward-compatible is_forget_me_request checks.
_FORGET_ME_TRIGGERS: tuple[str, ...] = _FORGET_ME_TRIGGERS_EN + _FORGET_ME_TRIGGERS_ES

# ForgetMeRequest.status values (issue #212).
STATUS_PENDING = "pending"
STATUS_DELETING = "deleting"
STATUS_CONSUMED = "consumed"  # legacy — no longer written; kept for migration compat

# Bounded stale-lease recovery threshold: a turn lease older than this
# is reclaimed so a crashed runtime cannot strand deletion forever
# (issue #212, fix-r10).
STALE_LEASE_SECONDS = 30


class ForgetStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine)
        # Test-only hook: called between the read and upsert in
        # request_forget_me (issue #212, fix-r6 barrier test).
        self._pre_upsert_hook: Callable[[], Awaitable[None]] | None = None  # type: ignore[assignment]

    async def forget_member(self, member_id: int) -> None:
        """Hard-delete every trace of a Member. Idempotent: a second call on an
        already-forgotten Member is a no-op, not an error.

        The conversation history (the SDK's own tables) is cleared FIRST, then
        the domain rows in one transaction. The two stores can't share a
        transaction, so ordering is the guarantee: if the history clear fails,
        nothing else has run and the still-linked Member can simply retry; the
        Member's channel identity is only removed once the domain delete
        commits, so we never strand orphaned history behind a cold-started id.
        """
        # 1. Conversation history — the most sensitive residue — goes first.
        await SQLAlchemySession(f"member:{member_id}", engine=self.engine).clear_session()

        # 2. Domain rows, atomically. Child rows before parents so foreign keys
        #    never block the delete (Postgres); the channel identity dies last.
        member_session_ids = select(Session.id).where(Session.member_id == member_id)
        member_routine_ids = select(Routine.id).where(Routine.member_id == member_id)
        member_workout_ids = select(Workout.id).where(Workout.routine_id.in_(member_routine_ids))

        async with self._sessions() as db:
            await db.execute(delete(Set).where(Set.session_id.in_(member_session_ids)))
            await db.execute(delete(Session).where(Session.member_id == member_id))
            await db.execute(
                delete(WorkoutExercise).where(WorkoutExercise.workout_id.in_(member_workout_ids))
            )
            await db.execute(delete(Workout).where(Workout.routine_id.in_(member_routine_ids)))
            await db.execute(delete(Routine).where(Routine.member_id == member_id))
            await db.execute(delete(MemberNote).where(MemberNote.member_id == member_id))
            # References OTHER Members' rows hold back: a forgotten Coach's
            # Routine actor stamps stay coach-authored but by nobody (NULL) —
            # the chip degrades to plain "Coach-authored" (issue #91), and
            # the Member delete below can't trip the FK (Postgres would abort
            # the whole wipe).
            await db.execute(
                update(Routine)
                .where(Routine.created_by_member_id == member_id)
                .values(created_by_member_id=None)
            )
            # Same for the safety-flag tick-offs: they stay acknowledged but
            # by nobody (NULL), and the Member's dashboard login tokens die
            # with them — residue-free.
            await db.execute(
                update(MemberNote)
                .where(MemberNote.acknowledged_by_member_id == member_id)
                .values(acknowledged_by_member_id=None)
            )
            await db.execute(
                delete(DashboardLoginToken).where(DashboardLoginToken.member_id == member_id)
            )
            # Pending confirmation dies with the Member; must run before the
            # Member delete so the FK doesn't block.
            await db.execute(
                delete(ForgetMeRequest).where(ForgetMeRequest.member_id == member_id)
            )
            # Release the model-turn lease (if any) during deletion so a
            # crashed runtime's lease doesn't block re-linking.  Must run
            # before the Member delete (FK constraint).
            await db.execute(
                delete(ModelTurnLease).where(ModelTurnLease.member_id == member_id)
            )
            await db.execute(delete(MemberChannel).where(MemberChannel.member_id == member_id))
            await db.execute(delete(Member).where(Member.id == member_id))
            await db.commit()

    # -- Two-turn confirmation (issue #212) -----------------------------------

    async def request_forget_me(
        self,
        member_id: int,
        gym_id: int,
        now: datetime,
        lifetime_seconds: int,
        language: str = "es",
    ) -> str:
        """Persist an expiring confirmation and return the exact phrase the
        Member must send to complete deletion.

        Uses a real database atomic upsert (INSERT … ON CONFLICT DO UPDATE)
        so two concurrent initial requests across processes both succeed
        without an IntegrityError on the unique member_id constraint
        (issue #212, P2).

        A row with status ``deleting`` (or legacy ``consumed`` — deletion
        already confirmed but not yet completed) is NEVER reset to ``pending``
        — the runtime handles these rows by completing deletion before this
        method is called, so the guard here is defense in depth (issue #212,
        fix-r5 P1).

        Model-turn leases live in the separate ``ModelTurnLease`` table and
        are never touched by this method — a ``request_forget_me`` can never
        overwrite or clear an active model-turn gate (issue #212, fix-r11).
        """
        phrase = "DELETE-ME-" + secrets.token_hex(3).upper()
        expires_at = now + timedelta(seconds=lifetime_seconds)

        # P1 fast-path read in its own transaction so a concurrent
        # runtime can interleave between this read and the upsert below
        # (the upsert's WHERE clause is the real guard).
        async with self._sessions() as db:
            existing = await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id
                )
            )
            if existing is not None and existing.status not in (
                STATUS_PENDING,
            ):
                # Deletion is already in progress; the caller must
                # complete it, not overwrite the row.
                await db.commit()
                return ""  # sentinel: deleting row exists

        # Test-only barrier hook — enables true interleaving tests
        # where a concurrent runtime consumes between read and upsert
        # (issue #212, fix-r6).
        if self._pre_upsert_hook is not None:
            await self._pre_upsert_hook()

        # P2: Atomic upsert with a WHERE guard on the conflict action.
        # If the row became deleting/consumed between the read above and
        # this upsert, the WHERE clause prevents the overwrite — the
        # DO UPDATE only fires on a still-pending row.
        async with self._sessions() as db:
            values = dict(
                member_id=member_id,
                gym_id=gym_id,
                confirmation_phrase=phrase,
                expires_at=expires_at,
                created_at=now,
                language=language,
                status=STATUS_PENDING,
            )
            dialect_name = self.engine.sync_engine.dialect.name
            if dialect_name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as _dialect_insert
            else:
                from sqlalchemy.dialects.sqlite import insert as _dialect_insert

            stmt = _dialect_insert(ForgetMeRequest).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["member_id"],
                set_=dict(
                    gym_id=gym_id,
                    confirmation_phrase=phrase,
                    expires_at=expires_at,
                    created_at=now,
                    language=language,
                    status=STATUS_PENDING,
                ),
                where=(ForgetMeRequest.status == STATUS_PENDING),
            )
            await db.execute(stmt)
            await db.commit()

        # P1 post-upsert re-check: if the row became deleting/consumed
        # between our read and upsert, the WHERE clause prevented the
        # overwrite.  Return the sentinel so the caller can recover.
        async with self._sessions() as db:
            existing_after = await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id
                )
            )
            if existing_after is not None and existing_after.status not in (
                STATUS_PENDING,
            ):
                await db.commit()
                return ""  # sentinel: deleting row exists
            await db.commit()

        return phrase

    async def get_pending_request(
        self, member_id: int
    ) -> ForgetMeRequest | None:
        """Return the pending confirmation for this Member, or None.

        Only returns rows with ``status == 'pending'`` — a ``deleting``
        row means deletion is in progress (or interrupted), which is
        handled by ``get_deleting_request``.
        """
        async with self._sessions() as db:
            return await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.status == STATUS_PENDING,
                )
            )

    async def get_deleting_request(
        self, member_id: int
    ) -> ForgetMeRequest | None:
        """Return a deleting (in-progress or interrupted) deletion request.

        When this returns a row, the confirmation was already claimed by
        a winner — the caller must complete the deletion (if interrupted)
        or return a safe reply without reaching the model.

        Also matches legacy ``consumed`` rows for migration compat.
        """
        async with self._sessions() as db:
            return await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.status.in_([STATUS_DELETING, STATUS_CONSUMED]),
                )
            )

    async def get_deleting_by_phrase(
        self, member_id: int, confirmation_phrase: str, now: datetime
    ) -> ForgetMeRequest | None:
        """Return a deleting request whose confirmation phrase still matches
        — the retry primitive for partial-failure recovery.

        Expiry is NOT checked here: expiry limits the initial confirmation
        (pending → deleting) only, not completion of an already-claimed
        deletion.  Once deletion is confirmed, sending the exact phrase
        resumes it regardless of how much time has passed (issue #212,
        fix-r7 P2).

        Only a message carrying the exact confirmation phrase can resume
        deletion; any other message falls through to normal processing.
        """
        async with self._sessions() as db:
            return await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.confirmation_phrase == confirmation_phrase,
                    ForgetMeRequest.status == STATUS_DELETING,
                )
            )

    async def cancel_forget_me(self, member_id: int) -> None:
        """Remove any pending confirmation without deleting Member data.

        Only cancels rows still in ``pending`` status — a ``deleting``
        (or legacy ``consumed``) row means deletion is in progress and
        must not be disturbed.
        """
        async with self._sessions() as db:
            await db.execute(
                delete(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.status == STATUS_PENDING,
                )
            )
            await db.commit()

    # -- Model-turn lease (issue #212, fix-r11) ------------------------------

    async def acquire_model_turn_lease(
        self, member_id: int, gym_id: int
    ) -> bool:
        """Atomically check that no deletion is in progress and acquire an
        exclusive model-turn lease in the separate ``ModelTurnLease`` table,
        closing the no-row-read→Runner race across runtimes (issue #212,
        fix-r11).

        Steps:
        1. If a ``ForgetMeRequest`` with status ``deleting`` exists → False.
        2. Try INSERT into ``ModelTurnLease`` with ON CONFLICT DO NOTHING.
        3. If our INSERT succeeded (rowcount > 0) → True.
        4. If a competing row exists, check for stale-lease recovery.
        5. If stale, UPDATE the stale row (reclaim it); otherwise False.

        Returns ``True`` when the model may proceed safely.  Returns
        ``False`` when a deleting request exists or another runtime holds
        a non-stale lease.
        """
        now = datetime.now(UTC)
        stale_cutoff = now - timedelta(seconds=STALE_LEASE_SECONDS)

        async with self._sessions() as db:
            # 1. Reject when a deleting request exists — deletion is
            #    confirmed and in progress; the model must never run.
            deleting = await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.status.in_([STATUS_DELETING, STATUS_CONSUMED]),
                )
            )
            if deleting is not None:
                await db.commit()
                return False

            # 2. Try to insert a fresh lease row.
            dialect_name = self.engine.sync_engine.dialect.name
            if dialect_name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as _dialect_insert  # type: ignore[assignment]
            else:
                from sqlalchemy.dialects.sqlite import insert as _dialect_insert  # type: ignore[assignment]

            stmt = _dialect_insert(ModelTurnLease).values(
                member_id=member_id,
                gym_id=gym_id,
                acquired_at=now,
            )
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["member_id"]
            )
            result = await db.execute(stmt)
            await db.commit()

            # 3. Our INSERT succeeded — the row is new and we own the lease.
            rowcount = getattr(result, "rowcount", 0)
            if rowcount > 0:
                return True

        # 4. INSERT was a no-op — a lease row already exists.
        #    Check for stale-lease recovery.
        async with self._sessions() as db:
            lease = await db.scalar(
                select(ModelTurnLease).where(
                    ModelTurnLease.member_id == member_id
                )
            )
            if lease is None:
                # Race: the row disappeared between the no-op insert and
                # this read.  Retry once by recursing.
                await db.commit()
                return await self.acquire_model_turn_lease(member_id, gym_id)

            # 5. Check if the existing lease is stale.
            if lease.acquired_at < stale_cutoff:
                # Reclaim the stale lease.
                await db.execute(
                    update(ModelTurnLease)
                    .where(
                        ModelTurnLease.member_id == member_id,
                        ModelTurnLease.acquired_at < stale_cutoff,
                    )
                    .values(acquired_at=now, gym_id=gym_id)
                )
                await db.commit()
                # Verify the UPDATE succeeded.
                lease2 = await db.scalar(
                    select(ModelTurnLease).where(
                        ModelTurnLease.member_id == member_id
                    )
                )
                if lease2 is not None and lease2.acquired_at >= now - timedelta(seconds=1):
                    return True
                await db.commit()
                return False

            await db.commit()
            return False

    async def release_model_turn_lease(self, member_id: int) -> None:
        """Release the model-turn lease so another runtime (or a claim)
        can proceed.  Must be called in a ``finally`` block so a crash
        during ``Runner.run()`` cannot strand deletion (issue #212, fix-r11).

        Idempotent: releasing when no lease exists is a no-op.
        """
        async with self._sessions() as db:
            await db.execute(
                delete(ModelTurnLease).where(
                    ModelTurnLease.member_id == member_id
                )
            )
            await db.commit()

    async def model_turn_lease_exists(self, member_id: int) -> bool:
        """Return True when a model-turn lease exists for this Member.

        Used by ``claim_forget_me_request`` to reject a claim while a
        model turn is active — the cross-runtime guard (issue #212, fix-r11).
        """
        async with self._sessions() as db:
            row = await db.scalar(
                select(ModelTurnLease).where(
                    ModelTurnLease.member_id == member_id
                )
            )
            return row is not None

    async def claim_forget_me_request(
        self, member_id: int, confirmation_phrase: str, now: datetime
    ) -> ForgetMeRequest | None:
        """Atomically claim the pending request only when the confirmation
        phrase matches, hasn't expired yet, and NO model-turn lease exists
        — closes the TOCTOU between the model-turn gate and a concurrent
        deletion (issue #212, fix-r11).

        Returns the claimed request (for language mirroring) or None if the
        claim lost.

        A single conditional UPDATE (``pending`` → ``deleting``) is the
        compare-and-claim primitive: two concurrent sessions can't both
        update the same row — exactly one sees ``rowcount > 0`` and becomes
        the winner.

        Before attempting the claim, checks the ``ModelTurnLease`` table.
        If a lease exists, a model turn is active and the claim must not
        proceed.  This check has a tiny TOCTOU window between the lease
        check and the UPDATE, but the model-turn gate in ``handle_message``
        catches any loser: if a lease appears after our check, the
        ``acquire_model_turn_lease`` call at the model gate will see the
        now-deleting row and reject the model — the deletion wins.
        """
        # Fast-path: if a model-turn lease exists, fail early.
        if await self.model_turn_lease_exists(member_id):
            return None

        async with self._sessions() as db:
            # Re-check lease inside the same transaction that claims.
            lease = await db.scalar(
                select(ModelTurnLease).where(
                    ModelTurnLease.member_id == member_id
                )
            )
            if lease is not None:
                await db.commit()
                return None

            result = await db.execute(
                update(ForgetMeRequest)
                .where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.confirmation_phrase == confirmation_phrase,
                    ForgetMeRequest.expires_at > now,
                    ForgetMeRequest.status == STATUS_PENDING,
                )
                .values(status=STATUS_DELETING)
            )
            # Use `getattr` for portable SQLAlchemy typing: the dialect's
            # Result proxy may not expose `rowcount` in stubs but always
            # returns it at runtime.
            rowcount = getattr(result, "rowcount", 0)
            if rowcount == 0:
                await db.commit()
                return None
            claimed = await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.status == STATUS_DELETING,
                )
            )
            # Detach the object before commit so callers can read its
            # attributes (language, status, etc.) after the session closes.
            if claimed is not None:
                db.expunge(claimed)
            await db.commit()
            return claimed


# -- Module-level helpers --------------------------------------------------


def is_forget_me_request(text: str) -> bool:
    """True when *text* looks like a Member asking to be forgotten.

    Matches whole phrases (word boundaries) so ordinary text like
    "forget metal" or "erase message" does not trigger.
    """
    return detect_forget_me_language(text) is not None


def detect_forget_me_language(text: str) -> str | None:
    """Return ``"en"``, ``"es"``, or ``None`` depending on which
    language's trigger phrases appear in *text* (English checked first
    so a mixed message picks English; in practice a Member will use one
    or the other).

    The caller defaults to ``"es"`` when this returns ``None`` — mirror
    the Member, safe-default Spanish (ADR-0002).
    """
    collapsed = " ".join(text.lower().split())
    for trigger in _FORGET_ME_TRIGGERS_EN:
        if re.search(r"\b" + re.escape(trigger) + r"\b", collapsed):
            return "en"
    for trigger in _FORGET_ME_TRIGGERS_ES:
        if re.search(r"\b" + re.escape(trigger) + r"\b", collapsed):
            return "es"
    return None


def normalize_confirmation(text: str) -> str:
    """Normalize raw text for confirmation-phrase comparison: collapse
    whitespace and uppercase so the Member's casing and spacing don't
    matter."""
    return " ".join(text.strip().upper().split())


# Per-language signal words for whole-conversation language detection
# (ADR-0002: sticky whole-conversation language, not trigger-text language).
# These are high-frequency words that carry clear language signal — terse
# lift logs like "bench 60 8,8,8" have none of these and carry no signal.
_SPANISH_SIGNAL_WORDS: set[str] = {
    "hola", "gracias", "por", "favor", "quiero", "entrenar", "entrenamiento",
    "rutina", "ejercicio", "peso", "series", "repeticiones", "hoy", "mañana",
    "día", "semana", "bien", "bueno", "buena", "así", "cómo", "qué", "cuál",
    "cuándo", "dónde", "puedo", "puedes", "tengo", "tienes", "hacer",
    "vamos", "claro", "vale", "genial", "perfecto", "ayuda", "duele", "dolor",
    "pecho", "pierna", "brazo", "espalda", "hombro", "músculo", "fuerza",
    "masa", "grasa", "perder", "ganar", "objetivo", "lesión", "calentamiento",
    "descanso", "comida", "dieta", "agua", "suplemento", "proteína",
    "adiós", "hasta", "luego", "nos", "vemos", "ánimo", "fuerte",
    "eso", "muy", "mucho", "más", "menos", "mejor", "peor", "nada", "todo",
    "siempre", "nunca", "tal", "vez", "creo", "pienso",
    "dime", "cuéntame", "explica", "enséñame", "muéstrame",
    "registrado", "anotado", "apuntado", "hecho", "listo",
}

_ENGLISH_SIGNAL_WORDS: set[str] = {
    "hello", "hi", "hey", "thanks", "thank", "please", "want", "train",
    "training", "routine", "exercise", "weight", "sets", "reps", "today",
    "tomorrow", "day", "week", "good", "great", "awesome", "nice", "well",
    "how", "what", "when", "where", "can", "could", "would", "should",
    "have", "has", "do", "does", "did", "let", "go", "going", "come",
    "sure", "ok", "okay", "fine", "cool", "perfect", "help", "hurt", "pain",
    "chest", "leg", "arm", "back", "shoulder", "muscle", "strength",
    "mass", "fat", "lose", "gain", "goal", "injury", "warmup", "warm",
    "rest", "food", "diet", "water", "supplement", "protein",
    "goodbye", "bye", "see", "later", "keep", "strong",
    "that", "very", "much", "more", "less", "better", "worse", "nothing",
    "everything", "always", "never", "maybe", "think", "thought",
    "tell", "explain", "show", "logged", "noted", "done", "ready",
}


def _extract_content_text(content) -> str:
    """Extract plain text from an SDK history item's content field.

    The OpenAI Responses API stores assistant/user content as a list of
    content blocks (e.g. ``[{"type": "text", "text": "¡Hola!"}]``);
    older history may still have plain strings.  This helper collapses
    both shapes into a single string so callers can match against signal
    words without caring about the serialisation format (issue #212, fix-r5).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text", "")
                if isinstance(text, str) and text:
                    parts.append(text)
        return " ".join(parts)
    return ""


async def detect_conversation_language(session) -> str | None:
    """Return ``"en"``, ``"es"``, or ``None`` by scanning the Member's
    SDK chat history for language signal words.  Terse lift logs like
    "bench 60 8,8,8" carry no signal and are skipped.

    Returns ``None`` when there is no clear signal (no history or not
    enough signal words) — the caller must fall back to trigger-text
    language.

    ADR-0002: sticky whole-conversation language, not just the last
    message or the trigger text.
    """
    items = await session.get_items()
    if not items:
        return None

    es_score = 0
    en_score = 0

    for item in items:
        text = _extract_content_text(item.get("content", ""))
        if not text:
            continue
        # Find word-character runs so punctuation (¡Hola! → hola) and
        # list-form content blocks are handled uniformly (issue #212, fix-r5).
        words = set(re.findall(r"\w+", text.lower()))
        es_score += len(words & _SPANISH_SIGNAL_WORDS)
        en_score += len(words & _ENGLISH_SIGNAL_WORDS)

    # Require at least a modest signal before deciding.
    if es_score == 0 and en_score == 0:
        return None
    if es_score > en_score:
        return "es"
    if en_score > es_score:
        return "en"
    # Tie — no clear signal.
    return None
