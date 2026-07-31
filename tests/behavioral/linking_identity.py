"""Identity guard for the linking phraser's replies (issue #66).

The linking voice has exactly one identity: a coach who works through
partner gyms. Two consecutive ``DEAD_END_INSTRUCTION`` calls once produced
two different self-descriptions, the second nonsense ("Soy tu enlace") —
the model re-improvising the app's identity per call.

Two layers, deliberately:

- OFFLINE (``identity_drift``): a narrow, high-precision EXACT PIN for the
  regression class from the issue — a claim verb (soy / I'm / I am)
  directly naming a forbidden role ("Soy tu enlace", "I'm your link",
  "I'm a bot", "I am an assistant"). It is intentionally NOT a grammar:
  15 review rounds proved POS-less parsing of EN+ES does not converge —
  every disambiguation rule has a dual (esta verb/demonstrative, just/only
  adverb/corrective, ni/nor fronted/mid).
- LIVE (``judge_identity``): full grammatical judgment delegated to the
  behavioral judge infrastructure (``behavioral.judge``), asking one
  question of each reply — ``IDENTITY_QUESTION``. That is the actual
  guard issue #66 needs; the offline pin only keeps the exact observed
  failure under CI.
"""

from __future__ import annotations

import os
import re

from behavioral.judge import (
    DEFAULT_JUDGE_MODEL,
    JudgeBackend,
    LiteLLMJudgeBackend,
    parse_judge_response,
)

# The identities the phraser prompt names and bans.
FORBIDDEN_ROLES = {"link", "enlace", "bot", "assistant", "asistente", "asistenta"}

# First-person identity verbs in the two languages the phraser speaks.
_CLAIM = re.compile(r"\b(soy|i am|i'm)\b", re.IGNORECASE)
_WORD = re.compile(r"[a-záéíóúñü'-]+")
# The optional determiner/possessive between the verb and the role.
_DETERMINERS = {
    "un", "una", "tu", "tus", "su", "sus", "mi", "mis", "el", "la",
    "a", "an", "the", "your", "my",
}
# Spanish pre-verbal negators of "ser" form a closed class — no, nunca,
# jamás (accented or not), tampoco, ni, and ni siquiera (whose token
# before the verb is "siquiera") — plus English "not".
_NEGATIONS = {"no", "not", "nunca", "jamás", "jamas", "tampoco", "ni", "siquiera"}
_TRAILING_WORD = re.compile(r"(\w+)([^\w]*)$")
# A negation only denies the claim it is glued to: any clause break
# (sentence punctuation, comma, colon, dash, newline) between it and the
# verb means it answered something else.
_CLAUSE_BREAK = re.compile(r"[,;.!?:\n—–…]|(?<=\s)-(?=\s)")

# Curly/typographic apostrophes read as ASCII so "I’m" is still a claim.
_APOSTROPHES = str.maketrans("’‘ʼ", "'''")


def identity_drift(reply: str) -> str | None:
    """The drifting self-description in ``reply``, or ``None`` — but ONLY
    the exact pinned shape: claim verb + optional determiner + forbidden
    role ("Soy tu enlace", "I'm a bot"). Denials adjacent to the verb are
    not claims — Spanish "No soy" glued to the verb (a "No" closed by any
    clause break negates nothing); English negation is post-verbal only,
    so a fronted "No" never denies an I-am claim ("No I am your link."
    flags). Anything more elaborate is the live judge's job, by design."""
    reply = reply.translate(_APOSTROPHES)
    for match in _CLAIM.finditer(reply):
        if match.group(1).lower() == "soy":
            trailing = _TRAILING_WORD.search(reply[: match.start()])
            if (
                trailing is not None
                and trailing.group(1).lower() in _NEGATIONS
                and not _CLAUSE_BREAK.search(trailing.group(2))
            ):
                continue
        words = _WORD.findall(reply[match.end() :].lower())
        if words and words[0] in _NEGATIONS:
            continue
        if words and words[0] in _DETERMINERS:
            words = words[1:]
        if words and words[0] in FORBIDDEN_ROLES:
            return reply[match.start() :].strip()
    return None


# The one question the live judge answers for every linking reply.
IDENTITY_QUESTION = (
    "Does the reply describe the SPEAKER ITSELF as a link, enlace, bot, "
    "assistant, asistente, or asistenta instead of a coach? (Mentioning "
    "an invite link as an object is fine.)"
)

IDENTITY_THRESHOLD = 4.0  # 1-5 scale: 1 = drifted identity, 5 = anchored


def build_identity_prompt(reply: str) -> tuple[str, str]:
    """System + user prompts for judging one linking reply's identity
    (evidence before score, JSON only — ``behavioral.judge``'s design)."""
    system = (
        "You are an expert evaluator of a gym-coaching chat agent's linking voice.\n"
        f"Answer this question about the reply: {IDENTITY_QUESTION}\n"
        "Cite evidence from the reply FIRST, then give a 1-5 identity score:\n"
        "1 = the reply describes the speaker itself as a link/enlace/bot/"
        "assistant/asistente/asistenta,\n"
        "5 = the speaker is a coach, or has no self-description at all.\n"
        "Respond with JSON only, no markdown fences, shaped as:\n"
        '{"identity": {"evidence": "...", "score": 1}}'
    )
    return system, f"## Reply\n{reply}\n"


async def judge_identity(backend: JudgeBackend, reply: str) -> tuple[bool, str]:
    """Live identity judgment for one reply: (passed, evidence)."""
    system, user = build_identity_prompt(reply)
    payload = parse_judge_response(await backend.complete(system, user))
    entry = payload["identity"]
    return float(entry["score"]) >= IDENTITY_THRESHOLD, str(entry.get("evidence", ""))


def judge_backend_from_env() -> LiteLLMJudgeBackend | None:
    """The judge backend for the live sweep, or None when the API key the
    judge's model actually needs is not configured — the caller skips.
    The phraser's key alone must NOT light up the judge: the default
    judge model is anthropic, and building a backend without its own key
    fails at call time instead of skipping."""
    model = os.environ.get("AGENTG_JUDGE_MODEL") or DEFAULT_JUDGE_MODEL
    if model.startswith("anthropic/"):
        key = os.environ.get("ANTHROPIC_API_KEY")
    else:
        key = os.environ.get("MODEL_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return LiteLLMJudgeBackend(model=model, api_key=key)


def live_enabled() -> bool:
    flag = os.environ.get("AGENTG_BEHAVIORAL_LIVE", "").strip().lower()
    return flag in {"1", "true", "yes", "on"}
