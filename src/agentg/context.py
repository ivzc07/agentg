"""The per-turn context: who this turn is about, and the stores to act with.

Shared by the Agent's function tools (tools.py) and the domain actions they
delegate to (coaching.py) — neither imports the other's internals through it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentg.checkin_sweep import Notifier
from agentg.demos import DemoRef
from agentg.stores import Stores


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
    # Channel notifier for pinging a Gym's Coaches on a safety flag.
    notifier: Notifier | None = None
    # Public origin the safety-flag deep links point at (DASHBOARD_BASE_URL);
    # None means no dashboard is wired and pings go out without a link.
    dashboard_base_url: str | None = None
    # Pre-resolved demo references the Agent asked to show this turn; the
    # channel sends them after the reply so the agent loop stays
    # channel-agnostic (ADR 0001) and no second resolution is needed.
    demo_requests: list[DemoRef] = field(default_factory=list)
