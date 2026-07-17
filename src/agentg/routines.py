"""Routine generation storage (spec §Routine generation & coach overrides).

The LLM writes the plan; this store owns the rules doc it must follow and
the structured Routine/Workout/WorkoutExercise rows it saves — structure
only, exercises from the catalog, never target weights.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.catalog import find_or_create_exercise
from agentg.models import Gym, Routine, Workout, WorkoutExercise

# The rules doc that ships with the product. A Gym gets its own editable copy
# only if it wants different rules; the Agent follows exactly one doc. Plain
# text on purpose — a Coach edits it without touching code (ticket #30).
DEFAULT_RULES_DOC = """\
# Default gym coaching rules

## Programme
- Match the Member's training days: 2 days → full-body; 3 → push/pull/legs
  or upper/lower/full; 4 → upper/lower split; 5+ → a sensible body-part split.
- Pin each Workout to a specific weekday the Member named. Leave the other
  days as rest.
- Compound lifts first, isolation after. 4-7 exercises per Workout.
- Only prescribe exercises that exist in the Exercise catalog.

## Sets and reps (structure, never weights)
- Strength goal: 3-5 sets of 3-6 reps on the main lifts.
- General/hypertrophy goal: 3-4 sets of 8-12 reps.
- Endurance goal: 2-3 sets of 12-20 reps.
- Never prescribe a target weight — weights are derived from logged Sets.

## Injuries and limitations
- Respect every injury the Member volunteers: avoid movements that load an
  injured joint, and prefer a safer substitute from the catalog.

## Progression (used later when suggesting weights)
- Add 2.5 kg (or one small plate) once all prescribed sets hit the top of the
  rep range; deload ~10% after two stalled sessions.
- After a gap of 10+ days, offer ~10% lighter to ease back in.
"""

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ExerciseSpec:
    exercise: str
    sets: int | None = None
    reps: str | None = None


@dataclass(frozen=True)
class WorkoutSpec:
    weekday: int  # 0=Monday .. 6=Sunday
    name: str
    exercises: list[ExerciseSpec] = field(default_factory=list)


class RoutineStore:
    def __init__(self, engine: AsyncEngine, clock: Clock = _utcnow) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

    async def effective_rules_doc(self, gym_id: int) -> str:
        """The one doc that governs generation: the Gym's, else the default."""
        async with self._sessions() as db:
            gym = await db.get(Gym, gym_id)
            if gym is not None and gym.rules_doc:
                return gym.rules_doc
            return DEFAULT_RULES_DOC

    async def set_rules_doc(self, gym_id: int, text: str) -> None:
        async with self._sessions() as db:
            await db.execute(update(Gym).where(Gym.id == gym_id).values(rules_doc=text))
            await db.commit()

    async def save_routine(
        self, member_id: int, gym_id: int, workouts: list[WorkoutSpec]
    ) -> Routine:
        """Save a generated Routine, replacing the Member's active one.

        The old Routine row is kept, deactivated (history), so exactly one is
        active. Exercises resolve against the catalog; a novel movement is
        added rather than dropped.
        """
        async with self._sessions() as db:
            await db.execute(
                update(Routine)
                .where(Routine.member_id == member_id, Routine.is_active.is_(True))
                .values(is_active=False)
            )
            routine = Routine(gym_id=gym_id, member_id=member_id, is_active=True, created_at=self._clock())
            db.add(routine)
            await db.flush()
            for spec in workouts:
                workout = Workout(
                    gym_id=gym_id, routine_id=routine.id, weekday=spec.weekday, name=spec.name
                )
                db.add(workout)
                await db.flush()
                for position, exercise_spec in enumerate(spec.exercises):
                    exercise = await find_or_create_exercise(db, exercise_spec.exercise)
                    db.add(
                        WorkoutExercise(
                            gym_id=gym_id,
                            workout_id=workout.id,
                            exercise_id=exercise.id,
                            position=position,
                            sets=exercise_spec.sets,
                            reps=exercise_spec.reps,
                        )
                    )
            await db.commit()
            return routine

    async def active_routine(self, member_id: int) -> dict[str, Any] | None:
        async with self._sessions() as db:
            routine = await db.scalar(
                select(Routine).where(
                    Routine.member_id == member_id, Routine.is_active.is_(True)
                )
            )
            if routine is None:
                return None
            workouts = await self._workouts(db, routine.id)
            return {
                "routine_id": routine.id,
                "coach_authored": routine.coach_authored,
                "workouts": workouts,
            }

    async def workout_for_weekday(self, member_id: int, weekday: int) -> dict[str, Any] | None:
        return self._pick_weekday(await self.active_routine(member_id), weekday)

    async def todays_workout(self, member_id: int) -> dict[str, Any] | None:
        return await self.workout_for_weekday(member_id, self._today())

    def pick_todays_workout(self, routine: dict[str, Any] | None) -> dict[str, Any] | None:
        """Today's Workout from an already-loaded Routine — saves a re-query
        for callers (e.g. the snapshot) that hold the Routine already."""
        return self._pick_weekday(routine, self._today())

    def _today(self) -> int:
        # Weekday is UTC for now; gym-local day boundaries arrive with #31.
        return self._clock().weekday()

    @staticmethod
    def _pick_weekday(routine: dict[str, Any] | None, weekday: int) -> dict[str, Any] | None:
        if routine is None:
            return None
        for workout in routine["workouts"]:
            if workout["weekday"] == weekday:
                return workout
        return None

    async def _workouts(self, db: Any, routine_id: int) -> list[dict[str, Any]]:
        from agentg.models import Exercise  # local: only needed for the join label

        rows = (
            await db.execute(
                select(Workout, WorkoutExercise, Exercise.name)
                .join(WorkoutExercise, WorkoutExercise.workout_id == Workout.id, isouter=True)
                .join(Exercise, WorkoutExercise.exercise_id == Exercise.id, isouter=True)
                .where(Workout.routine_id == routine_id)
                .order_by(Workout.weekday, WorkoutExercise.position)
            )
        ).all()
        by_workout: dict[int, dict[str, Any]] = {}
        for workout, workout_exercise, exercise_name in rows:
            entry = by_workout.setdefault(
                workout.id,
                {"weekday": workout.weekday, "name": workout.name, "exercises": []},
            )
            if workout_exercise is not None:
                entry["exercises"].append(
                    {
                        "exercise": exercise_name,
                        "sets": workout_exercise.sets,
                        "reps": workout_exercise.reps,
                    }
                )
        return sorted(by_workout.values(), key=lambda w: w["weekday"])
