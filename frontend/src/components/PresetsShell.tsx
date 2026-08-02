import { LangToggle } from "./LangToggle";
import type { ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { useT } from "../hooks/useT";

interface PresetsShellProps {
  name: string;
  gym: string;
  children: ReactNode;
}

/**
 * Minimal top-bar shell for the Presets screen.
 * Matches the RosterShell chrome: gym name, presets/settings nav with active
 * state, and the coach's name.
 */
export function PresetsShell({ name: _name, gym, children }: PresetsShellProps) {
  const t = useT();
  const { pathname } = useLocation();

  return (
    <div className="min-h-screen bg-bg text-ink font-sans antialiased">
      {/* Top bar */}
      <header className="sticky top-0 z-20 flex items-center gap-2 flex-wrap min-h-[46px] px-gut py-1.5 bg-elevation-0 border-b border-elevation-0-stroke shadow-elevation-1">
        <h1 className="text-[17px] font-semibold tracking-[-0.01em] overflow-hidden text-ellipsis whitespace-nowrap min-w-0">
          {gym}
        </h1>

        <span className="flex-1" />

        <Link
          to="/"
          className="text-[13px] text-ink-2 hover:text-ink transition-colors"
        >
          {t("back_to_roster")}
        </Link>

        {/* Quick links: Presets & Settings */}
        <nav
          className="quick flex gap-2 text-[13px]"
          aria-label={t("nav_sections")}
        >
          <Link
            to="/presets"
            className={`transition-colors ${
              pathname.startsWith("/presets")
                ? "text-ink font-semibold"
                : "text-ink-2 hover:text-ink"
            }`}
            aria-current={pathname.startsWith("/presets") ? "page" : undefined}
          >
            {t("presets")}
          </Link>
          <Link
            to="/settings"
            className="text-ink-2 hover:text-ink transition-colors"
          >
            {t("settings")}
          </Link>
        </nav>

        <LangToggle />
      </header>

      {children}
    </div>
  );
}
