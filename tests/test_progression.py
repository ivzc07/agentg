"""Pure progression math and rules-doc parsing (spec §Routine adaptation).

Suggested weights are derived from logged Sets under the doc's progression
rules. This module holds the deterministic core; editing the doc's numbers
must change the output with no code change.
"""

import pytest

from agentg.progression import (
    ProgressionRules,
    SessionResult,
    parse_progression_rules,
    parse_top_reps,
    suggest_weight,
)


def rules(**overrides) -> ProgressionRules:
    base = dict(
        increment=2.5,
        deload_percent=10.0,
        stall_sessions=2,
        gap_deload_days=10,
        gap_deload_percent=10.0,
    )
    base.update(overrides)
    return ProgressionRules(**base)


# --- parsing the rules doc ---


def test_defaults_when_the_doc_says_nothing():
    parsed = parse_progression_rules("# rules with no numbers")
    assert parsed == ProgressionRules()


def test_parses_the_progression_parameters_from_the_doc():
    doc = """\
## Progression
- increment: 5
- deload_percent: 15
- stall_sessions: 3
- gap_deload_days: 14
- gap_deload_percent: 20
"""
    parsed = parse_progression_rules(doc)
    assert parsed.increment == 5.0
    assert parsed.deload_percent == 15.0
    assert parsed.stall_sessions == 3
    assert parsed.gap_deload_days == 14
    assert parsed.gap_deload_percent == 20.0


def test_increment_kg_is_accepted_as_an_alias():
    assert parse_progression_rules("increment_kg: 1.25").increment == 1.25


def test_the_default_rules_doc_parses_to_the_shipped_progression_defaults():
    # the progression parameter lines are coach-editable and load-bearing
    # (issue #68) — rewording the doc must not disturb them
    from agentg.routines import DEFAULT_RULES_DOC

    assert parse_progression_rules(DEFAULT_RULES_DOC) == ProgressionRules()


@pytest.mark.parametrize(
    ("scheme", "top"),
    [("8-12", 12), ("5", 5), ("8", 8), ("12-15", 15), ("AMRAP", None), (None, None), ("", None)],
)
def test_parse_top_reps(scheme, top):
    assert parse_top_reps(scheme) == top


# --- the suggestion algorithm ---


def done(weight: float) -> SessionResult:
    return SessionResult(weight=weight, completed=True)


def missed(weight: float) -> SessionResult:
    return SessionResult(weight=weight, completed=False)


def test_all_sets_completed_suggests_the_increment_over_last_weight():
    s = suggest_weight([done(80.0)], gap_days=2, rules=rules())
    assert s.action == "increment"
    assert s.suggested_weight == 82.5


def test_a_different_increment_in_the_doc_changes_the_suggestion():
    s = suggest_weight([done(80.0)], gap_days=2, rules=rules(increment=5.0))
    assert s.suggested_weight == 85.0  # no code change, just the doc number


def test_an_incomplete_last_session_holds_the_weight():
    s = suggest_weight([missed(80.0)], gap_days=2, rules=rules())
    assert s.action == "hold"
    assert s.suggested_weight == 80.0


def test_a_stall_pattern_triggers_a_deload():
    # two consecutive missed sessions at the same weight → deload
    s = suggest_weight([missed(80.0), missed(80.0)], gap_days=2, rules=rules())
    assert s.action == "deload"
    assert s.suggested_weight == 72.5  # 80 * 0.9 = 72, rounded to nearest 2.5


def test_the_deload_percent_comes_from_the_doc():
    s = suggest_weight([missed(100.0), missed(100.0)], gap_days=2, rules=rules(deload_percent=20))
    assert s.suggested_weight == 80.0


def test_one_missed_session_is_not_yet_a_stall():
    s = suggest_weight([missed(80.0), done(80.0)], gap_days=2, rules=rules())
    assert s.action == "hold"


def test_a_stall_needs_the_same_weight_across_the_window():
    # missed at different weights is not a stall — hold
    s = suggest_weight([missed(80.0), missed(77.5)], gap_days=2, rules=rules())
    assert s.action == "hold"


def test_returning_after_a_gap_eases_back_from_the_last_weight():
    s = suggest_weight([done(80.0)], gap_days=14, rules=rules())
    assert s.action == "gap_deload"
    assert s.suggested_weight == 72.5  # ~10% lighter than 80, not the +2.5


def test_the_gap_threshold_and_reduction_come_from_the_doc():
    easy = suggest_weight([done(100.0)], gap_days=8, rules=rules(gap_deload_days=7, gap_deload_percent=20))
    assert easy.action == "gap_deload"
    assert easy.suggested_weight == 80.0


def test_a_short_gap_does_not_trigger_ease_back():
    s = suggest_weight([done(80.0)], gap_days=3, rules=rules(gap_deload_days=10))
    assert s.action == "increment"


def unknown(weight: float) -> SessionResult:
    return SessionResult(weight=weight, completed=None)


def test_unverifiable_sessions_hold_and_never_deload():
    # e.g. an AMRAP scheme — we can't confirm completion, so we neither push
    # nor punish: two such sessions at the same weight must not deload.
    s = suggest_weight([unknown(80.0), unknown(80.0)], gap_days=2, rules=rules())
    assert s.action == "hold"
    assert s.suggested_weight == 80.0


def test_a_deload_always_reduces_even_for_small_loads():
    s = suggest_weight([missed(5.0), missed(5.0)], gap_days=2, rules=rules())
    assert s.action == "deload"
    assert s.suggested_weight is not None and s.suggested_weight < 5.0


def test_bodyweight_history_yields_no_weight_suggestion():
    s = suggest_weight([SessionResult(weight=None, completed=True)], gap_days=2, rules=rules())
    assert s.suggested_weight is None
    assert s.action == "none"


def test_no_history_yields_no_suggestion():
    s = suggest_weight([], gap_days=None, rules=rules())
    assert s.suggested_weight is None
    assert s.action == "none"
