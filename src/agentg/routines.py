"""Routine generation storage (spec §Routine generation & coach overrides).

The LLM writes the plan; this store owns the rules doc it must follow and
the structured Routine/Workout/WorkoutExercise rows it saves — structure
only, exercises from the catalog, never target weights.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentg.catalog import find_exercise, normalize_exercise_name
from agentg.models import Gym, Member, Routine, RoutinePreset, Workout, WorkoutExercise
from agentg.timezones import local_date

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
- Strength goal (fuerza): 3-5 sets of 3-6 reps on the main lifts.
- General/hypertrophy goal (hipertrofia / masa muscular): 3-4 sets of 8-12 reps.
- Endurance goal (resistencia): 2-3 sets of 12-20 reps.
- Name the goal in the Member's language when you talk about it — the Spanish
  terms above are there so a Spanish-speaking Member never hears the English.
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
- On any of these, and on a new injury or new pain, flag it to the coach
  with flag_to_coach — every flag is logged and the coaches are pinged, no
  consent ask.
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


class StaleRoutineError(ValueError):
    """The active Routine changed since the editor loaded it — the save is
    refused so a web edit never silently erases a fresher write
    (spec-dashboard §Routines & Presets)."""


class UnknownExercisesError(ValueError):
    """Exercises the catalog doesn't have (spec: generation draws from the
    catalog, it does not extend it). Carries the offending names so each
    surface phrases its own error — the Agent's tool message in English for
    the model, the dashboard's in Spanish for the Coach."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        super().__init__(
            "not in the exercise catalog: "
            + ", ".join(names)
            + " — call list_exercises and pick exact catalog names"
        )


class DuplicatePresetNameError(ValueError):
    """A Gym already has a Preset with this reserved name (issue #102)."""


class NoPresetMasterError(ValueError):
    """A Preset cannot be applied until its Coach has saved a master."""


@dataclass(frozen=True)
class AppliedCopy:
    """The saved structure for one Member after applying a Preset."""

    member_id: int
    workouts: list[WorkoutSpec]
    routine_id: int


class _ReplaceActive:
    """Sentinel for ``_save``'s replace semantics (agent chat saves):
    whatever is active gets superseded, subject to the coach-authored
    guard. Coach web saves instead pass the exact active Routine the editor
    loaded — or ``None`` for "none may exist" — and are refused otherwise."""


REPLACE_ACTIVE = _ReplaceActive()


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
        created_by_member_id: int | None = None,
    ) -> Routine:
        """Save a Routine, replacing the Member's active one.

        The old Routine row is kept, deactivated (history), so exactly one is
        active. Exercises must already exist in the catalog — generation draws
        from it, it does not extend it (spec §Routine generation).

        Generation (``coach_authored=False``) never overwrites a coach-written
        Routine; a Coach hand-writing one (``coach_authored=True``) may replace
        anything and flags the result coach-authored, stamped with who wrote
        it (``created_by_member_id``; NULL means the Agent wrote it).

        Raises ``ValueError`` naming any exercises not in the catalog, or if
        generation would overwrite a Coach's Routine; ``StaleRoutineError``
        if a concurrent save won the one-active-per-Member index mid-save.
        """
        async with self._sessions() as db:
            try:
                preset_id = None
                if not coach_authored:
                    active = await db.scalar(
                        select(Routine).where(
                            Routine.member_id == member_id,
                            Routine.is_active.is_(True),
                        )
                    )
                    gym = await db.get(Gym, gym_id)
                    if active is None and gym is not None and gym.default_preset_id is not None:
                        preset = await db.scalar(
                            select(RoutinePreset).where(
                                RoutinePreset.id == gym.default_preset_id,
                                RoutinePreset.gym_id == gym_id,
                                RoutinePreset.retired_at.is_(None),
                            )
                        )
                        master = await db.scalar(
                            select(Routine).where(
                                Routine.preset_id == gym.default_preset_id,
                                Routine.member_id.is_(None),
                                Routine.is_active.is_(True),
                            )
                        ) if preset is not None else None
                        if master is not None:
                            workouts = self._specs_from_workouts(
                                await self._workouts(db, master.id)
                            )
                            preset_id = master.preset_id
                            coach_authored = True
                            created_by_member_id = master.created_by_member_id
                return await self._save(
                    db,
                    member_id,
                    gym_id,
                    workouts,
                    coach_authored=coach_authored,
                    created_by_member_id=created_by_member_id,
                    expected_active_id=REPLACE_ACTIVE,
                    preset_id=preset_id,
                )
            except IntegrityError:
                # Another save (a Coach's web save) committed between this
                # save's read and its insert — surface a structured error the
                # caller can recover from, never a raw IntegrityError.
                raise StaleRoutineError(
                    "the Member's Routine changed while saving — reload it and try again"
                ) from None

    async def save_coach_routine(
        self,
        member_id: int,
        gym_id: int,
        coach_member_id: int,
        workouts: list[WorkoutSpec],
        *,
        base_routine_id: int | None,
    ) -> Routine:
        """A Coach's web save (issue #100): the same supersession machinery,
        always coach-authored and actor-stamped — so the Agent never
        restructures the result.

        ``base_routine_id`` is the active Routine the editor loaded (``None``
        when there was none). The expectation is enforced at write time
        inside ``_save``: an exact-id conditional deactivation, or a strict
        no-active-may-exist check — a save that landed since the editor
        loaded is always refused with ``StaleRoutineError`` and never
        silently deactivated, and the one-active-per-Member partial unique
        index backs the whole thing at the database level.
        """
        async with self._sessions() as db:
            try:
                return await self._save(
                    db,
                    member_id,
                    gym_id,
                    workouts,
                    coach_authored=True,
                    created_by_member_id=coach_member_id,
                    expected_active_id=base_routine_id,
                )
            except IntegrityError:
                # A save that committed between our check and our insert hit
                # the one-active-per-Member index first — refuse, don't crash.
                raise StaleRoutineError(
                    "the active Routine changed since the editor loaded it"
                ) from None

    async def create_preset(self, gym_id: int, name: str) -> RoutinePreset:
        """Create a live Preset identity; its name remains reserved after retirement."""
        name = name.strip()
        if not name:
            raise ValueError("Preset name cannot be empty")
        if len(name) > 100:
            raise ValueError("Preset name cannot exceed 100 characters")
        async with self._sessions() as db:
            db.add(
                RoutinePreset(
                    gym_id=gym_id,
                    name=name,
                    created_at=self._clock(),
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise DuplicatePresetNameError(
                    "a Preset with this name already exists in the Gym"
                ) from None
            return await db.scalar(
                select(RoutinePreset).where(
                    RoutinePreset.gym_id == gym_id, RoutinePreset.name == name
                )
            )

    async def presets(self, gym_id: int) -> list[RoutinePreset]:
        """Live Presets for a Gym, ordered by their reserved name."""
        async with self._sessions() as db:
            return list(
                await db.scalars(
                    select(RoutinePreset)
                    .where(
                        RoutinePreset.gym_id == gym_id,
                        RoutinePreset.retired_at.is_(None),
                    )
                    .order_by(RoutinePreset.name)
                )
            )

    async def preset_ids_with_masters(self, gym_id: int) -> set[int]:
        """Return the set of live Preset ids that have an active master Routine."""
        async with self._sessions() as db:
            ids = await db.scalars(
                select(Routine.preset_id)
                .join(RoutinePreset, Routine.preset_id == RoutinePreset.id)
                .where(
                    RoutinePreset.gym_id == gym_id,
                    RoutinePreset.retired_at.is_(None),
                    Routine.member_id.is_(None),
                    Routine.is_active.is_(True),
                )
            )
            return set(ids)

    async def set_default_preset(self, gym_id: int, preset_id: int | None) -> None:
        """Set or clear the Gym's one live Preset default (issue #103)."""
        async with self._sessions() as db:
            gym = await db.get(Gym, gym_id)
            if gym is None:
                raise ValueError("unknown Gym")
            if preset_id is not None:
                preset = await db.scalar(
                    select(RoutinePreset).where(
                        RoutinePreset.id == preset_id,
                        RoutinePreset.gym_id == gym_id,
                        RoutinePreset.retired_at.is_(None),
                    )
                )
                if preset is None:
                    raise ValueError("unknown Preset")
            gym.default_preset_id = preset_id
            await db.commit()

    async def retire_preset(self, gym_id: int, preset_id: int) -> None:
        """Retire a Preset and clear its Gym default without touching copies."""
        async with self._sessions() as db:
            preset = await db.scalar(
                select(RoutinePreset).where(
                    RoutinePreset.id == preset_id,
                    RoutinePreset.gym_id == gym_id,
                    RoutinePreset.retired_at.is_(None),
                )
            )
            if preset is None:
                raise ValueError("unknown Preset")
            gym = await db.get(Gym, gym_id)
            preset.retired_at = self._clock()
            if gym is not None and gym.default_preset_id == preset_id:
                gym.default_preset_id = None
            await db.commit()

    async def save_preset_master(
        self,
        preset_id: int,
        gym_id: int,
        coach_member_id: int,
        workouts: list[WorkoutSpec],
        *,
        base_routine_id: int | None,
    ) -> Routine:
        """Save a Preset's Member-less master in that Preset's scope."""
        async with self._sessions() as db:
            preset = await db.scalar(
                select(RoutinePreset).where(
                    RoutinePreset.id == preset_id,
                    RoutinePreset.gym_id == gym_id,
                    RoutinePreset.retired_at.is_(None),
                )
            )
            if preset is None:
                raise ValueError("unknown Preset")
            try:
                master = await self._save(
                    db,
                    None,
                    gym_id,
                    workouts,
                    coach_authored=True,
                    created_by_member_id=coach_member_id,
                    expected_active_id=base_routine_id,
                    preset_id=preset_id,
                    commit=False,
                )
                linked = list(
                    await db.scalars(
                        select(Routine).where(
                            Routine.gym_id == gym_id,
                            Routine.member_id.is_not(None),
                            Routine.preset_id == preset_id,
                            Routine.is_active.is_(True),
                        )
                    )
                )
                await self._copy_master_to_members(
                    db,
                    preset_id,
                    gym_id,
                    coach_member_id,
                    workouts,
                    [routine.member_id for routine in linked if routine.member_id is not None],
                    expected_active_ids={
                        routine.member_id: routine.id
                        for routine in linked
                        if routine.member_id is not None
                    },
                )
                await db.commit()
                return master
            except IntegrityError:
                await db.rollback()
                raise StaleRoutineError(
                    "the Preset changed since the editor loaded it"
                ) from None
            except StaleRoutineError:
                await db.rollback()
                raise

    async def preset_master(self, preset_id: int) -> dict[str, Any] | None:
        """The active Member-less master, including a retired Preset's name."""
        async with self._sessions() as db:
            routine = await db.scalar(
                select(Routine).where(
                    Routine.preset_id == preset_id,
                    Routine.member_id.is_(None),
                    Routine.is_active.is_(True),
                )
            )
            if routine is None:
                return None
            return await self._routine_dict(db, routine)

    async def apply_preset(
        self, preset_id: int, gym_id: int, coach_member_id: int, member_ids: list[int]
    ) -> list[AppliedCopy]:
        """Stamp a fresh coach-authored copy onto each requested Member."""
        async with self._sessions() as db:
            preset = await db.scalar(
                select(RoutinePreset).where(
                    RoutinePreset.id == preset_id,
                    RoutinePreset.gym_id == gym_id,
                    RoutinePreset.retired_at.is_(None),
                )
            )
            if preset is None:
                raise ValueError("unknown Preset")
            master = await db.scalar(
                select(Routine).where(
                    Routine.preset_id == preset_id,
                    Routine.member_id.is_(None),
                    Routine.is_active.is_(True),
                )
            )
            if master is None:
                raise NoPresetMasterError("this Preset has no master Routine")
            master_workouts = await self._workouts(db, master.id)
            members = list(
                await db.scalars(
                    select(Member).where(
                        Member.id.in_(member_ids),
                        Member.gym_id == gym_id,
                        Member.is_coach.is_(False),
                    )
                )
            ) if member_ids else []
            unique_member_ids = list(dict.fromkeys(member_ids))
            if len(members) != len(unique_member_ids):
                raise ValueError("all applied Members must be non-coaches in the Gym")
            specs = self._specs_from_workouts(master_workouts)
            try:
                copies = await self._copy_master_to_members(
                    db,
                    preset_id,
                    gym_id,
                    coach_member_id,
                    specs,
                    unique_member_ids,
                )
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise StaleRoutineError(
                    "the Member's Routine changed while applying the Preset"
                ) from None
        return copies

    async def preset_linked_copies(self, preset_id: int, gym_id: int) -> list[AppliedCopy]:
        """Read the linked copies after a committed master edit for notices."""
        async with self._sessions() as db:
            routines = list(
                await db.scalars(
                    select(Routine).where(
                        Routine.gym_id == gym_id,
                        Routine.member_id.is_not(None),
                        Routine.preset_id == preset_id,
                        Routine.is_active.is_(True),
                    )
                )
            )
            return [
                AppliedCopy(
                    routine.member_id,
                    self._specs_from_workouts(await self._workouts(db, routine.id)),
                    routine.id,
                )
                for routine in routines
                if routine.member_id is not None
            ]

    async def _copy_master_to_members(
        self,
        db: Any,
        preset_id: int,
        gym_id: int,
        coach_member_id: int,
        specs: list[WorkoutSpec],
        member_ids: list[int],
        *,
        expected_active_ids: dict[int, int] | None = None,
    ) -> list[AppliedCopy]:
        """Stamp Preset copies inside the caller's transaction (issue #103)."""
        copies: list[AppliedCopy] = []
        for member_id in member_ids:
            routine = await self._save(
                db,
                member_id,
                gym_id,
                specs,
                coach_authored=True,
                created_by_member_id=coach_member_id,
                expected_active_id=(
                    expected_active_ids[member_id]
                    if expected_active_ids is not None
                    else REPLACE_ACTIVE
                ),
                preset_id=preset_id,
                commit=False,
            )
            copies.append(AppliedCopy(member_id, list(specs), routine.id))
        return copies

    @staticmethod
    def _specs_from_workouts(workouts: list[dict[str, Any]]) -> list[WorkoutSpec]:
        return [
            WorkoutSpec(
                weekday=workout["weekday"],
                name=workout["name"],
                exercises=[
                    ExerciseSpec(exercise["exercise"], exercise["sets"], exercise["reps"])
                    for exercise in workout["exercises"]
                ],
            )
            for workout in workouts
        ]

    async def _save(
        self,
        db: Any,
        member_id: int | None,
        gym_id: int,
        workouts: list[WorkoutSpec],
        *,
        coach_authored: bool,
        created_by_member_id: int | None,
        expected_active_id: int | None | _ReplaceActive,
        preset_id: int | None = None,
        commit: bool = True,
    ) -> Routine:
        """The writes of ``save_routine`` inside an already-open transaction.

        ``expected_active_id`` is the caller's stake on what is active right
        now: ``REPLACE_ACTIVE`` for replace semantics (agent chat saves), an
        exact id the save conditionally deactivates, or ``None`` when no
        Routine may be active. Coach saves never deactivate an active they
        did not expect — a mismatch is ``StaleRoutineError``.
        """
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
            raise UnknownExercisesError(unknown)

        is_master = member_id is None
        if is_master and preset_id is None:
            raise ValueError("a Member-less Routine needs a Preset")
        scope = (
            (Routine.member_id.is_(None), Routine.preset_id == preset_id)
            if is_master
            else (Routine.member_id == member_id,)
        )
        if isinstance(expected_active_id, _ReplaceActive):
            if is_master:
                raise ValueError("Preset masters require an explicit base Routine")
            # Agent chat saves: replace whatever is active — except a
            # coach-written Routine, which generation must not overwrite.
            active = await db.scalar(
                select(Routine).where(
                    *scope, Routine.is_active.is_(True)
                )
            )
            if active is not None and active.coach_authored and not coach_authored:
                raise ValueError(
                    "this Member has a coach-written Routine; only the Coach can change it "
                    "— tell the Member to ask their Coach, don't try to save again"
                )
            if active is not None:
                active.is_active = False
        elif expected_active_id is None:
            # The editor loaded an empty slot: nothing may have appeared
            # since. Refuse — never deactivate a Routine this save did not
            # expect to find.
            active_id = await db.scalar(
                select(Routine.id).where(
                    *scope, Routine.is_active.is_(True)
                )
            )
            if active_id is not None:
                raise StaleRoutineError(
                    "the active Routine changed since the editor loaded it"
                )
        else:
            # The stale check IS the write: deactivate only the exact row
            # the editor loaded, and only while it is still the active one.
            # A fresher save that superseded it first (the row is no longer
            # active, or the id never was this Member's) matches zero rows —
            # refused, and the fresher Routine stays active.
            result = cast(
                "CursorResult[Any]",
                await db.execute(
                    update(Routine)
                    .where(
                        Routine.id == expected_active_id,
                        *scope,
                        Routine.is_active.is_(True),
                    )
                    .values(is_active=False)
                ),
            )
            if result.rowcount == 0:
                raise StaleRoutineError(
                    "the active Routine changed since the editor loaded it"
                )

        routine = Routine(
            gym_id=gym_id,
            member_id=member_id,
            preset_id=preset_id,
            is_active=True,
            coach_authored=coach_authored,
            created_by_member_id=created_by_member_id,
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
        if commit:
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
            return await self._routine_dict(db, routine)

    async def _routine_dict(self, db: Any, routine: Routine) -> dict[str, Any]:
        workouts = await self._workouts(db, routine.id)
        author_name = None
        if routine.created_by_member_id is not None:
            author = await db.get(Member, routine.created_by_member_id)
            # The stamp blanks if the Coach's row ever disappears — the
            # chip degrades to plain "Coach-authored" (issue #91).
            author_name = author.name if author is not None else None
        preset_name = None
        if routine.preset_id is not None:
            preset = await db.get(RoutinePreset, routine.preset_id)
            preset_name = preset.name if preset is not None else None
        return {
            "routine_id": routine.id,
            "coach_authored": routine.coach_authored,
            "created_by_name": author_name,
            "preset_id": routine.preset_id,
            "preset_name": preset_name,
            "workouts": workouts,
        }

    async def workout_for_weekday(self, member_id: int, weekday: int) -> dict[str, Any] | None:
        return self._pick_weekday(await self.active_routine(member_id), weekday)

    async def todays_workout(
        self, member_id: int, timezone: str = "UTC"
    ) -> dict[str, Any] | None:
        return await self.workout_for_weekday(member_id, self._today(timezone))

    def pick_todays_workout(
        self, routine: dict[str, Any] | None, timezone: str = "UTC"
    ) -> dict[str, Any] | None:
        """Today's Workout from an already-loaded Routine — saves a re-query
        for callers (e.g. the snapshot) that hold the Routine already."""
        return self._pick_weekday(routine, self._today(timezone))

    async def weekday_workout_names(self, member_id: int) -> dict[int, str]:
        """Map each pinned weekday (0=Mon) to its Workout name — the pinned
        training days the check-in sweep reasons over."""
        routine = await self.active_routine(member_id)
        if routine is None:
            return {}
        return {w["weekday"]: w["name"] for w in routine["workouts"]}

    def _today(self, timezone: str = "UTC") -> int:
        # Weekday on the Gym's local day (issue #95), never the UTC day.
        return local_date(self._clock(), timezone).weekday()

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
