import { useParams, Link } from "react-router-dom";
import { useT } from "../hooks/useT";

/** Placeholder member page for React Router deep links (issue #149).
 *  The full Member page is a future screen; this gives the roster's
 *  member links a destination that resolves instead of 404'ing. */
export function MemberPage() {
  const { memberId } = useParams<{ memberId: string }>();
  const t = useT();

  return (
    <div className="min-h-screen bg-bg text-ink font-sans antialiased">
      <header className="sticky top-0 z-20 flex items-center gap-2 min-h-[46px] px-gut py-1.5 bg-elevation-0 border-b border-elevation-0-stroke shadow-elevation-1">
        <Link
          to="/"
          className="text-[13px] text-ink-2 hover:text-ink transition-colors duration-fast"
        >
          ← {t("back_to_roster")}
        </Link>
      </header>
      <main className="max-w-2xl mx-auto px-gut py-8">
        <span className="eyebrow">{t("member_eyebrow")}</span>
        <h1 className="text-[28px] leading-tight mt-1">
          {t("member_eyebrow")} #{memberId}
        </h1>
        <p className="text-ink-2 mt-4">
          Full member page coming in a future screen.
        </p>
        <p className="text-ink-3 mt-2 text-[13px]">
          <Link to="/" className="underline hover:text-ink-2 transition-colors duration-fast">
            {t("back_to_roster")}
          </Link>
        </p>
      </main>
    </div>
  );
}
