"""The Session loop: open, log, correct, close (spec §The logging conversation).

All training facts flow through these methods — the Agent's tools are thin
wrappers, and nothing here trusts chat history. The clock is injected so
gap math and the auto-close timeout are testable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentg.catalog import find_exercise, find_or_create_exercise, normalize_exercise_name
from agentg.models import Exercise, Gym, Member, Session, Set
from agentg.parsing import parse_set_line
from agentg.timezones import local_date

# Build-time choice (#26): a Session abandoned without "done" closes itself
# this long after its last activity, as of that activity.
SESSION_AUTO_CLOSE = timedelta(hours=3)

KG_PER_LB = 0.45359237

# A logged weight beyond this multiple of the Member's own last top set is
# still stored (the Member is right once they confirm) but flagged so the
# Agent double-checks conversationally — guards against plausible-but-wrong
# parses (unit mix-up, swapped weight/reps) poisoning future sessions.
SUSPECT_WEIGHT_MULTIPLE = 2.0

SEED_EXERCISES: dict[str, tuple[str, ...]] = {
    "bench press": ("bench",),
    "overhead press": ("ohp", "press", "shoulder press"),
    "squat": ("squats", "back squat"),
    "deadlift": ("deadlifts", "dl"),
    "dips": ("dip",),
    "pull-up": ("pull up", "pull ups", "pullup", "pullups", "chin-up"),
    "barbell row": ("row", "rows", "bent over row"),
    "lat pulldown": ("pulldown", "pulldowns"),
    "lunge": ("lunges",),
    "biceps curl": ("curl", "curls"),
}

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize(name: str) -> str:
    return normalize_exercise_name(name)


def _suspect_hint(weight: float | None, previous: dict[str, Any] | None) -> str | None:
    """Flag a weight that jumps beyond SUSPECT_WEIGHT_MULTIPLE × the last top set."""
    if weight is None or previous is None:
        return None
    last = previous.get("weight")
    if last is None or last <= 0:
        return None
    if weight > SUSPECT_WEIGHT_MULTIPLE * last:
        return (
            f"{weight:g} is more than {SUSPECT_WEIGHT_MULTIPLE:g}× last time's "
            f"{last:g} — double-check with the Member"
        )
    return None


@dataclass(frozen=True)
class OpenedSession:
    session_id: int
    reopened: bool
    days_since_last: int | None
    last_session: dict[str, Any] | None  # {"date", "exercises": [...]}


@dataclass(frozen=True)
class LoggedSets:
    exercise: str
    weight: float | None
    reps: list[int]
    previous: dict[str, Any] | None  # that exercise's previous-session numbers
    suspect: str | None = None  # hint when the weight jumps implausibly vs history


@dataclass(frozen=True)
class SessionSummary:
    session_id: int
    total_sets: int
    exercises: list[dict[str, Any]]


class TrainingStore:
    def __init__(self, engine: AsyncEngine, clock: Clock = _utcnow) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

    async def ensure_seeded(self) -> None:
        async with self._sessions() as db:
            existing = set((await db.scalars(select(Exercise.name))).all())
            for name, aliases in SEED_EXERCISES.items():
                if name not in existing:
                    db.add(Exercise(name=name, aliases=",".join(aliases)))
            await db.commit()

    # --- Sessions ---

    async def open_session(self, member_id: int, gym_id: int) -> OpenedSession:
        async with self._sessions() as db:
            now = self._clock()
            existing = await self._open_session_row(db, member_id)
            if existing is not None:
                await db.commit()
                days, last = await self._previous_session_info(db, member_id, existing.id, now)
                return OpenedSession(existing.id, True, days, last)
            days, last = await self._previous_session_info(db, member_id, None, now)
            session = Session(gym_id=gym_id, member_id=member_id, started_at=now)
            db.add(session)
            await db.commit()
            return OpenedSession(session.id, False, days, last)

    async def close_session(self, member_id: int) -> SessionSummary:
        async with self._sessions() as db:
            session = await self._open_session_row(db, member_id)
            if session is None:
                await db.commit()  # persists any auto-close that just happened
                raise ValueError(
                    "no open session to close — tell the Member nothing was open, "
                    "or call open_session if they're starting now"
                )
            lines = await self._session_exercises(db, session.id)
            total = 0
            for line in lines:
                total += len(line["reps"])
                previous = await self._last_sets_info(
                    db, member_id, line.pop("exercise_id"), exclude_session_id=session.id
                )
                line["previous_weight"] = previous["weight"] if previous else None
                line["previous_reps"] = previous["reps"] if previous else None
                line["weight_change"] = (
                    line["weight"] - previous["weight"]
                    if previous and line["weight"] is not None and previous["weight"] is not None
                    else None
                )
                line["reps_change"] = sum(line["reps"]) - sum(previous["reps"]) if previous else None
            session.closed_at = self._clock()
            await db.commit()
            return SessionSummary(session_id=session.id, total_sets=total, exercises=lines)

    async def latest_session_info(
        self, member_id: int
    ) -> tuple[int | None, dict[str, Any] | None]:
        """Days since the Member's last *prior* Session and its headline.

        The currently-open Session (today's visit, once "I'm here" opens it)
        is excluded, so the gap reflects the time off *before* today — that is
        what the opener and the ease-back suggestions need. Derived, never
        stored. A stale open Session is auto-closed first, so it counts.
        """
        async with self._sessions() as db:
            open_session = await self._open_session_row(db, member_id)
            exclude_id = open_session.id if open_session is not None else None
            info = await self._previous_session_info(db, member_id, exclude_id, self._clock())
            await db.commit()  # persist any auto-close the open-session check did
            return info

    def today(self, timezone: str = "UTC") -> date:
        """The current date in the Gym's timezone (issue #95) — the Agent
        computes snooze dates from it, and the sweep compares those against
        gym-local days."""
        return local_date(self._clock(), timezone)

    async def newest_session_date(self, member_id: int) -> date | None:
        """The gym-local date of the Member's most recent Session (open or
        closed).

        Any visit counts as activity for the check-in gap, so unlike
        ``latest_session_info`` this includes today's open Session.
        """
        async with self._sessions() as db:
            started = await db.scalar(
                select(Session.started_at)
                .where(Session.member_id == member_id)
                .order_by(Session.started_at.desc())
                .limit(1)
            )
            if started is None:
                return None
            timezone = await self._member_timezone(db, member_id)
            return local_date(started, timezone)

    async def get_session(self, session_id: int) -> Session:
        async with self._sessions() as db:
            session = await db.get(Session, session_id)
            if session is None:
                raise ValueError(f"no session {session_id}")
            return session

    # --- Sets ---

    async def log_sets(
        self,
        member_id: int,
        gym_id: int,
        line: str,
        exercise: str | None = None,
        rpe: float | None = None,
        note: str | None = None,
    ) -> LoggedSets:
        parsed = parse_set_line(line)
        if parsed is None:
            raise ValueError(
                f"could not parse {line!r} as sets — shorthand like 'bench 60 8,8,8' works"
            )
        name = parsed.exercise or exercise
        if not name:
            raise ValueError("the line names no exercise — pass the exercise it belongs to")
        async with self._sessions() as db:
            resolved = await self._match_or_create(db, name)
            session = await self._require_open_session(db, member_id, gym_id)
            weight = await self._to_gym_unit(db, gym_id, parsed.weight, parsed.unit)
            previous = await self._last_sets_info(
                db, member_id, resolved.id, exclude_session_id=session.id
            )
            self._add_sets(
                db, session, resolved.id, weight, parsed.reps, self._clock(), rpe=rpe, note=note
            )
            await db.commit()
            return LoggedSets(
                resolved.name,
                weight,
                list(parsed.reps),
                previous,
                suspect=_suspect_hint(weight, previous),
            )

    async def copy_last_sets(self, member_id: int, gym_id: int, exercise: str) -> LoggedSets:
        async with self._sessions() as db:
            resolved = await self._match_or_create(db, exercise)
            session = await self._require_open_session(db, member_id, gym_id)
            previous = await self._last_sets_info(
                db, member_id, resolved.id, exclude_session_id=session.id
            )
            if previous is None:
                raise ValueError(
                    f"no earlier sets of {resolved.name} to copy — check the exercise "
                    "name, or ask the Member for the weight and reps to log fresh"
                )
            self._add_sets(
                db, session, resolved.id, previous["weight"], previous["reps"], self._clock()
            )
            await db.commit()
            return LoggedSets(resolved.name, previous["weight"], list(previous["reps"]), previous)

    async def edit_logged_sets(
        self,
        member_id: int,
        exercise: str,
        weight: float | None = None,
        reps: list[int] | None = None,
    ) -> LoggedSets:
        """Correct the just-logged Sets of one Exercise — the latest batch in
        the current Session, so an earlier warm-up batch stays untouched."""
        async with self._sessions() as db:
            resolved = await self._find_exercise(db, _normalize(exercise))
            session = await self._open_session_row(db, member_id)
            rows: list[Set] = []
            if resolved is not None and session is not None:
                rows = list(
                    await db.scalars(
                        select(Set)
                        .where(Set.session_id == session.id, Set.exercise_id == resolved.id)
                        .order_by(Set.id)
                    )
                )
            if not rows:
                await db.commit()  # persists any auto-close that just happened
                raise ValueError(
                    f"no {exercise} sets in the current session to edit — check the "
                    "exercise name matches what was just logged, or ask the Member "
                    "what to change"
                )
            batch_time = max(row.created_at for row in rows)  # one log call = one batch
            batch = [row for row in rows if row.created_at == batch_time]
            if weight is not None:
                for row in batch:
                    row.weight = weight
            if reps is not None:
                for row, rep in zip(batch, reps):
                    row.reps = rep
                for row in batch[len(reps) :]:
                    await db.delete(row)
                template = batch[0]
                for rep in reps[len(batch) :]:
                    db.add(
                        Set(
                            gym_id=template.gym_id,
                            session_id=template.session_id,
                            exercise_id=template.exercise_id,
                            weight=weight if weight is not None else template.weight,
                            reps=rep,
                            created_at=batch_time,  # stays correctable as one batch
                        )
                    )
            assert resolved is not None  # rows exist, so the exercise did too
            previous = await self._last_sets_info(
                db, member_id, resolved.id, exclude_session_id=session.id if session else None
            )
            await db.commit()
            final_weight = weight if weight is not None else batch[0].weight
            final_reps = list(reps) if reps is not None else [row.reps for row in batch]
            return LoggedSets(
                resolved.name,
                final_weight,
                final_reps,
                previous,
                suspect=_suspect_hint(final_weight, previous),
            )

    async def last_sets(self, member_id: int, exercise: str) -> dict[str, Any] | None:
        """The previous Session's numbers for an Exercise (never the open one)."""
        async with self._sessions() as db:
            resolved = await self._find_exercise(db, _normalize(exercise))
            if resolved is None:
                return None
            current = await self._open_session_row(db, member_id)
            info = await self._last_sets_info(
                db, member_id, resolved.id, exclude_session_id=current.id if current else None
            )
            await db.commit()
            return info

    async def current_session_sets(self, member_id: int) -> list[Set]:
        async with self._sessions() as db:
            session = await self._open_session_row(db, member_id)
            if session is None:
                await db.commit()
                return []
            rows = list(
                await db.scalars(
                    select(Set).where(Set.session_id == session.id).order_by(Set.id)
                )
            )
            await db.commit()
            return rows

    # --- Exercises ---

    async def match_or_create_exercise(self, text: str) -> Exercise:
        async with self._sessions() as db:
            resolved = await self._match_or_create(db, text)
            await db.commit()
            return resolved

    async def catalog_names(self) -> list[str]:
        """The Exercise catalog a Routine may draw from (spec §Routine gen)."""
        async with self._sessions() as db:
            return sorted((await db.scalars(select(Exercise.name))).all())

    async def exercise_history(
        self, member_id: int, exercise: str, limit: int
    ) -> list[dict[str, Any]]:
        """An Exercise's recent CLOSED Sessions, most-recent-first.

        Each entry is the top working weight that Session and the reps of the
        sets at that weight — the input the weight suggester reasons over. The
        open Session (today, in progress) is excluded so a suggestion for now
        never reads from itself.
        """
        result = await self.exercise_history_batch(member_id, [exercise], limit)
        return result.get(exercise, [])

    async def exercise_history_batch(
        self, member_id: int, exercises: list[str], limit: int
    ) -> dict[str, list[dict[str, Any]]]:
        """Like ``exercise_history`` but for multiple Exercises at once.

        Gathers the same information in a small constant number of queries
        instead of one per Exercise per past Session (issue #170).
        """
        async with self._sessions() as db:
            # Resolve every exercise name to an id in one pass over the
            # catalog (the catalog stays small — issue #162 will index it).
            all_exercises = list(await db.scalars(select(Exercise)))
            name_to_id: dict[str, int] = {}
            for ex_name in exercises:
                norm = _normalize(ex_name)
                found = False
                for row in all_exercises:
                    if row.name == norm:
                        name_to_id[ex_name] = row.id
                        found = True
                        break
                if not found:
                    for row in all_exercises:
                        if norm in [a for a in row.aliases.split(",") if a]:
                            name_to_id[ex_name] = row.id
                            break
            ex_ids = list(name_to_id.values())
            result: dict[str, list[dict[str, Any]]] = {
                ex: [] for ex in exercises
            }
            if not ex_ids:
                return result

            # One query: all sets for all requested Exercises in the Member's
            # closed Sessions, most-recent-first so the per-Exercise limit
            # is applied in Python.
            rows = list(
                await db.execute(
                    select(
                        Set.session_id,
                        Set.exercise_id,
                        Set.weight,
                        Set.reps,
                        Session.started_at,
                    )
                    .join(Session, Set.session_id == Session.id)
                    .where(
                        Session.member_id == member_id,
                        Session.closed_at.is_not(None),
                        Set.exercise_id.in_(ex_ids),
                    )
                    .order_by(Session.started_at.desc())
                )
            )

            # Group by exercise, then by session; enforce the per-Exercise
            # limit on the grouped sessions (most-recent-first from the
            # ordering above).
            ex_sessions: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
            for session_id, ex_id, weight, reps, started_at in rows:
                if session_id not in ex_sessions[ex_id]:
                    ex_sessions[ex_id][session_id] = {
                        "started_at": started_at,
                        "weight_reps": [],
                    }
                ex_sessions[ex_id][session_id]["weight_reps"].append((weight, reps))

            id_to_names: dict[int, list[str]] = defaultdict(list)
            for ex_name, ex_id in name_to_id.items():
                id_to_names[ex_id].append(ex_name)
            for ex_id, sessions in ex_sessions.items():
                sorted_sessions = sorted(
                    sessions.items(),
                    key=lambda kv: kv[1]["started_at"],
                    reverse=True,
                )[:limit]
                history: list[dict[str, Any]] = []
                for _session_id, data in sorted_sessions:
                    weights = [w for w, _r in data["weight_reps"] if w is not None]
                    top_weight = max(weights) if weights else None
                    top_reps = [
                        r for w, r in data["weight_reps"] if w == top_weight
                    ]
                    history.append({"top_weight": top_weight, "top_reps": top_reps})
                for ex_name in id_to_names[ex_id]:
                    result[ex_name] = history

            return result

    def _add_sets(
        self,
        db: AsyncSession,
        session: Session,
        exercise_id: int,
        weight: float | None,
        reps: list[int],
        created_at: datetime,
        rpe: float | None = None,
        note: str | None = None,
    ) -> None:
        for rep in reps:
            db.add(
                Set(
                    gym_id=session.gym_id,
                    session_id=session.id,
                    exercise_id=exercise_id,
                    weight=weight,
                    reps=rep,
                    rpe=rpe,
                    note=note,
                    created_at=created_at,
                )
            )

    async def _match_or_create(self, db: AsyncSession, text: str) -> Exercise:
        # A Member's reported movement is a fact — record it, never drop it.
        return await find_or_create_exercise(db, text)

    async def _find_exercise(self, db: AsyncSession, norm: str) -> Exercise | None:
        return await find_exercise(db, norm)

    # --- internals ---

    async def _open_session_row(self, db: AsyncSession, member_id: int) -> Session | None:
        """The member's open Session — auto-closing it first if it went stale."""
        session = await db.scalar(
            select(Session)
            .where(Session.member_id == member_id, Session.closed_at.is_(None))
            .order_by(Session.started_at.desc())
        )
        if session is None:
            return None
        last_activity = await self._last_activity(db, session)
        if self._clock() - last_activity > SESSION_AUTO_CLOSE:
            session.closed_at = last_activity  # closed as of when it was abandoned
            await db.flush()
            return None
        return session

    async def _require_open_session(
        self, db: AsyncSession, member_id: int, gym_id: int
    ) -> Session:
        session = await self._open_session_row(db, member_id)
        if session is None:  # logging sets implies being at the gym
            session = Session(gym_id=gym_id, member_id=member_id, started_at=self._clock())
            db.add(session)
            await db.flush()
        return session

    async def _last_activity(self, db: AsyncSession, session: Session) -> datetime:
        newest_set = await db.scalar(
            select(Set.created_at)
            .where(Set.session_id == session.id)
            .order_by(Set.created_at.desc())
            .limit(1)
        )
        return newest_set or session.started_at

    async def _previous_session_info(
        self, db: AsyncSession, member_id: int, exclude_session_id: int | None, now: datetime
    ) -> tuple[int | None, dict[str, Any] | None]:
        query = (
            select(Session)
            .where(Session.member_id == member_id)
            .order_by(Session.started_at.desc())
            .limit(1)
        )
        if exclude_session_id is not None:
            query = query.where(Session.id != exclude_session_id)
        previous = await db.scalar(query)
        if previous is None:
            return None, None
        timezone = await self._member_timezone(db, member_id)
        # Day boundaries are gym-local (issue #95): an evening visit can fall
        # after UTC midnight and must still count on the local day.
        last_on = local_date(previous.started_at, timezone)
        days = (local_date(now, timezone) - last_on).days
        lines = await self._session_exercises(db, previous.id)
        for line in lines:
            line.pop("exercise_id")
        return days, {"date": last_on.isoformat(), "exercises": lines}

    async def _member_timezone(self, db: AsyncSession, member_id: int) -> str:
        timezone = await db.scalar(
            select(Gym.timezone).join(Member, Member.gym_id == Gym.id).where(Member.id == member_id)
        )
        return timezone or "UTC"

    async def _session_exercises(self, db: AsyncSession, session_id: int) -> list[dict[str, Any]]:
        rows = (
            await db.execute(
                select(Set, Exercise.name)
                .join(Exercise, Set.exercise_id == Exercise.id)
                .where(Set.session_id == session_id)
                .order_by(Set.id)
            )
        ).all()
        by_exercise: dict[str, dict[str, Any]] = {}
        for row, name in rows:
            entry = by_exercise.setdefault(
                name,
                {"exercise": name, "exercise_id": row.exercise_id, "weight": None, "reps": []},
            )
            # Report the top set: a 40 kg warm-up must not mask the 60 kg work.
            if row.weight is not None and (entry["weight"] is None or row.weight > entry["weight"]):
                entry["weight"] = row.weight
            entry["reps"].append(row.reps)
        return list(by_exercise.values())

    async def _last_sets_info(
        self, db: AsyncSession, member_id: int, exercise_id: int, exclude_session_id: int | None
    ) -> dict[str, Any] | None:
        query = (
            select(Session.id)
            .join(Set, Set.session_id == Session.id)
            .where(Session.member_id == member_id, Set.exercise_id == exercise_id)
            .order_by(Session.started_at.desc())
            .limit(1)
        )
        if exclude_session_id is not None:
            query = query.where(Session.id != exclude_session_id)
        session_id = await db.scalar(query)
        if session_id is None:
            return None
        rows = list(
            await db.scalars(
                select(Set)
                .where(Set.session_id == session_id, Set.exercise_id == exercise_id)
                .order_by(Set.id)
            )
        )
        weights = [row.weight for row in rows if row.weight is not None]
        return {  # top-set weight: warm-ups must not set the reference
            "weight": max(weights) if weights else None,
            "reps": [row.reps for row in rows],
        }

    async def _to_gym_unit(
        self, db: AsyncSession, gym_id: int, weight: float | None, unit: str | None
    ) -> float | None:
        if weight is None or unit is None:
            return weight
        gym = await db.get(Gym, gym_id)
        gym_unit = gym.weight_unit if gym is not None else "kg"
        if unit == gym_unit:
            return weight
        if unit == "lb":  # typed in pounds at a kg gym
            return round(weight * KG_PER_LB, 2)
        return round(weight / KG_PER_LB, 2)  # typed in kg at a lb gym
