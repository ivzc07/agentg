import type { ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { LayoutGrid, SlidersHorizontal, Users } from "lucide-react";
import { LangToggle } from "./LangToggle";
import { useT } from "../hooks/useT";

interface AppHeaderProps {
  /** Gym name shown as the operator workspace. */
  gym?: string;
  /** Optional leading slot — typically a back link. */
  leading?: ReactNode;
  /** Optional trailing slot. */
  trailing?: ReactNode;
  /** Show the signed-in product navigation. Default true. */
  showNav?: boolean;
  /** Main column content. */
  children?: ReactNode;
  /** Retained for route compatibility; all signed-in routes share GoGym v5. */
  variant?: "default" | "settings-brand";
}

/**
 * Shared GoGym v5 operator chrome: dark 272px desktop console, compact mobile
 * header, and bottom navigation on narrow screens.
 */
export function AppHeader({
  gym,
  leading,
  trailing,
  showNav = true,
  children,
  variant: _variant = "default",
}: AppHeaderProps) {
  const t = useT();
  const { pathname } = useLocation();
  const onRoster = pathname === "/";
  const onPresets = pathname.startsWith("/presets");
  const onSettings = pathname.startsWith("/settings");

  const items = [
    { to: "/", label: t("nav_roster"), current: onRoster, Icon: Users },
    { to: "/presets", label: t("presets"), current: onPresets, Icon: LayoutGrid },
    { to: "/settings", label: t("settings"), current: onSettings, Icon: SlidersHorizontal },
  ];

  if (!showNav) {
    return (
      <header className="sticky top-0 z-20 flex min-h-14 flex-wrap items-center gap-3 border-b border-elevation-0-stroke bg-white px-gut">
        {leading}
        <span className="flex-1" />
        {trailing}
        <LangToggle />
      </header>
    );
  }

  return (
    <div className="min-h-screen bg-bg font-sans text-ink antialiased lg:flex">
      <aside className="hidden h-screen w-[272px] shrink-0 flex-col bg-ink text-white lg:sticky lg:top-0 lg:flex">
        <div className="px-5 pb-7 pt-6">
          <div className="flex items-center gap-3.5">
            <Link
              to="/"
              className="grid size-11 shrink-0 place-items-center rounded-xl bg-lime text-[16px] font-bold text-ink shadow-[0_0_32px_rgba(199,255,0,0.18)]"
              aria-label={gym ?? t("nav_roster")}
            >
              G
            </Link>
            <div className="min-w-0">
              {gym && (onRoster ? (
                <h1 className="truncate text-[14px] font-semibold tracking-[-0.01em] text-white">
                  {gym}
                </h1>
              ) : (
                <Link to="/" className="block truncate text-[14px] font-semibold tracking-[-0.01em] text-white hover:text-white/75">
                  {gym}
                </Link>
              ))}
              <span className="mt-1 block font-mono text-[9px] uppercase tracking-[0.18em] text-white/40">
                {t("nav_dashboard")}
              </span>
            </div>
          </div>
        </div>

        <nav className="flex-1 px-3" aria-label={t("nav_sections")}>
          <p className="px-3 pb-2 pt-3 font-mono text-[9px] uppercase tracking-[0.18em] text-white/30">
            {t("nav_workspace")}
          </p>
          <div className="space-y-1.5">
            {items.map(({ to, label, current, Icon }) => (
              <Link
                key={to}
                to={to}
                aria-current={current ? "page" : undefined}
                className={`group relative flex h-12 items-center gap-3 rounded-xl px-3.5 text-[13px] font-medium transition-colors ${
                  current
                    ? "bg-white/10 text-white"
                    : "text-white/55 hover:bg-white/5 hover:text-white"
                }`}
              >
                {current && (
                  <span className="absolute -left-1 h-5 w-0.5 rounded-full bg-lime shadow-[0_0_12px_rgba(199,255,0,0.8)]" />
                )}
                <span className={`grid size-7 shrink-0 place-items-center rounded-lg ${
                  current
                    ? "bg-lime text-ink"
                    : "bg-white/5 text-white/45 group-hover:text-white"
                }`}>
                  <Icon className="size-3.5" aria-hidden="true" />
                </span>
                <span className="truncate">{label}</span>
              </Link>
            ))}
          </div>
        </nav>

        <div className="p-4">
          <div className="rounded-xl border border-white/10 bg-white/[0.04] p-3.5">
            <p className="mb-3 font-mono text-[9px] uppercase tracking-[0.16em] text-white/35">
              {t("nav_language")}
            </p>
            <div className="flex items-center justify-between border-t border-white/10 pt-3">
              <span className="text-[11px] text-white/50">EN / ES</span>
              <LangToggle />
            </div>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col pb-20 lg:pb-0">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-elevation-0-stroke bg-white/95 px-4 backdrop-blur lg:hidden">
          <Link
            to="/"
            className="flex size-9 shrink-0 items-center justify-center rounded-full bg-ink text-[14px] font-semibold text-lime"
            aria-label={gym ?? t("nav_roster")}
          >
            G
          </Link>
          <div className="min-w-0 flex-1">
            <span
              aria-hidden="true"
              data-label={gym ?? ""}
              className="block truncate text-[13px] font-semibold text-ink after:content-[attr(data-label)]"
            />
          </div>
          {trailing}
          <LangToggle />
        </header>

        {trailing ? (
          <div className="sticky top-0 z-10 hidden min-h-12 items-center gap-2 border-b border-elevation-0-stroke bg-bg px-gut lg:flex">
            {trailing}
          </div>
        ) : null}
        {children}
      </div>

      <nav className="fixed inset-x-0 bottom-0 z-30 grid h-16 grid-cols-3 border-t border-elevation-0-stroke bg-white/95 pb-[env(safe-area-inset-bottom)] backdrop-blur lg:hidden" aria-label={t("nav_sections")}>
        {items.map(({ to, label, current, Icon }) => (
          <Link
            key={to}
            to={to}
            aria-label={t("mobile_navigation_item").replace("{label}", label)}
            className={`flex flex-col items-center justify-center gap-1 text-[10px] font-medium transition-colors ${
              current ? "text-ink" : "text-ink-3"
            }`}
            aria-current={current ? "page" : undefined}
          >
            <Icon className={`size-[18px] ${current ? "text-lime-deep" : "text-ink-3"}`} aria-hidden="true" />
            <span
              aria-hidden="true"
              data-label={label}
              className="max-w-full truncate px-1 after:content-[attr(data-label)]"
            />
          </Link>
        ))}
      </nav>

    </div>
  );
}
