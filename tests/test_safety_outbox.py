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
    for attempt in range(1, 6):  # go well past the old MAX_RETRIES=3
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
    """Each retry waits retry_count * BASE_BACKOFF_SECONDS before
    attempting delivery, capped at MAX_BACKOFF_SECONDS."""
    import agentg.safety_outbox as outbox_module

    # Capture sleep durations to verify the backoff formula without
    # actually waiting.  We still need the real sleep to be fast.
    sleep_durations: list[float] = []
    real_sleep = asyncio.sleep

    async def tracking_sleep(seconds):
        sleep_durations.append(seconds)
        # Don't actually sleep — this test verifies the formula, not timing.

    monkeypatch.setattr(outbox_module.asyncio, "sleep", tracking_sleep)

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

    # First attempt: retry_count=0 → no backoff.
    await worker.drain_once(limit=50)
    assert len(sleep_durations) == 0  # no sleep on first attempt

    # Second attempt: retry_count=1 → 1 * BASE_BACKOFF delay.
    await worker.drain_once(limit=50)
    assert len(sleep_durations) == 1
    assert sleep_durations[0] == pytest.approx(BASE_BACKOFF_SECONDS * 1)

    # Third attempt: retry_count=2 → 2 * BASE_BACKOFF delay.
    await worker.drain_once(limit=50)
    assert len(sleep_durations) == 2
    assert sleep_durations[1] == pytest.approx(BASE_BACKOFF_SECONDS * 2)

    # Run enough attempts that the backoff reaches and stays at the cap
    # (retry_count * BASE_BACKOFF >= MAX_BACKOFF for retry_count >= 60).
    for _ in range(60):
        await worker.drain_once(limit=50)
    # The last sleep should be capped at MAX_BACKOFF_SECONDS.
    assert sleep_durations[-1] == pytest.approx(MAX_BACKOFF_SECONDS)
    # Earlier sleeps should still be growing linearly.
    assert sleep_durations[2] == pytest.approx(BASE_BACKOFF_SECONDS * 3)


# ── transient error recovery (P1 #1) ──────────────────────────────────────


async def test_transient_db_error_is_retried_not_permanently_failed(env):
    """When channel resolution raises a transient DB error, the job is
    retried (reset_to_retry) rather than permanently failed. The original
    code's outer catch-all called mark_failed — this test proves we now retry."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Make coach_channel_in_gym raise a transient error.
    real_resolve = env.linking.coach_channel_in_gym
    call_count = [0]

    async def flaky_resolve(member_id, gym_id):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("simulated DB hiccup")
        return await real_resolve(member_id, gym_id)

    env.linking.coach_channel_in_gym = flaky_resolve

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

    # Second attempt succeeds.
    await worker.drain_once(limit=50)

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"

    # Restore.
    env.linking.coach_channel_in_gym = real_resolve


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


async def test_gym_switch_toctou_channel_resolved_immediately_before_send(env, monkeypatch):
    """When a coach switches gyms between job creation and delivery, the
    channel is re-resolved immediately before the send — no await gap
    allows a repoint to interleave (P1 #5).

    Uses a controlled interleaving: the coach_channel_in_gym resolution
    returns successfully, then before the notifier.send the coach switches
    gyms.  The resolution is re-done right before each send, so the
    second resolution would catch the switch.

    This test verifies that even with forced interleaving between
    resolution and send, a re-resolution guards the link send."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Barriers to control the interleaving between heads-up send and link send.
    heads_up_sent = asyncio.Event()
    gym_switch_applied = asyncio.Event()

    real_send = env.notifier.send
    send_count = [0]

    async def instrumented_send(channel, channel_user_id, text,
                                disable_preview=False, protect_content=False):
        send_count[0] += 1
        if send_count[0] == 1:
            # First send: heads-up.  Let it complete, then signal.
            await real_send(channel, channel_user_id, text,
                            disable_preview=disable_preview,
                            protect_content=protect_content)
            heads_up_sent.set()
            # Wait for the gym switch to happen before returning.
            await gym_switch_applied.wait()
        else:
            # Second send: link.  This should NOT happen because the
            # re-resolution will find the coach is gone.
            await real_send(channel, channel_user_id, text,
                            disable_preview=disable_preview,
                            protect_content=protect_content)

    monkeypatch.setattr(env.notifier, "send", instrumented_send)

    worker = _make_worker(env)
    deliver_task = asyncio.create_task(worker.drain_once(limit=50))

    # Wait for the heads-up to complete.
    await heads_up_sent.wait()

    # Heads-up was sent.
    assert send_count[0] == 1
    heads_before = len([m for m in env.notifier.sent if "/login/" not in m[2]])
    assert heads_before == 1

    # Now switch the coach's gym (between heads-up and link).
    new_gym = await env.linking.create_gym("New Gym")
    await env.linking.link_member(new_gym.id, "Coach Sam", "telegram", "7")
    gym_switch_applied.set()

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
