"""Seed realistic demo data into a gym for the roster pilot (issue #149).

Usage: ``python -m agentg.scripts.seed_demo <gym_id> <coach_member_id>``

The gym must already exist and the member must be a coach. This is a
management/CLI-only tool — it is NOT exposed over HTTP.
"""

from __future__ import annotations

import asyncio
import random
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from agentg.checkin import LAPSED, OFF, ON, SNOOZED
from agentg.config import Settings
from agentg.dashboard_store import DashboardStore
from agentg.db import create_engine
from agentg.linking_store import LinkingStore
from agentg.models import Exercise, Gym, Member, MemberNote, Routine, Session, Set
from agentg.routines import ExerciseSpec, WorkoutSpec


async def seed_demo_data(
    store: DashboardStore,
    linking: LinkingStore,
    gym_id: int,
    coach_member_id: int,
) -> dict:
    """Seed realistic demo data for the roster pilot (issue #149).

    Creates members at varying stages — some with routines and session
    history, some new with no sessions, some snoozed, some lapsed — so
    the roster reads populated, not hollow, when judged against the #133
    visual bar.

    Returns a summary of what was created.
    """
    now = datetime.now(UTC)
    today = now.date()

    member_defs: list[dict] = [
        # name, checkin_state, snoozed_until offset, gap offset (days since last session),
        # has_routine, session_count, missed_window_days
        {"name": "Elena Vargas", "state": ON, "gap": 1, "routine": True, "sessions": 12, "missed": 3},
        {"name": "Marcus Chen", "state": ON, "gap": 2, "routine": True, "sessions": 8, "missed": 1},
        {"name": "Sofia Ricci", "state": ON, "gap": 0, "routine": True, "sessions": 20, "missed": 0},
        {"name": "Jamal Owens", "state": ON, "gap": 6, "routine": True, "sessions": 4, "missed": 5},
        {"name": "Yuki Tanaka", "state": ON, "gap": 3, "routine": True, "sessions": 15, "missed": 2},
        {"name": "Piotr Nowak", "state": SNOOZED, "gap": 8, "routine": True, "sessions": 6, "missed": 4,
         "snoozed_days": 4},
        {"name": "Clara Beaufort", "state": ON, "gap": 1, "routine": False, "sessions": 0, "missed": 0},
        {"name": "Ravi Kapoor", "state": LAPSED, "gap": 18, "routine": True, "sessions": 3, "missed": 12},
        {"name": "Leila Haddad", "state": ON, "gap": 4, "routine": True, "sessions": 10, "missed": 3},
        {"name": "Tom Bakker", "state": ON, "gap": 0, "routine": True, "sessions": 7, "missed": 0},
        {"name": "Nia Mensah", "state": OFF, "gap": 10, "routine": False, "sessions": 0, "missed": 0},
        {"name": "Diego Moretti", "state": ON, "gap": 5, "routine": True, "sessions": 2, "missed": 6,
         "safety_flag": "sharp knee pain reported"},
    ]

    # Exercises for sessions
    exercises_list = [
        "squat", "bench press", "deadlift", "overhead press", "barbell row",
        "pull-up", "dip", "lunge", "leg press", "bicep curl",
        "tricep extension", "lat pulldown", "calf raise", "plank",
    ]

    async with store.session() as db:
        # Create exercises if they don't exist
        for ex_name in exercises_list:
            existing = await db.scalar(
                select(Exercise).where(Exercise.name == ex_name)
            )
            if existing is None:
                db.add(Exercise(name=ex_name))
        await db.commit()

    created_members = 0
    created_sessions = 0
    created_routines = 0

    for i, md in enumerate(member_defs):
        # Create member and channel
        member = await linking.link_member(
            gym_id, md["name"], "telegram", f"demo-g{gym_id}-{i:04d}"
        )
        created_members += 1

        # Set checkin state
        async with store.session() as db:
            m = await db.get(Member, member.id)
            m.checkin_state = md["state"]
            if md.get("snoozed_days"):
                m.snoozed_until = today + timedelta(days=md["snoozed_days"])
            await db.commit()

        gap_days = md["gap"]
        session_count = md.get("sessions", 0)

        # Create sessions going back from the gap
        if session_count > 0 and md.get("routine"):
            # First create a routine for the member
            workouts_data = [
                {"weekday": 0, "name": "Push", "exercises": [
                    {"exercise": "bench press", "sets": 4, "reps": "8-10"},
                    {"exercise": "overhead press", "sets": 3, "reps": "10-12"},
                    {"exercise": "dip", "sets": 3, "reps": "8-12"},
                ]},
                {"weekday": 2, "name": "Pull", "exercises": [
                    {"exercise": "deadlift", "sets": 4, "reps": "5"},
                    {"exercise": "barbell row", "sets": 3, "reps": "8-10"},
                    {"exercise": "pull-up", "sets": 3, "reps": "6-8"},
                ]},
                {"weekday": 4, "name": "Legs", "exercises": [
                    {"exercise": "squat", "sets": 4, "reps": "8-10"},
                    {"exercise": "lunge", "sets": 3, "reps": "10"},
                    {"exercise": "calf raise", "sets": 3, "reps": "15"},
                ]},
            ]
            routine = await store.save_routine_from_web(
                gym_id, member.id, coach_member_id, None, [
                    _spec_from_dict(w) for w in workouts_data
                ]
            )
            created_routines += 1

            # Create sessions on various days
            session_dates = []
            for s in range(session_count):
                days_ago = gap_days + s * 2 + (s % 3)  # stagger sessions
                session_date = today - timedelta(days=days_ago)
                session_dates.append(session_date)

            # Back-date the Routine so it governs the period from the
            # first session to yesterday, putting planned weekdays inside
            # the severity window (issue #149, PR review).
            oldest_session = min(session_dates)
            async with store.session() as db:
                routine_row = await db.get(Routine, routine.id)
                routine_row.created_at = datetime.combine(
                    oldest_session - timedelta(days=1),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
                await db.commit()

            for sd in session_dates:
                started = datetime.combine(
                    sd, datetime.min.time(), tzinfo=UTC
                ).replace(hour=10 + (len(session_dates) % 6))
                session = Session(
                    gym_id=gym_id,
                    member_id=member.id,
                    started_at=started,
                    closed_at=started + timedelta(hours=1),
                )
                async with store.session() as db:
                    db.add(session)
                    await db.flush()
                    # Add 2-4 random sets
                    rng = random.Random(member.id * 1000 + len(session_dates))
                    n_sets = rng.randint(2, 4)
                    for _ in range(n_sets):
                        ex = rng.choice(exercises_list[:8])
                        ex_id = await db.scalar(
                            select(Exercise.id).where(Exercise.name == ex)
                        )
                        db.add(Set(
                            gym_id=gym_id,
                            session_id=session.id,
                            exercise_id=ex_id,
                            weight=round(rng.uniform(20, 100), 1),
                            reps=rng.randint(5, 12),
                            created_at=started,
                        ))
                    await db.commit()
                created_sessions += 1

        # Add safety flag if specified
        if md.get("safety_flag"):
            async with store.session() as db:
                db.add(MemberNote(
                    member_id=member.id,
                    gym_id=gym_id,
                    kind="safety",
                    text=md["safety_flag"],
                    created_at=now - timedelta(days=2),
                ))
                await db.commit()

    return {
        "members": created_members,
        "sessions": created_sessions,
        "routines": created_routines,
    }


def _spec_from_dict(d: dict) -> WorkoutSpec:
    """A WorkoutSpec from a plain dict for the seeder."""
    return WorkoutSpec(
        weekday=d["weekday"],
        name=d["name"],
        exercises=[
            ExerciseSpec(e["exercise"], e.get("sets"), e.get("reps"))
            for e in d["exercises"]
        ],
    )


async def _run(gym_id: int, coach_member_id: int) -> int:
    settings = Settings.from_env()
    engine = create_engine(settings.database_url)
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    store = DashboardStore(engine)

    # Verify the gym and coach exist
    async with store.session() as db:
        gym = await db.get(Gym, gym_id)
        if gym is None:
            print(f"Gym {gym_id} not found.", file=sys.stderr)
            return 1
        coach = await db.get(Member, coach_member_id)
        if coach is None:
            print(f"Coach member {coach_member_id} not found.", file=sys.stderr)
            return 1
        if coach.gym_id != gym_id:
            print(
                f"Coach member {coach_member_id} belongs to gym {coach.gym_id}, not {gym_id}.",
                file=sys.stderr,
            )
            return 1
        if not coach.is_coach:
            print(f"Member {coach_member_id} is not a coach.", file=sys.stderr)
            return 1

    result = await seed_demo_data(store, linking, gym_id, coach_member_id)
    await engine.dispose()
    print(f"Seeded: {result['members']} members, {result['sessions']} sessions, {result['routines']} routines")
    return 0


def main() -> None:
    if len(sys.argv) != 3:
        print(
            "usage: python -m agentg.scripts.seed_demo <gym_id> <coach_member_id>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        gym_id = int(sys.argv[1])
        coach_id = int(sys.argv[2])
    except ValueError:
        print("gym_id and coach_member_id must be integers", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(_run(gym_id, coach_id)))


if __name__ == "__main__":
    main()
