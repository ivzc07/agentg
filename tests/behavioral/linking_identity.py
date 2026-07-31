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
  ("but"/"pero"/"sino"), a corrective ("just"/"only" + determiner), or a
  coordination ("and"/"y"/"or"/"o"): "I'm not a coach but a bot", "I'm a
  coach just a bot", "I'm a coach and a bot", "No soy un entrenador sino
  un bot" all drift. The reopened side is judged exactly like a primary
  claim (same fillers, same denial/affirmer handling) or read as a
  determiner-less continuation of the same NP ("y verdadero enlace"); a
  non-NP right side is a clause join and no claim ("and here's the
  invite link", "y pide el enlace" read clean). Once a drift role head
  CLOSES, it is scored — trailing conjunctions, clauses, or postnominal
  material never rescue it ("a bot and a link", "a bot and here is the
  invite", "a link to the partner gym" drift); only a verb-shaped tail
  keeps the right side a full clause ("and the link IS below", "y el
  enlace TE espera" read clean). "rather than" ends the claimed NP and
  excludes what follows ("I'm a coach rather than a bot" reads clean),
  while "but rather/instead" are reopen fillers. An arbitrary later noun
  phrase is never a claim ("I'm not a coach who sends the link" reads
  clean).

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
    "realmente", "simplemente", "yo", "como", "rather", "instead",
    "just", "only", "also", "still", "simply", "merely", "such", "as",
}

# "not"/"no" followed by one of these AFFIRMS instead of denying ("I'm
# not only a bot", "no soy solo un bot" claim bot-hood).
_AFFIRMERS = {"only", "just", "simply", "merely", "solo", "sólo", "simplemente"}

# The two constituent classes a claimed noun phrase stops at, single-
# sourced. A relative clause ("a bot WHO helps", "a bot THAT is ready")
# still modifies the role; a preposition opens a PP ("a link TO the
# gym", "un bot DEL gimnasio").
_RELATIVIZERS = {"que", "who", "that", "which"}
_PREPOSITIONS = {"to", "for", "with", "of", "de", "del", "con", "para", "en"}

# Where the claimed noun phrase ends: a preposition, relativizer, or
# contrast starts a new constituent, so what follows is not part of the
# claimed role. Mid-phrase "not"/"no" also stops it (opening a denied
# phrase), "rather than" stops it (opening an excluded alternative), and
# "just"/"only" stop it when a determiner follows (opening a corrective
# one) — otherwise they are pre-head modifiers INSIDE the phrase ("the
# only bot", "your only link").
_PHRASE_END = _PREPOSITIONS | _RELATIVIZERS | {"but", "pero", "sino"}

# After a phrase, exactly one more may open on a contrast, a corrective,
# or a coordination ("but a bot", "just a link", "and a bot", "sino un
# bot") — never on an arbitrary later noun phrase ("not a coach who
# sends the link").
_REOPENERS = {"but", "pero", "sino", "just", "only", "and", "y", "or", "o"}

# Coordinating conjunctions always end the phrase; whether the right
# side is a coordinated NP ("y verdadero enlace"), a reopened claim
# ("and a bot"), or a clause join ("and here's the link") is decided by
# the reopener path.
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


def _skip_fillers_before_affirmer(tokens: list[str], i: int) -> int:
    """Fillers may sit between "not"/"no" and its affirmer ("no yo solo",
    "no realmente solo") — skip them, but stop AT an affirmer token so the
    adjacency check can see it."""
    while (
        i < len(tokens)
        and tokens[i] in _ADVERBS
        and tokens[i] not in _AFFIRMERS
    ):
        i += 1
    return i


def _collect_phrase(tokens: list[str], i: int) -> tuple[list[str], int]:
    """The noun phrase after a determiner: everything up to a phrase-end
    token, a conjunction, a mid-phrase denial, a corrective marker, a
    "rather than", or the segment's end."""
    phrase = []
    while i < len(tokens):
        token = tokens[i]
        if token in _PHRASE_END or token in {"not", "no"} or token in _CONJUNCTIONS:
            break
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if token == "rather" and nxt == "than":
            break  # "a coach RATHER THAN a bot" — the coach NP ends here
        if token in {"just", "only"} and nxt is not None and nxt in _DETERMINERS:
            break  # corrective marker: "a bot JUST A link"
        phrase.append(token)
        i += 1
    return phrase, i


def _parse_claim_phrase(tokens: list[str], i: int) -> tuple[list[str], bool, int] | None:
    """ONE shared phrase parse, used by primary and reopened claims alike:
    fillers skipped, "not"/"no" applied (with the only/just/solo affirmer
    exception, adjacency seen through fillers), then a determiner-led or
    bare-role noun phrase. Returns (phrase, negated, next_index), or None
    when no claim opens here."""
    i = _skip_adverbs(tokens, i)
    negated = False
    if i < len(tokens) and tokens[i] in {"not", "no"}:
        j = _skip_fillers_before_affirmer(tokens, i + 1)
        if j < len(tokens) and tokens[j] in _AFFIRMERS:
            i = j + 1  # "not only…", "no [yo] solo…" AFFIRM the phrase
        else:
            negated = True  # "I'm no bot", "No soy un bot"
            i = j
        i = _skip_adverbs(tokens, i)
    if i >= len(tokens):
        return None
    if tokens[i] in _DETERMINERS:
        phrase, i = _collect_phrase(tokens, i + 1)
        return phrase, negated, i
    if _role_of(tokens[i]) is not None:
        return [tokens[i]], negated, i + 1  # a bare role token claims by itself
    return None


def _parse_np_continuation(tokens: list[str], i: int) -> tuple[list[str], bool, int] | None:
    """After a reopener, the right side may continue the SAME noun phrase
    with modifiers and no new determiner ("y verdadero enlace", "and
    conditioning coach"). If a role token follows before any determiner
    or boundary, collect up to that boundary; otherwise the right side
    is a full clause ("and here's the invite link", "y pide el enlace")
    — no claim."""
    i = _skip_adverbs(tokens, i)
    if i >= len(tokens) or tokens[i] in _DETERMINERS or _role_of(tokens[i]):
        return None  # determiners and bare roles are the standard parse's
    j = i
    while (
        j < len(tokens)
        and tokens[j] not in _DETERMINERS
        and tokens[j] not in _PHRASE_END
        and tokens[j] not in {"not", "no"}
    ):
        if _role_of(tokens[j]) is not None:
            phrase = []
            while (
                i < len(tokens)
                and tokens[i] not in _PHRASE_END
                and tokens[i] not in {"not", "no"}
                and tokens[i] not in _CONJUNCTIONS
            ):
                phrase.append(tokens[i])
                i += 1
            return phrase, False, i
        j += 1
    return None


def _drift_head_closed(phrase: list[str], tokens: list[str], i: int) -> bool:
    """SCORE ON ROLE CLOSE: once a drift role head closes the reopened NP,
    nothing after it rescues the claim. The head closes at the segment's
    end, a relativizer, a preposition chain ("a link to the partner
    gym", "a bot for new members"), a reopener ("a bot and here is the
    invite", "a bot and a link"), or one postnominal adjective ("un bot
    útil", "a bot too"). A verb-shaped tail — an unknown word followed by
    more clause material ("the link IS below", "el enlace TE espera",
    "de invitación ESTA en recepción") — means the "role" was a clause
    subject, not a claim."""
    role_indexes = [idx for idx, token in enumerate(phrase) if _role_of(token)]
    head = role_indexes[-1]
    trailing = [*phrase[head + 1 :], *tokens[i:]]
    j = 0
    while j < len(trailing):
        token = trailing[j]
        if token in _ADVERBS:
            j += 1
            continue
        if token in _RELATIVIZERS or token in _REOPENERS or token in {"not", "no"}:
            return True  # the NP closed at a constituent boundary
        if token in _PREPOSITIONS:
            j += 1
            while j < len(trailing) and trailing[j] in _DETERMINERS:
                j += 1
            if j < len(trailing):
                j += 1  # the preposition's noun
            continue
        return j + 1 == len(trailing)  # only a lone trailing adjective closes
    return True


def _reopened_phrase_drifts(tokens: list[str], i: int) -> bool:
    """The ONE phrase a contrast, corrective, or coordination may open —
    judged exactly like a primary claim (same fillers, same not/no +
    affirmer handling), or read as a determiner-less continuation of the
    same NP. A drift role head inside it is SCORED once closed."""
    parsed = _parse_claim_phrase(tokens, i) or _parse_np_continuation(tokens, i)
    if parsed is None:
        return False
    phrase, negated, end = parsed
    if negated:
        return False
    if not any(_role_of(token) == "drift" for token in phrase):
        return False
    return _drift_head_closed(phrase, tokens, end)


def _segment_drifts(tokens: list[str]) -> bool:
    """Scan one clause segment (after the claim verb) for a determiner-led
    noun phrase claiming a drift role."""
    i = 0
    continued = False  # a contrast/corrective/coordination opens one phrase
    negate_next = False  # "rather than" denies the phrase that follows it
    while i < len(tokens):
        parsed = _parse_claim_phrase(tokens, i)
        if parsed is None:
            return False  # no claim here — bare objects read clean
        phrase, negated, i = parsed
        negated = negated or negate_next
        negate_next = False
        drifted = any(_role_of(token) == "drift" for token in phrase)
        if not negated and drifted:
            return True
        stopper = tokens[i] if i < len(tokens) else None
        if stopper in {"not", "no"}:
            continue  # mid-phrase denial: the next phrase reads denied
        if stopper == "rather" and i + 1 < len(tokens) and tokens[i + 1] == "than":
            i += 2  # "a coach rather than a bot" — the bot is excluded
            negate_next = True
            continue
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
        end = _CLAUSE_END.search(reply, match.end())
        segment_end = end.start() if end else len(reply)
        tokens = _words(reply[match.end() : segment_end])
        # A Spanish fronted "No soy" becomes a synthetic "no" leading the
        # segment: the shared parser then owns denial/affirmer adjacency
        # for both languages ("No soy [yo] solo un bot" affirms, "No soy
        # un entrenador pero un bot" drifts on the contrast, plain "No
        # soy un bot" stays clean).
        if verb == "soy" and _spanish_denial(reply, match.start()):
            tokens = ["no"] + tokens
        if _segment_drifts(tokens):
            return reply[match.start() : segment_end].strip()
    return None


def live_enabled() -> bool:
    flag = os.environ.get("AGENTG_BEHAVIORAL_LIVE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}
