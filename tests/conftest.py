"""Shared test helpers."""

from datetime import UTC, datetime, timedelta


class FakeClock:
    """An injectable clock: starts at a fixed instant, advances on demand."""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 7, 15, 18, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta
