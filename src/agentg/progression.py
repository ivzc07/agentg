"""Deterministic weight-progression math (spec §Routine adaptation).

Suggestions are *derived* from logged Sets under the Gym's rules doc — never
stored. This module is pure: it parses the doc's progression numbers and,
given an Exercise's recent completion history, decides the next weight. The
doc's numbers drive it, so a Coach editing the doc changes behaviour with no
code change.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# Round deloaded/eased weights to the nearest loadable step (kg or lb — plates
# come in 2.5 either way). Increments are added exactly as the doc states.
PLATE_STEP = 2.5


@dataclass(frozen=True)
class ProgressionRules:
    increment: float = 2.5  # weight added after a fully completed session
    deload_percent: float = 10.0  # lighter by this % after a stall
    stall_sessions: int = 2  # consecutive missed sessions that trigger a deload
    gap_deload_days: int = 10  # a gap at least this long eases weights back
    gap_deload_percent: float = 10.0  # lighter by this % when easing back


@dataclass(frozen=True)
class SessionResult:
    """One past Session's outcome for an Exercise, most-recent-first in a list.

    ``completed`` is tri-state: True/False when the prescription let us judge
    it, None when it couldn't be verified (e.g. an AMRAP or target-less
    scheme). Unverifiable Sessions never drive an increment *or* a deload.
    """

    weight: float | None  # the top working weight (None for bodyweight)
    completed: bool | None


@dataclass(frozen=True)
class Suggestion:
    suggested_weight: float | None
    action: str  # increment | hold | deload | gap_deload | none
    reason: str


_KEYS = {
    "increment": ("increment", "increment_kg"),
    "deload_percent": ("deload_percent",),
    "stall_sessions": ("stall_sessions",),
    "gap_deload_days": ("gap_deload_days",),
    "gap_deload_percent": ("gap_deload_percent",),
}


def parse_progression_rules(doc: str) -> ProgressionRules:
    """Read progression numbers from ``key: value`` lines in the doc.

    Missing keys keep their default, so a doc that omits a number (or the
    whole section) still works.  Out-of-range values are clamped to the
    defaults — a Coach's typo must never reach a Member as a coaching
    instruction (issue #167).
    """
    values: dict[str, float] = {}
    for field, aliases in _KEYS.items():
        for alias in aliases:
            match = re.search(rf"(?mi)^\s*[-*]?\s*{alias}\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", doc)
            if match:
                values[field] = float(match.group(1))
                break

    raw_increment = float(values.get("increment", ProgressionRules.increment))
    raw_deload_percent = float(values.get("deload_percent", ProgressionRules.deload_percent))
    raw_stall_sessions = int(values.get("stall_sessions", ProgressionRules.stall_sessions))
    raw_gap_deload_days = int(values.get("gap_deload_days", ProgressionRules.gap_deload_days))
    raw_gap_deload_percent = float(values.get("gap_deload_percent", ProgressionRules.gap_deload_percent))

    return ProgressionRules(
        increment=raw_increment if raw_increment >= 0 else ProgressionRules.increment,
        deload_percent=raw_deload_percent
        if 0 < raw_deload_percent < 100
        else ProgressionRules.deload_percent,
        stall_sessions=raw_stall_sessions
        if raw_stall_sessions >= 1
        else ProgressionRules.stall_sessions,
        gap_deload_days=raw_gap_deload_days
        if raw_gap_deload_days >= 1
        else ProgressionRules.gap_deload_days,
        gap_deload_percent=raw_gap_deload_percent
        if 0 < raw_gap_deload_percent < 100
        else ProgressionRules.gap_deload_percent,
    )


def parse_top_reps(scheme: str | None) -> int | None:
    """The top of a rep scheme: "8-12" → 12, "5" → 5, "AMRAP"/blank → None."""
    if not scheme:
        return None
    numbers = re.findall(r"\d+", scheme)
    return int(numbers[-1]) if numbers else None


def _plate_round(weight: float) -> float:
    return round(weight / PLATE_STEP) * PLATE_STEP


def suggest_weight(
    history: list[SessionResult], gap_days: int | None, rules: ProgressionRules
) -> Suggestion:
    """Next-weight suggestion for one Exercise. ``history`` is most-recent-first."""
    if not history or history[0].weight is None:
        return Suggestion(None, "none", "no prior working weight to progress from")
    last_weight = history[0].weight

    # A real break wins over any progression — ease back from the last weight.
    if gap_days is not None and gap_days >= rules.gap_deload_days:
        eased = max(
            PLATE_STEP,
            _plate_round(last_weight * (1 - rules.gap_deload_percent / 100)),
        )
        return Suggestion(
            eased,
            "gap_deload",
            f"{gap_days} days off — about {rules.gap_deload_percent:g}% lighter to ease back",
        )

    if history[0].completed is True:
        return Suggestion(
            last_weight + rules.increment,
            "increment",
            f"all sets done last time — up {rules.increment:g}",
        )

    # A stall is only a stall when every Session in the window verifiably
    # missed at the same weight — an unverifiable Session (completed is None)
    # is not evidence of a stall, so it holds instead of deloading.
    window = history[: rules.stall_sessions]
    stalled = (
        len(window) >= rules.stall_sessions
        and all(result.completed is False for result in window)
        and all(
            result.weight is not None and math.isclose(result.weight, last_weight, abs_tol=0.01)
            for result in window
        )
    )
    if stalled:
        deloaded = max(
            PLATE_STEP,
            _plate_round(last_weight * (1 - rules.deload_percent / 100)),
        )
        if deloaded >= last_weight:  # rounding must never leave a "deload" heavier or equal
            deloaded = max(PLATE_STEP, last_weight - PLATE_STEP)
        return Suggestion(
            deloaded,
            "deload",
            f"stalled {rules.stall_sessions} sessions — deload about {rules.deload_percent:g}%",
        )

    return Suggestion(last_weight, "hold", "hold here and aim to complete every set")
