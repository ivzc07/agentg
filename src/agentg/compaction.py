"""Conversation compaction (docs/design/memory.md §Growth policy).

History is a working buffer, not an archive: past the token-estimate
threshold, the oldest turns are summarized into one item and deleted —
after anything durable is pushed into member notes. Retention = compaction
(spec §Privacy & data retention). The summarize step is an injected
callable so the mechanics stay deterministic under test; production uses
the configured model.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from agentg.config import Settings
from agentg.notes import NotesStore

logger = logging.getLogger(__name__)

# History is a working buffer. Estimated tokens = total chars // 4 (no
# tokenizer dependency). Compact when the estimate exceeds ~70% of the
# budget history may occupy, so we fire well before the attention cliff
# (issue #54). KEEP_RECENT is an item floor — the live exchange is never
# folded away even when tokens are high. When the only items outside the
# recent window are previous summaries, compaction skips — it has nothing
# new to fold and would only re-summarize its own output (issue #165).
HISTORY_TOKEN_BUDGET = 12_000
COMPACT_AT_TOKENS = (HISTORY_TOKEN_BUDGET * 7) // 10  # 8_400 ≈ 70%
KEEP_RECENT = 20
# When summaries already exist, skip compaction unless a meaningful number of
# fresh items have aged out of the recent window.  This prevents re-compacting
# on every message once the recent-item floor alone exceeds the budget (issue #165).
MIN_FRESH_TO_SUMMARIZE = 2


@dataclass(frozen=True)
class CompactionSummary:
    summary: str
    notes: list[tuple[str, str]]  # (kind, text) durables worth keeping


class SessionLike(Protocol):  # the SDK session surface compaction touches
    async def get_items(self, limit: int | None = None) -> list[Any]: ...
    async def clear_session(self) -> None: ...
    async def add_items(self, items: list[Any]) -> None: ...


async def _replace_items_atomically(session: Any, new_items: list[Any]) -> None:
    """Replace all session items with *new_items* in a single transaction.

    The SDK's ``clear_session`` + ``add_items`` pattern runs in two separate
    transactions, so a crash or failure between them destroys the entire
    conversation.  This helper uses the engine directly to delete old rows
    and insert new ones atomically — either both happen or neither does
    (issue #165).
    """
    from sqlalchemy import delete, insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    engine = session.engine
    messages = session._messages
    sessions = session._sessions

    serialized = [
        json.dumps(item, ensure_ascii=True, separators=(",", ":"))
        for item in new_items
    ]

    async with engine.begin() as conn:
        # Idempotent session-row upsert (matches add_items behaviour).
        await conn.execute(
            sqlite_insert(sessions)
            .values(session_id=session.session_id)
            .on_conflict_do_nothing()
        )
        # Delete every existing message for this session …
        await conn.execute(
            delete(messages).where(messages.c.session_id == session.session_id)
        )
        # … and write the replacement set in the same transaction.
        if serialized:
            payload = [
                {"session_id": session.session_id, "message_data": item}
                for item in serialized
            ]
            await conn.execute(insert(messages), payload)


Summarizer = Callable[[list[Any], list[str]], Awaitable[CompactionSummary]]


def estimate_tokens(items: list[Any]) -> int:
    """Rough token count via chars/4. Good enough to fire before the cliff."""
    total_chars = 0
    for item in items:
        if isinstance(item, str):
            total_chars += len(item)
        else:
            total_chars += len(json.dumps(item, default=str))
    return total_chars // 4


async def maybe_compact(
    session: SessionLike,
    summarizer: Summarizer,
    notes: NotesStore,
    member_id: int,
    gym_id: int,
) -> bool:
    items = await session.get_items()
    if estimate_tokens(items) <= COMPACT_AT_TOKENS:
        return False
    if len(items) <= KEEP_RECENT:
        return False  # nothing outside the live-exchange floor to fold
    old, recent = items[:-KEEP_RECENT], items[-KEEP_RECENT:]
    # Separate previous summaries from fresh content.  Summaries are never
    # fed back into the summarizer — re-summarizing a summary loses fidelity.
    # When there is no fresh content to fold we skip the summarizer call
    # entirely (convergence guard, issue #165).
    old_summaries = [it for it in old if "Summary of earlier conversation" in str(it)]
    to_summarize = [it for it in old if "Summary of earlier conversation" not in str(it)]
    if not to_summarize:
        return False
    # When summaries already exist and only a trivial number of fresh items
    # have aged out of the recent window, skip compaction.  Without this
    # guard a long-running member whose recent-item floor alone exceeds the
    # budget re-triggers compaction on every single message (issue #165).
    if old_summaries and len(to_summarize) < MIN_FRESH_TO_SUMMARIZE:
        return False
    existing = [note.text for note in await notes.active(member_id)]
    result = await summarizer(to_summarize, existing)
    for kind, text in result.notes:  # durables first — deletion comes after
        await notes.remember(member_id, gym_id, kind, text)
    summary_item = {
        "role": "assistant",
        "content": f"[Summary of earlier conversation]\n{result.summary}",
    }
    # Prevent unbounded summary accumulation: when prior compactions have
    # already left multiple summary items, merge them into one so the
    # history floor does not grow without bound (issue #165).
    if len(old_summaries) > 1:
        merged = "\n\n".join(
            s.get("content", str(s)) if isinstance(s, dict) else str(s)
            for s in old_summaries
        )
        old_summaries = [{"role": "assistant", "content": merged}]
    new_items = old_summaries + [summary_item] + recent
    # Single-transaction replacement: the old items are only deleted after
    # the new items are committed — a crash cannot leave an empty session
    # (issue #165).
    await _replace_items_atomically(session, new_items)
    return True


_SUMMARIZER_PROMPT = """\
You compact a gym-coaching chat history. Reply with JSON only:
{"summary": "<a short paragraph a coach could read to catch up: training \
done, weights and reps mentioned, injuries, moods, plans>",
"notes": [["injury|preference|goal|constraint|other", "<durable fact>"], ...]}
Only include notes for durable facts the member volunteered that are NOT \
already in this list of existing notes: %s
"""


def build_summarizer(settings: Settings) -> Summarizer:
    """The production summarizer: one plain model call over the old turns."""

    async def summarize(old_items: list[Any], existing_notes: list[str]) -> CompactionSummary:
        from litellm import acompletion  # deferred: import cost and test isolation

        transcript = "\n".join(str(item) for item in old_items)
        response = await acompletion(
            model=settings.model,
            api_key=settings.model_api_key,
            messages=[
                {"role": "system", "content": _SUMMARIZER_PROMPT % json.dumps(existing_notes)},
                {"role": "user", "content": transcript},
            ],
            timeout=60,  # background — longer window, not on the critical path
            num_retries=1,
            max_tokens=500,  # summaries are compact by design
            temperature=0.3,  # factual, consistent
        )
        text = response.choices[0].message.content or ""
        try:
            data = json.loads(text)
            notes = [(str(k), str(t)) for k, t in data.get("notes", [])]
            return CompactionSummary(summary=str(data["summary"]), notes=notes)
        except (ValueError, KeyError, TypeError):
            logger.warning("summarizer returned non-JSON; keeping raw text as the summary")
            return CompactionSummary(summary=text, notes=[])

    return summarize
