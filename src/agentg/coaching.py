"""Coach-facing domain actions (spec §Routine generation & coach overrides,
§Safety rules): the is_coach gate, rules-doc edits, coach-written Routines,
and the always-on safety referral.

Behavior lives here so tools.py stays a thin adapter between the Agent and
the domain. Errors come back as ``{"error": ...}`` payloads so the Agent can
recover conversationally instead of crashing the turn.
"""

from __future__ import annotations

import logging
from typing import Any

from agentg.context import MemberContext
from agentg.routines import WorkoutSpec

logger = logging.getLogger(__name__)

# A single message is the enforcement point: a Member without the coach flag
# never gets past it, whatever the model is asked to do.
_NOT_A_COACH = {
    "error": (
        "that's a coach-only action, and you're not flagged as a coach — "
        "tell the Member only their Coach can do this"
    )
}


async def update_rules_doc_action(c: MemberContext, new_doc: str) -> dict[str, Any]:
    if not c.is_coach:
        return _NOT_A_COACH
    # A Gym still on the shipped default gets its own editable copy here.
    await c.stores.routines.set_rules_doc(c.gym_id, new_doc)
    return {"saved": True, "gym": c.gym_name}


async def _resolve_member(
    c: MemberContext, member_name: str, member_id: int | None
) -> Any:
    """The target Member for a coach action, or an ``{"error": ...}`` payload."""
    if member_id is not None:
        member = await c.stores.linking.member_in_gym(c.gym_id, member_id)
        if member is None:
            return {
                "error": (
                    f"no Member with id {member_id} in your Gym — check the id "
                    "or look them up by name instead"
                )
            }
        return member
    matches = await c.stores.linking.members_by_name(c.gym_id, member_name)
    if not matches:
        return {
            "error": (
                f"no Member named {member_name!r} in your Gym — check the spelling "
                "or ask the Coach which Member they mean"
            )
        }
    if len(matches) > 1:
        return {
            "error": f"several Members named {member_name!r}: {[m.id for m in matches]} "
            "— pass member_id to pick one"
        }
    return matches[0]


async def write_routine_action(
    c: MemberContext, member_name: str, member_id: int | None, specs: list[WorkoutSpec]
) -> dict[str, Any]:
    if not c.is_coach:
        return _NOT_A_COACH
    if not specs:
        return {
            "error": (
                "a Routine needs at least one Workout — include at least one weekday "
                "with exercises from list_exercises"
            )
        }
    target = await _resolve_member(c, member_name, member_id)
    if isinstance(target, dict):
        return target
    try:
        routine = await c.stores.routines.save_routine(
            target.id,
            c.gym_id,
            specs,
            coach_authored=True,
            # A Coach's write is actor-stamped wherever it happens (chat or
            # dashboard) — NULL means the Agent wrote it (issue #91).
            created_by_member_id=c.member_id,
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


async def flag_to_coach_action(c: MemberContext, summary: str) -> dict[str, Any]:
    """Log a safety concern and durably queue one notification per Coach.

    The flag is a Note of the ``safety`` kind with the bare summary (no
    prefix hack), so the roster can filter on the kind. There is no consent
    ask (issue #101): the Note is always written and every Coach is always
    queued for notification.  The outbox worker sends the pings without
    delaying the Member's reply (issue #172).

    The safety Note and its outbox jobs commit atomically (issue #216): an
    incomplete outbox rolls back the safety operation so a committed Note
    can never lose every Coach notification on delivery failure or process
    exit.

    Eligible Coaches are queried **inside** the same transaction as the
    Note write so eligibility is atomic with commit (P1 #3).  The durable
    outbox does not depend on whether a notifier is currently wired — jobs
    are always created and the worker picks them up when it starts (P1 #2).
    """
    note, jobs = await c.stores.safety_outbox.create_note_and_jobs(
        member_id=c.member_id,
        gym_id=c.gym_id,
        text=summary,
        member_name=c.member_name,
        member_is_coach=c.is_coach,
        linking_store=c.stores.linking,
        exclude_member_id=c.member_id,
    )
    logger.info(
        "safety note #%d queued %d outbox jobs", note.id, len(jobs),
    )
    return {"logged": True, "coaches_to_notify": len(jobs)}
