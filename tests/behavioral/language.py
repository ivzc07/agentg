"""Deterministic stray-English gate for Spanish conversations (issue #67).

The live judge scores language consistency; this offline lexicon is the CI
gate: English training vocabulary with an everyday Spanish equivalent must
never surface in a Spanish reply. Exercise catalog names are English by
design and deliberately absent from the lexicon — the carve-out in the
Agent's language rule covers them and nothing else. Allowlisted exercise-name
shapes are exempt too ("Muscle-up", "strength band pull-apart"), but a hit
next to one ("muscle pull-ups") or spelled with hyphens ("weight-loss")
still flags.

Ambiguity policy: name-internal tokens are clean; anything adjacent-but-
outside flags. "strength" is name-internal only when it STARTS a name's
modifier chain — preceded by nothing, by prose (a Spanish word), or by
punctuation. Preceded by a name-modifier ("kipping strength band
pull-apart") or by lexicon goal vocabulary ("stamina strength band
pull-apart"), it is mid-chain vocabulary and flags. A non-modifier word
glued INSIDE a hyphen chain ("toca-strength-band-pull-apart") breaks the
name shape, so the chain's lexicon parts are adjacent-outside and flag.
True hyphens (U+2010-U+2012, U+2212) and NBSP are normalized to their ASCII
twins before any matching; en/em/horizontal-bar dashes (U+2013-U+2015) are
separators, never chain links — they split tokens and block absorption,
with one exception: an allowlisted name core may be recognized across the
gap ("Muscle–up" == "Muscle-up").
"""

from __future__ import annotations

import re

# General training/goal vocabulary — never exercise names (those stay in
# their catalog form). tests/behavioral/test_language.py pins that no
# catalog name or alias collides with this list.
ENGLISH_TRAINING_VOCAB: tuple[str, ...] = (
    "muscle",
    "muscles",
    "strength",
    "fat",
    "fat loss",
    "weight loss",
    "conditioning",
    "endurance",
    "stamina",
    "bulking",
    "cutting",
    "lean",
    "hypertrophy",
)

def _compile(term: str) -> re.Pattern[str]:
    # Multi-word terms match any run of whitespace, hyphens, or dash-derived
    # gaps between words, so "weight loss" catches "weight-loss" and
    # "weight–loss", doubled spaces, tabs, newlines.
    return re.compile(
        r"\b" + rf"[\s\-{_DASH_GAP}]+".join(re.escape(part) for part in term.split()) + r"\b",
        re.IGNORECASE,
    )


# Dash polarity (round-16): TRUE hyphens (U+2010, U+2011, U+2012, U+2212)
# map to ASCII "-" and glue like it. EN/EM/HORIZONTAL-BAR dashes
# (U+2013/U+2014/U+2015) are SEPARATORS, never chain links: they map to a
# private placeholder that splits tokens and blocks chain absorption, with
# one exception -- an allowlisted name core may be recognized across the
# gap ("Muscle–up" == "Muscle-up"). NBSP maps to a plain space. All
# one-code-point-for-one, so offsets are unchanged.
_DASH_GAP = ""

_NORMALIZE = str.maketrans(
    {cp: "-" for cp in "‐‑‒−"}
    | {cp: _DASH_GAP for cp in "–—―"}
    | {" ": " "}
)

_PATTERNS = {term: _compile(term) for term in ENGLISH_TRAINING_VOCAB}

_LETTER = r"A-Za-zÀ-ÖØ-öø-ÿ"
_TOKEN_RE = re.compile(rf"[{_LETTER}]+(?:-[{_LETTER}]+)*")

# Hyphenated exercise-name shapes the catalog carve-out covers
# (case-insensitive, optional plural "s" on the final particle). Anything
# particle-bearing but not listed here ("lean-out", "bulking-up") is NOT a
# name — its lexicon parts flag normally. Adding a catalog name is one line.
EXERCISE_NAME_CORES: frozenset[str] = frozenset(
    {
        "pull-up",
        "push-up",
        "chin-up",
        "sit-up",
        "step-up",
        "muscle-up",
        "pull-apart",
        "pull-over",
        "pull-through",
    }
)

# Equipment modifiers that may immediately precede a core ("band
# pull-apart"). "strength" counts only directly before one of these
# ("strength band pull-apart") — never alone next to a core.
_EQUIPMENT_MODIFIERS = frozenset({"band", "bar", "ring", "cable"})

# Variant words allowed in a hyphen-joined prefix chain ("kipping-muscle-up").
_VARIANT_WORDS = frozenset({"kipping", "strict", "weighted", "banded"})


def _equipment_base(word: str) -> str:
    lowered = word.lower()
    return lowered[:-1] if lowered.endswith("s") else lowered  # plural ok


def _is_equipment(word: str) -> bool:
    """The ONE equipment-modifier predicate, shared by the space path and the
    hyphen path."""
    return _equipment_base(word) in _EQUIPMENT_MODIFIERS


def _leads_with_equipment(token: str) -> bool:
    # Plain equipment token, or a hyphen token whose first part is equipment
    # ("band-pull-apart" counts as equipment-led for the strength check).
    return _is_equipment(token.split("-", 1)[0])


def _is_allowed_prefix(parts: list[str]) -> bool:
    # Every prefix part must be an equipment modifier or variant word;
    # "strength" only as the FIRST part of a leading strength+equipment unit
    # ("strength-band-pull-apart") — mid-chain strength ("band-strength-
    # band-pull-apart") is laundered vocabulary and flags, as is any other
    # lexicon term glued to a core ("muscle-pull-up").
    for i, part in enumerate(parts):
        if _is_equipment(part) or part in _VARIANT_WORDS:
            continue
        following = parts[i + 1] if i + 1 < len(parts) else ""
        if part == "strength" and i == 0 and _is_equipment(following):
            continue
        return False
    return True


def _match_name_core(token: str) -> tuple[str, str] | None:
    # (form, core) for an exact allowlist match, or the allowlisted core as a
    # hyphen SUFFIX of a longer token whose prefix parts are all
    # equipment/variant words ("bar-muscle-up", "kipping-muscle-up",
    # "strength-band-pull-apart"); plural "s" allowed. None if not a core.
    lowered = token.lower()
    forms = [lowered, lowered[:-1]] if lowered.endswith("s") else [lowered]
    for form in forms:
        if form in EXERCISE_NAME_CORES:
            return form, form
        for core in EXERCISE_NAME_CORES:
            if form.endswith(f"-{core}") and _is_allowed_prefix(form[: -len(core) - 1].split("-")):
                return form, core
    return None


def _absorbable(word: str, following: str) -> bool:
    # A token the space path may absorb before a core: "strength" when the
    # following token leads with equipment (plain "band" or equipment-led
    # "band-pull-apart"), or any token whose own hyphen parts form an
    # allowed prefix chain — equipment, variant words, glued
    # "strength-band(s)", multi-part "strength-band-bar" — the same rule the
    # hyphen path applies to core prefixes.
    if word == "strength" and _leads_with_equipment(following):
        return True
    return _is_allowed_prefix(word.split("-"))


_HORIZONTAL_GAP_RE = re.compile(r"[ \t]+")


def _hyphen_parts(start: int, token_text: str) -> list[tuple[int, int, str]]:
    """(start, end, word) per hyphen-separated part of a token."""
    parts = []
    offset = 0
    for part in token_text.split("-"):
        parts.append((start + offset, start + offset + len(part), part.lower()))
        offset += len(part) + 1
    return parts


def _build_units(text: str) -> list[tuple[int, int, str, list[tuple[int, int, str]]]]:
    """Matchable units: tokens, plus token pairs joined across ONE
    dash-derived gap when — and only when — the joined form is an EXACT
    allowlisted core ("Muscle–up"). Prefix+core joins are not names:
    "strength–band-pull-apart" stays two units and strength flags. Chain
    links never cross the gap, and prose never merges ("toca—Muscle-up" is
    not a core).
    """
    tokens = list(_TOKEN_RE.finditer(text))
    units = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        joined = tok.group() + "-" + tokens[i + 1].group() if i + 1 < len(tokens) else ""
        matched = _match_name_core(joined) if joined else None
        if (
            i + 1 < len(tokens)
            and text[tok.end() : tokens[i + 1].start()] == _DASH_GAP
            and matched is not None
            and matched[0] == matched[1]  # exact core only, no prefix chains
        ):
            nxt = tokens[i + 1]
            units.append(
                (
                    tok.start(),
                    nxt.end(),
                    tok.group() + "-" + nxt.group(),
                    _hyphen_parts(tok.start(), tok.group())
                    + _hyphen_parts(nxt.start(), nxt.group()),
                )
            )
            i += 2
        else:
            units.append(
                (tok.start(), tok.end(), tok.group(), _hyphen_parts(tok.start(), tok.group()))
            )
            i += 1
    return units


def _lexicon_hit_overlaps(
    hits: list[tuple[str, int, int]], start: int, end: int
) -> list[tuple[str, int, int]]:
    # Lexicon hits intersecting the token's span — exact ends ("weight
    # loss") and prefix-shaped compounds ("muscle-gain", "stamina-focused")
    # alike.
    return [hit for hit in hits if hit[1] < end and hit[2] > start]


def _exercise_name_spans(text: str, hits: list[tuple[str, int, int]]) -> list[tuple[int, int]]:
    """Spans the catalog carve-out covers: an allowlisted name core plus the
    modifier tokens immediately preceding it. The walk is bounded by the
    modifier rule itself, not a count; no right-side absorption, no generic
    neighbor walking, and gaps are spaces/tabs only — a name mention broken
    across lines or separator characters is not one name.

    The mid-chain strength discriminator runs on ONE uniform part sequence
    (absorbed tokens and the core token's own glued prefix, all split into
    hyphen parts), so glued forms and multi-word lexicon prefixes can't
    dodge it: "strength" is clean only as the FIRST chain part, preceded by
    nothing, prose, or punctuation; any preceding chain content — modifier
    parts or an adjacent lexicon hit — makes it mid-chain, and the span
    starts after it."""
    units = _build_units(text)
    # Pass 1: chains and mid-chain-strength cuts (position only).
    entries: list[dict] = []
    for i, (unit_start, unit_end, match_text, unit_parts) in enumerate(units):
        matched = _match_name_core(match_text)
        if matched is None:
            continue
        form, core = matched
        start = unit_start
        first = i  # unit index where the modifier chain starts
        k = i - 1
        while k >= 0 and _HORIZONTAL_GAP_RE.fullmatch(text[units[k][1] : start]):
            word = units[k][2].lower()
            following = units[k + 1][2].lower()
            if not _absorbable(word, following):
                break
            # A dash-separated prefix blocks the strength absorb too
            # ("non–strength band pull-apart" mirrors ASCII gluing).
            if (
                word == "strength"
                and k > 0
                and text[units[k - 1][1] : units[k][0]] == _DASH_GAP
            ):
                break
            start = units[k][0]
            first = k
            k -= 1
        # One uniform part sequence: absorbed units' hyphen parts, then the
        # core unit's own glued prefix parts (all but the core's own parts).
        parts: list[tuple[int, int, str]] = []
        for j in range(first, i):
            parts.extend(units[j][3])
        if form != core:
            core_part_count = core.count("-") + 1
            parts.extend(unit_parts[: len(unit_parts) - core_part_count])
        # Strength is clean only as the FIRST chain part; cut the span after
        # the last mid-chain strength (preceded by modifier parts).
        leading_strength = bool(parts) and parts[0][2] == "strength"
        cut = None
        for idx, (_, _, word) in enumerate(parts):
            if word == "strength" and idx > 0:
                cut = idx
        if cut is not None:
            leading_strength = False
            start = parts[cut + 1][0] if cut + 1 < len(parts) else parts[cut][1]
        entries.append(
            {
                "start": start,
                "end": unit_end,
                "first": first,
                "parts": parts,
                "leading_strength": leading_strength,
            }
        )
    base_spans = [(entry["start"], entry["end"]) for entry in entries]
    # Pass 2: a chain-leading strength preceded by a SURVIVING lexicon hit —
    # one no name span exempts — overlapping the previous unit is mid-chain
    # too ("stamina", "weight loss", "muscle-gain"). A name-internal hit
    # ("muscle" inside "muscle-up") is not goal vocab and marks nothing.
    for entry in entries:
        if not entry["leading_strength"] or entry["first"] == 0:
            continue
        prev_idx = entry["first"] - 1
        if not _HORIZONTAL_GAP_RE.fullmatch(text[units[prev_idx][1] : units[entry["first"]][0]]):
            continue
        # The preceding content is the whole dash-connected run before the
        # chain ("muscle–gain" marks strength mid-chain like ASCII
        # "muscle-gain" does).
        run_idx = prev_idx
        while run_idx > 0 and text[units[run_idx - 1][1] : units[run_idx][0]] == _DASH_GAP:
            run_idx -= 1
        overlapping = _lexicon_hit_overlaps(hits, units[run_idx][0], units[prev_idx][1])
        survives = [
            hit
            for hit in overlapping
            if not any(span_start <= hit[1] and hit[2] <= span_end for span_start, span_end in base_spans)
        ]
        if survives:
            parts = entry["parts"]
            entry["start"] = parts[1][0] if len(parts) > 1 else parts[0][1]
    return [(entry["start"], entry["end"]) for entry in entries]


def find_english_leaks(reply: str) -> set[str]:
    """English training vocabulary found in a reply — empty means clean."""
    text = reply.translate(_NORMALIZE)  # dashes/NBSP -> ASCII, offsets kept
    hits = [
        (term, match.start(), match.end())
        for term, pattern in _PATTERNS.items()
        for match in pattern.finditer(text)
    ]
    names = _exercise_name_spans(text, hits)
    return {
        term
        for term, start, end in hits
        if not any(span_start <= start and end <= span_end for span_start, span_end in names)
    }
