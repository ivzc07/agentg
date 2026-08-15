import { getLang } from "../lib/i18n";

/**
 * The per-browser EN/ES toggle (issue #106; part of the chrome since the
 * server-HTML days, carried into the SPA by the #154 cutover).
 *
 * Deliberately plain anchors, not router links: `/lang/{lang}` is a server
 * route that persists the pick in a long-lived cookie and redirects back,
 * and the i18n bootstrap is injected per request — so a full page load is
 * required for the new language to take effect anyway.
 */
export function LangToggle() {
  const lang = getLang();
  const next =
    typeof window === "undefined"
      ? "/"
      : window.location.pathname + window.location.search;
  const q = `?next=${encodeURIComponent(next)}`;

  return (
    <span
      className="lang-toggle inline-flex items-center rounded-sm border border-elevation-2-stroke p-0.5 text-[11px] font-semibold tracking-[0.06em]"
      aria-label="Language"
    >
      {(["en", "es"] as const).map((l) => (
        <a
          key={l}
          href={`/lang/${l}${q}`}
          aria-current={l === lang ? "true" : undefined}
          className={
            l === lang
              ? "px-1.5 py-0.5 rounded-xs bg-ink text-bg"
              : "px-1.5 py-0.5 rounded-xs text-ink-3 hover:text-ink transition-colors duration-fast"
          }
        >
          {l.toUpperCase()}
        </a>
      ))}
    </span>
  );
}
