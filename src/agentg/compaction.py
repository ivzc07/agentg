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


_MAX_MERGED_SUMMARY_CHARS = 2_000  # ~500 tokens, matching the summarizer max_tokens


class SessionLike(Protocol):  # the SDK session surface compaction touches
    async def get_items(self, limit: int | None = None) -> list[Any]: ...
    async def add_items(self, items: list[Any]) -> None: ...


_SQLITE_LOCK_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


async def _replace_items_atomically(session: Any, new_items: list[Any]) -> None:
    """Replace all session items with *new_items* in a single transaction.

    The SDK's ``clear_session`` + ``add_items`` pattern runs in two separate
    transactions, so a crash or failure between them destroys the entire
    conversation.  This helper uses the engine directly to delete old rows
    and insert new ones atomically — either both happen or neither does
    (issue #165).
    """
    import asyncio

    from sqlalchemy import delete, insert, select, update
    from sqlalchemy import text as sql_text
    from sqlalchemy.exc import IntegrityError, OperationalError

    engine = session.engine
    messages = session._messages
    sessions = session._sessions
    ensure_ascii = getattr(session, "_ensure_ascii", True)

    serialized = [
        json.dumps(item, ensure_ascii=ensure_ascii, separators=(",", ":"))
        for item in new_items
    ]

    async def _replace() -> None:
        async with engine.begin() as conn:
            # Idempotent session-row upsert adapted from the SDK's add_items
            # (check-then-insert with IntegrityError catch — portable across
            # SQLite and Postgres without dialect-specific on_conflict syntax).
            existing = await conn.execute(
                select(sessions.c.session_id).where(
                    sessions.c.session_id == session.session_id
                )
            )
            if not existing.scalar_one_or_none():
                try:
                    async with conn.begin_nested():
                        await conn.execute(
                            insert(sessions).values({"session_id": session.session_id})
                        )
                except IntegrityError:
                    pass  # concurrent writer created the row first
            # Bump updated_at so readers see the change (matches SDK add_items).
            await conn.execute(
                update(sessions)
                .where(sessions.c.session_id == session.session_id)
                .values(updated_at=sql_text("CURRENT_TIMESTAMP"))
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

    # Retry transient SQLite lock errors with bounded backoff (the SDK's
    # add_items uses its own retry helper; we replicate the pattern here
    # because we go directly to the engine for atomicity).
    if engine.dialect.name == "sqlite":
        for attempt, delay in enumerate((0.0,) + _SQLITE_LOCK_RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                await _replace()
                return
            except OperationalError as exc:
                if "database is locked" not in str(exc).lower():
                    raise
                if attempt == len(_SQLITE_LOCK_RETRY_DELAYS):
                    raise
    else:
        await _replace()


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


def _is_summary_item(item: Any) -> bool:
    """True when *item* is a compaction summary, anchored on role + prefix.

    Substring matching (``"Summary of earlier conversation" in str(it)``)
    can false-positive on a genuine message that quotes the phrase; we
    require the assistant role and the exact bracketed prefix on dict items.
    """
    if isinstance(item, dict) and item.get("role") == "assistant":
        content = item.get("content", "")
        if isinstance(content, str) and content.startswith(
            "[Summary of earlier conversation]"
        ):
            return True
    return False


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
    old_summaries = [it for it in old if _is_summary_item(it)]
    to_summarize = [it for it in old if not _is_summary_item(it)]
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
        if len(merged) > _MAX_MERGED_SUMMARY_CHARS:
            merged = (
                merged[:_MAX_MERGED_SUMMARY_CHARS]
                + "\n[... summary truncated to stay within budget]"
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
        import litellm  # deferred: import cost and test isolation

        transcript = "\n".join(str(item) for item in old_items)
        response = await litellm.acompletion(
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
