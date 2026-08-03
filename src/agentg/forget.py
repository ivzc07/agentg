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
from sqlalchemy import delete, insert, select, update
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

# Centralized predicate: statuses that block model turns, linking, and
# new forget-me requests.  Used by Linking and runtime so they never
# diverge (fix-r22).
STATUS_BLOCKING = [STATUS_DELETING, STATUS_CONSUMED]

# Default stale-lease recovery threshold: a turn lease older than this
# is reclaimed so a crashed runtime cannot strand deletion forever.
# With heartbeat renewal every THIRD of this interval, a live Runner
# keeps its lease fresh; a crash stops the heartbeat and the lease
# becomes reclaimable within this bound.
# Configured via STALE_LEASE_SECONDS env var (default: 30s).
DEFAULT_STALE_LEASE_SECONDS = 30

# Minimum viable stale-lease interval: shorter values risk reclaiming
# a live lease whose heartbeat delayed momentarily.
_MIN_STALE_LEASE_SECONDS = 30


class ForgetStore:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        stale_lease_seconds: int = DEFAULT_STALE_LEASE_SECONDS,
    ) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine)
        self.stale_lease_seconds = max(stale_lease_seconds, _MIN_STALE_LEASE_SECONDS)
        # Heartbeat interval: beat at most a third of the stale bound so
        # two consecutive heartbeats can be lost before the lease looks
        # stale (margin for GC pauses, scheduler load).
        self._heartbeat_seconds = max(1, self.stale_lease_seconds // 3)
        # Test-only hook: called between the read and upsert in
        # request_forget_me (issue #212, fix-r6 barrier test).
        self._pre_upsert_hook: Callable[[], Awaitable[None]] | None = None  # type: ignore[assignment]
        # Per-Member heartbeat tasks — cancelled on release.  Keyed by
        # (member_id, owner_token) so a stale owner's heartbeat can't
        # fight the new owner's lease (fix-r21).
        self._heartbeat_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        # Test-only hooks for barrier tests (fix-r12 R3, fix-r19).
        # _pre_write_lock_hook: after SELECT FOR UPDATE but before the
        #   noop UPDATE that acquires the real SQLite write lock.  Both
        #   acquire_model_turn_lease and claim_forget_me_request call it.
        # _post_acquire_lock_hook / _post_claim_lock_hook: after the real
        #   write lock, before business logic.
        self._pre_write_lock_hook: Callable[[int], Awaitable[None]] | None = None  # type: ignore[assignment]
        self._post_acquire_lock_hook: Callable[[int], Awaitable[None]] | None = None  # type: ignore[assignment]
        self._post_claim_lock_hook: Callable[[int], Awaitable[None]] | None = None  # type: ignore[assignment]

    async def forget_member(self, member_id: int) -> None:
        """Hard-delete every trace of a Member. Idempotent: a second call on an
        already-forgotten Member is a no-op, not an error.

        The conversation history (the SDK's own tables) is cleared FIRST, then
        the domain rows in one transaction. The two stores can't share a
        transaction, so ordering is the guarantee: if the history clear fails,
        nothing else has run and the still-linked Member can simply retry; the
        Member's channel identity is only removed once the domain delete
        commits, so we never strand orphaned history behind a cold-started id.

        fix-r22: The ModelTurnLease is deleted BEFORE the session clear so a
        concurrent stale Runner's FencedSession writes (add_items) see the
        missing/incremented fence and become no-ops — no chat-history residue
        from a reclaimed Runner survives deletion.
        """
        # 0. Revoke fence FIRST so a concurrent stale Runner's SDK writes
        #    become no-ops before we clear history (fix-r22).
        async with self._sessions() as db:
            await db.execute(
                delete(ModelTurnLease).where(ModelTurnLease.member_id == member_id)
            )
            await db.commit()
        # 1. Conversation history — the most sensitive residue — goes next.
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

        Atomically creates a row only when no active pending request exists
        for this Member (INSERT … ON CONFLICT DO NOTHING).  When a pending
        row already exists — including one concurrently created by another
        runtime — the method returns the *stored* phrase from that single
        persisted row, so every warning is confirmable even under concurrent
        initial requests (issue #212, fix-r14 P2).

        Expired pending rows are silently removed before the insert so a
        re-request after expiry creates a fresh phrase.

        A row with status ``deleting`` (or legacy ``consumed`` — deletion
        already confirmed but not yet completed) is NEVER reset to ``pending``
        — the runtime handles these rows by completing deletion before this
        method is called, so the guard here is defense in depth (issue #212,
        fix-r5 P1).

        fix-r24 #4: The persisted row can disappear between the atomic
        upsert commit and the re-read (e.g. a concurrent ``cancel_forget_me``
        on a wrong-phrase handler).  The method retries the create+read
        cycle boundedly; if the row keeps disappearing it returns the empty
        sentinel so the runtime sends truthful guidance — never a local
        phrase that wasn't persisted.

        Model-turn leases live in the separate ``ModelTurnLease`` table and
        are never touched by this method — a ``request_forget_me`` can never
        overwrite or clear an active model-turn gate (issue #212, fix-r11).
        """
        _MAX_RETRIES = 3

        # P1 fast-path read in its own transaction so a concurrent
        # runtime can interleave between this read and the upsert below
        # (the upsert's ON CONFLICT DO NOTHING is the real guard).
        async with self._sessions() as db:
            existing = await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id
                )
            )
            if existing is not None and existing.status in (
                STATUS_BLOCKING
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

        # fix-r24 #4: retry loop — if the row disappears between the
        # upsert commit and the re-read (concurrent cancel as wrong
        # phrase), retry the atomic create+read boundedly.
        for attempt in range(_MAX_RETRIES):
            stored = await self._atomic_create_and_read(
                member_id, gym_id, now, lifetime_seconds, language
            )
            if stored is not None:
                return stored
            # Row disappeared between write and read — retry.

        # Exhausted retries — the row keeps disappearing (concurrent
        # cancel loop).  Return sentinel so the runtime gives truthful
        # guidance rather than a local phrase that was never persisted.
        return ""

    async def _atomic_create_and_read(
        self,
        member_id: int,
        gym_id: int,
        now: datetime,
        lifetime_seconds: int,
        language: str,
    ) -> str | None:
        """Atomically create or read a pending ForgetMeRequest row and
        return its persisted confirmation phrase.

        Returns ``None`` when the row disappeared between the upsert
        commit and the re-read — the caller must retry or give up.
        Returns the empty string when the row has a blocking status
        (deleting) — the caller must not proceed.
        """
        phrase = "DELETE-ME-" + secrets.token_hex(3).upper()
        expires_at = now + timedelta(seconds=lifetime_seconds)

        async with self._sessions() as db:
            # Remove expired pending rows — a new request after expiry
            # should create a fresh phrase, not resurrect the old one.
            await db.execute(
                delete(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.status == STATUS_PENDING,
                    ForgetMeRequest.expires_at <= now,
                )
            )

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
            stmt = stmt.on_conflict_do_nothing(index_elements=["member_id"])
            await db.execute(stmt)
            await db.commit()

        # Post-upsert: always re-read the single persisted row so every
        # caller — concurrent or sequential — returns the same winning
        # phrase (issue #212, fix-r14 P2).
        async with self._sessions() as db:
            existing_after = await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id
                )
            )
            if existing_after is not None and existing_after.status in (
                STATUS_BLOCKING
            ):
                # Row became deleting/consumed between our operations
                # (a concurrent claim won).
                await db.commit()
                return ""  # sentinel: deleting row exists

            if existing_after is not None:
                # Return the single persisted winning phrase — not our
                # locally generated one — so every warning is confirmable.
                stored_phrase: str = existing_after.confirmation_phrase
                await db.commit()
                return stored_phrase

            await db.commit()

        # No row exists — the row disappeared between the upsert and
        # the re-read (e.g. a concurrent cancel).  Return None so the
        # caller can retry (fix-r24 #4).
        return None

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
                    ForgetMeRequest.status.in_(STATUS_BLOCKING),
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

    async def cancel_forget_me(
        self,
        member_id: int,
        *,
        confirmation_phrase: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        """Remove any pending confirmation without deleting Member data.

        Only cancels rows still in ``pending`` status — a ``deleting``
        (or legacy ``consumed``) row means deletion is in progress and
        must not be disturbed.

        When ``confirmation_phrase`` and ``expires_at`` are supplied,
        only the EXACT pending request observed by the caller is deleted
        — a stale wrong-message handler cannot delete a newer request
        created concurrently (fix-r19).
        """
        async with self._sessions() as db:
            stmt = (
                delete(ForgetMeRequest)
                .where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.status == STATUS_PENDING,
                )
            )
            if confirmation_phrase is not None and expires_at is not None:
                stmt = stmt.where(
                    ForgetMeRequest.confirmation_phrase == confirmation_phrase,
                    ForgetMeRequest.expires_at == expires_at,
                )
            await db.execute(stmt)
            await db.commit()

    # -- Model-turn lease (issue #212, fix-r11) ------------------------------

    async def acquire_model_turn_lease(
        self, member_id: int, gym_id: int
    ) -> str | None:
        """Atomically check that no deletion is in progress and acquire an
        exclusive model-turn lease, serialised with
        ``claim_forget_me_request`` via a shared Member-row lock (fix-r12).

        Uses ``engine.begin()`` for an explicit transaction.  Postgres
        serialises via ``SELECT … FOR UPDATE`` row-level locks; SQLite
        ignores ``FOR UPDATE`` so a noop UPDATE on the Member row after
        the SELECT forces a real write lock — only one connection can
        proceed past this point (fix-r19).

        Only reclaims a lease that is explicitly stale; a live lease owned
        by another runtime is never touched (fix-r12).

        Returns the per-turn immutable owner_token (UUID string) when the
        model may proceed safely, or ``None`` when the gate is closed.
        The caller captures this token in a local turn scope and passes it
        to heartbeat/release — a stale/reclaimed runtime can never
        overwrite or delete the new owner's lease (fix-r21).
        """
        now = datetime.now(UTC)
        stale_cutoff = now - timedelta(seconds=self.stale_lease_seconds)
        owner_token = secrets.token_hex(16)  # 32-char hex = 128-bit random

        async with self.engine.begin() as conn:
            # Lock the Member row to serialize with claim_forget_me_request.
            # On Postgres this is a row-level lock; on SQLite WAL the
            # FOR UPDATE is a no-op so we follow with a noop UPDATE that
            # takes the real SQLite write lock (fix-r19).
            member_row = await conn.execute(
                select(Member.id).where(Member.id == member_id).with_for_update()
            )
            if member_row.first() is None:
                return None  # Member doesn't exist

            # Test-only barrier hook — fires after SELECT FOR UPDATE but
            # before the real write lock so both tasks can be paused at
            # this point for simultaneous-race tests (fix-r19).
            if self._pre_write_lock_hook is not None:
                await self._pre_write_lock_hook(member_id)

            # Take the real SQLite write lock via a noop UPDATE — on
            # Postgres the row is already locked by FOR UPDATE so this
            # is a harmless no-op.  Only one connection can hold the
            # SQLite write lock, so concurrent acquire / claim calls
            # are now serialised before the business-logic checks.
            await conn.execute(
                update(Member).where(Member.id == member_id).values(id=member_id)
            )

            # Test-only barrier hook — fires after the real write lock is
            # held so a concurrent claim is serialised (fix-r12 R3, fix-r19).
            if self._post_acquire_lock_hook is not None:
                await self._post_acquire_lock_hook(member_id)

            # 1. Reject when a blocking (deleting/consumed) request exists.
            del_result = await conn.execute(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.status.in_(STATUS_BLOCKING),
                )
            )
            if del_result.first() is not None:
                return None

            # 2. Check existing lease — use table columns for Core connection.
            lease_row = (
                await conn.execute(
                    select(
                        ModelTurnLease.member_id,
                        ModelTurnLease.acquired_at,
                    ).where(ModelTurnLease.member_id == member_id)
                )
            ).first()

            if lease_row is None:
                # The noop UPDATE above serialises access on SQLite, so
                # only one transaction can reach this insert.  ON CONFLICT
                # DO NOTHING is defense-in-depth for the stale-recovery
                # path (two concurrent reclaimers racing below).
                dialect_name = self.engine.sync_engine.dialect.name
                if dialect_name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as _dialect_insert
                else:
                    from sqlalchemy.dialects.sqlite import insert as _dialect_insert

                stmt = _dialect_insert(ModelTurnLease).values(
                    member_id=member_id,
                    gym_id=gym_id,
                    acquired_at=now,
                    owner_token=owner_token,
                )
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["member_id"]
                )
                result = await conn.execute(stmt)
                if getattr(result, "rowcount", 0) == 0:
                    # Defense-in-depth: the unique constraint fired.
                    # Return None so the caller handles the loss.
                    return None
                self._start_heartbeat(member_id, owner_token)
                return owner_token

            # 3. Stale-lease recovery: only reclaim if explicitly stale.
            #    Use rowcount to verify OUR update won — two concurrent
            #    reclaimers cannot both succeed (fix-r12).
            #    The new owner_token fences the old owner: heartbeat and
            #    release from the stale runtime become no-ops after the
            #    reclaim because their token no longer matches (fix-r21).
            if lease_row.acquired_at < stale_cutoff:
                result = await conn.execute(
                    update(ModelTurnLease)
                    .where(
                        ModelTurnLease.member_id == member_id,
                        ModelTurnLease.acquired_at < stale_cutoff,
                    )
                    .values(acquired_at=now, gym_id=gym_id, owner_token=owner_token)
                )
                if getattr(result, "rowcount", 0) > 0:
                    self._start_heartbeat(member_id, owner_token)
                    return owner_token

            return None

    async def release_model_turn_lease(
        self, member_id: int, owner_token: str | None = None
    ) -> None:
        """Release our model-turn lease so another runtime (or a claim)
        can proceed.  Must be called in a ``finally`` block so a crash
        during ``Runner.run()`` cannot strand deletion (issue #212, fix-r11).

        When *owner_token* is provided (the per-turn immutable token returned
        by ``acquire_model_turn_lease``), only a lease row matching BOTH
        ``member_id`` AND ``owner_token`` is deleted — a stale/reclaimed
        runtime can never delete the new owner's lease (fix-r21).

        When *owner_token* is ``None`` (legacy caller or no lease was ever
        acquired), the call is a no-op — no blanket delete is performed.

        Idempotent: releasing when no lease exists, or when the token
        doesn't match, is a no-op.
        """
        if owner_token is None:
            return  # No token → we never acquired a lease — no-op
        # Cancel the heartbeat for this (member_id, owner_token) key so it
        # doesn't bump the row while we're trying to delete it (fix-r20).
        await self._stop_heartbeat(member_id, owner_token)
        async with self._sessions() as db:
            result = await db.execute(
                delete(ModelTurnLease).where(
                    ModelTurnLease.member_id == member_id,
                    ModelTurnLease.owner_token == owner_token,
                )
            )
            await db.commit()

    # -- Heartbeat renewal (issue #212, fix-r20) ---------------------------

    def _start_heartbeat(self, member_id: int, owner_token: str) -> None:
        """Start a background task that periodically bumps
        ``acquired_at`` on our lease row so a live Runner is never
        reclaimed by a concurrent runtime.

        The heartbeat UPDATE is guarded by BOTH ``member_id`` AND
        ``owner_token`` — after a stale reclaim replaces the token,
        the old owner's heartbeat becomes a no-op (fix-r21).

        The heartbeat interval is at most a third of the stale-lease
        bound, so two consecutive heartbeats can be lost (GC pause,
        scheduler load) before the lease looks stale.
        """
        key = (member_id, owner_token)
        # Cancel any prior heartbeat for this key (defense-in-depth).
        existing = self._heartbeat_tasks.pop(key, None)
        if existing is not None:
            existing.cancel()

        _TRANSIENT_DB_ERRORS = (
            "database is locked",
            "OperationalError",
            "TimeoutError",
        )

        async def _beat() -> None:
            interval = self._heartbeat_seconds
            consecutive_failures = 0
            max_consecutive = 3
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        now = datetime.now(UTC)
                        async with self._sessions() as db:
                            result = await db.execute(
                                update(ModelTurnLease)
                                .where(
                                    ModelTurnLease.member_id == member_id,
                                    ModelTurnLease.owner_token == owner_token,
                                )
                                .values(acquired_at=now)
                            )
                            await db.commit()
                            if getattr(result, "rowcount", 0) == 0:
                                # Token mismatch — our lease was reclaimed by
                                # another runtime.  Stop beating gracefully.
                                break
                        consecutive_failures = 0  # reset on success
                    except Exception as exc:
                        exc_str = str(exc)
                        if any(
                            transient in exc_str
                            for transient in _TRANSIENT_DB_ERRORS
                        ):
                            consecutive_failures += 1
                            if consecutive_failures >= max_consecutive:
                                break  # too many transient failures
                            continue  # retry next interval
                        # Non-transient error — stop beating permanently.
                        break
            finally:
                # Self-cleanup: remove our key so a stale heartbeat task
                # can never linger in the dict (fix-r22).
                self._heartbeat_tasks.pop(key, None)

        self._heartbeat_tasks[key] = asyncio.create_task(_beat())

    async def _stop_heartbeat(
        self, member_id: int, owner_token: str
    ) -> None:
        """Cancel and await the heartbeat task for *(member_id, owner_token)*."""
        key = (member_id, owner_token)
        task = self._heartbeat_tasks.pop(key, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def model_turn_lease_exists(self, member_id: int) -> bool:
        """Return True when a model-turn lease exists for this Member.

        Used by non-critical-path checks; the real gate is the atomic
        Member-row lock inside ``acquire_model_turn_lease`` and
        ``claim_forget_me_request`` (fix-r12).
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
        phrase matches, hasn't expired yet, and no non-stale model-turn
        lease exists — serialised with ``acquire_model_turn_lease`` via a
        shared Member-row lock so both cannot succeed concurrently (fix-r12).

        Uses ``engine.begin()`` for an explicit transaction.  Postgres
        serialises via ``SELECT … FOR UPDATE`` row-level locks; SQLite
        ignores ``FOR UPDATE`` so a noop UPDATE on the Member row after
        the SELECT forces a real write lock — only one connection can
        proceed past this point (fix-r19).

        Returns the claimed request (for language mirroring) or None if the
        claim lost.
        """
        stale_cutoff = now - timedelta(seconds=self.stale_lease_seconds)

        async with self.engine.begin() as conn:
            # Lock the Member row to serialize with acquire_model_turn_lease.
            # On Postgres this is a row-level lock; on SQLite WAL the
            # FOR UPDATE is a no-op so we follow with a noop UPDATE that
            # takes the real SQLite write lock (fix-r19).
            member_row = await conn.execute(
                select(Member.id).where(Member.id == member_id).with_for_update()
            )
            if member_row.first() is None:
                return None  # Member doesn't exist

            # Test-only barrier hook — fires after SELECT FOR UPDATE but
            # before the real write lock so both tasks can be paused at
            # this point for simultaneous-race tests (fix-r19).
            if self._pre_write_lock_hook is not None:
                await self._pre_write_lock_hook(member_id)

            # Take the real SQLite write lock via a noop UPDATE — on
            # Postgres the row is already locked by FOR UPDATE so this
            # is a harmless no-op.  Only one connection can hold the
            # SQLite write lock, so concurrent claim / acquire calls
            # are now serialised before the business-logic checks.
            await conn.execute(
                update(Member).where(Member.id == member_id).values(id=member_id)
            )

            # Test-only barrier hook — fires after the real write lock is
            # held so a concurrent acquire is serialised (fix-r12 R3, fix-r19).
            if self._post_claim_lock_hook is not None:
                await self._post_claim_lock_hook(member_id)

            # 1. Check for a non-stale model-turn lease.  A stale lease
            #    means the owning runtime crashed — the claim may proceed.
            lease_row = (
                await conn.execute(
                    select(ModelTurnLease.acquired_at).where(
                        ModelTurnLease.member_id == member_id
                    )
                )
            ).first()
            if lease_row is not None and lease_row.acquired_at >= stale_cutoff:
                return None  # Active lease — claim must lose

            # 2. Atomic compare-and-claim on ForgetMeRequest.
            result = await conn.execute(
                update(ForgetMeRequest)
                .where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.confirmation_phrase == confirmation_phrase,
                    ForgetMeRequest.expires_at > now,
                    ForgetMeRequest.status == STATUS_PENDING,
                )
                .values(status=STATUS_DELETING)
            )
            rowcount = getattr(result, "rowcount", 0)
            if rowcount == 0:
                return None

            # Read the claimed row to return language info.  Use the
            # session maker so the returned ORM instance is usable after
            # the transaction closes (its column attributes are loaded).
            pass  # fall through to post-transaction read

        # Read the claimed row outside the lock — the atomic UPDATE above
        # already settled the winner; this read is just for the caller.
        async with self._sessions() as db:
            claimed = await db.scalar(
                select(ForgetMeRequest).where(
                    ForgetMeRequest.member_id == member_id,
                    ForgetMeRequest.status == STATUS_DELETING,
                )
            )
            if claimed is not None:
                db.expunge(claimed)
            return claimed

    async def get_lease_owner_token(self, member_id: int) -> str | None:
        """Return the current lease owner_token for *member_id*, or None
        when no lease exists.  Used by FencedSession for SDK write fencing
        (fix-r22).
        """
        async with self._sessions() as db:
            row = await db.scalar(
                select(ModelTurnLease.owner_token).where(
                    ModelTurnLease.member_id == member_id
                )
            )
            return row

    async def stop_lease_heartbeat(
        self, member_id: int, owner_token: str
    ) -> None:
        """Stop the heartbeat for *(member_id, owner_token)* without
        deleting the lease row.  The lease remains valid for fencing
        (compaction) but will become stale without renewal — a dropped
        after_send can no longer block a later confirmation indefinitely
        (fix-r24 #3).

        Idempotent: a second call or a subsequent release_model_turn_lease
        with the same token is harmless."""
        await self._stop_heartbeat(member_id, owner_token)

    async def is_lease_held_by_other(
        self, member_id: int, our_token: str | None
    ) -> bool:
        """Return True when a non-stale lease exists owned by someone
        other than *our_token* — the owning runtime is active and our
        operation must not race through.

        When *our_token* is ``None``, any non-stale lease is "held by
        other" because we have no token at all.

        Used by the runtime to detect a dropped-after_send lease on a
        pending confirmation (fix-r22 P1 #1).
        """
        now = datetime.now(UTC)
        stale_cutoff = now - timedelta(seconds=self.stale_lease_seconds)
        async with self._sessions() as db:
            if our_token is None:
                # Any non-stale lease is "other" — we have no token.
                row = await db.scalar(
                    select(ModelTurnLease.owner_token).where(
                        ModelTurnLease.member_id == member_id,
                        ModelTurnLease.acquired_at >= stale_cutoff,
                    )
                )
                return row is not None
            row = await db.scalar(
                select(ModelTurnLease.owner_token).where(
                    ModelTurnLease.member_id == member_id,
                    ModelTurnLease.acquired_at >= stale_cutoff,
                    ModelTurnLease.owner_token != our_token,
                )
            )
            return row is not None


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
