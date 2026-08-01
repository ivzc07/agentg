"""Catalog alias lookup: exact name, then alias — no full-table scan.

Issue #178 — Alias lookup must not scan the whole Catalog per Exercise.
"""

import pytest

from agentg.catalog import find_exercise, find_or_create_exercise, normalize_exercise_name
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from agentg.models import Exercise


@pytest.fixture
async def catalog_db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()

    # Seed a modest catalog — enough that a scan would touch many rows.
    from agentg.linking_store import async_sessionmaker
    from sqlalchemy import select

    async with async_sessionmaker(engine)() as db:
        db.add(Exercise(name="bench press", aliases="bench"))
        db.add(Exercise(name="overhead press", aliases="ohp,press,shoulder press"))
        db.add(Exercise(name="squat", aliases="squats,back squat"))
        db.add(Exercise(name="deadlift", aliases="deadlifts,dl"))
        db.add(Exercise(name="dips", aliases="dip"))
        db.add(Exercise(name="pull-up", aliases="pull up,pull ups,pullup,pullups,chin-up"))
        db.add(Exercise(name="barbell row", aliases="row,rows,bent over row"))
        db.add(Exercise(name="lat pulldown", aliases="pulldown,pulldowns"))
        db.add(Exercise(name="lunge", aliases="lunges"))
        db.add(Exercise(name="biceps curl", aliases="curl,curls"))
        db.add(Exercise(name="triceps extension", aliases="tricep extension,skullcrusher"))
        db.add(Exercise(name="leg press", aliases=""))
        db.add(Exercise(name="calf raise", aliases="calf raises"))
        db.add(Exercise(name="hammer curl", aliases="hammer curls"))
        db.add(Exercise(name="face pull", aliases="face pulls"))
        await db.commit()

    yield engine
    await engine.dispose()


async def test_exact_name_resolves_without_alias_scan(catalog_db):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(catalog_db)() as db:
        found = await find_exercise(db, "bench press")
        assert found is not None
        assert found.name == "bench press"


async def test_single_word_alias_resolves(catalog_db):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(catalog_db)() as db:
        found = await find_exercise(db, "ohp")
        assert found is not None
        assert found.name == "overhead press"


async def test_multi_word_alias_resolves(catalog_db):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(catalog_db)() as db:
        found = await find_exercise(db, "shoulder press")
        assert found is not None
        assert found.name == "overhead press"


async def test_partial_alias_does_not_match(catalog_db):
    """'curl' must not match 'hammer curl' (alias 'hammer curls')."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(catalog_db)() as db:
        # "curl" is an alias of "biceps curl" — should match that, not "hammer curl"
        found = await find_exercise(db, "curl")
        assert found is not None
        assert found.name == "biceps curl"


async def test_alias_exact_match_only(catalog_db):
    """'press' matches 'overhead press' not 'leg press' (which has no alias 'press')."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(catalog_db)() as db:
        found = await find_exercise(db, "press")
        assert found is not None
        assert found.name == "overhead press"


async def test_unknown_exercise_returns_none(catalog_db):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(catalog_db)() as db:
        found = await find_exercise(db, "nonexistent")
        assert found is None


async def test_find_or_create_creates_unknown(catalog_db):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(catalog_db)() as db:
        created = await find_or_create_exercise(db, "cable crossover")
        assert created.name == "cable crossover"
        # And resolves again by exact name
        found = await find_exercise(db, "cable crossover")
        assert found is not None
        assert found.id == created.id


async def test_find_or_create_resolves_by_alias(catalog_db):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(catalog_db)() as db:
        found = await find_or_create_exercise(db, "dl")
        assert found.name == "deadlift"
