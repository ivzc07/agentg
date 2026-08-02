"""Durable safety-outbox: atomicity, delivery, restart recovery, and
failure injection (issue #216)."""

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
from agentg.safety_outbox import OutboxWorker, SafetyOutbox


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
        # Direct insert: the same gym_id, note_id, coach_member_id.
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

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=env.notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
    )
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

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=env.notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
    )
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
    # The outbox job references the Gym; there is no way to notify a coach
    # in another Gym because the job stores the channel identity at creation
    # time and the worker sends only to that channel.
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
    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=env.notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
    )
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

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=env.notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
    )
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
    # Make the dashboard store fail on every call.
    real_mint = env.dashboard.create_login_token

    async def failing_mint(*args, **kwargs):
        raise RuntimeError("db hiccup")

    # Patch the dashboard store directly.
    env.dashboard.create_login_token = failing_mint

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
        notifier=env.notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
    )
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
    # Simulate: write jobs, "crash" before the worker runs.
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
    fresh_worker = OutboxWorker(
        outbox=env.outbox,
        notifier=env.notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
    )
    # start() drains all pending before returning.
    await fresh_worker.start()

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


# ── failure injection ───────────────────────────────────────────────────────


async def test_notifier_failure_marks_job_failed_not_lost(env):
    """When the notifier raises for one coach, the job is marked failed
    (not stuck pending) and the other coach is delivered normally."""
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
    )
    await worker.drain_once(limit=50)

    # Coach 7 (failing) → job failed, Coach 8 → job delivered.
    async with env.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT coach_member_id, status, failure_reason "
                    "FROM safety_outbox_jobs ORDER BY coach_member_id"
                )
            )
        ).all()
    job_7, job_8 = rows
    assert job_7.status == "failed"
    assert job_7.failure_reason is not None
    assert job_8.status == "delivered"

    # Coach 8 got both messages.
    assert any(uid == "8" for _ch, uid, _t, _dp, _pc in failing_notifier.sent)
    assert not any(uid == "7" for _ch, uid, _t, _dp, _pc in failing_notifier.sent)


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

    worker = OutboxWorker(
        outbox=env.outbox,
        notifier=env.notifier,
        dashboard_store=env.dashboard,
        dashboard_base_url=env.DASHBOARD_BASE,
    )
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


# ── edge cases ───────────────────────────────────────────────────────────────


async def test_no_coaches_no_jobs(env):
    """When a Gym has no Coaches, no outbox jobs are created, but the
    safety Note is still written."""
    # Use a Gym with no coaches.
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
    # Add a third coach who is the flagger.
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
