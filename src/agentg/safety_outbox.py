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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentg.models import Gym, Member, MemberChannel, MemberNote, SafetyOutboxJob

if TYPE_CHECKING:
    from agentg.linking_store import LinkingStore  # pragma: no cover

MAX_NOTE_LENGTH = 400

# Lease: a job claimed longer than this is considered abandoned and is
# reset to pending by the next poll cycle (stale-claim recovery).
LEASE_TIMEOUT_SECONDS = 60

# Backoff: each retry waits retry_count * BASE_BACKOFF seconds before
# the next attempt (linear backoff), capped at MAX_BACKOFF_SECONDS.
# Jobs are never permanently failed solely from retry count (P1 #4).
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300  # 5-minute cap on retry delay
# Bounded send: a notifier.send that hangs longer than this is cancelled
# so a hung channel adapter never blocks gather or strands other jobs.
SEND_TIMEOUT_SECONDS = 15

Clock = Callable[[], datetime]

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 10  # seconds between pending-job sweeps
_STARTUP_BATCH = 50  # max jobs to drain in one batch


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _coaches_for_gym_in_session(
    db: AsyncSession,
    gym_id: int,
    exclude_member_id: int | None = None,
) -> list[tuple[int, str, str, str]]:
    """Return exactly one Coach per Coach in the Gym, as
    ``(member_id, name, channel, channel_user_id)``, queried within an
    already-open session so eligibility is atomic with the Note commit.

    A Coach may have multiple channel identities (MemberChannel rows).
    Exactly one is selected per Coach (deterministic: first by channel
    name, then by channel_user_id) so the outbox invariant — one job per
    Note/Coach — always holds.

    The Gym row is locked (SELECT … FOR UPDATE) so a concurrent
    *promote_to_coach* or *set_coach* serializes with this query — a
    coach-flag change that lands before this lock is visible to the
    eligibility check; one that starts after the lock waits until we
    commit (P1 #3)."""
    # Lock the Gym row to prevent concurrent coach flag changes.
    await db.execute(
        select(Gym).where(Gym.id == gym_id).with_for_update()
    )
    rows = (
        await db.execute(
            select(
                Member.id,
                Member.name,
                MemberChannel.channel,
                MemberChannel.channel_user_id,
            )
            .join(MemberChannel, MemberChannel.member_id == Member.id)
            .where(Member.gym_id == gym_id, Member.is_coach.is_(True))
            .order_by(
                Member.id,
                MemberChannel.channel,
                MemberChannel.channel_user_id,
            )
        )
    ).all()
    # Deduplicate: a Coach may have multiple MemberChannel rows.
    # Pick the first channel per Coach deterministically (ordering above).
    seen: set[int] = set()
    result: list[tuple[int, str, str, str]] = []
    for member_id, name, channel, channel_user_id in rows:
        if member_id == exclude_member_id or member_id in seen:
            continue
        seen.add(member_id)
        result.append((member_id, name, channel, channel_user_id))
    return result


class SafetyOutbox:
    """Create and manage outbox jobs for safety-flag coach notifications."""

    def __init__(self, engine, clock: Clock = _utcnow) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

    async def create_note_and_jobs(
        self,
        member_id: int,
        gym_id: int,
        text: str,
        member_name: str,
        member_is_coach: bool,
        coaches: list[tuple[int, str, str, str]] | None = None,
        *,
        linking_store: "LinkingStore | None" = None,
        exclude_member_id: int | None = None,
    ) -> tuple[MemberNote, list[SafetyOutboxJob]]:
        """Write a safety Note and one outbox job per eligible Coach in one
        transaction — committing neither if the outbox can't be built.

        When ``linking_store`` is provided, eligible Coaches are queried
        **inside** the transaction so the committed Note always has one job
        per Coach that was eligible at commit time (P1 #3).  When
        ``coaches`` is passed directly (test path), that pre-resolved list
        is used.

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

            # Resolve eligible Coaches inside the transaction when a
            # linking_store is provided so eligibility is atomic with the
            # Note commit (P1 #3).
            resolved_coaches: list[tuple[int, str, str, str]]
            if linking_store is not None:
                resolved_coaches = await _coaches_for_gym_in_session(
                    db, gym_id, exclude_member_id=exclude_member_id,
                )
            elif coaches is not None:
                resolved_coaches = coaches
            else:
                resolved_coaches = []

            jobs: list[SafetyOutboxJob] = []
            for coach_id, _coach_name, channel, channel_user_id in resolved_coaches:
                # Validate that the denormalized job Gym matches the Note Gym
                # — the unique constraint on (note_id, coach_member_id) allows
                # only one job per Note/Coach regardless of gym_id, so a
                # mismatched gym_id would be a silent bug.
                assert gym_id == note.gym_id, (
                    f"job gym_id {gym_id} does not match note gym_id {note.gym_id}"
                )
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
        can never claim the same job.  Jobs whose ``next_retry_at`` is still
        in the future are excluded so bounded backoff survives a restart (P1 #4).
        """
        now = self._clock()
        async with self._sessions() as db:
            sub = (
                select(SafetyOutboxJob.id)
                .where(
                    SafetyOutboxJob.status == "pending",
                    or_(
                        SafetyOutboxJob.next_retry_at == None,
                        SafetyOutboxJob.next_retry_at <= now,
                    ),
                )
                .order_by(SafetyOutboxJob.created_at)
                .limit(limit)
            )
            result = await db.execute(
                update(SafetyOutboxJob)
                .where(
                    SafetyOutboxJob.id.in_(sub),
                    SafetyOutboxJob.status == "pending",
                )
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
        tries again.  Jobs are never permanently failed solely from retry
        count — the backoff is bounded at *MAX_BACKOFF_SECONDS* so
        persistently-failing jobs retry at that ceiling forever (P1 #4).

        Only acts when the job is still ``sending`` — a delivery that
        completed between the attempt and this guard is not overwritten.
        ``claimed_at`` is cleared so the next claim can stamp a fresh lease.
        ``last_error`` records the transient reason for diagnostics.
        ``next_retry_at`` gates the next claim so the bounded backoff is
        honoured even across process restarts."""
        next_count = job.retry_count + 1
        delay = min(next_count * BASE_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS)
        next_retry_at = self._clock() + timedelta(seconds=delay)
        async with self._sessions() as db:
            await db.execute(
                update(SafetyOutboxJob)
                .where(
                    SafetyOutboxJob.id == job.id,
                    SafetyOutboxJob.status == "sending",
                )
                .values(
                    status="pending",
                    retry_count=next_count,
                    last_error=reason[:400],
                    claimed_at=None,
                    next_retry_at=next_retry_at,
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
                .where(
                    SafetyOutboxJob.id.in_(stale_ids),
                    SafetyOutboxJob.status == "sending",
                )
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

        The Gym row is locked (SELECT ... FOR UPDATE) before authorization
        so no Gym switch, demotion, or Forget-me can repoint authorization
        between the final check and the actual send.  ``set_coach`` and
        ``link_member`` (gym switch) also lock the Gym row, so they
        serialize with delivery — the authorization is stable through the
        entire send (P1 #2 r5).

        The job and note are re-verified before sending: if the Note was
        deleted by forget-me between claim and delivery the job is failed
        instead of sending retained text (P1 #1).

        Transient failures are always retried with bounded backoff — jobs
        are never permanently failed solely from attempt count (P1 #4).

        Each notifier.send is wrapped in asyncio.wait_for so a hung
        channel adapter never blocks gather or strands other jobs (P2).
        """
        # Backoff is enforced solely by next_retry_at gating in
        # claim_pending — no second sleep here.  reset_for_retry sets
        # next_retry_at to now + bounded delay, and claim_pending skips
        # jobs whose next_retry_at hasn't passed.  A single durable gate
        # avoids doubling the delay and survives restart (P2 r6).
        try:
            # Resolve note text first — if the note is gone (forget-me),
            # fail immediately without sending anything.
            note_text = await self._note_text(job.note_id)
            if note_text is None:
                return await self._outbox.mark_failed(
                    job, "safety note no longer exists"
                )

            # Verify the job still exists (cascade-delete from forget-me
            # would remove it).  If it's gone, nothing more to do.
            if not await self._job_still_sending(job.id):
                return

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
                        job.coach_member_id,
                    )

            # Send heads-up with Gym row locking — the lock serializes
            # with set_coach and link_member so authorization is stable
            # through the entire send (P1 #2 r5).
            headsup_failed: str | None = None
            async with self._semaphore:
                # Pre-verify the job is still sending before authorizing.
                if not await self._job_still_sending(job.id):
                    headsup_failed = "job_gone"
                else:
                    headsup_failed = await self._authorized_send(
                        job, text, disable_preview=True, protect_content=False,
                    )

            if headsup_failed == "coach no longer reachable in this gym":
                return await self._outbox.mark_failed(
                    job, "coach no longer reachable in this gym"
                )
            elif headsup_failed == "safety note no longer exists":
                return await self._outbox.mark_failed(
                    job, "safety note no longer exists"
                )
            elif headsup_failed == "job_gone":
                return
            elif headsup_failed in ("notifier send failed", "notifier send timed out"):
                return await self._outbox.reset_for_retry(
                    job, headsup_failed
                )

            if link is not None:
                # Re-authorize with Gym lock for the link send too — the
                # lock serializes with set_coach so a demotion cannot land
                # between authorization and link send (P1 #2 r5).
                link_failed: str | None = None
                async with self._semaphore:
                    if not await self._job_still_sending(job.id):
                        link_failed = "job_gone"
                    else:
                        link_failed = await self._authorized_send(
                            job, link, disable_preview=True, protect_content=True,
                        )
                        # Map the heads-up error codes to link variants.
                        if link_failed == "coach no longer reachable in this gym":
                            link_failed = "unauthorized"
                        elif link_failed in (
                            "notifier send failed", "notifier send timed out"
                        ):
                            link_failed = "notifier"

                if link_failed == "unauthorized":
                    # Heads-up already landed; link can't be delivered.
                    # Mark delivered anyway — the safety text arrived.
                    return await self._outbox.mark_delivered(job)
                # link_failed == "job_gone" or "safety note no longer
                # exists": the note/job was deleted (forget-me) between
                # heads-up and link; bail without marking anything —
                # forget-me already cleaned up.  (P1 #1 r6)
                if link_failed in ("job_gone", "safety note no longer exists"):
                    return
                # link_failed == "notifier": the heads-up already landed;
                # a missing link is unfortunate but not worth marking the
                # whole job failed.

            await self._outbox.mark_delivered(job)
        except Exception as exc:
            logger.exception("error delivering outbox job %s", job.id)
            try:
                await self._outbox.reset_for_retry(
                    job, f"delivery error: {type(exc).__name__}"
                )
            except Exception:
                logger.exception("failed to reset job %s for retry", job.id)

    async def _authorized_send(
        self, job: SafetyOutboxJob, text: str, *,
        disable_preview: bool = True, protect_content: bool = False,
    ) -> str | None:
        """Lock the Gym and MemberChannel rows, check authorization, and send.

        The Gym row lock serializes with ``set_coach`` (which also locks
        the Gym row via ``SELECT ... FOR UPDATE``).  The MemberChannel row
        lock serializes with ``link_member`` (gym switch), which repoints
        the same MemberChannel row.  Together they ensure authorization is
        stable through the entire send — no demotion or gym-switch can
        interleave between the check and the notifier call (P1 #2 r5).

        Returns ``None`` on success, or an error code string on failure.
        """
        from sqlalchemy import select as sa_select

        conn = await self._outbox._engine.connect()
        try:
            await conn.begin()
            # Lock the Gym row — serializes with set_coach's
            # ``SELECT ... FOR UPDATE`` on the same row.
            result = await conn.execute(
                sa_select(Gym).where(Gym.id == job.gym_id).with_for_update()
            )
            if result.first() is None:
                await conn.rollback()
                return "coach no longer reachable in this gym"

            # Lock the MemberChannel row — serializes with link_member's
            # repoint of the channel identity (gym switch).
            await conn.execute(
                sa_select(MemberChannel)
                .where(
                    MemberChannel.member_id == job.coach_member_id,
                    MemberChannel.gym_id == job.gym_id,
                )
                .with_for_update()
            )

            # Lock and re-check the job and note while holding the Gym
            # lock — serializes with ForgetStore which also locks the Gym
            # row before deleting.  Together they guarantee no notification
            # can send after the note/job are deleted (P1 #1 r6).
            job_row = (
                await conn.execute(
                    sa_select(SafetyOutboxJob)
                    .where(
                        SafetyOutboxJob.id == job.id,
                        SafetyOutboxJob.status == "sending",
                    )
                    .with_for_update()
                )
            ).first()
            if job_row is None:
                await conn.rollback()
                return "job_gone"

            note_row = (
                await conn.execute(
                    sa_select(MemberNote)
                    .where(MemberNote.id == job.note_id)
                    .with_for_update()
                )
            ).first()
            if note_row is None:
                await conn.rollback()
                return "safety note no longer exists"

            # Check authorization on the locked connection: the Member
            # must still be flagged Coach with a channel pointing at the
            # correct Gym.  Order deterministically so when a Coach has
            # multiple channels the same one is always picked.
            result = await conn.execute(
                sa_select(
                    MemberChannel.channel, MemberChannel.channel_user_id,
                )
                .join(Member, Member.id == MemberChannel.member_id)
                .where(
                    MemberChannel.member_id == job.coach_member_id,
                    MemberChannel.gym_id == job.gym_id,
                    Member.is_coach.is_(True),
                )
                .order_by(
                    MemberChannel.channel,
                    MemberChannel.channel_user_id,
                )
            )
            row = result.first()
            if row is None:
                await conn.rollback()
                return "coach no longer reachable in this gym"

            channel, channel_user_id = row.channel, row.channel_user_id

            # Send while holding both locks — they prevent concurrent
            # set_coach / link_member from repointing authorization
            # (P1 #2 r5).
            try:
                await asyncio.wait_for(
                    self._notifier.send(
                        channel, channel_user_id, text,
                        disable_preview=disable_preview,
                        protect_content=protect_content,
                    ),
                    timeout=SEND_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "send timeout for coach %s", channel_user_id,
                )
                await conn.rollback()
                return "notifier send timed out"
            except Exception:
                logger.exception(
                    "failed to send to coach %s", channel_user_id,
                )
                await conn.rollback()
                return "notifier send failed"

            await conn.commit()
            return None
        finally:
            if not conn.closed:
                await conn.close()

    async def _note_text(self, note_id: int) -> str | None:
        async with self._outbox._sessions() as db:
            note = await db.get(MemberNote, note_id)
            return note.text if note is not None else None

    async def _job_still_sending(self, job_id: int) -> bool:
        """True when the outbox job still exists AND is in ``sending`` status.

        A job that was cascade-deleted (via forget-me deleting its Note)
        returns False here, so delivery can bail out without sending.
        A job whose status changed to ``failed`` (e.g. forget-me marking it
        before cascade) also returns False — defense in depth for P1 #1.
        """
        async with self._outbox._sessions() as db:
            row = await db.get(SafetyOutboxJob, job_id)
            return row is not None and row.status == "sending"
