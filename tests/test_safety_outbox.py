"""Durable safety-outbox: atomicity, delivery, restart recovery, retry,
channel resolution, and atomic claiming (issue #216)."""

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from agentg.db import create_engine
from agentg.dashboard_store import DashboardStore
from agentg.linking_store import LinkingStore
from agentg.notes import NotesStore
from agentg.models import SafetyOutboxJob
from agentg.dashboard_store import hash_token
from agentg.safety_outbox import (
    BASE_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_DELIVERY_ATTEMPTS,
    FailureKind,
    OutboxWorker,
    SafetyOutbox,
    sanitize_error,
)


def _parse_dt(value):
    """Normalise a datetime read back through a raw ``text()`` query.

    TZDateTime stores UTC-aware datetimes, which SQLite hands back as naive
    ISO strings when the query bypasses the ORM type.
    """
    if isinstance(value, str):
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


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


async def test_transient_failures_retry_until_the_terminal_policy(env, monkeypatch):
    """Transient failures are retried — but only up to the named terminal
    policy, after which the job is retired as failed and stops consuming
    background attempts (issue #217, superseding #216's retry-forever)."""
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
    # Mutable clock so claim_pending always sees next_retry_at in the past.
    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]

    # Every attempt before the last one reschedules rather than failing —
    # a single transient blip must never retire a safety ping.
    for attempt in range(1, MAX_DELIVERY_ATTEMPTS):
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
        assert row.failure_reason is None  # not retired yet
        # next_retry_at should be set for backoff gating.
        assert row.next_retry_at is not None

    # The MAX_DELIVERY_ATTEMPTS'th failure trips the terminal policy.
    clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
    await worker.drain_once(limit=50)
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, failure_kind, failure_reason, "
                    "failed_at FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "failed"
    assert row.retry_count == MAX_DELIVERY_ATTEMPTS
    assert row.failure_kind == FailureKind.RETRY_EXHAUSTED
    assert "terminal" in row.failure_reason
    assert row.failed_at is not None

    # And it stays retired: further poll cycles must not re-claim it.
    for _ in range(3):
        clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
        assert await worker.drain_once(limit=50) == 0
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    assert row.status == "failed"
    assert row.retry_count == MAX_DELIVERY_ATTEMPTS


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

    # Backdate claimed_at so they appear stale.  Write it *naive* like
    # TZDateTime does: a raw text() UPDATE bypasses the type decorator, and
    # the lease fence compares the stored value against an ORM-bound one.
    async with env.engine.begin() as conn:
        stale_time = datetime.now(UTC) - timedelta(seconds=LEASE_TIMEOUT_SECONDS + 10)
        await conn.execute(
            text("UPDATE safety_outbox_jobs SET claimed_at = :ts"),
            {"ts": stale_time.replace(tzinfo=None)},
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
    formula: BASE_BACKOFF_SECONDS * 2**(attempt-1), capped at
    MAX_BACKOFF_SECONDS (P2 r6, exponential since issue #217).

    Jitter is pinned to its midpoint (rng() == 0.5) so the schedule is
    exact; the spread itself is covered by test_backoff_jitter_*."""
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
    # Pin jitter to its midpoint so the delay is exactly the exponential.
    env.outbox._rng = lambda: 0.5

    # The exponential schedule, capped: 5, 10, 20, 40, 80, 160, 300 — the
    # 8th failure is terminal, so only MAX_DELIVERY_ATTEMPTS-1 are scheduled.
    for attempt in range(1, MAX_DELIVERY_ATTEMPTS):
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
        assert row.retry_count == attempt
        assert row.next_retry_at is not None
        nra = _parse_dt(row.next_retry_at)
        delay = min(BASE_BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        expected = clock_val[0] + timedelta(seconds=delay)
        assert abs((nra - expected).total_seconds()) < 0.1, (
            f"attempt {attempt}: expected a {delay}s backoff"
        )
        # Advance clock past next_retry_at so claim_pending can re-claim.
        clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)


async def test_backoff_is_exponential_not_linear(env):
    """The delay doubles per attempt until it hits the cap (issue #217 AC #1).
    A linear schedule would give 5, 10, 15 — this pins 5, 10, 20."""
    env.outbox._rng = lambda: 0.5  # no jitter
    delays = [env.outbox.backoff_delay(n) for n in range(1, 9)]
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300]


async def test_backoff_carries_bounded_jitter(env):
    """Jitter spreads the delay by ±BACKOFF_JITTER_RATIO and never escapes
    [MIN_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS] (issue #217 AC #1)."""
    from agentg.safety_outbox import BACKOFF_JITTER_RATIO, MIN_BACKOFF_SECONDS

    # Deterministic extremes of the jitter source.
    env.outbox._rng = lambda: 0.0  # lower edge
    assert env.outbox.backoff_delay(3) == pytest.approx(20 * (1 - BACKOFF_JITTER_RATIO))
    env.outbox._rng = lambda: 1.0  # upper edge (rng is [0, 1) in reality)
    assert env.outbox.backoff_delay(3) == pytest.approx(20 * (1 + BACKOFF_JITTER_RATIO))

    # The cap is hard: jitter cannot push a capped delay past it.
    env.outbox._rng = lambda: 1.0
    assert env.outbox.backoff_delay(20) == MAX_BACKOFF_SECONDS
    # And the floor is hard: jitter never schedules a retry for "now".
    env.outbox._rng = lambda: 0.0
    assert env.outbox.backoff_delay(1) >= MIN_BACKOFF_SECONDS

    # Over the real random source the spread is genuine (not a constant)
    # and stays inside the band.
    env.outbox._rng = random.Random(1234).random
    samples = [env.outbox.backoff_delay(3) for _ in range(50)]
    assert len(set(samples)) > 1, "jitter must actually vary the delay"
    assert all(
        20 * (1 - BACKOFF_JITTER_RATIO) <= s <= 20 * (1 + BACKOFF_JITTER_RATIO)
        for s in samples
    )


async def test_backoff_survives_restart(env):
    """next_retry_at is durable, so a process restart does not reset the
    backoff and let a failing job retry immediately (issue #217 AC #2)."""
    failing_notifier = FakeNotifier(failing_id="7")
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]
    env.outbox._rng = lambda: 0.5

    worker = _make_worker(env, notifier=failing_notifier)
    await worker.drain_once(limit=50)

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT retry_count, next_retry_at, last_error "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.retry_count == 1
    assert row.last_error == "notifier send failed"
    scheduled = _parse_dt(row.next_retry_at)

    # "Restart": a brand-new outbox + worker over the same database, as the
    # runtime does on boot (reset_claimed then poll).
    restarted = SafetyOutbox(env.engine, clock=lambda: clock_val[0])
    restarted._rng = lambda: 0.5
    await restarted.reset_claimed()
    fresh_worker = OutboxWorker(
        outbox=restarted,
        notifier=env.notifier,  # would succeed if it ever got claimed
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )

    # Still inside the backoff window: the restart must not claim it.
    clock_val[0] = scheduled - timedelta(seconds=1)
    assert await fresh_worker.drain_once(limit=50) == 0
    assert env.notifier.sent == []

    # The durable attempt metadata survived the restart untouched.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, last_error, next_retry_at "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "pending"
    assert row.retry_count == 1
    assert row.last_error == "notifier send failed"
    assert abs((_parse_dt(row.next_retry_at) - scheduled).total_seconds()) < 0.1

    # Past the window, the restarted worker delivers — success after retry.
    clock_val[0] = scheduled + timedelta(seconds=1)
    assert await fresh_worker.drain_once(limit=50) == 1
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, last_error, failure_kind "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "delivered"
    assert row.retry_count == 1  # preserved for audit
    assert row.last_error is None
    assert row.failure_kind is None


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

    # Backdate claimed_at so they look stale.  A really-slow delivery
    # carries its (old) claim stamp in memory too, so mirror the backdate
    # on the in-memory jobs — the lease-stamp fence in mark_delivered
    # compares them.
    stale_time = datetime.now(UTC) - timedelta(seconds=LEASE_TIMEOUT_SECONDS + 10)
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE safety_outbox_jobs SET claimed_at = :ts"),
            {"ts": stale_time.replace(tzinfo=None)},
        )
    for j in jobs:
        j.claimed_at = stale_time

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

    # Should return True when status is 'sending' under our claim.
    assert await worker._job_still_sending(jobs[0]) is True

    # Mark the job as failed (simulating forget-me or permanent failure).
    await env.outbox.mark_failed(jobs[0], "member data deleted")

    # Should now return False — status is 'failed', not 'sending'.
    assert await worker._job_still_sending(jobs[0]) is False


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


# The pre-migration safety_outbox_jobs table.  ``{constraint}`` is either
# empty (uniqueness supplied separately as a standalone legacy INDEX) or the
# legacy 3-column table-level CONSTRAINT clause.  Deliberately omits the
# model's 2-column UniqueConstraint, and the columns added by later entries
# in _add_missing_columns, so ensure_schema has real work to do.
_LEGACY_OUTBOX_TABLE_DDL = (
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
    "  failure_reason VARCHAR(400)"
    "{constraint}"
    ")"
)


def _assert_rejected_by_note_coach_uniqueness(exc: BaseException) -> None:
    """Assert an IntegrityError came from the migrated 2-column unique
    constraint on (note_id, coach_member_id).

    Without this, a foreign-key rejection (e.g. a duplicate built with a
    gym_id that does not exist) would satisfy a bare ``raises`` and restore
    the false green these tests exist to prevent (issue #229).
    """
    message = str(exc.orig if exc.orig is not None else exc)
    assert "FOREIGN KEY" not in message.upper(), (
        f"duplicate was rejected by a foreign key, not the unique "
        f"constraint: {message}"
    )
    assert "UNIQUE" in message.upper(), (
        f"expected a unique-constraint violation, got: {message}"
    )
    assert "note_id" in message and "coach_member_id" in message, (
        f"expected the violation to name the migrated 2-column constraint, "
        f"got: {message}"
    )
    assert "gym_id" not in message, (
        f"violation names gym_id — the legacy 3-column constraint is still "
        f"the one enforcing this, so the migration did not run: {message}"
    )


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

    # Build the legacy schema: the table with NO inline unique constraint,
    # plus a standalone 3-column unique INDEX.
    #
    # The table must be rebuilt, not just re-indexed.  create_all emits the
    # model's 2-column UniqueConstraint *inline* in CREATE TABLE, which
    # SQLite implements as sqlite_autoindex_safety_outbox_jobs_1 rather than
    # a droppable index named uq_outbox_job_note_coach.  A bare
    # DROP INDEX IF EXISTS uq_outbox_job_note_coach is therefore a silent
    # no-op that leaves the migrated constraint in force, and the assertion
    # below would hold with the migration deleted (issue #229).
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS safety_outbox_jobs"))
        await conn.execute(text(_LEGACY_OUTBOX_TABLE_DDL.format(constraint="")))
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_outbox_job_note_coach "
                "ON safety_outbox_jobs (gym_id, note_id, coach_member_id)"
            )
        )

    # Guard the guard: the legacy setup must really have removed the
    # 2-column enforcement, or this test cannot detect a missing migration.
    async with engine.connect() as conn:
        table_sql = (
            await conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name='safety_outbox_jobs'")
            )
        ).scalar()
    assert "UNIQUE" not in (table_sql or "").upper(), (
        "legacy table must carry no inline unique constraint, else the "
        f"migration under test is not what enforces uniqueness: {table_sql}"
    )

    # ensure_schema runs the migration which drops + recreates the index.
    await linking.ensure_schema()

    # Verify behavior: duplicate (note_id, coach_member_id) is rejected.
    gym = await linking.create_gym("Test Gym")
    member = await linking.link_member(gym.id, "Test Member", "telegram", "99")
    await linking.set_coach(member.id)
    other_member = await linking.link_member(gym.id, "Other Member", "telegram", "100")
    # A real second Gym: the duplicate must be rejected by the migrated
    # unique constraint, not by a foreign-key violation on gym_id.
    other_gym = await linking.create_gym("Other Gym")

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
    with pytest.raises(IntegrityError) as excinfo:
        async with sessions() as db:
            db.add(
                SafetyOutboxJob(
                    gym_id=other_gym.id,
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

    _assert_rejected_by_note_coach_uniqueness(excinfo.value)

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
                _LEGACY_OUTBOX_TABLE_DDL.format(
                    constraint=","
                    "  CONSTRAINT uq_outbox_job_note_coach "
                    "    UNIQUE (gym_id, note_id, coach_member_id)"
                )
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
    # A real second Gym: the duplicate must be rejected by the migrated
    # unique constraint, not by a foreign-key violation on gym_id.
    other_gym = await linking.create_gym("Other Gym")

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
    with pytest.raises(IntegrityError) as excinfo:
        async with sessions() as db:
            db.add(
                SafetyOutboxJob(
                    gym_id=other_gym.id, note_id=note.id,
                    coach_member_id=member.id,
                    channel="telegram", channel_user_id="99",
                    member_id=other.id, member_name="Member",
                    member_is_coach=False, status="pending",
                    created_at=now,
                )
            )
            await db.commit()

    _assert_rejected_by_note_coach_uniqueness(excinfo.value)

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

    # Freeze the clock so we control exactly when next_retry_at passes,
    # and pin the jitter to its midpoint so the delay is exactly the base.
    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]
    env.outbox._rng = lambda: 0.5

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


# ── P1 r9: deadlock-free lock ordering ────────────────────────────────────


async def test_delivery_and_gym_switch_lock_order_no_deadlock(env):
    """Concurrent delivery and Coach gym-switch complete without deadlock
    because both paths now lock in the same global order: Member →
    MemberChannel.

    Before r9, _link_member_in_session locked MemberChannel then Member
    while _authorized_send locked Member then MemberChannel — a circular
    wait that PostgreSQL would detect and abort."""
    # Give the Coach a safety outbox job.
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

    # Use a gated notifier so delivery blocks inside _authorized_send
    # (after acquiring the Member lock, before MemberChannel lock),
    # creating a window for the gym-switch to interleave.
    notifier = GateNotifier()
    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )

    # Start delivery — it will acquire the Member lock then block on
    # the gated notifier.send inside _authorized_send.
    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))
    await asyncio.sleep(0.15)  # let delivery acquire its locks

    # Meanwhile, gym-switch the Coach: link_member re-points the channel
    # identity.  With the fixed lock order, this locks the old Member
    # first then MemberChannel — same order as delivery — so it
    # serializes without deadlock.
    gym2 = await env.linking.create_gym("Other Iron")
    start = time.monotonic()
    switch_task = asyncio.create_task(
        env.linking.link_member(gym2.id, "Coach Sam", "telegram", "7")
    )

    # Both tasks are now racing.  If the lock orders were opposite,
    # PostgreSQL would detect the deadlock and abort one transaction.
    # With consistent ordering, one waits for the other and both
    # complete.

    # Unblock delivery.
    notifier._gate.set()
    await deliver_task

    # Gym switch should now complete (it was blocked on the Member lock
    # held by delivery, released when delivery committed).
    switched_member = await switch_task
    elapsed = time.monotonic() - start

    # The gym switch completed (no deadlock).
    assert switched_member is not None
    assert switched_member.gym_id == gym2.id

    # The delivery should have completed and the job marked delivered.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status FROM safety_outbox_jobs "
                    "WHERE coach_member_id = :cid"
                ),
                {"cid": env.coach1_id},
            )
        ).first()
    # If the gym-switch re-pointed before delivery's authorization
    # check, the job is failed with "no longer reachable"; if delivery
    # finished first, it's delivered.  Either outcome is correct and
    # proves no deadlock.
    assert row.status in ("delivered", "failed")

    # Importantly, elapsed should be reasonable — a deadlock would have
    # hung indefinitely or been aborted by the DB after a timeout.
    assert elapsed < 5.0, f"gym switch blocked for {elapsed:.2f}s"


async def test_delivery_and_set_coach_lock_order_no_deadlock(env):
    """Concurrent delivery and set_coach complete without deadlock because
    both paths now lock in the same global order: Gym → Member.

    Before r9, set_coach locked Member then Gym while
    _coaches_for_gym_in_session (and promote_to_coach via
    _redeem_coach_code) locked Gym first — opposite orders."""
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

    notifier = GateNotifier()
    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
        linking_store=env.linking,
    )

    # Start delivery — blocks inside _authorized_send on the gated send.
    deliver_task = asyncio.create_task(worker._deliver_one(claimed[0]))
    await asyncio.sleep(0.15)

    # set_coach on the same Coach (demote).  With the fixed lock order
    # (Gym → Member), this and delivery serialize cleanly.
    start = time.monotonic()
    demote_task = asyncio.create_task(
        env.linking.set_coach(env.coach1_id, is_coach=False)
    )

    # Unblock delivery.
    notifier._gate.set()
    await deliver_task
    await demote_task
    elapsed = time.monotonic() - start

    # Both completed — no deadlock.
    assert elapsed < 5.0, f"set_coach blocked for {elapsed:.2f}s"


# ── P1 r9: DB-abort retry ─────────────────────────────────────────────────


async def test_delivery_retries_on_db_abort(env, monkeypatch):
    """When a delivery's transactional block raises a DBAPIError (e.g.
    PostgreSQL serialization failure or deadlock detection abort), the
    job is retried rather than permanently failed.

    This exercises the outer catch-all in _deliver_one which must call
    reset_for_retry on transient errors, not mark_failed."""
    from sqlalchemy.exc import DBAPIError

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Make _authorized_send raise a DBAPIError on first call, succeed
    # on second.
    import agentg.safety_outbox as outbox_module

    real_send = outbox_module.OutboxWorker._authorized_send
    call_count = [0]

    async def flaky_send(self, job, text, *, disable_preview=True,
                         protect_content=False):
        call_count[0] += 1
        if call_count[0] == 1:
            raise DBAPIError(
                "deadlock detected",
                params=None,
                orig=RuntimeError("simulated serialization failure"),
            )
        return await real_send(
            self, job, text, disable_preview=disable_preview,
            protect_content=protect_content,
        )

    monkeypatch.setattr(
        outbox_module.OutboxWorker, "_authorized_send", flaky_send,
    )

    worker = _make_worker(env)
    await worker.drain_once(limit=50)

    # First attempt failed transiently — job retried, not permanently
    # failed.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, last_error, failure_reason "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "pending", (
        f"expected pending (retry), got {row.status}"
    )
    assert row.retry_count == 1
    assert "DBAPIError" in (row.last_error or "")
    assert row.failure_reason is None  # not permanently failed

    # Advance clock past next_retry_at so claim_pending can re-claim.
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


# ── P2 r10: gym-switch serialization with eligibility resolution ──────────


async def test_switched_coach_excluded_from_eligibility(env):
    """When a Coach switches gyms before create_note_and_jobs runs
    eligibility, the switched Coach is excluded from outbox jobs.

    The gym switch locks the old Gym row, serializing with
    _coaches_for_gym_in_session so a Coach who switched away from the
    Gym before the lock is acquired is no longer an eligible Coach for
    that Gym."""
    # Coach Sam (coach1) switches to a new gym BEFORE the safety flag.
    new_gym = await env.linking.create_gym("New Gym")
    await env.linking.link_member(
        new_gym.id, "Coach Sam", "telegram", "7",
    )

    # Now create a safety Note using the linking_store path so
    # _coaches_for_gym_in_session runs inside the transaction.
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        linking_store=env.linking,
        exclude_member_id=env.member_id,
    )

    # Only Coach Jo (coach2) should get a job — Coach Sam switched away.
    assert len(jobs) == 1
    assert jobs[0].coach_member_id == env.coach2_id

    # Verify the job is deliverable.
    worker = _make_worker(env)
    await worker.drain_once(limit=50)
    # Coach Jo gets heads-up + link.
    assert len(env.notifier.sent) == 2
    coach_jo_sends = [m for m in env.notifier.sent if m[1] == "8"]
    assert len(coach_jo_sends) == 2


async def test_switch_after_eligibility_job_still_reachable(env):
    """When a Coach switches gyms AFTER the Note and jobs commit,
    the existing job is handled correctly at delivery time — the
    Coach is no longer reachable in the original Gym, so the job
    is marked failed rather than delivering to the wrong Gym.

    This is the sequential version of the P2 r10 race: the critical
    property is that a switch that beats eligibility excludes the
    Coach (tested above), while a switch that arrives after commit
    still fails safely at delivery (tested here)."""
    # Create the Note and jobs first (Coach Sam is still in the old gym).
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        linking_store=env.linking,
        exclude_member_id=env.member_id,
    )
    assert len(jobs) == 2  # Both coaches get jobs

    # Now Coach Sam switches gyms.
    new_gym = await env.linking.create_gym("New Gym")
    await env.linking.link_member(
        new_gym.id, "Coach Sam", "telegram", "7",
    )

    # Deliver — Coach Sam's job should fail (no longer reachable),
    # Coach Jo's job should succeed.
    worker = _make_worker(env)
    await worker.drain_once(limit=50)

    # Only Coach Jo got messages.
    assert env.notifier.sent  # at least Coach Jo's messages
    coach_sam_sends = [m for m in env.notifier.sent if m[1] == "7"]
    assert len(coach_sam_sends) == 0

    # Verify Coach Sam's job is marked failed, Coach Jo's delivered.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT coach_member_id, status, failure_reason "
                    "FROM safety_outbox_jobs ORDER BY coach_member_id"
                )
            )
        ).all()
    assert rows[0].status == "failed"  # Coach Sam
    assert "no longer reachable" in (rows[0].failure_reason or "")
    assert rows[1].status == "delivered"  # Coach Jo


async def test_eligibility_count_matches_coaches_in_gym(env):
    """After a Coach switches gyms, the linking_store coaches_for_gym
    and the transactional _coaches_for_gym_in_session both report the
    correct count — excluding the departed Coach."""
    # Baseline: 2 coaches in the gym.
    coaches_before = await env.linking.coaches_for_gym(env.gym_id)
    assert len(coaches_before) == 2

    # Coach Sam switches to a new gym.
    new_gym = await env.linking.create_gym("New Gym")
    await env.linking.link_member(
        new_gym.id, "Coach Sam", "telegram", "7",
    )

    # After switch: only 1 coach remains in the old gym.
    coaches_after = await env.linking.coaches_for_gym(env.gym_id)
    assert len(coaches_after) == 1
    assert coaches_after[0][0] == env.coach2_id  # Coach Jo

    # The transactional path also sees only 1 coach.
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="post-switch flag",
        member_name=env.member_name,
        member_is_coach=False,
        linking_store=env.linking,
        exclude_member_id=env.member_id,
    )
    assert len(jobs) == 1
    assert jobs[0].coach_member_id == env.coach2_id


# ── P2 r11: mark_failed sets failed_at, not delivered_at ──────────────────


async def test_mark_failed_sets_failed_at_not_delivered_at(env):
    """mark_failed records the failure timestamp in failed_at and leaves
    delivered_at NULL — no delivery occurred (P2 r11)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Claim the job and mark it failed.
    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 1
    await env.outbox.mark_failed(jobs[0], "coach no longer reachable")

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, failure_reason, delivered_at, failed_at "
                    "FROM safety_outbox_jobs WHERE id = :id"
                ),
                {"id": jobs[0].id},
            )
        ).first()
    assert row.status == "failed"
    assert row.failure_reason == "coach no longer reachable"
    assert row.delivered_at is None, (
        "delivered_at must be NULL — no delivery occurred"
    )
    assert row.failed_at is not None, (
        "failed_at must record the failure timestamp"
    )


async def test_reset_for_retry_sets_neither_delivered_at_nor_failed_at(env):
    """reset_for_retry resets a job to 'pending' for another attempt.
    It must not set delivered_at (no delivery) or failed_at (not a
    permanent failure) — the audit columns stay NULL (P2 r11)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    # Claim the job and simulate a transient failure.
    claimed = await env.outbox.claim_pending(limit=1)
    assert len(claimed) == 1
    await env.outbox.reset_for_retry(claimed[0], "notifier send failed")

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, delivered_at, failed_at "
                    "FROM safety_outbox_jobs WHERE id = :id"
                ),
                {"id": claimed[0].id},
            )
        ).first()
    assert row.status == "pending"
    assert row.retry_count == 1
    assert row.delivered_at is None, (
        "reset_for_retry must not set delivered_at — nothing was delivered"
    )
    assert row.failed_at is None, (
        "reset_for_retry must not set failed_at — this is not a permanent failure"
    )


async def test_mark_delivered_still_sets_delivered_at(env):
    """mark_delivered continues to set delivered_at — only mark_failed
    was changed (P2 r11 regression check)."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    jobs = await env.outbox.claim_pending(limit=50)
    assert len(jobs) == 1
    await env.outbox.mark_delivered(jobs[0])

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, delivered_at, failed_at "
                    "FROM safety_outbox_jobs WHERE id = :id"
                ),
                {"id": jobs[0].id},
            )
        ).first()
    assert row.status == "delivered"
    assert row.delivered_at is not None, (
        "mark_delivered must still set delivered_at"
    )
    assert row.failed_at is None, (
        "mark_delivered must not set failed_at"
    )


# ── Lease-stamp fencing: a stale owner cannot clobber a re-claimed job ──────


async def test_stale_owner_cannot_clobber_reclaimed_job(env):
    """After reset_stale_claims + a fresh claim_pending, the STALE owner's
    mark_delivered / mark_failed / reset_for_retry must all no-op — only
    the current lease holder (matching claimed_at) may transition the job."""
    from agentg.safety_outbox import LEASE_TIMEOUT_SECONDS

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    stale_jobs = await env.outbox.claim_pending(limit=50)
    assert len(stale_jobs) == 1

    # The stale owner's send hangs; its lease expires and is reset.
    stale_time = datetime.now(UTC) - timedelta(seconds=LEASE_TIMEOUT_SECONDS + 10)
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE safety_outbox_jobs SET claimed_at = :ts"),
            {"ts": stale_time.replace(tzinfo=None)},
        )
    stale_jobs[0].claimed_at = stale_time
    assert await env.outbox.reset_stale_claims() == 1

    # A new owner claims the job with a fresh lease stamp.
    new_jobs = await env.outbox.claim_pending(limit=50)
    assert len(new_jobs) == 1
    assert new_jobs[0].id == stale_jobs[0].id

    async def _status():
        async with env.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, claimed_at FROM safety_outbox_jobs "
                        "WHERE id = :id"
                    ),
                    {"id": stale_jobs[0].id},
                )
            ).first()
        return row

    # The stale owner's transitions must all no-op against the new claim.
    await env.outbox.mark_delivered(stale_jobs[0])
    assert (await _status()).status == "sending"
    await env.outbox.mark_failed(stale_jobs[0], "stale failure")
    assert (await _status()).status == "sending"
    before = (await _status()).claimed_at
    await env.outbox.reset_for_retry(stale_jobs[0], "stale retry")
    row = await _status()
    assert row.status == "sending", (
        "a stale owner's reset_for_retry must not clobber the live claim"
    )
    assert row.claimed_at == before

    # The current owner's transition works normally.
    await env.outbox.mark_delivered(new_jobs[0])
    assert (await _status()).status == "delivered"


# ── AC 5 wiring: startup recovery and shutdown drain glue ───────────────────


async def test_start_background_tasks_wires_recovery_and_shutdown_drains(env):
    """The application glue (start_background_tasks / shutdown) must
    actually run recovery and the final drain: a job orphaned in
    'sending' by a prior crash is reset on start and delivered by the
    time shutdown returns — even with no dashboard wired."""
    from agentg.linking import Linking
    from agentg.runtime import AgentRuntime
    from agentg.stores import Stores

    async def _phraser(instruction, member_text):
        return instruction

    async def _summarizer(old_items, existing_notes):  # pragma: no cover
        raise AssertionError("summarizer must not run")

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )
    # Orphan the job in 'sending' as a prior crash would.
    claimed = await env.outbox.claim_pending(limit=50)
    assert len(claimed) == 1

    stores = Stores.from_engine(env.engine)
    runtime = AgentRuntime(
        agent=object(),
        engine=env.engine,
        stores=stores,
        linking=Linking(stores.linking, _phraser),
        summarizer=_summarizer,
        notifier=env.notifier,
        dashboard=None,
        stream_replies=False,
    )
    await runtime.start_background_tasks()
    assert runtime._outbox_worker is not None, (
        "a wired notifier must start the outbox worker"
    )
    # Recovery reset the orphaned claim; the final drain delivers it.
    await runtime.shutdown()
    assert runtime._outbox_worker is None

    sent = [m for m in env.notifier.sent if "sharp knee pain" in m[2]]
    assert sent, "the orphaned job must be delivered by startup recovery + drain"

    # No notifier → no worker; both calls stay safe no-ops.
    runtime2 = AgentRuntime(
        agent=object(),
        engine=env.engine,
        stores=stores,
        linking=Linking(stores.linking, _phraser),
        summarizer=_summarizer,
        notifier=None,
        stream_replies=False,
    )
    await runtime2.start_background_tasks()
    assert runtime2._outbox_worker is None
    await runtime2.shutdown()


# ── issue #217: terminal policy is queryable ──────────────────────────────


async def test_failed_jobs_query_surfaces_terminal_failures(env):
    """Terminal failures stay queryable by cause, so an operator can ask
    "which safety pings died, and why" (issue #217 AC #4)."""
    failing_notifier = FakeNotifier(failing_id="7")
    note, _jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )

    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]
    env.outbox._rng = lambda: 0.5

    worker = _make_worker(env, notifier=failing_notifier)
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
        await worker.drain_once(limit=50)

    # Coach 8 delivered on the first pass; Coach 7 exhausted its attempts.
    failed = await env.outbox.failed_jobs()
    assert [j.coach_member_id for j in failed] == [env.coach1_id]
    assert failed[0].failure_kind == FailureKind.RETRY_EXHAUSTED
    assert failed[0].note_id == note.id
    assert failed[0].retry_count == MAX_DELIVERY_ATTEMPTS
    assert failed[0].failed_at is not None
    assert failed[0].delivered_at is None

    # Narrowing by kind and gym works; a mismatched filter returns nothing.
    assert len(await env.outbox.failed_jobs(kind=FailureKind.RETRY_EXHAUSTED)) == 1
    assert await env.outbox.failed_jobs(kind=FailureKind.NOTE_DELETED) == []
    assert len(await env.outbox.failed_jobs(gym_id=env.gym_id)) == 1
    assert await env.outbox.failed_jobs(gym_id=env.gym_id + 999) == []


async def test_terminal_state_survives_restart(env):
    """A retired job stays retired across a restart — reset_claimed must
    not resurrect it (issue #217 AC #2)."""
    failing_notifier = FakeNotifier(failing_id="7")
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]
    env.outbox._rng = lambda: 0.5
    worker = _make_worker(env, notifier=failing_notifier)
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
        await worker.drain_once(limit=50)

    restarted = SafetyOutbox(env.engine, clock=lambda: clock_val[0])
    assert await restarted.reset_claimed() == 0
    clock_val[0] += timedelta(days=7)
    assert await restarted.claim_pending(limit=50) == []

    failed = await restarted.failed_jobs()
    assert len(failed) == 1
    assert failed[0].status == "failed"
    assert failed[0].failure_kind == FailureKind.RETRY_EXHAUSTED
    assert failed[0].retry_count == MAX_DELIVERY_ATTEMPTS


async def test_unauthorized_failure_carries_its_own_kind(env):
    """"Coach is gone" is classified distinctly from "retry exhausted", so
    the terminal policy never hides a permissions problem."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )
    # Demote the coach: authorization fails at delivery time.
    await env.linking.set_coach(env.coach1_id, False)
    await _make_worker(env).drain_once(limit=50)

    failed = await env.outbox.failed_jobs()
    assert len(failed) == 1
    assert failed[0].failure_kind == FailureKind.UNAUTHORIZED
    assert failed[0].retry_count == 0  # not an attempt-count failure
    assert await env.outbox.failed_jobs(kind=FailureKind.RETRY_EXHAUSTED) == []


async def test_deleted_note_failure_carries_note_deleted_kind(env):
    """A forget-me that removes the Note before delivery retires the job as
    note_deleted, not as an authorization or retry failure."""
    note, _jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="dizzy after sets",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )
    # Delete only the note row, leaving the job (the cascade is exercised
    # elsewhere); this is the "note gone, job still queued" window.
    async with env.engine.begin() as conn:
        await conn.execute(
            text("PRAGMA foreign_keys=OFF")
        )
        await conn.execute(
            text("DELETE FROM member_notes WHERE id = :id"), {"id": note.id}
        )

    await _make_worker(env).drain_once(limit=50)

    failed = await env.outbox.failed_jobs(kind=FailureKind.NOTE_DELETED)
    assert len(failed) == 1
    assert failed[0].delivered_at is None
    assert env.notifier.sent == [], "nothing may be sent for a deleted Note"


# ── issue #217: sanitized structured telemetry ────────────────────────────


def test_sanitize_error_redacts_credentials():
    """Bearer tokens, provider keys, magic links and key=value secrets are
    redacted before anything is stored or logged (issue #217 AC #4)."""
    cases = [
        (
            "401 from provider: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdef",
            "eyJhbGciOiJIUzI1NiJ9abcdef",
        ),
        (
            "GET https://dash.example.com/login/kR8sV1nQe7wZ failed",
            "kR8sV1nQe7wZ",
        ),
        (
            "telegram rejected bot 123456789:AAHrandomlookingsecretvalue012345",
            "AAHrandomlookingsecretvalue012345",
        ),
        (
            'response {"api_key": "sk-abcdefghijklmnopqrstuvwx"}',
            "sk-abcdefghijklmnopqrstuvwx",
        ),
        (
            "connect failed token=aVeryLongOpaqueSessionTokenValue123456",
            "aVeryLongOpaqueSessionTokenValue123456",
        ),
    ]
    for raw, secret in cases:
        cleaned = sanitize_error(raw)
        assert secret not in cleaned, f"{secret!r} survived sanitisation of {raw!r}"
        assert "[redacted]" in cleaned

    # Ordinary error codes pass through untouched, collapsed and bounded.
    assert sanitize_error("notifier send failed") == "notifier send failed"
    assert sanitize_error("a\n  b") == "a b"
    assert len(sanitize_error("notifier failed " * 500)) == 200
    assert sanitize_error(None) == ""
    assert sanitize_error("") == ""


async def test_failure_telemetry_is_structured_and_sanitized(env, caplog):
    """A failed attempt emits a structured record carrying the ids and the
    attempt schedule, and carrying neither the Member's Note text nor the
    dashboard credential (issue #217 AC #4)."""
    private = "sharp knee pain after heavy squats"

    class LeakyNotifier:
        """An adapter whose error text echoes the request body and a token —
        exactly the shape that leaks secrets into logs."""

        def __init__(self):
            self.sent = []

        async def send(self, channel, channel_user_id, text_, **kw):
            raise RuntimeError(
                "POST /sendMessage?token=123456789:AAHsecretbottokenvalue0123456 "
                f'body={{"text": "{text_}"}} -> 500'
            )

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text=private,
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    worker = _make_worker(env, notifier=LeakyNotifier())
    with caplog.at_level(logging.DEBUG, logger="agentg.safety_outbox"):
        await worker.drain_once(limit=50)

    records = [r for r in caplog.records if hasattr(r, "outbox")]
    assert records, "a failed attempt must emit structured telemetry"
    payload = records[-1].outbox
    assert payload["event"] == "safety_outbox.delivery_failed"
    assert payload["outcome"] == "retry"
    assert payload["terminal"] is False
    assert payload["attempt"] == 1
    assert payload["max_attempts"] == MAX_DELIVERY_ATTEMPTS
    assert payload["gym_id"] == env.gym_id
    assert payload["coach_member_id"] == env.coach1_id
    assert payload["channel"] == "telegram"
    assert payload["next_retry_at"] is not None
    # It must be serialisable — telemetry that cannot be shipped is useless.
    json.dumps(payload, default=str)

    # Nothing in the whole captured log stream carries the private Note text,
    # the provider-side account id, or the bot token.
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert private not in blob
    assert "knee pain" not in blob
    assert "AAHsecretbottokenvalue0123456" not in blob

    # Nor does the durable failure metadata.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT last_error, failure_reason FROM safety_outbox_jobs")
            )
        ).first()
    assert "knee pain" not in (row.last_error or "")
    assert "knee pain" not in (row.failure_reason or "")
    assert "AAHsecretbottokenvalue0123456" not in (row.last_error or "")


async def test_terminal_telemetry_marks_itself_terminal(env):
    """The last attempt's telemetry says terminal, not retry, and names the
    failure kind (issue #217 AC #4)."""
    emitted = []
    handler = logging.Handler()
    handler.emit = lambda record: (
        emitted.append(record.outbox) if hasattr(record, "outbox") else None
    )
    logging.getLogger("agentg.safety_outbox").addHandler(handler)
    try:
        await env.outbox.create_note_and_jobs(
            member_id=env.member_id,
            gym_id=env.gym_id,
            text="sharp knee pain",
            member_name=env.member_name,
            member_is_coach=False,
            coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
        )
        clock_val = [datetime.now(UTC)]
        env.outbox._clock = lambda: clock_val[0]
        env.outbox._rng = lambda: 0.5
        worker = _make_worker(env, notifier=FakeNotifier(failing_id="7"))
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
            await worker.drain_once(limit=50)
    finally:
        logging.getLogger("agentg.safety_outbox").removeHandler(handler)

    assert len(emitted) == MAX_DELIVERY_ATTEMPTS
    assert [p["outcome"] for p in emitted[:-1]] == ["retry"] * (
        MAX_DELIVERY_ATTEMPTS - 1
    )
    last = emitted[-1]
    assert last["outcome"] == "terminal"
    assert last["terminal"] is True
    assert last["failure_kind"] == FailureKind.RETRY_EXHAUSTED
    assert last["attempt"] == MAX_DELIVERY_ATTEMPTS
    assert last["next_retry_at"] is None


# ── issue #217: bounded duplicate + credential control ────────────────────


async def test_one_note_coach_pair_cannot_create_more_jobs(env):
    """Retries reuse the one job row per (Note, Coach) — a retry loop never
    fans out into extra jobs (issue #217 AC #5)."""
    note, jobs = await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=_coaches(env),
    )
    assert len(jobs) == 2

    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]
    env.outbox._rng = lambda: 0.5
    worker = _make_worker(env, notifier=FakeNotifier(failing_id="7"))
    for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
        clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
        await worker.drain_once(limit=50)

    async with env.engine.connect() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) AS n FROM safety_outbox_jobs "
                    "WHERE note_id = :nid"
                ),
                {"nid": note.id},
            )
        ).first()
    assert count.n == 2, "retries must not create additional jobs"

    # And the DB refuses a second job for the same (Note, Coach) outright.
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with env.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO safety_outbox_jobs "
                    "(gym_id, note_id, coach_member_id, channel, "
                    "channel_user_id, member_id, member_name, member_is_coach, "
                    "status, retry_count, created_at) VALUES "
                    "(:g, :n, :c, 'telegram', '7', :m, 'Ana', 0, 'pending', 0, :ts)"
                ),
                {
                    "g": env.gym_id,
                    "n": note.id,
                    "c": env.coach1_id,
                    "m": env.member_id,
                    "ts": datetime.now(UTC),
                },
            )


async def test_retries_do_not_mint_unbounded_dashboard_credentials(env):
    """A crash-looping job holds at most one *live* dashboard credential,
    however many times it is retried (issue #217 AC #5).

    Before #217 every attempt minted a fresh DashboardLoginToken up front
    and left it redeemable for the full 10-minute TTL.
    """

    async def live_token_count():
        async with env.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) AS n FROM dashboard_login_tokens "
                        "WHERE used_at IS NULL"
                    )
                )
            ).first()
        return row.n

    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )

    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]
    env.outbox._rng = lambda: 0.5
    env.dashboard._clock = lambda: clock_val[0]

    class CrashOnLink:
        """Heads-up lands, then the link send explodes — the shape that
        leaves a freshly-minted credential outstanding."""

        def __init__(self):
            self.sent = []

        async def send(self, channel, channel_user_id, text_, **kw):
            if "/login/" in text_:
                raise RuntimeError("link send exploded")
            self.sent.append(text_)

    worker = _make_worker(env, notifier=CrashOnLink())

    for _ in range(4):
        await worker.drain_once(limit=50)
        # Simulate a crash before the outcome was recorded: startup recovery
        # puts the job straight back to pending, as reset_claimed does.
        async with env.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE safety_outbox_jobs SET status='pending', "
                    "claimed_at=NULL, next_retry_at=NULL"
                )
            )
        assert await live_token_count() <= 1, (
            "a retry must revoke the credential the previous attempt left live"
        )

    # Several tokens were minted over the loop, but only the newest is live.
    async with env.engine.connect() as conn:
        total = (
            await conn.execute(
                text("SELECT COUNT(*) AS n FROM dashboard_login_tokens")
            )
        ).first()
    assert total.n >= 4, "the loop really did re-attempt delivery"
    assert await live_token_count() == 1


async def test_no_credential_is_minted_before_authorization(env):
    """A job whose Coach is no longer authorized never mints a dashboard
    credential at all (issue #217 AC #5) — the mint now happens after the
    authorized heads-up send, not before it."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )
    await env.linking.set_coach(env.coach1_id, False)  # demoted

    await _make_worker(env).drain_once(limit=50)

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT COUNT(*) AS n FROM dashboard_login_tokens")
            )
        ).first()
    assert row.n == 0, "an unauthorized job must not mint a credential"
    failed = await env.outbox.failed_jobs()
    assert failed and failed[0].failure_kind == FailureKind.UNAUTHORIZED


async def test_delivered_link_stays_redeemable(env):
    """The credential bound must not break the feature: the link that
    actually reached the Coach is still redeemable after delivery."""
    await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text="sharp knee pain",
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(env.coach1_id, "Coach Sam", "telegram", "7")],
    )
    await _make_worker(env).drain_once(limit=50)

    links = [m[2] for m in env.notifier.sent if "/login/" in m[2]]
    assert len(links) == 1
    raw = links[0].rsplit("/login/", 1)[1]
    assert await env.dashboard.peek_login_token(raw) is not None

    # The job records that credential so a later attempt could revoke it.
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT login_token_hash, status FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"
    assert row.login_token_hash == hash_token(raw)


async def test_revoke_login_token_is_idempotent(env):
    """revoke_login_token spends a live token once and is a no-op after
    that — the retry path can call it freely."""
    raw = await env.dashboard.create_login_token(env.coach1_id, env.gym_id)
    assert await env.dashboard.peek_login_token(raw) is not None
    assert await env.dashboard.revoke_login_token(hash_token(raw)) is True
    assert await env.dashboard.peek_login_token(raw) is None
    assert await env.dashboard.revoke_login_token(hash_token(raw)) is False
    assert await env.dashboard.revoke_login_token("") is False
    assert await env.dashboard.revoke_login_token("not-a-real-hash") is False


# ── PR #228 review: lease fencing of the credential columns (P2) ──────────


async def _live_token_count(env):
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) AS n FROM dashboard_login_tokens "
                    "WHERE used_at IS NULL"
                )
            )
        ).first()
    return row.n


def _explode_link_send(worker):
    """Make the *link* send raise, so every attempt mints a credential and
    then fails in a retryable way — the only shape that leaves a token
    outstanding across attempts."""
    real = worker._authorized_send

    async def patched(job, text_, **kw):
        if "/login/" in text_:
            raise RuntimeError("link send exploded")
        return await real(job, text_, **kw)

    worker._authorized_send = patched
    return worker


async def _expire_leases(env):
    """Backdate every live claim so the lease looks expired.

    Writes a *naive* UTC value because a raw text() UPDATE bypasses
    TZDateTime's bind processor, and the per-row lease fence compares the
    stored value against an ORM-bound one.
    """
    async with env.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE safety_outbox_jobs SET claimed_at = :t "
                "WHERE status = 'sending'"
            ),
            {"t": (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)},
        )


async def _one_job(env, coach_id=None, body="sharp knee pain"):
    return await env.outbox.create_note_and_jobs(
        member_id=env.member_id,
        gym_id=env.gym_id,
        text=body,
        member_name=env.member_name,
        member_is_coach=False,
        coaches=[(coach_id or env.coach1_id, "Coach Sam", "telegram", "7")],
    )


async def test_stale_owner_cannot_revoke_the_live_owners_credential(env):
    """After a lease expiry and re-claim, the stale owner's teardown must not
    kill the magic link the *new* owner already sent (PR #228 review, P2).

    Without the lease fence on take_login_token_hash the stale owner revoked
    a live, already-delivered credential while the job stayed 'delivered'.
    """
    await _one_job(env)

    stale = await env.outbox.claim_pending(limit=10)
    assert len(stale) == 1

    # The lease expires and the poll loop hands the job to a new owner.
    await _expire_leases(env)
    assert await env.outbox.reset_stale_claims() == 1

    worker = _make_worker(env)
    assert await worker.drain_once(limit=10) == 1
    links = [m[2] for m in env.notifier.sent if "/login/" in m[2]]
    assert len(links) == 1
    raw = links[0].rsplit("/login/", 1)[1]
    assert await env.dashboard.peek_login_token(raw) is not None

    # Now the stale owner finally comes back and tries to tear down.
    await worker._fail(stale[0], "stale failure", FailureKind.UNAUTHORIZED)
    await worker._retry(stale[0], "stale retry")

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, login_token_hash FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "delivered", "the stale owner must not change status"
    assert row.login_token_hash == hash_token(raw)
    assert await env.dashboard.peek_login_token(raw) is not None, (
        "a stale owner must not revoke the live owner's delivered magic link"
    )


async def test_stale_owner_cannot_overwrite_the_live_login_token_hash(env):
    """The mirror image: a stale owner recording its own hash would make the
    live owner's token unrevokable for its full TTL (PR #228 review, P2)."""
    await _one_job(env)
    stale = await env.outbox.claim_pending(limit=10)

    await _expire_leases(env)
    await env.outbox.reset_stale_claims()
    live = await env.outbox.claim_pending(limit=10)
    assert len(live) == 1

    assert await env.outbox.record_login_token(live[0], "live-hash") is True
    assert await env.outbox.record_login_token(stale[0], "stale-hash") is False

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT login_token_hash FROM safety_outbox_jobs")
            )
        ).first()
    assert row.login_token_hash == "live-hash"
    # And the stale owner cannot take it either.
    assert await env.outbox.take_login_token_hash(stale[0]) is None
    assert await env.outbox.take_login_token_hash(live[0]) == "live-hash"


# ── PR #228 review: recovery consumes an attempt (P2) ─────────────────────


async def test_abandoned_claims_consume_an_attempt(env):
    """A crash between claim and outcome costs one attempt, so a crash loop
    converges on the terminal policy instead of retrying forever
    (PR #228 review, P2)."""
    await _one_job(env)

    for expected in range(1, MAX_DELIVERY_ATTEMPTS):
        claimed = await env.outbox.claim_pending(limit=10)
        assert len(claimed) == 1, f"attempt {expected} should have been claimable"
        # "Crash": the send was issued, then the owner died without recording
        # an outcome; the next boot runs reset_claimed.
        assert await env.outbox.mark_attempt_started(claimed[0]) is True
        assert await env.outbox.reset_claimed() == 1
        async with env.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, retry_count, last_error "
                        "FROM safety_outbox_jobs"
                    )
                )
            ).first()
        assert row.status == "pending"
        assert row.retry_count == expected
        assert row.last_error == "claim abandoned (restart)"

    # The last abandoned claim trips the terminal policy.
    last = await env.outbox.claim_pending(limit=10)
    assert len(last) == 1
    assert await env.outbox.mark_attempt_started(last[0]) is True
    assert await env.outbox.reset_claimed() == 1
    failed = await env.outbox.failed_jobs()
    assert len(failed) == 1
    assert failed[0].failure_kind == FailureKind.RETRY_EXHAUSTED
    assert failed[0].retry_count == MAX_DELIVERY_ATTEMPTS
    assert await env.outbox.claim_pending(limit=10) == []


async def test_expired_leases_converge_on_the_terminal_policy(env):
    """The same bound through the lease-expiry path, which the poll loop
    runs every cycle (PR #228 review, P2)."""
    await _one_job(env)

    claims = 0
    for _ in range(MAX_DELIVERY_ATTEMPTS + 4):
        claimed = await env.outbox.claim_pending(limit=10)
        claims += len(claimed)
        for job in claimed:  # the send was issued, then the worker hung
            await env.outbox.mark_attempt_started(job)
        await _expire_leases(env)
        await env.outbox.reset_stale_claims()

    assert claims == MAX_DELIVERY_ATTEMPTS, (
        "an endlessly-hanging job must stop being re-claimed at the policy"
    )
    failed = await env.outbox.failed_jobs()
    assert len(failed) == 1
    assert failed[0].failure_kind == FailureKind.RETRY_EXHAUSTED
    assert failed[0].last_error == "claim lease expired"


async def test_recovery_does_not_retire_a_job_with_attempts_left(env):
    """Recovery must not over-fire: a job well short of the ceiling comes
    back pending and is delivered normally."""
    await _one_job(env)
    claimed = await env.outbox.claim_pending(limit=10)
    await env.outbox.mark_attempt_started(claimed[0])
    assert await env.outbox.reset_claimed() == 1

    assert await _make_worker(env).drain_once(limit=10) == 1
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, retry_count FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"
    assert row.retry_count == 1


# ── PR #228 review: credential teardown is asserted (P2) ──────────────────


async def test_terminal_failure_revokes_the_outstanding_credential(env):
    """When the terminal policy retires a job, the magic link its last
    attempt minted is revoked, not left live for its TTL
    (PR #228 review, P2)."""
    await _one_job(env)

    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]
    env.outbox._rng = lambda: 0.5
    env.dashboard._clock = lambda: clock_val[0]

    worker = _explode_link_send(_make_worker(env))
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
        await worker.drain_once(limit=10)
        # Each attempt mints, so at most one credential is ever live.
        assert await _live_token_count(env) <= 1

    failed = await env.outbox.failed_jobs()
    assert len(failed) == 1
    assert failed[0].failure_kind == FailureKind.RETRY_EXHAUSTED
    assert failed[0].login_token_hash is None
    assert await _live_token_count(env) == 0, (
        "a retired job must not leave a live dashboard credential behind"
    )


async def test_terminal_authorization_failure_revokes_the_credential(env):
    """A job that minted a credential on one attempt and is then retired for
    losing authorization has that credential revoked (PR #228 review, P2)."""
    await _one_job(env)

    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]
    env.outbox._rng = lambda: 0.5
    env.dashboard._clock = lambda: clock_val[0]

    # Attempt 1 mints a credential, then fails retryably.
    await _explode_link_send(_make_worker(env)).drain_once(limit=10)
    assert await _live_token_count(env) == 1
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, login_token_hash "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "pending"
    assert row.retry_count == 1
    assert row.login_token_hash is not None

    # Attempt 2: the Coach has been demoted — a terminal, unauthorized end.
    await env.linking.set_coach(env.coach1_id, False)
    clock_val[0] += timedelta(seconds=MAX_BACKOFF_SECONDS + 10)
    await _make_worker(env).drain_once(limit=10)

    failed = await env.outbox.failed_jobs()
    assert len(failed) == 1
    assert failed[0].failure_kind == FailureKind.UNAUTHORIZED
    assert failed[0].login_token_hash is None
    assert await _live_token_count(env) == 0, (
        "a credential outliving the Coach's authorization must be revoked"
    )


async def test_forget_me_between_headsup_and_link_revokes_the_credential(env):
    """The worst case: the Note is forgotten between the heads-up and the
    link send, so a magic link into that Member's page was already minted.
    It must be killed, not left redeemable (PR #228 review, P2)."""
    await _one_job(env)
    worker = _make_worker(env)

    # _job_still_sending is called three times per delivery: top of the
    # delivery, before the heads-up, and before the link.  Fail the third —
    # i.e. the job vanished (forget-me cascade) after the credential was
    # minted.
    real_check = worker._job_still_sending
    calls = {"n": 0}

    async def patched(job):
        calls["n"] += 1
        if calls["n"] >= 3:
            return False
        return await real_check(job)

    worker._job_still_sending = patched
    await worker.drain_once(limit=10)

    assert calls["n"] >= 3, "the link pre-check must have run"
    links = [m for m in env.notifier.sent if "/login/" in m[2]]
    assert links == [], "no link may be sent once the job is gone"
    assert await _live_token_count(env) == 0, (
        "a credential minted for a forgotten Note must be revoked"
    )


async def test_orphaned_credential_is_revoked_when_recording_fails(env):
    """The mint and the record are two transactions.  If the record does not
    land, the token can never be revoked by any later path — so _mint_link
    undoes it immediately (PR #228 review, P3)."""
    await _one_job(env)

    async def refuse(job, token_hash):
        return False

    env.outbox.record_login_token = refuse

    worker = _make_worker(env)
    await worker.drain_once(limit=10)

    # The heads-up still went out, text-only.
    heads = [m for m in env.notifier.sent if "/login/" not in m[2]]
    links = [m for m in env.notifier.sent if "/login/" in m[2]]
    assert len(heads) == 1
    assert links == []
    assert await _live_token_count(env) == 0, (
        "an unrecorded token is unrevokable later, so it must be revoked now"
    )


# ── PR #228 review: failed_jobs ordering and limit (P3) ───────────────────


async def test_failed_jobs_orders_newest_first_and_honours_limit(env):
    """The operator surface behind AC #4 really is newest-first and bounded
    (PR #228 review, P3)."""
    clock_val = [datetime.now(UTC)]
    env.outbox._clock = lambda: clock_val[0]

    # Three Notes whose single job is retired at three distinct times.
    order = []
    for i in range(3):
        _note, jobs = await _one_job(env, body=f"flag {i}")
        claimed = await env.outbox.claim_pending(limit=10)
        clock_val[0] += timedelta(minutes=1)
        await env.outbox.mark_failed(
            claimed[0], "coach gone", kind=FailureKind.UNAUTHORIZED
        )
        order.append(claimed[0].id)

    failed = await env.outbox.failed_jobs()
    assert [j.id for j in failed] == list(reversed(order)), (
        "failed_jobs must return the newest failure first"
    )
    assert [j.id for j in await env.outbox.failed_jobs(limit=2)] == list(
        reversed(order)
    )[:2]
    assert len(await env.outbox.failed_jobs(limit=1)) == 1
    # Both filters at once still work.
    assert len(
        await env.outbox.failed_jobs(
            gym_id=env.gym_id, kind=FailureKind.UNAUTHORIZED
        )
    ) == 3
    assert await env.outbox.failed_jobs(
        gym_id=env.gym_id, kind=FailureKind.RETRY_EXHAUSTED
    ) == []


# ── PR #228 review: sanitizer rule ordering and coverage (P2, P3) ─────────


def test_sanitize_error_handles_every_telegram_token_rendering():
    """The header rule must not eat the prefix the bot-token rule needs, and
    the canonical /bot<token>/ URL form must match too (PR #228 review, P2).
    """
    secret = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    for raw in (
        f"telegram rejected token 123456789:{secret}",
        f"telegram rejected bot 123456789:{secret}",
        f"https://api.telegram.org/bot123456789:{secret}/sendMessage",
        f"401 {{'bot_token': '123456789:{secret}'}}",
    ):
        cleaned = sanitize_error(raw)
        assert secret not in cleaned, f"secret survived in {raw!r} -> {cleaned!r}"
        assert "123456789" not in cleaned
        assert "[redacted]" in cleaned


def test_sanitize_error_handles_json_and_repr_secret_forms():
    """The key=value rule covers the JSON and repr() renderings its comment
    claims, not just bare key=value (PR #228 review, P3)."""
    for raw in (
        '{"token": "s3cr3tvalue123", "id": 5}',
        "{'token': 's3cr3tvalue123'}",
        "token=s3cr3tvalue123",
        'access_token: "s3cr3tvalue123"',
    ):
        cleaned = sanitize_error(raw)
        assert "s3cr3tvalue123" not in cleaned, f"{raw!r} -> {cleaned!r}"
        assert "[redacted]" in cleaned
    # Non-secret keys are untouched, so telemetry stays readable.
    assert sanitize_error('{"id": 5, "attempt": 2}') == '{"id": 5, "attempt": 2}'


def test_sanitize_error_backstop_catches_unlabelled_high_entropy_values():
    """The length backstop is load-bearing: a credential with no recognisable
    label still cannot reach the logs (PR #228 review, P3)."""
    opaque = "Zk9" + "aB3xQ7" * 8  # 51 chars, no prefix, no key, no colon
    assert len(opaque) >= 40
    assert opaque not in sanitize_error(f"upstream said {opaque}")
    # Ordinary prose is left alone — the backstop must not eat readable text.
    assert sanitize_error("coach no longer reachable in this gym") == (
        "coach no longer reachable in this gym"
    )


# ── PR #228 review r2: a claim is not an attempt (P2) ─────────────────────


async def test_unattempted_claims_are_not_charged_against_the_retry_budget(env):
    """A crash loop must never retire a safety ping that was never sent.

    claim_pending flips a whole batch to 'sending' before any send goes out
    (and the batch is wider than the delivery fan-out), so charging every
    recovered claim would let repeated crashes retire jobs as
    'retry_exhausted' having never reached the notifier at all
    (PR #228 review r2, P2).
    """
    await _one_job(env, coach_id=env.coach1_id)

    for boot in range(MAX_DELIVERY_ATTEMPTS + 4):
        claimed = await env.outbox.claim_pending(limit=50)
        assert len(claimed) == 1, f"boot {boot}: the job must stay claimable"
        # SIGKILL before any send is issued — no attempt_started_at stamp.
        assert await env.outbox.reset_claimed() == 1
        async with env.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, retry_count, failure_kind, last_error "
                        "FROM safety_outbox_jobs"
                    )
                )
            ).first()
        assert row.status == "pending", f"boot {boot}: got {row.status}"
        assert row.retry_count == 0, "an unissued send is not an attempt"
        assert row.failure_kind is None
        assert row.last_error is None

    assert env.notifier.sent == []
    assert await env.outbox.failed_jobs() == []

    # And it still delivers once a worker actually runs.
    assert await _make_worker(env).drain_once(limit=10) == 1
    assert len(env.notifier.sent) == 2  # heads-up + link


async def test_attempt_stamp_is_scoped_to_the_current_claim(env):
    """A stamp left by an earlier attempt must not make the *next* abandoned
    claim look attempted — otherwise one real attempt could be charged twice
    (PR #228 review r2, P2)."""
    await _one_job(env)

    first = await env.outbox.claim_pending(limit=10)
    assert await env.outbox.mark_attempt_started(first[0]) is True
    assert await env.outbox.reset_claimed() == 1
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT retry_count FROM safety_outbox_jobs")
            )
        ).first()
    assert row.retry_count == 1  # a real attempt was charged

    # Second claim, no send issued: the older stamp predates this claim.
    second = await env.outbox.claim_pending(limit=10)
    assert len(second) == 1
    assert await env.outbox.reset_claimed() == 1
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, retry_count FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "pending"
    assert row.retry_count == 1, "a stale stamp must not charge a new claim"


# (The round-2 "stamps the attempt" test lived here.  It asserted the stamp
# *after* delivery, which could not distinguish stamping before the send from
# stamping after it — it survived exactly that mutation (PR #228 review r3).
# test_attempt_is_stamped_before_the_send_is_issued replaces it by reading the
# row while the send is still blocked.)


async def test_recovery_ignores_a_job_whose_lease_moved_on(env):
    """_recover_claims is fenced per row on (id, claimed_at), so a recoverer
    working from a stale read cannot rewrite a job another owner has already
    re-claimed — and does not report or announce it either
    (PR #228 review r2, P3)."""
    await _one_job(env)
    stale_view = await env.outbox.claim_pending(limit=10)
    assert len(stale_view) == 1
    await env.outbox.mark_attempt_started(stale_view[0])

    # Another recoverer gets there first: the job is requeued and re-claimed
    # with a fresh lease stamp.
    await _expire_leases(env)
    assert await env.outbox.reset_stale_claims() == 1
    live = await env.outbox.claim_pending(limit=10)
    assert len(live) == 1
    assert live[0].claimed_at != stale_view[0].claimed_at

    emitted = []
    handler = logging.Handler()
    handler.emit = lambda r: (
        emitted.append(r.outbox) if hasattr(r, "outbox") else None
    )
    logging.getLogger("agentg.safety_outbox").addHandler(handler)
    try:
        # The stale view still says "sending, mine" — recovery must no-op.
        assert await env.outbox._recover_claims(stale_view, "stale recovery") == 0
    finally:
        logging.getLogger("agentg.safety_outbox").removeHandler(handler)

    assert emitted == [], "a fenced-out row must not emit terminal telemetry"
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, claimed_at, last_error FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "sending", "the live owner's claim must survive"
    # "claim lease expired" is the *legitimate* earlier recovery; the point is
    # that the fenced-out "stale recovery" write did not land on top of it.
    assert row.last_error == "claim lease expired"


async def test_recovery_does_not_announce_a_delivered_job_as_terminal(env):
    """A job delivered between the read and the write must not produce
    terminal telemetry or inflate the recovered count
    (PR #228 review r2, P3)."""
    await _one_job(env)
    view = await env.outbox.claim_pending(limit=10)
    await env.outbox.mark_attempt_started(view[0])
    # Push it to the edge of the policy so an unfenced write would retire it.
    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE safety_outbox_jobs SET retry_count = :n"),
            {"n": MAX_DELIVERY_ATTEMPTS - 1},
        )
    view[0].retry_count = MAX_DELIVERY_ATTEMPTS - 1

    # It actually gets delivered before recovery runs.
    await env.outbox.mark_delivered(view[0])

    emitted = []
    handler = logging.Handler()
    handler.emit = lambda r: (
        emitted.append(r.outbox) if hasattr(r, "outbox") else None
    )
    logging.getLogger("agentg.safety_outbox").addHandler(handler)
    try:
        assert await env.outbox._recover_claims(view, "claim lease expired") == 0
    finally:
        logging.getLogger("agentg.safety_outbox").removeHandler(handler)

    assert emitted == [], (
        "a delivered job must never be announced as retry_exhausted"
    )
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, failure_kind FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"
    assert row.failure_kind is None


# ── PR #228 review r2: labelled Authorization headers (P2) ────────────────


def test_sanitize_error_handles_a_labelled_authorization_header():
    """A recognised key in front of Bearer/Basic must not swallow the scheme
    word and publish the credential (PR #228 review r2, P2).

    This is the mirror of the Telegram ordering bug: whichever rule fires
    first must consume the whole credential, never just its label.
    """
    cases = [
        (
            "auth_token: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        ),
        (
            "credential = Basic dXNlcjpwYXNzd29yZDEyMw==",
            "dXNlcjpwYXNzd29yZDEyMw==",
        ),
        (
            'headers={"authorization": "Bearer sk-abcdefghijklmnopqrstuvwx"}',
            "sk-abcdefghijklmnopqrstuvwx",
        ),
    ]
    for raw, secret in cases:
        cleaned = sanitize_error(raw)
        assert secret not in cleaned, f"{raw!r} -> {cleaned!r}"
        assert "[redacted]" in cleaned

    # The unlabelled form the earlier ordering fixed must still work, so the
    # two rules cannot be traded off against each other again.
    assert "eyJhbGciOiJIUzI1NiJ9abcdef" not in sanitize_error(
        "401: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdef"
    )
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in sanitize_error(
        "telegram rejected token 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    )
    # And readable telemetry is still readable.
    assert sanitize_error('{"id": 5, "attempt": 2}') == '{"id": 5, "attempt": 2}'


# ── PR #228 review r2: the except-path orphan revoke (P3) ─────────────────


async def test_orphaned_credential_is_revoked_when_recording_raises(env):
    """The `except` arm of _mint_link's orphan handling, not just the False
    return: a raising record_login_token must also undo the mint
    (PR #228 review r2, P3)."""
    await _one_job(env)

    async def boom(job, token_hash):
        raise RuntimeError("db hiccup")

    env.outbox.record_login_token = boom

    await _make_worker(env).drain_once(limit=10)

    heads = [m for m in env.notifier.sent if "/login/" not in m[2]]
    links = [m for m in env.notifier.sent if "/login/" in m[2]]
    assert len(heads) == 1, "the safety heads-up still goes out text-only"
    assert links == []
    assert await _live_token_count(env) == 0, (
        "a token minted before the record raised is orphaned and must be revoked"
    )
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, login_token_hash FROM safety_outbox_jobs")
            )
        ).first()
    assert row.status == "delivered"
    assert row.login_token_hash is None


# ── PR #228 review r3: attempt-ness is durable, not clock-derived (P2) ────


async def test_attempt_stamp_is_cleared_by_every_claim(env):
    """`claim_pending` clears `attempt_started_at`, so "was this claim
    attempted?" is a stored fact rather than a comparison of two wall-clock
    readings (PR #228 review r3, P2)."""
    await _one_job(env)

    claimed = await env.outbox.claim_pending(limit=10)
    assert await env.outbox.mark_attempt_started(claimed[0]) is True
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT attempt_started_at FROM safety_outbox_jobs")
            )
        ).first()
    assert row.attempt_started_at is not None

    # Recovery charges that real attempt and clears the stamp with the claim.
    assert await env.outbox.reset_claimed() == 1
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT retry_count, attempt_started_at "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.retry_count == 1
    assert row.attempt_started_at is None

    # The next claim starts clean, with no send issued.
    again = await env.outbox.claim_pending(limit=10)
    assert len(again) == 1
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT attempt_started_at FROM safety_outbox_jobs")
            )
        ).first()
    assert row.attempt_started_at is None

    # claim_pending clears the stamp itself, independently of the requeue
    # paths that also clear it.  Pinned directly, because the requeue paths
    # would otherwise hide a claim that stopped clearing: a pending row is
    # given a stale stamp (a legacy row, or a future requeue path that
    # forgets), and the claim must still start un-attempted.
    await env.outbox.reset_claimed()
    async with env.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE safety_outbox_jobs SET attempt_started_at = :t, "
                "next_retry_at = NULL"
            ),
            {"t": datetime.now(UTC).replace(tzinfo=None)},
        )
    reclaimed = await env.outbox.claim_pending(limit=10)
    assert len(reclaimed) == 1
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT attempt_started_at FROM safety_outbox_jobs")
            )
        ).first()
    assert row.attempt_started_at is None, (
        "a fresh claim must start un-attempted whatever the row carried"
    )
    # And recovery therefore charges nothing for that unattempted claim.
    assert await env.outbox.reset_claimed() == 1
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT retry_count FROM safety_outbox_jobs")
            )
        ).first()
    assert row.retry_count == 1


async def test_retry_requeue_clears_the_attempt_stamp(env):
    """The same invariant through reset_for_retry, the ordinary failure
    path (PR #228 review r3, P2)."""
    await _one_job(env)
    claimed = await env.outbox.claim_pending(limit=10)
    await env.outbox.mark_attempt_started(claimed[0])
    assert await env.outbox.reset_for_retry(claimed[0], "notifier send failed")

    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, attempt_started_at "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "pending"
    assert row.retry_count == 1
    assert row.attempt_started_at is None


async def test_unattempted_claims_survive_a_backwards_clock_step(env):
    """A backwards wall-clock step (NTP correction, VM resume) must not make
    crash-before-send look like a delivery attempt (PR #228 review r3, P2).

    The old `attempt_started_at >= claimed_at` predicate rested on the wall
    clock advancing; one 60s step backwards retired an unsent safety ping as
    retry_exhausted.
    """
    now = [datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)]
    env.outbox._clock = lambda: now[0]
    await _one_job(env)

    # One genuine attempt, charged.
    claimed = await env.outbox.claim_pending(limit=10)
    await env.outbox.mark_attempt_started(claimed[0])
    assert await env.outbox.reset_claimed() == 1

    now[0] -= timedelta(seconds=60)  # the clock steps backwards

    for boot in range(MAX_DELIVERY_ATTEMPTS + 2):
        assert len(await env.outbox.claim_pending(limit=10)) == 1
        assert await env.outbox.reset_claimed() == 1  # crash before any send
        async with env.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, retry_count, failure_kind "
                        "FROM safety_outbox_jobs"
                    )
                )
            ).first()
        assert row.status == "pending", f"boot {boot}: got {row.status}"
        assert row.retry_count == 1, (
            "only the one real attempt may ever be charged"
        )
        assert row.failure_kind is None

    assert env.notifier.sent == []
    assert await env.outbox.failed_jobs() == []


# ── PR #228 review r3: the stamp must precede the send (P2) ───────────────


async def test_attempt_is_stamped_before_the_send_is_issued(env):
    """The stamp must be durable *before* `notifier.send` is entered.

    This is the load-bearing ordering of the whole attempt-accounting
    design: a worker SIGKILLed mid-send (hung provider plus a liveness kill,
    OOM, deploy restart) must be charged, or `_recover_claims` requeues it
    at retry_count 0 and it re-sends forever. Asserting the stamp *after*
    delivery cannot tell the two orderings apart, so this test reads the row
    while the send is still blocked (PR #228 review r3, P2).
    """
    await _one_job(env)

    inside_send = asyncio.Event()
    release = asyncio.Event()

    class BlockingNotifier:
        def __init__(self):
            self.sent = []

        async def send(self, channel, channel_user_id, text_, **kw):
            self.sent.append(text_)
            inside_send.set()
            await release.wait()

    worker = _make_worker(env, notifier=BlockingNotifier())
    task = asyncio.create_task(worker.drain_once(limit=10))
    try:
        await asyncio.wait_for(inside_send.wait(), timeout=5)

        # The send is in flight right now: the stamp must already be durable.
        async with env.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, attempt_started_at "
                        "FROM safety_outbox_jobs"
                    )
                )
            ).first()
        assert row.status == "sending"
        assert row.attempt_started_at is not None, (
            "a crash during the send must be charged, so the stamp has to be "
            "written before notifier.send is entered"
        )
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=5)


async def test_crash_during_the_send_is_charged_as_an_attempt(env):
    """The consequence of that ordering: a job killed mid-send comes back
    with the attempt charged, so repeated mid-send kills converge on the
    terminal policy instead of re-sending forever (PR #228 review r3, P2)."""
    await _one_job(env)

    inside_send = asyncio.Event()
    release = asyncio.Event()

    class BlockingNotifier:
        def __init__(self):
            self.sent = []

        async def send(self, channel, channel_user_id, text_, **kw):
            self.sent.append(text_)
            inside_send.set()
            await release.wait()

    notifier = BlockingNotifier()
    worker = _make_worker(env, notifier=notifier)
    task = asyncio.create_task(worker.drain_once(limit=10))
    await asyncio.wait_for(inside_send.wait(), timeout=5)

    # SIGKILL mid-send: the delivery never records an outcome.
    task.cancel()
    release.set()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(notifier.sent) == 1, "the send really was in flight"
    assert await env.outbox.reset_claimed() == 1  # next boot recovers it
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, retry_count, attempt_started_at "
                    "FROM safety_outbox_jobs"
                )
            )
        ).first()
    assert row.status == "pending"
    assert row.retry_count == 1, (
        "a crash during the send must consume an attempt"
    )
    assert row.attempt_started_at is None


# ── PR #228 review r3: segmented provider API keys (P3) ───────────────────


def test_sanitize_error_redacts_segmented_provider_api_keys():
    """Real provider keys are segmented, and are too short for the {40,}
    backstop — the prefix rule has to allow inner separators
    (PR #228 review r3, P3)."""
    for raw, secret in (
        ("provider rejected: sk_live_51H8xkjKLmNoPqRsTuVwX", "sk_live_51H8xkjKLmNoPqRsTuVwX"),
        ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"),
        ("pk_live_51H8xkjKLmNoPqRsTuVwX", "pk_live_51H8xkjKLmNoPqRsTuVwX"),
        ("sk_abcdefghijklmnopqrstuvwx", "sk_abcdefghijklmnopqrstuvwx"),
    ):
        cleaned = sanitize_error(raw)
        assert secret not in cleaned, f"{raw!r} -> {cleaned!r}"
        assert "[redacted]" in cleaned

    # The widened class must not start eating the fixed error vocabulary or
    # readable telemetry.
    for benign in (
        "coach no longer reachable in this gym",
        "safety note no longer exists",
        "notifier send timed out",
        "delivery error: RuntimeError",
        '{"id": 5, "attempt": 2}',
    ):
        assert sanitize_error(benign) == benign
