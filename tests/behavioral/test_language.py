"""Language consistency in chat (issue #67).

A Member spoken to in Spanish hears Spanish training vocabulary; the ONLY
English allowed in a Spanish reply is Exercise catalog names in their
catalog form. The deterministic lexicon gate here runs offline in CI; the
live judge scores the same dimension (``language_consistency``).
"""

from __future__ import annotations

from agentg.training import SEED_EXERCISES
from behavioral.harness import ConversationHarness, message
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


async def test_spanish_intake_goal_question_uses_spanish_vocabulary(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        reply = await h.say("Hola, quiero empezar a entrenar", steps=[message(CORRECTED)])
        assert find_english_leaks(reply) == set()


async def test_catalog_names_stay_in_catalog_form_in_spanish_chat(tmp_path):
    async with ConversationHarness.create(tmp_path) as h:
        await h.linked_member()
        reply = await h.say(
            "¿qué toca hoy?",
            steps=[
                message("Hoy toca tren superior: bench press, overhead press y barbell row. 💪")
            ],
        )
        assert "bench press" in reply  # catalog form in chat
        assert find_english_leaks(reply) == set()
