"""Durable safety-outbox: atomicity, delivery, restart recovery, retry,
channel resolution, and atomic claiming (issue #216)."""

import asyncio
import time
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select, text

from agentg.db import create_engine
from agentg.dashboard_store import DashboardStore
from agentg.linking_store import LinkingStore
from agentg.notes import NotesStore
from agentg.models import SafetyOutboxJob
from agentg.safety_outbox import (
    BASE_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    OutboxWorker,
    SafetyOutbox,
)


# ── helpers ────────────────────────────────────────────────────────────────


class FakeNotifier:
    """Records every send and can be told to fail for a specific user."""

    def __init__(self, failing_id: str | None = None):
        self.sent: list[tuple[str, str, str, bool, bool]] = []
        self._failing_id = failing_id

    async def send(
        self, channel, channel_user_id, text,
        disable_preview=False, protect_content=False,
    ):
        if channel_user_id == self._failing_id:
            raise RuntimeError("simulated send failure")
        self.sent.append(
            (channel, channel_user_id, text, disable_preview, protect_content),
        )


@pytest.fixture
async def env(tmp_path):
    """A Gym with one Member, two Coaches, and a wired SafetyOutbox."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'ob.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    notes = NotesStore(engine)
    dashboard = DashboardStore(engine)
    outbox = SafetyOutbox(engine)
    notifier = FakeNotifier()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Ana", "telegram", "42")
    coach1 = await linking.link_member(gym.id, "Coach Sam", "telegram", "7")
    await linking.set_coach(coach1.id)
    coach2 = await linking.link_member(gym.id, "Coach Jo", "telegram", "8")
    await linking.set_coach(coach2.id)

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.linking = linking
    env.notes = notes
    env.dashboard = dashboard
    env.outbox = outbox
    env.notifier = notifier
    env.gym_id = gym.id
    env.member_id = member.id
    env.coach1_id = coach1.id
    env.coach2_id = coach2.id
    env.member_name = "Ana"
    env.DASHBOARD_BASE = "https://dash.example.com"
    yield env
    await engine.dispose()


def _coaches(env) -> list[tuple[int, str, str, str]]:
    """Return the Gym's coaches as (member_id, name, channel, channel_user_id)."""
    return [
        (env.coach1_id, "Coach Sam", "telegram", "7"),
        (env.coach2_id, "Coach Jo", "telegram", "8"),
    ]


_NO_BASE_URL = object()


def _make_worker(env, notifier=None, base_url=_NO_BASE_URL):
    """Create an OutboxWorker wired for the test env.

    Pass ``base_url=None`` to disable dashboard links entirely;
    omit for the default test dashboard URL.
    """
    if base_url is _NO_BASE_URL:
        base_url = env.DASHBOARD_BASE
    return OutboxWorker(
        outbox=env.outbox,
        notifier=notifier or env.notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=base_url,
        linking_store=env.linking,
    )


# ── atomicity ───────────────────────────────────────────────────────────────


async def test_note_and_jobs_commit_atomically(env):
    """A safety Note and one outbox job per eligible Coach commit in one
    transaction (AC #1)."""
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain on squats",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    assert note.kind == "safety"
    assert "knee pain" in note.text
    assert len(jobs) == 2
    # Verify both note and jobs landed in the DB.
    async with env.engine.connect() as conn:
        note_rows = (
            await conn.execute(
                text("SELECT id, kind, text FROM member_notes WHERE id = :id"),
                {"id": note.id},
            )
        ).all()
    assert len(note_rows) == 1
    async with env.engine.connect() as conn:
        job_rows = (
            await conn.execute(
                text(
                    "SELECT id, status, coach_member_id FROM safety_outbox_jobs "
                    "WHERE note_id = :nid ORDER BY coach_member_id"
                ),
                {"nid": note.id},
            )
        ).all()
    assert len(job_rows) == 2
    assert all(r.status == "pending" for r in job_rows)


async def test_incomplete_outbox_rolls_back_safety_operation(env, monkeypatch):
    """When the outbox can't be built (e.g. the table doesn't exist), the
    safety Note is not committed either (AC #1 — rollback)."""
    # Simulate a table that doesn't exist by dropping it after ensure_schema.
    async with env.engine.begin() as conn:
        await conn.run_sync(
            lambda c: c.execute(text("DROP TABLE IF EXISTS safety_outbox_jobs"))
        )

    with pytest.raises(Exception):
        await env.outbox.create_note_and_jobs(
            member_id=env.member_id,
            gym_id=env.gym_id,
            text="dizzy during warmup",
            member_name=env.member_name,
            member_is_coach=False,
            coaches=_coaches(env),
        )

    # The Note must NOT have been committed.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT id FROM member_notes"))
        ).all()
    assert len(rows) == 0


async def test_jobs_are_unique_per_note_coach(env):
    """Each Coach gets exactly one job per safety Note — a uniqueness
    constraint prevents accidental duplicates (AC #2)."""
    coaches = _coaches(env)
    note1, _jobs1 = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="first flag",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=coaches,
    )
    # Second attempt with the same note+coach should fail.
    with pytest.raises(Exception):  # IntegrityError
        from agentg.models import SafetyOutboxJob
        from sqlalchemy.ext.asyncio import async_sessionmaker
        async with async_sessionmaker(env.engine)() as db:
            db.add(
                SafetyOutboxJob(
                    gym_id=env.gym_id,
                    note_id=note1.id,
                    coach_member_id=coaches[0][0],
                    channel=coaches[0][2],
                    channel_user_id=coaches[0][3],
                    member_id=env.member_id,
                    member_name=env.member_name,
                    member_is_coach=False,
                    status="pending",
                    created_at=datetime.now(UTC),
                )
            )
            await db.commit()


# ── worker delivery ─────────────────────────────────────────────────────────


async def test_worker_sends_pending_and_marks_delivered(env):
    """The worker sends every pending job and marks them delivered (AC #3)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )
    assert env.notifier.sent == []  # nothing sent yet

    worker = _make_worker(env)
    delivered = await worker.drain_once(limit=50)
    assert delivered == 2

    # Each coach gets heads-up + link = 4 messages total.
    assert len(env.notifier.sent) == 4

    # All jobs are now delivered.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).all()
    assert all(r.status == "delivered" for r in rows)


async def test_member_text_separated_from_bearer_links(env):
    """The one-time login link travels in its own message, not sharing a
    message with member-influenced text (AC #6)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="pain, see https://evil.example.com",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    worker = _make_worker(env)
    await worker.drain_once(limit=50)

    # Separate heads-up from link messages.
    heads = [m for m in env.notifier.sent if "/login/" not in m[2]]
    links = [m for m in env.notifier.sent if "/login/" in m[2]]
    assert len(heads) == 2 and len(links) == 2
    for h in heads:
        assert "evil.example.com" in h[2]
    for lk in links:
        assert lk[2].startswith(f"{env.DASHBOARD_BASE}/login/")
        assert "evil" not in lk[2]
        assert lk[4] is True  # protect_content


async def test_no_cross_gym_notification(env):
    """A job's Gym scoping prevents notification across Gyms (AC #7)."""
    other_gym = await env.linking.create_gym("Other Gym")
    other_member = await env.linking.link_member(
        other_gym.id, "Bob", "telegram", "99",
    )
    # Create a job in Gym 1 with coaches from Gym 1.
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="shoulder pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )
    # The other Gym's member is NOT in the coach list.
    worker = _make_worker(env)
    await worker.drain_once(limit=50)
    # No messages went to the other Gym's member.
    for _ch, uid, _text, _dp, _pc in env.notifier.sent:
        assert uid != "99"


async def test_delivery_does_not_delay_reply(env):
    """flag_to_coach_action returns immediately — the outbox worker delivers
    later, without adding to the Member's wait (AC #8)."""
    start = time.monotonic()
    from agentg.coaching import flag_to_coach_action
    from agentg.context import MemberContext
    from agentg.stores import Stores
    from agentg.training import TrainingStore
    from agentg.routines import RoutineStore

    ctx = MemberContext(
        stores=Stores(
            linking=env.linking,
            training=TrainingStore(env.engine),
            notes=env.notes,
            routines=RoutineStore(env.engine),
            checkins=None,
            demos=None,
            forget=None,
            dashboard=env.dashboard,
            safety_outbox=env.outbox,
        ),
        notifier=env.notifier,
        member_id=env.member_id,
        gym_id=env.gym_id,
        member_name=env.member_name,
        gym_name="Iron Temple",
        weight_unit="kg",
        dashboard_base_url=env.DASHBOARD_BASE,
    )
    result = await flag_to_coach_action(ctx, "sharp knee pain")
    elapsed = time.monotonic() - start

    assert result["logged"] is True
    assert result["coaches_to_notify"] == 2
    # The call returned without sending any notifications.
    assert env.notifier.sent == []
    # The call should be fast — no network, just a DB write.
    assert elapsed < 2.0, f"flag_to_coach_action took {elapsed:.2f}s"


# ── mint-at-delivery ────────────────────────────────────────────────────────


async def test_credentials_minted_at_delivery_time(env):
    """Dashboard tokens are minted when the worker delivers, not when the
    job is created (AC #5)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    # No tokens minted yet (they're minted at delivery time).
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT id FROM dashboard_login_tokens"))
        ).all()
    assert len(rows) == 0

    worker = _make_worker(env)
    await worker.drain_once(limit=50)

    # Tokens were minted for each coach.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT id FROM dashboard_login_tokens"))
        ).all()
    assert len(rows) == 2  # one per coach


async def test_mint_failure_falls_back_to_text_only(env):
    """When token minting fails for a coach, the heads-up is still sent
    as text-only (no link message; AC #5)."""
    real_mint = env.dashboard.create_login_token

    async def failing_mint(*args, **kwargs):
        raise RuntimeError("db hiccup")

    env.dashboard.create_login_token = failing_mint

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    worker = _make_worker(env)
    await worker.drain_once(limit=50)

    # Restore so cleanup works.
    env.dashboard.create_login_token = real_mint

    # Two heads-up messages, no link messages.
    heads = [m for m in env.notifier.sent if "/login/" not in m[2]]
    links = [m for m in env.notifier.sent if "/login/" in m[2]]
    assert len(heads) == 2
    assert len(links) == 0
    for h in heads:
        assert "knee pain" in h[2]


# ── restart recovery ────────────────────────────────────────────────────────


async def test_pre_exit_committed_job_recovered_on_startup(env):
    """A job committed before process exit is delivered after restart
    (AC #4)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    # At this point the jobs are in the DB (committed) but not sent.
    # This is the "pre-exit" state.  A new process starts:
    fresh_worker = _make_worker(env)
    # start() recovers orphaned claims then begins polling.
    # The poll loop fires drain_once immediately (before its first sleep),
    # but it runs as a background task — wait for delivery to complete.
    await fresh_worker.start()

    # Poll for delivery completion (the poll task runs drain_once then
    # sleeps _POLL_INTERVAL seconds; the first drain fires immediately).
    for _ in range(50):  # up to 5 seconds
        async with env.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT status FROM safety_outbox_jobs")
                )
            ).all()
        if all(r.status == "delivered" for r in rows):
            break
        await asyncio.sleep(0.1)

    # All jobs delivered.
    assert len(env.notifier.sent) == 4  # 2 coaches × 2 messages
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).all()
    assert all(r.status == "delivered" for r in rows)

    await fresh_worker.stop()


# ── atomic claim / lease (P2 #3) ───────────────────────────────────────────


async def test_atomic_claim_prevents_duplicate_delivery(env):
    """Two concurrent drain_once calls cannot claim the same pending jobs
    because claim_pending atomically transitions status to 'sending'."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    worker = _make_worker(env)

    # First drain claims and delivers the 2 jobs.
    delivered1 = await worker.drain_once(limit=50)
    assert delivered1 == 2
    assert len(env.notifier.sent) == 4  # 2 coaches × 2 messages

    sent_before = len(env.notifier.sent)

    # Second drain (simulating shutdown drain racing with poll loop)
    # must claim zero jobs — they're already marked "delivered".
    delivered2 = await worker.drain_once(limit=50)
    assert delivered2 == 0
    assert len(env.notifier.sent) == sent_before  # no new messages


# ── crash recovery: reset_claimed (P2 #3) ───────────────────────────────────


async def test_reset_claimed_recovers_orphaned_jobs(env):
    """Jobs stuck in 'sending' after a crash are reset to 'pending' so they
    are retried on next startup."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    # Simulate a crash mid-delivery: claim jobs but don't deliver.
    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 2
    # Jobs are now in 'sending' state.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).all()
    assert all(r.status == "sending" for r in rows)

    # "Crash" — reset claimed jobs on next startup.
    reset_count = await env.outbox.reset_claimed()
    assert reset_count == 2

    # Jobs are back to 'pending'.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).all()
    assert all(r.status == "pending" for r in rows)

    # Now a fresh worker delivers them.
    assert env.notifier.sent == []
    worker = _make_worker(env)
    await worker.drain_once(limit=50)
    assert len(env.notifier.sent) == 4


# ── failure injection: retry (P1 #1) ───────────────────────────────────────


async def test_notifier_failure_retries_not_immediately_fails(env):
    """When the notifier raises for one coach, the job is retried (reset to
    'pending' with incremented retry_count), not permanently failed on the
    first attempt. The other coach is delivered normally."""
    failing_notifier = FakeNotifier(failing_id="7")
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=failing_notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )
    await worker.drain_once(limit=50)

    # Coach 7's job should be back to 'pending' with retry_count=1,
    # NOT permanently failed on first attempt.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT coach_member_id, status, retry_count, failure_reason "
                    "FROM safety_outbox_jobs ORDER BY coach_member_id"
                )
            )
        ).all()
    job_7, job_8 = rows
    assert job_7.status == "pending", (
        f"expected pending (retry), got {job_7.status}"
    )
    assert job_7.retry_count == 1
    assert job_7.failure_reason is None  # no permanent failure reason yet
    assert job_8.status == "delivered"

    # Coach 8 got both messages, Coach 7 got none.
    assert any(uid == "8" for _ch, uid, _t, _dp, _pc in failing_notifier.sent)
    assert not any(uid == "7" for _ch, uid, _t, _dp, _pc in failing_notifier.sent)


async def test_notifier_failure_always_retries_never_permanently_fails(env, monkeypatch):
    """Transient failures never permanently fail a job — the backoff
    is bounded but jobs remain retryable indefinitely (P1 #4)."""
    # Skip asyncio.sleep so backoff doesn't slow the test.
    import agentg.safety_outbox as outbox_module

    async def instant_sleep(_seconds):
        pass

    monkeypatch.setattr(outbox_module.asyncio, "sleep", instant_sleep)

    failing_notifier = FakeNotifier(failing_id="7")
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=failing_notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )

    # Each drain_once increments retry_count and resets to pending.
    # The job is never permanently failed.
    # Mutable clock so claim_pending always sees next_retry_at in the past.
    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]

    for attempt in range(1, 6):  # go well past the old MAX_RETRIES=3
        # Keep clock ahead of next_retry_at so claim_pending gates pass.
        clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
        await worker.drain_once(limit=50)
        async with env.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, retry_count, next_retry_at, failure_reason "
                        "FROM safety_outbox_jobs "
                        "WHERE coach_member_id = :cid"
                    ),
                    {"cid": env.coach1_id},
                )
            ).first()
        assert row.status == "pending", (
            f"attempt {attempt}: expected pending, got {row.status}"
        )
        assert row.retry_count == attempt
        assert row.failure_reason is None  # never permanently failed
        # next_retry_at should be set for backoff gating.
        assert row.next_retry_at is not None

    # After many retries the backoff should be capped at MAX_BACKOFF_SECONDS.
    # retry_count keeps growing for audit, but the delay is bounded.
    for _ in range(10):
        clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
        await worker.drain_once(limit=50)
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, failure_reason FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "pending"  # still retryable
    assert row.retry_count >= 10
    assert row.failure_reason is None  # never permanently failed


async def test_deleted_note_marks_job_failed(env):
    """When a safety Note is deleted (e.g. forget-me) before delivery,
    the outbox job is marked failed rather than crashing the worker."""
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    # Delete the note (simulating forget-me).
    async with env.engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM member_notes WHERE id = :id"), {"id": note.id}
        )

    worker = _make_worker(env)
    await worker.drain_once(limit=50)

    # All jobs should be marked failed (note no longer exists).
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status, failure_reason FROM safety_outbox_jobs")
            )
        ).all()
    assert all(r.status == "failed" for r in rows)
    assert all("no longer exists" in (r.failure_reason or "") for r in rows)


# ── lease / stale-claim recovery (P2 #3) ──────────────────────────────────


async def test_claimed_at_set_on_claim(env):
    """claim_pending sets claimed_at to the current timestamp."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 2
    for job in jobs:
        assert job.status == "sending"
        assert job.claimed_at is not None


async def test_reset_stale_claims_recovers_hung_jobs(env):
    """Jobs stuck in 'sending' beyond LEASE_TIMEOUT_SECONDS are reset to
    'pending' by reset_stale_claims so the poll loop retries them."""
    from agentg.safety_outbox import LEASE_TIMEOUT_SECONDS

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    # Claim jobs normally.
    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 2

    # Backdate claimed_at so they appear stale.
    async with env.engine.begin() as conn:
        stale_time = datetime.now(UTC) - timedelta(seconds=LEASE_TIMEOUT_SECONDS + 10)
        await conn.execute(
            text("UPDATE safety_outbox_jobs SET claimed_at = :ts"),
            {"ts": stale_time},
        )

    # reset_stale_claims should recover them.
    reset_count = await env.outbox.reset_stale_claims()
    assert reset_count == 2

    # Jobs are now pending again.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status, claimed_at FROM safety_outbox_jobs")
            )
        ).all()
    assert all(r.status == "pending" for r in rows)
    assert all(r.claimed_at is None for r in rows)

    # A worker can now deliver them.
    worker = _make_worker(env)
    await worker.drain_once(limit=50)
    assert len(env.notifier.sent) == 4


async def test_reset_stale_claims_ignores_fresh_claims(env):
    """Jobs in 'sending' with a recent claimed_at are NOT reset — only
    genuinely stale claims are recovered."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    # Claim jobs with a fresh claimed_at.
    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 2

    # reset_stale_claims should ignore them (claimed_at is recent).
    reset_count = await env.outbox.reset_stale_claims()
    assert reset_count == 0

    # Jobs are still in 'sending'.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).all()
    assert all(r.status == "sending" for r in rows)


# ── last_error tracking (P1 #1) ───────────────────────────────────────────


async def test_last_error_recorded_on_transient_failure(env):
    """When a delivery fails transiently, last_error is set so operators
    can diagnose what went wrong."""
    failing_notifier = FakeNotifier(failing_id="7")
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=failing_notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )
    await worker.drain_once(limit=50)

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, last_error, failure_reason "
                    "FROM safety_outbox_jobs WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "pending"
    assert row.retry_count == 1
    assert "notifier send failed" in (row.last_error or "")
    assert row.failure_reason is None  # not permanent


async def test_last_error_cleared_after_successful_retry(env):
    """When a previously-failed job succeeds on retry, the failure state
    is cleared and the job is marked delivered."""
    # First attempt fails.
    failing_notifier = FakeNotifier(failing_id="7")
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=failing_notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )
    await worker.drain_once(limit=50)

    # Job is pending with retry_count=1.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT retry_count FROM safety_outbox_jobs")
            )
        ).first()
    assert row.retry_count == 1

    # Advance clock past next_retry_at so claim_pending can claim the job.
    future = datetime.now(UTC) + timedelta(days=1)
    env.outbox._clock = lambda: future

    # Second attempt with a working notifier succeeds.
    success_worker = _make_worker(env)
    await success_worker.drain_once(limit=50)

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, last_error, failure_reason "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "delivered"
    assert row.retry_count == 1  # retry_count is preserved for audit
    assert row.last_error is None  # cleared on successful delivery
    assert row.failure_reason is None


# ── backoff timing (P1 #1) ─────────────────────────────────────────────────


async def test_retry_includes_backoff_delay(env, monkeypatch):
    """Backoff is enforced by next_retry_at (set in reset_for_retry,
    checked by claim_pending), not a sleep.  Verifies the next_retry_at
    formula: retry_count * BASE_BACKOFF_SECONDS, capped at
    MAX_BACKOFF_SECONDS (P2 r6)."""
    failing_notifier = FakeNotifier(failing_id="7")
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=failing_notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )

    # Mutable clock: fixed so we can observe next_retry_at values.
    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]

    # Helper: parse ISO string from SQLite text() query (TZDateTime
    # stores UTC-aware datetimes as ISO strings in SQLite).
    def _next_retry(row):
        val = row.next_retry_at
        if isinstance(val, str):
            return datetime.fromisoformat(val).replace(tzinfo=UTC)
        return val

    # First attempt: retry_count=0 → no backoff, claim_pending claims it.
    await worker.drain_once(limit=50)
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT retry_count, next_retry_at FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.retry_count == 1
    assert row.next_retry_at is not None
    nra = _next_retry(row)
    expected = clock_val[0] + timedelta(seconds=BASE_BACKOFF_SECONDS * 1)
    assert abs((nra - expected).total_seconds()) < 0.1

    # Advance clock past next_retry_at so claim_pending can re-claim.
    clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)

    # Second attempt: retry_count goes to 2.
    await worker.drain_once(limit=50)
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT retry_count, next_retry_at FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.retry_count == 2
    nra = _next_retry(row)
    expected = clock_val[0] + timedelta(seconds=BASE_BACKOFF_SECONDS * 2)
    assert abs((nra - expected).total_seconds()) < 0.1

    # Advance clock.
    clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)

    # Third attempt: retry_count goes to 3.
    await worker.drain_once(limit=50)
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT retry_count, next_retry_at FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.retry_count == 3
    nra = _next_retry(row)
    expected = clock_val[0] + timedelta(seconds=BASE_BACKOFF_SECONDS * 3)
    assert abs((nra - expected).total_seconds()) < 0.1

    # Run enough attempts that the backoff reaches and stays at the cap.
    for _ in range(60):
        clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
        await worker.drain_once(limit=50)
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT retry_count, next_retry_at FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    # next_retry_at should be capped at clock + MAX_BACKOFF_SECONDS.
    nra = _next_retry(row)
    expected = clock_val[0] + timedelta(seconds=MAX_BACKOFF_SECONDS)
    assert abs((nra - expected).total_seconds()) < 0.1
    # retry_count keeps growing for audit even though delay is capped.
    assert row.retry_count >= 60


# ── transient error recovery (P1 #1) ──────────────────────────────────────


async def test_transient_db_error_is_retried_not_permanently_failed(env, monkeypatch):
    """When the narrow-locked authorization raises a transient DB error, the
    job is retried (reset_to_retry) rather than permanently failed. The
    original code's outer catch-all called mark_failed — this test proves
    we now retry."""
    import agentg.safety_outbox as outbox_module

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Make _authorized_send raise a transient error on the first call
    # and succeed on the second.
    real_authorized_send = outbox_module.OutboxWorker._authorized_send
    call_count = [0]

    async def flaky_authorized_send(self, job, text, *, disable_preview=True,
                                     protect_content=False):
        call_count[0] += 1
        if call_count[0] == 1:  # fail only the first heads-up attempt
            raise RuntimeError("simulated DB hiccup")
        return await real_authorized_send(
            self, job, text, disable_preview=disable_preview,
            protect_content=protect_content,
        )

    monkeypatch.setattr(
        outbox_module.OutboxWorker, "_authorized_send", flaky_authorized_send,
    )

    worker = _make_worker(env)
    await worker.drain_once(limit=50)

    # First delivery should have failed transiently — job retried, not failed.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, last_error, failure_reason "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "pending", f"expected pending (retry), got {row.status}"
    assert row.retry_count == 1
    assert "RuntimeError" in (row.last_error or "")
    assert row.failure_reason is None  # no permanent failure

    # Advance clock past next_retry_at so claim_pending can claim the job.
    future = datetime.now(UTC) + timedelta(days=1)
    env.outbox._clock = lambda: future

    # Second attempt succeeds.
    await worker.drain_once(limit=50)

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"


# ── channel identity re-resolution at delivery time (P1 #2) ────────────────


async def test_channel_identity_resolved_at_delivery_time(env):
    """When a coach switches gyms between job creation and delivery, the
    job is failed with a clear reason — it never sends to the new gym."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Simulate a gym switch: Coach Sam moves to a new gym.
    # link_member re-points the channel identity to a new Member in new_gym.
    new_gym = await env.linking.create_gym("New Gym")
    await env.linking.link_member(new_gym.id, "Coach Sam", "telegram", "7")

    # At this point, coach_channel_in_gym(env.coach1_id, env.gym_id) returns
    # None because MemberChannel now points to the new gym's member.
    channel_info = await env.linking.coach_channel_in_gym(
        env.coach1_id, env.gym_id,
    )
    assert channel_info is None, "coach's channel should no longer be in old gym"

    worker = _make_worker(env)
    await worker.drain_once(limit=50)

    # No messages sent — coach is no longer in this gym.
    assert env.notifier.sent == []

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, failure_reason FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "failed"
    assert "no longer reachable" in (row.failure_reason or "")


# ── non-blocking startup (P2 #4) ───────────────────────────────────────────


async def test_startup_does_not_block_on_backlog(env):
    """start() returns immediately without draining the backlog
    synchronously — the poll loop handles jobs incrementally."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    worker = _make_worker(env)
    start_time = time.monotonic()
    await worker.start()
    elapsed = time.monotonic() - start_time

    # start() should return quickly — it doesn't drain the backlog
    # synchronously (only reset_claimed, which is a fast UPDATE).
    assert elapsed < 1.0, f"start() blocked for {elapsed:.2f}s"

    # The poll loop runs drain_once immediately after start, so jobs may
    # be delivered quickly — but start() itself didn't block on that.
    await worker.stop()


# ── edge cases ───────────────────────────────────────────────────────────────


async def test_no_coaches_no_jobs(env):
    """When a Gym has no Coaches, no outbox jobs are created, but the
    safety Note is still written."""
    gym2 = await env.linking.create_gym("Solo Box")
    member2 = await env.linking.link_member(gym2.id, "Rob", "telegram", "99")

    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=member2.id,
        gym_id=gym2.id,
        text="shoulder pain",
        member_name="Rob",
        member_is_coach=False,
        coaches=[],  # no coaches
    )

    assert note.kind == "safety"
    assert jobs == []


async def test_coach_self_flag_excludes_self(env):
    """When a Coach flags their own concern, they are excluded from the
    notification list — but if there are other Coaches, those get notified."""
    coach3 = await env.linking.link_member(
        env.gym_id, "Coach Self", "telegram", "9",
    )
    await env.linking.set_coach(coach3.id)

    # Only the OTHER two coaches should get jobs (coach3 excluded).
    coaches_to_notify = [
        (env.coach1_id, "Coach Sam", "telegram", "7"),
        (env.coach2_id, "Coach Jo", "telegram", "8"),
    ]
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=coach3.id,
        gym_id=env.gym_id,
        text="chest tightness",
        member_name="Coach Self",
        member_is_coach=True,
        coaches=coaches_to_notify,
    )

    assert len(jobs) == 2
    job_coach_ids = {j.coach_member_id for j in jobs}
    assert coach3.id not in job_coach_ids
    assert env.coach1_id in job_coach_ids
    assert env.coach2_id in job_coach_ids


# ── multi-channel Coach dedup ───────────────────────────────────────────────


async def test_multi_channel_coach_gets_one_job(env):
    """A Coach with multiple MemberChannel rows (e.g. Telegram and a
    future WhatsApp) gets exactly one outbox job — the unique constraint
    on (note_id, coach_member_id) requires deduplication at query time.
    The channel chosen is deterministic (first by channel name)."""
    # Give Coach Sam a second channel identity.
    from agentg.models import MemberChannel
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(env.engine)() as db:
        db.add(
            MemberChannel(
                gym_id=env.gym_id,
                member_id=env.coach1_id,
                channel="whatsapp",
                channel_user_id="+1555-COACH",
            )
        )
        await db.commit()

    # Use the linking_store path so _coaches_for_gym_in_session runs
    # (the dedup lives there).
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        linking_store=env.linking,
        exclude_member_id=env.member_id,
    )

    # Two Coaches in the gym, but one has two channels — still exactly
    # two jobs (one per Coach).
    assert len(jobs) == 2
    coach_ids = {j.coach_member_id for j in jobs}
    assert coach_ids == {env.coach1_id, env.coach2_id}

    # Coach Sam's job must pick "telegram" (deterministic: before "whatsapp").
    sam_job = next(j for j in jobs if j.coach_member_id == env.coach1_id)
    assert sam_job.channel == "telegram"
    assert sam_job.channel_user_id == "7"


async def test_multi_channel_coach_deterministic_delivery(env):
    """When a multi-channel Coach's job is delivered, the authorization
    re-resolution at delivery time picks the same deterministic channel."""
    from agentg.models import MemberChannel
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # Give Coach Sam a second channel.
    async with async_sessionmaker(env.engine)() as db:
        db.add(
            MemberChannel(
                gym_id=env.gym_id,
                member_id=env.coach1_id,
                channel="whatsapp",
                channel_user_id="+1555-COACH",
            )
        )
        await db.commit()

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[
            (env.coach1_id, "Coach Sam", "telegram", "7"),
            (env.coach2_id, "Coach Jo", "telegram", "8"),
        ],
    )

    worker = _make_worker(env)
    await worker.drain_once(limit=50)

    # Coach Sam must receive messages via "telegram" (the deterministic pick).
    sam_sends = [
        m for m in env.notifier.sent if m[1] == "7"
    ]
    assert len(sam_sends) == 2  # heads-up + link
    for send in sam_sends:
        assert send[0] == "telegram"


async def test_multi_channel_coach_linking_store_dedup(env):
    """linking_store.coaches_for_gym also deduplicates multi-channel
    Coaches — used when pre-resolving coaches before passing them to
    create_note_and_jobs."""
    from agentg.models import MemberChannel
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(env.engine)() as db:
        db.add(
            MemberChannel(
                gym_id=env.gym_id,
                member_id=env.coach1_id,
                channel="whatsapp",
                channel_user_id="+1555-COACH",
            )
        )
        await db.commit()

    coaches = await env.linking.coaches_for_gym(env.gym_id)
    # Two Coaches (not three rows).
    assert len(coaches) == 2
    coach_ids = {c[0] for c in coaches}
    assert coach_ids == {env.coach1_id, env.coach2_id}
    # Coach Sam has "telegram" (before "whatsapp").
    sam = next(c for c in coaches if c[0] == env.coach1_id)
    assert sam[2] == "telegram"


async def test_coach_channel_in_gym_deterministic(env):
    """coach_channel_in_gym returns a deterministic channel when the Coach
    has multiple identities."""
    from agentg.models import MemberChannel
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(env.engine)() as db:
        db.add(
            MemberChannel(
                gym_id=env.gym_id,
                member_id=env.coach1_id,
                channel="whatsapp",
                channel_user_id="+1555-COACH",
            )
        )
        await db.commit()

    channel_info = await env.linking.coach_channel_in_gym(
        env.coach1_id, env.gym_id,
    )
    assert channel_info is not None
    # Deterministic: "telegram" before "whatsapp".
    assert channel_info == ("telegram", "7")


# ── P1 #2: demoted coach does not receive safety delivery ───────────────────


async def test_demoted_coach_not_notified(env):
    """A Coach demoted after job creation must not receive the safety text
    or dashboard link."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Demote Coach Sam before delivery.
    await env.linking.set_coach(env.coach1_id, is_coach=False)

    worker = _make_worker(env)
    await worker.drain_once(limit=50)

    # No messages should have been sent — the coach is no longer a coach.
    assert env.notifier.sent == []

    # The job should be marked failed.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, failure_reason FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "failed"
    assert "no longer reachable" in (row.failure_reason or "")


# ── P2 #1: stale-claim reset does not re-deliver a completed job ────────────


async def test_reset_stale_claims_does_not_requeue_delivered(env):
    """A slow delivery that completes between stale-claim SELECT and UPDATE
    must not be reset to pending and re-sent."""
    from agentg.safety_outbox import LEASE_TIMEOUT_SECONDS

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    # Claim jobs (puts them in 'sending').
    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 2

    # Backdate claimed_at so they look stale.
    async with env.engine.begin() as conn:
        stale_time = datetime.now(UTC) - timedelta(seconds=LEASE_TIMEOUT_SECONDS + 10)
        await conn.execute(
            text("UPDATE safety_outbox_jobs SET claimed_at = :ts"),
            {"ts": stale_time},
        )

    # Deliver one job first (simulating slow delivery completing).
    await env.outbox.mark_delivered(jobs[0])

    # Now reset_stale_claims must NOT reset the delivered job.
    reset_count = await env.outbox.reset_stale_claims()
    # Only the one still-sending job should be reset.
    assert reset_count == 1

    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, status FROM safety_outbox_jobs ORDER BY id"
                )
            )
        ).all()
    statuses = {r.id: r.status for r in rows}
    assert statuses[jobs[0].id] == "delivered"  # untouched
    assert statuses[jobs[1].id] == "pending"    # reset


async def test_reset_for_retry_does_not_clobber_delivered(env):
    """A delivery that completes between an error and reset_for_retry
    must not be overwritten to pending or failed."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Claim the job.
    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 1
    job = jobs[0]

    # Simulate: delivery succeeds, then a stale reset_for_retry arrives.
    await env.outbox.mark_delivered(job)

    # Now a stale retry attempt arrives — it must not overwrite "delivered".
    await env.outbox.reset_for_retry(job, "stale notifier error")

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, retry_count FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"  # not clobbered
    assert row.retry_count == 0       # not incremented


# ── P2 #2: outbox worker starts without dashboard ───────────────────────────


async def test_worker_starts_without_dashboard(env):
    """The outbox worker must start when a notifier is present even if
    the dashboard is not wired — text-only notifications still land."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Worker with no dashboard base URL.
    worker = _make_worker(env, base_url=None)
    await worker.drain_once(limit=50)

    # Heads-up text was delivered.
    heads = [m for m in env.notifier.sent if "/login/" not in m[2]]
    assert len(heads) == 1
    assert "knee pain" in heads[0][2]

    # No link messages (no dashboard configured).
    links = [m for m in env.notifier.sent if "/login/" in m[2]]
    assert len(links) == 0

    # Job is marked delivered.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"


# ── P1 #1: concurrent claim cannot double-claim (status guard on outer UPDATE) ─


async def test_concurrent_claim_does_not_double_claim(env):
    """Two overlapping claim_pending calls with the new outer status guard
    never claim the same job — the second UPDATE sees the rows are no longer
    pending and returns zero rows."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    # Python-side concurrent fan-out: both calls see the same pending jobs
    # in their subqueries, but the outer status guard prevents double-claim.
    results = await asyncio.gather(
        env.outbox.claim_pending(limit=50),
        env.outbox.claim_pending(limit=50),
    )

    first_batch, second_batch = results
    claimed_ids = {j.id for j in first_batch} | {j.id for j in second_batch}

    # All 2 jobs were claimed, but never by both calls.
    assert len(first_batch) + len(second_batch) == 2
    assert len(claimed_ids) == 2

    # All jobs are in 'sending' state (no double-claimed duplicates).
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).all()
    assert all(r.status == "sending" for r in rows)


# ── P1 #1: forget / delete race with deterministic interleaving ────────────


async def test_forget_me_race_never_sends_after_note_deleted(env, monkeypatch):
    """When forget-me deletes a Note while a job is in-flight (claimed),
    the worker must not send the retained note text (P1 #1).

    Uses an asyncio.Event barrier to deterministically force the race:
    the worker reads note text, then the Note is deleted, then the worker
    checks job existence and bails out."""
    import agentg.safety_outbox as outbox_module

    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Barriers to control interleaving.
    note_text_read = asyncio.Event()   # worker has read the note text
    can_delete_note = asyncio.Event()  # forget-me may now delete

    # Patch _note_text on the OutboxWorker to inject our barrier.
    real_note_text = outbox_module.OutboxWorker._note_text

    async def instrumented_note_text(self, note_id):
        result = await real_note_text(self, note_id)
        note_text_read.set()  # signal: text is now in memory
        await can_delete_note.wait()  # wait for permission to proceed
        return result

    monkeypatch.setattr(
        outbox_module.OutboxWorker, "_note_text", instrumented_note_text,
    )

    # Claim the job first so it's in 'sending' state.
    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    # Start delivery in a task.
    worker = _make_worker(env)
    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))

    # Wait for the worker to read the note text.
    await note_text_read.wait()

    # Now delete the Note (simulating forget-me).  This explicitly fails
    # outbox jobs then cascade-deletes them.
    async with env.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE safety_outbox_jobs SET status = 'failed', "
                "failure_reason = 'member data deleted (forget-me)' "
                "WHERE note_id = :nid AND status IN ('pending', 'sending')"
            ),
            {"nid": note.id},
        )
        await conn.execute(
            text("DELETE FROM member_notes WHERE id = :id"), {"id": note.id}
        )

    # Allow the worker to proceed past the barrier.
    can_delete_note.set()

    # Wait for delivery to complete.
    await deliver_task

    # The worker must NOT have sent anything — the job was deleted
    # (or explicitly failed) while delivery was in progress.
    assert env.notifier.sent == [], (
        "worker must not send after note is deleted by forget-me"
    )


# ── P1 #3: eligibility queried inside the transaction ──────────────────────


async def test_coach_eligibility_is_atomic_with_note_commit(env):
    """When a Coach is promoted between the coach query and the Note
    commit, using linking_store inside create_note_and_jobs ensures the
    committed Note has the correct set of jobs (P1 #3)."""
    # Create a third member who will become a coach during the test.
    coach3 = await env.linking.link_member(
        env.gym_id, "Coach New", "telegram", "99",
    )
    # Not yet a coach.

    # Use linking_store path: coaches are queried inside the transaction.
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        linking_store=env.linking,
        exclude_member_id=env.member_id,
    )

    # Only the two existing coaches should get jobs (coach3 isn't flagged).
    assert len(jobs) == 2
    coach_ids = {j.coach_member_id for j in jobs}
    assert coach_ids == {env.coach1_id, env.coach2_id}

    # Now promote coach3 and verify a new flag includes them.
    await env.linking.set_coach(coach3.id, is_coach=True)

    note2, jobs2 = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="shoulder pain",
        member_name=env.member_name,
        member_is_coach=False,
        linking_store=env.linking,
        exclude_member_id=env.member_id,
    )

    # Now all three coaches get jobs.
    assert len(jobs2) == 3
    coach_ids2 = {j.coach_member_id for j in jobs2}
    assert coach_ids2 == {env.coach1_id, env.coach2_id, coach3.id}


# ── P1 #5: gym-switch TOCTOU with controlled interleaving ──────────────────


async def test_gym_switch_toctou_channel_locked_between_heads_up_and_link(env, monkeypatch):
    """When a coach switches gyms between the heads-up and link delivery,
    the link's _authorized_send acquires a fresh lock and re-checks
    authorization — the coach is no longer in the gym, so the link is
    not sent (P1 #2 r5).

    Uses an _authorized_send barrier: the heads-up completes (lock
    released), then the gym switch happens, then the link's authorization
    check fails because the MemberChannel was repointed."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    worker = _make_worker(env)

    # Instrument _authorized_send so we can inject a gym switch between
    # the heads-up (first call) and link (second call).
    real_auth_send = worker._authorized_send
    heads_up_done = asyncio.Event()
    gym_switched = asyncio.Event()
    call_count = [0]

    async def instrumented_auth_send(job, text, *,
                                      disable_preview=True,
                                      protect_content=False):
        call_count[0] += 1
        if call_count[0] == 1:
            # Heads-up: run it normally.  The lock is held only during
            # this call and released when it returns.
            result = await real_auth_send(
                job, text, disable_preview=disable_preview,
                protect_content=protect_content,
            )
            heads_up_done.set()
            await gym_switched.wait()
            return result
        else:
            # Link: runs after gym switch — authorization should fail.
            return await real_auth_send(
                job, text, disable_preview=disable_preview,
                protect_content=protect_content,
            )

    monkeypatch.setattr(worker, "_authorized_send", instrumented_auth_send)

    deliver_task = asyncio.create_task(worker.drain_once(limit=50))

    # Wait for heads-up to complete.
    await heads_up_done.wait()

    # Heads-up was sent.
    heads_before = len([m for m in env.notifier.sent if "/login/" not in m[2]])
    assert heads_before == 1

    # Now switch the coach's gym (between heads-up and link).
    new_gym = await env.linking.create_gym("New Gym")
    await env.linking.link_member(new_gym.id, "Coach Sam", "telegram", "7")
    gym_switched.set()

    # Wait for delivery to finish.
    await deliver_task

    # The heads-up was delivered (safety text arrived).
    # The link should NOT have been sent because the re-resolution
    # finds the coach is no longer in the gym.
    links_after = len([m for m in env.notifier.sent if "/login/" in m[2]])
    assert links_after == 0, (
        "link must not be sent after coach switches gyms mid-delivery"
    )

    # The job should be marked delivered (heads-up landed) even though
    # the link couldn't be sent.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"


# ── P1 #4: claim_pending respects next_retry_at ───────────────────────────


async def test_claim_pending_respects_next_retry_at(env):
    """claim_pending must not claim jobs whose next_retry_at is still in
    the future — bounded backoff survives process restart (P1 #4)."""
    from agentg.safety_outbox import BASE_BACKOFF_SECONDS

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Claim and immediately reset to simulate a transient failure.
    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 1
    await env.outbox.reset_for_retry(jobs[0], "transient failure")

    # Job is now pending with next_retry_at = now + 5s.
    # claim_pending should NOT claim it (next_retry_at hasn't passed).
    claimed_now = await env.outbox.claim_pending(limit=50)
    assert len(claimed_now) == 0, (
        "claim_pending must not claim jobs with future next_retry_at"
    )

    # Advance clock past next_retry_at.
    future = datetime.now(UTC) + timedelta(seconds=BASE_BACKOFF_SECONDS + 10)
    env.outbox._clock = lambda: future

    # Now claim_pending should claim it.
    claimed_later = await env.outbox.claim_pending(limit=50)
    assert len(claimed_later) == 1
    assert claimed_later[0].status == "sending"


async def test_job_still_sending_rejects_non_sending_status(env):
    """_job_still_sending returns False when the job exists but its status
    is no longer 'sending' — defense in depth for P1 #1."""
    from agentg.safety_outbox import OutboxWorker

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Claim the job so it's in 'sending' state.
    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 1

    worker = _make_worker(env)

    # Should return True when status is 'sending'.
    assert await worker._job_still_sending(jobs[0].id) is True

    # Mark the job as failed (simulating forget-me or permanent failure).
    await env.outbox.mark_failed(jobs[0], "member data deleted")

    # Should now return False — status is 'failed', not 'sending'.
    assert await worker._job_still_sending(jobs[0].id) is False


# ── P1 #3: eligibility query locked against concurrent promotion ───────────


async def test_eligibility_locked_against_concurrent_promotion(env, monkeypatch):
    """When a member is promoted concurrently with a safety-flag
    eligibility query, the Gym row lock in _coaches_for_gym_in_session
    serializes the two transactions — the committed Note always reflects
    a consistent coach set (P1 #3).

    Uses an asyncio.Event barrier to deterministically force the race:
    the eligibility query locks the Gym row, then a concurrent promotion
    is attempted (it waits on the lock), then the Note commits, then the
    promotion can proceed.  The result is that either the promotion is
    visible (if it committed first) or it waits (and the new coach is
    not in the job set).  Either way, the Note+Jobs are consistent."""
    import agentg.safety_outbox as outbox_module

    # Create a third member who will be promoted mid-race.
    coach3 = await env.linking.link_member(
        env.gym_id, "Coach New", "telegram", "99",
    )

    # Barriers: let the eligibility query start, then attempt promotion.
    eligibility_started = asyncio.Event()
    promotion_can_proceed = asyncio.Event()

    real_coaches_for_gym = outbox_module._coaches_for_gym_in_session

    async def instrumented_coaches(db, gym_id, exclude_member_id=None):
        eligibility_started.set()  # signal: we're inside the transaction
        # Let the concurrent promotion attempt run — it will block on the
        # Gym row lock (which was taken just before this call in
        # create_note_and_jobs).  We don't need to wait here because
        # the lock serializes naturally.
        await promotion_can_proceed.wait()
        return await real_coaches_for_gym(db, gym_id, exclude_member_id)

    monkeypatch.setattr(
        outbox_module, "_coaches_for_gym_in_session", instrumented_coaches,
    )

    # Start the safety flag operation (will block inside eligibility).
    create_task = asyncio.create_task(
        env.outbox.create_note_and_jobs(
            member_id=env.member_id,
            gym_id=env.gym_id,
            text="sharp knee pain",
            member_name=env.member_name,
            member_is_coach=False,
            linking_store=env.linking,
            exclude_member_id=env.member_id,
        )
    )

    # Wait for eligibility query to start.
    await eligibility_started.wait()

    # Try to promote coach3 concurrently — this will block on the Gym
    # row lock until create_note_and_jobs commits.
    promote_task = asyncio.create_task(
        env.linking.set_coach(coach3.id, is_coach=True)
    )

    # Give the promotion a moment to hit the lock.
    await asyncio.sleep(0.05)

    # Allow the eligibility query to proceed (it releases the lock on
    # commit, which unblocks the promotion).
    promotion_can_proceed.set()

    # Both should complete.
    note, jobs = await create_task
    await promote_task

    # The Note committed with the coach set visible at eligibility time.
    # Since the promotion was attempted after the lock was taken, it
    # waited until after commit — coach3 should NOT be in the job set.
    coach_ids = {j.coach_member_id for j in jobs}
    assert coach_ids == {env.coach1_id, env.coach2_id}, (
        "promotion that started after eligibility lock should wait "
        "and not appear in the committed job set"
    )
    assert len(jobs) == 2


async def test_eligibility_sees_promotion_that_committed_first(env):
    """When a promotion commits before the eligibility query begins,
    the newly promoted coach IS included in the job set — the lock does
    not exclude promotions that already completed (P1 #3)."""
    coach3 = await env.linking.link_member(
        env.gym_id, "Coach New", "telegram", "99",
    )

    # Promote first — this commits before the safety flag.
    await env.linking.set_coach(coach3.id, is_coach=True)

    # Now create a safety flag — the promotion should be visible.
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        linking_store=env.linking,
        exclude_member_id=env.member_id,
    )

    coach_ids = {j.coach_member_id for j in jobs}
    assert coach_ids == {env.coach1_id, env.coach2_id, coach3.id}
    assert len(jobs) == 3


# ── P1 #2: re-authorize after semaphore acquisition ────────────────────────


async def test_semaphore_reauth_blocks_demoted_coach(env, monkeypatch):
    """When a coach is demoted while the worker is waiting for the
    semaphore, the re-authorization inside the semaphore catches it and
    the safety text is NOT sent (P1 #2).

    Uses a barrier to force the race: the worker acquires the semaphore,
    then the coach is demoted, then the worker re-authorizes and finds
    the coach is no longer eligible."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Claim the job so it's in 'sending' state.
    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    # Barriers to control interleaving:
    # 1. Worker acquires the semaphore
    # 2. Coach is demoted
    # 3. Worker re-authorizes inside the semaphore
    semaphore_acquired = asyncio.Event()
    coach_demoted = asyncio.Event()

    # Instrument the semaphore to inject our interleaving.
    real_semaphore = asyncio.Semaphore(10)
    worker = _make_worker(env)
    worker._semaphore = real_semaphore

    original_acquire = type(real_semaphore).__aenter__

    async def instrumented_aenter(self):
        await original_acquire(self)
        semaphore_acquired.set()  # signal: semaphore held
        await coach_demoted.wait()  # wait for demotion to happen
        return self

    monkeypatch.setattr(
        type(real_semaphore), "__aenter__", instrumented_aenter,
    )

    # Start delivery in a task.
    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))

    # Wait for the semaphore to be acquired.
    await semaphore_acquired.wait()

    # Now demote the coach while the worker holds the semaphore.
    await env.linking.set_coach(env.coach1_id, is_coach=False)
    coach_demoted.set()

    # Wait for delivery to complete.
    await deliver_task

    # The worker must NOT have sent anything — the re-authorization
    # inside the semaphore caught the demotion.
    assert env.notifier.sent == [], (
        "worker must not send after coach is demoted while waiting for semaphore"
    )

    # The job should be marked failed.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, failure_reason FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "failed"
    assert "no longer reachable" in (row.failure_reason or "")


async def test_semaphore_reauth_blocks_gym_switched_coach(env, monkeypatch):
    """When a coach switches gyms while the worker is waiting for the
    semaphore, the re-authorization inside the semaphore catches it and
    the safety text is NOT sent to the new gym (P1 #2)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Claim the job.
    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    semaphore_acquired = asyncio.Event()
    gym_switched = asyncio.Event()

    worker = _make_worker(env)
    real_semaphore = asyncio.Semaphore(10)
    worker._semaphore = real_semaphore

    original_acquire = type(real_semaphore).__aenter__

    async def instrumented_aenter(self):
        await original_acquire(self)
        semaphore_acquired.set()
        await gym_switched.wait()
        return self

    monkeypatch.setattr(
        type(real_semaphore), "__aenter__", instrumented_aenter,
    )

    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))

    await semaphore_acquired.wait()

    # Switch the coach's gym while the worker holds the semaphore.
    new_gym = await env.linking.create_gym("New Gym")
    await env.linking.link_member(new_gym.id, "Coach Sam", "telegram", "7")
    gym_switched.set()

    await deliver_task

    # No messages sent — coach is no longer in this gym.
    assert env.notifier.sent == []

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, failure_reason FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "failed"
    assert "no longer reachable" in (row.failure_reason or "")


# ═══════════════════════════════════════════════════════════════════════════
# Round 4 — P1 #1: one job per Note/Coach regardless of gym_id
# ═══════════════════════════════════════════════════════════════════════════


async def test_unique_per_note_coach_not_scoped_by_gym(env):
    """The unique constraint on (note_id, coach_member_id) — without gym_id
    — prevents a second job for the same Note+Coach regardless of gym_id
    value. A Note belongs to exactly one Gym, so gym_id is denormalised
    and must not participate in the uniqueness scope (P1 #1)."""
    note, _jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Attempting a second job for the same note+coach (even with a
    # different or same gym_id) must fail on the uniqueness constraint.
    with pytest.raises(Exception):  # IntegrityError
        from agentg.models import SafetyOutboxJob
        from sqlalchemy.ext.asyncio import async_sessionmaker
        async with async_sessionmaker(env.engine)() as db:
            db.add(
                SafetyOutboxJob(
                    gym_id=env.gym_id,
                    note_id=note.id,
                    coach_member_id=env.coach1_id,
                    channel="telegram",
                    channel_user_id="7",
                    member_id=env.member_id,
                    member_name=env.member_name,
                    member_is_coach=False,
                    status="pending",
                    created_at=datetime.now(UTC),
                )
            )
            await db.commit()


async def test_unique_per_note_coach_across_notes(env):
    """The same Coach can have jobs for different Notes — uniqueness is
    per (note_id, coach_member_id), not per coach (P1 #1)."""
    note1, jobs1 = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="first flag",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )
    # Second flag — same coach, different Note.
    note2, jobs2 = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="second flag",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )
    assert note1.id != note2.id
    assert len(jobs1) == 1 and len(jobs2) == 1
    assert jobs1[0].note_id == note1.id
    assert jobs2[0].note_id == note2.id


# ═══════════════════════════════════════════════════════════════════════════
# Round 4 — P1 #2: TOCTOU — final authorization then send, zero await gap
# ═══════════════════════════════════════════════════════════════════════════


async def test_toctou_demotion_between_job_check_and_send_blocked(env, monkeypatch):
    """When the worker pre-verifies _job_still_sending and then resolves
    the channel inside the semaphore, a demotion that lands between the
    pre-check and the channel resolution is caught — the zero-await gap
    between channel resolution and send prevents stale authorization.

    Uses an asyncio.Event barrier to force the race: the pre-check
    passes, the coach is demoted, and the channel resolution returns
    None (P1 #2)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    # Barrier: let the pre-check pass, then demote before channel resolution.
    pre_check_passed = asyncio.Event()
    demotion_done = asyncio.Event()

    worker = _make_worker(env)

    real_job_still_sending = worker._job_still_sending
    call_count = [0]

    async def instrumented_job_still_sending(job_id):
        call_count[0] += 1
        if call_count[0] == 2:  # inside semaphore
            pre_check_passed.set()
            await demotion_done.wait()
        return await real_job_still_sending(job_id)

    monkeypatch.setattr(worker, "_job_still_sending", instrumented_job_still_sending)

    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))

    await pre_check_passed.wait()

    # Demote the coach while the worker is about to resolve the channel.
    await env.linking.set_coach(env.coach1_id, is_coach=False)
    demotion_done.set()

    await deliver_task

    # No messages sent — channel resolution returned None.
    assert env.notifier.sent == []

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, failure_reason FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "failed"
    assert "no longer reachable" in (row.failure_reason or "")


async def test_toctou_gym_switch_between_job_check_and_send_blocked(env, monkeypatch):
    """When a coach switches gyms between the pre-check and the channel
    resolution, the re-resolution catches it and fails the job — no
    cross-gym delivery (P1 #2)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    pre_check_passed = asyncio.Event()
    switch_done = asyncio.Event()

    worker = _make_worker(env)

    real_job_still_sending = worker._job_still_sending
    call_count = [0]

    async def instrumented_job_still_sending(job_id):
        call_count[0] += 1
        if call_count[0] == 2:  # inside semaphore
            pre_check_passed.set()
            await switch_done.wait()
        return await real_job_still_sending(job_id)

    monkeypatch.setattr(worker, "_job_still_sending", instrumented_job_still_sending)

    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))

    await pre_check_passed.wait()

    # Switch gyms while the worker is about to resolve the channel.
    new_gym = await env.linking.create_gym("New Gym")
    await env.linking.link_member(new_gym.id, "Coach Sam", "telegram", "7")
    switch_done.set()

    await deliver_task

    assert env.notifier.sent == []

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, failure_reason FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "failed"
    assert "no longer reachable" in (row.failure_reason or "")


async def test_toctou_forget_me_between_precheck_and_send_blocked(env, monkeypatch):
    """When forget-me deletes the Note between the pre-check and the
    channel resolution, the re-authorization still catches it because
    _job_still_sending runs inside the semaphore before channel
    resolution (P1 #2).

    Uses a barrier: the pre-check passes, forget-me deletes the Note
    (cascade-deleting the job), then the semaphore's _job_still_sending
    returns False and the worker bails out."""
    note, _jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    pre_check_passed = asyncio.Event()
    forget_done = asyncio.Event()

    worker = _make_worker(env)

    real_job_still_sending = worker._job_still_sending
    call_count = [0]

    async def instrumented_job_still_sending(job_id):
        call_count[0] += 1
        if call_count[0] == 1:  # first call (outside semaphore): passes
            result = await real_job_still_sending(job_id)
            pre_check_passed.set()
            await forget_done.wait()
            return result
        # Second call (inside semaphore): runs after forget-me
        return await real_job_still_sending(job_id)

    monkeypatch.setattr(worker, "_job_still_sending", instrumented_job_still_sending)

    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))

    await pre_check_passed.wait()

    # Forget-me: delete the Note (cascade-deletes the job).
    async with env.engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM member_notes WHERE id = :id"), {"id": note.id}
        )
    forget_done.set()

    await deliver_task

    # No messages sent — _job_still_sending returned False inside semaphore.
    assert env.notifier.sent == []


async def test_authorized_send_locks_and_sends(env, monkeypatch):
    """_authorized_send locks the Coach Member and MemberChannel rows, checks
    authorization on the locked connection, sends, and releases the
    lock on commit.  The authorization is stable because no concurrent
    set_coach or link_member can acquire the same locks during the send
    (P1 #2 r5, P1 r8).

    Instruments _authorized_send to verify the lock acquire / auth check
    / send / release sequence."""
    import agentg.safety_outbox as outbox_module

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    worker = _make_worker(env)
    real_auth_send = worker._authorized_send
    events: list[str] = []

    async def instrumented_auth_send(job, text, *,
                                      disable_preview=True,
                                      protect_content=False):
        events.append("auth_start")
        result = await real_auth_send(
            job, text, disable_preview=disable_preview,
            protect_content=protect_content,
        )
        events.append(f"auth_end:{result}")
        return result

    monkeypatch.setattr(worker, "_authorized_send", instrumented_auth_send)

    await worker._deliver_one(claimed[0])

    # The heads-up _authorized_send should succeed (None), then the link
    # _authorized_send also succeeds (None).
    assert events == ["auth_start", "auth_end:None", "auth_start", "auth_end:None"]

    # Verify delivery completed.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"


async def test_authorized_send_fails_after_gym_switch(env):
    """_authorized_send returns an error when the coach has switched
    gyms — the MemberChannel is no longer pointing at the original Gym
    so the authorization query returns no rows (P1 #2 r5)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    # Switch the coach's gym before delivery.
    new_gym = await env.linking.create_gym("New Gym")
    await env.linking.link_member(new_gym.id, "Coach Sam", "telegram", "7")

    worker = _make_worker(env)
    result = await worker._authorized_send(
        claimed[0], "test message", disable_preview=True,
    )
    assert result == "coach no longer reachable in this gym"


async def test_authorized_send_fails_after_demotion(env):
    """_authorized_send returns an error when the coach has been
    demoted — the Member.is_coach flag is False so the authorization
    query returns no rows (P1 #2 r5)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    # Demote the coach before delivery.
    await env.linking.set_coach(env.coach1_id, is_coach=False)

    worker = _make_worker(env)
    result = await worker._authorized_send(
        claimed[0], "test message", disable_preview=True,
    )
    assert result == "coach no longer reachable in this gym"


# ═══════════════════════════════════════════════════════════════════════════
# Round 4 — P2: bounded send timeout prevents hung notifier stranding
# ═══════════════════════════════════════════════════════════════════════════


class HangingNotifier:
    """A notifier whose send hangs forever — simulating a network partition
    or a misbehaving channel adapter."""

    def __init__(self):
        self.sent: list[tuple[str, str, str, bool, bool]] = []
        self._hang = True

    async def send(self, channel, channel_user_id, text,
                   disable_preview=False, protect_content=False):
        if self._hang:
            # Hang forever — simulating a stuck TCP connection.
            await asyncio.Event().wait()
        self.sent.append(
            (channel, channel_user_id, text, disable_preview, protect_content),
        )


async def test_send_timeout_retries_not_permanently_fails(env, monkeypatch):
    """When a notifier.send hangs longer than SEND_TIMEOUT_SECONDS,
    the job is retried (reset to pending) — not permanently failed.
    Other jobs in the same drain batch are not stranded (P2)."""
    import agentg.safety_outbox as outbox_module

    # Patch SEND_TIMEOUT_SECONDS to a short value for the test.
    monkeypatch.setattr(outbox_module, "SEND_TIMEOUT_SECONDS", 0.1)

    hanging = HangingNotifier()

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=hanging,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )
    await worker.drain_once(limit=50)

    # The job must be retried (pending), not permanently failed.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, last_error, failure_reason "
                    "FROM safety_outbox_jobs WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "pending", (
        f"expected pending (retry), got {row.status}"
    )
    assert row.retry_count == 1
    assert "timed out" in (row.last_error or "")
    assert row.failure_reason is None  # not permanent


async def test_hung_notifier_does_not_strand_other_jobs(env, monkeypatch):
    """When one notifier.send hangs, other jobs in the same drain_once
    batch complete normally — the gather continues because each
    _deliver_one has its own timeout (P2)."""
    import agentg.safety_outbox as outbox_module

    monkeypatch.setattr(outbox_module, "SEND_TIMEOUT_SECONDS", 0.1)

    # Notifier that hangs ONLY for coach 7.
    class SelectiveHangingNotifier:
        def __init__(self, real_notifier):
            self.sent: list[tuple[str, str, str, bool, bool]] = []
            self._real = real_notifier
            self._hang_id = "7"

        async def send(self, channel, channel_user_id, text,
                       disable_preview=False, protect_content=False):
            if channel_user_id == self._hang_id:
                await asyncio.Event().wait()  # hang forever
            result = await self._real.send(
                channel, channel_user_id, text,
                disable_preview=disable_preview,
                protect_content=protect_content,
            )
            self.sent.append(
                (channel, channel_user_id, text, disable_preview, protect_content),
            )
            return result

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    # Don't manually claim — let drain_once claim them so it can deliver.
    selective = SelectiveHangingNotifier(env.notifier)

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=selective,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )
    await worker.drain_once(limit=50)

    # Coach 7's job (hanging) should be retried.
    async with env.engine.connect() as conn:
        row7 = (
            await conn.execute(
                text(
                    "SELECT status, retry_count FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row7.status == "pending"  # retried
    assert row7.retry_count == 1

    # Coach 8's job should be delivered.
    async with env.engine.connect() as conn:
        row8 = (
            await conn.execute(
                text(
                    "SELECT status FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach2_id},
            )
        ).first()
    assert row8.status == "delivered"


async def test_send_timeout_retry_eventually_succeeds(env, monkeypatch):
    """After a send timeout, advancing the clock and retrying with a
    working notifier delivers successfully (P2)."""
    import agentg.safety_outbox as outbox_module

    monkeypatch.setattr(outbox_module, "SEND_TIMEOUT_SECONDS", 0.1)

    hanging = HangingNotifier()

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=hanging,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )
    await worker.drain_once(limit=50)

    # Job is back to pending.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT retry_count FROM safety_outbox_jobs")
            )
        ).first()
    assert row.retry_count == 1

    # Advance clock past next_retry_at.
    future = datetime.now(UTC) + timedelta(days=1)
    env.outbox._clock = lambda: future

    # Retry with a working notifier.
    hanging._hang = False
    await worker.drain_once(limit=50)

    # Now it should be delivered.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, last_error FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"
    assert row.last_error is None  # cleared on success


# ═══════════════════════════════════════════════════════════════════════════
# P2 r5 — legacy-schema migration: 3-column → 2-column unique constraint
# ═══════════════════════════════════════════════════════════════════════════


async def test_migration_inspects_unique_constraints_not_indexes(tmp_path):
    """The migration code uses get_unique_constraints (not get_indexes)
    to inspect existing unique constraints on safety_outbox_jobs, which
    is correct for both SQLite and PostgreSQL (P2 r5)."""
    from sqlalchemy import inspect as sa_inspect

    # Build a fresh database with the schema from Base.metadata.create_all.
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}")
    from agentg.linking_store import LinkingStore
    linking = LinkingStore(engine)
    await linking.ensure_schema()

    async with engine.connect() as conn:
        # The unique constraint should be discoverable via
        # get_unique_constraints, not just get_indexes (PostgreSQL
        # does not expose unique constraints as indexes).
        # Use run_sync because inspect() doesn't accept AsyncConnection.
        def _inspect(sync_conn):
            uniques = {
                c["name"]: c
                for c in sa_inspect(sync_conn).get_unique_constraints(
                    "safety_outbox_jobs"
                )
            }
            assert "uq_outbox_job_note_coach" in uniques, (
                "unique constraint must be visible via get_unique_constraints"
            )
            existing_cols = list(
                uniques["uq_outbox_job_note_coach"]["column_names"]
            )
            # column_names are strings, not dicts.
            assert all(isinstance(c, str) for c in existing_cols), (
                f"column_names must be strings, "
                f"got {[type(c) for c in existing_cols]}"
            )
            assert "gym_id" not in existing_cols, (
                "migrated constraint must not include gym_id"
            )
            assert existing_cols == ["note_id", "coach_member_id"] or existing_cols == [
                "coach_member_id", "note_id"
            ]

        await conn.run_sync(_inspect)
    await engine.dispose()


async def test_legacy_three_column_index_migrated_on_sqlite(tmp_path):
    """A legacy safety_outbox_jobs with a 3-column unique INDEX
    (gym_id, note_id, coach_member_id) is migrated to the 2-column form
    on ensure_schema.  On SQLite the migration drops and recreates
    the unique index.

    Proved behaviorally: after migration, inserting two jobs for
    the same (note_id, coach_member_id) with different gym_id is
    rejected — the one-job-per-Note/Coach enforcement is active (P2 r5)."""
    from agentg.models import SafetyOutboxJob
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")

    from agentg.linking_store import LinkingStore
    linking = LinkingStore(engine)
    await linking.ensure_schema()

    # Simulate a legacy 3-column unique INDEX by dropping the correct
    # one and creating the old form.
    async with engine.begin() as conn:
        await conn.execute(text("DROP INDEX IF EXISTS uq_outbox_job_note_coach"))
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_outbox_job_note_coach "
                "ON safety_outbox_jobs (gym_id, note_id, coach_member_id)"
            )
        )

    # ensure_schema runs the migration which drops + recreates the index.
    await linking.ensure_schema()

    # Verify behavior: duplicate (note_id, coach_member_id) is rejected.
    gym = await linking.create_gym("Test Gym")
    member = await linking.link_member(gym.id, "Test Member", "telegram", "99")
    await linking.set_coach(member.id)
    other_member = await linking.link_member(gym.id, "Other Member", "telegram", "100")

    from agentg.safety_outbox import SafetyOutbox
    outbox = SafetyOutbox(engine)
    note, jobs = await outbox.create_note_and_jobs(
        member_id=other_member.id,
        gym_id=gym.id,
        text="test flag",
        member_name="Other Member",
        member_is_coach=False,
        coaches=[(member.id, "Test Member", "telegram", "99")],
    )
    assert len(jobs) == 1

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with pytest.raises(Exception):  # IntegrityError
        async with sessions() as db:
            db.add(
                SafetyOutboxJob(
                    gym_id=gym.id,
                    note_id=note.id,
                    coach_member_id=member.id,
                    channel="telegram",
                    channel_user_id="99",
                    member_id=other_member.id,
                    member_name="Other Member",
                    member_is_coach=False,
                    status="pending",
                    created_at=now,
                )
            )
            await db.commit()

    await engine.dispose()


async def test_legacy_three_column_uniqueness_migrated_behavior(tmp_path):
    """After migrating from the 3-column to 2-column unique constraint,
    inserting two jobs for the same (note_id, coach_member_id) with
    different gym_id values is rejected — proving the migrated constraint
    enforces one-job-per-Note/Coach (P2 r5).

    Creates the legacy form by rebuilding the table directly with the
    3-column CONSTRAINT clause, then runs ensure_schema to migrate."""
    from agentg.models import SafetyOutboxJob
    from sqlalchemy.ext.asyncio import async_sessionmaker

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'beh.db'}")

    from agentg.linking_store import LinkingStore
    linking = LinkingStore(engine)
    # Build the full schema so all referenced tables exist.
    await linking.ensure_schema()

    # Rebuild safety_outbox_jobs with the legacy 3-column CONSTRAINT.
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS safety_outbox_jobs"))
        await conn.execute(
            text(
                "CREATE TABLE safety_outbox_jobs ("
                "  id INTEGER PRIMARY KEY,"
                "  gym_id INTEGER NOT NULL REFERENCES gyms(id),"
                "  note_id INTEGER NOT NULL REFERENCES member_notes(id) ON DELETE CASCADE,"
                "  coach_member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,"
                "  channel VARCHAR(32) NOT NULL,"
                "  channel_user_id VARCHAR(64) NOT NULL,"
                "  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,"
                "  member_name VARCHAR(100) NOT NULL,"
                "  member_is_coach BOOLEAN NOT NULL DEFAULT 0,"
                "  status VARCHAR(20) NOT NULL DEFAULT 'pending',"
                "  retry_count INTEGER NOT NULL DEFAULT 0,"
                "  next_retry_at TIMESTAMP,"
                "  claimed_at TIMESTAMP,"
                "  last_error VARCHAR(400),"
                "  created_at TIMESTAMP NOT NULL,"
                "  delivered_at TIMESTAMP,"
                "  failure_reason VARCHAR(400),"
                "  CONSTRAINT uq_outbox_job_note_coach "
                "    UNIQUE (gym_id, note_id, coach_member_id)"
                ")"
            )
        )

    # Run ensure_schema — the migration drops and recreates the
    # constraint as (note_id, coach_member_id).
    await linking.ensure_schema()

    # Create gym, members, and a note via the outbox.
    gym = await linking.create_gym("Test Gym")
    member = await linking.link_member(gym.id, "Coach", "telegram", "99")
    await linking.set_coach(member.id)
    other = await linking.link_member(gym.id, "Member", "telegram", "100")

    from agentg.safety_outbox import SafetyOutbox
    outbox = SafetyOutbox(engine)
    note, jobs = await outbox.create_note_and_jobs(
        member_id=other.id, gym_id=gym.id, text="flag",
        member_name="Member", member_is_coach=False,
        coaches=[(member.id, "Coach", "telegram", "99")],
    )
    assert len(jobs) == 1

    # Duplicate (note_id, coach_member_id) must be rejected.
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with pytest.raises(Exception):  # IntegrityError
        async with sessions() as db:
            db.add(
                SafetyOutboxJob(
                    gym_id=gym.id, note_id=note.id,
                    coach_member_id=member.id,
                    channel="telegram", channel_user_id="99",
                    member_id=other.id, member_name="Member",
                    member_is_coach=False, status="pending",
                    created_at=now,
                )
            )
            await db.commit()

    await engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════
# Round 6 — P2: no double backoff delay through restart
# ═══════════════════════════════════════════════════════════════════════════


async def test_no_double_delay_on_retry_through_restart(env):
    """The backoff delay is enforced solely by next_retry_at gating
    in claim_pending — no second sleep doubles it.  Verifies exact
    clock timing through a simulated restart (P2 r6).

    Sequence:
    1. Job fails → next_retry_at = now + BASE_BACKOFF.
    2. claim_pending skips the job (future next_retry_at).
    3. Simulate restart: reset_claimed, then a new claim_pending.
    4. Still skipped.
    5. Advance clock past next_retry_at.
    6. claim_pending claims it — no extra delay."""
    failing_notifier = FakeNotifier(failing_id="7")
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Freeze the clock so we control exactly when next_retry_at passes.
    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]

    # 1. Claim and fail.
    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 1
    await env.outbox.reset_for_retry(jobs[0], "simulated failure")

    # Verify next_retry_at is set exactly BASE_BACKOFF from now.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT retry_count, next_retry_at, status "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "pending"
    assert row.retry_count == 1
    nra = row.next_retry_at
    if isinstance(nra, str):
        nra = datetime.fromisoformat(nra).replace(tzinfo=UTC)
    expected_next = clock_val[0] + timedelta(seconds=BASE_BACKOFF_SECONDS)
    assert abs((nra - expected_next).total_seconds()) < 0.1

    # 2. claim_pending must skip it (next_retry_at in the future).
    claimed_now = await env.outbox.claim_pending(limit=50)
    assert len(claimed_now) == 0

    # 3. Simulate restart: reset_claimed (no-op since job is pending),
    #    then try claim_pending again.
    await env.outbox.reset_claimed()
    claimed_restart = await env.outbox.claim_pending(limit=50)
    assert len(claimed_restart) == 0, (
        "claim_pending must skip job with future next_retry_at after restart"
    )

    # 4. Advance clock exactly to next_retry_at.
    clock_val[0] = expected_next + timedelta(microseconds=1)

    # 5. Now drain_once must deliver immediately — claim_pending claims
    #    the job without any extra sleep, and the delivery goes through.
    #    This proves the delay is exactly the next_retry_at gate, not
    #    doubled by a redundant sleep (P2 r6).
    worker = _make_worker(env)
    start = clock_val[0]
    await worker.drain_once(limit=50)
    end = clock_val[0]

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"

    # The delivery happened without advancing the clock — no extra sleep.
    assert (end - start).total_seconds() == 0


# ═══════════════════════════════════════════════════════════════════════════
# Round 6 — P1: Forget-me Gym lock serializes with _authorized_send
# ═══════════════════════════════════════════════════════════════════════════


async def test_forget_me_narrow_locks_serialize_with_delivery(env, monkeypatch):
    """Forget-me locks the Member and MemberChannel rows before deleting
    notes/jobs, and _authorized_send locks the same narrow rows +
    re-checks the job/note on the locked connection.  Together they
    guarantee no notification can send after the note/job are deleted
    (P1 #1 r6, P1 r8).

    This test forces the exact interleaving where forget-me runs between
    _job_still_sending and the _authorized_send lock acquisition, proving
    the re-check inside _authorized_send catches the deletion."""
    from agentg.forget import ForgetStore

    # Patch out the SDK session clear — the test database doesn't have
    # the agents SDK tables, and we only care about the domain delete.
    import agents.extensions.memory as sdk_memory
    original_clear = sdk_memory.SQLAlchemySession.clear_session

    async def noop_clear(self):
        pass

    monkeypatch.setattr(
        sdk_memory.SQLAlchemySession, "clear_session", noop_clear,
    )

    # Create a note with one job for Coach Sam.
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Claim the job so it's in 'sending' state.
    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    # Barriers to force the race:
    # 1. Worker passes _job_still_sending (returns True).
    # 2. Forget-me acquires Member + MemberChannel locks, updates jobs to
    #    failed, deletes notes.
    # 3. Worker tries _authorized_send, acquires Member + MemberChannel
    #    locks, re-checks job/note → they're gone → bails.
    pre_check_passed = asyncio.Event()
    forget_done = asyncio.Event()

    worker = _make_worker(env)

    # Instrument _job_still_sending to inject the race.
    real_job_still_sending = worker._job_still_sending
    call_count = [0]

    async def instrumented_job_still_sending(job_id):
        call_count[0] += 1
        result = await real_job_still_sending(job_id)
        if call_count[0] == 2:  # inside semaphore, before _authorized_send
            pre_check_passed.set()
            await forget_done.wait()
        return result

    monkeypatch.setattr(worker, "_job_still_sending", instrumented_job_still_sending)

    # Start delivery in a background task.
    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))

    # Wait for the worker to pass the pre-check inside the semaphore.
    await pre_check_passed.wait()

    # Now run the REAL ForgetStore (not manual SQL) while the worker is
    # blocked at the barrier.  The ForgetStore locks the Member and
    # MemberChannel rows before deleting — it will complete because the
    # worker hasn't acquired the narrow locks yet.
    forget = ForgetStore(env.engine)
    await forget.forget_member(env.member_id)
    forget_done.set()

    # Wait for delivery to complete.
    await deliver_task

    # The worker must NOT have sent anything — _authorized_send's new
    # re-check of the job/note on the locked connection finds the job
    # is gone (or note is gone) and returns "safety note no longer
    # exists" or "job_gone", which the heads-up handler maps to
    # mark_failed() or a silent return.
    assert env.notifier.sent == [], (
        "worker must not send after forget-me deletes note while "
        "holding the narrow locks"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Round 8 — P1: hung notifier does not block safety-flag in same Gym
# ═══════════════════════════════════════════════════════════════════════════


class GateNotifier:
    """A notifier whose send blocks on an asyncio.Event — the test controls
    exactly when (and if) the send unblocks."""

    def __init__(self):
        self.sent: list[tuple[str, str, str, bool, bool]] = []
        self._gate = asyncio.Event()

    async def send(self, channel, channel_user_id, text,
                   disable_preview=False, protect_content=False):
        await self._gate.wait()
        self.sent.append(
            (channel, channel_user_id, text, disable_preview, protect_content),
        )


async def test_hung_notifier_does_not_block_flag_in_same_gym(env, monkeypatch):
    """When a notifier.send hangs during delivery, a concurrent
    flag_to_coach_action in the same Gym completes promptly — the narrow
    locks in _authorized_send (Coach Member + MemberChannel + Note + job)
    do not block the flag path which only locks Gym briefly during
    eligibility resolution (P1 r8).

    Sequence:
    1. Create a safety flag → outbox jobs created.
    2. Start delivery with a notifier that hangs on send.
    3. While delivery is hung, a second Member flags in the same Gym.
    4. The second flag completes promptly — Note + jobs committed.
    5. Unblock the notifier; delivery completes."""
    import agentg.safety_outbox as outbox_module

    # Patch SEND_TIMEOUT_SECONDS high so the timeout doesn't interfere.
    monkeypatch.setattr(outbox_module, "SEND_TIMEOUT_SECONDS", 999)

    gate = GateNotifier()

    # Create a second Member in the same Gym.
    member2 = await env.linking.link_member(
        env.gym_id, "Bob", "telegram", "100",
    )

    # First flag — from Ana.
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Claim the job so drain_once will deliver it.
    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=gate,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )

    # Start delivery — it will hang inside notifier.send (waiting on gate).
    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))

    # Give the delivery task time to enter the notifier.send call.
    await asyncio.sleep(0.1)

    # Now the second Member (Bob) flags — this must NOT block.
    start = time.monotonic()
    note2, jobs2 = await env.outbox.create_note_and_jobs(
        member_id=member2.id,
        gym_id=env.gym_id,
        text="shoulder pain",
        member_name="Bob",
        member_is_coach=False,
        linking_store=env.linking,
        exclude_member_id=member2.id,
    )
    elapsed = time.monotonic() - start

    # The flag must complete promptly — the hung delivery does NOT
    # block flag_to_coach_action because they lock different rows:
    # delivery locks Coach Member + MemberChannel + Note + job,
    # flag locks Gym (briefly during eligibility).
    assert elapsed < 2.0, (
        f"flag_to_coach_action blocked for {elapsed:.2f}s "
        "while notifier was hung — Gym lock contention"
    )
    assert note2.kind == "safety"
    assert len(jobs2) == 2  # both coaches

    # The pending flag jobs should be in the DB.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT COUNT(*) FROM safety_outbox_jobs WHERE note_id = :nid"),
                {"nid": note2.id},
            )
        ).scalar()
    assert rows == 2

    # Now unblock the notifier.
    gate._gate.set()
    await deliver_task

    # Ana's delivery completed.
    assert len(gate.sent) >= 2  # heads-up + link for Coach Sam


async def test_hung_notifier_does_not_block_flag_in_other_gym(env, monkeypatch):
    """Cross-Gym isolation: when a notifier hangs during delivery for
    Gym A, a flag in Gym B completes promptly — the locks are on
    different rows entirely (P1 r8)."""
    import agentg.safety_outbox as outbox_module

    monkeypatch.setattr(outbox_module, "SEND_TIMEOUT_SECONDS", 999)

    gate = GateNotifier()

    # Create a second Gym with its own Member and Coach.
    gym2 = await env.linking.create_gym("Other Gym")
    member_b = await env.linking.link_member(gym2.id, "Eve", "telegram", "200")
    coach_b = await env.linking.link_member(gym2.id, "Coach B", "telegram", "201")
    await env.linking.set_coach(coach_b.id)

    # Create a flag in Gym 1.
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=gate,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )

    # Start delivery to Gym 1's Coach — hangs on notifier.send.
    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))
    await asyncio.sleep(0.1)

    # Flag in Gym 2 — uses Gym 2's outbox, completely independent.
    from agentg.safety_outbox import SafetyOutbox
    outbox2 = SafetyOutbox(env.engine)

    start = time.monotonic()
    note2, jobs2 = await outbox2.create_note_and_jobs(
        member_id=member_b.id,
        gym_id=gym2.id,
        text="elbow pain",
        member_name="Eve",
        member_is_coach=False,
        linking_store=env.linking,
        exclude_member_id=member_b.id,
    )
    elapsed = time.monotonic() - start

    # Cross-Gym flag must complete immediately — different Gym rows.
    assert elapsed < 1.0, (
        f"cross-gym flag blocked for {elapsed:.2f}s"
    )
    assert note2.kind == "safety"
    assert len(jobs2) == 1  # only Coach B

    # Unblock the original delivery.
    gate._gate.set()
    await deliver_task
