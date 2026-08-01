"""Per-turn instrumentation: wall-clock duration, model calls, SQL statements.

Issue #161 — the measurement baseline for latency work. Every Agent turn
emits one structured log line with the turn's total wall-clock duration,
model-call count, and SQL-statement count.

ContextVars carry turn state so counters are isolated across concurrent
turns (different Members are serialised per-identity but can interleave
across identities, all in the same async loop).

Model calls are counted inline by wrapping ``litellm.acompletion`` inside
the turn context so the count is correct at ``__exit__`` time — litellm's
``_async_success_callback`` fires asynchronously and would lag behind.
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_turn: contextvars.ContextVar[TurnInstrument | None] = contextvars.ContextVar(
    "_instrument_turn", default=None
)


@dataclass
class TurnInstrument:
    """Per-turn counters, populated during ``handle_message``.

    All fields are mutable so SQLAlchemy event listeners and inline
    wrappers can increment them from outside the turn body.
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


class TurnContext:
    """Start and end a single message turn for instrumentation.

    Wraps ``litellm.acompletion`` so model calls are counted inline
    before each call returns, avoiding the race in litellm's async
    success-callback machinery.

    Usage inside ``handle_message``::

        with TurnContext():
            # ... turn body ...
        # instrument is logged automatically on exit
    """

    def __init__(self) -> None:
        self.instrument = TurnInstrument()
        self._token: contextvars.Token[TurnInstrument | None] | None = None
        self._original_acompletion: Any = None

    def __enter__(self) -> TurnInstrument:
        self._token = _turn.set(self.instrument)
        # Wrap litellm.acompletion so every model call made during the turn
        # is counted *before* the call completes.  litellm's async success
        # callback fires after the turn log line, so counting there would
        # systematically undercount (issue #161 review round 1).
        import litellm

        self._original_acompletion = litellm.acompletion

        async def _counting_acompletion(*args: Any, **kwargs: Any) -> Any:
            instrument = _turn.get()
            if instrument is not None:
                instrument.model_call_count += 1
            return await self._original_acompletion(*args, **kwargs)

        litellm.acompletion = _counting_acompletion
        return self.instrument

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        assert self._token is not None
        _turn.reset(self._token)
        # Restore the original acompletion so future turns (and code outside
        # any turn) are unaffected.
        import litellm

        if self._original_acompletion is not None:
            litellm.acompletion = self._original_acompletion
        duration = time.monotonic() - self.instrument.start_time
        logger.info(
            "turn completed in %.3fs, %d model calls, %d SQL statements",
            duration,
            self.instrument.model_call_count,
            self.instrument.sql_count,
        )
        return False  # don't suppress exceptions
