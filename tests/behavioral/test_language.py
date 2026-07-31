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

import pytest

from agentg.agent import INSTRUCTIONS
from agentg.training import SEED_EXERCISES
from behavioral.language import ENGLISH_TRAINING_VOCAB, find_english_leaks

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


def test_a_core_may_be_a_hyphen_suffix_of_a_longer_token():
    # Round-5: equipment/variant prefixes join the core with a hyphen.
    assert find_english_leaks("bar-muscle-up") == set()
    assert find_english_leaks("ring-muscle-up") == set()
    assert find_english_leaks("kipping-muscle-up") == set()
    assert find_english_leaks("strength-band-pull-apart") == set()


def test_equipment_modifiers_accept_plural_s():
    for equipment in ("band", "bands", "bar", "bars", "ring", "rings", "cable", "cables"):
        assert find_english_leaks(f"strength {equipment} pull-apart") == set(), equipment


def test_a_core_suffix_prefix_must_be_equipment_or_variant_words():
    # Round-6: a lexicon term glued to a core is a leak, not a name prefix.
    assert find_english_leaks("muscle-pull-up") == {"muscle"}
    assert find_english_leaks("strength-pull-up") == {"strength"}
    assert find_english_leaks("fat-pull-up") == {"fat"}
    assert find_english_leaks("lean-muscle-up") == {"lean", "muscle"}
    assert find_english_leaks("weight-loss-pull-up") == {"weight loss"}
    assert find_english_leaks("muscle-building-pull-up") == {"muscle"}


def test_mixed_space_and_hyphen_name_shapes_stay_clean():
    # Round-7: the space path and hyphen path share one modifier predicate.
    assert find_english_leaks("strength band-pull-apart") == set()
    assert find_english_leaks("strength bar-muscle-up") == set()
    assert find_english_leaks("strength ring-muscle-ups") == set()
    assert find_english_leaks("strength cable-pull-through") == set()
    assert find_english_leaks("strength-band pull-apart") == set()
    assert find_english_leaks("strength-bands pull-apart") == set()
    assert find_english_leaks("strength-band muscle-up") == set()
    assert find_english_leaks("strength  band pull-apart") == set()  # double space
    assert find_english_leaks("strength\tband pull-apart") == set()  # tab


def test_multi_part_glued_modifiers_extend_like_their_hyphenated_twins():
    # Round-8: the space path reuses the same prefix-chain rule.
    assert find_english_leaks("strength-band-bar pull-apart") == set()
    assert find_english_leaks("strength-band-cable pull-through") == set()
    assert find_english_leaks("strength-bands-bar pull-apart") == set()


def test_absorption_does_not_cross_newlines():
    # Round-8: a name mention broken across lines is not one name.
    assert find_english_leaks("Objetivo: strength\nband-pull-apart 3x10") == {"strength"}
    assert find_english_leaks("Tu objetivo es strength\nband pull-apart") == {"strength"}
    assert find_english_leaks("para ganar strength\n\nbar-muscle-up") == {"strength"}


def test_absorption_gaps_are_space_or_tab_only():
    # Round-9: \v, \f, NEL, line separators are not name-internal gaps.
    assert find_english_leaks("strength\x85band-pull-apart") == {"strength"}
    assert find_english_leaks("strength bar-muscle-up") == {"strength"}
    assert find_english_leaks("strength\vband pull-apart") == {"strength"}
    assert find_english_leaks("strength\fband-pull-apart") == {"strength"}


def test_the_space_walk_is_bounded_by_the_modifier_rule_not_a_count():
    # Round-9: longer modifier chains are clean like their hyphenated twins.
    assert find_english_leaks("strength band bar pull-apart") == set()
    assert find_english_leaks("strength bands bar pull-apart") == set()
    assert find_english_leaks("strength cable bar pull-through") == set()


def test_strength_only_counts_as_the_first_prefix_part():
    # Round-10: a single leading strength+equipment unit, nothing more —
    # strength mid-chain is laundered vocabulary and flags.
    assert find_english_leaks("kipping strength band pull-apart") == {"strength"}
    assert find_english_leaks("bar strength band pull-apart") == {"strength"}
    assert find_english_leaks("band strength band pull-apart") == {"strength"}
    assert find_english_leaks("band-strength-band-pull-apart") == {"strength"}
    assert find_english_leaks("strength band strength band pull-apart") == {"strength"}
    assert find_english_leaks("stamina strength band pull-apart") == {"stamina", "strength"}


def test_strength_starting_a_chain_after_prose_is_name_internal():
    # Round-11: ordinary prose before strength = chain start = clean.
    assert find_english_leaks("Hoy toca strength band pull-apart") == set()
    assert find_english_leaks("haz strength-band pull-apart") == set()


def test_mid_chain_strength_flags_in_glued_and_multiword_shapes():
    # Round-12 P1: the discriminator runs on chain PARTS, not raw tokens, so
    # glued cores and multi-word lexicon prefixes can't dodge it.
    assert find_english_leaks("kipping strength-band-pull-apart") == {"strength"}
    assert find_english_leaks("weighted strength-band-pull-apart") == {"strength"}
    assert find_english_leaks("weight loss strength band pull-apart") == {"weight loss", "strength"}
    # "fat" is itself a lexicon term, so it flags alongside the phrase
    assert find_english_leaks("fat loss strength-band-pull-apart") == {"fat", "fat loss", "strength"}
    assert find_english_leaks("stamina-endurance strength band pull-apart") == {
        "stamina",
        "endurance",
        "strength",
    }


def test_lexicon_compounds_before_a_chain_also_mark_strength_mid_chain():
    # Round-13: any lexicon hit OVERLAPPING the previous token counts, not
    # only one ending exactly at the token boundary.
    assert find_english_leaks("muscle-gain strength band pull-apart") == {"muscle", "strength"}
    assert find_english_leaks("stamina-focused strength-band-pull-apart") == {"stamina", "strength"}
    assert find_english_leaks("lean-out strength band pull-apart") == {"lean", "strength"}
    assert find_english_leaks("bulking-up strength band pull-apart") == {"bulking", "strength"}
    assert find_english_leaks("muscle-building strength band pull-apart") == {"muscle", "strength"}


def test_a_name_internal_hit_does_not_mark_strength_mid_chain():
    # Round-14 P1: only hits that survive as actual leaks count as preceding
    # goal vocab — "muscle" inside "muscle-up" is name-internal, so the
    # strength chain after it starts clean.
    assert find_english_leaks("muscle-up strength band pull-apart") == set()
    # …while the same word as an actual leak still marks it mid-chain.
    assert find_english_leaks("muscle-gain strength band pull-apart") == {"muscle", "strength"}


def test_multi_word_terms_match_typographic_dashes():
    # Round-14 P2: en/em dashes are separators too.
    assert find_english_leaks("tu objetivo es weight–loss") == {"weight loss"}
    assert find_english_leaks("tu objetivo es weight—loss") == {"weight loss"}
    assert find_english_leaks("weight–loss strength band pull-apart") == {"weight loss", "strength"}


def test_dashed_prose_prefixes_block_the_strength_absorb():
    # Round-17 P2: a dash-separated prose/lexicon prefix blocks the
    # right-hand strength+equipment absorb, mirroring ASCII gluing
    # ("non-strength band pull-apart" flags today).
    assert find_english_leaks("non–strength band pull-apart") == {"strength"}
    assert find_english_leaks("super–strength band pull-apart") == {"strength"}
    assert find_english_leaks("muscle–gain strength band pull-apart") == {
        "muscle",
        "strength",
    }
    # a SPACE-separated prose prefix still means chain start (clean).
    assert find_english_leaks("non strength band pull-apart") == set()


def test_dash_before_a_glued_strength_chain_marks_it_mid_chain():
    # Round-18 P1: a name unit whose FIRST part is strength, preceded by a
    # dash separator, is blocked exactly like the bare absorb path.
    assert find_english_leaks("non–strength-band-pull-apart") == {"strength"}
    assert find_english_leaks("super–strength-band-pull-apart") == {"strength"}
    assert find_english_leaks("kipping–strength-band-pull-apart") == {"strength"}
    assert find_english_leaks("non–strength-band pull-apart") == {"strength"}
    assert find_english_leaks("stamina–strength-band-pull-apart") == {"stamina", "strength"}
    # …while the same unit at chain start stays clean.
    assert find_english_leaks("strength-band-pull-apart") == set()


def test_underscores_are_separators_too():
    # Round-18 P2: \w includes "_", so \b boundaries dodged it — the
    # normalization pass maps "_" to a space.
    assert find_english_leaks("weight_loss") == {"weight loss"}
    assert find_english_leaks("fat_loss") == {"fat", "fat loss"}
    assert find_english_leaks("muscle_pull-ups") == {"muscle"}
    assert find_english_leaks("weight_loss strength band pull-apart") == {"weight loss", "strength"}


def test_dash_gap_joins_accept_exact_cores_only():
    # Round-17 P1: prefix+core joins across a dash gap are not names -
    # only EXACT allowlisted cores may be recognized across the gap.
    for dash in ("–", "—", "―"):
        assert find_english_leaks(f"strength{dash}band-pull-apart") == {"strength"}, dash
    assert find_english_leaks("Hoy toca strength–band-pull-apart") == {"strength"}
    assert find_english_leaks("El foco es strength—band-pull-apart 3x10") == {"strength"}
    # exact cores still join across the gap; full-ASCII chains stay clean.
    assert find_english_leaks("Muscle–up") == set()
    assert find_english_leaks("strength-band-pull-apart") == set()


def test_dash_and_nbsp_name_shapes_stay_clean_like_ascii():
    # Round-15 P2 / round-16: same names, typographic spelling -- an
    # allowlisted core is recognized across a dash-derived gap.
    assert find_english_leaks("Muscle–up") == set()
    assert find_english_leaks("Muscle—up") == set()
    assert find_english_leaks("Muscle‐up") == set()
    assert find_english_leaks("strength band pull-apart") == set()


def test_en_em_dashes_never_link_a_modifier_chain():
    # Round-16: chain absorption and strength-before-equipment never cross
    # a dash-derived gap, so strength flags...
    assert find_english_leaks("strength—band pull-apart") == {"strength"}
    assert find_english_leaks("El foco es strength–band pull-apart") == {"strength"}
    # ...prose glued by dashes stays separate from names...
    assert find_english_leaks("toca—Muscle-up") == set()
    assert find_english_leaks("pull-apart—luego") == set()
    # ...and multi-word lexicon still matches across the gap.
    assert find_english_leaks("weight–loss") == {"weight loss"}


_HYPHEN_VARIANTS = ("-", "‐", "‑", "‒", "−")  # true hyphens
_SEPARATOR_DASHES = ("–", "—", "―")  # en/em/horizontal bar
_INVARIANT_BASES = (
    "strength band pull-apart",
    "Muscle-up",
    "non-strength band pull-apart",
    "kipping strength-band-pull-apart",
    "weight loss strength band pull-apart",
    "muscle-gain strength band pull-apart",
    "Hoy toca strength band pull-apart",
    "muscle pull-apart",
    "weight-loss",
    "strength band strength band pull-apart",
)

# Documented differences from ASCII when en/em dashes replace "-": NONE left
# after round-17. Dash gaps block the strength absorb and dash-connected
# prefixes count for the mid-chain cut, so dashed prose/lexicon prefixes
# flag exactly like ASCII gluing; and exact-core joins keep the clean cases
# identical. The table stays as the mechanism for future differences.
_SEPARATOR_EXPECTED_DIFFS: dict[str, set[str]] = {}


@pytest.mark.parametrize("base", _INVARIANT_BASES)
@pytest.mark.parametrize("dash", _HYPHEN_VARIANTS)
def test_hyphen_and_nbsp_invariance(base: str, dash: str):
    # True hyphens, NBSP, and underscores: swapping never changes the result.
    expected = find_english_leaks(base)
    assert find_english_leaks(base.replace("-", dash)) == expected
    assert find_english_leaks(base.replace(" ", " ")) == expected


    assert find_english_leaks(base.replace(" ", "_")) == expected


@pytest.mark.parametrize("base", _INVARIANT_BASES)
@pytest.mark.parametrize("dash", _SEPARATOR_DASHES)
def test_separator_dash_behavior(base: str, dash: str):
    # En/em/bar dashes: same as ASCII except the documented differences.
    expected = _SEPARATOR_EXPECTED_DIFFS.get(base, find_english_leaks(base))
    assert find_english_leaks(base.replace("-", dash)) == expected


def test_hypertrophy_is_a_leak():
    # Round-11 P2: the rules doc lists hipertrofia as a primary goal.
    assert (
        find_english_leaks("¿Quieres ganar fuerza, hypertrophy, perder grasa...?")
        == {"hypertrophy"}
    )


def test_the_lexicon_covers_the_rules_docs_goal_vocabulary():
    # DEFAULT_RULES_DOC names its goals in English; each must be a flaggable leak.
    for word in ("strength", "hypertrophy", "endurance"):
        assert word in ENGLISH_TRAINING_VOCAB


# --- the mandatory convergence matrix (round-11) ---
#
# Rule intent: a leak is goal vocabulary OUTSIDE a legitimate name. Ambiguity
# policy: name-internal tokens are clean; anything adjacent-but-outside flags.
# "strength" is name-internal only when it STARTS a name's modifier chain —
# preceded by nothing, prose (Spanish), or punctuation. Preceded by a
# name-modifier or by lexicon goal vocabulary, it is mid-chain vocab and
# flags. A Spanish word glued INSIDE a hyphen chain breaks the name shape,
# so the chain's strength is adjacent-outside and flags.

_PREFIXES = ("none", "spanish", "modifier", "lexicon", "multiword-lexicon")
_UNITS = ("spaced", "glued", "glued-core", "chain")
_CORES = ("pull-apart", "muscle-up", "pull-aparts")  # singular + plural
_SUFFIXES = ("none", "prose", "another-name")

_PREFIX_TEXT = {
    "spanish": "toca",
    "modifier": "kipping",
    "lexicon": "stamina",
    "multiword-lexicon": "weight loss",
}
_SUFFIX_TEXT = {"none": "", "prose": " hoy", "another-name": " y dips"}


def _matrix_text(prefix: str, unit: str, core: str, suffix: str) -> str:
    body = {
        "spaced": f"strength band {core}",
        "glued": f"strength-band {core}",
        "glued-core": f"strength-band-{core}",
        "chain": f"strength-band-{core}",
    }[unit]
    if prefix != "none":
        joiner = "-" if unit == "chain" else " "
        body = f"{_PREFIX_TEXT[prefix]}{joiner}{body}"
    return body + _SUFFIX_TEXT[suffix]


def _matrix_expected(prefix: str, unit: str, core: str) -> set[str]:
    leaks: set[str] = set()
    if prefix == "lexicon":
        leaks.add("stamina")
    if prefix == "multiword-lexicon":
        leaks.add("weight loss")
    if prefix in {"modifier", "lexicon", "multiword-lexicon"} or (
        prefix == "spanish" and unit == "chain"
    ):
        leaks.add("strength")
    if unit == "chain" and prefix != "none" and core == "muscle-up":
        # the glued chain is broken by an invalid prefix part, so the whole
        # token is not a name — its lexicon parts are adjacent-outside
        leaks.add("muscle")
    return leaks


@pytest.mark.parametrize("suffix", _SUFFIXES)
@pytest.mark.parametrize("core", _CORES)
@pytest.mark.parametrize("unit", _UNITS)
@pytest.mark.parametrize("prefix", _PREFIXES)
def test_strength_modifier_matrix(prefix: str, unit: str, core: str, suffix: str):
    text = _matrix_text(prefix, unit, core, suffix)
    assert find_english_leaks(text) == _matrix_expected(prefix, unit, core), text


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
