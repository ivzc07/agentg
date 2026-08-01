"""Per-turn instrumentation: wall-time, model calls, SQL statements (#161)."""

import logging

import pytest

import agentg.instrument as mod
from agentg.db import create_engine
from agentg.instrument import TurnContext, TurnInstrument, register_sql_counter, _turn


class TestTurnContext:
    async def test_logs_duration_model_count_and_sql_count_on_exit(
        self, caplog: pytest.LogCaptureFixture
    ):
        caplog.set_level(logging.INFO)
        with TurnContext() as instrument:
            instrument.sql_count = 3
            instrument.model_call_count = 2
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == "INFO"
        assert "turn completed in" in record.message
        assert "2 model calls" in record.message
        assert "3 SQL statements" in record.message

    async def test_clears_context_on_exit(self):
        assert _turn.get() is None
        with TurnContext():
            assert _turn.get() is not None
        assert _turn.get() is None


class TestSqlCounter:
    async def test_increments_sql_count_when_instrument_is_active(self):
        engine = create_engine("sqlite+aiosqlite://")
        try:
            # Create tables so the SELECT has somewhere to run.
            from agentg.models import Base

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            from agentg.models import Gym

            _turn.set(TurnInstrument())
            async with engine.connect() as conn:
                await conn.execute(
                    Gym.__table__.select().where(Gym.__table__.c.id == 1)
                )
            instrument = _turn.get()
            assert instrument is not None
            assert instrument.sql_count >= 1  # at least our SELECT
        finally:
            _turn.set(None)
            await engine.dispose()

    async def test_does_not_increment_when_no_turn_is_active(self):
        engine = create_engine("sqlite+aiosqlite://")
        try:
            from agentg.models import Base

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            from agentg.models import Gym

            _turn.set(None)
            async with engine.connect() as conn:
                await conn.execute(
                    Gym.__table__.select().where(Gym.__table__.c.id == 1)
                )
            assert _turn.get() is None
        finally:
            await engine.dispose()


class TestModelCounter:
    async def test_register_does_not_raise(self):
        # The litellm callback list must accept our counter.
        mod.register_model_counter()
        # Registering twice is harmless (idempotent append, but idempotency
        # is hard to test — just verify the second call doesn't crash).
        mod.register_model_counter()


class TestIntegration:
    async def test_turn_context_wraps_handle_message(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A full turn through handle_message logs its instrument line.

        Uses the runtime fixture pattern from test_runtime.py — a linked
        Member with a mocked Runner so no real model call is needed.
        """
        import asyncio
        from types import SimpleNamespace

        from agentg.db import create_engine
        from agentg.linking import Linking
        from agentg.messages import IncomingMessage
        from agentg.runtime import AgentRuntime
        from agentg.stores import Stores
        from conftest import unused_phraser

        from agentg import runtime as runtime_module

        async def null_summarizer(old_items, existing_notes):
            raise AssertionError("compaction should not trigger in this test")

        engine = create_engine("sqlite+aiosqlite://")
        stores = Stores.from_engine(engine)
        runtime = AgentRuntime(
            agent=object(),
            engine=engine,
            stores=stores,
            linking=Linking(stores.linking, unused_phraser),
            summarizer=null_summarizer,
        )
        try:
            await runtime.ensure_schema()
            gym = await stores.linking.create_gym("Iron Temple")
            await stores.linking.link_member(gym.id, "Ana", "telegram", "42")

            # A fake Runner.run that returns a simple reply.
            async def fake_run(agent, text, *, session, context=None):
                return SimpleNamespace(final_output="hey Ana, logged that")

            import agentg.runtime as rt_mod

            monkeypatch = pytest.MonkeyPatch()
            monkeypatch.setattr(rt_mod.Runner, "run", fake_run)

            caplog.set_level(logging.INFO, logger="agentg.instrument")

            reply = await runtime.handle_message(
                IncomingMessage(
                    channel="telegram",
                    channel_user_id="42",
                    text="bench 60 8,8,8",
                )
            )

            assert isinstance(reply, str)
            # The instrument log line should have fired.
            log_lines = [
                r.message
                for r in caplog.records
                if r.name == "agentg.instrument"
                and "turn completed" in r.message
            ]
            assert len(log_lines) == 1, (
                f"expected one instrument log line, got {log_lines}"
            )
            line = log_lines[0]
            assert "model calls" in line
            assert "SQL statements" in line
        finally:
            await engine.dispose()
