"""Deterministic identity guard for the linking phraser's replies (issue #66).

The linking voice has exactly one identity: a coach who works through
partner gyms. The guard's only job is to catch the phraser claiming a
FORBIDDEN identity — the ones the prompt names and bans (link, enlace,
bot, assistant, asistente). It finds each first-person claim (soy / I'm /
I am, apostrophes normalized) and flags it only when a forbidden role word
lands within a few tokens of the verb and the claim is not negated.
Negation is punctuation-aware: "No soy un bot" and "I'm not a coach" are
denials, but a "No" closed by punctuation ("No, soy tu enlace.") or
fronting an English claim ("No I'm your link") negates nothing. A coach
claim needs no anchor to pass; unknown phrasing passes by default. The
offline suite exercises the guard on canned replies; the live sweep behind
the ``live`` marker runs the same check against real phraser output.
"""

from __future__ import annotations

import os
import re

# The identities the phraser prompt names and bans; one claimed within a
# few tokens of the verb marks the reply drifted.
FORBIDDEN_ROLES = {"link", "enlace", "bot", "assistant", "asistente", "asistenta"}

# First-person identity verbs in the two languages the phraser speaks.
_CLAIM = re.compile(r"\b(soy|i am|i'm)\b", re.IGNORECASE)
_WORD = re.compile(r"[a-záéíóúñü']+")
_TRAILING_WORD = re.compile(r"(\w+)([^\w]*)$")
_PUNCTUATION = re.compile(r"[,;.!?—–]")
# A claim runs to the first clause or sentence boundary.
_SEGMENT_END = re.compile(r"[,;.!?\n:—–-]")

# How far after the verb a forbidden word still reads as the claimed role
# ("soy tu ENLACE", "I'm your LINK"); beyond that it belongs to the ask.
_WINDOW_TOKENS = 4

# Curly/typographic apostrophes read as ASCII so "I’m" is still a claim.
_APOSTROPHES = str.maketrans("’‘ʼ", "'''")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _spanish_denial(reply: str, claim_start: int) -> bool:
    """"No soy" glued to the verb negates it; a "No" closed by punctuation
    ("No, soy…") answered something else. English claims negate after the
    verb ("I'm not…"), never from a fronted "No"."""
    trailing = _TRAILING_WORD.search(reply[:claim_start])
    return (
        trailing is not None
        and trailing.group(1).lower() == "no"
        and not _PUNCTUATION.search(trailing.group(2))
    )


def identity_drift(reply: str) -> str | None:
    """The drifting self-description in ``reply``, or ``None`` when no
    claim names a forbidden identity (or there are no claims at all)."""
    reply = reply.translate(_APOSTROPHES)
    for match in _CLAIM.finditer(reply):
        verb = match.group(1).lower()
        if verb == "soy" and _spanish_denial(reply, match.start()):
            continue
        end = _SEGMENT_END.search(reply, match.end())
        segment_end = end.start() if end else len(reply)
        words = _words(reply[match.end() : segment_end])
        if verb != "soy" and words and words[0] == "not":
            continue  # "I'm not a coach" — a denial, not a claim
        if any(word in FORBIDDEN_ROLES for word in words[:_WINDOW_TOKENS]):
            return reply[match.start() : segment_end].strip()
    return None


def live_enabled() -> bool:
    flag = os.environ.get("AGENTG_BEHAVIORAL_LIVE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}
