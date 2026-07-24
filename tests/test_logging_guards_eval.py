"""Behavioral eval cases for logging guards (issue #53).

These score replies against the restate / suspect-check contract — they do
not grep the system prompt. The #51 harness will drive real conversations
through the same scorers; here we pin the scorer behaviour with worked
examples (the prototype script's good reply, and a bare ack that must fail).
"""

from eval_cases.logging_guards import (
    ALL_CASES,
    RESTATE_AFTER_COPY_LAST_SETS,
    RESTATE_AFTER_LOG_SETS,
    SUSPECT_JUMP_DOUBLE_CHECK,
    reply_double_checks_suspect,
    reply_restates_logged_sets,
)


def test_restate_scorer_accepts_the_prototype_echo():
    # docs/prototypes/workout-logging-conversation.md — Variant A after "bench 60 8,8,8"
    logged = RESTATE_AFTER_LOG_SETS["logged"]
    reply = "Bench 60 kg ×8 ×8 ×8 ✅ — all three at 8, that's the 8/8/7 from last week beaten."
    assert reply_restates_logged_sets(reply, logged)


def test_restate_scorer_accepts_a_copy_last_sets_echo():
    logged = RESTATE_AFTER_COPY_LAST_SETS["logged"]
    reply = "Overhead press 40 kg ×8 ×7 ×6 ✅ (copied from last push day)."
    assert reply_restates_logged_sets(reply, logged)


def test_restate_scorer_rejects_a_bare_ack_that_hides_a_bad_parse():
    logged = RESTATE_AFTER_LOG_SETS["logged"]
    assert not reply_restates_logged_sets("Got it, nice work!", logged)


def test_restate_scorer_rejects_a_reply_that_drops_the_weight():
    logged = RESTATE_AFTER_LOG_SETS["logged"]
    assert not reply_restates_logged_sets("Bench ×8 ×8 ×8 logged.", logged)


def test_restate_scorer_rejects_a_wrong_echo_containing_the_digits():
    # "600" must not satisfy weight 60 via substring accident.
    logged = RESTATE_AFTER_LOG_SETS["logged"]
    assert not reply_restates_logged_sets("Bench 600 kg ×8 ×8 ×8 ✅", logged)


def test_suspect_scorer_accepts_a_double_check():
    # Issue #53 example: "600 - did you mean 60?"
    assert reply_double_checks_suspect(
        "Logged bench 600kg 8/8/8 — 600, did you mean 60?"
    )


def test_suspect_scorer_rejects_a_silent_accept():
    assert not reply_double_checks_suspect("Bench 600 kg ×8 ×8 ×8 ✅ solid.")


def test_eval_cases_cover_restate_after_both_log_paths_and_suspect():
    ids = {case["id"] for case in ALL_CASES}
    assert RESTATE_AFTER_LOG_SETS["id"] in ids
    assert RESTATE_AFTER_COPY_LAST_SETS["id"] in ids
    assert SUSPECT_JUMP_DOUBLE_CHECK["id"] in ids
    assert SUSPECT_JUMP_DOUBLE_CHECK["expect_suspect"] is True
