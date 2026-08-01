import { useParams, Link, useOutletContext } from "react-router-dom";
import { useT } from "../hooks/useT";
import type { RosterMember } from "../types/roster";
import { gapText } from "./roster-utils";

/** Context provided by RosterShell via <Outlet />. */
export interface RosterOutletContext {
  members: RosterMember[];
}

/** Renders a member's detail — lean for now (issue #150 delivers the full
 *  page).  When nested inside the Split view's right pane the rail stays
 *  mounted; in Table/Cards the member renders full-page. */
export function MemberPage() {
  const { memberId } = useParams<{ memberId: string }>();
  const t = useT();
  const ctx = useOutletContext<RosterOutletContext | null>();
  const member = ctx?.members.find(
    (m) => m.member_id === (memberId != null ? Number(memberId) : 0)
  );

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
          {member ? member.name : `${t("member_eyebrow")} #${memberId}`}
        </h1>
        {member && (
          <p className="text-ink-2 mt-2">{gapText(member, t)}</p>
        )}
        <p className="text-ink-3 mt-4 text-[13px]">
          Full member page coming in a future screen.
        </p>
      </main>
    </div>
  );
}
