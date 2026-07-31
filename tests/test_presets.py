"""Preset masters and copy-on-apply (issue #102).

Presets deliberately reuse Routine structure while keeping their Member-less
master rows out of every Member-facing read.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from agentg.db import create_engine
from agentg.forget import ForgetStore
from agentg.linking_store import LinkingStore
from agentg.models import Routine
from agentg.routines import (
    DuplicatePresetNameError,
    ExerciseSpec,
    NoPresetMasterError,
    RoutineStore,
    StaleRoutineError,
    WorkoutSpec,
)
from agentg.training import TrainingStore


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'presets.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    training = TrainingStore(engine)
    await training.ensure_seeded()
    gym = await linking.create_gym("Iron Temple")
    coach = await linking.link_member(gym.id, "Coach Ana", "telegram", "1")
    await linking.set_coach(coach.id, True)
    routines = RoutineStore(engine)

    class Env:
        pass

    result = Env()
    result.engine = engine
    result.linking = linking
    result.training = training
    result.routines = routines
    result.gym = gym
    result.coach = coach
    yield result
    await engine.dispose()


def plan(name: str = "Full body") -> list[WorkoutSpec]:
    return [
        WorkoutSpec(
            weekday=0,
            name=name,
            exercises=[ExerciseSpec("squat", 3, "8-10")],
        )
    ]


async def test_preset_names_are_trimmed_and_unique_per_gym(env):
    preset = await env.routines.create_preset(env.gym.id, "  Beginner  ")
    assert preset.name == "Beginner"
    with pytest.raises(DuplicatePresetNameError):
        await env.routines.create_preset(env.gym.id, "Beginner")
    assert [p.name for p in await env.routines.presets(env.gym.id)] == ["Beginner"]


async def test_preset_master_supersession_is_scoped_to_that_preset(env):
    first = await env.routines.create_preset(env.gym.id, "Beginner")
    second = await env.routines.create_preset(env.gym.id, "Advanced")

    first_master = await env.routines.save_preset_master(
        first.id, env.gym.id, env.coach.id, plan("Beginner"), base_routine_id=None
    )
    second_master = await env.routines.save_preset_master(
        second.id, env.gym.id, env.coach.id, plan("Advanced"), base_routine_id=None
    )
    newer_first = await env.routines.save_preset_master(
        first.id,
        env.gym.id,
        env.coach.id,
        plan("Beginner v2"),
        base_routine_id=first_master.id,
    )

    async with env.routines._sessions() as db:
        active = list(
            await db.scalars(
                select(Routine).where(
                    Routine.is_active.is_(True), Routine.member_id.is_(None)
                )
            )
        )
    assert {row.id for row in active} == {newer_first.id, second_master.id}
    assert (await env.routines.preset_master(second.id))["routine_id"] == second_master.id


async def test_apply_preset_copies_the_master_to_each_member(env):
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    await env.routines.save_preset_master(
        preset.id, env.gym.id, env.coach.id, plan(), base_routine_id=None
    )
    member = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")
    other = await env.linking.link_member(env.gym.id, "Mara", "telegram", "3")

    copies = await env.routines.apply_preset(
        preset.id, env.gym.id, env.coach.id, [member.id, other.id]
    )

    assert [copy.member_id for copy in copies] == [member.id, other.id]
    assert all(copy.workouts == plan() for copy in copies)
    for member_id in (member.id, other.id):
        active = await env.routines.active_routine(member_id)
        assert active["preset_id"] == preset.id
        assert active["preset_name"] == "Beginner"
        assert active["coach_authored"] is True


async def test_applying_a_preset_without_a_master_is_typed_and_does_not_save(env):
    preset = await env.routines.create_preset(env.gym.id, "Empty")
    member = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")
    with pytest.raises(NoPresetMasterError):
        await env.routines.apply_preset(preset.id, env.gym.id, env.coach.id, [member.id])
    assert await env.routines.active_routine(member.id) is None


async def test_apply_preset_rolls_back_the_batch_on_a_late_write_conflict(env, monkeypatch):
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    await env.routines.save_preset_master(
        preset.id, env.gym.id, env.coach.id, plan(), base_routine_id=None
    )
    first = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")
    second = await env.linking.link_member(env.gym.id, "Mara", "telegram", "3")
    original = env.routines._save
    calls = 0

    async def conflict_on_second(db, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise IntegrityError("concurrent Routine", {}, Exception("unique"))
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(env.routines, "_save", conflict_on_second)
    with pytest.raises(ValueError):
        await env.routines.apply_preset(
            preset.id, env.gym.id, env.coach.id, [first.id, second.id]
        )
    assert await env.routines.active_routine(first.id) is None
    assert await env.routines.active_routine(second.id) is None


async def test_editing_a_master_refreshes_only_still_linked_members(env):
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    master = await env.routines.save_preset_master(
        preset.id, env.gym.id, env.coach.id, plan(), base_routine_id=None
    )
    linked = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")
    forked = await env.linking.link_member(env.gym.id, "Mara", "telegram", "3")
    await env.routines.apply_preset(preset.id, env.gym.id, env.coach.id, [linked.id, forked.id])
    forked_routine = await env.routines.active_routine(forked.id)
    await env.routines.save_coach_routine(
        forked.id,
        env.gym.id,
        env.coach.id,
        plan("Mara's fork"),
        base_routine_id=forked_routine["routine_id"],
    )

    await env.routines.save_preset_master(
        preset.id, env.gym.id, env.coach.id, plan("Beginner v2"), base_routine_id=master.id
    )

    linked_active = await env.routines.active_routine(linked.id)
    forked_active = await env.routines.active_routine(forked.id)
    assert linked_active["preset_id"] == preset.id
    assert linked_active["workouts"][0]["name"] == "Beginner v2"
    assert forked_active["preset_id"] is None
    assert forked_active["workouts"][0]["name"] == "Mara's fork"


async def test_master_propagation_is_atomic_when_a_member_write_conflicts(env, monkeypatch):
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    old_master = await env.routines.save_preset_master(
        preset.id, env.gym.id, env.coach.id, plan(), base_routine_id=None
    )
    first = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")
    second = await env.linking.link_member(env.gym.id, "Mara", "telegram", "3")
    await env.routines.apply_preset(
        preset.id, env.gym.id, env.coach.id, [first.id, second.id]
    )
    original = env.routines._save
    calls = 0

    async def conflict_on_second_copy(db, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:  # master, first copy, second copy
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("concurrent Routine", {}, Exception("unique"))
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(env.routines, "_save", conflict_on_second_copy)
    with pytest.raises(StaleRoutineError):
        await env.routines.save_preset_master(
            preset.id,
            env.gym.id,
            env.coach.id,
            plan("Beginner v2"),
            base_routine_id=old_master.id,
        )

    assert (await env.routines.preset_master(preset.id))["routine_id"] == old_master.id
    assert (await env.routines.active_routine(first.id))["workouts"][0]["name"] == "Full body"
    assert (await env.routines.active_routine(second.id))["workouts"][0]["name"] == "Full body"


async def test_default_preset_lands_on_a_new_member_at_agent_save_time(env):
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    await env.routines.save_preset_master(
        preset.id, env.gym.id, env.coach.id, plan("Coach plan"), base_routine_id=None
    )
    await env.routines.set_default_preset(env.gym.id, preset.id)
    member = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")

    await env.routines.save_routine(member.id, env.gym.id, plan("Generated plan"))

    active = await env.routines.active_routine(member.id)
    assert active["preset_id"] == preset.id
    assert active["preset_name"] == "Beginner"
    assert active["workouts"][0]["name"] == "Coach plan"


async def test_agent_save_without_a_default_keeps_the_generated_plan(env):
    member = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")

    await env.routines.save_routine(member.id, env.gym.id, plan("Generated plan"))

    active = await env.routines.active_routine(member.id)
    assert active["preset_id"] is None
    assert active["workouts"][0]["name"] == "Generated plan"


async def test_retiring_a_default_clears_the_slot_but_keeps_member_copies(env):
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    await env.routines.save_preset_master(
        preset.id, env.gym.id, env.coach.id, plan(), base_routine_id=None
    )
    await env.routines.set_default_preset(env.gym.id, preset.id)
    member = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")
    await env.routines.apply_preset(preset.id, env.gym.id, env.coach.id, [member.id])

    await env.routines.retire_preset(env.gym.id, preset.id)

    async with env.routines._sessions() as db:
        from agentg.models import Gym

        gym = await db.get(Gym, env.gym.id)
        assert gym.default_preset_id is None
    assert await env.routines.presets(env.gym.id) == []
    active = await env.routines.active_routine(member.id)
    assert active["preset_name"] == "Beginner"


async def test_agent_cannot_overwrite_a_preset_copy(env):
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    await env.routines.save_preset_master(
        preset.id, env.gym.id, env.coach.id, plan(), base_routine_id=None
    )
    member = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")
    await env.routines.apply_preset(preset.id, env.gym.id, env.coach.id, [member.id])

    with pytest.raises(ValueError):
        await env.routines.save_routine(member.id, env.gym.id, plan("Agent deviation"))
    assert (await env.routines.active_routine(member.id))["workouts"][0]["name"] == "Full body"


async def test_master_rows_do_not_change_roster_or_attendance(env):
    from agentg.dashboard_store import DashboardStore

    member = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")
    dashboard = DashboardStore(env.engine)
    before = await dashboard.roster(env.gym.id)
    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    await env.routines.save_preset_master(
        preset.id, env.gym.id, env.coach.id, plan(), base_routine_id=None
    )
    after = await dashboard.roster(env.gym.id)
    assert after == before
    grid = await dashboard.attendance(env.gym.id, [member.id])
    assert all(cell.state in {"plain", "future"} for cell in grid[member.id])


async def test_forgetting_a_member_keeps_preset_masters(env):
    from agents.extensions.memory import SQLAlchemySession

    preset = await env.routines.create_preset(env.gym.id, "Beginner")
    master = await env.routines.save_preset_master(
        preset.id, env.gym.id, env.coach.id, plan(), base_routine_id=None
    )
    member = await env.linking.link_member(env.gym.id, "Luis", "telegram", "2")
    await SQLAlchemySession("startup:schema", engine=env.engine, create_tables=True).get_items(
        limit=1
    )
    await ForgetStore(env.engine).forget_member(member.id)
    assert (await env.routines.preset_master(preset.id))["routine_id"] == master.id


async def test_routine_master_columns_and_indexes_are_present(env):
    async with env.engine.connect() as conn:
        columns = {
            row[1]: row[3]
            for row in await conn.execute(text("PRAGMA table_info(routines)"))
        }
        indexes = {
            row[1]
            for row in await conn.execute(text("PRAGMA index_list(routines)"))
        }
    assert columns["member_id"] == 0
    assert "preset_id" in columns
    assert "uq_routines_one_active_master_per_preset" in indexes
