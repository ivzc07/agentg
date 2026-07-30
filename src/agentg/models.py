"""Domain tables (docs/spec.md §Data model). Every domain row carries gym_id."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class TZDateTime(TypeDecorator[datetime]):
    """Aware-UTC datetimes that round-trip identically on Postgres and SQLite."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


class Gym(Base):
    """The tenant every record belongs to (CONTEXT.md)."""

    __tablename__ = "gyms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # One active, regenerable Invite code per Gym; stored lowercase.
    invite_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    weight_unit: Mapped[str] = mapped_column(String(8), default="kg")
    # The Gym's own plain-text rules doc, or NULL to follow the shipped
    # default. Exactly one doc governs generation (spec §Routine generation).
    rules_doc: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Member(Base):
    """A person who trains at a Gym; coaches are coach-flagged Members."""

    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    gym_id: Mapped[int] = mapped_column(ForeignKey("gyms.id"))
    name: Mapped[str] = mapped_column(String(100))
    is_coach: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Proactive check-in state (spec §Proactive check-ins).
    checkin_state: Mapped[str] = mapped_column(String(12), default="on")  # on/off/snoozed/lapsed
    snoozed_until: Mapped[date | None] = mapped_column(Date, default=None)
    last_nudge_on: Mapped[date | None] = mapped_column(Date, default=None)
    nudges_this_week: Mapped[int] = mapped_column(default=0)
    ignored_nudges: Mapped[int] = mapped_column(default=0)  # sends since a reply/Session


class MemberChannel(Base):
    """Maps a channel identity to its Member; re-pointed on a gym switch."""

    __tablename__ = "member_channels"
    __table_args__ = (
        UniqueConstraint("channel", "channel_user_id", name="uq_member_channels_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    gym_id: Mapped[int] = mapped_column(ForeignKey("gyms.id"))
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    channel: Mapped[str] = mapped_column(String(32))
    channel_user_id: Mapped[str] = mapped_column(String(64))


class Exercise(Base):
    """A named movement. Product-level catalog; a demo animation the Agent can
    send to show how it's done (spec §Exercise demo media)."""

    __tablename__ = "exercises"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # Comma-separated normalized alternate names ("bench" -> bench press).
    aliases: Mapped[str] = mapped_column(String(400), default="")
    # Filename of the canonical soundless MP4 in our media store (the system of
    # record), or NULL when this Exercise has no demo yet.
    demo_slug: Mapped[str | None] = mapped_column(String(200), default=None)


class DemoOverride(Base):
    """A Gym's own demo for an Exercise — wins over the Exercise default.

    Same media path as the default (a soundless MP4 slug in our store); a
    Coach's filmed clip lands here (spec §Exercise demo media)."""

    __tablename__ = "demo_overrides"
    __table_args__ = (
        UniqueConstraint("gym_id", "exercise_id", name="uq_demo_override_gym_exercise"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    gym_id: Mapped[int] = mapped_column(ForeignKey("gyms.id"))
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id"))
    demo_slug: Mapped[str] = mapped_column(String(200))


class DemoFileId(Base):
    """A disposable per-bot cache of a demo's Telegram file_id.

    The MP4 in our store is canonical; this is a lazily-seeded cache so later
    sends resend by file_id with no upload. file_ids are unique per bot, so the
    bot is part of the key — a token migration simply misses and re-uploads.
    ``gym_id`` NULL caches the Exercise default; set caches a Gym override."""

    __tablename__ = "demo_file_ids"
    __table_args__ = (
        UniqueConstraint(
            "exercise_id", "gym_id", "bot", name="uq_demo_file_id_scope"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id"))
    gym_id: Mapped[int | None] = mapped_column(ForeignKey("gyms.id"), default=None)
    bot: Mapped[str] = mapped_column(String(64))  # cache namespace (per bot)
    file_id: Mapped[str] = mapped_column(String(256))
    file_unique_id: Mapped[str | None] = mapped_column(String(64), default=None)


class DashboardLoginToken(Base):
    """A one-time magic link the bot hands a Coach for `/dashboard`.

    The raw token goes only into the URL; the row stores its SHA-256 hash,
    so a database read never yields a redeemable link (spec-dashboard
    §Access & identity).
    """

    __tablename__ = "dashboard_login_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    gym_id: Mapped[int] = mapped_column(ForeignKey("gyms.id"))
    expires_at: Mapped[datetime] = mapped_column(TZDateTime())
    # NULL until redeemed; single-use is "used_at is NULL" at redeem time.
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime(), default=None)


class Session(Base):
    """One real gym visit — the record of what actually happened."""

    __tablename__ = "sessions"
    # (member_id, started_at) powers gap queries and the check-in sweep.
    __table_args__ = (Index("ix_sessions_member_started", "member_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    gym_id: Mapped[int] = mapped_column(ForeignKey("gyms.id"))
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    started_at: Mapped[datetime] = mapped_column(TZDateTime())
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime(), default=None)


class Set(Base):
    """One performed set within a Session: weight (nullable for bodyweight)
    x reps; RPE and notes only when volunteered."""

    __tablename__ = "sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    gym_id: Mapped[int] = mapped_column(ForeignKey("gyms.id"))
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id"))
    weight: Mapped[float | None] = mapped_column(Float, default=None)
    reps: Mapped[int]
    rpe: Mapped[float | None] = mapped_column(Float, default=None)
    note: Mapped[str | None] = mapped_column(String(400), default=None)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())


class MemberNote(Base):
    """What the Agent learned from a Member: volunteered durable facts.

    Deliberately plain rows (docs/design/memory.md): portable across
    frameworks, inspectable by a Coach, soft-retired via retired_at.
    """

    __tablename__ = "member_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    gym_id: Mapped[int] = mapped_column(ForeignKey("gyms.id"))
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # injury/preference/goal/constraint/other
    text: Mapped[str] = mapped_column(String(400))
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    retired_at: Mapped[datetime | None] = mapped_column(TZDateTime(), default=None)


class Routine(Base):
    """A Member's written training plan: Workouts pinned to weekdays.

    Structure only, never target weights (spec §Data model). One active
    Routine per Member; superseded plans stay, deactivated.
    """

    __tablename__ = "routines"
    __table_args__ = (Index("ix_routines_member_active", "member_id", "is_active"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    gym_id: Mapped[int] = mapped_column(ForeignKey("gyms.id"))
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    is_active: Mapped[bool] = mapped_column(default=True)
    # Set when a Coach hand-writes the plan (ticket #30); the Agent never
    # restructures a coach-authored Routine.
    coach_authored: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())


class Workout(Base):
    """The plan for one training day: a named, ordered list of Exercises
    pinned to a weekday (0=Monday .. 6=Sunday)."""

    __tablename__ = "workouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    gym_id: Mapped[int] = mapped_column(ForeignKey("gyms.id"))
    routine_id: Mapped[int] = mapped_column(ForeignKey("routines.id"), index=True)
    weekday: Mapped[int]  # 0=Monday .. 6=Sunday
    name: Mapped[str] = mapped_column(String(100))


class WorkoutExercise(Base):
    """One Exercise in a Workout: structure (order, optional set/rep scheme)
    drawn from the Exercise catalog. Never a target weight."""

    __tablename__ = "workout_exercises"

    id: Mapped[int] = mapped_column(primary_key=True)
    gym_id: Mapped[int] = mapped_column(ForeignKey("gyms.id"))
    workout_id: Mapped[int] = mapped_column(ForeignKey("workouts.id"), index=True)
    exercise_id: Mapped[int] = mapped_column(ForeignKey("exercises.id"))
    position: Mapped[int]
    sets: Mapped[int | None] = mapped_column(default=None)
    reps: Mapped[str | None] = mapped_column(String(40), default=None)  # e.g. "8-12", "AMRAP"
