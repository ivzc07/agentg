"""Conversational tone lives in behavioral end-state evals, not prompt wording.

Historically this file asserted that words like "react" / "greet" / "joke"
appeared in ``INSTRUCTIONS``. Those checks passed even when the agent
misbehaved and failed on harmless rewording (issue #51).

Tone, safety-compliance, and no-nagging are now covered by:
- ``tests/behavioral/`` deterministic conversations (end-state DB asserts)
- ``tests/behavioral/test_judge.py`` optional per-dimension judge rubric
"""

from agentg.agent import INSTRUCTIONS


def test_agent_still_ships_non_empty_instructions():
    """Smoke only — wording is not the contract; behavior is."""
    assert isinstance(INSTRUCTIONS, str)
    assert len(INSTRUCTIONS) > 100
