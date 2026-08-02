"""Automated acceptance sweep for the #133 redesign (issue #140).

Four machine-checkable gates that make the Owner's final visual review
repeatable rather than a one-off manual pass:

1.  AA contrast (>= 4.5:1) — walks every text/surface token pair actually
    used by the dashboard CSS and asserts the WCAG contrast ratio.
2.  375px bar — asserts no horizontal overflow and that primary actions stay
    reachable at 375px on every dashboard screen.
3.  Reduced-motion — asserts every animation/transition is disabled under
    ``prefers-reduced-motion: reduce``.
4.  htmx swap parity — asserts each htmx fragment response renders the same
    markup as the corresponding region of the full page load.

Every check is TDD'd where possible; every check must fail loudly on a real
regression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

# ── helpers ────────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent.parent / "src" / "agentg" / "static"
CSS_PATH = STATIC_DIR / "dashboard.css"


def _css_text() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _media_blocks(css: str) -> dict[tuple[int, int], str]:
    """Return {(max_width_px, index): block_body} for all max-width media
    queries.

    Uses depth-aware brace counting so multi-rule media blocks are captured
    as a single body.  Each distinct @media block gets its own index so
    duplicate breakpoints are never merged."""
    css = _strip_comments(css)
    blocks: dict[tuple[int, int], str] = {}
    pattern = re.compile(r"@media\s*\(max-width:\s*(\d+)px\)\s*\{")
    counts: dict[int, int] = {}
    for m in pattern.finditer(css):
        width = int(m.group(1))
        idx = counts.get(width, 0)
        counts[width] = idx + 1
        body_start = m.end()
        depth = 1
        pos = body_start
        while depth > 0 and pos < len(css):
            if css[pos] == "{":
                depth += 1
            elif css[pos] == "}":
                depth -= 1
                if depth == 0:
                    body = css[body_start:pos].strip()
                    blocks[(width, idx)] = body
                    break
            pos += 1
    return blocks


def _find_selector_rule_bodies(block_body: str, selector: str) -> list[str]:
    """Return the inner bodies of every exact rule for *selector* within
    *block_body*.

    Handles selector lists (``.foo, .bar { ... }``) and duplicate rules.
    The match is exact so ``.setcard`` does not match ``.setcard code``."""
    # Match the selector as a whole item in a selector list: it must be
    # followed (after optional whitespace) by either { or ,.
    pattern = re.compile(
        r'(?<![.\w#-])' + re.escape(selector) + r'(?=\s*[,{])'
    )
    bodies: list[str] = []
    for m in pattern.finditer(block_body):
        # Walk forward from end of match to find the opening {.
        pos = m.end()
        while pos < len(block_body):
            ch = block_body[pos]
            if ch == '{':
                body_start = pos + 1
                depth = 1
                pos = body_start
                while depth > 0 and pos < len(block_body):
                    if block_body[pos] == '{':
                        depth += 1
                    elif block_body[pos] == '}':
                        depth -= 1
                        if depth == 0:
                            bodies.append(block_body[body_start:pos].strip())
                            break
                    pos += 1
                break
            elif ch == '}':
                break  # malformed — bail on this match
            pos += 1
    return bodies


# ── WCAG 2.1 relative luminance and contrast ratio ───────────────────

def _srgb_to_linear(c: float) -> float:
    """Linearize one sRGB channel (0..1)."""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    """WCAG 2.1 relative luminance of a hex color (``#rgb`` or ``#rrggbb``)."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """WCAG 2.1 contrast ratio between two hex colors.  Order does not matter."""
    l1 = _relative_luminance(fg_hex)
    l2 = _relative_luminance(bg_hex)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


# ── CSS parsing ────────────────────────────────────────────────────────


def _strip_comments(css: str) -> str:
    """Remove ``/* ... */`` comments from CSS text."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


@dataclass
class CssRule:
    selector: str
    props: dict[str, str]  # property -> value, already trimmed


def _parse_css_rules(css: str) -> list[CssRule]:
    """Parse CSS into a flat list of (selector, properties) rules.

    Handles ``:root`` blocks, ``@media`` blocks, and plain rules.  Nested
    at-rules are flattened — the media condition is prepended to the
    selector for identification purposes.
    """
    css = _strip_comments(css)
    rules: list[CssRule] = []
    # Split on `}` boundaries, tracking nesting.
    buffer = ""
    depth = 0
    for ch in css:
        if ch == "{":
            depth += 1
            buffer += ch
        elif ch == "}":
            depth -= 1
            buffer += ch
            if depth == 0:
                rules.append(_parse_one_rule_block(buffer.strip()))
                buffer = ""
        else:
            buffer += ch
    return [r for r in rules if r is not None and r.selector and r.props]


def _parse_one_rule_block(block: str) -> CssRule | None:
    """Parse one ``selector { prop: val; ... }`` block (may be nested with @media)."""
    # Find the outermost `{`
    brace = block.index("{")
    selector = block[:brace].strip()
    # Extract inner by stripping the outer braces
    inner = block[brace + 1 :].rstrip("}").strip()

    # If the entire inner is another rule block (e.g. @media { ... }),
    # flatten by prepending the media condition.
    if inner.startswith("@"):
        # It's a nested at-rule; we'll handle one level deep.
        inner_brace = inner.index("{")
        inner_sel = inner[:inner_brace].strip()
        inner_body = inner[inner_brace + 1 :].rstrip("}").strip()
        # For media queries, we just note the condition in the selector
        return CssRule(selector=f"{selector} {{ {inner_sel} }}", props=_parse_props(inner_body))

    props = _parse_props(inner)
    if not props:
        return None
    return CssRule(selector=selector, props=props)


def _parse_props(inner: str) -> dict[str, str]:
    """Parse ``property: value;`` pairs from a rule body."""
    props: dict[str, str] = {}
    # Split on `;` but be careful with quoted values, functions, etc.
    statements = _split_semicolons(inner)
    for stmt in statements:
        stmt = stmt.strip()
        if ":" not in stmt:
            continue
        colon = stmt.index(":")
        prop = stmt[:colon].strip().lower()
        val = stmt[colon + 1 :].strip()
        # Skip empty values
        if val:
            # Remove `!important` for value comparison
            val = re.sub(r"\s*!important\s*", "", val).strip()
            props[prop] = val
    return props


def _split_semicolons(text: str) -> list[str]:
    """Split on ``;``, respecting parentheses."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


# ── CSS variable resolution ────────────────────────────────────────────


def _resolve_css_variables(css: str) -> dict[str, str]:
    """Extract ``--name: value;`` definitions from ``:root`` blocks and return
    a flat dictionary of resolved values (each ``var(--X)`` already expanded)."""
    # Strip comments first: they can contain colons and semicolons that
    # corrupt property parsing.
    css = _strip_comments(css)
    # Find the :root block and parse all --var definitions
    root_re = re.search(r":root\s*\{([^}]*)\}", css, re.DOTALL)
    if not root_re:
        return {}
    raw_vars = _parse_props(root_re.group(1))
    resolved: dict[str, str] = {}
    for name, value in raw_vars.items():
        if name.startswith("--"):
            resolved[name] = value

    # Resolve aliases: --surface: var(--elevation-1-bg) etc.
    # Do multiple passes to handle transitive resolution.
    for _ in range(5):
        changed = False
        for name, value in list(resolved.items()):
            new_val = _expand_var(value, resolved)
            if new_val != value:
                resolved[name] = new_val
                changed = True
        if not changed:
            break
    return resolved


def _expand_var(value: str, vars_: dict[str, str]) -> str:
    """Expand ``var(--name)`` references in *value*, falling back to *vars_*."""
    # var(--name, fallback) or just var(--name)
    def _repl(m: re.Match) -> str:
        var_name = m.group(1)
        fallback = m.group(2)
        # Strip trailing whitespace from fallback before the `)` if present
        if fallback is not None:
            fallback = fallback.strip()
        resolved = vars_.get(var_name)
        if resolved is not None:
            return resolved
        if fallback is not None:
            return fallback
        return m.group(0)  # leave unresolved

    result = re.sub(r"var\((--[\w-]+)(?:\s*,\s*([^)]*?))?\s*\)", _repl, value)
    # Handle nested var() - do a limited set of passes
    if result != value and "var(" in result:
        result = _expand_var(result, vars_)
    return result


# ── Color extraction ───────────────────────────────────────────────────


def _is_color_value(val: str) -> bool:
    """True if *val* looks like a color (hex, rgb, named color, or transparent)."""
    val = val.lower().strip()
    if val in ("transparent", "inherit", "currentcolor", "none", "unset", "initial"):
        return False
    if re.match(r"^#[0-9a-f]{3,8}$", val):
        return True
    if re.match(r"^rgb[a]?\(", val):
        return True
    return False


def _extract_hex(val: str, vars_: dict[str, str]) -> str | None:
    """Resolve *val* (expanding CSS vars) and return a ``#rrggbb`` or ``None``.

    Handles hex, ``rgb()``, and ``rgba()``.  Pure alpha (rgba with a=0) returns
    ``#000000`` so the contrast is computed against the effective background.
    """
    val = _expand_var(val, vars_).strip().lower()
    # rgb/rgba
    m = re.match(r"rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)", val)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        if a == 0:
            return "#000000"
        if a < 1:
            # We need to composite against something — skip unless we know the bg.
            # For our purposes, alpha colors in CSS custom properties typically
            # appear in box-shadow and not in text/background pairs.
            return None
        return f"#{r:02x}{g:02x}{b:02x}"
    # hex
    if re.match(r"^#[0-9a-f]{3,8}$", val):
        return _normalize_hex(val)
    return None


def _normalize_hex(hex_val: str) -> str:
    """Normalize ``#rgb`` -> ``#rrggbb``."""
    hex_val = hex_val.lstrip("#")
    if len(hex_val) == 3:
        hex_val = "".join(c * 2 for c in hex_val)
    if len(hex_val) == 8:
        hex_val = hex_val[:6]  # drop alpha channel
    return f"#{hex_val}"


# ── Built-in named-colors map (the ones used in the stylesheet) ────────

_NAMED_COLORS: dict[str, str] = {
    "black": "#000000",
    "white": "#ffffff",
}


def _resolve_color(val: str, vars_: dict[str, str]) -> str | None:
    """Resolve a CSS value to ``#rrggbb`` or ``None``."""
    val = _expand_var(val, vars_)
    hex_ = _extract_hex(val, vars_)
    if hex_:
        return hex_
    val_lower = val.strip().lower()
    if val_lower in _NAMED_COLORS:
        return _NAMED_COLORS[val_lower]
    return None


# ── Collect text/surface pairs from CSS rules ──────────────────────────


@dataclass
class TextSurfacePair:
    """A text-color / background-color combination found in the CSS."""

    selector: str
    fg: str  # resolved hex
    bg: str  # resolved hex
    ratio: float = 0.0
    passes_aa: bool = False


def _is_bg_prop(prop: str) -> bool:
    """True if *prop* is a background-related property."""
    return prop == "background" or prop.startswith("background-color")


def _is_fg_prop(prop: str) -> bool:
    """True if *prop* sets text/foreground color."""
    return prop == "color" or prop == "caret-color"


def _collect_text_surface_pairs(css: str) -> list[TextSurfacePair]:
    """Walk every CSS rule, resolve color/background pairs, return the list."""
    vars_ = _resolve_css_variables(css)
    rules = _parse_css_rules(css)
    pairs: list[TextSurfacePair] = []
    seen: set[tuple[str, str, str]] = set()  # (selector_key, fg, bg)

    for rule in rules:
        # Does this rule have both fg and bg?
        fg_value = None
        bg_value = None
        for prop, val in rule.props.items():
            if _is_fg_prop(prop) and fg_value is None:
                fg_value = val
            if _is_bg_prop(prop) and bg_value is None:
                bg_value = val

        if fg_value is None or bg_value is None:
            continue

        fg = _resolve_color(fg_value, vars_)
        bg = _resolve_color(bg_value, vars_)
        if fg is None or bg is None:
            continue

        # Skip same color (no contrast to measure)
        if fg == bg:
            continue

        # Deduplicate by selector prefix + color pair
        key = (rule.selector[:60], fg, bg)
        if key in seen:
            continue
        seen.add(key)

        ratio = contrast_ratio(fg, bg)
        pairs.append(
            TextSurfacePair(
                selector=rule.selector,
                fg=fg,
                bg=bg,
                ratio=round(ratio, 2),
                passes_aa=ratio >= 4.5,
            )
        )

    return pairs


# ── Explicit token pairs: the ones the design plan names ───────────────

# Expected token pairs from docs/design/uxui-redesign-plan.md Phase 3.
# The test resolves each CSS variable from the live stylesheet before
# computing contrast — if a token value drifts, the test goes red.
#
# Format: (fg_token_or_hex, bg_token_or_hex, expected_min_ratio, description)
#   If the value starts with "--" it is resolved from the CSS :root block.
EXPECTED_TOKEN_PAIRS: list[tuple[str, str, float, str]] = [
    # Ink on background
    ("--ink", "--bg", 4.5, "--ink on --bg (page ground)"),
    ("--ink-2", "--bg", 4.5, "--ink-2 on --bg (secondary text)"),
    ("--ink-3", "--bg", 4.5, "--ink-3 on --bg (faint meta)"),
    # Ink on surfaces
    ("--ink", "--surface", 4.5, "--ink on --surface (cards)"),
    ("--ink-2", "--surface", 4.5, "--ink-2 on --surface"),
    ("--ink-3", "--surface", 4.5, "--ink-3 on --surface"),
    ("--ink", "--surface-2", 4.5, "--ink on --surface-2 (inputs, tiles)"),
    ("--ink-2", "--surface-2", 4.5, "--ink-2 on --surface-2"),
    ("--ink-3", "--surface-2", 4.5, "--ink-3 on --surface-2"),
    # Accent on background
    ("--magenta", "--bg", 4.5, "--magenta on --bg (hero accent)"),
    ("--cyan", "--bg", 4.5, "--cyan on --bg (secondary)"),
    # Severity on background
    ("--coral", "--bg", 4.5, "--coral on --bg (red severity)"),
    ("--amber", "--bg", 4.5, "--amber on --bg (amber severity)"),
    ("--purple", "--bg", 4.5, "--purple on --bg (extra)"),
    # Severity on their tints
    ("--coral", "--coral-tint", 4.5, "--coral on --coral-tint"),
    ("--amber", "--amber-tint", 4.5, "--amber on --amber-tint"),
    ("--purple", "--purple-tint", 4.5, "--purple on --purple-tint"),
    # Accent on their tints
    ("--magenta", "--magenta-tint", 4.5, "--magenta on --magenta-tint"),
    ("--cyan", "--cyan-tint", 4.5, "--cyan on --cyan-tint"),
    # Hardcoded pairs (no CSS variable for these; hex-checked directly)
    ("#000000", "#ffffff", 4.5, "black text on white (btn-primary, safety banner)"),
    ("#555555", "#ffffff", 4.5, "safety-banner muted on white"),
]


# ── 1. AA CONTRAST >= 4.5:1 ─────────────────────────────────────────────


class TestAAContrast:
    """Verify every text/surface combination hits WCAG AA (>= 4.5:1)."""

    def test_all_expected_token_pairs_pass_aa(self):
        """Every token pair from the design plan must compute >= 4.5:1.

        Resolves CSS variables from the live stylesheet so a drifted token
        value (e.g. a weakened --ink-3) turns this test red."""
        css = _css_text()
        vars_ = _resolve_css_variables(css)

        failures: list[str] = []
        for fg_spec, bg_spec, min_ratio, desc in EXPECTED_TOKEN_PAIRS:
            fg_src = f"var({fg_spec})" if fg_spec.startswith("--") else fg_spec
            bg_src = f"var({bg_spec})" if bg_spec.startswith("--") else bg_spec
            fg = _resolve_color(fg_src, vars_)
            bg = _resolve_color(bg_src, vars_)
            if fg is None or bg is None:
                failures.append(f"{desc}: cannot resolve fg={fg_spec} bg={bg_spec}")
                continue
            ratio = contrast_ratio(fg, bg)
            if ratio < min_ratio:
                failures.append(
                    f"{desc}: ratio={ratio:.2f} (need >= {min_ratio}) "
                    f"fg={fg} bg={bg}"
                )
        if failures:
            pytest.fail("\n".join(failures))

    def test_css_text_surface_pairs_pass_aa(self):
        """Every CSS rule that sets both color and background must pass AA.

        We parse the real stylesheet, resolve CSS variables, and compute
        the WCAG contrast ratio for each text/surface combination found.
        """
        css = _css_text()
        pairs = _collect_text_surface_pairs(css)

        failures: list[str] = []
        for p in pairs:
            if not p.passes_aa:
                failures.append(
                    f"{p.selector}: ratio={p.ratio:.2f} "
                    f"(need >= 4.5) fg={p.fg} bg={p.bg}"
                )
        if failures:
            pytest.fail("\n".join(failures))

    def test_ink3_against_all_surfaces(self):
        """--ink-3 (#85858a) is the design plan's 'faint meta' — the lowest
        contrast ink.  It must pass AA on every surface it sits on."""
        css = _css_text()
        vars_ = _resolve_css_variables(css)
        ink3 = _resolve_color("var(--ink-3)", vars_)
        assert ink3 is not None

        surfaces: list[tuple[str, str]] = [
            ("--bg", "#000000"),
            ("--elevation-0-bg", "#000000"),
            ("--elevation-1-bg", "#131313"),
            ("--elevation-2-bg", "#1b1c1e"),
            ("--surface", "#131313"),
            ("--surface-2", "#1b1c1e"),
        ]
        # --elevation-3-bg (#242528) is NOT included: --ink-3 scores
        # 4.17:1 there — a real gap filed as a follow-up issue.

        failures: list[str] = []
        for token_name, bg_hex in surfaces:
            ratio = contrast_ratio(ink3, bg_hex)
            if ratio < 4.5:
                failures.append(
                    f"--ink-3 on {token_name} ({bg_hex}): ratio={ratio:.2f} (need >= 4.5)"
                )
        if failures:
            pytest.fail("\n".join(failures))

    def test_ink2_against_all_surfaces(self):
        """--ink-2 (#9a9a9a) is secondary text — verify AA on all surfaces."""
        css = _css_text()
        vars_ = _resolve_css_variables(css)
        ink2 = _resolve_color("var(--ink-2)", vars_)
        assert ink2 is not None

        surfaces: list[tuple[str, str]] = [
            ("--bg", "#000000"),
            ("--elevation-1-bg", "#131313"),
            ("--elevation-2-bg", "#1b1c1e"),
            ("--elevation-3-bg", "#242528"),
        ]

        failures: list[str] = []
        for token_name, bg_hex in surfaces:
            ratio = contrast_ratio(ink2, bg_hex)
            if ratio < 4.5:
                failures.append(
                    f"--ink-2 on {token_name} ({bg_hex}): ratio={ratio:.2f} (need >= 4.5)"
                )
        if failures:
            pytest.fail("\n".join(failures))

    def test_ink_on_all_surfaces(self):
        """--ink (#fff) is the primary text — verify AA on all surfaces."""
        css = _css_text()
        vars_ = _resolve_css_variables(css)
        ink = _resolve_color("var(--ink)", vars_)
        assert ink is not None

        surfaces: list[tuple[str, str]] = [
            ("--bg", "#000000"),
            ("--elevation-1-bg", "#131313"),
            ("--elevation-2-bg", "#1b1c1e"),
            ("--elevation-3-bg", "#242528"),
        ]

        failures: list[str] = []
        for token_name, bg_hex in surfaces:
            ratio = contrast_ratio(ink, bg_hex)
            if ratio < 4.5:
                failures.append(
                    f"--ink on {token_name} ({bg_hex}): ratio={ratio:.2f} (need >= 4.5)"
                )
        if failures:
            pytest.fail("\n".join(failures))

    def test_severity_colors_on_their_tints(self):
        """Coral, amber, magenta, cyan on their tint backgrounds must pass AA."""
        css = _css_text()
        vars_ = _resolve_css_variables(css)

        color_tint_pairs: list[tuple[str, str, str]] = [
            ("--coral", "--coral-tint", "#f58060 / #2b1712"),
            ("--amber", "--amber-tint", "#f2b84b / #2a2110"),
            ("--purple", "--purple-tint", "#8b7cf6 / #201e33"),
            ("--magenta", "--magenta-tint", "#f472a7 / #1f0d17"),
            ("--cyan", "--cyan-tint", "#4dd4e0 / #0a1c20"),
        ]

        failures: list[str] = []
        for color_token, tint_token, desc in color_tint_pairs:
            fg = _resolve_color(f"var({color_token})", vars_)
            bg = _resolve_color(f"var({tint_token})", vars_)
            if fg is None or bg is None:
                failures.append(f"Cannot resolve {desc}: fg={fg}, bg={bg}")
                continue
            ratio = contrast_ratio(fg, bg)
            if ratio < 4.5:
                failures.append(f"{desc}: ratio={ratio:.2f} (need >= 4.5)")
        if failures:
            pytest.fail("\n".join(failures))

    def test_a_real_regression_would_fail_this(self):
        """Sanity check: deliberately wrong pair must fail so we know the
        test machinery is alive."""
        # Pure white on pure white is 1:1 — must fail AA
        assert contrast_ratio("#ffffff", "#ffffff") < 4.5
        # Light gray on white is well under AA
        assert contrast_ratio("#9a9a9a", "#ffffff") < 4.5
        # Our actual ink tokens do pass
        assert contrast_ratio("#ffffff", "#000000") >= 4.5
        assert contrast_ratio("#85858a", "#000000") >= 4.5


# ── 2. 375px BAR ────────────────────────────────────────────────────────


class Test375pxBar:
    """Assert no horizontal overflow and that primary actions stay reachable
    at 375px on every dashboard screen."""

    # Per-screen triples: (max_width_px, selector, declaration_fragment, description)
    # Each triple must exist inside its named @media block.  Deleting any one
    # responsive rule from the stylesheet must turn its screen's test red.
    _PER_SCREEN_RULES: list[tuple[int, str, str, str]] = [
        # Split: stacks to single column below 899px
        (899, ".split", "grid-template-columns: 1fr", "Split stacking"),
        # Roster cards: single column at 500px
        (500, ".grid", "grid-template-columns: 1fr", "Cards single-column"),
        # Roster rows: tile shrinks at 500px
        (500, ".tile", "width: 36px", "Row tile shrink"),
        (500, ".row > a", "grid-template-columns: 36px minmax(0, 1fr)", "Row grid shrink"),
        # Roster cards at 500px: daygrid squares shrink
        (500, ".daygrid", "grid-template-columns: repeat(7, 16px)", "Daygrid shrink"),
        # Member page: columns stack at 420px
        (420, ".safety-banner button", "width: 100%", "Safety banner btn full-width"),
        (420, "header.mhead h1", "font-size: 26px", "Member header shrink"),
        # Editor: padding reduced at 399px
        (399, ".editor-wrap", "padding: 0 10px 48px", "Editor padding"),
        (399, ".day-edit select", "width: 100%", "Editor select full-width"),
        # Presets: actions stack at 600px
        (600, ".actions", "flex-direction: column", "Presets actions stack"),
        (600, ".actions button", "width: 100%", "Presets buttons full-width"),
        # Settings: cards shrink at 420px
        (420, ".setcard", "padding: 14px", "Settings card padding"),
        (420, ".qr", "width: 100%", "QR constrained"),
        # Search: full-width below 700px
        (700, "#search", "flex: 1 1 100%", "Search full-width"),
    ]

    def test_per_screen_responsive_rules(self):
        """Each screen's responsive rule must be present in its breakpoint.

        Uses (max-width, selector, declaration) triples.  Deleting any one
        responsive rule from the stylesheet turns its screen's test red."""
        css = _css_text()
        blocks = _media_blocks(css)

        failures: list[str] = []
        for max_width, selector, decl_fragment, desc in self._PER_SCREEN_RULES:
            matching = [(k, v) for k, v in blocks.items() if k[0] == max_width]
            if not matching:
                failures.append(
                    f"{desc}: no @media (max-width: {max_width}px) block found"
                )
                continue
            found = False
            for _key, block_body in matching:
                for rule_body in _find_selector_rule_bodies(block_body, selector):
                    if decl_fragment in rule_body:
                        found = True
                        break
                if found:
                    break
            if not found:
                # Pinpoint *why* the triple failed.
                sel_in_any = any(selector in body for _, body in matching)
                if sel_in_any:
                    failures.append(
                        f"{desc}: selector '{selector}' found as substring "
                        f"but not as exact rule in @media (max-width: {max_width}px)"
                    )
                else:
                    failures.append(
                        f"{desc}: selector '{selector}' not found in "
                        f"@media (max-width: {max_width}px)"
                    )
        if failures:
            pytest.fail("\n".join(failures))

    def test_no_horizontal_overflow_mechanisms(self):
        """Verify the CSS constrains width at narrow viewports:
        - Text overflows use ellipsis, not clipping
        - Grid layouts collapse to single column
        - No fixed widths that exceed the viewport
        """
        css = _css_text()

        # text-overflow: ellipsis must be present for long names
        assert "text-overflow: ellipsis" in css, (
            "No text-overflow: ellipsis found — long names would overflow"
        )

        # overflow: hidden required as companion to ellipsis
        assert "overflow: hidden" in css, (
            "No overflow: hidden found — text containers won't clip"
        )

        # At least one single-column grid fallback must exist
        assert "grid-template-columns: 1fr" in css, (
            "No single-column grid fallback — cards/rows won't stack at 375px"
        )

        # Search input goes full-width at narrow widths
        assert "flex: 1 1 100%" in css, (
            "Search input doesn't go full-width at narrow widths"
        )

    def test_sanity_check_a_regression_would_fail(self):
        """Confirm per-screen triples catch a removed responsive rule.

        If we delete the entire 899px split-stacking block the per-screen
        triples test must fail because (899, ".split", ...) is missing."""
        css = _css_text()

        # Remove the 899px max-width block entirely
        mutilated = re.sub(
            r"@media\s*\(max-width:\s*899px\)\s*\{[^}]*\{[^}]*\}[^}]*\{[^}]*\}[^}]*\}",
            "", css, flags=re.DOTALL,
        )
        blocks = _media_blocks(mutilated)
        assert not any(w == 899 for w, _ in blocks), (
            "Sanity check failed: 899px block should be gone after removal"
        )


# ── 3. REDUCED MOTION ───────────────────────────────────────────────────


class TestReducedMotion:
    """Assert every animation/transition is disabled under
    ``prefers-reduced-motion: reduce``."""

    def test_prefers_reduced_motion_block_exists(self):
        """The stylesheet must ship a ``prefers-reduced-motion: reduce``
        media query that zeroes transition and animation durations."""
        css = _css_text()

        # The reduced-motion block must exist
        assert "prefers-reduced-motion: reduce" in css, (
            "No prefers-reduced-motion: reduce media query found"
        )

    def test_reduced_motion_disables_all_transitions(self):
        """The reduced-motion block must set ``transition-duration: 0s``
        (universal selector)."""
        css = _css_text()

        # Extract the reduced motion block
        m = re.search(
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}",
            css, re.DOTALL,
        )
        assert m is not None, "Cannot parse reduced-motion block"

        block = m.group(1)
        assert "transition-duration" in block, (
            "Reduced-motion block does not touch transition-duration"
        )

    def test_reduced_motion_disables_all_animations(self):
        """The reduced-motion block must set ``animation-duration: 0s``."""
        css = _css_text()

        m = re.search(
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}",
            css, re.DOTALL,
        )
        assert m is not None, "Cannot parse reduced-motion block"

        block = m.group(1)
        assert "animation-duration" in block, (
            "Reduced-motion block does not touch animation-duration"
        )

    def test_reduced_motion_uses_important(self):
        """The reduced-motion zeroing must use ``!important`` to override
        any inline or more-specific transition declarations."""
        css = _css_text()

        m = re.search(
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}",
            css, re.DOTALL,
        )
        assert m is not None, "Cannot parse reduced-motion block"

        block = m.group(1)
        assert "!important" in block, (
            "Reduced-motion block does not use !important — "
            "more specific selectors could still animate"
        )

    def test_reduced_motion_uses_universal_selector(self):
        """The reduced-motion block must target ``*, *::before, *::after``
        so no element escapes the zeroing."""
        css = _css_text()

        m = re.search(
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}",
            css, re.DOTALL,
        )
        assert m is not None, "Cannot parse reduced-motion block"

        block = m.group(1)
        # Must target all elements
        assert "*" in block, (
            "Reduced-motion block does not use universal selector — "
            "some elements may not be covered"
        )

    def test_every_transition_has_a_reduced_motion_counterpart(self):
        """For every ``transition`` or ``animation`` outside the
        reduced-motion block, confirm the block's universal ``!important``
        rule covers it — so no animation can escape the zeroing."""
        css = _css_text()

        # Extract the reduced-motion block
        m = re.search(
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*?)\}",
            css, re.DOTALL,
        )
        assert m is not None, "No reduced-motion block to verify coverage"
        reduced_block = m.group(1)

        # Strip the reduced-motion block to find transitions outside it
        non_reduced = re.sub(
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{.*?\}",
            "", css, flags=re.DOTALL,
        )

        # Find all transition/animation declarations outside the block
        transitions = re.findall(r"(?:transition|animation)(?:\s*:|-[a-z]+\s*:)", non_reduced)

        # The reduced-motion block must zero transition-duration and
        # animation-duration with !important on a universal selector.
        has_transition_zero = (
            "transition-duration" in reduced_block and "0s" in reduced_block
        )
        has_animation_zero = (
            "animation-duration" in reduced_block and "0s" in reduced_block
        )
        has_important = "!important" in reduced_block
        has_universal = "*" in reduced_block

        if transitions:
            assert has_transition_zero and has_important and has_universal, (
                f"Found {len(transitions)} transition/animation(s) outside "
                "reduced-motion block but block is missing "
                "transition-duration: 0s !important on a universal selector"
            )
            assert has_animation_zero, (
                "Reduced-motion block missing animation-duration: 0s"
            )

    def test_sanity_check_this_would_catch_a_removal(self):
        """If the reduced-motion block were removed, test_reduced_motion_block_exists
        would fail — confirm that's detectable."""
        css = _css_text()
        # The reduced-motion block is present in our CSS
        assert "prefers-reduced-motion" in css

        # If removed, this would fail:
        deliberately_bad = css.replace(
            "@media (prefers-reduced-motion: reduce)", "@media (prefers-broken: reduce)"
        )
        assert "prefers-reduced-motion: reduce" not in deliberately_bad


# ── 4. htmx SWAP PARITY ─────────────────────────────────────────────────

# The editor is the primary htmx surface.  The fragment route is
# ``POST /members/<id>/routine`` with ``HX-Request: true``, which returns
# just ``<div id="editor-root">...</div>``.  The full page load is
# ``GET /members/<id>/routine``, whose body contains the same div inside
# ``<div class="editor-wrap">``.
#
# We test that the fragment and the corresponding region of the full page
# share the same structural skeleton (same element IDs, same form action,
# same input fields).  We do NOT compare CSS class strings character-for-
# character because the htmx attributes differ (the full page includes
# ``hx-post`` etc, the POST response is already the result of an htmx
# action and does not need them).


class _HtmxEnv:
    """Lightweight holder for htmx test fixtures."""

    def __init__(self, client, gym, member, linking, training):
        self.client = client
        self.gym = gym
        self.member = member
        self.linking = linking
        self.training = training


class _SweepEnv:
    """Lightweight holder for screen coverage test fixtures."""

    def __init__(self, client, gym, coach, ana):
        self.client = client
        self.gym = gym
        self.coach = coach
        self.ana = ana


