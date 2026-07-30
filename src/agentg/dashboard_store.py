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
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.models import DashboardLoginToken, Gym, Member

TOKEN_TTL = timedelta(minutes=10)
TOKEN_BYTES = 32

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


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
