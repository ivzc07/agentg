"""Turn logged Sets + the rules doc into per-Exercise weight suggestions.

Derives suggestions for today's Workout (spec §Routine adaptation): reads
the Gym's progression rules and each Exercise's recent completion history,
then defers the arithmetic to the pure ``progression`` module. Reads only —
suggestions are ephemeral, never written to Routine/Workout rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentg.progression import (
    SessionResult,
    parse_progression_rules,
    parse_top_reps,
    suggest_weight,
)
from agentg.routines import RoutineStore
from agentg.training import TrainingStore

_UNSET_ROUTINE: Any = object()


@dataclass(frozen=True)
class ExerciseSuggestion:
    exercise: str
    last_weight: float | None
    suggested_weight: float | None
    action: str  # increment | hold | deload | gap_deload | none
    reason: str


def _completed(
    top_reps: list[int], target_sets: int | None, target_top_reps: int | None
) -> bool | None:
    """Did a Session complete an Exercise — all prescribed sets at the top of
    the rep range? ``None`` when the prescription can't tell us (no rep target,
    or nothing logged), so the suggester holds rather than pushing OR deloading."""
    if target_sets is None or target_top_reps is None or not top_reps:
        return None
    return len(top_reps) >= target_sets and min(top_reps) >= target_top_reps


async def suggest_for_today(
    training: TrainingStore,
    routines: RoutineStore,
    member_id: int,
    gym_id: int,
    timezone: str = "UTC",
    *,
    routine: Any = _UNSET_ROUTINE,
) -> list[ExerciseSuggestion]:
    """Weight suggestions for each Exercise in today's Workout (empty on a
    rest day or with no Routine).

    When *routine* is provided (the pre-loaded active Routine from the
    per-turn cache), today's Workout is derived from it without a re-query.
    ``None`` means the cache was consulted and there is no Routine — no
    re-query.  Omit the argument to fall back to ``todays_workout``."""
    if routine is not _UNSET_ROUTINE:
        workout = routines.pick_todays_workout(routine, timezone)
    else:
        workout = await routines.todays_workout(member_id, timezone)
    if workout is None:
        return []
    rules = parse_progression_rules(await routines.effective_rules_doc(gym_id))
    gap_days, _last = await training.latest_session_info(member_id)

    exercise_names = [ex["exercise"] for ex in workout["exercises"]]
    limit = rules.stall_sessions + 1
    histories = await training.exercise_history_batch(member_id, exercise_names, limit)

    suggestions: list[ExerciseSuggestion] = []
    for exercise in workout["exercises"]:
        target_top = parse_top_reps(exercise.get("reps"))
        rows = histories.get(exercise["exercise"], [])
        history = [
            SessionResult(
                weight=row["top_weight"],
                completed=_completed(row["top_reps"], exercise.get("sets"), target_top),
            )
            for row in rows
        ]
        result = suggest_weight(history, gap_days, rules)
        suggestions.append(
            ExerciseSuggestion(
                exercise=exercise["exercise"],
                last_weight=history[0].weight if history else None,
                suggested_weight=result.suggested_weight,
                action=result.action,
                reason=result.reason,
            )
        )
    return suggestions
