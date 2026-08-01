/** Types and helpers for window.__I18N__ (ADR 0004 §i18n 7a). */

export type I18NStrings = Record<string, string | string[]>;

/** Read the bootstrap-injected i18n strings. */
export function getI18N(): I18NStrings {
  if (typeof window === "undefined" || !window.__I18N__) {
    return {};
  }
  return window.__I18N__;
}

/** The seven full weekday names (Mon first) for the active language. */
export function getWeekdays(): string[] {
  const raw = getI18N()["_weekdays"];
  if (Array.isArray(raw)) return raw as string[];
  // Fallback: English.
  return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
}
