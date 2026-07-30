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
