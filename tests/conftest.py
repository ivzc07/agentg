"""Shared test helpers."""

from datetime import UTC, datetime, timedelta


async def unused_phraser(instruction: str, member_text: str) -> str:
    """An Onboarding phraser for tests that don't exercise onboarding replies."""
    raise AssertionError("onboarding should not phrase anything in this test")


async def identity_phraser(instruction: str, member_text: str) -> str:
    """An Onboarding phraser for tests that exercise onboarding replies: no
    model, so a reply is exactly its instruction — assertions about facts
    (gym/name) exercise the real instruction text the production phraser
    would receive."""
    return instruction


class FakeClock:
    """An injectable clock: starts at a fixed instant, advances on demand."""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 7, 15, 18, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta
