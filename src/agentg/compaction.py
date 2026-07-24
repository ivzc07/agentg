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
# folded away even when tokens are high.
HISTORY_TOKEN_BUDGET = 12_000
COMPACT_AT_TOKENS = (HISTORY_TOKEN_BUDGET * 7) // 10  # 8_400 ≈ 70%
KEEP_RECENT = 20


@dataclass(frozen=True)
class CompactionSummary:
    summary: str
    notes: list[tuple[str, str]]  # (kind, text) durables worth keeping


class SessionLike(Protocol):  # the SDK session surface compaction touches
    async def get_items(self, limit: int | None = None) -> list[Any]: ...
    async def clear_session(self) -> None: ...
    async def add_items(self, items: list[Any]) -> None: ...


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
    existing = [note.text for note in await notes.active(member_id)]
    result = await summarizer(old, existing)
    for kind, text in result.notes:  # durables first — deletion comes after
        await notes.remember(member_id, gym_id, kind, text)
    summary_item = {
        "role": "assistant",
        "content": f"[Summary of earlier conversation]\n{result.summary}",
    }
    await session.clear_session()
    await session.add_items([summary_item] + recent)
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
