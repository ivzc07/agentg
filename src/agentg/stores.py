"""Every domain store, built together over one engine.

The single wiring point for persistence: a new store is added here once and
arrives everywhere a ``Stores`` travels (the runtime, the tool context),
instead of being threaded through main.py, AgentRuntime, and MemberContext
by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from agentg.checkin_store import CheckinStore
from agentg.dashboard_store import DashboardStore
from agentg.demos import DemoStore
from agentg.forget import ForgetStore
from agentg.notes import NotesStore
from agentg.routines import RoutineStore
from agentg.linking_store import LinkingStore
from agentg.safety_outbox import SafetyOutbox
from agentg.training import Clock, TrainingStore


@dataclass(frozen=True)
class Stores:
    linking: LinkingStore
    training: TrainingStore
    notes: NotesStore
    routines: RoutineStore
    checkins: CheckinStore
    demos: DemoStore
    forget: ForgetStore
    dashboard: DashboardStore
    safety_outbox: SafetyOutbox

    @classmethod
    def from_engine(cls, engine: AsyncEngine, clock: Clock | None = None) -> "Stores":
        """Build every store over one engine. ``clock`` overrides the wall
        clock the time-aware stores use (tests inject it; prod leaves it)."""
        if clock is not None:
            return cls(
                linking=LinkingStore(engine),
                training=TrainingStore(engine, clock=clock),
                notes=NotesStore(engine, clock=clock),
                routines=RoutineStore(engine, clock=clock),
                checkins=CheckinStore(engine),
                demos=DemoStore(engine),
                forget=ForgetStore(engine),
                dashboard=DashboardStore(engine, clock=clock),
                safety_outbox=SafetyOutbox(engine, clock=clock),
            )
        return cls(
            linking=LinkingStore(engine),
            training=TrainingStore(engine),
            notes=NotesStore(engine),
            routines=RoutineStore(engine),
            checkins=CheckinStore(engine),
            demos=DemoStore(engine),
            forget=ForgetStore(engine),
            dashboard=DashboardStore(engine),
            safety_outbox=SafetyOutbox(engine),
        )
