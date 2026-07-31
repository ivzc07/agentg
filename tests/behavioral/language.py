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

# Variant words allowed in a hyphen-joined prefix chain ("kipping-muscle-up").
_VARIANT_WORDS = frozenset({"kipping", "strict", "weighted", "banded"})


def _equipment_base(word: str) -> str:
    lowered = word.lower()
    return lowered[:-1] if lowered.endswith("s") else lowered  # plural ok


def _is_equipment(word: str) -> bool:
    """The ONE equipment-modifier predicate, shared by the space path and the
    hyphen path."""
    return _equipment_base(word) in _EQUIPMENT_MODIFIERS


def _leads_with_equipment(token: str) -> bool:
    # Plain equipment token, or a hyphen token whose first part is equipment
    # ("band-pull-apart" counts as equipment-led for the strength check).
    return _is_equipment(token.split("-", 1)[0])


def _is_allowed_prefix(parts: list[str]) -> bool:
    # Every prefix part must be an equipment modifier or variant word;
    # "strength" only immediately before an equipment part — a lexicon term
    # glued to a core ("muscle-pull-up") is a leak, not a name.
    for i, part in enumerate(parts):
        if _is_equipment(part) or part in _VARIANT_WORDS:
            continue
        following = parts[i + 1] if i + 1 < len(parts) else ""
        if part == "strength" and _is_equipment(following):
            continue
        return False
    return True


def _is_name_core(token: str) -> bool:
    # Exact allowlist match, or the allowlisted core as a hyphen SUFFIX of a
    # longer token whose prefix parts are all equipment/variant words
    # ("bar-muscle-up", "kipping-muscle-up", "strength-band-pull-apart");
    # plural "s" allowed.
    lowered = token.lower()
    forms = [lowered, lowered[:-1]] if lowered.endswith("s") else [lowered]
    for form in forms:
        if form in EXERCISE_NAME_CORES:
            return True
        for core in EXERCISE_NAME_CORES:
            if form.endswith(f"-{core}") and _is_allowed_prefix(form[: -len(core) - 1].split("-")):
                return True
    return False


def _absorbable(word: str, following: str) -> bool:
    # A token the space path may absorb before a core: "strength" when the
    # following token leads with equipment (plain "band" or equipment-led
    # "band-pull-apart"), or any token whose own hyphen parts form an
    # allowed prefix chain — equipment, variant words, glued
    # "strength-band(s)", multi-part "strength-band-bar" — the same rule the
    # hyphen path applies to core prefixes.
    if word == "strength" and _leads_with_equipment(following):
        return True
    return _is_allowed_prefix(word.split("-"))


_HORIZONTAL_GAP_RE = re.compile(r"[ \t]+")


def _exercise_name_spans(text: str) -> list[tuple[int, int]]:
    """Spans the catalog carve-out covers: an allowlisted name core plus the
    modifier tokens immediately preceding it. The walk is bounded by the
    modifier rule itself, not a count; no right-side absorption, no generic
    neighbor walking, and gaps are spaces/tabs only — a name mention broken
    across lines or separator characters is not one name."""
    tokens = list(_TOKEN_RE.finditer(text))
    spans = []
    for i, tok in enumerate(tokens):
        if not _is_name_core(tok.group()):
            continue
        start = tok.start()
        k = i - 1
        while k >= 0 and _HORIZONTAL_GAP_RE.fullmatch(text[tokens[k].end() : start]):
            word = tokens[k].group().lower()
            following = tokens[k + 1].group().lower()
            if not _absorbable(word, following):
                break
            start = tokens[k].start()
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
