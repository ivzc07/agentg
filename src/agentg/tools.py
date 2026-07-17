"""The Agent's function tools — the only path between the Agent and facts.

Thin wrappers over ``TrainingStore``; store errors come back as ``{"error"}``
payloads so the Agent can recover conversationally instead of crashing the
turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents import RunContextWrapper, Tool, function_tool
from pydantic import BaseModel, Field

from agentg.advice import suggest_for_today
from agentg.notes import NotesStore
from agentg.routines import ExerciseSpec, RoutineStore, WorkoutSpec
from agentg.store import LinkingStore
from agentg.training import LoggedSets, TrainingStore


class ExerciseInput(BaseModel):
    """One prescribed Exercise in a Workout. Never a target weight."""

    exercise: str
    sets: int | None = None
    reps: str | None = None  # a scheme, e.g. "8-12" or "AMRAP"


class WorkoutInput(BaseModel):
    """One training day pinned to a weekday (0=Monday .. 6=Sunday)."""

    weekday: int = Field(ge=0, le=6)
    name: str
    exercises: list[ExerciseInput]


@dataclass(frozen=True)
class MemberContext:
    """Everything a tool needs to act for the Member this turn is about."""

    training: TrainingStore
    notes: NotesStore
    routines: RoutineStore
    linking: LinkingStore
    member_id: int
    gym_id: int
    member_name: str
    gym_name: str
    weight_unit: str
    is_coach: bool = False


def _logged(payload: LoggedSets, unit: str) -> dict[str, Any]:
    return {
        "exercise": payload.exercise,
        "weight": payload.weight,
        "weight_unit": unit,
        "reps": payload.reps,
        "previous": payload.previous,
    }


async def open_session_payload(c: MemberContext) -> dict[str, Any]:
    """Open (or resume) the Member's Session and assemble the opener facts:
    the gap, the last Session's numbers, and today's Workout from the Routine."""
    opened = await c.training.open_session(c.member_id, c.gym_id)
    return {
        "session_id": opened.session_id,
        "resumed_existing": opened.reopened,
        "days_since_last_session": opened.days_since_last,
        "last_session": opened.last_session,
        "todays_workout": await c.routines.todays_workout(c.member_id),
        "weight_unit": c.weight_unit,
    }


@function_tool
async def open_session(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """Open (or resume) a Session because the Member is at the gym now.

    Returns days since the last Session, that Session's exercises with weights
    and reps (reference numbers to offer, never to assume logged), and today's
    Workout from the Member's Routine.
    """
    return await open_session_payload(ctx.context)


@function_tool
async def log_sets(
    ctx: RunContextWrapper[MemberContext],
    line: str,
    exercise: str | None = None,
    rpe: float | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Store the sets a Member just reported, from their message verbatim.

    Pass the Member's shorthand line exactly as typed (e.g. "bench 60 8,8,8",
    "dips 10,10,9", "60 8/7/6"). When the line omits the exercise name, pass
    the exercise the conversation is about as ``exercise``. Pass ``rpe`` or
    ``note`` ONLY when the Member volunteered them — never ask for effort.
    """
    c = ctx.context
    try:
        logged = await c.training.log_sets(
            c.member_id, c.gym_id, line, exercise=exercise, rpe=rpe, note=note
        )
    except ValueError as error:
        return {"error": str(error)}
    return _logged(logged, c.weight_unit)


@function_tool
async def copy_last_sets(ctx: RunContextWrapper[MemberContext], exercise: str) -> dict[str, Any]:
    """Log "same as last time": copy this exercise's Sets from the previous Session."""
    c = ctx.context
    try:
        logged = await c.training.copy_last_sets(c.member_id, c.gym_id, exercise)
    except ValueError as error:
        return {"error": str(error)}
    return _logged(logged, c.weight_unit)


@function_tool
async def edit_logged_sets(
    ctx: RunContextWrapper[MemberContext],
    exercise: str,
    weight: float | None = None,
    reps: list[int] | None = None,
) -> dict[str, Any]:
    """Correct sets already logged in the current Session ("actually bench was 62.5").

    Pass only what changed: a new weight, a new rep list, or both. This never
    touches earlier Sessions.
    """
    c = ctx.context
    try:
        edited = await c.training.edit_logged_sets(c.member_id, exercise, weight=weight, reps=reps)
    except ValueError as error:
        return {"error": str(error)}
    return _logged(edited, c.weight_unit)


@function_tool
async def get_last_sets(ctx: RunContextWrapper[MemberContext], exercise: str) -> dict[str, Any]:
    """Read an exercise's numbers from the previous Session (never from memory)."""
    c = ctx.context
    info = await c.training.last_sets(c.member_id, exercise)
    if info is None:
        return {"error": f"no logged sets of {exercise} yet"}
    return {**info, "weight_unit": c.weight_unit}


@function_tool
async def close_session(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """Close the Session because the Member said they're done.

    Returns the summary data: total sets, and per exercise the numbers plus
    the change vs the previous Session (weight_change / reps_change).
    """
    c = ctx.context
    try:
        summary = await c.training.close_session(c.member_id)
    except ValueError as error:
        return {"error": str(error)}
    return {
        "session_id": summary.session_id,
        "total_sets": summary.total_sets,
        "exercises": summary.exercises,
        "weight_unit": c.weight_unit,
    }


@function_tool
async def remember_note(
    ctx: RunContextWrapper[MemberContext], kind: str, text: str
) -> dict[str, Any]:
    """Store a durable fact the Member VOLUNTEERED — never one you asked for.

    kind is one of injury / preference / goal / constraint / other. Use it
    for things worth knowing next month ("shoulder's been hurting", "hates
    burpees", "training for a half marathon") — not for small talk.
    """
    c = ctx.context
    note = await c.notes.remember(c.member_id, c.gym_id, kind, text)
    return {"note_id": note.id, "kind": note.kind, "text": note.text}


@function_tool
async def retire_note(ctx: RunContextWrapper[MemberContext], note_id: int) -> dict[str, Any]:
    """Retire a note that no longer holds ("the shoulder's fine now").

    Use the note id shown in your snapshot. The note is kept, dated, for the
    Coach — it just leaves your recall.
    """
    c = ctx.context
    try:
        note = await c.notes.retire(c.member_id, note_id)
    except ValueError as error:
        return {"error": str(error)}
    return {"retired_note_id": note.id, "text": note.text}


@function_tool
async def get_rules_doc(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """Read the gym's coaching rules doc. Follow it when generating a Routine.

    It governs the split, set/rep schemes, injury handling, and progression.
    Do not invent rules that contradict it.
    """
    c = ctx.context
    return {"rules_doc": await c.routines.effective_rules_doc(c.gym_id)}


@function_tool
async def list_exercises(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """The Exercise catalog to draw a Routine from. Prescribe only these names."""
    c = ctx.context
    return {"exercises": await c.training.catalog_names()}


@function_tool
async def save_routine(
    ctx: RunContextWrapper[MemberContext], workouts: list[WorkoutInput]
) -> dict[str, Any]:
    """Save the generated Routine, replacing the Member's current one.

    Each Workout is a weekday (0=Monday .. 6=Sunday), a name, and an ordered
    list of exercises with optional sets and rep scheme. Structure only —
    never include a target weight. Prescribe only exercises from
    list_exercises, pin each Workout to a weekday the Member named, and
    respect the rules doc and their injuries. Deliver directly after saving;
    for a requested structural change, save again only once they agree.
    """
    c = ctx.context
    if not workouts:
        return {"error": "a routine needs at least one workout"}
    specs = [
        WorkoutSpec(
            weekday=workout.weekday,
            name=workout.name,
            exercises=[
                ExerciseSpec(exercise=item.exercise, sets=item.sets, reps=item.reps)
                for item in workout.exercises
            ],
        )
        for workout in workouts
    ]
    try:
        routine = await c.routines.save_routine(c.member_id, c.gym_id, specs)
    except ValueError as error:
        # e.g. an exercise not in the catalog — the Agent should pick from
        # list_exercises and try again.
        return {"error": str(error)}
    return {"routine_id": routine.id, "workouts_saved": len(specs)}


@function_tool
async def get_routine(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """Read the Member's current Routine (their weekly plan), or none if unset."""
    c = ctx.context
    routine = await c.routines.active_routine(c.member_id)
    if routine is None:
        return {"routine": None}
    return {"routine": routine}


@function_tool
async def suggest_weights(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """Suggested working weights for today's Workout, derived from logged Sets.

    Per Exercise: the last weight, a suggested next weight, and why (action is
    increment / hold / deload / gap_deload / none). These come from the gym's
    progression rules — offer them in chat as suggestions, never as logged
    sets, and never state a number this tool did not return. After a long gap
    the suggestions ease back; open warm and guilt-free.
    """
    c = ctx.context
    suggestions = await suggest_for_today(c.training, c.routines, c.member_id, c.gym_id)
    return {
        "weight_unit": c.weight_unit,
        "suggestions": [
            {
                "exercise": s.exercise,
                "last_weight": s.last_weight,
                "suggested_weight": s.suggested_weight,
                "action": s.action,
                "reason": s.reason,
            }
            for s in suggestions
        ],
    }


# --- Coach-only tools (spec §Routine generation & coach overrides) ---

# A single message is the enforcement point: a Member without the coach flag
# never gets past it, whatever the model is asked to do.
_NOT_A_COACH = {"error": "that's a coach-only action, and you're not flagged as a coach"}


async def update_rules_doc_action(c: MemberContext, new_doc: str) -> dict[str, Any]:
    if not c.is_coach:
        return _NOT_A_COACH
    # A Gym still on the shipped default gets its own editable copy here.
    await c.routines.set_rules_doc(c.gym_id, new_doc)
    return {"saved": True, "gym": c.gym_name}


async def _resolve_member(
    c: MemberContext, member_name: str, member_id: int | None
) -> Any:
    """The target Member for a coach action, or an ``{"error": ...}`` payload."""
    if member_id is not None:
        member = await c.linking.member_in_gym(c.gym_id, member_id)
        if member is None:
            return {"error": f"no member with id {member_id} in your gym"}
        return member
    matches = await c.linking.members_by_name(c.gym_id, member_name)
    if not matches:
        return {"error": f"no member named {member_name!r} in your gym"}
    if len(matches) > 1:
        return {
            "error": f"several members named {member_name!r}: {[m.id for m in matches]} "
            "— pass member_id to pick one"
        }
    return matches[0]


async def write_routine_action(
    c: MemberContext, member_name: str, member_id: int | None, specs: list[WorkoutSpec]
) -> dict[str, Any]:
    if not c.is_coach:
        return _NOT_A_COACH
    if not specs:
        return {"error": "a routine needs at least one workout"}
    target = await _resolve_member(c, member_name, member_id)
    if isinstance(target, dict):
        return target
    try:
        routine = await c.routines.save_routine(
            target.id, c.gym_id, specs, coach_authored=True
        )
    except ValueError as error:
        return {"error": str(error)}
    return {
        "routine_id": routine.id,
        "member": target.name,
        "member_id": target.id,
        "coach_authored": True,
        "workouts_saved": len(specs),
    }


@function_tool
async def update_rules_doc(ctx: RunContextWrapper[MemberContext], new_doc: str) -> dict[str, Any]:
    """(Coach only) Replace the gym's rules doc with new plain text.

    Show the coach the proposed doc first and call this only once they
    confirm. A gym still on the shipped default gets its own copy on first
    edit. Keep the progression parameter lines (increment, deload_percent,
    stall_sessions, gap_deload_days, gap_deload_percent) — they drive weight
    suggestions.
    """
    return await update_rules_doc_action(ctx.context, new_doc)


@function_tool
async def write_routine(
    ctx: RunContextWrapper[MemberContext],
    member_name: str,
    workouts: list[WorkoutInput],
    member_id: int | None = None,
) -> dict[str, Any]:
    """(Coach only) Hand-write a Routine for a Member of your gym.

    Identify the Member by name (or member_id if the name is ambiguous).
    Preview the plan to the coach and call this only on confirm. The saved
    Routine is flagged coach-authored and delivered to the Member; structure
    only, exercises from the catalog, never target weights.
    """
    specs = [
        WorkoutSpec(
            weekday=workout.weekday,
            name=workout.name,
            exercises=[
                ExerciseSpec(exercise=item.exercise, sets=item.sets, reps=item.reps)
                for item in workout.exercises
            ],
        )
        for workout in workouts
    ]
    return await write_routine_action(ctx.context, member_name, member_id, specs)


def build_tools() -> list[Tool]:
    return [
        open_session,
        log_sets,
        copy_last_sets,
        edit_logged_sets,
        get_last_sets,
        close_session,
        remember_note,
        retire_note,
        get_rules_doc,
        list_exercises,
        save_routine,
        get_routine,
        suggest_weights,
        update_rules_doc,
        write_routine,
    ]
