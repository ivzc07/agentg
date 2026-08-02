"""Per-turn instrumentation: wall-clock duration, model calls, SQL statements.

Issue #161 — the measurement baseline for latency work. Every Agent turn
emits one structured log line with the turn's total wall-clock duration,
model-call count, and SQL-statement count.

ContextVars carry turn state so counters are isolated across concurrent
turns (different Members are serialised per-identity but can interleave
across identities, all in the same async loop).

Model calls are counted inline by a single global wrapper installed once
at import time; the wrapper reads ``_turn.get()`` to attribute each call
to the active turn.  This avoids the nesting bugs that would come from
save/restore of a global per-turn (double-counting, permanent corruption
under concurrency).

The instrumentation itself adds no measurable latency (acceptance
criterion 5, issue #161): the per-call overhead is a single contextvar
read and an integer increment — no allocation, no I/O.
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import litellm
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

_turn: contextvars.ContextVar[TurnInstrument | None] = contextvars.ContextVar(
    "_instrument_turn", default=None
)

# ---------------------------------------------------------------------------
# Global model-call counter — installed once, contextvar-attributed per turn.
# Mirrors the register_sql_counter pattern: one global listener / wrapper,
# per-turn attribution via the _turn contextvar.
# ---------------------------------------------------------------------------

_original_acompletion = litellm.acompletion


async def _counting_acompletion(*args: Any, **kwargs: Any) -> Any:
    instrument = _turn.get()
    if instrument is not None:
        instrument.model_call_count += 1
    return await _original_acompletion(*args, **kwargs)


# Idempotence: if this module is ever reloaded, skip patching rather than
# chaining wrappers and double-counting every call (issue #161 PR review).
if litellm.acompletion is not _counting_acompletion:
    litellm.acompletion = _counting_acompletion


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

    Model-call counting is handled by the global ``_counting_acompletion``
    wrapper installed at import time — it reads ``_turn.get()`` to
    attribute each call to the active turn.  This avoids nesting bugs
    that save/restore of a global per-turn would cause under concurrency.

    Usage inside ``handle_message``::

        with TurnContext():
            # ... turn body ...
        # instrument is logged automatically on exit
    """

    def __init__(self) -> None:
        self.instrument = TurnInstrument()
        self._token: contextvars.Token[TurnInstrument | None] | None = None
        # Streaming turns are not over when the ``with`` block exits: the
        # model generation continues until the channel consumes the stream.
        # Set this and the exit resets the contextvar but defers the log line
        # to ``finish()`` (issue #161 + #176).
        self.defer_logging = False
        self._logged = False

    def __enter__(self) -> TurnInstrument:
        self._token = _turn.set(self.instrument)
        return self.instrument

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        assert self._token is not None
        _turn.reset(self._token)
        if exc_type is not None:
            # Exception-aborted turn — don't log "completed" so the
            # latency baseline isn't polluted (issue #161: only completed
            # turns are measured).
            return False  # don't suppress the exception
        if self.defer_logging:
            # The turn is still running (streaming): finish() logs it once the
            # stream is consumed, so the counts include the generation.
            return False
        self.finish()
        return False

    def finish(self) -> None:
        """Log the completed turn.  Safe to call once; later calls are no-ops.

        Separate from ``__exit__`` because the contextvar token can only be
        reset in the context that set it, while a streaming turn only truly
        ends later, when the channel finishes consuming the stream.  The
        model-call and SQL counters keep working across that gap: the task
        ``Runner.run_streamed`` starts inherits a copy of the context that
        still points at this instrument.
        """
        if self._logged:
            return
        self._logged = True
        duration = time.monotonic() - self.instrument.start_time
        logger.info(
            "turn completed in %.3fs, %d model calls, %d SQL statements",
            duration,
            self.instrument.model_call_count,
            self.instrument.sql_count,
        )
