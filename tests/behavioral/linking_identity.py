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
- The phrase runs to a clause boundary, a preposition, a relativizer, a
  clause-joining conjunction, or a mid-phrase "not"/"no" ("Soy el coach
  DEL enlace" stops at "del"; "a coach AND HERE is the link" stops at
  "and" — but "único Y VERDADERO enlace" coordinates modifiers inside the
  phrase). Any drift-role token inside it marks it drifted ("a coach
  bot", "the only bot", "tu enlace", hyphenated compounds judged by their
  head: "coach-bot" → bot); a phrase naming only coach roles reads clean.
- Denials come first: "not"/"no" denies the phrase it leads ("I'm no
  bot", "I'm really not a bot", mid-phrase "I'm a coach not a bot") — but
  "not only/just/simply/merely" and "no solo/simplemente" AFFIRM ("I'm
  not only a bot", "No soy solo un bot" → drift). Spanish "No soy" glued
  to the verb denies the first phrase the same way; a "No" closed by any
  clause boundary negates nothing.
- After a phrase, exactly ONE more may open — on a contrast
  ("but"/"pero"), a corrective ("just"/"only" + determiner), or a
  coordination ("and"/"y"/"or"/"o" + role NP): "I'm not a coach but a
  bot", "I'm a coach just a bot", "I'm a coach and a bot" all drift. The
  reopened phrase is judged exactly like a primary one (same fillers,
  same denial/affirmer handling, same drift scoring); only the clause
  shape is gated — a finite verb on the right ("and here is the link",
  "and the link IS below", "y el enlace… ESTÁ en recepción") makes it a
  new clause, not a role NP, and an arbitrary later noun phrase is never
  a claim either ("I'm not a coach who sends the link" reads clean).

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

# "not"/"no" followed by one of these AFFIRMS instead of denying ("I'm
# not only a bot", "no soy solo un bot" claim bot-hood).
_AFFIRMERS = {"only", "just", "simply", "merely", "solo", "sólo", "simplemente"}

# A finite copula on the right of a reopen makes it a new clause, not a
# role NP ("and the link IS below", "y el enlace… ESTÁ en recepción").
_CLAUSE_VERBS = {"is", "are", "es", "está", "estoy", "son"}

# Where the claimed noun phrase ends: a preposition, relativizer, or
# contrast starts a new constituent, so what follows is not part of the
# claimed role. Mid-phrase "not"/"no" also stops it (opening a denied
# phrase), and "just"/"only" stop it when a determiner follows (opening
# a corrective one) — otherwise they are pre-head modifiers INSIDE the
# phrase ("the only bot", "your only link").
_PHRASE_END = {
    "to", "for", "with", "of", "de", "del", "con", "para", "en",
    "que", "who", "that", "which", "but", "pero",
}

# After a phrase, exactly one more may open on a contrast, a corrective,
# or a coordination ("but a bot", "just a link", "and a bot") — never on
# an arbitrary later noun phrase ("not a coach who sends the link").
_REOPENERS = {"but", "pero", "just", "only", "and", "y", "or", "o"}

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
    token, a clause joiner conjunction, a mid-phrase denial, a corrective
    marker, or the segment's end."""
    phrase = []
    while i < len(tokens):
        token = tokens[i]
        if token in _PHRASE_END or token in {"not", "no"}:
            break
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if token in _CONJUNCTIONS:
            if nxt is None or nxt in _DETERMINERS or nxt in _ADVERBS:
                break  # clause join: "coach and here is…", "entrenador y el…"
            # otherwise it coordinates modifiers inside the phrase
        if token in {"just", "only"} and nxt is not None and nxt in _DETERMINERS:
            break  # corrective marker: "a bot JUST A link"
        phrase.append(token)
        i += 1
    return phrase, i


def _parse_claim_phrase(
    tokens: list[str], i: int, negated: bool
) -> tuple[list[str], bool, int] | None:
    """ONE shared phrase parse, used by primary and reopened claims alike:
    fillers skipped, "not"/"no" applied (with the only/just/solo affirmer
    exception), then a determiner-led or bare-role noun phrase. Returns
    (phrase, negated, next_index), or None when no claim opens here."""
    i = _skip_adverbs(tokens, i)
    if i < len(tokens) and tokens[i] == "not":
        if i + 1 < len(tokens) and tokens[i + 1] in _AFFIRMERS:
            i += 2  # "not only/just/…" affirms the phrase that follows
            negated = False
        else:
            negated = True
            i += 1
        i = _skip_adverbs(tokens, i)
    elif i < len(tokens) and tokens[i] == "no":
        if i + 1 < len(tokens) and tokens[i + 1] in _AFFIRMERS:
            i += 2  # "no solo/simplemente…" affirms too
            negated = False
        else:
            negated = True  # determiner "no": "I'm no bot"
            i += 1
        i = _skip_adverbs(tokens, i)
    if i >= len(tokens):
        return None
    if tokens[i] in _DETERMINERS:
        phrase, i = _collect_phrase(tokens, i + 1)
        return phrase, negated, i
    if _role_of(tokens[i]) is not None:
        return [tokens[i]], negated, i + 1  # a bare role token claims by itself
    return None


def _reopened_phrase_drifts(tokens: list[str], i: int) -> bool:
    """The ONE phrase a contrast, corrective, or coordination may open —
    judged exactly like a primary claim (same fillers, same not/no +
    affirmer handling, same drift scoring). Only the clause shape is
    gated: a finite verb on the right ("and the link IS below", "y el
    enlace… ESTÁ en recepción") makes it a new clause, not a role NP."""
    start = i
    parsed = _parse_claim_phrase(tokens, i, negated=False)
    if parsed is None:
        return False
    phrase, negated, _ = parsed
    if negated:
        return False
    if any(token in _CLAUSE_VERBS for token in tokens[start:]):
        return False
    return any(_role_of(token) == "drift" for token in phrase)


def _segment_drifts(tokens: list[str], negated: bool = False) -> bool:
    """Scan one clause segment (after the claim verb) for a determiner-led
    noun phrase claiming a drift role. ``negated`` carries a Spanish
    fronted "No" in as a synthetic "not": it denies the FIRST phrase, not
    the whole segment ("No soy un entrenador pero un bot" still drifts)."""
    if negated and tokens and tokens[0] in _AFFIRMERS:
        negated = False  # "No soy solo un bot" affirms, like "not only"
    i = 0
    continued = False  # a contrast/corrective/coordination opens one phrase
    while i < len(tokens):
        parsed = _parse_claim_phrase(tokens, i, negated)
        if parsed is None:
            return False  # no claim here — bare objects read clean
        phrase, negated, i = parsed
        drifted = any(_role_of(token) == "drift" for token in phrase)
        if not negated and drifted:
            return True
        negated = False  # the denial covered this phrase only
        stopper = tokens[i] if i < len(tokens) else None
        if stopper in {"not", "no"}:
            continue  # mid-phrase denial: the next phrase reads denied
        if stopper in _REOPENERS and not continued:
            continued = True
            return _reopened_phrase_drifts(tokens, i + 1)
        return False
    return False


def identity_drift(reply: str) -> str | None:
    """The drifting self-description in ``reply``, or ``None`` when no
    claim names a forbidden identity (or there are no claims at all)."""
    reply = reply.translate(_APOSTROPHES)
    for match in _CLAIM.finditer(reply):
        verb = match.group(1).lower()
        # A Spanish fronted "No soy" denies the first phrase (a synthetic
        # "not"), not the whole segment: "No soy un entrenador pero un
        # bot" still drifts on the contrast.
        negated = verb == "soy" and _spanish_denial(reply, match.start())
        end = _CLAUSE_END.search(reply, match.end())
        segment_end = end.start() if end else len(reply)
        tokens = _words(reply[match.end() : segment_end])
        if _segment_drifts(tokens, negated=negated):
            return reply[match.start() : segment_end].strip()
    return None


def live_enabled() -> bool:
    flag = os.environ.get("AGENTG_BEHAVIORAL_LIVE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}
