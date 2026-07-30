"""Gym-local day boundaries (issue #95).

A Session logged in the local evening can fall after UTC midnight, so any
"today" or Gap day count derived in UTC lands on the wrong day. Every day
boundary that feeds Gap honours ``Gym.timezone`` instead.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def gym_zone(timezone: str) -> tzinfo:
    """The Gym's IANA zone; an unknown name falls back to UTC."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def local_date(moment: datetime, timezone: str) -> date:
    """The calendar date of ``moment`` in the Gym's timezone.

    A naive ``moment`` is read as UTC (SQLite drops tzinfo), and an unknown
    timezone falls back to UTC — the same fallback the check-in sweep uses.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(gym_zone(timezone)).date()
