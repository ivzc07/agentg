"""Simple stratum: single clean actions leave the expected DB rows."""

from __future__ import annotations

from behavioral.harness import ConversationHarness, message, tool


async def test_clean_bench_log_leaves_three_sets(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "I'm here",
            steps=[
                tool("open_session"),
                message("Welcome back."),
            ],
        )
        await h.say(
            "bench 60 8,8,8",
            steps=[tool("log_sets", line="bench 60 8,8,8"), message("Logged bench.")],
        )

        by_ex = await h.current_sets_by_exercise()
        assert by_ex == {
            "bench press": [
                {"weight": 60.0, "reps": 8, "rpe": None, "note": None},
                {"weight": 60.0, "reps": 8, "rpe": None, "note": None},
                {"weight": 60.0, "reps": 8, "rpe": None, "note": None},
            ]
        }
        session = await h.stores.training.get_session(
            (await h.stores.training.current_session_sets(h.member_id))[0].session_id
        )
        assert session.gym_id == h.gym_id
        assert session.member_id == h.member_id
        assert session.closed_at is None


async def test_clean_squat_log_resolves_alias(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "squats 100 5,5,5",
            steps=[
                tool("open_session"),
                tool("log_sets", line="squats 100 5,5,5"),
                message("Squat logged."),
            ],
        )
        by_ex = await h.current_sets_by_exercise()
        assert list(by_ex) == ["squat"]
        assert [s["reps"] for s in by_ex["squat"]] == [5, 5, 5]
        assert by_ex["squat"][0]["weight"] == 100.0


async def test_slash_rep_shorthand_logs_each_set(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "ohp 40 8/7/6",
            steps=[
                tool("open_session"),
                tool("log_sets", line="ohp 40 8/7/6"),
                message("OHP logged."),
            ],
        )
        by_ex = await h.current_sets_by_exercise()
        assert list(by_ex) == ["overhead press"]
        assert [s["reps"] for s in by_ex["overhead press"]] == [8, 7, 6]
        assert by_ex["overhead press"][0]["weight"] == 40.0


async def test_dips_bodyweight_style_line(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "dips 10,10,9",
            steps=[
                tool("open_session"),
                tool("log_sets", line="dips 10,10,9"),
                message("Dips logged."),
            ],
        )
        by_ex = await h.current_sets_by_exercise()
        assert list(by_ex) == ["dips"]
        assert [s["reps"] for s in by_ex["dips"]] == [10, 10, 9]


async def test_two_exercises_in_one_session(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "I'm here",
            steps=[tool("open_session"), message("Let's go.")],
        )
        await h.say(
            "bench 60 8,8,8",
            steps=[tool("log_sets", line="bench 60 8,8,8"), message("Bench ok.")],
        )
        await h.say(
            "row 50 8,8,8",
            steps=[tool("log_sets", line="row 50 8,8,8"), message("Rows ok.")],
        )
        by_ex = await h.current_sets_by_exercise()
        assert set(by_ex) == {"bench press", "barbell row"}
        assert len(by_ex["bench press"]) == 3
        assert len(by_ex["barbell row"]) == 3


async def test_close_session_marks_session_closed_with_set_count(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "bench 60 8,8,8",
            steps=[
                tool("open_session"),
                tool("log_sets", line="bench 60 8,8,8"),
                message("Logged."),
            ],
        )
        open_sets = await h.stores.training.current_session_sets(h.member_id)
        session_id = open_sets[0].session_id

        await h.say(
            "done",
            steps=[tool("close_session"), message("Solid session.")],
        )

        session = await h.stores.training.get_session(session_id)
        assert session.closed_at is not None
        assert await h.stores.training.current_session_sets(h.member_id) == []


async def test_open_alone_creates_a_visit_with_no_sets(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "I'm here",
            steps=[tool("open_session"), message("Welcome.")],
        )
        # Open session exists (reopened path) with zero sets.
        opened = await h.stores.training.open_session(h.member_id, h.gym_id)
        assert opened.reopened is True
        assert await h.stores.training.current_session_sets(h.member_id) == []
