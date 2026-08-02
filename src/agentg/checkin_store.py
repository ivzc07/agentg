"""Persistence for proactive check-ins (spec §Proactive check-ins).

Owns the check-in columns on ``members``: the per-Member state, the nudge
bookkeeping the frequency cap and give-up rule read, and the reset a reply or
Session triggers. The pure decision lives in ``checkin``; the sweep in
``checkin_sweep`` composes this with the training and routine stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.checkin import LAPSED, OFF, ON, SNOOZED, WEEKLY_CAP
from agentg.models import Gym, Member, MemberChannel
from agentg.timezones import local_date


@dataclass(frozen=True)
class SweepRow:
    """One Member the sweep considers, with the channel to reach them on."""

    member_id: int
    gym_id: int
    timezone: str
    channel: str
    channel_user_id: str
    state: str
    snoozed_until: date | None
    last_nudge_on: date | None
    nudges_this_week: int
    ignored_nudges: int
    signup_date: date


class CheckinStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def sweep_rows(self) -> list[SweepRow]:
        """Every Member reachable on a channel, with their check-in state.

        Off and lapsed Members are included; the decision layer filters them —
        keeping the query dumb and the policy in one place.
        """
        async with self._sessions() as db:
            rows = (
                await db.execute(
                    select(Member, Gym.timezone, MemberChannel.channel, MemberChannel.channel_user_id)
                    .join(Gym, Member.gym_id == Gym.id)
                    .join(MemberChannel, MemberChannel.member_id == Member.id)
                    .order_by(Member.id)
                )
            ).all()
        return [
            SweepRow(
                member_id=member.id,
                gym_id=member.gym_id,
                timezone=timezone or "UTC",
                channel=channel,
                channel_user_id=channel_user_id,
                state=member.checkin_state,
                snoozed_until=member.snoozed_until,
                last_nudge_on=member.last_nudge_on,
                nudges_this_week=member.nudges_this_week,
                ignored_nudges=member.ignored_nudges,
                # gym-local (issue #95): this anchors the fallback gap, which
                # the decision layer measures against gym-local days
                signup_date=local_date(member.created_at, timezone or "UTC"),
            )
            for member, timezone, channel, channel_user_id in rows
        ]

    async def record_nudge(self, member_id: int, today: date) -> None:
        """Log a sent nudge: stamp today, roll the weekly count, count it as
        ignored until a reply or Session clears it."""
        async with self._sessions() as db:
            member = await db.get(Member, member_id)
            if member is None:
                return
            same_week = (
                member.last_nudge_on is not None
                and member.last_nudge_on.isocalendar()[:2] == today.isocalendar()[:2]
            )
            member.nudges_this_week = (member.nudges_this_week + 1) if same_week else 1
            member.last_nudge_on = today
            member.ignored_nudges += 1
            await db.commit()

    async def lapse(self, member_id: int) -> None:
        await self._set(member_id, checkin_state=LAPSED)

    async def wake_from_snooze(self, member_id: int) -> None:
        """Flip an expired snooze back to on (the decision already treats it so)."""
        await self._set(member_id, checkin_state=ON, snoozed_until=None)

    async def reset_rhythm(self, member_id: int) -> None:
        """A reply or logged Session resets the cadence and revives a lapsed
        Member — 'message me whenever, we'll pick it right back up'."""
        async with self._sessions() as db:
            member = await db.get(Member, member_id)
            if member is None:
                return
            if member.ignored_nudges == 0 and member.checkin_state != LAPSED:
                return  # nothing to change — avoid a write on every message
            member.ignored_nudges = 0
            if member.checkin_state == LAPSED:
                member.checkin_state = ON
            await db.commit()

    async def turn_off(self, member_id: int) -> None:
        await self._set(member_id, checkin_state=OFF, snoozed_until=None)

    async def snooze_until(self, member_id: int, until: date) -> None:
        await self._set(member_id, checkin_state=SNOOZED, snoozed_until=until)

    async def resume(self, member_id: int) -> None:
        await self._set(member_id, checkin_state=ON, snoozed_until=None)

    async def get_state(self, member_id: int) -> tuple[str, date | None]:
        async with self._sessions() as db:
            member = await db.get(Member, member_id)
            if member is None:
                return ON, None
            return member.checkin_state, member.snoozed_until

    async def _set(self, member_id: int, **values: object) -> None:
        async with self._sessions() as db:
            await db.execute(update(Member).where(Member.id == member_id).values(**values))
            await db.commit()


# Re-exported so callers don't reach past the store for the cap constant.
__all__ = ["CheckinStore", "SweepRow", "WEEKLY_CAP"]
