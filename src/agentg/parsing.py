"""Deterministic parsing of terse set shorthand ("bench 60 8,8,8").

Free-text parse quality is the logging conversation's load-bearing UX risk
(docs/spec.md §The logging conversation), so the grammar is code, not model
behaviour: a line is ``[exercise words] [weight [unit]] reps`` and anything
else returns ``None`` — the Agent then asks rather than guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_WEIGHT = 999.0
# Reps above this read as a weight that strayed into the reps group (e.g. the
# fused "60,8,8" from "deadlift 60, 8,8") — refuse and let the Agent ask.
MAX_REPS = 50
MAX_SETS = 20

_REPS_LIST = re.compile(r"^\d+(?:[,/]\d+)+$")
_SETS_X_REPS = re.compile(r"^(\d+)x(\d+)$")
_INT = re.compile(r"^\d+$")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")
_NUMBER_WITH_UNIT = re.compile(r"^(\d+(?:\.\d+)?)(kg|kgs|lb|lbs)$")
_EXERCISE_WORD = re.compile(r"^[a-z][a-z-]*$")
_UNITS = {"kg": "kg", "kgs": "kg", "lb": "lb", "lbs": "lb"}


@dataclass(frozen=True)
class ParsedSetLine:
    exercise: str | None  # None when the line leans on conversation context
    weight: float | None  # None for bodyweight work
    unit: str | None  # "kg"/"lb" only when the Member typed it
    reps: list[int]  # one entry per performed Set


def parse_set_line(text: str) -> ParsedSetLine | None:
    line = text.strip().lower()
    if not line or not any(char.isdigit() for char in line):
        return None
    line = re.sub(r"\s*([,/])\s*", r"\1", line)  # "8, 8, 8" -> "8,8,8"
    tokens = line.split()

    reps = _parse_reps(tokens[-1])
    if reps is None:
        return None
    rest = tokens[:-1]

    weight, unit, rest = _parse_weight(rest)
    if weight is not None and not (0 < weight <= MAX_WEIGHT):
        return None
    # A lone trailing number ("dips 10") is ambiguous — reps or weight? —
    # unless a weight precedes it ("bench 60 8" is one set of 8 at 60).
    if _INT.fullmatch(tokens[-1]) and weight is None:
        return None

    if any(not _EXERCISE_WORD.fullmatch(word) for word in rest):
        return None
    exercise = " ".join(rest) or None
    return ParsedSetLine(exercise=exercise, weight=weight, unit=unit, reps=reps)


def _parse_reps(token: str) -> list[int] | None:
    if _REPS_LIST.fullmatch(token):
        reps = [int(part) for part in re.split(r"[,/]", token)]
    elif match := _SETS_X_REPS.fullmatch(token):
        sets, per_set = int(match[1]), int(match[2])
        if not 1 <= sets <= MAX_SETS:
            return None
        reps = [per_set] * sets
    elif _INT.fullmatch(token):
        reps = [int(token)]
    else:
        return None
    if len(reps) > MAX_SETS:
        return None
    if any(not 1 <= rep <= MAX_REPS for rep in reps):
        return None
    return reps


def _parse_weight(rest: list[str]) -> tuple[float | None, str | None, list[str]]:
    if len(rest) >= 2 and rest[-1] in _UNITS and _NUMBER.fullmatch(rest[-2]):
        return float(rest[-2]), _UNITS[rest[-1]], rest[:-2]  # "135 lbs"
    if rest and (match := _NUMBER_WITH_UNIT.fullmatch(rest[-1])):
        return float(match[1]), _UNITS[match[2]], rest[:-1]  # "60kg"
    if rest and _NUMBER.fullmatch(rest[-1]):
        return float(rest[-1]), None, rest[:-1]  # "60"
    return None, None, rest
