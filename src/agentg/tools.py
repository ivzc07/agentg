"""The Agent's function tools — the only path between the Agent and facts.

Thin wrappers over ``TrainingStore``; store errors come back as ``{"error"}``
payloads so the Agent can recover conversationally instead of crashing the
turn.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from agents import RunContextWrapper, Tool, function_tool
from pydantic import BaseModel, Field

from agentg.advice import suggest_for_today
from agentg.coaching import (
    flag_to_coach_action,
    update_rules_doc_action,
    write_routine_action,
)
from agentg.context import MemberContext
from agentg.routines import ExerciseSpec, WorkoutSpec
from agentg.training import LoggedSets


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


def _logged(payload: LoggedSets, unit: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exercise": payload.exercise,
        "weight": payload.weight,
        "weight_unit": unit,
        "reps": payload.reps,
        "previous": payload.previous,
    }
    if payload.suspect is not None:
        result["suspect"] = payload.suspect
    return result


async def open_session_payload(c: MemberContext) -> dict[str, Any]:
    """Open (or resume) the Member's Session and assemble the opener facts:
    the gap, the last Session's numbers, and today's Workout from the Routine."""
    opened = await c.stores.training.open_session(c.member_id, c.gym_id)
    return {
        "session_id": opened.session_id,
        "resumed_existing": opened.reopened,
        "days_since_last_session": opened.days_since_last,
        "last_session": opened.last_session,
        "todays_workout": await c.stores.routines.todays_workout(c.member_id),
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

    Always restate the returned exercise/weight/reps in your reply. If the
    payload includes ``suspect``, the sets were still stored — double-check
    the numbers with the Member before treating them as settled.
    """
    c = ctx.context
    try:
        logged = await c.stores.training.log_sets(
            c.member_id, c.gym_id, line, exercise=exercise, rpe=rpe, note=note
        )
    except ValueError as error:
        return {"error": str(error)}
    return _logged(logged, c.weight_unit)


@function_tool
async def copy_last_sets(ctx: RunContextWrapper[MemberContext], exercise: str) -> dict[str, Any]:
    """Log "same as last time": copy this exercise's Sets from the previous Session.

    Always restate the returned exercise/weight/reps in your reply.
    """
    c = ctx.context
    try:
        logged = await c.stores.training.copy_last_sets(c.member_id, c.gym_id, exercise)
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

    Always restate the corrected numbers in your reply. If the payload
    includes ``suspect``, the correction was still stored — double-check the
    numbers with the Member before treating them as settled.
    """
    c = ctx.context
    try:
        edited = await c.stores.training.edit_logged_sets(c.member_id, exercise, weight=weight, reps=reps)
    except ValueError as error:
        return {"error": str(error)}
    return _logged(edited, c.weight_unit)


@function_tool
async def get_last_sets(ctx: RunContextWrapper[MemberContext], exercise: str) -> dict[str, Any]:
    """Read an exercise's numbers from the previous Session (never from memory)."""
    c = ctx.context
    info = await c.stores.training.last_sets(c.member_id, exercise)
    if info is None:
        return {
            "error": (
                f"no logged sets of {exercise} yet — ask the Member for the weight "
                "and reps, or try a different exercise name"
            )
        }
    return {**info, "weight_unit": c.weight_unit}


@function_tool
async def close_session(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """Close the Session because the Member said they're done.

    Returns the summary data: total sets, and per exercise the numbers plus
    the change vs the previous Session (weight_change / reps_change).
    """
    c = ctx.context
    try:
        summary = await c.stores.training.close_session(c.member_id)
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
    note = await c.stores.notes.remember(c.member_id, c.gym_id, kind, text)
    return {"note_id": note.id, "kind": note.kind, "text": note.text}


@function_tool
async def retire_note(ctx: RunContextWrapper[MemberContext], note_id: int) -> dict[str, Any]:
    """Retire a note that no longer holds ("the shoulder's fine now").

    Use the note id shown in your snapshot. The note is kept, dated, for the
    Coach — it just leaves your recall.
    """
    c = ctx.context
    try:
        note = await c.stores.notes.retire(c.member_id, note_id)
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
    return {"rules_doc": await c.stores.routines.effective_rules_doc(c.gym_id)}


@function_tool
async def list_exercises(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """The Exercise catalog to draw a Routine from. Prescribe only these names."""
    c = ctx.context
    return {"exercises": await c.stores.training.catalog_names()}


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
        return {
            "error": (
                "a routine needs at least one workout — include at least one weekday "
                "with exercises from list_exercises"
            )
        }
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
        routine = await c.stores.routines.save_routine(c.member_id, c.gym_id, specs)
    except ValueError as error:
        # e.g. an exercise not in the catalog — the Agent should pick from
        # list_exercises and try again.
        return {"error": str(error)}
    return {"routine_id": routine.id, "workouts_saved": len(specs)}


@function_tool
async def get_routine(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """Read the Member's current Routine (their weekly plan), or none if unset."""
    c = ctx.context
    routine = await c.stores.routines.active_routine(c.member_id)
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
    suggestions = await suggest_for_today(
        c.stores.training, c.stores.routines, c.member_id, c.gym_id
    )
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
# The gate and the behavior live in coaching.py; these wrappers only adapt.


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


@function_tool
async def stop_checkins(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """Turn off proactive check-ins ("stop checking in on me").

    Confirm warmly and tell them they can say "start checking in again" to
    turn them back on.
    """
    c = ctx.context
    await c.stores.checkins.turn_off(c.member_id)
    return {"checkins": "off", "reenable": "say 'start checking in again' anytime"}


@function_tool
async def snooze_checkins(ctx: RunContextWrapper[MemberContext], until: str) -> dict[str, Any]:
    """Pause check-ins until a date ("I'm traveling for two weeks").

    ``until`` is an ISO date (YYYY-MM-DD) — compute it from today's date in
    the snapshot. Confirm the date warmly and note they'll hear from you again
    after it.
    """
    c = ctx.context
    try:
        until_date = date.fromisoformat(until)
    except ValueError:
        return {
            "error": (
                f"{until!r} isn't a YYYY-MM-DD date — pass an ISO date like "
                f"{c.stores.training.today().isoformat()}"
            )
        }
    await c.stores.checkins.snooze_until(c.member_id, until_date)
    return {"checkins": "snoozed", "until": until_date.isoformat()}


@function_tool
async def resume_checkins(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """Turn proactive check-ins back on ("start checking in again")."""
    c = ctx.context
    await c.stores.checkins.resume(c.member_id)
    return {"checkins": "on"}


@function_tool
async def show_demo(ctx: RunContextWrapper[MemberContext], exercise: str) -> dict[str, Any]:
    """Send the Member a short autoplaying demo of how to do an Exercise.

    Use it when they ask how to do a movement ("how do I do a goblet squat?").
    If a demo exists it's queued and sent right after your reply — tell them
    it's on the way and add a form cue or two. If none exists, say so and
    describe the movement in words instead.
    """
    c = ctx.context
    ref = await c.stores.demos.resolve(exercise, c.gym_id)
    if ref is None:
        return {"available": False, "exercise": exercise}
    c.demo_requests.append(ref.exercise_name)
    return {"available": True, "exercise": ref.exercise_name}


@function_tool
async def flag_to_coach(
    ctx: RunContextWrapper[MemberContext], summary: str, share_with_coach: bool
) -> dict[str, Any]:
    """Log a safety concern, and ping the gym's coaches only if the Member agrees.

    First ask "want me to flag this to your coach?"; pass share_with_coach=True
    only on a clear yes, False on a no. Either way the concern is logged. Use
    this for the consent-gated referrals in the rules doc (injuries, disordered-
    eating red flags, anything you'd want a human to know).
    """
    return await flag_to_coach_action(ctx.context, summary, share_with_coach)


@function_tool
async def delete_my_data(ctx: RunContextWrapper[MemberContext], confirm: bool) -> dict[str, Any]:
    """Forget-me: permanently erase EVERYTHING about the Member — profile,
    Sessions, Sets, Routines, notes, and chat history — across every store.

    Irreversible, no grace period. Ask for confirmation once first ("this wipes
    everything permanently and can't be undone — are you sure?"); call with
    confirm=True only on a clear yes. On confirm=False, nothing is deleted.
    Only the Member can do this; never mention it to their coach.
    """
    c = ctx.context
    if not confirm:
        return {"deleted": False, "need_confirmation": True}
    await c.stores.forget.forget_member(c.member_id)
    return {"deleted": True}


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
        stop_checkins,
        snooze_checkins,
        resume_checkins,
        show_demo,
        flag_to_coach,
        delete_my_data,
    ]
