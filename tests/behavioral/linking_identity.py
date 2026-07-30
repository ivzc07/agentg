"""Deterministic identity guard for the linking phraser's replies (issue #66).

The linking voice has exactly one identity: a coach who works through
partner gyms. The phraser is an LLM, so the prompt alone leaves no evidence
the identity holds — this checker scans a reply for first-person role
claims ("soy un …", "I am a …", "I'm your …") and flags any whose claimed
role names anything other than that coach. The anchor must be the claimed
role itself: an anchor word appearing later in the sentence ("I'm a link
for partner gym coaches") does not excuse the drift, denials ("No soy un
bot") are not claims, and non-role first person ("I'm at the gym", "Soy
nuevo") is ignored. The offline suite exercises the checker on canned
replies; the live sweep behind the ``live`` marker runs the same check
against real phraser output.
"""

from __future__ import annotations

import os
import re

# The only roles a self-description may claim (Spanish default + English
# mirror, singular and plural).
COACH_ROLES = {
    "coach",
    "coaches",
    "entrenador",
    "entrenadora",
    "entrenadores",
    "entrenadoras",
    "trainer",
    "trainers",
}

# First-person identity verbs in the two languages the phraser speaks.
_CLAIM = re.compile(r"\b(soy|i am|i'm)\b", re.IGNORECASE)
# A role claim runs to the first clause or sentence boundary.
_SEGMENT_END = re.compile(r"[,;.!?\n]")
_WORD = re.compile(r"[a-záéíóúñü']+")

# A role claim names its role right after a determiner ("soy TU enlace",
# "I'm YOUR link"); anything else after the verb is a state, not a role.
_DETERMINERS = {
    "un", "una", "tu", "tus", "su", "sus", "mi", "mis", "el", "la", "los", "las",
    "a", "an", "the", "your", "my", "his", "her", "its", "our", "their",
}
# Fillers that may sit between the verb and the determiner ("soy solo un …").
_ADVERBS = {"solo", "sólo", "también", "tambien", "aún", "aun", "just", "only", "also", "still"}
_NEGATIONS = {"no", "not"}

# Curly/typographic apostrophes read as ASCII so "I’m" is still a claim.
_APOSTROPHES = str.maketrans("’‘ʼ", "'''")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def identity_drift(reply: str) -> str | None:
    """The drifting self-description in ``reply``, or ``None`` when every
    role claim names the coach identity (or there are no claims at all)."""
    reply = reply.translate(_APOSTROPHES)
    for match in _CLAIM.finditer(reply):
        before = _words(reply[: match.start()])
        if before and before[-1] in _NEGATIONS:
            continue  # "No soy un bot" — a denial, not a claim
        end = _SEGMENT_END.search(reply, match.end())
        segment_end = end.start() if end else len(reply)
        words = _words(reply[match.end() : segment_end])
        while words and words[0] in _ADVERBS:
            words.pop(0)
        if not words or words[0] in _NEGATIONS:
            continue  # "I'm not a coach" — a denial, not a claim
        if words[0] not in _DETERMINERS:
            continue  # non-role first person: "I'm at the gym", "Soy nuevo"
        claimed = words[1:3]  # the role head, allowing one adjective before it
        if any(word in COACH_ROLES for word in claimed):
            continue
        return reply[match.start() : segment_end].strip()
    return None


def live_enabled() -> bool:
    flag = os.environ.get("AGENTG_BEHAVIORAL_LIVE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}
