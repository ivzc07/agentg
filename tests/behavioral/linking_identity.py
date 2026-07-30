"""Deterministic identity guard for the linking phraser's replies (issue #66).

The linking voice has exactly one identity: a coach who works through
partner gyms. The phraser is an LLM, so the prompt alone leaves no evidence
the identity holds — this checker parses each first-person claim as
claim-verb + noun phrase ("soy UN ENTRENADOR personal", "I'm a certified
personal TRAINER") and judges the HEAD noun of that phrase: the first token
after the determiner in Spanish (adjectives trail the noun), the last in
English (they lead it). A phrase carrying a known drift word ("link coach")
is flagged however its head reads; denials ("No soy un bot") and non-role
first person ("I'm at the gym", "Soy nuevo") are not claims and pass. The
offline suite exercises the checker on canned replies; the live sweep
behind the ``live`` marker runs the same check against real phraser output.
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

# Non-coach identities the phraser prompt names and bans; one anywhere in
# the claimed noun phrase marks it drifted, even with a coach head noun
# ("a link coach", "un enlace entrenador").
DRIFT_ROLES = {"link", "enlace", "bot", "assistant", "asistente", "asistenta"}

# First-person identity verbs in the two languages the phraser speaks.
_CLAIM = re.compile(r"\b(soy|i am|i'm)\b", re.IGNORECASE)
# A claim runs to the first clause or sentence boundary.
_SEGMENT_END = re.compile(r"[,;.!?\n—–]")
_WORD = re.compile(r"[a-záéíóúñü']+")
_TRAILING_WORD = re.compile(r"(\w+)([^\w]*)$")
_PUNCTUATION = re.compile(r"[,;.!?—–]")

# A role claim names its role after a determiner ("soy TU enlace", "I'm
# YOUR link"); anything else after the verb is a state, not a role.
_DETERMINERS = {
    "un", "una", "tu", "tus", "su", "sus", "mi", "mis", "el", "la", "los", "las",
    "a", "an", "the", "your", "my", "his", "her", "its", "our", "their",
}
# Fillers that may sit between the verb and the determiner ("soy solo un …").
_ADVERBS = {"solo", "sólo", "también", "tambien", "aún", "aun", "just", "only", "also", "still"}
_NEGATIONS = {"no", "not"}

# Words that close the noun phrase: anything from here on is a relative
# clause, conjunction, or prepositional phrase — not part of the claimed
# role ("a coach WHO…", "un entrenador DE gimnasios").
_PHRASE_END = {
    "que", "who", "that", "which", "and", "y", "or", "o", "but", "pero",
    "because", "porque", "if", "si", "when", "cuando", "while", "mientras",
    "para", "por", "con", "de", "en", "a", "desde", "sin", "sobre",
    "for", "with", "of", "in", "on", "at", "to", "from", "without", "about", "as",
}

# Curly/typographic apostrophes read as ASCII so "I’m" is still a claim.
_APOSTROPHES = str.maketrans("’‘ʼ", "'''")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _is_denied(reply: str, claim_start: int) -> bool:
    """A bare "no/not" glued to the verb negates it ("No soy…", "I'm not…");
    one closed by punctuation ("No, soy…") answered something else."""
    trailing = _TRAILING_WORD.search(reply[:claim_start])
    return (
        trailing is not None
        and trailing.group(1).lower() in _NEGATIONS
        and not _PUNCTUATION.search(trailing.group(2))
    )


def _noun_phrase(tokens: list[str]) -> list[str]:
    """The claimed noun phrase: everything up to the first phrase-boundary
    word (relative pronoun, conjunction, preposition)."""
    phrase = []
    for token in tokens:
        if token in _PHRASE_END:
            break
        phrase.append(token)
    return phrase


def identity_drift(reply: str) -> str | None:
    """The drifting self-description in ``reply``, or ``None`` when every
    role claim names the coach identity (or there are no claims at all)."""
    reply = reply.translate(_APOSTROPHES)
    for match in _CLAIM.finditer(reply):
        if _is_denied(reply, match.start()):
            continue
        end = _SEGMENT_END.search(reply, match.end())
        segment_end = end.start() if end else len(reply)
        words = _words(reply[match.end() : segment_end])
        while words and words[0] in _ADVERBS:
            words.pop(0)
        if not words or words[0] in _NEGATIONS:
            continue  # "I'm not a coach" — a denial, not a claim
        if words[0] not in _DETERMINERS:
            continue  # non-role first person: "I'm at the gym", "Soy nuevo"
        phrase = _noun_phrase(words[1:])
        if not phrase:
            continue
        if any(word in DRIFT_ROLES for word in phrase):
            return reply[match.start() : segment_end].strip()
        # Spanish trails its adjectives (head first); English leads them.
        head = phrase[0] if match.group(1).lower() == "soy" else phrase[-1]
        if head not in COACH_ROLES:
            return reply[match.start() : segment_end].strip()
    return None


def live_enabled() -> bool:
    flag = os.environ.get("AGENTG_BEHAVIORAL_LIVE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}
