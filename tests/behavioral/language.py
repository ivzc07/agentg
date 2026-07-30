"""Deterministic stray-English gate for Spanish conversations (issue #67).

The live judge scores language consistency; this offline lexicon is the CI
gate: English training vocabulary with an everyday Spanish equivalent must
never surface in a Spanish reply. Exercise catalog names are English by
design and deliberately absent from the lexicon — the carve-out in the
Agent's language rule covers them and nothing else. Exercise-name shapes are
exempt too: a lexicon hit inside a hyphenated particle compound
("Muscle-up", "strength band pull-apart") is part of the name, not a leak —
but a hit next to one ("muscle pull-ups") or spelled with hyphens
("weight-loss") still flags.
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

# Direction particles that mark an exercise-name shape ("Muscle-up",
# "pull-apart"). Deliberately singular: "pull-ups" is the catalog plural of
# pull-up, not a compound, and must not carve out neighboring leaks.
_PARTICLES = frozenset({"up", "down", "apart", "over", "out", "through"})

# Common Spanish words. They break an exercise-name span, so a leak on the
# other side of one ("strength haz Muscle-up") stays flagged.
_SPANISH_WORDS = frozenset(
    "a al con de del e el en haz la las los ni o para pero por que sin u un una unas unos y".split()
)


def _exercise_name_spans(text: str) -> list[tuple[int, int]]:
    """Spans shaped like exercise names the catalog carve-out covers.

    The core is a hyphenated token carrying a direction particle
    ("Muscle-up", "pull-apart") — never "weight-loss" or "muscle-building",
    whose other parts are ordinary words. It extends over single-space-joined
    neighbors that are not Spanish words ("strength band pull-apart"), and
    nowhere else: "muscle pull-ups" is a leak next to a catalog name, not a
    compound. Shape-based only — no dependence on the seed catalog list.
    """
    tokens = list(_TOKEN_RE.finditer(text))
    spans = []
    for i, tok in enumerate(tokens):
        parts = tok.group().split("-")
        if len(parts) < 2 or not any(p.lower() in _PARTICLES for p in parts):
            continue
        start, end = tok.start(), tok.end()
        k = i - 1
        while (
            k >= 0
            and text[tokens[k].end() : start] == " "
            and "-" not in tokens[k].group()
            and tokens[k].group().lower() not in _SPANISH_WORDS
        ):
            start = tokens[k].start()
            k -= 1
        k = i + 1
        while (
            k < len(tokens)
            and text[end : tokens[k].start()] == " "
            and "-" not in tokens[k].group()
            and tokens[k].group().lower() not in _SPANISH_WORDS
        ):
            end = tokens[k].end()
            k += 1
        spans.append((start, end))
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
