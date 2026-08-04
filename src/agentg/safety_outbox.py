"""Durable outbox for safety-flag coach notifications (issues #216, #217).

Safety Notes commit with one outbox job per eligible Coach in the same
transaction.  A background worker sends pending jobs without delaying the
Member's reply, recovers on startup, mints authenticated dashboard links at
delivery time, and falls back to text-only when minting fails.

Delivery guarantee: **at-least-once with bounded duplicate risk — not
exactly-once.**  A Coach can see the same heads-up twice.  The windows are
named, not hand-waved:

* A job is claimed (``pending`` → ``sending``) *before* the network send and
  only marked ``delivered`` *after* it returns.  A crash, a lease expiry
  (``reset_stale_claims``), or a restart (``reset_claimed``) in between
  re-queues a job whose message may already have landed.
* A send that times out (``SEND_TIMEOUT_SECONDS``) is retried even though the
  provider may have delivered it.
* The ``claimed_at`` lease stamp fences a *stale* owner out of
  ``mark_delivered``/``mark_failed``/``reset_for_retry``, so a re-claimed job
  has exactly one owner that can record an outcome — but it cannot un-send a
  message the stale owner already put on the wire.

What *is* bounded:

* One job per (Note, Coach) forever — ``uq_outbox_job_note_coach``.
* At most ``MAX_DELIVERY_ATTEMPTS`` attempts per job, after which the
  terminal policy retires it as ``failed`` with
  ``failure_kind="retry_exhausted"``.  An abandoned claim (crash, expired
  lease) consumes an attempt too, so a crash loop converges on the policy
  instead of re-claiming the same job forever.
* At most **one live dashboard credential per job**: every mint first revokes
  the token the previous attempt left outstanding (``login_token_hash``), so
  a crash-loop cannot accumulate valid magic links for their 10-minute TTL.

See ``docs/spec-dashboard.md`` §Safety flags for the product-level wording.

Global lock order across all paths that acquire multiple row locks:
  Gym → Member → MemberChannel → SafetyOutboxJob → MemberNote

The row-lock serialization below relies on ``SELECT … FOR UPDATE``, which
is honoured on PostgreSQL (the production default, spec §Hosting) but is a
no-op on SQLite — there, a forget-me commit can interleave between the
delivery re-checks and the network send.  SQLite deployments (tests, dev)
instead rely on the status/lease re-checks, which close all but the final
check-to-send window; the full guarantee is PostgreSQL-only.  Taking
SQLite's real write lock (the noop-UPDATE idiom used elsewhere) is not an
option here: it would hold the global write lock across a network send.

Every path that needs two or more of these rows must acquire them in this
order.  The delivery path (_authorized_send) locks Member then
MemberChannel then SafetyOutboxJob then MemberNote.  The gym-switch path
(_link_member_in_session) locks the old Member first then MemberChannel.
set_coach locks Gym then Member.  ForgetStore locks Member then
MemberChannel.  All consistent — no circular wait.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentg.dashboard_store import hash_token
from agentg.models import Gym, Member, MemberChannel, MemberNote, SafetyOutboxJob

if TYPE_CHECKING:
    from agentg.linking_store import LinkingStore  # pragma: no cover

MAX_NOTE_LENGTH = 400

# Lease: a job claimed longer than this is considered abandoned and is
# reset to pending by the next poll cycle (stale-claim recovery).
LEASE_TIMEOUT_SECONDS = 60

# Backoff (issue #217): attempt *n* waits BASE_BACKOFF * 2**(n-1) seconds,
# capped at MAX_BACKOFF_SECONDS, then spread by ±BACKOFF_JITTER_RATIO so a
# provider outage that failed a whole batch at once does not retry the whole
# batch at once either (thundering herd).
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300  # 5-minute cap on retry delay
BACKOFF_JITTER_RATIO = 0.25  # ±25% around the exponential delay
MIN_BACKOFF_SECONDS = 1  # jitter never schedules a retry sooner than this

# Terminal policy (issue #217): a job that has failed this many times stops
# retrying and is retired as ``failed`` with failure_kind RETRY_EXHAUSTED.
# Retrying forever turns one dead channel into an unbounded background load
# and hides the failure from operators; with the exponential schedule above
# the eight attempts span ~10 minutes before the job is surfaced instead.
MAX_DELIVERY_ATTEMPTS = 8

# Bounded send: a notifier.send that hangs longer than this is cancelled
# so a hung channel adapter never blocks gather or strands other jobs.
SEND_TIMEOUT_SECONDS = 15

Clock = Callable[[], datetime]
Random = Callable[[], float]  # returns a float in [0, 1)

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 10  # seconds between pending-job sweeps
_STARTUP_BATCH = 50  # max jobs to drain in one batch


class FailureKind:
    """Machine-readable classes of a terminal outbox failure (issue #217).

    Stored on ``SafetyOutboxJob.failure_kind`` so failed jobs stay queryable
    by *why* they died rather than by prose that may be reworded later.
    """

    #: The terminal policy retired the job after MAX_DELIVERY_ATTEMPTS.
    RETRY_EXHAUSTED = "retry_exhausted"
    #: The Coach is no longer reachable in this Gym (demoted, gym switch).
    UNAUTHORIZED = "unauthorized"
    #: The safety Note is gone (forget-me) — nothing left to deliver.
    NOTE_DELETED = "note_deleted"
    #: Retired by a caller that did not classify the cause.
    UNSPECIFIED = "unspecified"


TERMINAL_FAILURE_KINDS = frozenset(
    {
        FailureKind.RETRY_EXHAUSTED,
        FailureKind.UNAUTHORIZED,
        FailureKind.NOTE_DELETED,
        FailureKind.UNSPECIFIED,
    }
)

# Sanitisation (issue #217): failure strings and telemetry are operator-facing
# and land in logs, so credentials must never survive into them.  These are a
# backstop — the delivery path only ever records fixed error codes and
# exception *type* names, never a Member's Note text or an exception message.
MAX_ERROR_LENGTH = 200
_REDACTED = "[redacted]"
#
# Order matters, and the whole-credential rules must come FIRST: a header rule
# that fires early can eat the prefix a later rule needs and leave the actual
# secret behind.  Concretely, redacting the ``token 123456789`` half of
# ``token 123456789:AAH…`` destroys the ``\d{6,}:`` anchor of the Telegram
# rule and publishes the secret half verbatim (PR #228 review, P2).
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Our own magic links: https://dash/login/<raw token>.
    (re.compile(r"(?i)(/login/)[A-Za-z0-9._\-~+/=]+"), r"\1" + _REDACTED),
    # Telegram bot tokens: "123456789:AAH...".  No leading \b: the canonical
    # provider rendering is "/bot123456789:AAH…", where "t" and "1" are both
    # word characters so \b can never anchor.  (?<!\d) keeps the whole numeric
    # half inside the match instead of letting it start mid-number.
    (re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_\-]{20,}"), _REDACTED),
    # Provider API keys with a conventional prefix.
    (re.compile(r"(?i)\b(sk|pk|rk|api)[-_][A-Za-z0-9]{16,}\b"), _REDACTED),
    # key=value / "key": "value" secrets in URLs, JSON, and repr() output.
    # The optional quote before the separator is what makes the JSON and
    # repr() forms ('"token": "…"', "{'token': '…'}") match at all.
    (
        re.compile(
            r"(?i)\b(api[-_]?key|access[-_]?token|refresh[-_]?token|auth[-_]?token"
            r"|token|secret|password|passwd|pwd|signature|credential)\b"
            r"[\"']?\s*[=:]\s*[\"']?[^\s\"'&,;}]+"
        ),
        r"\1=" + _REDACTED,
    ),
    # Authorization headers: "Bearer eyJ...", "Basic dXNlcjpwdw==".
    (
        re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._\-~+/=]{8,}"),
        r"\1 " + _REDACTED,
    ),
    # Backstop: anything else long and high-entropy enough to be a credential.
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), _REDACTED),
)


def sanitize_error(reason: str | None, *, limit: int = MAX_ERROR_LENGTH) -> str:
    """Return *reason* with credential-shaped substrings redacted, collapsed
    to one line and truncated to *limit* characters.

    Applied to everything the outbox durably records (``last_error``,
    ``failure_reason``) and to every structured telemetry payload, so a
    bearer token, a provider API key, or one of our own ``/login/<token>``
    magic links can never reach the database or the log stream.

    It does **not** try to detect a Member's private Note text — that is
    guaranteed by construction instead: the delivery path records only fixed
    error codes and exception type names, never message bodies.
    """
    if not reason:
        return ""
    cleaned = " ".join(str(reason).split())
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned[:limit]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def delivery_telemetry(
    job: SafetyOutboxJob,
    *,
    outcome: str,
    error: str | None,
    attempt: int,
    failure_kind: str | None = None,
    next_retry_at: datetime | None = None,
) -> dict[str, object]:
    """Build the structured, sanitized payload for a failed delivery.

    Carries the identifiers an operator needs to find the job (ids, channel
    name, attempt count, next attempt time, failure class) and deliberately
    omits everything private or secret: the Note text, the heads-up message,
    the magic link, the raw dashboard token, and the Coach's
    ``channel_user_id`` (the provider-side account identifier).
    """
    return {
        "event": "safety_outbox.delivery_failed",
        "outcome": outcome,  # "retry" | "terminal"
        "terminal": outcome == "terminal",
        "job_id": job.id,
        "gym_id": job.gym_id,
        "note_id": job.note_id,
        "coach_member_id": job.coach_member_id,
        "channel": job.channel,
        "attempt": attempt,
        "max_attempts": MAX_DELIVERY_ATTEMPTS,
        "failure_kind": failure_kind,
        "error": sanitize_error(error),
        "next_retry_at": _iso(next_retry_at),
    }


def _emit_telemetry(payload: dict[str, object]) -> None:
    """Log one delivery-failure payload as both a readable line and a
    machine-parseable record (``record.outbox``)."""
    logger.warning(
        "safety outbox delivery failed: %s",
        json.dumps(payload, sort_keys=True, default=str),
        extra={"outbox": payload},
    )


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

    def __init__(
        self, engine, clock: Clock = _utcnow, rng: Random = random.random
    ) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock
        # Injectable jitter source so backoff is deterministic in tests
        # (rng() == 0.5 yields exactly the unjittered exponential delay).
        self._rng = rng

    def backoff_delay(self, attempt: int) -> float:
        """Seconds to wait before *attempt* (1-based): an exponential
        ``BASE_BACKOFF_SECONDS * 2**(attempt-1)`` capped at
        ``MAX_BACKOFF_SECONDS``, then spread by ±``BACKOFF_JITTER_RATIO``.

        The jittered result is clamped into
        ``[MIN_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS]``, so the cap is a hard
        ceiling that jitter cannot push past and a retry is never scheduled
        for the same instant it failed.
        """
        exponent = max(attempt - 1, 0)
        # Cap the exponent too: 2**attempt overflows into absurd floats long
        # before a real job gets there, and min() would hide it.
        exponent = min(exponent, 32)
        base = min(BASE_BACKOFF_SECONDS * (2**exponent), MAX_BACKOFF_SECONDS)
        spread = base * BACKOFF_JITTER_RATIO * (2 * self._rng() - 1)
        return max(MIN_BACKOFF_SECONDS, min(base + spread, MAX_BACKOFF_SECONDS))

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
        ``sending`` state under OUR claim (``claimed_at`` lease stamp).
        A stale owner whose claim was reset and re-claimed cannot clobber
        the new owner's live claim."""
        async with self._sessions() as db:
            await db.execute(
                update(SafetyOutboxJob)
                .where(
                    SafetyOutboxJob.id == job.id,
                    SafetyOutboxJob.status == "sending",
                    SafetyOutboxJob.claimed_at == job.claimed_at,
                )
                .values(status="delivered", delivered_at=self._clock(), last_error=None)
            )
            await db.commit()

    async def mark_failed(
        self,
        job: SafetyOutboxJob,
        reason: str,
        *,
        kind: str = FailureKind.UNSPECIFIED,
        retry_count: int | None = None,
    ) -> None:
        """Mark a single job as terminally failed — only if still ``sending``.

        ``delivered_at`` stays ``None`` — no delivery occurred.
        ``failed_at`` records the failure timestamp for audit and
        ``failure_kind`` records *which* terminal policy retired it, so
        ``failed_jobs`` can query by cause (issue #217).
        Guarded by the ``claimed_at`` lease stamp like ``mark_delivered``.

        The reason is sanitized before it is stored: it lands in operator
        logs and dashboards, so it must never carry a credential."""
        safe = sanitize_error(reason)
        attempt = retry_count if retry_count is not None else job.retry_count
        values: dict[str, object] = {
            "status": "failed",
            "failure_reason": safe,
            "failure_kind": kind,
            "last_error": safe,
            "failed_at": self._clock(),
        }
        if retry_count is not None:
            values["retry_count"] = retry_count
        async with self._sessions() as db:
            result = await db.execute(
                update(SafetyOutboxJob)
                .where(
                    SafetyOutboxJob.id == job.id,
                    SafetyOutboxJob.status == "sending",
                    SafetyOutboxJob.claimed_at == job.claimed_at,
                )
                .values(**values)
            )
            await db.commit()
        if result.rowcount:  # type: ignore[attr-defined]
            _emit_telemetry(
                delivery_telemetry(
                    job,
                    outcome="terminal",
                    error=safe,
                    attempt=attempt,
                    failure_kind=kind,
                )
            )

    async def reset_for_retry(self, job: SafetyOutboxJob, reason: str) -> bool:
        """Schedule the next attempt, or retire the job when the terminal
        policy is reached.  Returns ``True`` when the job was rescheduled and
        ``False`` when it was retired as ``failed``.

        Attempts are counted durably in ``retry_count``.  Once it reaches
        ``MAX_DELIVERY_ATTEMPTS`` the job stops retrying and is marked
        ``failed`` with ``failure_kind="retry_exhausted"`` (issue #217) — the
        named terminal policy that replaces #216's retry-forever behaviour,
        which turned one dead channel into unbounded background load and left
        the failure invisible.

        Otherwise the job returns to ``pending`` with ``next_retry_at`` set
        ``backoff_delay(attempt)`` seconds ahead — exponential, jittered, and
        capped at ``MAX_BACKOFF_SECONDS``.

        Only acts when the job is still ``sending`` under OUR claim
        (``claimed_at`` lease stamp) — a delivery that completed between
        the attempt and this guard, or a claim reset-and-re-claimed by
        another owner, is not overwritten.
        ``claimed_at`` is cleared so the next claim can stamp a fresh lease.
        ``last_error`` records the sanitized transient reason for diagnostics.
        ``next_retry_at`` gates the next claim so the bounded backoff is
        honoured even across process restarts."""
        next_count = job.retry_count + 1
        safe = sanitize_error(reason)
        if next_count >= MAX_DELIVERY_ATTEMPTS:
            await self.mark_failed(
                job,
                f"{safe} (terminal after {next_count} attempts)",
                kind=FailureKind.RETRY_EXHAUSTED,
                retry_count=next_count,
            )
            return False
        next_retry_at = self._clock() + timedelta(
            seconds=self.backoff_delay(next_count)
        )
        async with self._sessions() as db:
            result = await db.execute(
                update(SafetyOutboxJob)
                .where(
                    SafetyOutboxJob.id == job.id,
                    SafetyOutboxJob.status == "sending",
                    SafetyOutboxJob.claimed_at == job.claimed_at,
                )
                .values(
                    status="pending",
                    retry_count=next_count,
                    last_error=safe,
                    claimed_at=None,
                    next_retry_at=next_retry_at,
                )
            )
            await db.commit()
        if result.rowcount:  # type: ignore[attr-defined]
            _emit_telemetry(
                delivery_telemetry(
                    job,
                    outcome="retry",
                    error=safe,
                    attempt=next_count,
                    next_retry_at=next_retry_at,
                )
            )
        return True

    async def failed_jobs(
        self,
        *,
        gym_id: int | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[SafetyOutboxJob]:
        """Terminally-failed jobs, newest failure first — the queryable
        surface behind the terminal policy (issue #217).

        Optionally narrowed to one Gym and/or one ``FailureKind`` so an
        operator can ask "which safety pings died, and why" without reading
        prose out of ``failure_reason``.
        """
        stmt = select(SafetyOutboxJob).where(SafetyOutboxJob.status == "failed")
        if gym_id is not None:
            stmt = stmt.where(SafetyOutboxJob.gym_id == gym_id)
        if kind is not None:
            stmt = stmt.where(SafetyOutboxJob.failure_kind == kind)
        stmt = stmt.order_by(SafetyOutboxJob.failed_at.desc()).limit(limit)
        async with self._sessions() as db:
            return list((await db.scalars(stmt)).all())

    async def record_login_token(
        self, job: SafetyOutboxJob, token_hash: str
    ) -> bool:
        """Remember the dashboard credential this job currently has
        outstanding, so the next attempt can revoke it before minting
        another (issue #217).  ``True`` when the write landed.

        Fenced by the ``claimed_at`` lease stamp like every other write:
        without it a stale owner could overwrite the live owner's hash and
        make the live, already-sent token unrevokable for its full TTL
        (PR #228 review, P2).  A caller whose write does *not* land still
        holds the raw token and must revoke it — see ``_mint_link``.
        """
        async with self._sessions() as db:
            result = await db.execute(
                update(SafetyOutboxJob)
                .where(
                    SafetyOutboxJob.id == job.id,
                    SafetyOutboxJob.claimed_at == job.claimed_at,
                )
                .values(login_token_hash=token_hash)
            )
            await db.commit()
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def take_login_token_hash(self, job: SafetyOutboxJob) -> str | None:
        """Read and clear this job's outstanding dashboard-credential hash,
        under OUR claim.  ``None`` when the job holds none, or when the lease
        has moved on.

        The lease fence matters: after a lease expiry and re-claim, an
        unfenced take would let the stale owner revoke the *new* owner's
        token — one already sent to the Coach — killing a live magic link on
        a job that goes on to be marked delivered (PR #228 review, P2).

        Fenced on ``claimed_at`` only, not on ``status``: the terminal path
        revokes *after* ``mark_failed`` has already moved the row to
        ``failed``, and that revoke must still land.

        The read and the clear are two statements; a lost race can only
        revoke the same hash twice, which the revoke itself makes idempotent.
        """
        async with self._sessions() as db:
            current = await db.scalar(
                select(SafetyOutboxJob.login_token_hash).where(
                    SafetyOutboxJob.id == job.id,
                    SafetyOutboxJob.claimed_at == job.claimed_at,
                )
            )
            if current is None:
                return None
            await db.execute(
                update(SafetyOutboxJob)
                .where(
                    SafetyOutboxJob.id == job.id,
                    SafetyOutboxJob.claimed_at == job.claimed_at,
                    SafetyOutboxJob.login_token_hash == current,
                )
                .values(login_token_hash=None)
            )
            await db.commit()
            return current

    async def _recover_claims(
        self, rows: list[SafetyOutboxJob], reason: str
    ) -> int:
        """Return abandoned claims to ``pending``, **consuming one attempt
        each**, and retire the ones that reach the terminal policy.

        The attempt has to be counted here.  ``retry_count`` used to be
        incremented only in ``reset_for_retry``, which requires the owner to
        live long enough to record an outcome — so a worker that crashed (or
        whose lease expired) after the send came back to ``pending`` with the
        counter untouched and was re-claimed immediately.  A crash-looping
        process could then attempt one job forever, which is precisely the
        unbounded background load ``MAX_DELIVERY_ATTEMPTS`` exists to stop
        (PR #228 review, P2).

        No backoff is applied: an abandoned claim is not a provider
        rejection, and delaying a safety ping because the process restarted
        would be the wrong trade.  The attempt ceiling is what bounds the
        loop.

        A job retired here may still hold one outstanding dashboard
        credential; the store has no dashboard to revoke it with, so that one
        token expires on its own 10-minute TTL.  Still bounded at one per
        job, which is the guarantee.
        """
        if not rows:
            return 0
        now = self._clock()
        safe = sanitize_error(reason)
        retire = [r for r in rows if r.retry_count + 1 >= MAX_DELIVERY_ATTEMPTS]
        requeue_ids = [
            r.id for r in rows if r.retry_count + 1 < MAX_DELIVERY_ATTEMPTS
        ]
        async with self._sessions() as db:
            if requeue_ids:
                await db.execute(
                    update(SafetyOutboxJob)
                    .where(
                        SafetyOutboxJob.id.in_(requeue_ids),
                        SafetyOutboxJob.status == "sending",
                    )
                    .values(
                        status="pending",
                        claimed_at=None,
                        retry_count=SafetyOutboxJob.retry_count + 1,
                        last_error=safe,
                    )
                )
            if retire:
                await db.execute(
                    update(SafetyOutboxJob)
                    .where(
                        SafetyOutboxJob.id.in_([r.id for r in retire]),
                        SafetyOutboxJob.status == "sending",
                    )
                    .values(
                        status="failed",
                        claimed_at=None,
                        retry_count=SafetyOutboxJob.retry_count + 1,
                        last_error=safe,
                        failure_reason=(
                            f"{safe} (terminal after {MAX_DELIVERY_ATTEMPTS} attempts)"
                        ),
                        failure_kind=FailureKind.RETRY_EXHAUSTED,
                        failed_at=now,
                    )
                )
            await db.commit()
        for row in retire:
            _emit_telemetry(
                delivery_telemetry(
                    row,
                    outcome="terminal",
                    error=safe,
                    attempt=row.retry_count + 1,
                    failure_kind=FailureKind.RETRY_EXHAUSTED,
                )
            )
        return len(rows)

    async def reset_claimed(self) -> int:
        """Reset every ``sending`` job back to ``pending``.

        Called on startup so jobs orphaned by a prior crash (claimed but
        never delivered) are retried.  The abandoned attempt is counted, so
        a crash loop still converges on the terminal policy rather than
        retrying forever — see ``_recover_claims``.
        """
        async with self._sessions() as db:
            rows = list(
                (
                    await db.scalars(
                        select(SafetyOutboxJob).where(
                            SafetyOutboxJob.status == "sending"
                        )
                    )
                ).all()
            )
        return await self._recover_claims(rows, "claim abandoned (restart)")

    async def reset_stale_claims(
        self, max_age_seconds: int = LEASE_TIMEOUT_SECONDS
    ) -> int:
        """Reset every ``sending`` job whose lease has expired back to
        ``pending`` so a future poll cycle retries them.

        Called periodically by the poll loop so a hang (not a crash) that
        outlasts the lease timeout does not permanently strand jobs.  Like
        ``reset_claimed`` this counts the abandoned attempt, so a job that
        hangs every time still hits the terminal policy.
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
            stale = [
                r for r in rows
                if r.claimed_at is not None
                and (cutoff - r.claimed_at).total_seconds() > max_age_seconds
            ]
        return await self._recover_claims(stale, "claim lease expired")


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
            except Exception as exc:
                # Class only — a SQLAlchemy error renders the statement and
                # its bound parameters, which on this path can include a
                # Member's Note text (issue #217 sanitization).
                logger.error("outbox poll cycle failed: %s", type(exc).__name__)
            try:
                await asyncio.sleep(_POLL_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def _deliver_one(self, job: SafetyOutboxJob) -> None:
        """Send one outbox job: heads-up + (optionally) magic link.

        Narrow locks (Coach Member, MemberChannel, Note, job) are acquired
        before authorization so no demotion, gym-switch, or Forget-me can
        repoint authorization between the final check and the actual send.
        ``set_coach``, ``link_member`` (gym switch), and ``ForgetStore``
        acquire the same narrow locks, so they serialize with delivery —
        the authorization is stable through the entire send (P1 #2 r5,
        P1 r8).

        The job and note are re-verified before sending: if the Note was
        deleted by forget-me between claim and delivery the job is failed
        instead of sending retained text (P1 #1).

        Transient failures are retried with bounded exponential, jittered
        backoff until ``MAX_DELIVERY_ATTEMPTS``, at which point the terminal
        policy retires the job as ``failed`` (issue #217).

        The dashboard credential is minted only **after** the heads-up send
        has re-checked authorization, and every mint first revokes the token
        the previous attempt left outstanding — so retries cannot accumulate
        live magic links (issue #217, deferred P3 from PR #224).

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
                return await self._fail(
                    job, "safety note no longer exists", FailureKind.NOTE_DELETED,
                )

            # Verify the job still exists (cascade-delete from forget-me
            # would remove it).  If it's gone, nothing more to do.
            if not await self._job_still_sending(job):
                return

            text = f"Heads-up from your member {job.member_name}: {note_text}"

            # Send heads-up with narrow row locking — the lock serializes
            # with set_coach and link_member so authorization is stable
            # through the entire send (P1 #2 r5, P1 r8).
            headsup_failed: str | None = None
            async with self._semaphore:
                # Pre-verify the job is still sending before authorizing.
                if not await self._job_still_sending(job):
                    headsup_failed = "job_gone"
                else:
                    headsup_failed = await self._authorized_send(
                        job, text, disable_preview=True, protect_content=False,
                    )

            if headsup_failed == "coach no longer reachable in this gym":
                return await self._fail(
                    job,
                    "coach no longer reachable in this gym",
                    FailureKind.UNAUTHORIZED,
                )
            elif headsup_failed == "safety note no longer exists":
                return await self._fail(
                    job, "safety note no longer exists", FailureKind.NOTE_DELETED,
                )
            elif headsup_failed == "job_gone":
                return
            elif headsup_failed in ("notifier send failed", "notifier send timed out"):
                await self._retry(job, headsup_failed)
                return

            # Mint the dashboard link only now: _authorized_send has just
            # re-verified that this Coach is still a Coach of this Gym, so a
            # job that can never be authorized never mints a credential, and
            # a retry loop mints at most one live token at a time (#217).
            link = await self._mint_link(job)

            if link is not None:
                # Re-authorize with narrow locks for the link send too —
                # the locks serialize with set_coach so a demotion cannot
                # land between authorization and link send (P1 #2 r5).
                link_failed: str | None = None
                async with self._semaphore:
                    if not await self._job_still_sending(job):
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
                    # The credential we minted a moment ago was never sent
                    # and the Coach is no longer authorized — revoke it
                    # rather than leave it live for its TTL (#217).
                    await self._revoke_outstanding_token(job)
                    # Mark delivered anyway — the safety text arrived.
                    return await self._outbox.mark_delivered(job)
                # link_failed == "job_gone" or "safety note no longer
                # exists": the note/job was deleted (forget-me) between
                # heads-up and link; bail without marking anything —
                # forget-me already cleaned up.  (P1 #1 r6)
                if link_failed in ("job_gone", "safety note no longer exists"):
                    await self._revoke_outstanding_token(job)
                    return
                # link_failed == "notifier": the heads-up already landed;
                # a missing link is unfortunate but not worth marking the
                # whole job failed.  The token is left alone deliberately:
                # a timeout may still have delivered the link, and revoking
                # a link the Coach can see would be worse than its TTL.

            await self._outbox.mark_delivered(job)
        except Exception as exc:
            # Log the exception *class*, never its message: an adapter's
            # error text can embed the request body (the Note) or the URL
            # (the magic link).  The traceback is dropped for the same
            # reason — the structured telemetry carries the diagnostics.
            logger.error(
                "error delivering outbox job %s: %s", job.id, type(exc).__name__,
            )
            try:
                await self._retry(job, f"delivery error: {type(exc).__name__}")
            except Exception as inner:
                logger.error(
                    "failed to reset job %s for retry: %s",
                    job.id,
                    type(inner).__name__,
                )

    # ── outcome helpers ────────────────────────────────────────────

    async def _fail(self, job: SafetyOutboxJob, reason: str, kind: str) -> None:
        """Terminally fail a job, revoking any credential it still holds."""
        await self._revoke_outstanding_token(job)
        await self._outbox.mark_failed(job, reason, kind=kind)

    async def _retry(self, job: SafetyOutboxJob, reason: str) -> None:
        """Schedule the next attempt; revoke the outstanding credential when
        the terminal policy retires the job instead (issue #217)."""
        rescheduled = await self._outbox.reset_for_retry(job, reason)
        if not rescheduled:
            await self._revoke_outstanding_token(job)

    # ── dashboard credentials ─────────────────────────────────────

    async def _revoke_outstanding_token(self, job: SafetyOutboxJob) -> bool:
        """Retire the dashboard credential this job still has outstanding.

        Returns True when a live token was revoked.  Idempotent, and never
        raises: a revoke failure must not turn a recoverable delivery into a
        crash, it just leaves the token to expire on its own TTL.
        """
        try:
            token_hash = await self._outbox.take_login_token_hash(job)
            if token_hash is None:
                return False
            return bool(await self._dashboard.revoke_login_token(token_hash))
        except Exception as exc:
            logger.error(
                "failed to revoke dashboard credential for job %s: %s",
                job.id,
                type(exc).__name__,
            )
            return False

    async def _mint_link(self, job: SafetyOutboxJob) -> str | None:
        """Mint the Coach's authenticated deep link for this attempt.

        Revokes the credential a previous attempt left outstanding first, so
        one Note/Coach pair never has more than one live dashboard token
        however many times delivery is retried (issue #217).  Returns None
        when no dashboard is configured or minting fails — the heads-up is
        text-only then, which is the pre-existing fallback.
        """
        if self._base_url is None:
            return None
        token: str | None = None
        try:
            await self._revoke_outstanding_token(job)
            next_path = "/" if job.member_is_coach else f"/members/{job.member_id}"
            token = await self._dashboard.create_login_token(
                job.coach_member_id, job.gym_id, next_path=next_path,
            )
            # The mint and the record are two transactions, so the record can
            # fail (or lose the lease) after a live credential already exists.
            # An unrecorded token could never be revoked by any later path, so
            # undo it here rather than leave it live for its TTL (PR #228
            # review, P3).
            if not await self._outbox.record_login_token(job, hash_token(token)):
                await self._dashboard.revoke_login_token(hash_token(token))
                return None
            return f"{self._base_url}/login/{token}"
        except Exception as exc:
            logger.error(
                "failed to mint dashboard link for job %s: %s",
                job.id,
                type(exc).__name__,
            )
            if token is not None:
                # Same reasoning: a minted-but-unrecorded token is orphaned.
                try:
                    await self._dashboard.revoke_login_token(hash_token(token))
                except Exception:
                    logger.error("failed to revoke orphaned token for job %s", job.id)
            return None

    async def _authorized_send(
        self, job: SafetyOutboxJob, text: str, *,
        disable_preview: bool = True, protect_content: bool = False,
    ) -> str | None:
        """Lock the Coach Member, MemberChannel, Note, and job rows, check
        authorization, and send.

        Narrow, targeted locks replace the broad Gym row lock so network
        delivery cannot block a concurrent ``flag_to_coach_action`` which
        locks the Gym row briefly during eligibility resolution (P1 r8).

        * Coach Member row lock serializes with ``set_coach`` (demotion).
        * MemberChannel row lock serializes with ``link_member`` (gym switch).
        * Note + job locks serialize with ``ForgetStore`` (forget-me).

        Together they ensure authorization is stable through the entire send
        — no demotion, gym-switch, or forget-me can interleave between the
        check and the notifier call.

        PostgreSQL-only guarantee: SQLite ignores ``FOR UPDATE`` (see the
        module docstring), so on SQLite these are plain reads and the
        interleave window between the re-checks and the send remains.

        Returns ``None`` on success, or an error code string on failure.
        """
        from sqlalchemy import select as sa_select

        conn = await self._outbox._engine.connect()
        try:
            await conn.begin()
            # Lock the Coach Member row — serializes with set_coach's
            # ``SELECT ... FOR UPDATE`` on the same Member row.
            member_row = (
                await conn.execute(
                    sa_select(Member)
                    .where(Member.id == job.coach_member_id)
                    .with_for_update()
                )
            ).first()
            if member_row is None:
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

            # Lock and re-check the job and note — serializes with
            # ForgetStore which also locks the Member, MemberChannel,
            # Note, and job rows before deleting.  Together they guarantee
            # no notification can send after the note/job are deleted
            # (P1 #1 r6).
            job_row = (
                await conn.execute(
                    sa_select(SafetyOutboxJob)
                    .where(
                        SafetyOutboxJob.id == job.id,
                        SafetyOutboxJob.status == "sending",
                        SafetyOutboxJob.claimed_at == job.claimed_at,
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

            # Send while holding the narrow locks — they prevent concurrent
            # set_coach / link_member / forget-me from repointing
            # authorization (P1 #2 r5, P1 r8).
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
                # Identify the Coach by internal id, not by the
                # provider-side channel_user_id (issue #217 telemetry).
                logger.error(
                    "send timeout for outbox job %s (coach %s)",
                    job.id,
                    job.coach_member_id,
                )
                await conn.rollback()
                return "notifier send timed out"
            except Exception as exc:
                # Class only, never the message or traceback: an adapter's
                # error text routinely echoes the request body (the Note)
                # or the URL (the magic link).
                logger.error(
                    "failed to send outbox job %s (coach %s): %s",
                    job.id,
                    job.coach_member_id,
                    type(exc).__name__,
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

    async def _job_still_sending(self, job: SafetyOutboxJob) -> bool:
        """True when the outbox job still exists, is in ``sending`` status,
        AND still carries OUR ``claimed_at`` lease stamp.

        A job that was cascade-deleted (via forget-me deleting its Note)
        returns False here, so delivery can bail out without sending.
        A job whose status changed to ``failed`` (e.g. forget-me marking it
        before cascade) also returns False — defense in depth for P1 #1.
        A job reset by ``reset_stale_claims`` and re-claimed by another
        owner also returns False — the stale owner must not deliver.
        """
        async with self._outbox._sessions() as db:
            row = await db.get(SafetyOutboxJob, job.id)
            return (
                row is not None
                and row.status == "sending"
                and row.claimed_at == job.claimed_at
            )
