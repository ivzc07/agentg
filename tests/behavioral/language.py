"""Deterministic stray-English gate for Spanish conversations (issue #67).

The live judge scores language consistency; this offline lexicon is the CI
gate: English training vocabulary with an everyday Spanish equivalent must
never surface in a Spanish reply. Exercise catalog names are English by
design and deliberately absent from the lexicon — the carve-out in the
Agent's language rule covers them and nothing else. Allowlisted exercise-name
shapes are exempt too ("Muscle-up", "strength band pull-apart"), but a hit
next to one ("muscle pull-ups") or spelled with hyphens ("weight-loss")
still flags.
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

_LETTER = r"A-Za-zÀ-ÖØ-öø-ÿ"
_TOKEN_RE = re.compile(rf"[{_LETTER}]+(?:-[{_LETTER}]+)*")

# Hyphenated exercise-name shapes the catalog carve-out covers
# (case-insensitive, optional plural "s" on the final particle). Anything
# particle-bearing but not listed here ("lean-out", "bulking-up") is NOT a
# name — its lexicon parts flag normally. Adding a catalog name is one line.
EXERCISE_NAME_CORES: frozenset[str] = frozenset(
    {
        "pull-up",
        "push-up",
        "chin-up",
        "sit-up",
        "step-up",
        "muscle-up",
        "pull-apart",
        "pull-over",
        "pull-through",
    }
)

# Equipment modifiers that may immediately precede a core ("band
# pull-apart"). "strength" counts only directly before one of these
# ("strength band pull-apart") — never alone next to a core.
_EQUIPMENT_MODIFIERS = frozenset({"band", "bar", "ring", "cable"})


def _is_name_core(token: str) -> bool:
    lowered = token.lower()
    return lowered in EXERCISE_NAME_CORES or (
        lowered.endswith("s") and lowered[:-1] in EXERCISE_NAME_CORES
    )


def _exercise_name_spans(text: str) -> list[tuple[int, int]]:
    """Spans the catalog carve-out covers: an allowlisted name core plus at
    most two immediately preceding equipment-modifier tokens. No right-side
    absorption, no generic neighbor walking."""
    tokens = list(_TOKEN_RE.finditer(text))
    spans = []
    for i, tok in enumerate(tokens):
        if not _is_name_core(tok.group()):
            continue
        start = tok.start()
        absorbed = 0
        k = i - 1
        while k >= 0 and absorbed < 2 and text[tokens[k].end() : start] == " ":
            word = tokens[k].group().lower()
            following = tokens[k + 1].group().lower()
            if word not in _EQUIPMENT_MODIFIERS and not (
                word == "strength" and following in _EQUIPMENT_MODIFIERS
            ):
                break
            start = tokens[k].start()
            absorbed += 1
            k -= 1
        spans.append((start, tok.end()))
    return spans


def find_english_leaks(reply: str) -> set[str]:
    """English training vocabulary found in a reply — empty means clean."""
    names = _exercise_name_spans(reply)
    leaks = set()
    for term, pattern in _PATTERNS.items():
        for match in pattern.finditer(reply):
            if not any(start <= match.start() and match.end() <= end for start, end in names):
                leaks.add(term)
    return leaks
