/**
 * Design-token contrast validation for #156:
 *   ink.3 on elevation-3.DEFAULT must clear WCAG AA (≥ 4.5:1).
 *
 * Imports the live Tailwind config; no regex, no framework.
 */

import { describe, it, expect } from 'vitest';
import tailwindConfig from '../../tailwind.config';

// ---------------------------------------------------------------------------
// Dig tokens from the live config — one direct access per key
// ---------------------------------------------------------------------------

const colors = tailwindConfig.theme?.extend?.colors as Record<string, unknown> | undefined;
if (!colors) throw new Error('tailwind config has no theme.extend.colors');

const ink3 = (colors.ink as Record<string, string> | undefined)?.['3'];
if (typeof ink3 !== 'string') throw new Error('ink.3 token missing or not a string');

const elevation3 = (colors['elevation-3'] as { DEFAULT?: string } | undefined)?.DEFAULT;
if (typeof elevation3 !== 'string') throw new Error('elevation-3.DEFAULT token missing or not a string');

// ---------------------------------------------------------------------------
// WCAG 2.1 relative luminance & contrast
// ---------------------------------------------------------------------------

function linearize(c: number): number {
  if (c <= 0.04045) return c / 12.92;
  return ((c + 0.055) / 1.055) ** 2.4;
}

const HEX_RE = /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/;

function expandHex(hex: string): string {
  if (!HEX_RE.test(hex)) throw new Error(`Invalid hex color: ${hex}`);
  if (hex.length === 7) return hex;
  // 3-char shorthand
  return `#${hex[1]}${hex[1]}${hex[2]}${hex[2]}${hex[3]}${hex[3]}`;
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

// ===========================================================================
// Tests
// ===========================================================================

describe('ink.3 on elevation-3.DEFAULT — WCAG AA (#156)', () => {
  // -----------------------------------------------------------------------
  // NaN guard: malformed hex must throw, not silently pass
  // -----------------------------------------------------------------------

  it('malformed hex tokens throw instead of silently passing (NaN guard)', () => {
    expect(() => relativeLuminance('#zzzzzz')).toThrow(/Invalid hex/);
    expect(() => relativeLuminance('#GGGGGG')).toThrow(/Invalid hex/);
    expect(() => relativeLuminance('#xyz')).toThrow(/Invalid hex/);
    expect(() => contrastRatio('#000000', '#zzzzzz')).toThrow(/Invalid hex/);
    expect(() => contrastRatio('#zzzzzz', '#000000')).toThrow(/Invalid hex/);
  });

  // -----------------------------------------------------------------------
  // #156 regression canary — direct, no abstraction
  // -----------------------------------------------------------------------

  it('ink.3 on elevation-3.DEFAULT clears AA', () => {
    const cr = contrastRatio(ink3, elevation3);
    expect(
      cr,
      `ink.3 (${ink3}) on elevation-3.DEFAULT (${elevation3}): ${cr.toFixed(2)}:1 < ${AA_MINIMUM}:1`,
    ).toBeGreaterThanOrEqual(AA_MINIMUM);
  });

  // -----------------------------------------------------------------------
  // Elevation monotonicity — justified because changing elevation-3 could
  // invert the scale (higher elevation must be lighter)
  // -----------------------------------------------------------------------

  it('elevation scale is monotonic (higher = lighter)', () => {
    const entries: [string, string][] = [];
    for (const [key, val] of Object.entries(colors)) {
      if (key.startsWith('elevation-') && typeof val === 'object' && val !== null) {
        const d = (val as { DEFAULT?: string }).DEFAULT;
        if (typeof d === 'string') entries.push([key, d]);
      }
    }
    entries.sort(([a], [b]) => parseInt(a.split('-')[1], 10) - parseInt(b.split('-')[1], 10));

    expect(entries.length, 'need at least 2 elevation levels').toBeGreaterThanOrEqual(2);

    for (let i = 0; i < entries.length - 1; i++) {
      const [loName, loHex] = entries[i];
      const [hiName, hiHex] = entries[i + 1];
      const loLum = relativeLuminance(loHex);
      const hiLum = relativeLuminance(hiHex);
      expect(
        loLum,
        `Elevation scale broken: ${loName} (${loLum.toFixed(5)}) >= ${hiName} (${hiLum.toFixed(5)})`,
      ).toBeLessThan(hiLum);
    }
  });
});
