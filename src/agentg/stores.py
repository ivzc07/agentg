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
from agentg.demos import DemoStore
from agentg.forget import ForgetStore
from agentg.notes import NotesStore
from agentg.routines import RoutineStore
from agentg.store import LinkingStore
from agentg.training import TrainingStore


@dataclass(frozen=True)
class Stores:
    linking: LinkingStore
    training: TrainingStore
    notes: NotesStore
    routines: RoutineStore
    checkins: CheckinStore
    demos: DemoStore
    forget: ForgetStore

    @classmethod
    def from_engine(cls, engine: AsyncEngine) -> "Stores":
        return cls(
            linking=LinkingStore(engine),
            training=TrainingStore(engine),
            notes=NotesStore(engine),
            routines=RoutineStore(engine),
            checkins=CheckinStore(engine),
            demos=DemoStore(engine),
            forget=ForgetStore(engine),
        )
