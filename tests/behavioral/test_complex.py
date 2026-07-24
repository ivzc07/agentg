"""Complex stratum: routine generation leaves structured plan state."""

from __future__ import annotations

from behavioral.harness import ConversationHarness, message, tool

_PUSH_PULL_LEGS = [
    {
        "weekday": 0,
        "name": "Push",
        "exercises": [
            {"exercise": "bench press", "sets": 3, "reps": "8"},
            {"exercise": "overhead press", "sets": 3, "reps": "8"},
            {"exercise": "dips", "sets": 3, "reps": "10"},
        ],
    },
    {
        "weekday": 2,
        "name": "Pull",
        "exercises": [
            {"exercise": "deadlift", "sets": 3, "reps": "5"},
            {"exercise": "barbell row", "sets": 3, "reps": "8"},
            {"exercise": "pull-up", "sets": 3, "reps": "6"},
        ],
    },
    {
        "weekday": 4,
        "name": "Legs",
        "exercises": [
            {"exercise": "squat", "sets": 3, "reps": "5"},
            {"exercise": "lunge", "sets": 3, "reps": "8"},
        ],
    },
]


async def test_routine_request_saves_catalog_only_weekdays(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()

        await h.say(
            "I want a 3-day push pull legs routine, Mon Wed Fri, intermediate, build muscle, no injuries",
            steps=[
                tool("get_rules_doc"),
                tool("list_exercises"),
                tool("save_routine", workouts=_PUSH_PULL_LEGS),
                message("Here's your plan."),
            ],
        )

        routine = await h.stores.routines.active_routine(h.member_id)
        assert routine is not None
        assert routine["coach_authored"] is False
        weekdays = sorted(w["weekday"] for w in routine["workouts"])
        assert weekdays == [0, 2, 4]
        names = {ex["exercise"] for w in routine["workouts"] for ex in w["exercises"]}
        catalog = set(await h.stores.training.catalog_names())
        assert names <= catalog
        # Structure only — no target weights on the plan.
        for w in routine["workouts"]:
            for ex in w["exercises"]:
                assert set(ex) <= {"exercise", "sets", "reps"}


async def test_routine_with_injury_records_note_and_stores_scripted_routine(tmp_path):
    # The scripted model picks the shoulder-friendly exercises here — this
    # proves the note + routine plumbing, not the real model's judgment
    # under injury (the judge layer covers that on the live path).
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()

        await h.say(
            "shoulder hurts, want upper/lower 4 days, beginner, general fitness",
            steps=[
                tool(
                    "remember_note",
                    kind="injury",
                    text="right shoulder pain",
                ),
                tool("get_rules_doc"),
                tool("list_exercises"),
                tool(
                    "save_routine",
                    workouts=[
                        {
                            "weekday": 0,
                            "name": "Lower A",
                            "exercises": [
                                {"exercise": "squat", "sets": 3, "reps": "8"},
                                {"exercise": "lunge", "sets": 3, "reps": "10"},
                            ],
                        },
                        {
                            "weekday": 1,
                            "name": "Upper A",
                            "exercises": [
                                # no overhead press / dips — shoulder-friendly
                                {"exercise": "barbell row", "sets": 3, "reps": "8"},
                                {"exercise": "lat pulldown", "sets": 3, "reps": "10"},
                            ],
                        },
                        {
                            "weekday": 3,
                            "name": "Lower B",
                            "exercises": [
                                {"exercise": "deadlift", "sets": 3, "reps": "5"},
                                {"exercise": "lunge", "sets": 3, "reps": "10"},
                            ],
                        },
                        {
                            "weekday": 4,
                            "name": "Upper B",
                            "exercises": [
                                {"exercise": "barbell row", "sets": 3, "reps": "8"},
                                {"exercise": "biceps curl", "sets": 3, "reps": "12"},
                            ],
                        },
                    ],
                ),
                message("Plan saved, shoulder protected."),
            ],
        )

        notes = await h.stores.notes.active(h.member_id)
        assert any("shoulder" in n.text.lower() for n in notes)
        assert any(n.kind == "injury" for n in notes)

        routine = await h.stores.routines.active_routine(h.member_id)
        assert routine is not None
        names = {ex["exercise"] for w in routine["workouts"] for ex in w["exercises"]}
        assert "overhead press" not in names
        assert "dips" not in names
        assert len(routine["workouts"]) == 4


async def test_replacing_a_routine_deactivates_the_old_one(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        first = [
            {
                "weekday": 0,
                "name": "Full body",
                "exercises": [{"exercise": "squat", "sets": 3, "reps": "5"}],
            }
        ]
        second = [
            {
                "weekday": 1,
                "name": "Upper",
                "exercises": [{"exercise": "bench press", "sets": 3, "reps": "8"}],
            },
            {
                "weekday": 4,
                "name": "Lower",
                "exercises": [{"exercise": "deadlift", "sets": 3, "reps": "5"}],
            },
        ]

        await h.say(
            "give me a simple monday full body",
            steps=[
                tool("list_exercises"),
                tool("save_routine", workouts=first),
                message("Full body saved."),
            ],
        )
        first_id = (await h.stores.routines.active_routine(h.member_id))["routine_id"]

        await h.say(
            "actually switch me to upper/lower Tue Fri",
            steps=[
                tool("list_exercises"),
                tool("save_routine", workouts=second),
                message("Switched."),
            ],
        )

        active = await h.stores.routines.active_routine(h.member_id)
        assert active is not None
        assert active["routine_id"] != first_id
        assert sorted(w["weekday"] for w in active["workouts"]) == [1, 4]


async def test_remember_goal_note_during_intake(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        await h.say(
            "my goal is to get stronger for rugby",
            steps=[
                tool("remember_note", kind="goal", text="get stronger for rugby"),
                message("Got it — strength for rugby."),
            ],
        )
        notes = await h.stores.notes.active(h.member_id)
        assert len(notes) == 1
        assert notes[0].kind == "goal"
        assert "rugby" in notes[0].text


async def test_retire_injury_note_when_healed(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        note = await h.stores.notes.remember(
            h.member_id, h.gym_id, "injury", "left knee twinge"
        )
        await h.say(
            "knee is fine now",
            steps=[
                tool("retire_note", note_id=note.id),
                message("Great — knee cleared."),
            ],
        )
        active = await h.stores.notes.active(h.member_id)
        assert active == []
