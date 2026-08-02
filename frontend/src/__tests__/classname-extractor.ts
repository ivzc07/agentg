/**
 * Shared className extraction for repository-wide static audits.
 *
 * Extracts every className string from TSX component source strings,
 * handling the JSX forms used in this repo:
 *
 *   1. className="plain string"
 *   2. className={`template literal`}
 *   3. className={ternary ? "a" : "b"}  (multiline expression)
 *   4. className={"expression string literal"}
 *
 * For forms 3–4 individual string sub-expressions (double- or backtick-
 * quoted) are hoisted from the JSX expression.  Variable references
 * (``colors[severity]``, ``stateClass[...]``) are collected by name so
 * a test can assert the variable is safe.
 *
 * Every function here is pure (string in → entries out) so tests that
 * depend on this module don't duplicate the extraction logic.
 */

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** A single className entry extracted from a source string. */
export interface ClassNameEntry {
  /** The raw className value. */
  raw: string;
  /** Resolved class tokens (space-separated static parts). */
  tokens: string[];
  /** Unresolvable variable references (e.g. ``stateClass[cell.state]``). */
  variables: string[];
  /** Source file path for diagnostics. */
  file: string;
  /** Approximate 1-based line number. */
  line: number;
}

// ---------------------------------------------------------------------------
// Regex patterns (compiled once)
// ---------------------------------------------------------------------------

const STRING_LITERAL_RE = /className="([^"]*)"/g;

/** Template literals: className={`...`}. The closing `` `} `` sequence
 *  is unambiguous. */
const TEMPLATE_LITERAL_RE = /className=\{`([^`]*)`\}/g;

/**
 * Expression forms: className={...} that are NOT template literals.
 * Skips ``className={`...`}`` entries already captured above.
 */
const EXPRESSION_BRACES_RE = /className=\{((?!\s*`)[\s\S])*?\}/g;

const DQ_STRING_RE = /"([^"]*)"/g;
const BT_STRING_RE = /`([^`]*)`/g;

/** Simple variable references inside expressions. */
const VAR_REF_RE = /\b([a-zA-Z_$]\w*(?:\[[^\]]+\])?)\s*(?=[?;:)\]}]\s*(?:"|`|$|\n))/g;

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Extract all className entries from a single TSX source string.
 */
export function extractClassNames(
  source: string,
  filePath = "<inline>",
): ClassNameEntry[] {
  const results: ClassNameEntry[] = [];

  // Strategy 1: plain string literals  className="..."
  for (const m of source.matchAll(STRING_LITERAL_RE)) {
    results.push({
      raw: m[1],
      tokens: tokenize(m[1]),
      variables: [],
      file: filePath,
      line: lineOf(source, m.index!),
    });
  }

  // Strategy 2: template literals  className={`...`}
  for (const m of source.matchAll(TEMPLATE_LITERAL_RE)) {
    const raw = m[1];
    const { tokens, variables } = parseTemplateLiteral(raw);
    results.push({ raw, tokens, variables, file: filePath, line: lineOf(source, m.index!) });
  }

  // Strategy 3+4: expression forms  className={...}
  for (const m of source.matchAll(EXPRESSION_BRACES_RE)) {
    const expr = m[1];
    if (/^\s*`.*`\s*$/s.test(expr)) continue; // already captured as template

    const strings: string[] = [];
    for (const sm of expr.matchAll(DQ_STRING_RE)) strings.push(sm[1]);
    for (const sm of expr.matchAll(BT_STRING_RE)) strings.push(sm[1]);

    const variables: string[] = [];
    for (const vm of expr.matchAll(VAR_REF_RE)) {
      if (!variables.includes(vm[1])) variables.push(vm[1]);
    }

    if (strings.length > 0 || variables.length > 0) {
      results.push({
        raw: expr.trim(),
        tokens: strings.flatMap(tokenize),
        variables,
        file: filePath,
        line: lineOf(source, m.index!),
      });
    }
  }

  return results;
}

// ---------------------------------------------------------------------------
// Helpers (exported for synthetic tests)
// ---------------------------------------------------------------------------

/** Split a className string into space-separated tokens. */
export function tokenize(raw: string): string[] {
  return raw.split(/\s+/).map((t) => t.trim()).filter(Boolean);
}

/** Parse a template literal body.  Template expressions like
 *  ``${cond ? "a" : "b"}`` are stripped; any quoted strings inside
 *  them are hoisted into the token list. */
export function parseTemplateLiteral(
  raw: string,
): { tokens: string[]; variables: string[] } {
  // Static portions (outside ${...})
  const stripped = raw.replace(/\$\{[^}]*\}/g, " ");
  const tokens = tokenize(stripped);

  // Hoist strings from inside template expressions
  for (const exprMatch of raw.matchAll(/\$\{([^}]*)\}/g)) {
    const inner = exprMatch[1];
    for (const sm of inner.matchAll(DQ_STRING_RE)) tokens.push(...tokenize(sm[1]));
    for (const sm of inner.matchAll(BT_STRING_RE)) tokens.push(...tokenize(sm[1]));
  }

  // Collect variable refs from inside expressions
  const variables: string[] = [];
  for (const exprMatch of raw.matchAll(/\$\{([^}]*)\}/g)) {
    for (const vm of exprMatch[1].matchAll(VAR_REF_RE)) {
      if (!variables.includes(vm[1])) variables.push(vm[1]);
    }
  }

  return { tokens, variables };
}

/** Compute the 1-based line number for a character offset. */
function lineOf(source: string, offset: number): number {
  return (source.slice(0, offset).match(/\n/g) ?? []).length + 1;
}
