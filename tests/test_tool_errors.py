"""Tool error payloads must name the bad input and a concrete next step.

Every ``{"error": ...}`` the Agent sees is quoted nearly verbatim in chat, so
messages stay Member-safe and always include a recovery cue (what to try next).
"""

from __future__ import annotations

import json
import re

import pytest
from agents.tool_context import ToolContext

from agentg.context import MemberContext
from agentg.db import create_engine
from agentg.routines import ExerciseSpec, WorkoutSpec
from agentg.stores import Stores
from agentg.coaching import update_rules_doc_action, write_routine_action
from agentg.tools import (
    close_session,
    copy_last_sets,
    edit_logged_sets,
    get_last_sets,
    log_sets,
    retire_note,
    save_routine,
    snooze_checkins,
)

# Problem statement, then an em dash, then what to change before retrying.
_RECOVERY = re.compile(r"—.+\S")


def has_recovery_cue(message: str) -> bool:
    return bool(_RECOVERY.search(message))


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'tool-errors.db'}")
    stores = Stores.from_engine(engine)
    await stores.linking.ensure_schema()
    await stores.training.ensure_seeded()
    gym = await stores.linking.create_gym("Iron Temple")
    member = await stores.linking.link_member(gym.id, "Dani", "telegram", "42")
    context = MemberContext(
        stores=stores,
        member_id=member.id,
        gym_id=gym.id,
        member_name=member.name,
        gym_name=gym.name,
        weight_unit="kg",
        is_coach=False,
    )

    class Env:
        pass

    e = Env()
    e.engine = engine
    e.stores = stores
    e.context = context
    e.member_id = member.id
    e.gym_id = gym.id
    yield e
    await engine.dispose()


async def call_tool(tool, context: MemberContext, **kwargs):
    payload = json.dumps(kwargs)
    tc = ToolContext(
        context=context,
        tool_name=tool.name,
        tool_call_id="test",
        tool_arguments=payload,
    )
    return await tool.on_invoke_tool(tc, payload)


async def test_retiring_a_missing_note_names_the_id_and_how_to_recover(env):
    result = await call_tool(retire_note, env.context, note_id=999)

    assert "error" in result
    assert "999" in result["error"]
    assert has_recovery_cue(result["error"])
    # concrete next step: re-check note ids or ask the Member
    lower = result["error"].lower()
    assert "snapshot" in lower or "ask" in lower


async def test_unknown_catalog_exercise_names_the_input_and_points_at_list_exercises(env):
    result = await call_tool(
        save_routine,
        env.context,
        workouts=[
            {
                "weekday": 0,
                "name": "Push",
                "exercises": [{"exercise": "incline hammer press"}],
            }
        ],
    )

    assert "error" in result
    assert "incline hammer press" in result["error"]
    assert has_recovery_cue(result["error"])
    assert "list_exercises" in result["error"]


async def test_agent_save_reports_the_default_preset_that_landed(env):
    coach = await env.stores.linking.link_member(env.gym_id, "Coach Ana", "telegram", "1")
    await env.stores.linking.set_coach(coach.id, True)
    preset = await env.stores.routines.create_preset(env.gym_id, "Beginner")
    await env.stores.routines.save_preset_master(
        preset.id,
        env.gym_id,
        coach.id,
        [WorkoutSpec(weekday=0, name="Coach plan", exercises=[ExerciseSpec("squat")])],
        base_routine_id=None,
    )
    await env.stores.routines.set_default_preset(env.gym_id, preset.id)

    result = await call_tool(
        save_routine,
        env.context,
        workouts=[
            {
                "weekday": 1,
                "name": "Generated plan",
                "exercises": [{"exercise": "bench press"}],
            }
        ],
    )

    assert result["applied_preset"] == "Beginner"
    assert result["routine"]["workouts"][0]["name"] == "Coach plan"


async def test_closing_with_no_open_session_says_what_to_do_next(env):
    result = await call_tool(close_session, env.context)

    assert "error" in result
    assert "open session" in result["error"].lower()
    assert has_recovery_cue(result["error"])
    lower = result["error"].lower()
    assert "tell" in lower or "open_session" in lower


async def test_copy_last_sets_without_history_names_exercise_and_recovery(env):
    await env.stores.training.open_session(env.member_id, env.gym_id)

    result = await call_tool(copy_last_sets, env.context, exercise="bench")

    assert "error" in result
    assert "bench" in result["error"].lower() or "bench press" in result["error"].lower()
    assert has_recovery_cue(result["error"])
    lower = result["error"].lower()
    assert "ask" in lower or "check" in lower or "log" in lower


async def test_edit_with_no_sets_in_session_names_exercise_and_recovery(env):
    await env.stores.training.open_session(env.member_id, env.gym_id)

    result = await call_tool(
        edit_logged_sets, env.context, exercise="bench", weight=62.5
    )

    assert "error" in result
    assert "bench" in result["error"].lower()
    assert has_recovery_cue(result["error"])
    lower = result["error"].lower()
    assert "check" in lower or "ask" in lower or "log" in lower


async def test_copy_last_sets_tool_response_includes_ordered_copied_sets(env):
    """The copy_last_sets tool result carries ``copied_sets`` with mixed
    weights, optional fields, and ordering so the Agent can restate each
    set with its own weight."""
    # Log mixed warm-up and working sets across two log calls (batches).
    await env.stores.training.log_sets(env.member_id, env.gym_id, "bench 40 10",
                                       rpe=6.5, note="warm-up")
    await env.stores.training.log_sets(env.member_id, env.gym_id, "bench 60 5,5",
                                       rpe=8.0, note="paused")
    await env.stores.training.close_session(env.member_id)

    # New session — copy the previous.
    await env.stores.training.open_session(env.member_id, env.gym_id)
    result = await call_tool(copy_last_sets, env.context, exercise="bench")

    # Top-set summary still works for progression.
    assert result["exercise"] in ("bench press", "bench")
    assert result["weight"] == 60.0
    assert result["reps"] == [10, 5, 5]

    # The per-set detail the Agent needs for an accurate restatement.
    assert "copied_sets" in result
    copied = result["copied_sets"]
    assert isinstance(copied, list)
    assert len(copied) == 3

    # Order is preserved — warm-up first, then working sets.
    assert copied[0] == {"weight": 40.0, "reps": 10, "rpe": 6.5, "note": "warm-up"}
    assert copied[1] == {"weight": 60.0, "reps": 5, "rpe": 8.0, "note": "paused"}
    assert copied[2] == {"weight": 60.0, "reps": 5, "rpe": 8.0, "note": "paused"}


async def _collect_tool_error_messages(env) -> dict[str, str]:
    """Hit every tool error path once; return {label: error message}."""
    errors: dict[str, str] = {}

    async def record(label: str, result: dict) -> None:
        assert "error" in result, f"{label} did not return an error payload: {result!r}"
        errors[label] = result["error"]

    await record("retire_note.missing", await call_tool(retire_note, env.context, note_id=999))
    await record(
        "save_routine.unknown_exercise",
        await call_tool(
            save_routine,
            env.context,
            workouts=[
                {
                    "weekday": 0,
                    "name": "Push",
                    "exercises": [{"exercise": "not-a-real-lift"}],
                }
            ],
        ),
    )
    await record(
        "save_routine.empty",
        await call_tool(save_routine, env.context, workouts=[]),
    )
    await record("close_session.none_open", await call_tool(close_session, env.context))
    await env.stores.training.open_session(env.member_id, env.gym_id)
    await record(
        "copy_last_sets.no_history",
        await call_tool(copy_last_sets, env.context, exercise="bench"),
    )
    await record(
        "edit_logged_sets.no_sets",
        await call_tool(edit_logged_sets, env.context, exercise="bench", weight=62.5),
    )
    await record(
        "get_last_sets.none",
        await call_tool(get_last_sets, env.context, exercise="bench"),
    )
    await record(
        "log_sets.unparseable",
        await call_tool(log_sets, env.context, line="felt strong today"),
    )
    await record(
        "log_sets.no_exercise",
        await call_tool(log_sets, env.context, line="60 8,8,7"),
    )
    await record(
        "snooze_checkins.bad_date",
        await call_tool(snooze_checkins, env.context, until="next friday"),
    )

    # Coach-gated paths (non-coach caller).
    await record(
        "update_rules_doc.not_coach",
        await update_rules_doc_action(env.context, "hacked"),
    )
    await record(
        "write_routine.not_coach",
        await write_routine_action(
            env.context,
            "Ana",
            None,
            [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench press")])],
        ),
    )

    # Coach-only member-resolution paths.
    coach = await env.stores.linking.link_member(
        env.gym_id, "Coach Sam", "telegram", "99"
    )
    await env.stores.linking.set_coach(coach.id)
    coach_ctx = MemberContext(
        stores=env.stores,
        member_id=coach.id,
        gym_id=env.gym_id,
        member_name=coach.name,
        gym_name=env.context.gym_name,
        weight_unit="kg",
        is_coach=True,
    )
    await record(
        "write_routine.unknown_member",
        await write_routine_action(
            coach_ctx,
            "Nobody",
            None,
            [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench press")])],
        ),
    )
    await record(
        "write_routine.bad_member_id",
        await write_routine_action(
            coach_ctx,
            "Nobody",
            99999,
            [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench press")])],
        ),
    )
    await record(
        "write_routine.empty",
        await write_routine_action(coach_ctx, "Dani", None, []),
    )
    await record(
        "write_routine.unknown_exercise",
        await write_routine_action(
            coach_ctx,
            "Dani",
            None,
            [
                WorkoutSpec(
                    weekday=0,
                    name="Push",
                    exercises=[ExerciseSpec("not-a-real-lift")],
                )
            ],
        ),
    )

    # Generation blocked by a coach-authored routine.
    await env.stores.routines.save_routine(
        env.member_id,
        env.gym_id,
        [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench press")])],
        coach_authored=True,
    )
    await record(
        "save_routine.coach_authored",
        await call_tool(
            save_routine,
            env.context,
            workouts=[
                {
                    "weekday": 1,
                    "name": "Pull",
                    "exercises": [{"exercise": "deadlift"}],
                }
            ],
        ),
    )

    # Ambiguous member name for coach write.
    await env.stores.linking.link_member(env.gym_id, "Dani", "telegram", "77")
    await record(
        "write_routine.ambiguous_name",
        await write_routine_action(
            coach_ctx,
            "Dani",
            None,
            [WorkoutSpec(weekday=0, name="Push", exercises=[ExerciseSpec("bench press")])],
        ),
    )

    return errors


async def test_every_tool_error_path_includes_a_recovery_cue(env):
    """Convention: no bare 'failed'/'invalid' — every error names a next move."""
    errors = await _collect_tool_error_messages(env)
    assert errors, "expected at least one tool error path"

    bare = re.compile(r"^(failed|invalid|error)[.!]?$", re.I)
    missing = {
        label: msg
        for label, msg in errors.items()
        if not has_recovery_cue(msg) or bare.match(msg.strip())
    }
    assert not missing, "error paths missing a recovery cue:\n" + "\n".join(
        f"  {label}: {msg!r}" for label, msg in sorted(missing.items())
    )
