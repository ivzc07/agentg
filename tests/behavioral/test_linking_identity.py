"""Linking identity: the phraser's self-description is anchored, not improvised (issue #66).

The linking voice has exactly one identity — a coach who works through
partner gyms — yet two consecutive ``DEAD_END_INSTRUCTION`` calls produced
two different self-descriptions, one of them nonsense ("Soy tu enlace").
Offline, a narrow exact pin keeps the observed regression under CI; full
grammatical judgment is delegated to the live judge sweep (``pytest -m
live`` + ``AGENTG_BEHAVIORAL_LIVE=1``), which asks ``IDENTITY_QUESTION``
of every real phraser reply.
"""

from __future__ import annotations

import os

import pytest

from agentg import linking
from behavioral.linking_identity import (
    IDENTITY_QUESTION,
    build_identity_prompt,
    identity_drift,
    judge_backend_from_env,
    judge_identity,
    live_enabled,
)

# --- the issue-#66 repro: the observed drift is caught; the anchored phrasing passes ---


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


def test_the_offline_pin_is_deliberately_narrow():
    # Denials adjacent to the verb are not claims…
    assert identity_drift("No soy un bot. Soy tu entrenador.") is None
    assert identity_drift("I'm not a bot — I'm your coach.") is None
    # …and an invite link mentioned as an object is fine. Full grammatical
    # judgment beyond the exact pin belongs to the live judge, by design.
    assert identity_drift("I'm a coach and the link is below.") is None


def test_a_punctuated_no_does_not_negate_the_claim():
    # A sentence boundary between the "No" and the verb means the "No"
    # answered something else — the claim stands and must flag.
    drift = identity_drift("No. Soy tu enlace.")
    assert drift is not None and "enlace" in drift
    drift = identity_drift("No! Soy tu enlace.")
    assert drift is not None and "enlace" in drift
    # …while a "No" glued to the verb still denies.
    assert identity_drift("No soy un bot.") is None


def test_a_clause_broken_no_does_not_negate_the_claim():
    # Comma, colon, dash, or newline between them: a discourse "No", then
    # the claim — all must flag.
    for reply in (
        "No, soy tu enlace.",
        "No: soy tu enlace.",
        "No - soy tu enlace.",
        "No\nsoy tu enlace.",
    ):
        drift = identity_drift(reply)
        assert drift is not None and "enlace" in drift, reply


def test_an_ellipsis_breaks_the_denial():
    # U+2026 and its ASCII expansion both end the discourse "No".
    drift = identity_drift("No… soy tu enlace.")
    assert drift is not None and "enlace" in drift
    drift = identity_drift("No... soy tu enlace.")
    assert drift is not None and "enlace" in drift


def test_spanish_denial_markers_beyond_no():
    # tampoco and ni deny like "no" when glued to the verb.
    assert identity_drift("Tampoco soy un bot") is None
    assert identity_drift("Yo tampoco soy un enlace") is None
    assert identity_drift("Ni soy un bot") is None
    assert identity_drift("Ni soy un bot, soy un entrenador.") is None
    drift = identity_drift("Soy un bot")
    assert drift is not None and "bot" in drift


def test_a_fronted_no_never_negates_an_english_claim():
    # English negation is post-verbal ("I am not"); a bare fronted "No"
    # with no punctuation is a discourse marker, not a denial.
    drift = identity_drift("No I am your link.")
    assert drift is not None and "link" in drift
    drift = identity_drift("No I am a bot.")
    assert drift is not None and "bot" in drift
    assert identity_drift("I am not a bot.") is None


def test_the_identity_question_covers_the_gendered_role():
    # The live rubric must name every role the offline pin bans.
    assert "asistenta" in IDENTITY_QUESTION
    assert "asistente" in IDENTITY_QUESTION


# --- the prompt pins the one identity ---


def test_the_phraser_prompt_anchors_a_single_coach_identity():
    text = linking._PHRASER_PROMPT.lower()
    # The one allowed self-description…
    assert "coach" in text and "partner gyms" in text
    # …and a ban on re-improvising it per call.
    assert "never describe yourself" in text


# --- the judge wiring (offline, injected backend) ---


def test_the_identity_prompt_asks_the_issue_question():
    system, user = build_identity_prompt("Soy tu enlace.")
    assert IDENTITY_QUESTION in system
    assert "invite link as an object" in system  # object mentions are fine
    assert "evidence" in system.lower() and "JSON" in system
    assert "Soy tu enlace." in user


class _FixedBackend:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self.text


async def test_judge_identity_fails_a_drifted_reply():
    backend = _FixedBackend('{"identity": {"evidence": "claims to be a link", "score": 1}}')
    passed, evidence = await judge_identity(backend, "Soy tu enlace.")
    assert backend.calls == 1
    assert passed is False and "link" in evidence


async def test_judge_identity_passes_an_anchored_reply():
    backend = _FixedBackend('{"identity": {"evidence": "a coach", "score": 5}}')
    passed, _ = await judge_identity(backend, "Soy un entrenador.")
    assert passed is True


# --- the live judge backend is built from the key it will actually use ---


def test_the_phrasers_key_alone_does_not_light_up_the_judge(monkeypatch):
    # The default judge model is anthropic; with only the phraser's
    # MODEL_API_KEY set, the backend must NOT be built (it would fail at
    # call time instead of skipping).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENTG_JUDGE_MODEL", raising=False)
    monkeypatch.setenv("MODEL_API_KEY", "agent-key")
    assert judge_backend_from_env() is None


def test_the_default_judge_model_uses_the_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "judge-key")
    monkeypatch.delenv("AGENTG_JUDGE_MODEL", raising=False)
    backend = judge_backend_from_env()
    assert backend is not None
    assert backend.api_key == "judge-key"
    assert backend.model.startswith("anthropic/")


def test_a_judge_model_override_uses_the_matching_key(monkeypatch):
    monkeypatch.setenv("AGENTG_JUDGE_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("MODEL_API_KEY", "agent-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    backend = judge_backend_from_env()
    assert backend is not None and backend.api_key == "agent-key"


# --- the live phraser sweep (opt-in: needs network + both model keys) ---


@pytest.mark.live
async def test_live_phraser_holds_one_identity_across_calls_and_instructions():
    """Opt-in live call — skipped unless marker selected and env enabled."""
    if not live_enabled():
        pytest.skip("set AGENTG_BEHAVIORAL_LIVE=1 to run the live phraser sweep")
    if not os.environ.get("MODEL_API_KEY"):
        pytest.skip("MODEL_API_KEY not configured (the phraser's model)")
    backend = judge_backend_from_env()
    if backend is None:
        pytest.skip("judge API key not configured for the judge model")

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
        # The near-miss invite-code path lacks the built-in coach-identity
        # wording — the likeliest drift path.
        (linking.CODE_NOT_FOUND_INSTRUCTION, "XM7K29"),
        # The expired-code path from _confirm_name.
        (linking.LINK_EXPIRED_INSTRUCTION, "yes"),
        # The happy-path name ask also carries no coach-identity wording.
        (linking.NAME_ASK_INSTRUCTION.format(gym="Iron Temple"), "no"),
        # The coach paths: welcoming a new coach and re-assuring an
        # existing one.
        (
            linking.COACH_WELCOME_INSTRUCTION.format(name="Ana", gym="Iron Temple"),
            "yes",
        ),
        (
            linking.ALREADY_COACH_INSTRUCTION.format(name="Ana", gym="Iron Temple"),
            "/start x",
        ),
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
        passed, evidence = await judge_identity(backend, reply)
        assert passed, f"drifted identity: {reply!r} — {evidence}"
