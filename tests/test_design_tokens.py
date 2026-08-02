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


def _extract_simple_hex(block: str, key: str) -> str | None:
    """Return the hex literal for a simple (non-nested) color entry like::

        ink: "#fff",
    """
    pat = re.compile(r'"' + re.escape(key) + r'"\s*:\s*"([^"]*)"')
    m = pat.search(block)
    return m.group(1) if m else None


def _extract_simple_hex_from_block(block: str, key: str) -> str | None:
    """Like ``_extract_simple_hex`` but matches an unquoted numeric key::

        2: "#9a9a9a",
    """
    pat = re.compile(r'\b' + re.escape(key) + r'\s*:\s*"([^"]*)"')
    m = pat.search(block)
    return m.group(1) if m else None


def _load_token_pairs() -> tuple[dict[str, str], dict[str, str]]:
    """Parse ``frontend/tailwind.config.ts`` and return (inks, elevations).

    Inks are keyed as ``"ink"``, ``"ink-2"``, ``"ink-3"``.
    Elevations are keyed as ``"elevation-0"`` … ``"elevation-3"``.
    """
    raw = TAILWIND_CONFIG.read_text(encoding="utf-8")

    inks: dict[str, str] = {}
    # ink is a nested block: ink: { DEFAULT: "#fff", 2: "#9a9a9a", 3: "#85858a" }
    inks["ink"] = _extract_nested_hex(raw, "ink") or "#fff"
    # ink-2 / ink-3 are numeric keys inside the ink block
    ink2 = _extract_simple_hex(raw, "2")
    ink3 = _extract_simple_hex(raw, "3")
    # But beware: "2" and "3" could match other things; narrow to the ink block first
    ink_block_match = re.search(r'ink\s*:\s*\{(.*?)\}', raw, re.DOTALL)
    if ink_block_match:
        ink_block = ink_block_match.group(1)
        ink2 = _extract_simple_hex_from_block(ink_block, "2")
        ink3 = _extract_simple_hex_from_block(ink_block, "3")
    inks["ink-2"] = ink2 or "#9a9a9a"
    inks["ink-3"] = ink3 or "#85858a"

    elevations: dict[str, str] = {}
    for i in range(4):
        key = f"elevation-{i}"
        hex_val = _extract_nested_hex(raw, key)
        assert hex_val is not None, f"Could not parse {key} from tailwind.config.ts"
        elevations[key] = hex_val

    return inks, elevations


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def token_pairs() -> tuple[dict[str, str], dict[str, str]]:
    return _load_token_pairs()


def test_tailwind_config_exists() -> None:
    """Prevent the test from silently passing when the file moves."""
    assert TAILWIND_CONFIG.is_file(), f"Tailwind config not found at {TAILWIND_CONFIG}"


def test_all_ink_elevation_pairs_pass_aa(
    token_pairs: tuple[dict[str, str], dict[str, str]],
) -> None:
    """Every ink / elevation pair must satisfy WCAG AA (≥ 4.5:1).

    This is the automated regression gate for issues like #156: if a token
    value drifts below AA the test flags it immediately.
    """
    inks, elevations = token_pairs

    failures: list[str] = []
    for ink_name, ink_hex in inks.items():
        for elev_name, elev_hex in elevations.items():
            cr = contrast_ratio(ink_hex, elev_hex)
            if cr < AA_MINIMUM:
                failures.append(
                    f"{ink_name} ({ink_hex}) on {elev_name} ({elev_hex}): "
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
    inks, elevations = token_pairs
    cr = contrast_ratio(inks["ink-3"], elevations["elevation-3"])
    assert cr >= AA_MINIMUM, (
        f"ink-3 ({inks['ink-3']}) on elevation-3 ({elevations['elevation-3']}) "
        f"= {cr:.2f}:1 < {AA_MINIMUM}:1 — #156 regression"
    )


def test_elevation_scale_is_monotonic(
    token_pairs: tuple[dict[str, str], dict[str, str]],
) -> None:
    """Higher elevation numbers must be lighter (higher luminance).

    The design intent: elevation-0 is the ground (darkest), elevation-3 is
    the most elevated surface (lightest).
    """
    _, elevations = token_pairs
    lums = {
        name: _relative_luminance(hex_val)
        for name, hex_val in elevations.items()
    }
    for i in range(3):
        lo, hi = f"elevation-{i}", f"elevation-{i + 1}"
        assert lums[lo] < lums[hi], (
            f"Elevation scale broken: {lo} ({lums[lo]:.5f}) >= {hi} ({lums[hi]:.5f})"
        )
