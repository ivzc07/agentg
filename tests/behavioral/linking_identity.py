"""Deterministic identity guard for the linking phraser's replies (issue #66).

The linking voice has exactly one identity: a coach who works through
partner gyms. The guard catches the phraser claiming a FORBIDDEN identity —
the ones the prompt names and bans (link, enlace, bot, assistant,
asistente). One coherent rule: a claim is a DETERMINER-LED NOUN PHRASE,
not any role word in the clause.

- Split the reply into clauses (``. , ; ! ? : \\n``, em/en dashes, and
  space-surrounded ``-``; an intra-token ``-`` joins a compound instead).
- After each claim verb (soy / I'm / I am, apostrophes normalized), skip
  fillers: adverbs ("really", "definitely", "aquí", …), pronoun "yo",
  predeterminer "such", role-preserving "as"/"como". A claim exists ONLY
  if the next token opens a noun phrase — a determiner/possessive
  (tu/su/un/una/el/la | your/my/a/an/the) or a bare role token itself.
  Anything else ("I'm waiting for the invite link", "Soy quien te ayuda
  con el enlace") is no claim and reads clean.
- The phrase runs to a clause boundary, a preposition, a relativizer, or
  a clause-joining conjunction ("Soy el coach DEL enlace" stops at "del";
  "a coach AND HERE is the link" stops at "and" — but "único Y VERDADERO
  enlace" coordinates modifiers inside the phrase). Any drift-role token
  inside it marks it drifted ("a coach bot", "tu enlace", hyphenated
  compounds judged by their head: "coach-bot" → bot); a phrase naming
  only coach roles reads clean.
- Denials come first: "not"/"no" before the phrase denies THAT phrase
  ("I'm no bot", "I'm really not a bot") — but "not only/just/simply/
  merely" AFFIRMS ("I'm not only a bot" → drift). A phrase reopens after
  evaluation ONLY on a contrast ("but"/"pero", exactly once: "I'm not a
  coach but a bot" → drift) or, after a denial, on a corrective
  "just"/"only" ("I'm not a bot just a link" → drift) — never on an
  arbitrary later noun phrase ("I'm not a coach who sends the link"
  reads clean). Spanish "No soy" glued to the verb denies too; a "No"
  closed by any clause boundary negates nothing.

A coach claim needs no anchor to pass; unknown phrasing passes by default.
The offline suite exercises the guard on canned replies; the live sweep
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

# The identities the phraser prompt names and bans; one inside the claimed
# noun phrase marks the reply drifted.
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

# Openers of the claimed noun phrase: a determiner/possessive ("soy TU
# enlace", "I'm YOUR link"). A bare role token opens a claim too.
_DETERMINERS = {
    "un", "una", "tu", "tus", "su", "sus", "mi", "mis", "el", "la", "los", "las",
    "a", "an", "the", "your", "my", "his", "her", "its", "our", "their",
}

# Fillers between the verb and the phrase ("I'm really not…", "soy aquí…").
# Claim-preserving tokens live here too: pronoun "yo", predeterminer
# "such", role-preserving "as"/"como" ("I'm here as a bot", "Soy yo el
# enlace" still claim). Identity-breaking tokens (gerunds like "waiting",
# relativizers like "quien", prepositions like "for"/"con") are NOT here —
# they abort the claim instead.
_ADVERBS = {
    "really", "honestly", "actually", "truly", "here", "there", "now", "today",
    "definitely", "basically", "currently", "certainly", "totally",
    "aqui", "aquí", "ya", "solo", "sólo", "también", "tambien", "aún", "aun",
    "realmente", "simplemente", "yo", "como",
    "just", "only", "also", "still", "simply", "merely", "such", "as",
}

# "not" followed by one of these AFFIRMS instead of denying
# ("I'm not only a bot" claims bot-hood).
_AFFIRMERS = {"only", "just", "simply", "merely"}

# Where the claimed noun phrase ends: a preposition, relativizer, or
# contrast starts a new constituent, so what follows is not part of the
# claimed role. "just"/"only" also stop a phrase — after a denial they
# open a corrective one ("I'm not a bot JUST a link").
_PHRASE_END = {
    "to", "for", "with", "of", "de", "del", "con", "para", "en",
    "que", "who", "that", "which", "but", "pero", "just", "only",
}

# Coordinating conjunctions end the phrase when they join clauses ("a
# coach AND HERE is the link", "un entrenador Y EL enlace…") but not when
# they coordinate modifiers inside it ("único Y VERDADERO enlace",
# "strength AND CONDITIONING coach"): the token after the conjunction
# decides.
_CONJUNCTIONS = {"and", "y", "or", "o"}

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


def _skip_adverbs(tokens: list[str], i: int) -> int:
    while i < len(tokens) and tokens[i] in _ADVERBS:
        i += 1
    return i


def _collect_phrase(tokens: list[str], i: int) -> tuple[list[str], int]:
    """The noun phrase after a determiner: everything up to a phrase-end
    token, a clause joiner conjunction, or the segment's end."""
    phrase = []
    while i < len(tokens):
        token = tokens[i]
        if token in _PHRASE_END:
            break
        if token in _CONJUNCTIONS:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is None or nxt in _DETERMINERS or nxt in _ADVERBS:
                break  # clause join: "coach and here is…", "entrenador y el…"
            # otherwise it coordinates modifiers inside the phrase
        phrase.append(token)
        i += 1
    return phrase, i


def _segment_drifts(tokens: list[str]) -> bool:
    """Scan one clause segment (after the claim verb) for a determiner-led
    noun phrase claiming a drift role. A denied phrase reopens ONLY on a
    contrast ("but a bot") or a corrective ("just a link") — never on an
    arbitrary later noun phrase."""
    i = 0
    continued = False  # but/pero may open exactly one more phrase
    while i < len(tokens):
        i = _skip_adverbs(tokens, i)
        if i >= len(tokens):
            return False
        negated = False
        if tokens[i] == "not":
            if i + 1 < len(tokens) and tokens[i + 1] in _AFFIRMERS:
                i += 2  # "not only/just/…" affirms the phrase that follows
            else:
                negated = True
                i += 1
            i = _skip_adverbs(tokens, i)
        elif tokens[i] == "no":  # determiner "no": "I'm no bot"
            negated = True
            i = _skip_adverbs(tokens, i + 1)
        if i >= len(tokens):
            return False
        if tokens[i] in _DETERMINERS:
            i += 1
            phrase, i = _collect_phrase(tokens, i)
        elif _role_of(tokens[i]) is not None:
            phrase = [tokens[i]]  # a bare role token is a claim by itself
            i += 1
        else:
            return False  # no claim here — bare objects read clean
        drifted = any(_role_of(token) == "drift" for token in phrase)
        if not negated and drifted:
            return True
        stopper = tokens[i] if i < len(tokens) else None
        if stopper in {"but", "pero"} and not continued:
            continued = True  # a contrast opens exactly one more phrase
            i += 1
            continue
        if negated and stopper in {"just", "only"}:
            i += 1  # corrective without "but": "I'm not a bot just a link"
            continue
        return False
    return False


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
        if _segment_drifts(tokens):
            return reply[match.start() : segment_end].strip()
    return None


def live_enabled() -> bool:
    flag = os.environ.get("AGENTG_BEHAVIORAL_LIVE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}
