"""Exercise-catalog resolution, shared by the training and routine stores.

Members log against it and Routines are drawn from it, so name matching
(exact name, then alias) lives in one place. A reported movement that
matches nothing is added, never dropped — the catalog is a growing product
fact, not a fixed enum.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentg.models import Exercise


def normalize_exercise_name(text: str) -> str:
    return " ".join(text.strip().lower().split())


async def resolve_exercise_names(
    db: AsyncSession, names: list[str]
) -> dict[str, int]:
    """Resolve a batch of exercise names (or aliases) to ids.

    Returns a dict mapping each input name to its resolved Exercise id.
    Unknown exercises are silently omitted — the caller decides how to
    handle them.
    """
    normalized = [(name, normalize_exercise_name(name)) for name in names]
    all_exercises = await db.scalars(select(Exercise))
    all_exercises = list(all_exercises)

    result: dict[str, int] = {}
    for original, norm in normalized:
        for row in all_exercises:
            if row.name == norm:
                result[original] = row.id
                break
        else:
            for row in all_exercises:
                if norm in [a for a in row.aliases.split(",") if a]:
                    result[original] = row.id
                    break
    return result


async def find_exercise(db: AsyncSession, norm: str) -> Exercise | None:
    """Resolve an Exercise by exact name, then by comma-separated alias.

    Aliases are matched with comma boundary guards so a search for 'curl'
    never falsely hits 'hammer curls' (issue #178)."""
    return await db.scalar(
        select(Exercise).where(
            or_(
                Exercise.name == norm,
                func.concat(',', Exercise.aliases, ',').contains(',' + norm + ',', autoescape=True),
            )
        )
    )


async def find_or_create_exercise(db: AsyncSession, name: str) -> Exercise:
    norm = normalize_exercise_name(name)
    found = await find_exercise(db, norm)
    if found is not None:
        return found
    created = Exercise(name=norm, aliases="")
    db.add(created)
    await db.flush()
    return created
