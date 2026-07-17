"""The proactive check-in sweep (spec §Proactive check-ins).

Runs in-process on a schedule. For each Member it computes the gym-local time,
asks the pure decision layer whether to nudge or wind down, sends through a
channel-agnostic notifier, and records the outcome. Channel-agnostic: the
Telegram adapter supplies the notifier (ADR 0001).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentg.checkin import CheckinData, decide_checkin
from agentg.checkin_store import CheckinStore, SweepRow
from agentg.routines import RoutineStore
from agentg.training import TrainingStore

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(self, channel: str, channel_user_id: str, text: str) -> None: ...


def _previous_pinned_weekday(today: date, pinned: frozenset[int]) -> int | None:
    for back in range(1, 8):
        weekday = date.fromordinal(today.toordinal() - back).weekday()
        if weekday in pinned:
            return weekday
    return None


async def _build_data(
    row: SweepRow, now_local: datetime, training: TrainingStore, routines: RoutineStore
) -> CheckinData:
    names = await routines.weekday_workout_names(row.member_id)
    pinned = frozenset(names)
    today = now_local.date()
    missed_weekday = _previous_pinned_weekday(today, pinned) if pinned else None
    return CheckinData(
        state=row.state,
        snoozed_until=row.snoozed_until,
        last_nudge_on=row.last_nudge_on,
        nudges_this_week=row.nudges_this_week,
        ignored_nudges=row.ignored_nudges,
        last_session_date=await training.newest_session_date(row.member_id),
        signup_date=row.signup_date,
        pinned_weekdays=pinned,
        missed_workout=names.get(missed_weekday) if missed_weekday is not None else None,
        missed_weekday=missed_weekday,
        todays_workout=names.get(today.weekday()),
    )


def _gym_now(row: SweepRow, now_utc: datetime) -> datetime:
    try:
        return now_utc.astimezone(ZoneInfo(row.timezone))
    except (ZoneInfoNotFoundError, ValueError):  # a bad tz falls back to UTC
        logger.warning("member %s has unknown timezone %r; using UTC", row.member_id, row.timezone)
        return now_utc.astimezone(ZoneInfo("UTC"))


async def run_sweep(
    now_utc: datetime,
    checkin_store: CheckinStore,
    training: TrainingStore,
    routines: RoutineStore,
    notifier: Notifier,
) -> int:
    """Run one pass. Returns how many proactive messages were sent."""
    sent = 0
    for row in await checkin_store.sweep_rows():
        now_local = _gym_now(row, now_utc)
        today = now_local.date()

        # Clear an expired snooze so state reflects reality going forward.
        if row.state == "snoozed" and row.snoozed_until is not None and row.snoozed_until <= today:
            await checkin_store.wake_from_snooze(row.member_id)

        data = await _build_data(row, now_local, training, routines)
        decision = decide_checkin(now_local, data)
        if decision.action == "none" or decision.message is None:
            continue
        try:
            await notifier.send(row.channel, row.channel_user_id, decision.message)
        except Exception:
            logger.exception("failed to send check-in to member %s", row.member_id)
            continue
        sent += 1
        # Best-effort: a crash between the send and the record could re-nudge
        # next day. The frequency cap bounds the blast radius (never worse than
        # one extra nudge), so no idempotency key is warranted at this scale.
        if decision.action == "winddown":
            await checkin_store.lapse(row.member_id)
        else:
            await checkin_store.record_nudge(row.member_id, today)
    return sent
