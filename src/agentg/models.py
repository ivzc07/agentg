"""Domain tables (docs/spec.md §Data model). Every domain row carries gym_id."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
