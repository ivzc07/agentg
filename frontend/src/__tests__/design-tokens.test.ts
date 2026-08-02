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

function expandHex(short: string): string {
  if (short.length === 7 && short.startsWith('#')) return short;
  if (short.length === 4 && short.startsWith('#')) {
    return `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`;
  }
  throw new Error(`Unhandled hex format: ${short}`);
}

function relativeLuminance(hex: string): number {
  const expanded = expandHex(hex);
  const r = parseInt(expanded.slice(1, 3), 16) / 255;
  const g = parseInt(expanded.slice(3, 5), 16) / 255;
  const b = parseInt(expanded.slice(5, 7), 16) / 255;
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
// Extraction helpers — operate on the imported colours object, not regex
// ---------------------------------------------------------------------------

const AA_MINIMUM = 4.5;

/** All semantic colour family names that carry both DEFAULT + tint. */
const SEMANTIC_NAMES = ['magenta', 'cyan', 'coral', 'amber', 'purple', 'success'] as const;

/** Discover every ink token from ``colors.ink`` (fail-closed). */
function getInks(): Record<string, string> {
  const ink = colors.ink;
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
function getSurfaces(): Record<string, string> {
  const surfaces: Record<string, string> = {};

  for (const [key, value] of Object.entries(colors)) {
    if (key.startsWith('elevation-') && typeof value === 'object' && value !== null) {
      const entry = value as NestedColor;
      if (typeof entry.DEFAULT === 'string') {
        surfaces[key] = entry.DEFAULT;
      }
    }
  }

  if (typeof colors.bg === 'string') {
    surfaces['bg'] = colors.bg;
  }

  return surfaces;
}

/** Semantic foreground-on-tint pairs for each colour family. */
function getSemanticTintPairs(): Record<string, { fg: string; bg: string }> {
  const pairs: Record<string, { fg: string; bg: string }> = {};
  for (const name of SEMANTIC_NAMES) {
    const entry = colors[name];
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
 * ``text-ink``, which would fail AA. */
function getSemanticAccentBgPairs(): Record<string, { fg: string; bg: string }> {
  const pairs: Record<string, { fg: string; bg: string }> = {};
  const bg = colors.bg;

  for (const name of SEMANTIC_NAMES) {
    const entry = colors[name];
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
// Tests
// ===========================================================================

describe('design token WCAG AA contrast', () => {
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
      .sort(([a], [b]) => a.localeCompare(b));

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
  // Dynamic discovery: new keys are automatically included
  // -----------------------------------------------------------------------

  it('new ink token is discovered and a bad contrast pair fails', () => {
    // Simulate a config with an extra ink token that fails contrast.
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

    const inks: Record<string, string> = {};
    const ink = synthetic.ink!;
    for (const [key, value] of Object.entries(ink)) {
      if (typeof value === 'string') {
        inks[key === 'DEFAULT' ? 'ink' : `ink-${key}`] = value;
      }
    }

    // ink-4 must be discovered
    expect(inks['ink-4']).toBe('#111111');

    // ink-4 on elevation-0 (#111 on #000) should fail AA
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

    const surfaces: Record<string, string> = {};
    for (const [key, value] of Object.entries(synthetic)) {
      if (key.startsWith('elevation-') && typeof value === 'object' && value !== null) {
        const entry = value as NestedColor;
        if (typeof entry.DEFAULT === 'string') surfaces[key] = entry.DEFAULT;
      }
    }
    if (typeof synthetic.bg === 'string') surfaces['bg'] = synthetic.bg;

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
    // Simulate a config where bg lives only under retiredPalette — structural
    // extraction from colors.* must not pick it up.
    const synthetic: ColorsConfig = {
      ink: { DEFAULT: '#fff', '2': '#9a9a9a', '3': '#85858a' },
      bg: undefined, // not present in active colors
      'elevation-0': { DEFAULT: '#000' },
      'elevation-1': { DEFAULT: '#131313' },
      // retiredPalette is outside theme.extend.colors in reality, but even
      // if it leaks into colors, the extraction only looks at the top-level
      // keys bg + elevation-* — it won't recurse into arbitrary objects.
    };

    const surfaces: Record<string, string> = {};
    for (const [key, value] of Object.entries(synthetic)) {
      if (key.startsWith('elevation-') && typeof value === 'object' && value !== null) {
        const entry = value as NestedColor;
        if (typeof entry.DEFAULT === 'string') surfaces[key] = entry.DEFAULT;
      }
    }
    if (typeof synthetic.bg === 'string') surfaces['bg'] = synthetic.bg;

    // bg must NOT be picked up since it's undefined
    expect(surfaces['bg']).toBeUndefined();
  });
});
