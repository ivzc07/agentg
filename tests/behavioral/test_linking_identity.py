"""Linking identity: the phraser's self-description is anchored, not improvised (issue #66).

The linking voice has exactly one identity — a coach who works through
partner gyms — yet two consecutive ``DEAD_END_INSTRUCTION`` calls produced
two different self-descriptions, one of them nonsense ("Soy tu enlace").
Offline, the deterministic checker must flag that drift and accept the
anchored phrasing; the prompt itself is pinned to the single identity. The
live sweep (``pytest -m live`` + ``AGENTG_BEHAVIORAL_LIVE=1``) runs the
real phraser across the linking instructions and applies the same check.
"""

from __future__ import annotations

import os

import pytest

from agentg import linking
from behavioral.linking_identity import identity_drift, live_enabled

# --- the observed drift is caught; the anchored phrasing passes ---


def test_the_observed_drifted_identity_is_flagged():
    reply = (
        "¡Hola! Soy tu enlace, solo trabajo con gimnasios asociados. "
        "Pídele el código de invitación a tu gimnasio."
    )
    drift = identity_drift(reply)
    assert drift is not None and "enlace" in drift


def test_an_english_drifted_identity_is_flagged():
    reply = "Hi! I'm your link — I only work with partner gyms."
    drift = identity_drift(reply)
    assert drift is not None and "link" in drift


def test_the_anchored_coach_identity_passes():
    reply = (
        "¡Hola! 👋 Soy un entrenador que solo trabaja a través de gimnasios "
        "asociados. Pide en recepción el enlace de invitación de tu gimnasio "
        "y tócalo para empezar."
    )
    assert identity_drift(reply) is None


def test_the_anchored_english_coach_identity_passes():
    reply = (
        "Hi! I'm a coach who only works through partner gyms. Ask your gym's "
        "front desk for the invite link and tap it to get started."
    )
    assert identity_drift(reply) is None


def test_a_reply_with_no_self_description_passes():
    reply = (
        "¡Hola! Para empezar, pide el enlace de invitación de tu gimnasio en "
        "recepción y tócalo."
    )
    assert identity_drift(reply) is None


def test_a_later_sentence_drifting_is_flagged():
    reply = (
        "¡Hola! 👋 Solo trabajo con gimnasios asociados. Soy tu asistente "
        "para empezar — pide el enlace de invitación a tu gimnasio."
    )
    drift = identity_drift(reply)
    assert drift is not None and "asistente" in drift


# --- the anchor must name the claimed role, not appear anywhere later ---


def test_an_anchor_appearing_later_does_not_excuse_a_drifted_role():
    # "coaches" contains "coach", and the word even appears in the reply —
    # but the claimed role is "link", so this is drift.
    reply = "I'm a link for partner gym coaches. Ask your gym for its invite."
    drift = identity_drift(reply)
    assert drift is not None and "link" in drift


def test_an_adjective_before_the_role_still_passes():
    reply = "I'm your personal trainer — ask your gym for the invite link."
    assert identity_drift(reply) is None


# --- Unicode apostrophes are still claims ---


def test_a_curly_apostrophe_claim_is_detected():
    reply = "Hi! I’m your link — I only work with partner gyms."
    drift = identity_drift(reply)
    assert drift is not None and "link" in drift


# --- only actual role claims are judged ---


def test_a_denial_then_the_coach_identity_passes():
    reply = "No soy un bot. Soy tu entrenador — pide el enlace de invitación a tu gimnasio."
    assert identity_drift(reply) is None


def test_a_denied_coach_role_is_not_a_claim():
    reply = "I'm not a coach yet — get your gym's invite link first."
    assert identity_drift(reply) is None


def test_non_role_first_person_passes():
    assert identity_drift("I'm at the gym already, send the invite link when you have it.") is None
    assert identity_drift("Soy nuevo por aquí, ¿cómo empiezo?") is None


# --- the claim is parsed as verb + noun phrase; the head noun is judged ---


def test_a_punctuated_no_does_not_negate_the_next_clause():
    # "No" closed by punctuation answers something else; the claim stands.
    for reply in ("No, soy tu enlace.", "No. Soy tu enlace."):
        drift = identity_drift(reply)
        assert drift is not None and "enlace" in drift


def test_a_compound_role_carrying_a_drift_word_is_flagged():
    drift = identity_drift("I'm a link coach.")
    assert drift is not None and "link" in drift
    drift = identity_drift("Soy un enlace entrenador.")
    assert drift is not None and "enlace" in drift


def test_modifiers_between_the_determiner_and_the_head_still_reach_it():
    assert identity_drift("I'm a partner gym coach.") is None
    assert identity_drift("I'm a certified personal trainer.") is None
    # Spanish adjectives trail the head noun.
    assert identity_drift("Soy un entrenador personal.") is None


# --- the guard only catches forbidden identities; everything else passes ---


def test_a_link_beyond_the_claim_window_does_not_flag():
    # "link" belongs to the ask, not to the self-description.
    assert identity_drift("I'm a coach: ask for the invite link.") is None
    assert identity_drift("Soy un entrenador: pide el enlace de invitación.") is None


def test_a_bare_no_does_not_negate_an_english_claim():
    # English negation is "I'm not"; a fronted "No" answers something else.
    drift = identity_drift("No I'm your link")
    assert drift is not None and "link" in drift


def test_coach_claims_need_no_particular_phrasing():
    # No coach-anchor validation: any non-forbidden phrasing passes.
    assert identity_drift("I'm your coach today.") is None
    assert identity_drift("Soy un gran entrenador.") is None
    assert identity_drift("I'm a strength and conditioning coach.") is None


# --- one coherent rule: clause segments, first role token, compound heads ---


def test_a_no_closed_by_any_clause_boundary_negates_nothing():
    for reply in ("No: soy tu enlace.", "No - soy tu enlace.", "No\nsoy tu enlace."):
        drift = identity_drift(reply)
        assert drift is not None and "enlace" in drift


def test_a_hyphenated_compound_is_judged_by_its_head():
    drift = identity_drift("I'm a coach-bot.")
    assert drift is not None and "coach-bot" in drift
    drift = identity_drift("I'm the ai-assistant.")
    assert drift is not None and "ai-assistant" in drift


def test_not_anywhere_before_the_role_is_a_denial():
    assert identity_drift("I'm really not a bot.") is None
    assert identity_drift("I'm honestly not an assistant.") is None


def test_the_whole_clause_is_scanned_not_a_fixed_window():
    drift = identity_drift("I'm your very own personal link to partner gyms.")
    assert drift is not None and "link" in drift
    drift = identity_drift("Soy tu único y verdadero enlace con el gimnasio.")
    assert drift is not None and "enlace" in drift


def test_a_coach_role_coming_first_makes_the_clause_clean():
    assert identity_drift("Soy el coach del enlace de invitación.") is None


# --- a claim is a determiner-led noun phrase, not any role word in the clause ---


def test_bare_object_mentions_are_not_claims():
    assert identity_drift("I'm waiting for the invite link.") is None
    assert identity_drift("I'm here to help with the invite link from your gym.") is None
    assert identity_drift("Soy quien te ayuda con el enlace de invitación.") is None
    assert identity_drift("Soy nuevo y aquí está el enlace de invitación.") is None


def test_no_as_determiner_denies_the_phrase():
    assert identity_drift("I'm no bot.") is None
    assert identity_drift("I'm no assistant.") is None
    assert identity_drift("I am no link.") is None


def test_a_denied_phrase_does_not_hide_a_following_affirmative_one():
    drift = identity_drift("I'm not a coach but a bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("I'm not a coach but a link")
    assert drift is not None and "link" in drift


def test_a_coach_role_compounded_with_a_drift_head_is_drift():
    drift = identity_drift("I'm a coach bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("I'm the coach assistant")
    assert drift is not None and "assistant" in drift
    drift = identity_drift("Soy el coach bot")
    assert drift is not None and "bot" in drift


def test_not_only_affirms_instead_of_denying():
    drift = identity_drift("I'm not only a bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("I am not just an assistant")
    assert drift is not None and "assistant" in drift
    drift = identity_drift("I'm not simply a link")
    assert drift is not None and "link" in drift


# --- conjunctions end the phrase; only a contrast opens one more ---


def test_a_coach_claim_followed_by_the_invite_ask_stays_clean():
    assert identity_drift("I'm a coach and here is the link") is None
    assert identity_drift("I'm a coach and the link is below") is None
    assert identity_drift("Soy un entrenador y el enlace de invitación está en recepción") is None


def test_a_denied_phrase_does_not_reopen_on_arbitrary_later_noun_phrases():
    assert identity_drift("I'm not a coach who sends the link") is None
    assert identity_drift("I'm not a bot so ask for the invite link") is None
    assert identity_drift("I'm not a coach for the link") is None


def test_a_contrast_or_correlative_opens_exactly_one_more_phrase():
    drift = identity_drift("I'm not only a coach but a bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("I'm not just a coach but a link")
    assert drift is not None and "link" in drift
    drift = identity_drift("I'm a coach but a bot")
    assert drift is not None and "bot" in drift


def test_a_corrective_without_but_still_claims():
    drift = identity_drift("I'm not a bot just a link")
    assert drift is not None and "link" in drift


def test_fillers_do_not_hide_a_drift_claim():
    for reply in (
        "I'm definitely a bot",
        "I'm basically a bot",
        "I'm currently a bot",
        "Soy realmente un bot",
        "Soy yo el enlace",
        "I'm here as a bot",
        "I'm here as your link",
        "I'm such a bot",
    ):
        assert identity_drift(reply) is not None, reply


# --- one phrase machine: conjunctions reopen like contrasts ---


def test_a_conjunction_before_a_role_np_reopens_one_more_phrase():
    for reply in (
        "I'm a coach and a bot",
        "I'm a trainer or an assistant",
        "Soy un entrenador y un bot",
        "I'm a coach and also a bot",
        "I'm a coach and definitely a bot",
        "I'm a coach and as a bot",
        "Soy un entrenador y también un bot",
    ):
        assert identity_drift(reply) is not None, reply
    # but not when the right side is a full clause, not a role NP
    assert identity_drift("I'm a coach and here is the link") is None


def test_a_spanish_fronted_no_denies_the_phrase_not_the_segment():
    drift = identity_drift("No soy un entrenador pero un bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("No soy un bot just a link")
    assert drift is not None and "link" in drift
    # a denial with no reopen stays clean
    assert identity_drift("No soy tu enlace.") is None


def test_only_and_just_are_pre_head_modifiers_inside_the_phrase():
    drift = identity_drift("I'm the only bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("I'm your only link")
    assert drift is not None and "link" in drift
    # and they reopen one more phrase after an affirmative one
    drift = identity_drift("I'm a coach just a bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("I'm a coach only a link")
    assert drift is not None and "link" in drift


def test_a_mid_phrase_not_opens_a_denied_phrase():
    assert identity_drift("I'm a coach not a bot") is None
    assert identity_drift("I'm your coach not your link") is None
    assert identity_drift("Soy un coach no un bot") is None


# --- the reopen path shares the primary judging ---


def test_the_reopen_path_keeps_the_not_only_affirmer_exception():
    drift = identity_drift("I'm a coach but not only a bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("I'm a coach but not just a link")
    assert drift is not None and "link" in drift
    drift = identity_drift("I'm a coach but not simply a bot")
    assert drift is not None and "bot" in drift


def test_spanish_no_solo_affirms_like_not_only():
    drift = identity_drift("Soy un coach no solo un bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("Soy un entrenador y no solo un bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("No soy solo un bot")
    assert drift is not None and "bot" in drift
    assert identity_drift("Soy solo un bot") is not None
    assert identity_drift("No soy un bot") is None


def test_a_reopened_np_is_judged_like_a_primary_one():
    for reply in (
        "I'm not a coach but a link to the gym",
        "I'm not a coach but a bot who helps",
        "I'm a coach and a bot who helps",
        "No soy un entrenador pero un bot del gimnasio",
        "I'm a coach but a stupid bot",
        "I'm a coach and a helpful assistant",
    ):
        assert identity_drift(reply) is not None, reply
    # a full clause on the right is still no claim
    assert identity_drift("I'm a coach and here is the link") is None


# --- the clause gate is structural, not a word list ---


def test_a_full_clause_on_the_right_is_clean_regardless_of_the_verb():
    for reply in (
        "Soy un entrenador y el enlace de invitación esta en recepción",  # no accent
        "Soy un entrenador y el enlace estan en recepción",
        "Soy un entrenador y el enlace te espera en recepción",
        "I'm a coach and the link will arrive from your gym",
        "I'm a coach and the link was below",
    ):
        assert identity_drift(reply) is None, reply


def test_a_relative_clause_after_the_role_keeps_the_drift():
    drift = identity_drift("I'm a coach and a bot that is helpful")
    assert drift is not None and "bot" in drift
    drift = identity_drift("I'm not a coach but a bot that is ready")
    assert drift is not None and "bot" in drift


def test_the_fronted_no_affirmer_check_survives_fillers():
    drift = identity_drift("No soy yo solo un bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("No soy realmente solo un bot")
    assert drift is not None and "bot" in drift


# --- score on role close; conjunction clause joins; correlatives ---


def test_a_conjunction_before_a_non_np_right_side_is_a_clause_join():
    assert identity_drift("I'm a coach and here's the invite link") is None
    assert identity_drift("I'm a coach and there's the link") is None
    assert identity_drift("I'm a coach and get the invite link") is None
    assert identity_drift("Soy un entrenador y pide el enlace") is None


def test_a_closed_drift_head_is_scored_whatever_follows():
    for reply in (
        "I'm a coach and a bot and here is the invite",
        "I'm a coach and a bot and a link",
        "I'm not a coach but a bot and here is the link",
        "Soy un entrenador y un bot y aquí está el enlace",
        "Soy un entrenador y un asistente personal",
        "Soy un entrenador y un bot útil",
        "I'm a coach and a bot too",
        "I'm a coach and a bot today",
        "I'm a coach and a link to the partner gym",
        "I'm a coach and a bot for new members",
    ):
        assert identity_drift(reply) is not None, reply


def test_sino_reopens_like_pero_and_rather_is_a_reopen_filler():
    drift = identity_drift("No soy un entrenador sino un bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("No soy un bot sino tu enlace")
    assert drift is not None and "enlace" in drift
    drift = identity_drift("I'm not a coach but rather a bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("I'm a coach but instead a bot")
    assert drift is not None and "bot" in drift


def test_rather_than_ends_the_claimed_np():
    assert identity_drift("I'm a coach rather than a bot") is None


# --- negation scopes, role-as-head, comma-sino ---


def test_a_denial_scopes_over_a_following_coordination():
    assert identity_drift("I'm not a bot or a link") is None
    assert identity_drift("I'm not a bot and a link") is None
    assert identity_drift("No soy un bot o un enlace") is None
    assert identity_drift("I'm a coach not a bot or a link") is None
    # but a contrast still reopens after a denial
    drift = identity_drift("I'm not a bot but a link")
    assert drift is not None and "link" in drift


def test_the_role_must_head_its_own_noun_phrase():
    assert identity_drift("I'm your coach and the front desk has your invite link") is None
    assert identity_drift("Soy un entrenador y el gimnasio tiene el enlace de invitación") is None
    assert identity_drift("I'm a coach and the invite link awaits") is None
    assert identity_drift("I'm a coach and the link is below") is None
    drift = identity_drift("I'm a coach and a bot")
    assert drift is not None and "bot" in drift


def test_a_comma_sino_after_a_fronted_no_is_a_corrective_claim():
    drift = identity_drift("No soy un enlace, sino un bot de soporte.")
    assert drift is not None and "bot" in drift
    drift = identity_drift("No soy solo un entrenador, sino un bot.")
    assert drift is not None and "bot" in drift


# --- the opener type judges the reopened right side ---


def test_an_indefinite_reopened_np_is_scored_whatever_pp_follows():
    for reply in (
        "I'm a coach and a link from your gym",
        "I'm not a coach but a bot in training",
        "I'm a coach and a bot on duty",
        "I'm a coach and a bot by design",
        "I'm a coach and a link without context",
        "I'm a coach and a bot about fitness",
        "Soy un entrenador y un bot por defecto",
        "Soy un entrenador y un bot sin contexto",
        "Soy un entrenador y un enlace desde hoy",
    ):
        assert identity_drift(reply) is not None, reply


def test_a_denied_coordination_is_absorbed_until_a_contrast():
    for reply in (
        "I'm not a coach or a trainer but a bot",
        "I'm not a bot or a link but an assistant",
        "No soy un entrenador o un coach sino un bot",
        "I'm a coach not a bot or a link but an assistant",
    ):
        assert identity_drift(reply) is not None, reply
    # with no contrastive reopener, the denial stands
    assert identity_drift("I'm not a bot or a link") is None
    assert identity_drift("No soy un bot ni un enlace") is None


def test_a_definite_reopened_np_is_an_object_reference():
    assert identity_drift("I'm a coach and the link to the gym is below") is None
    assert identity_drift("I'm a coach and the invite link to the gym awaits") is None
    assert identity_drift("I'm a coach and the link for your gym is below") is None
    drift = identity_drift("I'm a coach and a bot")
    assert drift is not None and "bot" in drift


# --- the reopened-NP decision table is complete ---


def test_a_reopened_np_that_is_a_clause_subject_is_clean():
    assert identity_drift("I'm a coach and your invite link is below") is None
    assert identity_drift("Soy un entrenador y tu enlace de invitación está abajo") is None
    assert identity_drift("I'm a coach and a link will be sent") is None


def test_demonstratives_are_claim_openers():
    for reply in (
        "I'm this bot",
        "I'm that link",
        "Soy este enlace",
        "I'm a coach and that bot",
        "I'm a coach and this bot",
    ):
        assert identity_drift(reply) is not None, reply
    # "that" stays a relativizer after a collected head
    drift = identity_drift("I'm a coach and a bot that is helpful")
    assert drift is not None and "bot" in drift


def test_ni_coordinates_under_denial_until_a_sino():
    drift = identity_drift("No soy un entrenador ni un coach sino un bot")
    assert drift is not None and "bot" in drift
    drift = identity_drift("No soy un bot ni un enlace sino un asistente")
    assert drift is not None and "asistente" in drift
    assert identity_drift("No soy un bot ni un enlace") is None


# --- PP + predicate, multi-word closers, the residue policy ---


def test_a_pp_followed_by_a_predicate_is_a_clause():
    assert identity_drift("I'm a coach and a link from your gym is below") is None
    assert identity_drift("I'm a coach and your invite link from reception is ready") is None
    # a PP with no predicate still modifies the claimed role
    drift = identity_drift("I'm a coach and a link from your gym")
    assert drift is not None and "link" in drift


def test_multi_word_closers_still_score_the_drift_head():
    for reply in (
        "I'm a coach and a bot as well",
        "I'm a coach and a bot as always",
        "I'm a coach and a bot rather than a trainer",
        "I'm a coach and a bot instead of a trainer",
    ):
        assert identity_drift(reply) is not None, reply


def test_post_head_residue_is_classified_by_policy():
    # one unknown token reads as a postnominal adjective -> drift
    drift = identity_drift("Soy un entrenador y un bot útil")
    assert drift is not None and "bot" in drift
    # a degree word leads degree+adjective -> drift
    drift = identity_drift("Soy un entrenador y un bot muy útil")
    assert drift is not None and "bot" in drift
    # a known Spanish finite verb (or any longer unknown residue) is a
    # clause predicate -> clean. POLICY: adjective vs verb is undecidable
    # from tokens; unknown residue defaults clean, precision over recall.
    assert identity_drift("Soy un entrenador y un enlace llega") is None
    assert identity_drift("Soy un entrenador y un enlace llega mañana") is None


# --- false-positive precision fixes: tampoco, unaccented copulas, just/only ---


def test_tampoco_is_a_denial_marker():
    assert identity_drift("Tampoco soy un bot") is None
    assert identity_drift("Yo tampoco soy un enlace") is None


def test_unaccented_copulas_are_predicates_not_demonstratives():
    assert identity_drift("Soy un entrenador y tu enlace esta abajo") is None
    assert identity_drift("Soy un entrenador y tu enlace estan abajo") is None
    # the accented form was already a predicate
    assert identity_drift("Soy un entrenador y tu enlace está abajo") is None


def test_post_head_just_and_only_are_preverbal_adverbs_without_an_np():
    assert identity_drift("I am a coach and your invite link just arrived") is None
    assert identity_drift("I'm a coach and a link just arrived") is None
    assert identity_drift("I'm a coach and your invite link only works after signup") is None
    # directly followed by a determiner-led NP they stay corrective markers
    drift = identity_drift("I am not a bot just a link")
    assert drift is not None and "link" in drift


# --- the combinatorial matrix: the grammar space, enumerated ---

_EN = {
    "v": "I'm",
    "coach": "coach",
    "drift_roles": ["bot", "link", "assistant"],
    "det": "a",
    "def": "the",
    "demo": "that",
    "verb_tail": "is below",
    "poss": "your",
    "adj": "stupid",
    "rel": "who helps",
    "pp": "for the gym",
    "pp_list": [
        "from your gym", "in training", "on duty", "by design", "at heart",
        "about fitness", "via software", "without context", "under pressure",
        "for the gym", "to members", "with attitude",
    ],
    "adjunct": "today",
    "residues": [
        ("cool", True),  # lone postnominal adjective closes the claim
        ("really cool", True),
        ("quite cool", True),
        ("as well", True),  # multi-word closers
        ("as always", True),
        ("is below", False),  # a verb tail makes the NP a clause subject
        ("was below", False),
        ("will be sent", False),
        ("from your gym", True),  # a PP with no predicate modifies the role
        ("to the partner gym", True),
        ("for new members", True),
        ("from your gym is below", False),  # PP + predicate: a clause
        ("from reception is ready", False),
        ("just arrived", False),  # pre-verbal just/only: a clause follows
        ("only works after signup", False),
    ],
    "conj": ["and", "or"],
    "contr": ["but"],
    "invite_join": "and here's the invite link",
    "clause_obj": "and the front desk has your invite link",
}
_ES = {
    "v": "Soy",
    "coach": "entrenador",
    "drift_roles": ["bot", "enlace", "asistente"],
    "det": "un",
    "def": "el",
    "demo": "este",
    "verb_tail": "está abajo",
    "poss": "tu",
    "adj": "gran",
    "rel": "que ayuda",
    "pp": "para el gimnasio",
    "pp_list": [
        "por defecto", "sin contexto", "sobre fitness", "desde hoy",
        "al servicio", "de apoyo", "con actitud", "para el gimnasio",
    ],
    "adjunct": "hoy",
    "residues": [
        ("útil", True),  # lone postnominal adjective closes the claim
        ("personal", True),
        ("muy útil", True),  # degree + adjective
        ("llega", False),  # a known Spanish finite verb: a clause predicate
        ("llega mañana", False),
        ("está aquí", False),
        ("de tu gimnasio", True),  # a PP with no predicate modifies the role
        ("del gimnasio", True),
        ("de tu gimnasio está abajo", False),  # PP + predicate: a clause
        ("esta abajo", False),  # unaccented copulas are predicates too
        ("estan abajo", False),
    ],
    "conj": ["y", "o"],
    "contr": ["pero", "sino"],
    "invite_join": "y pide el enlace",
    "clause_obj": "y el gimnasio tiene el enlace",
}


def _matrix_cases() -> list[tuple[str, bool]]:
    """Every combination labeled by RULE INTENT: drift iff the phraser
    identifies ITSELF as a forbidden role. Genuinely ambiguous shapes are
    labeled clean (false negatives are acceptable; false positives break
    the live sweep)."""
    cases: list[tuple[str, bool]] = []
    for lang in (_EN, _ES):
        v, coach, det, poss = lang["v"], lang["coach"], lang["det"], lang["poss"]
        roles = [(coach, False)] + [(r, True) for r in lang["drift_roles"]]
        en = lang is _EN
        deny = (lambda s: f"{v} not {s}") if en else (lambda s: f"No soy {s}")
        for role, drift in roles:
            np = f"{det} {role}"
            cases += [
                (f"{v} {np}", drift),
                (f"{v} {poss} {role}", drift),
                (f"{v} {det} {lang['adj']} {role}", drift),
                (f"{v} {np} {lang['pp']}", drift),
                (f"{v} {np} {lang['rel']}", drift),
                (f"{v} {np} {lang['adjunct']}", drift),
                (deny(np), False),  # a denial claims nothing
            ]
            # affirmers claim again
            cases.append(
                (f"{v} not only {np}" if en else f"No soy solo {np}", drift)
            )
            for conj in lang["conj"]:
                cases += [
                    (f"{v} {det} {coach} {conj} {np}", drift),
                    # negation scopes over coordination (round 11)
                    (deny(f"{det} {coach} {conj} {np}"), False),
                    # a closed head is scored whatever follows
                    (
                        f"{v} {det} {coach} {conj} {np} {lang['conj'][0]} "
                        f"{'here is the invite' if en else 'aquí está el enlace'}",
                        drift,
                    ),
                ]
            for contr in lang["contr"]:
                cases += [
                    (f"{v} {det} {coach} {contr} {np}", drift),
                    (deny(f"{det} {coach} {contr} {np}"), drift),
                    # a comma ends the clause — no cross-clause contrast
                    # (except sino after a fronted No, covered below)
                    (f"{v} {det} {coach}, {contr} {np}", False),
                ]
            # a drift role as object of the following clause is no claim
            cases.append((f"{v} {det} {coach} {lang['invite_join']}", False))
            cases.append((f"{v} {det} {coach} {lang['clause_obj']}", False))
        # reopened right side: the OPENER TYPE judges — indefinite or
        # possessive claims score whatever PP follows; definite is an
        # object reference (round 12)
        for prep in lang["pp_list"]:
            for role2, drift2 in roles:
                cases.append(
                    (f"{v} {det} {coach} {lang['conj'][0]} {det} {role2} {prep}", drift2)
                )
        for role2, drift2 in roles:
            cases += [
                (f"{v} {det} {coach} {lang['conj'][0]} {poss} {role2}", drift2),
                (f"{v} {det} {coach} {lang['conj'][0]} {lang['def']} {role2}", False),
            ]
        # denial + coordination + contrast chains (round 12)
        for role2, drift2 in roles:
            np2 = f"{det} {role2}"
            cases.append(
                (
                    deny(f"{det} {coach} {lang['conj'][0]} {det} {coach}")
                    + f" {lang['contr'][0]} {np2}",
                    drift2,
                )
            )
            cases.append(
                (
                    deny(f"{det} {role2} {lang['conj'][0]} {np2}")
                    + f" {lang['contr'][0]} {np2}",
                    drift2,
                )
            )
        # opener x post-head class (round 13): none / relativizer / every
        # preposition / verb-ish tail, for each opener type
        opener_cases = [
            (det, "indefinite"), (poss, "possessive"),
            (lang["demo"], "demonstrative"), (lang["def"], "definite"),
        ]
        tails = (
            [("", True), (lang["rel"], True)]
            + [(p, True) for p in lang["pp_list"]]
            + [(lang["verb_tail"], False)]
        )
        for opener, kind in opener_cases:
            for tail, tail_drifts in tails:
                for role2, drift2 in roles:
                    expected = drift2 and (False if kind == "definite" else tail_drifts)
                    np2 = f"{opener} {role2}"
                    cases.append(
                        (
                            f"{v} {det} {coach} {lang['conj'][0]} "
                            f"{np2}{' ' + tail if tail else ''}",
                            expected,
                        )
                    )
        # head x residue-class and PP x predicate crossings (round 14):
        # postnominal adjective and degree+adj close the claim; a verb
        # tail — after the head or after a PP — makes it a clause
        for role2, drift2 in roles:
            np2 = f"{det} {role2}"
            for residue, residue_drifts in lang["residues"]:
                cases.append(
                    (
                        f"{v} {det} {coach} {lang['conj'][0]} {np2} {residue}",
                        drift2 and residue_drifts,
                    )
                )
        # correctives and correlatives (English shapes)
        if en:
            for role, drift in roles:
                np = f"a {role}"
                cases += [
                    (f"{v} not a {coach} just {np}", drift),
                    (f"{v} not a {coach} only {np}", drift),
                    (f"{v} a {coach} just {np}", drift),
                    (f"{v} a {coach} rather than {np}", False),
                    (f"{v} not a {coach} but rather {np}", drift),
                    (f"{v} a {coach} but instead {np}", drift),
                    (f"{v} not only a {coach} but {np}", drift),
                    (f"{v} no {np}", False),
                ]
        else:
            for role, drift in roles:
                np = f"un {role}"
                cases += [
                    # comma + sino after a fronted No: the corrective claim
                    (f"No soy un {coach}, sino {np}", drift),
                    (f"No soy {np}, sino un {coach}", False),
                    (f"No soy solo un {coach}, sino {np}", drift),
                    # "ni" coordinates under denial until a sino (round 13)
                    (f"No soy un {coach} ni {np} sino {np}", drift),
                    (f"No soy {np} ni {np}", False),
                    # "tampoco" is a denial marker like "no" (round 15)
                    (f"Tampoco soy {np}", False),
                    (f"Yo tampoco soy {np}", False),
                ]
    return cases


@pytest.mark.parametrize(
    "sentence,expected",
    _matrix_cases(),
    ids=[s for s, _ in _matrix_cases()],
)
def test_the_identity_grammar_matrix(sentence, expected):
    assert (identity_drift(sentence) is not None) is expected, sentence


# --- the prompt pins the one identity ---


def test_the_phraser_prompt_anchors_a_single_coach_identity():
    text = linking._PHRASER_PROMPT.lower()
    # The one allowed self-description…
    assert "coach" in text and "partner gyms" in text
    # …and a ban on re-improvising it per call.
    assert "never describe yourself" in text


# --- the live phraser sweep (opt-in: needs network + the agent's model key) ---


@pytest.mark.live
async def test_live_phraser_holds_one_identity_across_calls_and_instructions():
    """Opt-in live call — skipped unless marker selected and env enabled."""
    if not live_enabled():
        pytest.skip("set AGENTG_BEHAVIORAL_LIVE=1 to run the live phraser sweep")
    if not os.environ.get("MODEL_API_KEY"):
        pytest.skip("MODEL_API_KEY not configured")

    from agentg.config import DEFAULT_MODEL, Settings

    settings = Settings(
        telegram_bot_token="",  # the phraser never touches Telegram
        model=os.environ.get("MODEL") or DEFAULT_MODEL,
        model_api_key=os.environ["MODEL_API_KEY"],
        database_url="",
    )
    phraser = linking.build_phraser(settings)

    cases = [
        # Two consecutive dead-end calls: the drift this issue observed.
        (linking.DEAD_END_INSTRUCTION, "hola"),
        (linking.DEAD_END_INSTRUCTION, "hola"),
        # The fix must hold for the other linking instructions too.
        (
            linking.NAME_CONFIRM_INSTRUCTION.format(gym="Iron Temple", name="Ana García"),
            "/start x",
        ),
        (linking.WELCOME_INSTRUCTION.format(name="Ana", gym="Iron Temple"), "yes"),
        (
            linking.SWITCH_CONFIRM_INSTRUCTION.format(
                new_gym="Steel Yard", old_gym="Iron Temple"
            ),
            "/start x",
        ),
    ]
    for instruction, member_text in cases:
        reply = await phraser(instruction, member_text)
        assert identity_drift(reply) is None, reply
