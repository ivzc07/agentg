"""Language rule & AI-disclosure (ADR 0002).

The Agent mirrors the Member's language, defaulting to Spanish; onboarding and
check-in templates — the surfaces with no LLM turn to mirror — are Spanish; and
the Agent never announces it is an AI (deflects if asked) while keeping the
behavioral safety floor.
"""

from agentg import checkin, onboarding
from agentg.agent import INSTRUCTIONS
from agentg.routines import DEFAULT_RULES_DOC


# --- the Agent mirrors the Member, defaulting to Spanish ---


def test_the_instructions_pin_the_mirror_and_spanish_default():
    text = INSTRUCTIONS.lower()
    assert "language" in text
    assert "mirror" in text
    assert "spanish" in text  # the default when there is no signal yet


def test_the_instructions_keep_lift_shorthand_language_neutral():
    # a terse log line must not flip the conversation's language
    assert "bench 60 8,8,8" in INSTRUCTIONS  # the worked example lives in the rule


# --- disclosure: never announce being an AI; deflect if asked ---


def test_the_agent_has_no_spoken_ai_disclaimer():
    text = INSTRUCTIONS.lower()
    assert "i'm an ai coach" not in text
    assert "not a medical professional" not in text  # the spoken line is gone


def test_the_agent_deflects_when_asked_if_it_is_an_ai():
    text = INSTRUCTIONS.lower()
    assert "deflect" in text
    assert "bot" in text or "real person" in text


def test_the_default_rules_doc_drops_the_disclaimer_section():
    doc = DEFAULT_RULES_DOC.lower()
    assert "## disclaimer" not in doc
    assert "i'm an ai coach" not in doc


# --- onboarding copy is Spanish ---


def test_onboarding_copy_is_spanish():
    assert "recepción" in onboarding.DEAD_END.lower()
    assert "gusto" in onboarding.WELCOME.lower()
    assert "cambiarte" in onboarding.SWITCH_CONFIRM.lower()
    assert "sí / no" in onboarding.SWITCH_CONFIRM.lower()
    for constant in (onboarding.DEAD_END, onboarding.WELCOME, onboarding.NAME_ASK):
        assert "welcome" not in constant.lower()  # no leftover English


def test_onboarding_still_accepts_spanish_yes():
    assert onboarding._is_affirmative("sí")
    assert onboarding._is_affirmative("si")


# --- check-in templates are Spanish ---


def test_checkin_templates_are_spanish():
    assert "días" in checkin.GAP_NUDGE
    assert "saltaste" in checkin.PINNED_NUDGE
    assert "cuando" in checkin.WINDDOWN.lower()


def test_checkin_weekday_names_are_spanish():
    assert checkin.WEEKDAY_NAMES[0] == "lunes"
    assert checkin.WEEKDAY_NAMES[6] == "domingo"


def test_pinned_nudge_fallback_stays_spanish_when_the_workout_is_unnamed():
    # a skipped workout with no name must not leak English into the nudge
    from datetime import date, datetime, UTC

    from agentg.checkin import CheckinData, decide_checkin

    d = CheckinData(
        state="on",
        snoozed_until=None,
        last_nudge_on=None,
        nudges_this_week=0,
        ignored_nudges=0,
        last_session_date=date(2026, 7, 10),
        signup_date=date(2026, 7, 1),
        pinned_weekdays=frozenset({0, 2}),  # Mon & Wed
        missed_workout=None,  # unnamed skipped workout → falls back
        missed_weekday=0,
        todays_workout=None,
    )
    message = decide_checkin(datetime(2026, 7, 15, 9, tzinfo=UTC), d).message  # Wed
    assert message and "your last session" not in message.lower()
    assert "sesión" in message.lower()


# --- channel-level fallbacks (no LLM turn) are Spanish too ---


def test_telegram_fallback_copy_is_spanish():
    from agentg.channels import telegram

    assert "de nuevo" in telegram.ERROR_REPLY.lower()
    assert "welcome" not in telegram.EMPTY_REPLY_FALLBACK.lower()
    assert "?" in telegram.EMPTY_REPLY_FALLBACK
