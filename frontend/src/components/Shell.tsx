import { useT } from "../hooks/useT";

export interface ShellProps {
  /** The coach's name from /api/session. */
  name: string;
  /** The gym name from /api/session. */
  gym: string;
}

/**
 * The thinnest vertical slice: a coach signs in and sees a React shell
 * that greets them by name, proving build -> auth -> API -> fetch ->
 * render -> i18n end to end (issue #155).
 */
export function Shell({ name, gym }: ShellProps) {
  const t = useT();

  return (
    <div className="min-h-screen bg-bg text-ink font-sans antialiased">
      {/* Top bar — same shape as the server-rendered chrome */}
      <header className="sticky top-0 z-20 flex items-center gap-1.5 flex-wrap min-h-[46px] px-gut py-1.5 bg-elevation-0 border-b border-elevation-0-stroke shadow-elevation-1">
        <h1 className="text-[17px] font-semibold tracking-[-0.01em] overflow-hidden text-ellipsis whitespace-nowrap min-w-0">
          {gym}
        </h1>
        <span className="spacer flex-1" />
        <span className="text-[13px] text-ink-2">{name}</span>
      </header>

      {/* Main shell content */}
      <main className="max-w-2xl mx-auto px-gut py-12">
        <span className="eyebrow">{t("member_eyebrow")}</span>
        <h1 className="text-[33px] leading-tight mt-1">{name}</h1>
        <p className="text-ink-2 mt-4">
          {t("settings")}: {gym}
        </p>
      </main>
    </div>
  );
}
