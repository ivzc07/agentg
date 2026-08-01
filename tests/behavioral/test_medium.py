"""Medium stratum: context-dependent logging (shorthand, copy, correct)."""

from __future__ import annotations

from behavioral.harness import ConversationHarness, message, tool


async def test_bare_numbers_attach_to_exercise_under_discussion(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "I'm here",
            steps=[tool("open_session"), message("Bench next.")],
        )
        # Member omits the exercise name; the agent supplies it from context.
        await h.say(
            "60 8/7/6",
            steps=[
                tool("log_sets", line="60 8/7/6", exercise="bench press"),
                message("Bench 60 ×8 ×7 ×6."),
            ],
        )
        by_ex = await h.current_sets_by_exercise()
        assert list(by_ex) == ["bench press"]
        assert [s["reps"] for s in by_ex["bench press"]] == [8, 7, 6]
        assert by_ex["bench press"][0]["weight"] == 60.0


async def test_same_as_last_time_copies_previous_sets(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("overhead press 40 8,7,6")

        await h.say(
            "I'm here",
            steps=[tool("open_session"), message("Push day.")],
        )
        await h.say(
            "same as last time",
            steps=[
                tool("copy_last_sets", exercise="overhead press"),
                message("OHP copied."),
            ],
        )
        by_ex = await h.current_sets_by_exercise()
        assert list(by_ex) == ["overhead press"]
        assert by_ex["overhead press"][0]["weight"] == 40.0
        assert [s["reps"] for s in by_ex["overhead press"]] == [8, 7, 6]


async def test_correction_rewrites_weight_on_current_session_only(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("bench 60 8,8,7")

        await h.say(
            "bench 60 8,8,8",
            steps=[
                tool("open_session"),
                tool("log_sets", line="bench 60 8,8,8"),
                message("Bench logged."),
            ],
        )
        await h.say(
            "actually bench was 62.5 not 60",
            steps=[
                tool("edit_logged_sets", exercise="bench press", weight=62.5),
                message("Fixed to 62.5."),
            ],
        )

        by_ex = await h.current_sets_by_exercise()
        assert {s["weight"] for s in by_ex["bench press"]} == {62.5}
        # Prior closed session is untouched.
        prior = await h.stores.training.last_sets(h.member_id, "bench press")
        # After edit, last_sets still reads previous closed session when
        # excluding current — weight there stays 60.
        assert prior is not None
        assert prior["weight"] == 60.0


async def test_chatter_wrapped_line_logs_shorthand_and_optional_note(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "bench 60 8,8,8 felt heavy",
            steps=[
                tool("open_session"),
                tool("log_sets", line="bench 60 8,8,8", note="felt heavy"),
                message("Logged — felt heavy noted."),
            ],
        )
        by_ex = await h.current_sets_by_exercise()
        assert by_ex["bench press"][0]["weight"] == 60.0
        assert by_ex["bench press"][0]["note"] == "felt heavy"


async def test_full_push_day_with_copy_and_correction(tmp_path):
    """Prototype Variant A shape: open → log → copy → correct → close."""
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session(
            "bench 60 8,8,7",
            "overhead press 40 8,7,6",
            "dips 10,10,8",
        )

        await h.say(
            "I'm here",
            steps=[
                tool("open_session"),
                message("Push day."),
            ],
        )
        await h.say(
            "bench 60 8,8,8",
            steps=[tool("log_sets", line="bench 60 8,8,8"), message("Bench ok.")],
        )
        await h.say(
            "same as last time",
            steps=[
                tool("copy_last_sets", exercise="overhead press"),
                message("OHP copied."),
            ],
        )
        await h.say(
            "dips 10,10,9",
            steps=[tool("log_sets", line="dips 10,10,9"), message("Dips ok.")],
        )
        await h.say(
            "actually bench was 62.5 not 60",
            steps=[
                tool("edit_logged_sets", exercise="bench press", weight=62.5),
                message("Fixed."),
            ],
        )
        open_sets = await h.stores.training.current_session_sets(h.member_id)
        session_id = open_sets[0].session_id
        await h.say(
            "done",
            steps=[tool("close_session"), message("Done.")],
        )

        session = await h.stores.training.get_session(session_id)
        assert session.closed_at is not None
        assert session.gym_id == h.gym_id
        # Re-open nothing; inspect via last_sets per exercise.
        bench = await h.stores.training.last_sets(h.member_id, "bench press")
        ohp = await h.stores.training.last_sets(h.member_id, "overhead press")
        dips = await h.stores.training.last_sets(h.member_id, "dips")
        assert bench is not None and bench["weight"] == 62.5 and bench["reps"] == [8, 8, 8]
        assert ohp is not None and ohp["weight"] == 40.0 and ohp["reps"] == [8, 7, 6]
        assert dips is not None and dips["reps"] == [10, 10, 9]


async def test_get_last_sets_does_not_mutate_state(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.seed_closed_session("squat 100 5,5,5")
        before = await h.stores.training.last_sets(h.member_id, "squat")

        await h.say(
            "what did I squat last time?",
            steps=[
                tool("get_last_sets", exercise="squat"),
                message("You hit 100 for 5/5/5."),
            ],
        )

        after = await h.stores.training.last_sets(h.member_id, "squat")
        assert after == before
        assert await h.stores.training.current_session_sets(h.member_id) == []
