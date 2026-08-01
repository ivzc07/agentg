"""Per-turn instrumentation: wall-clock duration, model calls, SQL statements.

Issue #161 — the measurement baseline for latency work. Every Agent turn
emits one structured log line with the turn's total wall-clock duration,
model-call count, and SQL-statement count.

ContextVars carry turn state so counters are isolated across concurrent
turns (different Members are serialised per-identity but can interleave
across identities, all in the same async loop).
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_turn: contextvars.ContextVar[TurnInstrument | None] = contextvars.ContextVar(
    "_instrument_turn", default=None
)


@dataclass
class TurnInstrument:
    """Per-turn counters, populated during ``handle_message``.

    All fields are mutable so SQLAlchemy event listeners and litellm
    callbacks can increment them from outside the turn body.
    """

    start_time: float = field(default_factory=time.monotonic)
    sql_count: int = 0
    model_call_count: int = 0


def _sql_counter(
    conn, cursor, statement, parameters, context, executemany
) -> None:
    instrument = _turn.get()
    if instrument is not None:
        instrument.sql_count += 1


def register_sql_counter(engine: AsyncEngine) -> None:
    """Register a per-statement counter on a SQLAlchemy async engine.

    Attaches to the sync engine inside so every statement — whether issued
    by a domain store or the SDK's SQLAlchemySession — is counted.
    """
    event.listen(engine.sync_engine, "after_cursor_execute", _sql_counter)


async def _count_model_call(
    kwargs, completion_response, start_time, end_time
) -> None:
    instrument = _turn.get()
    if instrument is not None:
        instrument.model_call_count += 1


def register_model_counter() -> None:
    """Register a per-call counter on litellm's async-success-callback list.

    All model calls in the Agent turn flow use ``litellm.acompletion``:
    the Agent's ``Runner.run`` (via ``LitellmModel``) and the linking
    phraser / compaction summarizer (direct ``acompletion``).  Counting
    the async-success callback catches every one.
    """
    try:
        import litellm

        litellm._async_success_callback.append(_count_model_call)
    except ImportError:
        pass


class TurnContext:
    """Start and end a single message turn for instrumentation.

    Usage inside ``handle_message``::

        with TurnContext() as instrument:
            # ... turn body ...
        # instrument is logged automatically on exit
    """

    def __init__(self) -> None:
        self.instrument = TurnInstrument()
        self._token: contextvars.Token[TurnInstrument | None] | None = None

    def __enter__(self) -> TurnInstrument:
        self._token = _turn.set(self.instrument)
        return self.instrument

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        assert self._token is not None
        _turn.reset(self._token)
        duration = time.monotonic() - self.instrument.start_time
        logger.info(
            "turn completed in %.3fs, %d model calls, %d SQL statements",
            duration,
            self.instrument.model_call_count,
            self.instrument.sql_count,
        )
        return False  # don't suppress exceptions
