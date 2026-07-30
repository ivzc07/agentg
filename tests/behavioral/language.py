"""Deterministic stray-English gate for Spanish conversations (issue #67).

The live judge scores language consistency; this offline lexicon is the CI
gate: English training vocabulary with an everyday Spanish equivalent must
never surface in a Spanish reply. Exercise catalog names are English by
design and deliberately absent from the lexicon — the carve-out in the
Agent's language rule covers them and nothing else. Compound exercise-style
names ("Muscle-up", "strength band pull-apart") are exempt too: a lexicon
hit inside a hyphenated compound is part of the name, not a leak.
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

# Common Spanish connectors. They break an exercise-name compound, so a leak
# on the other side of one ("ganar muscle y hacer pull-ups") stays flagged.
_CONNECTORS = frozenset(
    "a al con de del e el en la las los ni o para pero por que sin u un una unas unos y".split()
)


def _compound_spans(text: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """(compound span, hyphenated-token span) per hyphenated token.

    A compound is a hyphenated token ("Muscle-up", "pull-apart") extended
    over single-space-joined neighbor tokens that are not Spanish connectors
    ("strength band pull-apart"). It is an exercise-style name shape, so the
    catalog carve-out covers it without depending on the seed catalog list.
    """
    tokens = list(_TOKEN_RE.finditer(text))
    spans = []
    for i, tok in enumerate(tokens):
        if "-" not in tok.group():
            continue
        start, end = tok.start(), tok.end()
        k = i - 1
        while (
            k >= 0
            and text[tokens[k].end() : start] == " "
            and "-" not in tokens[k].group()
            and tokens[k].group().lower() not in _CONNECTORS
        ):
            start = tokens[k].start()
            k -= 1
        k = i + 1
        while (
            k < len(tokens)
            and text[end : tokens[k].start()] == " "
            and "-" not in tokens[k].group()
            and tokens[k].group().lower() not in _CONNECTORS
        ):
            end = tokens[k].end()
            k += 1
        spans.append(((start, end), (tok.start(), tok.end())))
    return spans


def _inside_a_compound(hit: tuple[int, int], compounds: list[tuple[tuple[int, int], tuple[int, int]]]) -> bool:
    # A hit inside a compound is part of the exercise-style name — unless it
    # spans the hyphenated token itself ("weight-loss" is still the phrase
    # "weight loss", hyphen or not).
    return any(
        c_start <= hit[0] and hit[1] <= c_end and not (hit[0] <= h_start and hit[1] >= h_end)
        for (c_start, c_end), (h_start, h_end) in compounds
    )


def find_english_leaks(reply: str) -> set[str]:
    """English training vocabulary found in a reply — empty means clean."""
    compounds = _compound_spans(reply)
    leaks = set()
    for term, pattern in _PATTERNS.items():
        for match in pattern.finditer(reply):
            if not _inside_a_compound((match.start(), match.end()), compounds):
                leaks.add(term)
    return leaks
