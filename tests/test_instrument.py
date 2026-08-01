"""Per-turn instrumentation: wall-time, model calls, SQL statements (#161)."""

import asyncio
import logging

import litellm
import pytest

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

    def test_does_not_log_on_exception(self, caplog: pytest.LogCaptureFixture):
        """Exception-aborted turns must not pollute the latency baseline."""
        caplog.set_level(logging.INFO, logger="agentg.instrument")
        with pytest.raises(ValueError):
            with TurnContext() as instrument:
                instrument.sql_count = 5
                raise ValueError("boom")
        # No "turn completed" line should be emitted.
        log_lines = [
            r.message
            for r in caplog.records
            if "turn completed" in r.message
        ]
        assert len(log_lines) == 0, (
            f"exception path should not log, got {log_lines}"
        )

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
    async def test_increments_model_call_count_during_turn(self):
        """litellm.acompletion inside a TurnContext is counted inline.

        The count is available before __exit__ because the TurnContext wraps
        acompletion — no race with litellm's async success-callback machinery.
        To verify this test is not tautological: revert the __enter__ wrapper
        and model_call_count stays 0, failing the asserts.
        """
        with TurnContext() as instrument:
            assert instrument.model_call_count == 0
            await litellm.acompletion(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "hi"}],
                mock_response="hello",
            )
            assert instrument.model_call_count == 1
            await litellm.acompletion(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "hi"}],
                mock_response="hello",
            )
            assert instrument.model_call_count == 2
            await litellm.acompletion(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "hi"}],
                mock_response="hello",
            )
            assert instrument.model_call_count == 3

    async def test_logs_correct_count_on_exit(self, caplog: pytest.LogCaptureFixture):
        """The log line emitted at __exit__ reports the correct model-call count."""
        caplog.set_level(logging.INFO, logger="agentg.instrument")
        with TurnContext():
            await litellm.acompletion(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "hi"}],
                mock_response="hello",
            )
            await litellm.acompletion(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "hi"}],
                mock_response="hello",
            )
        record = caplog.records[0]
        assert record.levelname == "INFO"
        assert "2 model calls" in record.message

    async def test_does_not_count_outside_turn(self):
        """acompletion outside any TurnContext leaves the counter alone."""
        await litellm.acompletion(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "hi"}],
            mock_response="hello",
        )
        # No active instrument — nothing to assert beyond no crash.

    async def test_production_path_guard(self):
        """Guard: the SDK's LitellmModel must reach our wrapper at call time.

        If an SDK upgrade switches from ``litellm.acompletion(...)``
        (attribute access) to ``from litellm import acompletion`` (local
        reference), the production count silently becomes 0 with the
        suite still green.  This assertion fails on that change.
        """
        import agents.extensions.models.litellm_model  # noqa: F401

        import agentg.instrument

        assert litellm.acompletion is agentg.instrument._counting_acompletion, (
            "litellm.acompletion is not our counting wrapper — "
            "the SDK may have switched to a local reference import"
        )


class TestConcurrency:
    async def test_concurrent_turns_have_independent_model_counts(
        self, caplog: pytest.LogCaptureFixture
    ):
        """Two interleaved TurnContexts each count their own model calls.

        Revert test for the P1 finding: the old save/restore approach
        chained global wrappers under concurrency, so a single acompletion
        call fired both wrappers and double-counted (or leaked the wrapper
        permanently).  With the fix — a single persistent wrapper +
        contextvar — each turn sees exactly its own calls.
        """
        caplog.set_level(logging.INFO, logger="agentg.instrument")

        async def turn(name: str) -> int:
            with TurnContext() as inst:
                await litellm.acompletion(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": name}],
                    mock_response="hello",
                )
                return inst.model_call_count

        counts = await asyncio.gather(turn("A"), turn("B"))
        assert counts == [1, 1], (
            f"expected [1, 1] under concurrency, got {counts}"
        )

        # Each turn's log line should report exactly 1 model call.
        log_lines = [
            r.message
            for r in caplog.records
            if r.name == "agentg.instrument"
            and "turn completed" in r.message
        ]
        assert len(log_lines) == 2
        for line in log_lines:
            assert "1 model calls" in line, (
                f"expected '1 model calls', got: {line}"
            )


class TestIntegration:
    async def test_turn_context_wraps_handle_message(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A full turn through handle_message logs its instrument line.

        Uses the runtime fixture pattern from test_runtime.py — a linked
        Member with a mocked Runner so no real model call is needed.
        """
        from types import SimpleNamespace

        from agentg.db import create_engine
        from agentg.linking import Linking
        from agentg.messages import IncomingMessage
        from agentg.runtime import AgentRuntime
        from agentg.stores import Stores
        from conftest import unused_phraser

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

            with pytest.MonkeyPatch.context() as monkeypatch:
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
                # At least one SQL statement must have been counted — the
                # handle_message body issues several queries.  A regression
                # that zeros the SQL counter must fail this assertion.
                assert "0 SQL statements" not in line, (
                    f"SQL counter appears zero in log line: {line}"
                )
        finally:
            await engine.dispose()

    async def test_linking_only_turn_emits_instrument_log(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A turn that short-circuits at linking still logs instrumentation.

        Acceptance criterion 4 (issue #161): the linking path is covered,
        not silently missing.  The runtime wraps the *entire* body in
        TurnContext, so a linking-only reply emits a "turn completed" line.

        An *unlinked* identity sending any non-code text triggers
        ``Linking._reply_unlinked_unknown``, which calls the phraser and
        returns a reply — ``handle_message`` returns before ``Runner.run``.
        """
        from agentg.db import create_engine
        from agentg.linking import Linking
        from agentg.messages import IncomingMessage
        from agentg.runtime import AgentRuntime
        from agentg.stores import Stores
        from conftest import identity_phraser

        async def null_summarizer(old_items, existing_notes):
            raise AssertionError("compaction should not trigger in this test")

        engine = create_engine("sqlite+aiosqlite://")
        stores = Stores.from_engine(engine)
        # identity_phraser returns the instruction text it receives, so
        # every unlinked message receives a linking reply — the Agent is
        # never reached.
        runtime = AgentRuntime(
            agent=object(),
            engine=engine,
            stores=stores,
            linking=Linking(stores.linking, identity_phraser),
            summarizer=null_summarizer,
        )
        try:
            await runtime.ensure_schema()
            # No link_member call — identity stays unlinked so every
            # message short-circuits at linking.

            caplog.set_level(logging.INFO, logger="agentg.instrument")

            reply = await runtime.handle_message(
                IncomingMessage(
                    channel="telegram",
                    channel_user_id="99",
                    text="hola",
                )
            )

            assert isinstance(reply, str)
            log_lines = [
                r.message
                for r in caplog.records
                if r.name == "agentg.instrument"
                and "turn completed" in r.message
            ]
            assert len(log_lines) == 1, (
                f"linking-only turn should log one instrument line, got {log_lines}"
            )
        finally:
            await engine.dispose()

    async def test_dashboard_turn_emits_instrument_log(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A /dashboard command turn logs instrumentation.

        Acceptance criterion 4 (issue #161): the dashboard door is covered,
        not silently missing.  The runtime wraps the *entire* body in
        TurnContext, so the dashboard path emits a "turn completed" line.
        """
        from agentg.dashboard import DashboardDoor
        from agentg.dashboard_store import DashboardStore
        from agentg.db import create_engine
        from agentg.linking import Linking
        from agentg.messages import IncomingMessage
        from agentg.runtime import AgentRuntime
        from agentg.stores import Stores
        from conftest import unused_phraser

        async def null_summarizer(old_items, existing_notes):
            raise AssertionError("compaction should not trigger in this test")

        engine = create_engine("sqlite+aiosqlite://")
        stores = Stores.from_engine(engine)
        dashboard_store = DashboardStore(engine)
        runtime = AgentRuntime(
            agent=object(),
            engine=engine,
            stores=stores,
            linking=Linking(stores.linking, unused_phraser),
            summarizer=null_summarizer,
            dashboard=DashboardDoor(
                store=dashboard_store,
                base_url="https://example.com",
            ),
        )
        try:
            await runtime.ensure_schema()
            gym = await stores.linking.create_gym("Iron Temple")
            coach = await stores.linking.link_member(
                gym.id, "Coach Ana", "telegram", "42"
            )
            await stores.linking.set_coach(coach.id, True)

            caplog.set_level(logging.INFO, logger="agentg.instrument")

            reply = await runtime.handle_message(
                IncomingMessage(
                    channel="telegram",
                    channel_user_id="42",
                    text="/dashboard",
                )
            )

            # /dashboard returns a Reply, which handle_message unwraps to str.
            assert isinstance(reply, str)
            assert "dashboard" in reply.lower()
            log_lines = [
                r.message
                for r in caplog.records
                if r.name == "agentg.instrument"
                and "turn completed" in r.message
            ]
            assert len(log_lines) == 1, (
                f"dashboard turn should log one instrument line, got {log_lines}"
            )
        finally:
            await engine.dispose()
