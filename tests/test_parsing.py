"""The parse suite for terse set shorthand (spec §The logging conversation).

Free-text parse quality is the product's load-bearing UX risk, so every
shorthand variant from the accepted prototype script is pinned here.
"""

import pytest

from agentg.parsing import ParsedSetLine, parse_set_line

# --- the accepted prototype script's forms ---


def test_exercise_weight_and_comma_reps():
    assert parse_set_line("bench 60 8,8,8") == ParsedSetLine(
        exercise="bench", weight=60.0, unit=None, reps=[8, 8, 8]
    )


def test_weight_and_reps_without_an_exercise():
    # the opener promises: "60 8,8,7 works"
    assert parse_set_line("60 8,8,7") == ParsedSetLine(
        exercise=None, weight=60.0, unit=None, reps=[8, 8, 7]
    )


def test_bodyweight_reps_only():
    assert parse_set_line("dips 10,10,9") == ParsedSetLine(
        exercise="dips", weight=None, unit=None, reps=[10, 10, 9]
    )


def test_decimal_weight():
    assert parse_set_line("bench 62.5 8,8,8") == ParsedSetLine(
        exercise="bench", weight=62.5, unit=None, reps=[8, 8, 8]
    )


def test_slash_separated_reps():
    assert parse_set_line("overhead press 40 8/7/6") == ParsedSetLine(
        exercise="overhead press", weight=40.0, unit=None, reps=[8, 7, 6]
    )


def test_bare_reps_group():
    assert parse_set_line("8/8/7") == ParsedSetLine(
        exercise=None, weight=None, unit=None, reps=[8, 8, 7]
    )


# --- units (kg/lb, gym default decides the stored unit) ---


def test_weight_with_attached_kg_unit():
    assert parse_set_line("bench 60kg 8,8,8") == ParsedSetLine(
        exercise="bench", weight=60.0, unit="kg", reps=[8, 8, 8]
    )


def test_weight_with_detached_lb_unit():
    assert parse_set_line("bench 135 lbs 5,5,5") == ParsedSetLine(
        exercise="bench", weight=135.0, unit="lb", reps=[5, 5, 5]
    )


def test_weight_with_attached_lb_unit():
    assert parse_set_line("squat 225lb 5/5/5") == ParsedSetLine(
        exercise="squat", weight=225.0, unit="lb", reps=[5, 5, 5]
    )


# --- NxM shorthand ---


def test_sets_times_reps_with_weight():
    assert parse_set_line("bench 60 3x8") == ParsedSetLine(
        exercise="bench", weight=60.0, unit=None, reps=[8, 8, 8]
    )


def test_bare_sets_times_reps():
    assert parse_set_line("3x8") == ParsedSetLine(
        exercise=None, weight=None, unit=None, reps=[8, 8, 8]
    )


def test_single_set_with_weight():
    assert parse_set_line("bench 60 8") == ParsedSetLine(
        exercise="bench", weight=60.0, unit=None, reps=[8]
    )


# --- forgiveness ---


def test_spaces_inside_the_reps_list_are_fine():
    assert parse_set_line("bench 60 8, 8, 8") == ParsedSetLine(
        exercise="bench", weight=60.0, unit=None, reps=[8, 8, 8]
    )


def test_case_and_outer_whitespace_are_ignored():
    assert parse_set_line("  Bench 60KG 8,8,8  ") == ParsedSetLine(
        exercise="bench", weight=60.0, unit="kg", reps=[8, 8, 8]
    )


def test_multi_word_exercises_keep_all_words():
    assert parse_set_line("lat pulldown 50 10,10,10") == ParsedSetLine(
        exercise="lat pulldown", weight=50.0, unit=None, reps=[10, 10, 10]
    )


# --- lines that are NOT set logs must not parse ---


@pytest.mark.parametrize(
    "text",
    [
        "I'm here",
        "done",
        "same as last time",
        "how do i do dips again?",
        "actually bench was 62.5 not 60",  # a correction, not a log line
        "bench",  # no numbers at all
        "dips 10",  # one bare number: reps or weight? ambiguous, ask
        "bench 60",  # weight but no reps
        "bench 60 8,8,8 felt heavy",  # trailing chatter — reformulate instead
        "",
        "see you friday at 6",
    ],
)
def test_non_log_lines_return_none(text):
    assert parse_set_line(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "bench 60 0,8,8",  # a zero-rep set is noise
        "bench 60 999x8",  # absurd set count
        "bench 9999 8,8,8",  # absurd weight
    ],
)
def test_implausible_numbers_return_none(text):
    assert parse_set_line(text) is None
