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


def test_lexicon_hits_inside_compound_exercise_names_are_not_leaks():
    # The carve-out covers compound exercise-style names too — hyphenated or
    # attached to a hyphenated compound — whatever the seed catalog contains.
    assert find_english_leaks("Muscle-up") == set()
    assert find_english_leaks("strength band pull-apart") == set()


def test_the_compound_exemption_still_flags_standalone_leaks():
    # A genuine leak across a Spanish connector from a compound stays flagged…
    assert find_english_leaks("¿Quieres ganar muscle y hacer pull-ups?") == {"muscle"}
    # …and a lexicon phrase spelled as a compound is still the phrase.
    assert find_english_leaks("tu objetivo es weight-loss") == {"weight loss"}


def test_space_separated_leaks_next_to_compounds_are_still_flagged():
    # Round-3: the exemption must not swallow real leaks near a compound.
    assert find_english_leaks("vamos a hacer muscle pull-ups hoy") == {"muscle"}
    assert find_english_leaks("para ganar strength haz Muscle-up") == {"strength"}
    assert find_english_leaks("tu muscle es weight-loss") == {"muscle", "weight loss"}
    assert find_english_leaks("haz pull-ups strength por favor") == {"strength"}


def test_a_hit_spanning_hyphens_is_a_leak_unless_in_a_particle_compound():
    # "weight loss" spelled with hyphens is still the phrase…
    assert find_english_leaks("weight-loss-plan") == {"weight loss"}
    assert find_english_leaks("pre-weight-loss") == {"weight loss"}
    # …and a lexicon word plus a generic suffix is a leak, not a name shape.
    assert find_english_leaks("muscle-building") == {"muscle"}
    assert find_english_leaks("strength-focus") == {"strength"}


def test_allowlisted_name_cores_stay_clean_including_plurals():
    # Round-4: cores come from an explicit allowlist, plural particle ok.
    assert find_english_leaks("muscle-ups") == set()
    assert find_english_leaks("Muscle-ups") == set()
    assert find_english_leaks("strength band pull-aparts") == set()
    assert find_english_leaks("Hoy toca muscle-ups y dips") == set()


def test_only_equipment_modifiers_extend_a_name_core_backwards():
    # "strength" counts only as part of "strength band/bar/…" before a core;
    # anything else next to a core — before or after — is a leak.
    assert find_english_leaks("strength Muscle-up") == {"strength"}
    assert find_english_leaks("Muscle-up strength") == {"strength"}
    assert find_english_leaks("muscle pull-apart") == {"muscle"}
    assert find_english_leaks("vamos a hacer strength Muscle-up hoy") == {"strength"}
    assert find_english_leaks("haz pull-up strength por favor") == {"strength"}
    assert find_english_leaks("pull-up for strength") == {"strength"}
    assert find_english_leaks("muscle pull-ups") == {"muscle"}


def test_other_hyphenated_tokens_are_not_name_cores():
    # Particle-bearing, but not on the allowlist: lexicon parts flag normally.
    assert find_english_leaks("lean-out") == {"lean"}
    assert find_english_leaks("cutting-down") == {"cutting"}
    assert find_english_leaks("bulking-up") == {"bulking"}
    assert find_english_leaks("fat-over") == {"fat"}
    assert find_english_leaks("strength-through-progress") == {"strength"}


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
