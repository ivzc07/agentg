"""The per-turn context: who this turn is about, and the stores to act with.

Shared by the Agent's function tools (tools.py) and the domain actions they
delegate to (coaching.py) — neither imports the other's internals through it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agentg.checkin_sweep import Notifier
from agentg.demos import DemoRef
from agentg.stores import Stores


@dataclass
class TurnCache:
    """Mutable per-turn cache — lives exactly one Agent run.

    A MemberContext is built fresh each time the Agent runs, so anything
    cached here is automatically dropped between turns.
    """

    _active_routine: dict[str, Any] | None = None
    _routine_loaded: bool = False

    async def get_or_load_routine(
        self, routines_store: Any, member_id: int
    ) -> dict[str, Any] | None:
        """Return the active Routine, loading it once per turn (#162)."""
        if not self._routine_loaded:
            self._active_routine = await routines_store.active_routine(member_id)
            self._routine_loaded = True
        return self._active_routine

    def set_routine(self, routine: dict[str, Any] | None) -> None:
        """Replace the cached Routine — call after saving a new one."""
        self._active_routine = routine
        self._routine_loaded = True


@dataclass(frozen=True)
class MemberContext:
    """Everything a tool needs to act for the Member this turn is about."""

    stores: Stores
    member_id: int
    gym_id: int
    member_name: str
    gym_name: str
    weight_unit: str
    # The Gym's IANA timezone — day boundaries (today, Gap) honour it (#95).
    timezone: str = "UTC"
    is_coach: bool = False
    # Precomputed before the Agent runs: True when routine-authoring tools
    # should be offered. Always True for Coaches (is_coach dominates); for
    # Members, True when they have no routine or an agent-generated one
    # they can ask to restructure (issue #174).
    can_author_routine: bool = False
    # Channel notifier for pinging a Gym's Coaches on a safety flag.
    notifier: Notifier | None = None
    # Public origin the safety-flag deep links point at (DASHBOARD_BASE_URL);
    # None means no dashboard is wired and pings go out without a link.
    dashboard_base_url: str | None = None
    # Pre-resolved demo references the Agent asked to show this turn; the
    # channel sends them after the reply so the agent loop stays
    # channel-agnostic (ADR 0001) and no second resolution is needed.
    demo_requests: list[DemoRef] = field(default_factory=list)
    # Coach safety-flag pings deferred past the Member's reply (issue #172).
    coach_pings: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    # Per-turn cache so the active Routine is loaded once and reused
    # across the snapshot, session opener, and weight suggestions (#162).
    turn_cache: TurnCache = field(default_factory=TurnCache)
