"""The Agent's function tools — the only path between the Agent and facts.

Thin wrappers over ``TrainingStore``; store errors come back as ``{"error"}``
payloads so the Agent can recover conversationally instead of crashing the
turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents import RunContextWrapper, Tool, function_tool

from agentg.training import LoggedSets, TrainingStore


@dataclass(frozen=True)
class MemberContext:
    """Everything a tool needs to act for the Member this turn is about."""

    training: TrainingStore
    member_id: int
    gym_id: int
    member_name: str
    gym_name: str
    weight_unit: str


def _logged(payload: LoggedSets, unit: str) -> dict[str, Any]:
    return {
        "exercise": payload.exercise,
        "weight": payload.weight,
        "weight_unit": unit,
        "reps": payload.reps,
        "previous": payload.previous,
    }


@function_tool
async def open_session(ctx: RunContextWrapper[MemberContext]) -> dict[str, Any]:
    """Open (or resume) a Session because the Member is at the gym now.

    Returns days since the last Session and that Session's exercises with
    weights and reps — reference numbers to offer, never to assume logged.
    """
    c = ctx.context
    opened = await c.training.open_session(c.member_id, c.gym_id)
    return {
        "session_id": opened.session_id,
        "resumed_existing": opened.reopened,
        "days_since_last_session": opened.days_since_last,
        "last_session": opened.last_session,
        "weight_unit": c.weight_unit,
    }


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


def build_tools() -> list[Tool]:
    return [open_session, log_sets, copy_last_sets, edit_logged_sets, get_last_sets, close_session]
