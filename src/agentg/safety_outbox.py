"""Durable outbox for safety-flag coach notifications (issue #216).

Safety Notes commit with one outbox job per eligible Coach in the same
transaction.  A background worker sends pending jobs without delaying the
Member's reply, recovers on startup, mints authenticated dashboard links at
delivery time, and falls back to text-only when minting fails.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, update

from agentg.models import MemberNote, SafetyOutboxJob

MAX_NOTE_LENGTH = 400
MAX_RETRIES = 3  # transient failures before permanent failure

# Lease: a job claimed longer than this is considered abandoned and is
# reset to pending by the next poll cycle (stale-claim recovery).
LEASE_TIMEOUT_SECONDS = 60

# Backoff: each retry waits retry_count * BASE_BACKOFF seconds before
# the next attempt (linear backoff).
BASE_BACKOFF_SECONDS = 5

Clock = Callable[[], datetime]

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 10  # seconds between pending-job sweeps
_STARTUP_BATCH = 50  # max jobs to drain in one batch


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SafetyOutbox:
    """Create and manage outbox jobs for safety-flag coach notifications."""

    def __init__(self, engine, clock: Clock = _utcnow) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

    async def create_note_and_jobs(
        self,
        member_id: int,
        gym_id: int,
        text: str,
        member_name: str,
        member_is_coach: bool,
        coaches: list[tuple[int, str, str, str]],
    ) -> tuple[MemberNote, list[SafetyOutboxJob]]:
        """Write a safety Note and one outbox job per eligible Coach in one
        transaction — committing neither if the outbox can't be built.

        ``coaches`` is ``(member_id, name, channel, channel_user_id)`` as
        returned by ``LinkingStore.coaches_for_gym``.
        """
        note = MemberNote(
            gym_id=gym_id,
            member_id=member_id,
            kind="safety",
            text=" ".join(text.split())[:MAX_NOTE_LENGTH],
            created_at=self._clock(),
        )
        now = self._clock()
        async with self._sessions() as db:
            db.add(note)
            await db.flush()  # assign note.id
            jobs: list[SafetyOutboxJob] = []
            for coach_id, _coach_name, channel, channel_user_id in coaches:
                job = SafetyOutboxJob(
                    gym_id=gym_id,
                    note_id=note.id,
                    coach_member_id=coach_id,
                    channel=channel,
                    channel_user_id=channel_user_id,
                    member_id=member_id,
                    member_name=member_name,
                    member_is_coach=member_is_coach,
                    status="pending",
                    created_at=now,
                )
                db.add(job)
                jobs.append(job)
            await db.commit()
        return note, jobs

    async def claim_pending(self, limit: int = _STARTUP_BATCH) -> list[SafetyOutboxJob]:
        """Atomically claim the oldest pending jobs by transitioning their
        status from ``pending`` to ``sending`` in one statement.

        Uses a single UPDATE … RETURNING so the claim is atomic even against
        concurrent connections: the subquery selects pending ids and the
        outer UPDATE transitions them in one step.  Two concurrent calls
        can never claim the same job.
        """
        async with self._sessions() as db:
            sub = (
                select(SafetyOutboxJob.id)
                .where(SafetyOutboxJob.status == "pending")
                .order_by(SafetyOutboxJob.created_at)
                .limit(limit)
            )
            now = self._clock()
            result = await db.execute(
                update(SafetyOutboxJob)
                .where(SafetyOutboxJob.id.in_(sub))
                .values(status="sending", claimed_at=now)
                .returning(SafetyOutboxJob)
            )
            rows = result.fetchall()
            await db.commit()
            # fetchall returns Row tuples; extract the ORM objects.
            return [row[0] for row in rows]

    async def mark_delivered(self, job: SafetyOutboxJob) -> None:
        """Mark a single job as delivered — only if it is still in the
        ``sending`` state (a retry may have already reset it)."""
        async with self._sessions() as db:
            await db.execute(
                update(SafetyOutboxJob)
                .where(
                    SafetyOutboxJob.id == job.id,
                    SafetyOutboxJob.status == "sending",
                )
                .values(status="delivered", delivered_at=self._clock(), last_error=None)
            )
            await db.commit()

    async def mark_failed(self, job: SafetyOutboxJob, reason: str) -> None:
        """Mark a single job as permanently failed — only if still ``sending``."""
        async with self._sessions() as db:
            await db.execute(
                update(SafetyOutboxJob)
                .where(
                    SafetyOutboxJob.id == job.id,
                    SafetyOutboxJob.status == "sending",
                )
                .values(
                    status="failed",
                    failure_reason=reason[:400],
                    last_error=reason[:400],
                    delivered_at=self._clock(),
                )
            )
            await db.commit()

    async def reset_for_retry(self, job: SafetyOutboxJob, reason: str) -> None:
        """Increment the retry counter and reset to ``pending`` so the poller
        tries again.  After *MAX_RETRIES* the job is permanently failed.

        ``claimed_at`` is cleared so the next claim can stamp a fresh lease.
        ``last_error`` records the transient reason for diagnostics."""
        async with self._sessions() as db:
            next_count = job.retry_count + 1
            if next_count >= MAX_RETRIES:
                await db.execute(
                    update(SafetyOutboxJob)
                    .where(SafetyOutboxJob.id == job.id)
                    .values(
                        status="failed",
                        retry_count=next_count,
                        failure_reason=f"retries exhausted: {reason}"[:400],
                        last_error=reason[:400],
                        claimed_at=None,
                        delivered_at=self._clock(),
                    )
                )
            else:
                await db.execute(
                    update(SafetyOutboxJob)
                    .where(SafetyOutboxJob.id == job.id)
                    .values(
                        status="pending",
                        retry_count=next_count,
                        last_error=reason[:400],
                        claimed_at=None,
                    )
                )
            await db.commit()

    async def reset_claimed(self) -> int:
        """Reset every ``sending`` job back to ``pending``.

        Called on startup so jobs orphaned by a prior crash (claimed but
        never delivered) are retried.
        """
        async with self._sessions() as db:
            result = await db.execute(
                update(SafetyOutboxJob)
                .where(SafetyOutboxJob.status == "sending")
                .values(status="pending", claimed_at=None)
            )
            await db.commit()
            return result.rowcount

    async def reset_stale_claims(
        self, max_age_seconds: int = LEASE_TIMEOUT_SECONDS
    ) -> int:
        """Reset every ``sending`` job whose lease has expired back to
        ``pending`` so a future poll cycle retries them.

        Called periodically by the poll loop so a hang (not a crash) that
        outlasts the lease timeout does not permanently strand jobs.
        """
        cutoff = self._clock()
        async with self._sessions() as db:
            # Use Python-side cutoff because SQLite has no native datetime
            # arithmetic and the TZDateTime type stores UTC-naive values.
            rows = (
                await db.scalars(
                    select(SafetyOutboxJob).where(
                        SafetyOutboxJob.status == "sending",
                    )
                )
            ).all()
            stale_ids = [
                r.id for r in rows
                if r.claimed_at is not None
                and (cutoff - r.claimed_at).total_seconds() > max_age_seconds
            ]
            if not stale_ids:
                return 0
            await db.execute(
                update(SafetyOutboxJob)
                .where(SafetyOutboxJob.id.in_(stale_ids))
                .values(status="pending", claimed_at=None)
            )
            await db.commit()
            return len(stale_ids)


class OutboxWorker:
    """Background task that sends pending safety-outbox jobs.

    On ``start()`` it resets any claimed-but-undelivered jobs from a prior
    crash and begins polling.  Delivery does not block startup — backlog
    drain is incremental so Member replies are never delayed after a restart.
    """

    def __init__(
        self,
        outbox: SafetyOutbox,
        notifier,  # Notifier protocol — channel adapter
        dashboard_store,  # DashboardStore — to mint magic links
        dashboard_base_url: str | None,
        linking_store,  # LinkingStore — to resolve channel identity at delivery time
        clock: Clock = _utcnow,
    ) -> None:
        self._outbox = outbox
        self._notifier = notifier
        self._dashboard = dashboard_store
        self._base_url = dashboard_base_url
        self._linking = linking_store
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        # Bounded fan-out like the old in-memory pings (P3 #5153518045).
        self._semaphore = asyncio.Semaphore(10)

    async def start(self) -> None:
        """Recover orphaned jobs from a prior crash, then begin polling.

        Unlike the original implementation this does *not* drain the entire
        backlog synchronously — a large backlog would delay all Member
        replies after restart.  The poll loop handles pending jobs
        incrementally.
        """
        await self._outbox.reset_claimed()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Cancel the polling loop; does not drain pending jobs."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def drain_once(self, limit: int = _STARTUP_BATCH) -> int:
        """Claim and deliver one batch of pending jobs.  Public so a shutdown
        hook can do a final drain."""
        jobs = await self._outbox.claim_pending(limit)
        if not jobs:
            return 0
        await asyncio.gather(
            *[self._deliver_one(job) for job in jobs], return_exceptions=True,
        )
        return len(jobs)

    # ── internals ────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while True:
            try:
                # Recover jobs stranded by a hang (not a crash).
                stale = await self._outbox.reset_stale_claims()
                if stale:
                    logger.info("reset %d stale outbox claims", stale)
                await self.drain_once(limit=10)
            except Exception:
                logger.exception("outbox poll cycle failed")
            try:
                await asyncio.sleep(_POLL_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def _deliver_one(self, job: SafetyOutboxJob) -> None:
        """Send one outbox job: heads-up + (optionally) magic link.

        Channel identity is re-resolved at delivery time so a coach who
        switched gyms after job creation never receives a cross-gym
        notification (P1 #2).  Transient failures (notifier, DB) are retried
        up to *MAX_RETRIES* before the job is permanently failed (P1 #1).
        """
        # Backoff: wait before attempting delivery so transient outages
        # (network flap, DB restart) have time to resolve.
        if job.retry_count > 0:
            delay = job.retry_count * BASE_BACKOFF_SECONDS
            await asyncio.sleep(delay)

        try:
            # Resolve the current channel identity at delivery time (P1 #2).
            channel_info = await self._linking.coach_channel_in_gym(
                job.coach_member_id, job.gym_id,
            )
            if channel_info is None:
                return await self._outbox.mark_failed(
                    job, "coach no longer reachable in this gym"
                )

            current_channel, current_channel_user_id = channel_info

            # Resolve the note text; if the note is gone (e.g. forget-me),
            # fail the job — the safety concern no longer exists.
            note_text = await self._note_text(job.note_id)
            if note_text is None:
                return await self._outbox.mark_failed(
                    job, "safety note no longer exists"
                )

            text = f"Heads-up from your member {job.member_name}: {note_text}"

            # Mint a dashboard link at delivery time.
            link: str | None = None
            if self._base_url is not None:
                try:
                    next_path = "/" if job.member_is_coach else f"/members/{job.member_id}"
                    token = await self._dashboard.create_login_token(
                        job.coach_member_id, job.gym_id, next_path=next_path,
                    )
                    link = f"{self._base_url}/login/{token}"
                except Exception:
                    logger.exception(
                        "failed to mint dashboard link for coach %s",
                        current_channel_user_id,
                    )

            # Send heads-up via the current channel identity.
            try:
                async with self._semaphore:
                    await self._notifier.send(
                        current_channel, current_channel_user_id,
                        text, disable_preview=True,
                    )
            except Exception:
                logger.exception(
                    "failed to send heads-up to coach %s", current_channel_user_id,
                )
                return await self._outbox.reset_for_retry(
                    job, "notifier send failed"
                )

            if link is not None:
                try:
                    async with self._semaphore:
                        await self._notifier.send(
                            current_channel,
                            current_channel_user_id,
                            link,
                            disable_preview=True,
                            protect_content=True,
                        )
                except Exception:
                    logger.exception(
                        "failed to send dashboard link to coach %s",
                        current_channel_user_id,
                    )
                    # The heads-up already landed; a missing link is unfortunate
                    # but not worth marking the whole job failed.

            await self._outbox.mark_delivered(job)
        except Exception as exc:
            logger.exception("error delivering outbox job %s", job.id)
            try:
                await self._outbox.reset_for_retry(
                    job, f"delivery error: {type(exc).__name__}"
                )
            except Exception:
                logger.exception("failed to reset job %s for retry", job.id)

    async def _note_text(self, note_id: int) -> str | None:
        async with self._outbox._sessions() as db:
            note = await db.get(MemberNote, note_id)
            return note.text if note is not None else None
