"""Deterministic proactive check-in decisions (spec §Proactive check-ins).

Pure logic: from a Member's check-in state, last Session, Routine pinned days,
and the gym-local time, decide whether to nudge, wind down, or stay quiet.
Warm and sparing — the copy here is the canonical zero-guilt tone. The sweep
(checkin_sweep.py) wires this to the database and the channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

# All times gym-local. Proactive sends fire at 09:00; hard quiet hours
# 21:00–08:00 (nothing proactive, ever).
SEND_HOUR = 9
GAP_DAYS_FALLBACK = 3  # a Routine-less Member is nudged once this idle
WEEKLY_CAP = 2  # at most this many nudges per calendar week
GIVE_UP_NUDGES = 4  # this many ignored ≈ two weeks → wind down and lapse

ON, OFF, SNOOZED, LAPSED = "on", "off", "snoozed", "lapsed"

WEEKDAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

# Canonical copy (spec §Proactive check-ins / #8 resolution): warm, direct,
# zero guilt. Spanish — the check-in has no incoming message to mirror, and
# Spanish is the default (ADR 0002).
GAP_NUDGE = "Ya van {days} días — ¿tienes chance de una sesión hoy?"
PINNED_NUDGE = "Te saltaste {missed} — ¿lo retomamos hoy?"
PINNED_NUDGE_WITH_TODAY = "Te saltaste {missed} — ¿lo retomamos hoy? Ya tengo listo tu {today}."
WINDDOWN = "Dejaré de escribirte por ahora. Escríbeme cuando quieras y lo retomamos al instante."


@dataclass(frozen=True)
class CheckinData:
    state: str  # on / off / snoozed / lapsed
    snoozed_until: date | None
    last_nudge_on: date | None
    nudges_this_week: int
    ignored_nudges: int
    last_session_date: date | None
    signup_date: date  # the fallback gap anchor before any Session exists
    pinned_weekdays: frozenset[int]  # Routine training days (empty = no Routine)
    missed_workout: str | None = None  # name of the skipped Workout, for the copy
    missed_weekday: int | None = None  # weekday of the skipped Workout (0=Mon)
    todays_workout: str | None = None  # name of today's Workout, for the copy


@dataclass(frozen=True)
class CheckinDecision:
    action: str  # none / nudge / winddown
    message: str | None = None
    nudge_type: str | None = None  # missed_pinned_day / gap


_NONE = CheckinDecision("none")


def _active(state: str, snoozed_until: date | None, today: date) -> bool:
    """Whether check-ins are live: on, or a snooze that has expired."""
    if state in (OFF, LAPSED):
        return False
    if state == SNOOZED:
        return snoozed_until is not None and snoozed_until <= today
    return True


def _last_activity(data: CheckinData) -> date:
    return data.last_session_date or data.signup_date


def _previous_pinned_day(today: date, pinned: frozenset[int]) -> date | None:
    """The most recent pinned weekday strictly before today (within a week)."""
    for back in range(1, 8):
        day = date.fromordinal(today.toordinal() - back)
        if day.weekday() in pinned:
            return day
    return None


def _is_due(now_local: datetime, data: CheckinData) -> tuple[bool, str]:
    """Is a nudge warranted today (before the frequency cap)? Returns the
    nudge type when so."""
    today = now_local.date()
    if data.pinned_weekdays:
        # Routine-aware: on a pinned-day morning, nudge if the previous pinned
        # day was skipped (no Session on or after it).
        if today.weekday() not in data.pinned_weekdays:
            return False, ""
        previous = _previous_pinned_day(today, data.pinned_weekdays)
        if previous is None:
            return False, ""
        trained = data.last_session_date is not None and data.last_session_date >= previous
        return (not trained), "missed_pinned_day"
    # Fallback: flat gap since the last Session (or signup).
    gap = (today - _last_activity(data)).days
    return gap >= GAP_DAYS_FALLBACK, "gap"


def _capped(data: CheckinData, today: date) -> bool:
    """True when the frequency cap forbids a nudge today."""
    last = data.last_nudge_on
    if last is not None:
        if last == today:  # already nudged today
            return True
        if (today - last).days == 1:  # never on consecutive days
            return True
        same_week = last.isocalendar()[:2] == today.isocalendar()[:2]
        if same_week and data.nudges_this_week >= WEEKLY_CAP:
            return True
    return False


def _gap_message(data: CheckinData, today: date) -> str:
    return GAP_NUDGE.format(days=(today - _last_activity(data)).days)


def _pinned_message(data: CheckinData) -> str:
    missed = data.missed_workout or "tu última sesión"
    if data.missed_weekday is not None:  # "Te saltaste Piernas lunes" — matches the canonical copy
        missed = f"{missed} {WEEKDAY_NAMES[data.missed_weekday]}"
    if data.todays_workout:
        return PINNED_NUDGE_WITH_TODAY.format(missed=missed, today=data.todays_workout)
    return PINNED_NUDGE.format(missed=missed)


def decide_checkin(now_local: datetime, data: CheckinData) -> CheckinDecision:
    today = now_local.date()
    if not _active(data.state, data.snoozed_until, today):
        return _NONE
    if now_local.hour != SEND_HOUR:  # only the 09:00 tick; quiet hours are absolute
        return _NONE

    due, nudge_type = _is_due(now_local, data)
    if not due or _capped(data, today):
        return _NONE

    if data.ignored_nudges >= GIVE_UP_NUDGES:  # ~2 weeks ignored → wind down, lapse
        return CheckinDecision("winddown", message=WINDDOWN)

    message = _pinned_message(data) if nudge_type == "missed_pinned_day" else _gap_message(data, today)
    return CheckinDecision("nudge", message=message, nudge_type=nudge_type)
