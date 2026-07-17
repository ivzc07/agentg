"""Domain tables (docs/spec.md §Data model). Every domain row carries gym_id."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, TypeDecorator, UniqueConstraint, func
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
    """A named movement. Product-level catalog; gym-scoped demo overrides
    arrive with the demo-media ticket (#32)."""

    __tablename__ = "exercises"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # Comma-separated normalized alternate names ("bench" -> bench press).
    aliases: Mapped[str] = mapped_column(String(400), default="")


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
