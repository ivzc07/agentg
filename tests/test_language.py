"""Language rule & AI-disclosure (ADR 0002).

The Agent mirrors the Member's language, defaulting to Spanish. Linking's
phraser follows the same mirror-or-Spanish-default rule (its instructions are
English on purpose — they're never sent as-is, only phrased); check-in
templates — the one surface with no LLM turn at all — stay fixed Spanish. The
Agent never announces it is an AI (deflects if asked) while keeping the
behavioral safety floor.
"""

from agentg import checkin, linking
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


def test_the_catalog_carve_out_is_scoped_to_exercise_names_only():
    # #67: "muscle" leaked into a Spanish intake because the carve-out read as
    # blanket permission for English; it covers Exercise catalog names only
    text = INSTRUCTIONS.lower()
    assert "exercise names are the only exception" in text
    assert "masa muscular" in text  # the Spanish for the observed leak, pinned as the example


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


# --- the default rules doc does not anchor goal vocabulary in English only ---


def test_the_default_rules_doc_offers_spanish_goal_vocabulary():
    # the doc is the Agent's nearest vocabulary anchor at intake (issue #68);
    # every goal must carry its Spanish term so a Spanish-speaking Member
    # never hears the English one
    doc = DEFAULT_RULES_DOC.lower()
    assert "fuerza" in doc  # strength
    assert "hipertrofia" in doc  # general/hypertrophy
    assert "resistencia" in doc  # endurance


def test_the_default_rules_doc_still_maps_each_goal_to_its_sets_and_reps():
    # the goal terms are load-bearing: generation keys the scheme off them,
    # so each goal line must keep its sets and rep range unambiguous
    doc = DEFAULT_RULES_DOC.lower()
    assert "strength" in doc and "3-5 sets of 3-6 reps" in doc
    assert "hypertrophy" in doc and "3-4 sets of 8-12 reps" in doc
    assert "endurance" in doc and "2-3 sets of 12-20 reps" in doc
    # each scheme sits on the same line as its goal — no detached numbers
    lines = [line for line in doc.splitlines() if "goal" in line]
    assert any("strength" in line and "3-5 sets of 3-6 reps" in line for line in lines)
    assert any("hypertrophy" in line and "3-4 sets of 8-12 reps" in line for line in lines)
    assert any("endurance" in line and "2-3 sets of 12-20 reps" in line for line in lines)


# --- linking's phraser follows the same mirror-or-Spanish rule ---


def test_linking_phraser_pins_the_mirror_and_spanish_default():
    text = linking._PHRASER_PROMPT.lower()
    assert "mirror" in text
    assert "spanish" in text  # the default when there is no signal yet


def test_linking_still_accepts_spanish_yes():
    assert linking._is_affirmative("sí")
    assert linking._is_affirmative("si")


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
