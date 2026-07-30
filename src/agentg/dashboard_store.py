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
from typing import Any

from sqlalchemy import ColumnElement, Row, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.checkin import LAPSED, SNOOZED
from agentg.models import (
    DashboardLoginToken,
    Gym,
    Member,
    MemberChannel,
    Routine,
    Session,
    Workout,
)
from agentg.timezones import local_date

TOKEN_TTL = timedelta(minutes=10)
TOKEN_BYTES = 32

# Severity colouring (issue #98, spec-dashboard §The roster): consecutive
# missed planned Workout days since the last Session — amber at one, red at
# three, never a fixed day-count threshold.
SEVERITY_AMBER_AT = 1
SEVERITY_RED_AT = 3

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


class DashboardStore:
    def __init__(self, engine: AsyncEngine, clock: Clock = _utcnow) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

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
            # the last Session, so the read is pruned: every Routine created
            # after the oldest anchor (plus all of a Session-less Member's —
            # their count starts at the first Routine), and per Member the
            # single latest Routine created on or before it, which governs
            # the window's first dates. The newest Routine per Member is
            # always in the read, so the active-Routine flags come along free.
            member_ids = [member.id for member, _ in rows]
            anchors = {member.id: started for member, started in rows if started is not None}
            history: list[Row[Any]] = []
            if member_ids:
                oldest_anchor = min(anchors.values()) if anchors else None
                in_window: ColumnElement[bool] = Routine.member_id.in_(
                    [member.id for member, started in rows if started is None]
                )
                if oldest_anchor is not None:
                    in_window = or_(in_window, Routine.created_at > oldest_anchor)
                history.extend(
                    (
                        await db.execute(
                            select(
                                Routine.id,
                                Routine.member_id,
                                Routine.created_at,
                                Routine.is_active,
                                Workout.weekday,
                            )
                            .outerjoin(Workout, Workout.routine_id == Routine.id)
                            .where(
                                Routine.gym_id == gym_id,
                                Routine.member_id.in_(member_ids),
                                in_window,
                            )
                        )
                    ).all()
                )
                if oldest_anchor is not None:
                    ranked = (
                        select(
                            Routine.id,
                            Routine.member_id,
                            Routine.created_at,
                            Routine.is_active,
                            func.row_number()
                            .over(
                                partition_by=Routine.member_id,
                                order_by=Routine.created_at.desc(),
                            )
                            .label("rn"),
                        )
                        .where(
                            Routine.gym_id == gym_id,
                            Routine.member_id.in_(list(anchors)),
                            Routine.created_at <= oldest_anchor,
                        )
                        .subquery()
                    )
                    history.extend(
                        (
                            await db.execute(
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
                        ).all()
                    )
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
                missed_days=missed_planned_days(spans, since, yesterday),
            )
            (lapsed_rows if member.checkin_state == LAPSED else roster_rows).append(row)
        roster_rows.sort(key=lambda r: (-r.gap_days, r.name.lower()))
        lapsed_rows.sort(key=lambda r: (r.gap_days, r.name.lower()))
        return roster_rows, lapsed_rows
