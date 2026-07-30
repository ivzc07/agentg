"""Persistence for the dashboard door (spec-dashboard §Access & identity).

One-time magic-link tokens: the raw token exists only in the URL the bot
sends; the row stores its SHA-256 hex digest. Build-time choices (deferred
by the spec): 10-minute TTL, SHA-256 for hashing — token guesses are
already 256-bit random, so an unsalted fast hash is enough and keeps
redemption a plain indexed lookup.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.checkin import LAPSED, SNOOZED
from agentg.models import (
    DashboardLoginToken,
    Exercise,
    Gym,
    Member,
    MemberChannel,
    MemberNote,
    Routine,
    Session,
    Set,
)
from agentg.routines import RoutineStore
from agentg.timezones import local_date

TOKEN_TTL = timedelta(minutes=10)
TOKEN_BYTES = 32

# The Member page's Sessions list is paginated so a long history never
# renders in one page (spec-dashboard §The Member page).
SESSIONS_PER_PAGE = 10

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


@dataclass(frozen=True)
class RosterRow:
    """One line of the Table roster: a non-coach Member with a live channel.

    ``gap_days`` is gym-local days since the newest Session — the signup
    date for a Session-less Member, the same fallback the check-in sweep
    uses. ``snoozed_until`` is set only while a snooze is still running — an
    expired-but-unswept snooze renders as a normal row, never with a past
    date; lapsed Members are returned in the separate tail, never here with
    a marker.
    """

    member_id: int
    name: str
    gap_days: int
    has_sessions: bool
    is_new: bool  # no active Routine
    snoozed_until: date | None


@dataclass(frozen=True)
class RoutineDayView:
    """One pinned day of the active Routine: weekday (0=Mon), name, and the
    ordered exercises with their optional set/rep scheme."""

    weekday: int
    name: str
    exercises: list[tuple[str, int | None, str | None]]  # (name, sets, reps)


@dataclass(frozen=True)
class SessionView:
    """One Session on the Member page: the gym-local date and the logged
    sets grouped by Exercise (top weight first in the page's rendering)."""

    on: date
    # (exercise name, weight, reps) per logged set, in logging order.
    sets: list[tuple[str, float | None, int]]


@dataclass(frozen=True)
class LastWeight:
    """An Exercise's latest numbers: the top-set weight of the Member's most
    recent Session that logged it, and the reps at that weight."""

    exercise: str
    weight: float | None  # None for bodyweight-only logging
    reps: list[int]
    on: date


@dataclass(frozen=True)
class NoteView:
    """A Note as the Coach reads it; retired rows carry their retirement
    date so the collapsed tail stays dated (spec-dashboard §The Member page)."""

    kind: str
    text: str
    on: date
    retired_on: date | None


@dataclass(frozen=True)
class MemberPage:
    """Everything the read-only Member page renders (issue #99).

    ``sessions`` is one page (``page`` of ``pages``), most recent first.
    ``routine`` is empty when no Routine is active; ``snoozed_until`` is set
    only while a snooze still runs — the same rule the roster applies.
    """

    member_id: int
    name: str
    member_since: date
    weight_unit: str
    session_count: int
    gap_days: int
    has_sessions: bool
    last_session_on: date | None
    lapsed: bool
    snoozed_until: date | None
    routine: list[RoutineDayView]
    sessions: list[SessionView]
    page: int
    pages: int
    weights: list[LastWeight]
    notes: list[NoteView]
    retired_notes: list[NoteView]


class DashboardStore:
    def __init__(self, engine: AsyncEngine, clock: Clock = _utcnow) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock
        # The Member page renders the active Routine through the same loader
        # the Agent's tools use, so the two never diverge.
        self._routines = RoutineStore(engine, clock)

    async def create_login_token(self, member_id: int, gym_id: int) -> str:
        """Mint a one-time token; returns the raw value for the magic link."""
        raw = secrets.token_urlsafe(TOKEN_BYTES)
        async with self._sessions() as db:
            db.add(
                DashboardLoginToken(
                    token_hash=hash_token(raw),
                    member_id=member_id,
                    gym_id=gym_id,
                    expires_at=self._clock() + TOKEN_TTL,
                )
            )
            await db.commit()
        return raw

    async def peek_login_token(self, raw_token: str) -> DashboardLoginToken | None:
        """The token row if it is redeemable right now, without spending it.

        Lets the interstitial page distinguish "click to sign in" from a
        dead link; the actual spend happens on POST.
        """
        if not raw_token:
            return None
        async with self._sessions() as db:
            return await db.scalar(
                select(DashboardLoginToken).where(
                    DashboardLoginToken.token_hash == hash_token(raw_token),
                    DashboardLoginToken.used_at.is_(None),
                    DashboardLoginToken.expires_at > self._clock(),
                )
            )

    async def redeem_login_token(self, raw_token: str) -> DashboardLoginToken | None:
        """Spend a one-time token; ``None`` if unknown, used, or expired.

        The spend is one atomic UPDATE, so two concurrent redemptions of the
        same link can't both succeed — single-use holds even without the
        runtime's per-identity serialization.
        """
        if not raw_token:
            return None
        now = self._clock()
        async with self._sessions() as db:
            token = await db.scalar(
                update(DashboardLoginToken)
                .where(
                    DashboardLoginToken.token_hash == hash_token(raw_token),
                    DashboardLoginToken.used_at.is_(None),
                    DashboardLoginToken.expires_at > now,
                )
                .values(used_at=now)
                .returning(DashboardLoginToken)
            )
            await db.commit()
            return token

    async def coach_identity(self, member_id: int, gym_id: int) -> tuple[Member, Gym] | None:
        """Re-resolve a session's identity, ``None`` unless the Member is
        still a Coach of that Gym. Checked per request, not per session: a
        demoted coach is out on their next click (spec-dashboard §Access &
        identity)."""
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(Member, Gym)
                    .join(Gym, Member.gym_id == Gym.id)
                    .where(Member.id == member_id, Member.gym_id == gym_id)
                )
            ).first()
        if row is None or not row[0].is_coach:
            return None
        return row[0], row[1]

    async def roster(self, gym_id: int) -> tuple[list[RosterRow], list[RosterRow]]:
        """The Coach's roster as ``(rows, lapsed tail)``.

        Exactly the Gym's non-coach Members with a live channel — the
        ``member_channels`` join skips a gym switch's ghost row
        (spec-dashboard §The roster). Rows sort by Gap, largest first;
        lapsed Members fold into a tail ordered most-recently-active first,
        out of the Gap sort.
        """
        async with self._sessions() as db:
            gym = await db.get(Gym, gym_id)
            timezone = (gym.timezone if gym else None) or "UTC"
            rows = (
                await db.execute(
                    select(Member, func.max(Session.started_at))
                    .join(MemberChannel, MemberChannel.member_id == Member.id)
                    .outerjoin(Session, Session.member_id == Member.id)
                    .where(Member.gym_id == gym_id, Member.is_coach.is_(False))
                    .group_by(Member.id)
                )
            ).all()
            with_routines = set(
                (
                    await db.scalars(
                        select(Routine.member_id).where(
                            Routine.gym_id == gym_id, Routine.is_active.is_(True)
                        )
                    )
                ).all()
            )
        today = local_date(self._clock(), timezone)
        roster_rows, lapsed_rows = [], []
        for member, last_started in rows:
            row = RosterRow(
                member_id=member.id,
                name=member.name,
                gap_days=(today - local_date(last_started or member.created_at, timezone)).days,
                has_sessions=last_started is not None,
                is_new=member.id not in with_routines,
                snoozed_until=(
                    member.snoozed_until
                    if member.checkin_state == SNOOZED
                    and member.snoozed_until is not None
                    and member.snoozed_until > today
                    else None
                ),
            )
            (lapsed_rows if member.checkin_state == LAPSED else roster_rows).append(row)
        roster_rows.sort(key=lambda r: (-r.gap_days, r.name.lower()))
        lapsed_rows.sort(key=lambda r: (r.gap_days, r.name.lower()))
        return roster_rows, lapsed_rows

    async def member_page(self, gym_id: int, member_id: int, page: int = 1) -> MemberPage | None:
        """The read-only training record for one roster Member.

        ``None`` for anything that must not resolve: a mistyped or unknown
        id, a forgotten Member, a Member of another Gym, a coach-flagged
        Member, or a gym switch's ghost row (no live channel). The web layer
        turns every one of these into the same bare 404 — no tombstone, no
        "this member left" (spec-dashboard §What a Coach sees).
        """
        async with self._sessions() as db:
            member = await db.scalar(
                select(Member)
                .join(MemberChannel, MemberChannel.member_id == Member.id)
                .where(
                    Member.id == member_id,
                    Member.gym_id == gym_id,
                    Member.is_coach.is_(False),
                )
            )
            if member is None:
                return None
            gym = await db.get(Gym, gym_id)
            timezone = (gym.timezone if gym else None) or "UTC"
            weight_unit = (gym.weight_unit if gym else None) or "kg"

            session_rows = list(
                await db.scalars(
                    select(Session)
                    .where(Session.member_id == member_id)
                    .order_by(Session.started_at.desc(), Session.id.desc())
                )
            )
            today = local_date(self._clock(), timezone)
            last_started = session_rows[0].started_at if session_rows else None
            gap_days = (today - local_date(last_started or member.created_at, timezone)).days

            weights = await self._last_weights(db, member_id, timezone)
            notes = list(
                await db.scalars(
                    select(MemberNote)
                    .where(MemberNote.member_id == member_id)
                    .order_by(MemberNote.created_at, MemberNote.id)
                )
            )

            count = len(session_rows)
            pages = max(1, -(-count // SESSIONS_PER_PAGE))
            page = min(max(page, 1), pages)
            page_sessions = session_rows[(page - 1) * SESSIONS_PER_PAGE : page * SESSIONS_PER_PAGE]
            sets_by_session = await self._sessions_sets(
                db, [session.id for session in page_sessions]
            )
            sessions = [
                SessionView(
                    on=local_date(session.started_at, timezone),
                    sets=sets_by_session[session.id],
                )
                for session in page_sessions
            ]

        routine_dict = await self._routines.active_routine(member_id)
        routine = [
            RoutineDayView(
                workout["weekday"],
                workout["name"],
                [(e["exercise"], e["sets"], e["reps"]) for e in workout["exercises"]],
            )
            for workout in (routine_dict["workouts"] if routine_dict else [])
        ]

        return MemberPage(
            member_id=member.id,
            name=member.name,
            member_since=local_date(member.created_at, timezone),
            weight_unit=weight_unit,
            session_count=count,
            gap_days=gap_days,
            has_sessions=last_started is not None,
            last_session_on=local_date(last_started, timezone) if last_started else None,
            lapsed=member.checkin_state == LAPSED,
            snoozed_until=(
                member.snoozed_until
                if member.checkin_state == SNOOZED
                and member.snoozed_until is not None
                and member.snoozed_until > today
                else None
            ),
            routine=routine,
            sessions=sessions,
            page=page,
            pages=pages,
            weights=weights,
            notes=[
                NoteView(n.kind, n.text, local_date(n.created_at, timezone), None)
                for n in notes
                if n.retired_at is None
            ],
            retired_notes=[
                NoteView(
                    n.kind,
                    n.text,
                    local_date(n.created_at, timezone),
                    local_date(n.retired_at, timezone),
                )
                for n in notes
                if n.retired_at is not None
            ],
        )

    async def _sessions_sets(
        self, db, session_ids: list[int]
    ) -> dict[int, list[tuple[str, float | None, int]]]:
        """One page's sets in a single query (no per-Session round-trips),
        grouped by Session in logging order."""
        sets: dict[int, list[tuple[str, float | None, int]]] = {
            session_id: [] for session_id in session_ids
        }
        if not session_ids:
            return sets
        rows = (
            await db.execute(
                select(Set.session_id, Exercise.name, Set.weight, Set.reps)
                .join(Exercise, Set.exercise_id == Exercise.id)
                .where(Set.session_id.in_(session_ids))
                .order_by(Set.id)
            )
        ).all()
        for session_id, name, weight, reps in rows:
            sets[session_id].append((name, weight, reps))
        return sets

    async def _last_weights(self, db, member_id: int, timezone: str) -> list[LastWeight]:
        """Last weight per Exercise, read off the sets table directly
        (``exercise_history`` carries no date — spec-dashboard §Data model).

        One pass over the Member's sets most-recent-first: an Exercise's
        first sighting is its newest Session, and its top set there is the
        last weight. The ``sets.exercise_id`` index keeps the per-Exercise
        read from scanning.
        """
        rows = (
            await db.execute(
                select(Exercise.name, Set.weight, Set.reps, Session.id, Session.started_at)
                .join(Session, Set.session_id == Session.id)
                .join(Exercise, Set.exercise_id == Exercise.id)
                .where(Session.member_id == member_id)
                .order_by(Session.started_at.desc(), Session.id.desc(), Set.id)
            )
        ).all()
        by_exercise: dict[str, tuple[int, date, list[tuple[float | None, int]]]] = {}
        for name, weight, reps, session_id, started in rows:
            entry = by_exercise.get(name)
            if entry is None:
                entry = (session_id, local_date(started, timezone), [])
                by_exercise[name] = entry
            if entry[0] == session_id:
                entry[2].append((weight, reps))
        weights = []
        for name, (_, on, sets) in by_exercise.items():
            top = max((w for w, _ in sets if w is not None), default=None)
            weights.append(
                LastWeight(name, top, [reps for w, reps in sets if w == top], on)
            )
        return sorted(weights, key=lambda w: w.exercise)
