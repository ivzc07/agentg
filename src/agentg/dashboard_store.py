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
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Row, func, or_, select, union_all, update
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
    Workout,
)
from agentg.routines import RoutineStore
from agentg.timezones import local_date

TOKEN_TTL = timedelta(minutes=10)
TOKEN_BYTES = 32

# Severity colouring (issue #98, spec-dashboard §The roster): consecutive
# missed planned Workout days since the last Session — amber at one, red at
# three, never a fixed day-count threshold.
SEVERITY_AMBER_AT = 1
SEVERITY_RED_AT = 3

# The Member page's Sessions list is paginated so a long history never
# renders in one page (spec-dashboard §The Member page).
SESSIONS_PER_PAGE = 10

# The Cards attendance grid (issue #106): 4 weeks as a 7-column Mon–Sun day
# grid, ending with the current week.
GRID_WEEKS = 4

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _utc_day(d: date) -> datetime:
    """Midnight UTC at the start of ``d`` — a query bound for a date window."""
    return datetime.combine(d, time.min, tzinfo=UTC)


def _weekday_hits(start: date, end: date, weekdays: set[int]) -> int:
    """How many dates in the inclusive ``[start, end]`` fall on a planned weekday."""
    if start > end or not weekdays:
        return 0
    days = (end - start).days + 1
    full_weeks, extra = divmod(days, 7)
    hits = full_weeks * len(weekdays)
    tail_start = start + timedelta(weeks=full_weeks)
    for i in range(extra):
        if (tail_start + timedelta(days=i)).weekday() in weekdays:
            hits += 1
    return hits


def planned_weekdays_on(spans: list[tuple[date, set[int]]], day: date) -> set[int]:
    """The weekdays planned for ``day`` under the Routine governing it —
    the most recent span created on or before it (empty before the first)."""
    planned: set[int] = set()
    for span_start, weekdays in spans:
        if span_start > day:
            break
        planned = weekdays
    return planned


def missed_planned_days(
    spans: list[tuple[date, set[int]]], since: date, yesterday: date
) -> int:
    """Missed planned Workout days in the inclusive ``[since, yesterday]``.

    ``spans`` is the Member's Routine history as ``(first gym-local date the
    Routine governs, its planned weekdays)``, oldest first — the per-date
    reconstruction of spec-dashboard §Attendance: the most recent Routine
    created on or before a date judges it, day-grained, so a Routine created
    mid-day governs that whole day. Today is excluded by the caller passing
    ``yesterday``; a day with no Routine yet is never a miss.
    """
    missed = 0
    for i, (span_start, weekdays) in enumerate(spans):
        span_end = spans[i + 1][0] - timedelta(days=1) if i + 1 < len(spans) else yesterday
        missed += _weekday_hits(max(span_start, since), min(span_end, yesterday), weekdays)
    return missed


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

    ``missed_days`` feeds the severity colour (issue #98): consecutive
    missed planned Workout days since the last Session, each date judged
    against the Routine active on it; a Session-less Member counts from the
    moment their first Routine exists. Gap stays the sort key and row text —
    the colour never re-sorts.
    """

    member_id: int
    name: str
    gap_days: int
    has_sessions: bool
    is_new: bool  # no active Routine
    snoozed_until: date | None
    missed_days: int

    @property
    def severity(self) -> str | None:
        """``"amber"`` at one missed planned day, ``"red"`` at three, else
        ``None``. No active Routine means no colour (the grey "new" tag
        stands), and a running snooze shows no colour while it runs
        (spec-dashboard §The roster).
        """
        if self.is_new or self.snoozed_until is not None:
            return None
        if self.missed_days >= SEVERITY_RED_AT:
            return "red"
        if self.missed_days >= SEVERITY_AMBER_AT:
            return "amber"
        return None


@dataclass(frozen=True)
class DayCell:
    """One square of the Cards attendance grid.

    ``state`` is ``"hit"`` (a Session landed that day, planned or not),
    ``"miss"`` (a planned Workout day per the Routine governing that date,
    no Session), ``"future"`` (the day has not happened), or ``"plain"``
    (a past day that was neither). Today never counts as a miss until it is
    over — the severity engine's own rule.
    """

    on: date
    state: str


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
    # (exercise name, weight, reps, set comment) per logged set, in logging
    # order. The comment is the Member's own words, shown verbatim.
    sets: list[tuple[str, float | None, int, str | None]]


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

    ``routine_id`` is the active Routine's id (``None`` when there is none) —
    the stamp the Routine editor's stale-save check compares against. The
    ownership chip reads ``coach_authored`` and ``routine_author`` (the
    actor stamp's name, ``None`` for an agent-written plan or a blanked
    stamp).
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
    routine_id: int | None
    coach_authored: bool
    routine_author: str | None
    sessions: list[SessionView]
    page: int
    pages: int
    weights: list[LastWeight]
    notes: list[NoteView]
    retired_notes: list[NoteView]


def _gap_days(today: date, last_started: datetime | None, member_created: datetime, timezone: str) -> int:
    """Gym-local days since the newest Session — the signup date for a
    Session-less Member. One formula shared by the roster and the Member
    page header, so the two surfaces can never disagree."""
    return (today - local_date(last_started or member_created, timezone)).days


def _active_snooze(member: Member, today: date) -> date | None:
    """The snooze date only while a snooze is still running — an
    expired-but-unswept snooze renders plain. Shared by the roster and the
    Member page header."""
    if (
        member.checkin_state == SNOOZED
        and member.snoozed_until is not None
        and member.snoozed_until > today
    ):
        return member.snoozed_until
    return None


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
            # Per-date reconstruction input: each roster Member's Routines
            # (superseded ones keep their Workouts) with their planned
            # weekdays, oldest first. Severity only scores the window since
            # the Member's own last Session, so the read is pruned per
            # Member: every Routine created after their anchor (all of a
            # Session-less Member's — their count starts at the first
            # Routine), plus the single latest Routine created on or before
            # it, which governs the window's first dates. The newest Routine
            # per Member is always in the read, so the active-Routine flags
            # come along free. Both slices go out as one UNION ALL round-trip.
            member_ids = [member.id for member, _ in rows]
            history: list[Row[Any]] = []
            if member_ids:
                last_sess = (
                    select(
                        Session.member_id.label("member_id"),
                        func.max(Session.started_at).label("anchor"),
                    )
                    .where(Session.member_id.in_(member_ids))
                    .group_by(Session.member_id)
                    .subquery()
                )
                recent = (
                    select(
                        Routine.id,
                        Routine.member_id,
                        Routine.created_at,
                        Routine.is_active,
                        Workout.weekday,
                    )
                    .outerjoin(last_sess, last_sess.c.member_id == Routine.member_id)
                    .outerjoin(Workout, Workout.routine_id == Routine.id)
                    .where(
                        Routine.gym_id == gym_id,
                        Routine.member_id.in_(member_ids),
                        or_(
                            last_sess.c.anchor.is_(None),
                            Routine.created_at > last_sess.c.anchor,
                        ),
                    )
                )
                # The pre-anchor governing Routine per Member. The id
                # tie-break matters: same-instant saves (one clock tick, two
                # Routines) must pick the newest row — the active one.
                ranked = (
                    select(
                        Routine.id,
                        Routine.member_id,
                        Routine.created_at,
                        Routine.is_active,
                        func.row_number()
                        .over(
                            partition_by=Routine.member_id,
                            order_by=(Routine.created_at.desc(), Routine.id.desc()),
                        )
                        .label("rn"),
                    )
                    .join(last_sess, last_sess.c.member_id == Routine.member_id)
                    .where(
                        Routine.gym_id == gym_id,
                        Routine.member_id.in_(member_ids),
                        Routine.created_at <= last_sess.c.anchor,
                    )
                    .subquery()
                )
                governing = (
                    select(
                        ranked.c.id,
                        ranked.c.member_id,
                        ranked.c.created_at,
                        ranked.c.is_active,
                        Workout.weekday,
                    )
                    .outerjoin(Workout, Workout.routine_id == ranked.c.id)
                    .where(ranked.c.rn == 1)
                )
                history.extend((await db.execute(union_all(recent, governing))).all())
        history.sort(key=lambda r: (r.member_id, r.created_at, r.id))
        spans_by_member: dict[int, list[tuple[date, set[int]]]] = {}
        with_routines: set[int] = set()
        last_routine_id: int | None = None
        for routine_id, member_id, created_at, is_active, weekday in history:
            spans = spans_by_member.setdefault(member_id, [])
            if routine_id != last_routine_id:
                spans.append((local_date(created_at, timezone), set()))
                last_routine_id = routine_id
                if is_active:
                    with_routines.add(member_id)
            if weekday is not None:
                spans[-1][1].add(weekday)
        today = local_date(self._clock(), timezone)
        yesterday = today - timedelta(days=1)
        roster_rows: list[RosterRow] = []
        lapsed_rows: list[RosterRow] = []
        for member, last_started in rows:
            spans = spans_by_member.get(member.id, [])
            if last_started is not None:
                # Any Session resets the count, even on an unplanned day.
                since = local_date(last_started, timezone) + timedelta(days=1)
            elif spans:
                # No grace period: counting starts the moment a Routine exists.
                since = spans[0][0]
            else:
                since = today  # no Routine, no Sessions: nothing to count
            row = RosterRow(
                member_id=member.id,
                name=member.name,
                gap_days=_gap_days(today, last_started, member.created_at, timezone),
                has_sessions=last_started is not None,
                is_new=member.id not in with_routines,
                snoozed_until=_active_snooze(member, today),
                missed_days=missed_planned_days(spans, since, yesterday),
            )
            (lapsed_rows if member.checkin_state == LAPSED else roster_rows).append(row)
        roster_rows.sort(key=lambda r: (-r.gap_days, r.name.lower()))
        lapsed_rows.sort(key=lambda r: (r.gap_days, r.name.lower()))
        return roster_rows, lapsed_rows

    async def attendance(self, gym_id: int, member_ids: list[int]) -> dict[int, list[DayCell]]:
        """The Cards day grid per Member: 4 weeks, Mon–Sun, one cell per day
        (issue #106, spec-dashboard §The roster / §Attendance).

        Each day is judged against the Routine active on that date — the
        same day-grained reconstruction as the severity engine: a scheduled
        Workout day with no Session is a miss, a Session on an unplanned day
        shows trained but never cancels a miss, and today never counts until
        it is over. The window is the current week plus the three before it,
        so the tail of the grid can hold future days.
        """
        async with self._sessions() as db:
            gym = await db.get(Gym, gym_id)
            timezone = (gym.timezone if gym else None) or "UTC"
            today = local_date(self._clock(), timezone)
            start = today - timedelta(days=today.weekday() + 7 * (GRID_WEEKS - 1))
            end = start + timedelta(days=7 * GRID_WEEKS - 1)
            grids: dict[int, list[DayCell]] = {member_id: [] for member_id in member_ids}
            if not member_ids:
                return grids
            # Sessions and Routines in a generous UTC window around the
            # gym-local grid; exact per-date judgement happens below in
            # gym-local dates.
            session_rows = (
                await db.execute(
                    select(Session.member_id, Session.started_at).where(
                        Session.gym_id == gym_id,
                        Session.member_id.in_(member_ids),
                        Session.started_at >= _utc_day(start - timedelta(days=2)),
                        Session.started_at <= _utc_day(end + timedelta(days=2)),
                    )
                )
            ).all()
            routine_rows = (
                await db.execute(
                    select(Routine.id, Routine.member_id, Routine.created_at, Workout.weekday)
                    .outerjoin(Workout, Workout.routine_id == Routine.id)
                    .where(
                        Routine.gym_id == gym_id,
                        Routine.member_id.in_(member_ids),
                        Routine.created_at <= _utc_day(end + timedelta(days=2)),
                    )
                    .order_by(Routine.member_id, Routine.created_at, Routine.id)
                )
            ).all()
        trained: dict[int, set[date]] = {}
        for member_id, started_at in session_rows:
            on = local_date(started_at, timezone)
            if start <= on <= today:
                trained.setdefault(member_id, set()).add(on)
        spans_by_member: dict[int, list[tuple[date, set[int]]]] = {}
        last_routine_id: int | None = None
        for routine_id, member_id, created_at, weekday in routine_rows:
            created_on = local_date(created_at, timezone)
            if created_on > end:
                continue
            spans = spans_by_member.setdefault(member_id, [])
            if routine_id != last_routine_id:
                spans.append((created_on, set()))
                last_routine_id = routine_id
            if weekday is not None:
                spans[-1][1].add(weekday)
        for member_id in member_ids:
            spans = spans_by_member.get(member_id, [])
            hits = trained.get(member_id, set())
            grids[member_id] = [
                DayCell(
                    day,
                    (
                        "future"
                        if day > today
                        else "hit"
                        if day in hits
                        else "miss"
                        if day < today and day.weekday() in planned_weekdays_on(spans, day)
                        else "plain"
                    ),
                )
                for day in (start + timedelta(days=i) for i in range(7 * GRID_WEEKS))
            ]
        return grids

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
            gap_days = _gap_days(today, last_started, member.created_at, timezone)

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
            snoozed_until=_active_snooze(member, today),
            routine=routine,
            routine_id=routine_dict["routine_id"] if routine_dict else None,
            coach_authored=routine_dict["coach_authored"] if routine_dict else False,
            routine_author=routine_dict["created_by_name"] if routine_dict else None,
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

    async def roster_member(self, gym_id: int, member_id: int) -> Member | None:
        """The roster-scoped Member the Routine editor may write to, or
        ``None`` — the same rule as ``member_page``: another Gym's Member, a
        coach-flagged Member, and a gym switch's ghost row (no live channel)
        all get the shared 404."""
        async with self._sessions() as db:
            return await db.scalar(
                select(Member)
                .join(MemberChannel, MemberChannel.member_id == Member.id)
                .where(
                    Member.id == member_id,
                    Member.gym_id == gym_id,
                    Member.is_coach.is_(False),
                )
            )

    async def save_routine_from_web(
        self,
        gym_id: int,
        member_id: int,
        coach_member_id: int,
        base_routine_id: int | None,
        workouts: list[Any],
    ) -> Routine:
        """The Routine editor's save: through the supersession machinery,
        coach-authored and actor-stamped; ``StaleRoutineError`` when the
        active Routine changed since the editor loaded."""
        return await self._routines.save_coach_routine(
            member_id,
            gym_id,
            coach_member_id,
            workouts,
            base_routine_id=base_routine_id,
        )

    async def member_channel(self, member_id: int) -> tuple[str, str] | None:
        """The Member's live channel as ``(channel, channel_user_id)`` — where
        the "your coach updated your Routine" notice is sent."""
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(MemberChannel.channel, MemberChannel.channel_user_id).where(
                        MemberChannel.member_id == member_id
                    )
                )
            ).first()
        return (row[0], row[1]) if row is not None else None

    async def catalog_exercises(self) -> list[str]:
        """Every catalog Exercise name, ordered — the editor's reference list
        (a web save draws from the catalog, it does not extend it)."""
        async with self._sessions() as db:
            return list(await db.scalars(select(Exercise.name).order_by(Exercise.name)))

    async def _sessions_sets(
        self, db, session_ids: list[int]
    ) -> dict[int, list[tuple[str, float | None, int, str | None]]]:
        """One page's sets in a single query (no per-Session round-trips),
        grouped by Session in logging order."""
        sets: dict[int, list[tuple[str, float | None, int, str | None]]] = {
            session_id: [] for session_id in session_ids
        }
        if not session_ids:
            return sets
        rows = (
            await db.execute(
                select(Set.session_id, Exercise.name, Set.weight, Set.reps, Set.note)
                .join(Exercise, Set.exercise_id == Exercise.id)
                .where(Set.session_id.in_(session_ids))
                .order_by(Set.id)
            )
        ).all()
        for session_id, name, weight, reps, note in rows:
            sets[session_id].append((name, weight, reps, note))
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
