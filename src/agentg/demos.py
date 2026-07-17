"""Exercise demo media storage and resolution (spec §Exercise demo media).

The soundless MP4 in our media store is the system of record; Telegram
file_ids are a disposable per-bot cache so later sends resend with no upload.
Resolution is "the Gym's own demo if it has one, else the Exercise default".
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.catalog import find_exercise, find_or_create_exercise, normalize_exercise_name
from agentg.models import DemoFileId, DemoOverride, Exercise


@dataclass(frozen=True)
class DemoRef:
    """A resolved demo: which MP4 to send and the cache scope for its file_id."""

    exercise_id: int
    exercise_name: str
    gym_id: int | None  # None = the Exercise default; set = a Gym override
    slug: str


class DemoStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def resolve(self, exercise_name: str, gym_id: int) -> DemoRef | None:
        """The demo to serve this Gym for an Exercise, or None if it has none."""
        async with self._sessions() as db:
            exercise = await find_exercise(db, normalize_exercise_name(exercise_name))
            if exercise is None:
                return None
            override = await db.scalar(
                select(DemoOverride).where(
                    DemoOverride.gym_id == gym_id, DemoOverride.exercise_id == exercise.id
                )
            )
            if override is not None:  # a Gym's own demo wins over the default
                return DemoRef(exercise.id, exercise.name, gym_id, override.demo_slug)
            if exercise.demo_slug is not None:
                return DemoRef(exercise.id, exercise.name, None, exercise.demo_slug)
            return None

    async def cached_file_id(self, exercise_id: int, gym_id: int | None, bot: str) -> str | None:
        async with self._sessions() as db:
            row = await self._cache_row(db, exercise_id, gym_id, bot)
            return row.file_id if row is not None else None

    async def cache_file_id(
        self,
        exercise_id: int,
        gym_id: int | None,
        bot: str,
        file_id: str,
        file_unique_id: str | None = None,
    ) -> None:
        """Lazily seed (or refresh) the file_id cache for a demo scope.

        The default scope keys on a NULL gym_id, which the UNIQUE constraint
        treats as distinct, so two Members of different Gyms racing the first
        send of the same default demo can each insert a row — self-healing
        (both hold valid ids; a media change drops all rows), at the cost of
        one wasted upload. Not worth a DB-specific upsert at this scale.
        """
        async with self._sessions() as db:
            row = await self._cache_row(db, exercise_id, gym_id, bot)
            if row is None:
                db.add(
                    DemoFileId(
                        exercise_id=exercise_id,
                        gym_id=gym_id,
                        bot=bot,
                        file_id=file_id,
                        file_unique_id=file_unique_id,
                    )
                )
            else:
                row.file_id = file_id
                row.file_unique_id = file_unique_id
            await db.commit()

    async def set_default_demo(self, exercise_name: str, slug: str) -> Exercise:
        """Point an Exercise at its canonical demo MP4, creating the Exercise if
        new (dataset ingest). Changing the media drops the stale default cache."""
        async with self._sessions() as db:
            exercise = await find_or_create_exercise(db, exercise_name)
            changed = exercise.demo_slug != slug
            exercise.demo_slug = slug
            if changed:
                await self._drop_cache(db, exercise.id, None)
            await db.commit()
            return exercise

    async def set_override(self, gym_id: int, exercise_name: str, slug: str) -> None:
        """Give a Gym its own demo for an Exercise. Changing it drops the stale
        override cache so the new media is uploaded on next send."""
        async with self._sessions() as db:
            exercise = await find_or_create_exercise(db, exercise_name)
            override = await db.scalar(
                select(DemoOverride).where(
                    DemoOverride.gym_id == gym_id, DemoOverride.exercise_id == exercise.id
                )
            )
            if override is None:
                db.add(DemoOverride(gym_id=gym_id, exercise_id=exercise.id, demo_slug=slug))
                await self._drop_cache(db, exercise.id, gym_id)
            elif override.demo_slug != slug:
                override.demo_slug = slug
                await self._drop_cache(db, exercise.id, gym_id)
            await db.commit()

    async def _cache_row(self, db, exercise_id: int, gym_id: int | None, bot: str) -> DemoFileId | None:
        query = select(DemoFileId).where(
            DemoFileId.exercise_id == exercise_id, DemoFileId.bot == bot
        )
        query = query.where(
            DemoFileId.gym_id.is_(None) if gym_id is None else DemoFileId.gym_id == gym_id
        )
        return await db.scalar(query)

    async def _drop_cache(self, db, exercise_id: int, gym_id: int | None) -> None:
        rows = await db.scalars(
            select(DemoFileId).where(
                DemoFileId.exercise_id == exercise_id,
                DemoFileId.gym_id.is_(None) if gym_id is None else DemoFileId.gym_id == gym_id,
            )
        )
        for row in rows:
            await db.delete(row)
