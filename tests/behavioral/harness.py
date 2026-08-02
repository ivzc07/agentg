"""Scripted-conversation harness: drive the agent loop against a temp DB.

Public seams only:
- ``AgentRuntime.handle_message`` for the conversation
- ``Stores`` for end-state assertions
- injected ``ScriptedModel`` so the deterministic layer needs no network
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from agents import Agent, set_tracing_disabled
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentg.agent import dynamic_instructions
from agentg.dashboard import DashboardDoor
from agentg.dashboard_store import Clock as DashboardClock
from agentg.db import create_engine
from agentg.linking import Linking
from agentg.messages import IncomingMessage
from agentg.models import DashboardLoginToken, Exercise, Set
from agentg.runtime import AgentRuntime
from agentg.stores import Stores
from agentg.tools import build_tools
from agentg.training import Clock
from behavioral.scripted_model import MessageStep, ScriptedModel, Step, message, tool
from conftest import identity_phraser

__all__ = [
    "ConversationHarness",
    "FakeNotifier",
    "MessageStep",
    "Step",
    "message",
    "tool",
]


async def _null_summarizer(old_items: Any, existing_notes: Any) -> Any:
    raise AssertionError("compaction should not trigger in behavioral evals")


class FakeNotifier:
    """Captures coach pings without touching a real channel."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(
        self,
        channel: str,
        channel_user_id: str,
        text: str,
        disable_preview: bool = False,
        protect_content: bool = False,
    ) -> None:
        self.sent.append((channel, channel_user_id, text))


@dataclass
class ConversationHarness:
    """One linked Member chatting with a scripted Agent over a temp SQLite DB."""

    runtime: AgentRuntime
    model: ScriptedModel
    stores: Stores
    gym_id: int
    member_id: int
    notifier: FakeNotifier
    channel_user_id: str = "42"
    channel: str = "telegram"
    display_name: str = "Dani"
    _engine: Any = None
    _ok: bool = field(default=True, repr=False)

    @classmethod
    @asynccontextmanager
    async def create(
        cls,
        tmp_path: Path,
        clock: Clock | None = None,
        *,
        dashboard_base_url: str | None = None,
        dashboard_clock: DashboardClock | None = None,
    ) -> AsyncIterator["ConversationHarness"]:
        """Build the harness; with ``dashboard_base_url`` the runtime also
        gets the dashboard door (``/dashboard`` -> magic link). The injected
        clock is ``clock``, or ``dashboard_clock`` as a shorthand for tests
        whose only time-aware surface is the dashboard door."""
        set_tracing_disabled(True)
        engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'behavioral.db'}")
        # Stores.from_engine wires the clock into every time-aware store —
        # the DashboardStore included (flag expiry, token TTL).
        stores = Stores.from_engine(
            engine, clock=clock if clock is not None else dashboard_clock
        )
        dashboard = None
        if dashboard_base_url is not None:
            dashboard = DashboardDoor(stores.dashboard, dashboard_base_url)
        model = ScriptedModel()
        notifier = FakeNotifier()
        agent = Agent(
            name="Agent",
            instructions=dynamic_instructions,
            model=model,
            tools=build_tools(),
        )
        runtime = AgentRuntime(
            agent=agent,
            engine=engine,
            stores=stores,
            linking=Linking(stores.linking, identity_phraser),
            summarizer=_null_summarizer,
            notifier=notifier,
            dashboard=dashboard,
        )
        await runtime.ensure_schema()
        harness = cls(
            runtime=runtime,
            model=model,
            stores=stores,
            gym_id=0,
            member_id=0,
            notifier=notifier,
            _engine=engine,
        )
        try:
            yield harness
            harness._ok = True
        except BaseException:
            harness._ok = False
            raise
        finally:
            leftover = model.remaining
            await engine.dispose()
            if harness._ok and leftover:
                raise AssertionError(
                    f"conversation ended with {leftover} unused model steps"
                )

    async def linked_member(
        self,
        *,
        name: str = "Dani",
        gym_name: str = "Iron Temple",
        channel_user_id: str = "42",
        is_coach: bool = False,
        timezone: str = "UTC",
    ) -> None:
        gym = await self.stores.linking.create_gym(gym_name, timezone=timezone)
        member = await self.stores.linking.link_member(
            gym.id, name, self.channel, channel_user_id
        )
        if is_coach:
            await self.stores.linking.set_coach(member.id)
        self.gym_id = gym.id
        self.member_id = member.id
        self.channel_user_id = channel_user_id
        self.display_name = name

    async def add_coach(self, *, name: str = "Coach Sam", channel_user_id: str = "7") -> Any:
        coach = await self.stores.linking.link_member(
            self.gym_id, name, self.channel, channel_user_id
        )
        await self.stores.linking.set_coach(coach.id)
        return coach

    async def create_gym(self, name: str = "Other Gym") -> Any:
        return await self.stores.linking.create_gym(name)

    async def seed_closed_session(self, *lines: str) -> None:
        """Prior history the Member can refer back to (copy/edit baselines)."""
        await self.stores.training.open_session(self.member_id, self.gym_id)
        for line in lines:
            await self.stores.training.log_sets(self.member_id, self.gym_id, line)
        await self.stores.training.close_session(self.member_id)

    async def current_sets_by_exercise(self) -> dict[str, list[dict[str, Any]]]:
        """End-state helper: open-session sets grouped by catalog exercise name."""
        sets = await self.stores.training.current_session_sets(self.member_id)
        if not sets:
            return {}
        async with async_sessionmaker(self._engine)() as db:
            ids = {s.exercise_id for s in sets}
            rows = (
                await db.execute(select(Exercise).where(Exercise.id.in_(ids)))
            ).scalars().all()
            names = {row.id: row.name for row in rows}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for s in sets:
            grouped.setdefault(names[s.exercise_id], []).append(
                {"weight": s.weight, "reps": s.reps, "rpe": s.rpe, "note": s.note}
            )
        return grouped

    async def login_tokens(self) -> list[DashboardLoginToken]:
        """End-state helper: every dashboard login token row."""
        async with async_sessionmaker(self._engine)() as db:
            return list(await db.scalars(select(DashboardLoginToken)))

    async def say(
        self,
        text: str,
        *,
        steps: Sequence[Step] | None = None,
        link_code: str | None = None,
        channel_user_id: str | None = None,
        display_name: str | None = None,
        is_group: bool = False,
    ) -> str:
        """Send one member message. ``steps`` scripts the model for this turn.

        Linking turns (invite codes, name confirms) need no steps — the Agent
        does not run. Agent turns must supply steps ending in a ``message``.
        ``is_group`` reports the message as arriving in a shared chat.
        """
        if steps:
            self.model.enqueue(steps)
        reply = await self.runtime.handle_message(
            IncomingMessage(
                channel=self.channel,
                channel_user_id=channel_user_id or self.channel_user_id,
                text=text,
                display_name=display_name if display_name is not None else self.display_name,
                link_code=link_code,
                is_group=is_group,
            )
        )
        if reply.after_send is not None:
            await reply.after_send()
        return str(reply)
