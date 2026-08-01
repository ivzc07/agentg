import { getI18N } from "../lib/i18n";

/**
 * ``useT()`` resolves strings from the server-injected ``window.__I18N__``
 * (ADR 0004 §i18n 7a). Returns a function ``t(key)`` that looks up the key
 * in the bootstrap payload.
 */
export function useT() {
  const strings = getI18N();
  return function t(key: string): string {
    return strings[key] ?? key;
  };
}
