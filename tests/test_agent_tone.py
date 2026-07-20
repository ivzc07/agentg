"""Conversational tone: one standing trait, not a patch per symptom.

The Agent must react to what's actually said instead of running a script —
that covers both not re-greeting (onboarding already said hello) and not
steamrolling past a joke or hesitation to the next scripted step.
"""

from agentg.agent import INSTRUCTIONS


def test_the_instructions_say_to_react_to_what_was_actually_said():
    text = INSTRUCTIONS.lower()
    assert "react" in text


def test_the_instructions_cover_not_re_greeting():
    text = INSTRUCTIONS.lower()
    assert "greeting" in text or "greet" in text


def test_the_instructions_cover_not_steamrolling_past_a_joke_or_hesitation():
    text = INSTRUCTIONS.lower()
    assert "joke" in text
    assert "hesitation" in text
