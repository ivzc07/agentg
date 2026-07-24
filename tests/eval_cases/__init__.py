"""Behavioral eval cases for the Agent (feeds issue #51).

Each case describes a conversation shape and how to score the Agent's reply
or end-state — not which words appear in the system prompt. The full harness
that drives the Agent loop lives with #51; the scorers here are deterministic
and run offline so a case can be unit-tested on its own.
"""
