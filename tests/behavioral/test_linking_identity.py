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
