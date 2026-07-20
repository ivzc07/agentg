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

from agentg.catalog import find_exercise, normalize_exercise_name
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

## Progression (the coach may edit these numbers)
- Add the increment once all prescribed sets hit the top of the rep range;
  deload after a stall; ease back after a long gap. The numbers below drive
  the weight suggestions — change them here, no code change needed.
- increment: 2.5
- deload_percent: 10
- stall_sessions: 2
- gap_deload_days: 10
- gap_deload_percent: 10

## Safety (coach-editable; the medical floor below it is not)
- Injuries are a hard avoid until cleared: never program a movement that loads
  an injured area, prefer a pain-free alternative, and when in doubt leave it
  out. The restriction stands until the Member says it has healed.
- Nutrition and supplement questions: decline politely and point them to their
  coach — you don't give diet or supplement advice.
- Steroids / PEDs: refuse outright, with a brief health warning.
- Rehab or treatment for an injury: refer to a physiotherapist. You program
  around an injury, never treatment for it.
- Disordered-eating red flags: respond warmly, refer to the coach and
  professional support, and never coach toward the harmful goal.
- Urgent symptoms (chest pain, fainting, severe or spreading pain): tell them
  to stop training now and seek emergency care.
- On any of these, and on a new injury or new pain, offer to flag it to their
  coach — share only if they say yes.
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
        self,
        member_id: int,
        gym_id: int,
        workouts: list[WorkoutSpec],
        *,
        coach_authored: bool = False,
    ) -> Routine:
        """Save a Routine, replacing the Member's active one.

        The old Routine row is kept, deactivated (history), so exactly one is
        active. Exercises must already exist in the catalog — generation draws
        from it, it does not extend it (spec §Routine generation).

        Generation (``coach_authored=False``) never overwrites a coach-written
        Routine; a Coach hand-writing one (``coach_authored=True``) may replace
        anything and flags the result coach-authored.

        Raises ``ValueError`` naming any exercises not in the catalog, or if
        generation would overwrite a Coach's Routine.
        """
        async with self._sessions() as db:
            resolved: dict[str, int] = {}
            unknown: list[str] = []
            for spec in workouts:
                for exercise_spec in spec.exercises:
                    norm = normalize_exercise_name(exercise_spec.exercise)
                    if norm in resolved or norm in unknown:
                        continue
                    found = await find_exercise(db, norm)
                    if found is None:
                        unknown.append(exercise_spec.exercise)
                    else:
                        resolved[norm] = found.id
            if unknown:
                raise ValueError("not in the exercise catalog: " + ", ".join(unknown))

            active = await db.scalar(
                select(Routine).where(
                    Routine.member_id == member_id, Routine.is_active.is_(True)
                )
            )
            if active is not None and active.coach_authored and not coach_authored:
                raise ValueError(
                    "this Member has a coach-written Routine; only the Coach can change it"
                )
            if active is not None:
                active.is_active = False

            routine = Routine(
                gym_id=gym_id,
                member_id=member_id,
                is_active=True,
                coach_authored=coach_authored,
                created_at=self._clock(),
            )
            db.add(routine)
            await db.flush()
            for spec in workouts:
                workout = Workout(
                    gym_id=gym_id, routine_id=routine.id, weekday=spec.weekday, name=spec.name
                )
                db.add(workout)
                await db.flush()
                for position, exercise_spec in enumerate(spec.exercises):
                    db.add(
                        WorkoutExercise(
                            gym_id=gym_id,
                            workout_id=workout.id,
                            exercise_id=resolved[normalize_exercise_name(exercise_spec.exercise)],
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

    async def weekday_workout_names(self, member_id: int) -> dict[int, str]:
        """Map each pinned weekday (0=Mon) to its Workout name — the pinned
        training days the check-in sweep reasons over."""
        routine = await self.active_routine(member_id)
        if routine is None:
            return {}
        return {w["weekday"]: w["name"] for w in routine["workouts"]}

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
