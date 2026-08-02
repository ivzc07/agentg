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

/** The twelve month abbreviations for the active language. */
export function getMonths(): string[] {
  const raw = getI18N()["_months"];
  if (Array.isArray(raw)) return raw as string[];
  // Fallback: Spanish (the product's no-signal default).
  return ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
}

/** The seven weekday initials (Mon first) for the active language. */
export function getWeekdayInitials(): string[] {
  const raw = getI18N()["_weekday_initials"];
  if (Array.isArray(raw)) return raw as string[];
  // Fallback: English.
  return ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
}

/** The active language ("en" | "es"); Spanish is the no-signal default. */
export function getLang(): string {
  const raw = getI18N()["_lang"];
  if (raw === "en" || raw === "es") return raw;
  return "es";
}

/** The decimal mark for the active language (e.g. "." for en, "," for es). */
export function getDecimalMark(): string {
  const raw = getI18N()["_decimal_mark"];
  if (typeof raw === "string") return raw;
  // Fallback: period.
  return ".";
}
