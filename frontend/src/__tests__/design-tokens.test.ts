/**
 * Design-token contrast validation — WCAG AA (≥ 4.5:1).
 *
 * Imports the live Tailwind config structurally (no source-text regex)
 * so new token keys are discovered automatically.
 *
 * Issue #156: ink-3 on elevation-3 was 4.17:1; elevation-3 now
 * darkened to #1c1d1f.
 */

import { describe, it, expect } from 'vitest';

// ---------------------------------------------------------------------------
// Import the live config
// ---------------------------------------------------------------------------

import tailwindConfig from '../../tailwind.config';

interface NestedColor {
  DEFAULT?: string;
  [key: string]: string | undefined;
}

interface ColorsConfig {
  bg?: string;
  ink?: NestedColor;
  [key: string]: string | NestedColor | undefined;
}

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

function contrastRatio(fg: string, bg: string): number {
  const l1 = relativeLuminance(fg);
  const l2 = relativeLuminance(bg);
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

const AA_MINIMUM = 4.5;

// ---------------------------------------------------------------------------
// Extraction helpers — structural (live config), not regex
// ---------------------------------------------------------------------------

function getInks(): Record<string, string> {
  const ink = colors.ink;
  if (!ink || typeof ink !== 'object') {
    throw new Error('Could not parse ink block from tailwind config');
  }
  const inks: Record<string, string> = {};
  for (const [key, value] of Object.entries(ink)) {
    if (typeof value === 'string') {
      inks[key === 'DEFAULT' ? 'ink' : `ink-${key}`] = value;
    }
  }
  if (Object.keys(inks).length === 0) {
    throw new Error('No ink tokens found in tailwind config');
  }
  return inks;
}

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
  // #156 regression canary
  // -----------------------------------------------------------------------

  it('ink-3 on elevation-3 clears AA (#156 regression)', () => {
    const inks = getInks();
    const surfaces = getSurfaces();

    expect(inks['ink-3'], 'ink-3 must exist').toBeDefined();
    expect(surfaces['elevation-3'], 'elevation-3 must exist').toBeDefined();

    const cr = contrastRatio(inks['ink-3'], surfaces['elevation-3']);
    expect(
      cr,
      `ink-3 on elevation-3: ${cr.toFixed(2)}:1 < ${AA_MINIMUM}:1`,
    ).toBeGreaterThanOrEqual(AA_MINIMUM);
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

    expect(elevations.length, 'need at least 2 elevation levels').toBeGreaterThanOrEqual(2);

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
});
