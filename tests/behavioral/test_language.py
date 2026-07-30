"""Language consistency in chat (issue #67).

A Member spoken to in Spanish hears Spanish training vocabulary; the ONLY
English allowed in a Spanish reply is Exercise catalog names in their
catalog form. The deterministic lexicon gate here runs offline in CI; the
live judge scores the same dimension (``language_consistency``). What guards
the behavior in production is the Agent's language rule, so the prompt pins
live here too — a scripted-reply harness test would only check its own
canned input.
"""

from __future__ import annotations

from agentg.agent import INSTRUCTIONS
from agentg.training import SEED_EXERCISES
from behavioral.language import find_english_leaks

# The reply observed on the dev bot (2026-07-28), verbatim from the issue.
OBSERVED_LEAK = (
    "1️⃣ ¿Cuál es tu objetivo principal? — ¿Quieres ganar fuerza, muscle, "
    "perder grasa, mejorar condición general... algo así?"
)

CORRECTED = (
    "1️⃣ ¿Cuál es tu objetivo principal? — ¿Quieres ganar fuerza, "
    "masa muscular, perder grasa, mejorar condición general... algo así?"
)


def test_stray_english_vocabulary_in_a_spanish_reply_is_flagged():
    assert find_english_leaks(OBSERVED_LEAK) == {"muscle"}


def test_the_corrected_phrasing_passes():
    assert find_english_leaks(CORRECTED) == set()


def test_the_lexicon_never_collides_with_the_catalog():
    # The carve-out covers catalog names and aliases: none may read as a leak.
    for name, aliases in SEED_EXERCISES.items():
        for surface in (name, *aliases):
            assert find_english_leaks(surface) == set(), surface


def test_multi_word_terms_match_hyphen_and_loose_whitespace():
    # "weight loss" must catch every separator the model might emit.
    for variant in ("weight-loss", "weight  loss", "weight\tloss", "weight\nloss"):
        assert find_english_leaks(f"¿Quieres perder grasa o {variant}?") == {"weight loss"}, variant
    # …without swallowing the words apart from each other.
    assert find_english_leaks("levantar mucho weight es mi loss") == set()


# --- what actually guards the behavior: the Agent's language rule ---


def test_the_rule_still_passes_catalog_names_to_tools_exactly():
    # the carve-out narrows chat wording only — tool calls keep catalog form
    assert "pass catalog names to tools exactly" in INSTRUCTIONS.lower()


def test_the_rule_pins_spanish_goal_vocabulary_against_an_english_rules_doc():
    # the shipped rules doc states goals in English (#68); the rule must hold
    # anyway, since every Gym's doc is coach-editable and may say anything
    text = INSTRUCTIONS.lower()
    assert "masa muscular" in text
    assert "whatever language the rules doc" in text
