"""Deterministic identity guard for the linking phraser's replies (issue #66).

The linking voice has exactly one identity: a coach who works through
partner gyms. The guard catches the phraser claiming a FORBIDDEN identity —
the ones the prompt names and bans (link, enlace, bot, assistant,
asistente). One coherent rule:

- Split the reply into clauses (``. , ; ! ? : \\n``, em/en dashes, and
  space-surrounded ``-``; an intra-token ``-`` joins a compound instead).
- Each first-person claim (soy / I'm / I am, apostrophes normalized) owns
  the clause segment after its verb — scanned whole, no window. The FIRST
  role token in that segment decides: a coach role first reads clean
  ("Soy el coach del enlace de invitación"), a drift role first reads
  drifted, however far into the clause it lands.
- A hyphenated compound containing a role token is judged by its head
  ("coach-bot", "ai-assistant" → drift).
- Denials are not claims: Spanish "No soy" glued to the verb (a "No"
  closed by any clause boundary negates nothing), English "not" anywhere
  between the verb and the role ("I'm really not a bot").

A coach claim needs no anchor to pass; unknown phrasing passes by default.
The offline suite exercises the guard on canned replies; the live sweep
behind the ``live`` marker runs the same check against real phraser output.
"""

from __future__ import annotations

import os
import re

# The only roles a self-description may claim (Spanish default + English
# mirror, singular and plural) — one of these coming first reads clean.
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

# The identities the phraser prompt names and bans — one of these coming
# first marks the reply drifted.
FORBIDDEN_ROLES = {"link", "enlace", "bot", "assistant", "asistente", "asistenta"}

# First-person identity verbs in the two languages the phraser speaks.
_CLAIM = re.compile(r"\b(soy|i am|i'm)\b", re.IGNORECASE)

# ONE clause boundary, single-sourced: hard punctuation, em/en dashes, or a
# space-surrounded hyphen. An intra-token "-" is NOT a boundary — it joins
# a compound ("coach-bot") for splitting decisions.
_CLAUSE_END = re.compile(r"[,;.!?:\n—–]|(?<=\s)-(?=\s)")

# Letters joined by intra-token "-" or "'" stay one token ("coach-bot",
# "gym's"); everything else splits words apart.
_WORD = re.compile(r"[a-záéíóúñü]+(?:[-'][a-záéíóúñü]+)*")
_TRAILING_WORD = re.compile(r"(\w+)([^\w]*)$")

# Curly/typographic apostrophes read as ASCII so "I’m" is still a claim.
_APOSTROPHES = str.maketrans("’‘ʼ", "'''")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _spanish_denial(reply: str, claim_start: int) -> bool:
    """"No soy" glued to the verb negates it; a "No" closed by any clause
    boundary ("No, soy…", "No: soy…", "No - soy…", "No\\nsoy…") answered
    something else. English claims negate after the verb, never from a
    fronted "No"."""
    trailing = _TRAILING_WORD.search(reply[:claim_start])
    return (
        trailing is not None
        and trailing.group(1).lower() == "no"
        and not _CLAUSE_END.search(trailing.group(2))
    )


def _role_of(token: str) -> str | None:
    """"coach" / "drift" / None for one token. A hyphenated compound that
    contains a role token is judged by its head (its last part):
    "coach-bot" → drift, "bot-coach" → coach."""
    if "-" in token:
        parts = token.split("-")
        if any(part in COACH_ROLES or part in FORBIDDEN_ROLES for part in parts):
            token = parts[-1]
    if token in COACH_ROLES:
        return "coach"
    if token in FORBIDDEN_ROLES:
        return "drift"
    return None


def identity_drift(reply: str) -> str | None:
    """The drifting self-description in ``reply``, or ``None`` when no
    claim names a forbidden identity (or there are no claims at all)."""
    reply = reply.translate(_APOSTROPHES)
    for match in _CLAIM.finditer(reply):
        verb = match.group(1).lower()
        if verb == "soy" and _spanish_denial(reply, match.start()):
            continue
        end = _CLAUSE_END.search(reply, match.end())
        segment_end = end.start() if end else len(reply)
        tokens = _words(reply[match.end() : segment_end])
        first = next(
            ((i, role) for i, token in enumerate(tokens) if (role := _role_of(token))),
            None,
        )
        if first is None or first[1] == "coach":
            continue  # no role claimed, or the coach identity came first
        index, _ = first
        if verb != "soy" and "not" in tokens[:index]:
            continue  # "I'm really not a bot" — a denial, not a claim
        return reply[match.start() : segment_end].strip()
    return None


def live_enabled() -> bool:
    flag = os.environ.get("AGENTG_BEHAVIORAL_LIVE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}
