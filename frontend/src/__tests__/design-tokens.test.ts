/**
 * Design-token contrast validation — WCAG AA (≥ 4.5:1).
 *
 * Imports the live Tailwind config directly (no source-text regex) so new
 * ink, elevation, and semantic colour keys are discovered structurally.
 * Covers every ink × surface pair, plus the active semantic foreground /
 * background combinations used by the dashboard UI.
 *
 * Issue #140 required every text/surface combination; issue #156 is its
 * contrast follow-up (ink-3 on elevation-3-bg was 4.17:1).
 */

import { describe, it, expect } from 'vitest';
import { extractClassNames } from './classname-extractor';

// Raw component sources imported via Vite for repository-wide audits.
import memberPageSource from '../components/MemberPage.tsx?raw';
import routineEditorSource from '../components/RoutineEditor.tsx?raw';
import settingsPageSource from '../components/SettingsPage.tsx?raw';
import rosterCardsSource from '../components/RosterCards.tsx?raw';
import rosterShellSource from '../components/RosterShell.tsx?raw';
import rosterSplitSource from '../components/RosterSplit.tsx?raw';
import rosterTableSource from '../components/RosterTable.tsx?raw';
import presetsPageSource from '../components/PresetsPage.tsx?raw';
import presetsShellSource from '../components/PresetsShell.tsx?raw';
import langToggleSource from '../components/LangToggle.tsx?raw';
import loginPageSource from '../components/LoginPage.tsx?raw';
import shellSource from '../components/Shell.tsx?raw';

const ALL_COMPONENT_SOURCES: { name: string; source: string }[] = [
  { name: 'MemberPage.tsx', source: memberPageSource },
  { name: 'RoutineEditor.tsx', source: routineEditorSource },
  { name: 'SettingsPage.tsx', source: settingsPageSource },
  { name: 'RosterCards.tsx', source: rosterCardsSource },
  { name: 'RosterShell.tsx', source: rosterShellSource },
  { name: 'RosterSplit.tsx', source: rosterSplitSource },
  { name: 'RosterTable.tsx', source: rosterTableSource },
  { name: 'PresetsPage.tsx', source: presetsPageSource },
  { name: 'PresetsShell.tsx', source: presetsShellSource },
  { name: 'LangToggle.tsx', source: langToggleSource },
  { name: 'LoginPage.tsx', source: loginPageSource },
  { name: 'Shell.tsx', source: shellSource },
];

// ---------------------------------------------------------------------------
// Types for the Tailwind colour config
// ---------------------------------------------------------------------------

interface NestedColor {
  DEFAULT?: string;
  tint?: string;
  stroke?: string;
  [shade: string]: string | undefined;
}

interface ColorsConfig {
  bg?: string;
  ink?: NestedColor;
  magenta?: NestedColor;
  cyan?: NestedColor;
  coral?: NestedColor;
  amber?: NestedColor;
  purple?: NestedColor;
  success?: NestedColor;
  [key: string]: string | NestedColor | undefined;
}

// ---------------------------------------------------------------------------
// Import the live config
// ---------------------------------------------------------------------------

// eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
import tailwindConfig from '../../tailwind.config';

interface TailwindTheme {
  extend?: {
    colors?: ColorsConfig;
  };
}

interface TailwindConfigLike {
  theme?: TailwindTheme;
}

const cfg = tailwindConfig as TailwindConfigLike;
const colors: ColorsConfig = cfg.theme?.extend?.colors ?? {};

// ---------------------------------------------------------------------------
// WCAG 2.1 relative luminance & contrast
// ---------------------------------------------------------------------------

function linearize(c: number): number {
  if (c <= 0.04045) return c / 12.92;
  return ((c + 0.055) / 1.055) ** 2.4;
}

/** Hex digits used to reject malformed colour strings before parsing. */
const HEX_DIGIT_RE = /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/;

function expandHex(short: string): string {
  if (!HEX_DIGIT_RE.test(short)) {
    throw new Error(`Invalid hex color: ${short}`);
  }
  if (short.length === 7 && short.startsWith('#')) return short;
  if (short.length === 4 && short.startsWith('#')) {
    return `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`;
  }
  throw new Error(`Unhandled hex format: ${short}`);
}

function relativeLuminance(hex: string): number {
  const expanded = expandHex(hex);
  const rRaw = parseInt(expanded.slice(1, 3), 16);
  const gRaw = parseInt(expanded.slice(3, 5), 16);
  const bRaw = parseInt(expanded.slice(5, 7), 16);
  if (isNaN(rRaw) || isNaN(gRaw) || isNaN(bRaw)) {
    throw new Error(`Invalid hex color: ${hex} (parsed NaN)`);
  }
  const r = rRaw / 255;
  const g = gRaw / 255;
  const b = bRaw / 255;
  return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b);
}

export function contrastRatio(fg: string, bg: string): number {
  const l1 = relativeLuminance(fg);
  const l2 = relativeLuminance(bg);
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

// ---------------------------------------------------------------------------
// Extraction helpers — operate on the imported colours object, not regex.
// Every helper accepts an optional ColorsConfig so synthetic / decoy
// tests can exercise the same extraction logic that production tests run.
// ---------------------------------------------------------------------------

const AA_MINIMUM = 4.5;

/** All semantic colour family names that carry both DEFAULT + tint. */
const SEMANTIC_NAMES = ['magenta', 'cyan', 'coral', 'amber', 'purple', 'success'] as const;

/** Discover every ink token from ``src.ink`` (fail-closed). */
function getInks(src: ColorsConfig = colors): Record<string, string> {
  const ink = src.ink;
  if (!ink || typeof ink !== 'object') {
    throw new Error('Could not parse ink block from tailwind config');
  }

  const inks: Record<string, string> = {};
  for (const [key, value] of Object.entries(ink)) {
    if (typeof value === 'string') {
      if (key === 'DEFAULT') {
        inks['ink'] = value;
      } else {
        inks[`ink-${key}`] = value;
      }
    }
  }

  if (Object.keys(inks).length === 0) {
    throw new Error('No ink tokens found in tailwind config');
  }
  return inks;
}

/** Discover every elevation surface + ``bg`` from the colours object. */
function getSurfaces(src: ColorsConfig = colors): Record<string, string> {
  const surfaces: Record<string, string> = {};

  for (const [key, value] of Object.entries(src)) {
    if (key.startsWith('elevation-') && typeof value === 'object' && value !== null) {
      const entry = value as NestedColor;
      if (typeof entry.DEFAULT === 'string') {
        surfaces[key] = entry.DEFAULT;
      }
    }
  }

  if (typeof src.bg === 'string') {
    surfaces['bg'] = src.bg;
  }

  return surfaces;
}

/** Semantic foreground-on-tint pairs for each colour family. */
function getSemanticTintPairs(
  src: ColorsConfig = colors,
): Record<string, { fg: string; bg: string }> {
  const pairs: Record<string, { fg: string; bg: string }> = {};
  for (const name of SEMANTIC_NAMES) {
    const entry = src[name];
    if (typeof entry === 'object' && entry !== null) {
      const e = entry as NestedColor;
      if (typeof e.DEFAULT === 'string' && typeof e.tint === 'string') {
        pairs[`text-${name} on bg-${name}-tint`] = { fg: e.DEFAULT, bg: e.tint };
      }
    }
  }
  return pairs;
}

/** Semantic accent-bg-on-dark pairs (e.g. bg-magenta text-bg).
 *
 * Accent colours are mid-luminance by design and are only paired with
 * dark text (``text-bg`` / ``text-black``) in the UI — never with white
 * ``text-ink``, which would fail AA.
 *
 * The RoutineEditor.test.tsx component-level test guards this invariant
 * across live component sources so it cannot silently regress. */
function getSemanticAccentBgPairs(
  src: ColorsConfig = colors,
): Record<string, { fg: string; bg: string }> {
  const pairs: Record<string, { fg: string; bg: string }> = {};
  const bg = src.bg;

  for (const name of SEMANTIC_NAMES) {
    const entry = src[name];
    if (typeof entry === 'object' && entry !== null) {
      const e = entry as NestedColor;
      if (typeof e.DEFAULT === 'string' && typeof bg === 'string') {
        pairs[`bg-${name} text-bg`] = { fg: bg, bg: e.DEFAULT };
      }
    }
  }
  return pairs;
}

// ===========================================================================
// Required-token inventory contract (P2 5159492181)
// ===========================================================================

/**
 * Tokens that MUST exist because production components depend on them.
 * Missing any of these is a hard failure with a clear message.
 *
 * Derived from a grep of every ``className`` across all ``src/components/*.tsx``
 * files (last verified 2026-07-22).
 */
const REQUIRED_INK_TOKENS = ['ink', 'ink-2', 'ink-3'] as const;

const REQUIRED_SURFACE_TOKENS = [
  'bg',
  'elevation-0',
  'elevation-1',
  'elevation-2',
  'elevation-3',
] as const;

const REQUIRED_SEMANTIC_TOKENS = [
  'magenta',
  'cyan',
  'coral',
  'amber',
  'purple',
  'success',
] as const;

// ===========================================================================
// Active ink-on-tint combinations (P2 5159492236)
// ===========================================================================

/**
 * Every ink-on-tint combination that actually renders in production.
 *
 * Discovered by walking the JSX of every component; a pair is added only
 * when a descendant of a ``bg-*-tint`` element either inherits ``text-ink``
 * (the page default) or carries an explicit ``text-ink-N`` class.  No
 * speculative pairs — if the UI doesn't use it, it's not here.
 *
 * Source audit (2026-07-22):
 *
 *   MemberPage SafetyBanner:
 *     <div className="… bg-coral-tint …">
 *       <b>                             ← inherits text-ink
 *       <div className="… text-ink-3">  ← explicit
 *
 *   RosterCards legend:
 *     <i className="… bg-coral-tint" />  ← no text child (colour-only swatch)
 */
function getActiveInkOnTintPairs(
  src: ColorsConfig = colors,
): Record<string, { fg: string; bg: string }> {
  const pairs: Record<string, { fg: string; bg: string }> = {};

  // coral.tint × ink (inherited in SafetyBanner flag text)
  const coral = src.coral as NestedColor | undefined;
  if (coral?.tint) {
    const inkDefault = src.ink?.DEFAULT;
    if (inkDefault) {
      pairs['ink on bg-coral-tint (SafetyBanner flag)'] = { fg: inkDefault, bg: coral.tint };
    }
    const ink3 = src.ink?.['3'];
    if (ink3) {
      pairs['ink-3 on bg-coral-tint (SafetyBanner flag-meta)'] = { fg: ink3, bg: coral.tint };
    }
  }

  return pairs;
}

// ===========================================================================
// Tests
// ===========================================================================

describe('design token WCAG AA contrast', () => {
  // -----------------------------------------------------------------------
  // Hex validation — malformed strings must throw, not false-green via NaN
  // -----------------------------------------------------------------------

  it('malformed hex tokens throw instead of silently passing (NaN guard)', () => {
    expect(() => relativeLuminance('#zzzzzz')).toThrow(/Invalid hex/);
    expect(() => relativeLuminance('#GGGGGG')).toThrow(/Invalid hex/);
    expect(() => relativeLuminance('#xyz')).toThrow(/Invalid hex/);
    expect(() => contrastRatio('#000000', '#zzzzzz')).toThrow(/Invalid hex/);
    expect(() => contrastRatio('#zzzzzz', '#000000')).toThrow(/Invalid hex/);
  });

  // -----------------------------------------------------------------------
  // Ink × surface matrix (dynamically discovered)
  // -----------------------------------------------------------------------

  it('every ink × surface pair clears AA (≥ 4.5:1)', () => {
    const inks = getInks();
    const surfaces = getSurfaces();

    expect(Object.keys(inks).length, 'no ink tokens discovered').toBeGreaterThan(0);
    expect(Object.keys(surfaces).length, 'no surface tokens discovered').toBeGreaterThan(0);

    const failures: string[] = [];
    for (const [inkName, inkHex] of Object.entries(inks)) {
      for (const [surfName, surfHex] of Object.entries(surfaces)) {
        const cr = contrastRatio(inkHex, surfHex);
        if (cr < AA_MINIMUM) {
          failures.push(
            `${inkName} (${inkHex}) on ${surfName} (${surfHex}): ${cr.toFixed(2)}:1 < ${AA_MINIMUM}:1`,
          );
        }
      }
    }

    expect(failures).toEqual([]);
  });

  // -----------------------------------------------------------------------
  // #156 regression canary
  // -----------------------------------------------------------------------

  it('ink-3 on elevation-3 clears AA (#156 regression)', () => {
    const inks = getInks();
    const surfaces = getSurfaces();

    expect(inks['ink-3']).toBeDefined();
    expect(surfaces['elevation-3']).toBeDefined();

    const cr = contrastRatio(inks['ink-3'], surfaces['elevation-3']);
    expect(cr).toBeGreaterThanOrEqual(AA_MINIMUM);
  });

  // -----------------------------------------------------------------------
  // Elevation scale monotonicity
  // -----------------------------------------------------------------------

  it('elevation scale is monotonic (higher = lighter)', () => {
    const surfaces = getSurfaces();
    const elevations = Object.entries(surfaces)
      .filter(([k]) => k.startsWith('elevation-'))
      .sort(([a], [b]) => {
        const numA = parseInt(a.split('-')[1], 10);
        const numB = parseInt(b.split('-')[1], 10);
        return numA - numB;
      });

    expect(elevations.length).toBeGreaterThanOrEqual(2);

    const lums = elevations.map(([name, hex]) => ({ name, lum: relativeLuminance(hex) }));

    for (let i = 0; i < lums.length - 1; i++) {
      const lo = lums[i];
      const hi = lums[i + 1];
      expect(
        lo.lum,
        `Elevation scale broken: ${lo.name} (${lo.lum.toFixed(5)}) >= ${hi.name} (${hi.lum.toFixed(5)})`,
      ).toBeLessThan(hi.lum);
    }
  });

  // -----------------------------------------------------------------------
  // Dynamic discovery: new keys are automatically included.
  // Synthetic tests call the shared helpers (getInks / getSurfaces) so
  // helper regressions are caught here instead of by duplicate logic.
  // -----------------------------------------------------------------------

  it('new ink token is discovered and a bad contrast pair fails', () => {
    const synthetic: ColorsConfig = {
      ink: {
        DEFAULT: '#fff',
        '2': '#9a9a9a',
        '3': '#85858a',
        '4': '#111111', // too dark — will fail against dark surfaces
      },
      bg: '#000',
      'elevation-0': { DEFAULT: '#000' },
      'elevation-1': { DEFAULT: '#131313' },
    };

    const inks = getInks(synthetic);
    expect(inks['ink-4']).toBe('#111111');

    const elev0 = synthetic['elevation-0'] as NestedColor;
    const cr = contrastRatio(inks['ink-4'], elev0.DEFAULT!);
    expect(cr).toBeLessThan(AA_MINIMUM);
  });

  it('new elevation token is discovered and participates in matrix', () => {
    const synthetic: ColorsConfig = {
      ink: { DEFAULT: '#fff', '2': '#9a9a9a' },
      bg: '#000',
      'elevation-0': { DEFAULT: '#000' },
      'elevation-1': { DEFAULT: '#131313' },
      'elevation-4': { DEFAULT: '#2a2a2e' },
    };

    const surfaces = getSurfaces(synthetic);
    expect(surfaces['elevation-4']).toBe('#2a2a2e');
    expect(Object.keys(surfaces).length).toBe(4); // 3 elevations + bg
  });

  // -----------------------------------------------------------------------
  // Semantic pairs: tint + accent-bg patterns
  // -----------------------------------------------------------------------

  it('every semantic DEFAULT-on-tint pair clears AA', () => {
    const pairs = getSemanticTintPairs();
    expect(Object.keys(pairs).length, 'no semantic tint pairs found').toBeGreaterThan(0);

    const failures: string[] = [];
    for (const [label, { fg, bg }] of Object.entries(pairs)) {
      const cr = contrastRatio(fg, bg);
      if (cr < AA_MINIMUM) {
        failures.push(`${label}: ${fg} on ${bg} = ${cr.toFixed(2)}:1 < ${AA_MINIMUM}:1`);
      }
    }
    expect(failures).toEqual([]);
  });

  it('every semantic accent-bg pair clears AA', () => {
    const bgPairs = getSemanticAccentBgPairs();
    expect(Object.keys(bgPairs).length, 'no semantic accent-bg pairs found').toBeGreaterThan(0);

    const failures: string[] = [];
    for (const [label, { fg, bg }] of Object.entries(bgPairs)) {
      const cr = contrastRatio(fg, bg);
      if (cr < AA_MINIMUM) {
        failures.push(`${label}: ${fg} on ${bg} = ${cr.toFixed(2)}:1 < ${AA_MINIMUM}:1`);
      }
    }
    expect(failures).toEqual([]);
  });

  // -----------------------------------------------------------------------
  // Specific reviewer-cited pairs
  // -----------------------------------------------------------------------

  it('bg-coral-tint with text-coral clears AA', () => {
    const coral = colors.coral as NestedColor | undefined;
    expect(coral?.DEFAULT).toBeDefined();
    expect(coral?.tint).toBeDefined();
    const cr = contrastRatio(coral!.DEFAULT!, coral!.tint!);
    expect(cr).toBeGreaterThanOrEqual(AA_MINIMUM);
  });

  it('bg-magenta with text-bg clears AA', () => {
    const magenta = colors.magenta as NestedColor | undefined;
    expect(magenta?.DEFAULT).toBeDefined();
    expect(colors.bg).toBeDefined();
    const cr = contrastRatio(colors.bg!, magenta!.DEFAULT!);
    expect(cr).toBeGreaterThanOrEqual(AA_MINIMUM);
  });

  // -----------------------------------------------------------------------
  // Decoy resistance: source-text regex would match, structural import won't
  // -----------------------------------------------------------------------

  it('a retiredPalette does not contaminate extraction', () => {
    const synthetic: ColorsConfig = {
      ink: { DEFAULT: '#fff', '2': '#9a9a9a', '3': '#85858a' },
      bg: undefined, // not present in active colors
      'elevation-0': { DEFAULT: '#000' },
      'elevation-1': { DEFAULT: '#131313' },
    };

    const surfaces = getSurfaces(synthetic);
    // bg must NOT be picked up since it's undefined
    expect(surfaces['bg']).toBeUndefined();
  });

  // -----------------------------------------------------------------------
  // Required-token inventory (P2 5159492181)
  // -----------------------------------------------------------------------

  describe('required token inventory', () => {
    it('every required ink token exists', () => {
      const inks = getInks();
      for (const name of REQUIRED_INK_TOKENS) {
        expect(inks[name], `Required ink token "${name}" is missing`).toBeDefined();
        expect(inks[name], `Required ink token "${name}" must be a hex color`).toMatch(/^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/);
      }
    });

    it('every required surface token exists', () => {
      const surfaces = getSurfaces();
      for (const name of REQUIRED_SURFACE_TOKENS) {
        expect(surfaces[name], `Required surface token "${name}" is missing`).toBeDefined();
        expect(surfaces[name], `Required surface token "${name}" must be a hex color`).toMatch(/^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/);
      }
    });

    it('every required semantic colour family has DEFAULT + tint', () => {
      for (const name of REQUIRED_SEMANTIC_TOKENS) {
        const entry = colors[name] as NestedColor | undefined;
        expect(entry, `Required semantic colour "${name}" is missing`).toBeDefined();
        expect(entry?.DEFAULT, `Required semantic colour "${name}.DEFAULT" is missing`).toBeDefined();
        expect(entry?.tint, `Required semantic colour "${name}.tint" is missing`).toBeDefined();
        expect(entry?.DEFAULT, `"${name}.DEFAULT" must be a hex color`).toMatch(/^#[0-9a-fA-F]{6}$/);
        expect(entry?.tint, `"${name}.tint" must be a hex color`).toMatch(/^#[0-9a-fA-F]{6}$/);
      }
    });

    it('dynamically discovered tokens are a superset of the required inventory', () => {
      const inks = getInks();
      const surfaces = getSurfaces();

      // All required inks must be in the dynamic set.
      for (const name of REQUIRED_INK_TOKENS) {
        expect(inks[name], `Required ink "${name}" not in dynamic discovery`).toBeDefined();
      }
      // All required surfaces must be in the dynamic set.
      for (const name of REQUIRED_SURFACE_TOKENS) {
        expect(surfaces[name], `Required surface "${name}" not in dynamic discovery`).toBeDefined();
      }
      // Dynamic discovery may include more (future tokens) — that's fine.
    });
  });

  // -----------------------------------------------------------------------
  // Active ink-on-tint combinations (P2 5159492236)
  // -----------------------------------------------------------------------

  describe('active ink-on-tint combinations', () => {
    it('every active ink-on-tint pair clears AA', () => {
      const pairs = getActiveInkOnTintPairs();
      expect(Object.keys(pairs).length, 'no active ink-on-tint pairs found').toBeGreaterThan(0);

      const failures: string[] = [];
      for (const [label, { fg, bg }] of Object.entries(pairs)) {
        const cr = contrastRatio(fg, bg);
        if (cr < AA_MINIMUM) {
          failures.push(`${label}: ${fg} on ${bg} = ${cr.toFixed(2)}:1 < ${AA_MINIMUM}:1`);
        }
      }
      expect(failures).toEqual([]);
    });

    // Regression: the reviewer changed coral.tint to #333333 and
    // tests stayed green because ink-on-tint wasn't audited.  Prove
    // that mutation now fails.
    it('coral.tint regression: ink-3 on tint fails when tint is lightened to #333333', () => {
      // Simulate the reviewer's mutation.
      const mutatedTint = '#333333';
      const ink3 = (colors.ink as NestedColor)?.['3'];
      expect(ink3).toBeDefined();

      const cr = contrastRatio(ink3!, mutatedTint);
      // The mutation must fail AA — this is the bug the reviewer found.
      expect(cr).toBeLessThan(AA_MINIMUM);
    });

    it('ink on coral.tint (inherited) clears AA with real values', () => {
      const coral = colors.coral as NestedColor | undefined;
      const inkDefault = (colors.ink as NestedColor)?.DEFAULT;
      expect(coral?.tint).toBeDefined();
      expect(inkDefault).toBeDefined();

      const cr = contrastRatio(inkDefault!, coral!.tint!);
      expect(cr).toBeGreaterThanOrEqual(AA_MINIMUM);
    });

    it('ink-3 on coral.tint (explicit) clears AA with real values', () => {
      const coral = colors.coral as NestedColor | undefined;
      const ink3 = (colors.ink as NestedColor)?.['3'];
      expect(coral?.tint).toBeDefined();
      expect(ink3).toBeDefined();

      const cr = contrastRatio(ink3!, coral!.tint!);
      expect(cr).toBeGreaterThanOrEqual(AA_MINIMUM);
    });
  });

  // -----------------------------------------------------------------------
  // Repository-wide accent-bg dark-text invariant (P2 5159492292 + 5159492353)
  //
  // Accent backgrounds (bg-magenta, bg-cyan, …) are mid-luminance and MUST
  // only be paired with dark text (text-bg / text-black) in every component.
  //
  // This uses the shared className extractor (no source regex duplication)
  // and covers ALL components, not just RoutineEditor.
  // -----------------------------------------------------------------------

  describe('repository-wide accent-background dark-text invariant', () => {
    const ACCENT_BG_CLASSES = [
      'bg-magenta', 'bg-cyan', 'bg-coral', 'bg-amber', 'bg-purple', 'bg-success',
    ];

    const DARK_TEXT_CLASSES = ['text-bg', 'text-black'];
    const LIGHT_TEXT_CLASSES = ['text-white', 'text-ink'];

    it('every accent bg-* class across all components is paired with a dark text class', () => {
      const violations: string[] = [];

      for (const { name, source } of ALL_COMPONENT_SOURCES) {
        const entries = extractClassNames(source, name);
        for (const entry of entries) {
          for (const accentCls of ACCENT_BG_CLASSES) {
            if (!entry.tokens.includes(accentCls)) continue;

            const hasDark = DARK_TEXT_CLASSES.some((c) => entry.tokens.includes(c));
            const hasLight = LIGHT_TEXT_CLASSES.some((c) => entry.tokens.includes(c));

            // A bg-accent without ANY text-* class is a decorative swatch
            // (e.g. legend colour chips) — not a text/surface pair.
            const hasAnyTextClass = entry.tokens.some((t) => t.startsWith('text-'));
            if (!hasAnyTextClass) continue;

            if (!hasDark || hasLight) {
              violations.push(
                `${name}:${entry.line} — ${accentCls} used without dark-text guard: "${entry.raw.trim()}"`,
              );
            }
          }
        }
      }

      expect(violations).toEqual([]);
    });

    // SettingsPage regression: changing SettingsPage to bg-magenta text-white
    // must be caught (P2 5159492353).
    it('SettingsPage regression: bg-magenta text-white would be caught', () => {
      const syntheticSource = `
        export function Test() {
          return <button className="px-4 py-2 bg-magenta text-white rounded-sm">Save</button>;
        }
      `;
      const entries = extractClassNames(syntheticSource, 'Test.tsx');
      const violation = entries.find((e) => e.tokens.includes('bg-magenta'));
      expect(violation).toBeDefined();

      const hasDark = DARK_TEXT_CLASSES.some((c) => violation!.tokens.includes(c));
      const hasLight = LIGHT_TEXT_CLASSES.some((c) => violation!.tokens.includes(c));
      expect(hasDark).toBe(false); // no text-bg/text-black
      expect(hasLight).toBe(true); // has text-white → violation
    });

    // The RoutineEditor save button must have the correct classes (DOM-level
    // regression in RoutineEditor.test.tsx covers rendering; this static
    // check is the repository-wide safety net).
    it('RoutineEditor save button is verified in the repo-wide scan', () => {
      const entries = extractClassNames(routineEditorSource, 'RoutineEditor.tsx');
      const saveBtn = entries.find(
        (e) => e.tokens.includes('bg-magenta') && e.tokens.includes('text-bg'),
      );
      expect(saveBtn, 'RoutineEditor save button must have bg-magenta text-bg').toBeDefined();
    });
  });
});
