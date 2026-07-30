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
