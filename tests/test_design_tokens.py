"""Design token contrast validation — WCAG AA (>= 4.5:1).

Validates every foreground-ink / background-surface pair defined in the
Tailwind config against the WCAG 2.1 AA normal-text contrast minimum.
This is the automated replacement for the deleted
``test_redesign_acceptance_sweep.py``; its token-contrast gate lives here
while layout / reduced-motion / swap gates are covered by Playwright
(``test_375px_playwright.py``) and the frontend RTL suite.

Issue #156 (``--ink-3`` on ``--elevation-3-bg`` 4.17:1 → 4.59:1 after fix)
is the canary — it proved a single value change can silently drop below AA.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
TAILWIND_CONFIG = FRONTEND / "tailwind.config.ts"
AA_MINIMUM = 4.5


# ---------------------------------------------------------------------------
# WCAG 2.1 relative luminance & contrast
# ---------------------------------------------------------------------------


def _linearize(c: float) -> float:
    """Convert a single sRGB channel (0–1) to linear space."""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _expand_hex(short: str) -> str:
    """Normalise shorthand ``#rgb`` to ``#rrggbb``."""
    assert short.startswith("#"), short
    if len(short) == 7:
        return short
    if len(short) == 4:
        return f"#{short[1]}{short[1]}{short[2]}{short[2]}{short[3]}{short[3]}"
    raise AssertionError(f"Unhandled hex format: {short}")


def _relative_luminance(hex_color: str) -> float:
    """WCAG 2.1 relative luminance for an sRGB ``#rrggbb`` hex string."""
    expanded = _expand_hex(hex_color)
    r = int(expanded[1:3], 16) / 255.0
    g = int(expanded[3:5], 16) / 255.0
    b = int(expanded[5:7], 16) / 255.0
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.1 contrast ratio between two ``#rrggbb`` colours.

    Order of arguments does not matter.
    """
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------------------
# Comment stripping (line + block) — keep the regex parser honest
# ---------------------------------------------------------------------------

_TS_LINE_COMMENT = re.compile(r"//[^\n]*")
_TS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(source: str) -> str:
    """Remove JavaScript/TypeScript line and block comments.

    Stripping before extraction means a commented-out old ``ink``,
    ``bg``, or ``"elevation-N"`` block cannot satisfy the parser after
    the real token is removed — the token must be live in the config.
    """
    stripped = _TS_BLOCK_COMMENT.sub("", source)
    stripped = _TS_LINE_COMMENT.sub("", stripped)
    return stripped


# ---------------------------------------------------------------------------
# Token extraction from tailwind.config.ts
# ---------------------------------------------------------------------------


def _extract_nested_hex(block: str, key: str) -> str | None:
    """Return the DEFAULT hex inside a nested config block like::

        "elevation-3": {
          DEFAULT: "#242528",
          stroke: "#4a4b4e",
        },
    """
    # Match the entire nested block for *key*
    pat = re.compile(
        r'"' + re.escape(key) + r'"\s*:\s*\{(.*?)\}',
        re.DOTALL,
    )
    m = pat.search(block)
    if not m:
        return None
    inner = m.group(1)
    default_m = re.search(r'DEFAULT\s*:\s*"([^"]*)"', inner)
    return default_m.group(1) if default_m else None


def _load_token_pairs(
    raw: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Parse ``frontend/tailwind.config.ts`` and return (inks, surfaces).

    Inks are keyed as ``"ink"``, ``"ink-2"``, ``"ink-3"`` — all parsed
    from the unquoted ``ink`` block, **fail-closed**: any missing or
    malformed required token raises ``AssertionError``.

    Surfaces are keyed as ``"elevation-0"`` … ``"elevation-3"`` plus
    ``"bg"`` (the page-level background used with text in Shell.tsx).
    """
    if raw is None:
        raw = TAILWIND_CONFIG.read_text(encoding="utf-8")

    raw = _strip_comments(raw)

    # --- inks: all parsed from the unquoted ink block, fail-closed ---
    ink_block_m = re.search(r'ink\s*:\s*\{(.*?)\}', raw, re.DOTALL)
    assert ink_block_m is not None, (
        "Could not parse ink block from tailwind.config.ts"
    )
    ink_block = ink_block_m.group(1)

    inks: dict[str, str] = {}
    for token_key, pattern_key in [
        ("ink", "DEFAULT"),
        ("ink-2", "2"),
        ("ink-3", "3"),
    ]:
        m = re.search(
            r'\b' + re.escape(pattern_key) + r'\s*:\s*"([^"]*)"',
            ink_block,
        )
        assert m is not None, (
            f"Could not parse ink.{pattern_key} from tailwind.config.ts"
        )
        inks[token_key] = m.group(1)

    # --- surfaces: elevations + bg ---
    surfaces: dict[str, str] = {}

    for i in range(4):
        key = f"elevation-{i}"
        hex_val = _extract_nested_hex(raw, key)
        assert hex_val is not None, (
            f"Could not parse {key} from tailwind.config.ts"
        )
        surfaces[key] = hex_val

    bg_m = re.search(r'\bbg\s*:\s*"([^"]*)"', raw)
    assert bg_m is not None, "Could not parse bg from tailwind.config.ts"
    surfaces["bg"] = bg_m.group(1)

    return inks, surfaces


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def token_pairs() -> tuple[dict[str, str], dict[str, str]]:
    return _load_token_pairs()


def test_tailwind_config_exists() -> None:
    """Prevent the test from silently passing when the file moves."""
    assert TAILWIND_CONFIG.is_file(), f"Tailwind config not found at {TAILWIND_CONFIG}"


def test_all_ink_surface_pairs_pass_aa(
    token_pairs: tuple[dict[str, str], dict[str, str]],
) -> None:
    """Every ink / surface pair must satisfy WCAG AA (≥ 4.5:1).

    This is the automated regression gate for issues like #156: if a token
    value drifts below AA the test flags it immediately.  Surfaces include
    all elevation levels plus the page-level ``bg`` token.
    """
    inks, surfaces = token_pairs

    failures: list[str] = []
    for ink_name, ink_hex in inks.items():
        for surf_name, surf_hex in surfaces.items():
            cr = contrast_ratio(ink_hex, surf_hex)
            if cr < AA_MINIMUM:
                failures.append(
                    f"{ink_name} ({ink_hex}) on {surf_name} ({surf_hex}): "
                    f"{cr:.2f}:1 < {AA_MINIMUM}:1"
                )

    assert not failures, (
        f"{len(failures)} token pair(s) below AA minimum:\n"
        + "\n".join(failures)
    )


def test_ink3_on_elevation3_is_the_regression_canary(
    token_pairs: tuple[dict[str, str], dict[str, str]],
) -> None:
    """Direct regression test for issue #156 — the exact pair that failed."""
    inks, surfaces = token_pairs
    cr = contrast_ratio(inks["ink-3"], surfaces["elevation-3"])
    assert cr >= AA_MINIMUM, (
        f"ink-3 ({inks['ink-3']}) on elevation-3 ({surfaces['elevation-3']}) "
        f"= {cr:.2f}:1 < {AA_MINIMUM}:1 — #156 regression"
    )


def test_elevation_scale_is_monotonic(
    token_pairs: tuple[dict[str, str], dict[str, str]],
) -> None:
    """Higher elevation numbers must be lighter (higher luminance).

    The design intent: elevation-0 is the ground (darkest), elevation-3 is
    the most elevated surface (lightest).
    """
    _, surfaces = token_pairs
    elevations = {
        k: v for k, v in surfaces.items() if k.startswith("elevation-")
    }
    lums = {
        name: _relative_luminance(hex_val)
        for name, hex_val in elevations.items()
    }
    for i in range(3):
        lo, hi = f"elevation-{i}", f"elevation-{i + 1}"
        assert lums[lo] < lums[hi], (
            f"Elevation scale broken: {lo} ({lums[lo]:.5f}) >= {hi} ({lums[hi]:.5f})"
        )


# ---------------------------------------------------------------------------
# P1 regression — fail-closed ink parsing (no silent fallbacks)
# ---------------------------------------------------------------------------

_VALID_SKELETON = '''
    ink: { DEFAULT: "#fff", 2: "#9a9a9a", 3: "#85858a" }
    bg: "#000"
    "elevation-0": { DEFAULT: "#000", stroke: "#2a2b2d" }
    "elevation-1": { DEFAULT: "#131313", stroke: "#2a2b2d" }
    "elevation-2": { DEFAULT: "#1b1c1e", stroke: "#3a3a3c" }
    "elevation-3": { DEFAULT: "#1c1d1f", stroke: "#4a4b4e" }
'''


def test_synthetic_skeleton_parses() -> None:
    """The synthetic config skeleton is valid — guard against test-rot."""
    inks, surfaces = _load_token_pairs(_VALID_SKELETON)
    assert inks == {"ink": "#fff", "ink-2": "#9a9a9a", "ink-3": "#85858a"}
    assert surfaces["bg"] == "#000"
    assert surfaces["elevation-0"] == "#000"


def test_missing_ink_block_fails_closed() -> None:
    """Parser must raise when the ink block is absent."""
    with pytest.raises(AssertionError, match="Could not parse ink block"):
        _load_token_pairs("{}")


def test_missing_ink_default_fails_closed() -> None:
    """Parser must raise when ink.DEFAULT is missing."""
    no_default = _VALID_SKELETON.replace('DEFAULT: "#fff",', "")
    with pytest.raises(AssertionError, match="ink.DEFAULT"):
        _load_token_pairs(no_default)


def test_missing_ink_2_fails_closed() -> None:
    """Parser must raise when ink.2 is missing."""
    no_2 = _VALID_SKELETON.replace('2: "#9a9a9a",', "")
    with pytest.raises(AssertionError, match="ink.2"):
        _load_token_pairs(no_2)


def test_missing_ink_3_fails_closed() -> None:
    """Parser must raise when ink.3 is missing."""
    no_3 = _VALID_SKELETON.replace('3: "#85858a"', "")
    with pytest.raises(AssertionError, match="ink.3"):
        _load_token_pairs(no_3)


# ---------------------------------------------------------------------------
# P2 regression — commented-out tokens must not satisfy the parser
# ---------------------------------------------------------------------------


def test_commented_out_ink_block_does_not_satisfy() -> None:
    """A //-commented ink block must not satisfy extraction.

    If every ink key in the live block is deleted but a commented-out
    old block remains, the parser must still fail closed.
    """
    config = """
    // ink: { DEFAULT: "#fff", 2: "#9a9a9a", 3: "#85858a" }
    ink: { DEFAULT: "#eee" }
    bg: "#000"
    "elevation-0": { DEFAULT: "#000", stroke: "#2a2b2d" }
    "elevation-1": { DEFAULT: "#131313", stroke: "#2a2b2d" }
    "elevation-2": { DEFAULT: "#1b1c1e", stroke: "#3a3a3c" }
    "elevation-3": { DEFAULT: "#1c1d1f", stroke: "#4a4b4e" }
    """
    # ink.2 and ink.3 are gone; the commented-out block must NOT supply them.
    with pytest.raises(AssertionError, match="ink.2"):
        _load_token_pairs(config)


def test_commented_out_bg_does_not_satisfy() -> None:
    """A //-commented bg must not satisfy extraction."""
    config = """
    ink: { DEFAULT: "#fff", 2: "#9a9a9a", 3: "#85858a" }
    // bg: "#000"
    "elevation-0": { DEFAULT: "#000", stroke: "#2a2b2d" }
    "elevation-1": { DEFAULT: "#131313", stroke: "#2a2b2d" }
    "elevation-2": { DEFAULT: "#1b1c1e", stroke: "#3a3a3c" }
    "elevation-3": { DEFAULT: "#1c1d1f", stroke: "#4a4b4e" }
    """
    with pytest.raises(AssertionError, match="Could not parse bg"):
        _load_token_pairs(config)


def test_block_commented_tokens_do_not_satisfy() -> None:
    """/* ... */ commented tokens must not satisfy extraction."""
    config = """
    ink: { DEFAULT: "#fff", 2: "#9a9a9a", 3: "#85858a" }
    /* bg: "#000" */
    "elevation-0": { DEFAULT: "#000", stroke: "#2a2b2d" }
    "elevation-1": { DEFAULT: "#131313", stroke: "#2a2b2d" }
    "elevation-2": { DEFAULT: "#1b1c1e", stroke: "#3a3a3c" }
    "elevation-3": { DEFAULT: "#1c1d1f", stroke: "#4a4b4e" }
    """
    with pytest.raises(AssertionError, match="Could not parse bg"):
        _load_token_pairs(config)


def test_commented_out_elevation_block_does_not_satisfy() -> None:
    """A //-commented elevation-N block must not satisfy extraction."""
    config = """
    ink: { DEFAULT: "#fff", 2: "#9a9a9a", 3: "#85858a" }
    bg: "#000"
    "elevation-0": { DEFAULT: "#000", stroke: "#2a2b2d" }
    // "elevation-1": { DEFAULT: "#131313", stroke: "#2a2b2d" }
    "elevation-2": { DEFAULT: "#1b1c1e", stroke: "#3a3a3c" }
    "elevation-3": { DEFAULT: "#1c1d1f", stroke: "#4a4b4e" }
    """
    with pytest.raises(AssertionError, match="elevation-1"):
        _load_token_pairs(config)


# ---------------------------------------------------------------------------
# P2 regression — bg surface included in contrast matrix
# ---------------------------------------------------------------------------


def test_bg_is_in_surface_audit(
    token_pairs: tuple[dict[str, str], dict[str, str]],
) -> None:
    """bg from tailwind.config.ts must be included as a surface.

    The exact hex value is not locked — the meaningful gate is that bg
    participates in the contrast matrix so an inaccessible value is caught.
    """
    _, surfaces = token_pairs
    assert "bg" in surfaces, "bg surface not extracted from tailwind.config.ts"
    # Sanity: parsed bg must look like a hex colour.
    assert re.match(r"^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$", surfaces["bg"]), (
        f"bg surface is not a hex colour: {surfaces['bg']}"
    )


def test_missing_bg_fails_closed() -> None:
    """Parser must raise when the bg token is absent."""
    no_bg = _VALID_SKELETON.replace('bg: "#000"', "")
    with pytest.raises(AssertionError, match="Could not parse bg"):
        _load_token_pairs(no_bg)


def test_non_black_bg_passes_when_accessible() -> None:
    """A non-#000 bg with sufficient ink contrast must pass the matrix."""
    config = """
    ink: { DEFAULT: "#111", 2: "#222", 3: "#333" }
    bg: "#fff"
    "elevation-0": { DEFAULT: "#f0f0f0", stroke: "#ccc" }
    "elevation-1": { DEFAULT: "#e8e8e8", stroke: "#bbb" }
    "elevation-2": { DEFAULT: "#e0e0e0", stroke: "#aaa" }
    "elevation-3": { DEFAULT: "#d8d8d8", stroke: "#999" }
    """
    inks, surfaces = _load_token_pairs(config)
    assert surfaces["bg"] == "#fff"
    # All pairs must clear AA (dark ink on light bg).
    for ink_hex in inks.values():
        for surf_hex in surfaces.values():
            assert contrast_ratio(ink_hex, surf_hex) >= AA_MINIMUM


def test_inaccessible_bg_is_caught_by_matrix() -> None:
    """A bg that is too close to ink must fail the contrast sweep."""
    config = """
    ink: { DEFAULT: "#444", 2: "#555", 3: "#666" }
    bg: "#333"
    "elevation-0": { DEFAULT: "#222", stroke: "#111" }
    "elevation-1": { DEFAULT: "#2a2a2a", stroke: "#1a1a1a" }
    "elevation-2": { DEFAULT: "#333", stroke: "#222" }
    "elevation-3": { DEFAULT: "#3a3a3a", stroke: "#2a2a2a" }
    """
    inks, surfaces = _load_token_pairs(config)
    assert surfaces["bg"] == "#333"
    # At least one pair must fail AA — because ink.DEFAULT (#444) on
    # bg (#333) yields ~2.1:1, well under 4.5.
    failures: list[str] = []
    for ink_name, ink_hex in inks.items():
        for surf_name, surf_hex in surfaces.items():
            cr = contrast_ratio(ink_hex, surf_hex)
            if cr < AA_MINIMUM:
                failures.append(
                    f"{ink_name} ({ink_hex}) on {surf_name} ({surf_hex}): "
                    f"{cr:.2f}:1 < {AA_MINIMUM}:1"
                )
    assert failures, "Expected at least one failing pair but none failed AA"
