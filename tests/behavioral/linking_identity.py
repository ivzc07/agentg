"""Deterministic identity guard for the linking phraser's replies (issue #66).

The linking voice has exactly one identity: a coach who works through
partner gyms. The phraser is an LLM, so the prompt alone leaves no evidence
the identity holds — this checker scans a reply for first-person
self-descriptions ("soy …", "I am …", "I'm …") and flags any that name
anything other than that coach. The offline suite exercises it on canned
replies; the live sweep behind the ``live`` marker runs the same check
against real phraser output.
"""

from __future__ import annotations

import os
import re

# The only role a self-description may name (Spanish default + English mirror).
COACH_ANCHORS = ("coach", "entrenador", "entrenadora", "trainer")

# First-person identity claims in the two languages the phraser speaks; a
# claim runs to the end of its sentence.
_CLAIM = re.compile(r"\b(soy|i am|i'm)\b", re.IGNORECASE)
_SEGMENT_END = re.compile(r"[.!?\n;]")


def identity_drift(reply: str) -> str | None:
    """The drifting self-description in ``reply``, or ``None`` when the
    identity holds (including replies with no self-description at all)."""
    for match in _CLAIM.finditer(reply):
        end = _SEGMENT_END.search(reply, match.end())
        segment = reply[match.start() : end.start() if end else len(reply)]
        if not any(anchor in segment.lower() for anchor in COACH_ANCHORS):
            return segment.strip()
    return None


def live_enabled() -> bool:
    flag = os.environ.get("AGENTG_BEHAVIORAL_LIVE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}
