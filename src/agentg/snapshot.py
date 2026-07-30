"""The per-turn member snapshot (docs/design/memory.md §Recall).

Always-true, always-cheap facts for the Agent's dynamic instructions:
identity, gap, last-session headline, active notes. A few hundred tokens;
anything bulkier stays behind a tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

MAX_SNAPSHOT_NOTES = 15
MAX_NOTE_DISPLAY = 120

if TYPE_CHECKING:
    from agentg.context import MemberContext


def _headline(exercises: list[dict[str, Any]], unit: str) -> str:
    parts = []
    for entry in exercises:
        reps = "/".join(str(rep) for rep in entry["reps"])
        if entry["weight"] is not None:
            parts.append(f"{entry['exercise']} {entry['weight']:g} {unit} {reps}")
        else:
            parts.append(f"{entry['exercise']} {reps}")
    return " · ".join(parts)


async def member_snapshot(context: MemberContext) -> str:
    days, last = await context.stores.training.latest_session_info(context.member_id)
    routine = await context.stores.routines.active_routine(context.member_id)
    todays_workout = context.stores.routines.pick_todays_workout(routine, context.timezone)
    notes = await context.stores.notes.active(context.member_id)

    role = "Coach (coach tools available)" if context.is_coach else "Member"
    today = context.stores.training.today(context.timezone).isoformat()
    lines = [
        "--- Member snapshot (facts from tables; trust these over chat memory) ---",
        f"Today is {today}.",
        f"{role}: {context.member_name}, at {context.gym_name} "
        f"(weights in {context.weight_unit}).",
    ]
    if last is None:
        lines.append("No Sessions logged yet.")
    else:
        gap = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"
        headline = _headline(last["exercises"], context.weight_unit) or "no sets logged"
        lines.append(f"Last Session: {gap} ({last['date']}): {headline}.")
    if routine is None:
        lines.append("No routine yet — run intake and generate one before coaching sessions.")
    else:
        authored = "coach-written" if routine["coach_authored"] else "agent-generated"
        if todays_workout is None:
            lines.append(f"Today is a rest day in the Member's {authored} routine.")
        else:
            plan = ", ".join(e["exercise"] for e in todays_workout["exercises"]) or "no exercises"
            lines.append(f"Today's Workout ({authored} routine): {todays_workout['name']} ({plan}).")
    if not notes:
        lines.append("No active notes.")
    else:
        lines.append("Active notes (retire by id when the Member says they're outdated):")
        for note in notes[:MAX_SNAPSHOT_NOTES]:
            date = note.created_at.date().isoformat()
            lines.append(f"- {note.kind} #{note.id} ({date}): {note.text[:MAX_NOTE_DISPLAY]}")
        if len(notes) > MAX_SNAPSHOT_NOTES:
            lines.append(f"(+{len(notes) - MAX_SNAPSHOT_NOTES} more notes)")
    return "\n".join(lines)
