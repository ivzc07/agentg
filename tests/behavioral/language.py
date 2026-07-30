"""Deterministic stray-English gate for Spanish conversations (issue #67).

The live judge scores language consistency; this offline lexicon is the CI
gate: English training vocabulary with an everyday Spanish equivalent must
never surface in a Spanish reply. Exercise catalog names are English by
design and deliberately absent from the lexicon — the carve-out in the
Agent's language rule covers them and nothing else.
"""

from __future__ import annotations

import re

# General training/goal vocabulary — never exercise names (those stay in
# their catalog form). tests/behavioral/test_language.py pins that no
# catalog name or alias collides with this list.
ENGLISH_TRAINING_VOCAB: tuple[str, ...] = (
    "muscle",
    "muscles",
    "strength",
    "fat",
    "fat loss",
    "weight loss",
    "conditioning",
    "endurance",
    "stamina",
    "bulking",
    "cutting",
    "lean",
)

def _compile(term: str) -> re.Pattern[str]:
    # Multi-word terms match any run of whitespace or hyphens between words,
    # so "weight loss" catches "weight-loss", doubled spaces, tabs, newlines.
    return re.compile(
        r"\b" + r"[\s-]+".join(re.escape(part) for part in term.split()) + r"\b",
        re.IGNORECASE,
    )


_PATTERNS = {term: _compile(term) for term in ENGLISH_TRAINING_VOCAB}


def find_english_leaks(reply: str) -> set[str]:
    """English training vocabulary found in a reply — empty means clean."""
    return {term for term, pattern in _PATTERNS.items() if pattern.search(reply)}
