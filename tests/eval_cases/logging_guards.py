"""Logging-guard eval cases (issue #53).

A logged line becomes the previous-session reference the Agent quotes next
time. Plausible-but-wrong parses (unit mix-up, swapped weight/reps) must not
silently poison that history. Two cheap layers:

1. After log_sets / copy_last_sets the Agent restates the parsed numbers so
   the Member can catch a bad parse while edit_logged_sets is one message away.
2. log_sets flags a suspect weight jump vs the Member's own history; the
   Agent double-checks conversationally without rejecting the log.
"""

from __future__ import annotations

from typing import Any


def reply_restates_logged_sets(reply: str, logged: dict[str, Any]) -> bool:
    """True when the reply restates the weight (if any) and every rep.

    The Agent must echo the tool result's numbers, not a vague ack — that is
    how the Member spots a wrong parse. Tolerates formatting (kg/×// /, etc.);
    only the digits matter.
    """
    text = reply.lower()
    weight = logged.get("weight")
    if weight is not None:
        # 60.0 and 60 are the same number to a Member reading chat.
        candidates = {f"{weight:g}", f"{weight:.1f}", f"{weight:.2f}"}
        if float(weight) == int(weight):
            candidates.add(str(int(weight)))
        if not any(token in text for token in candidates):
            return False
    for rep in logged["reps"]:
        if str(rep) not in text:
            return False
    return True


def reply_double_checks_suspect(reply: str) -> bool:
    """True when the reply asks the Member to confirm a suspect parse."""
    return "?" in reply


# --- cases the #51 harness will drive through the Agent loop ---------------

RESTATE_AFTER_LOG_SETS = {
    "id": "restate-after-log-sets",
    "stratum": "simple",
    "description": (
        "After log_sets the Agent restates the parsed weight and reps so the "
        "Member can catch a bad parse."
    ),
    "member_messages": ["bench 60 8,8,8"],
    "expected_tool": "log_sets",
    "logged": {"exercise": "bench press", "weight": 60.0, "reps": [8, 8, 8]},
    "reply_check": "restate_logged_sets",
}

RESTATE_AFTER_COPY_LAST_SETS = {
    "id": "restate-after-copy-last-sets",
    "stratum": "medium",
    "description": (
        "After copy_last_sets the Agent restates the copied numbers the same "
        "way as a fresh log."
    ),
    "setup_messages": ["overhead press 40 8,7,6", "done"],
    "member_messages": ["same as last time"],  # with OHP under discussion
    "expected_tool": "copy_last_sets",
    "logged": {"exercise": "overhead press", "weight": 40.0, "reps": [8, 7, 6]},
    "reply_check": "restate_logged_sets",
}

SUSPECT_JUMP_DOUBLE_CHECK = {
    "id": "suspect-jump-double-check",
    "stratum": "edge",
    "description": (
        "When log_sets returns a suspect hint the Agent still logged the sets "
        "but double-checks the numbers conversationally."
    ),
    "setup_messages": ["bench 60 8,8,8", "done"],
    "member_messages": ["bench 600 8,8,8"],  # plausible-but-wrong (extra zero)
    "expected_tool": "log_sets",
    "logged": {"exercise": "bench press", "weight": 600.0, "reps": [8, 8, 8]},
    "expect_suspect": True,
    "reply_check": "double_check_suspect",
}

ALL_CASES = [
    RESTATE_AFTER_LOG_SETS,
    RESTATE_AFTER_COPY_LAST_SETS,
    SUSPECT_JUMP_DOUBLE_CHECK,
]
