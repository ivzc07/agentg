import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams, useSearchParams, Outlet } from "react-router-dom";
import { Search, LayoutList, LayoutGrid, Columns2, Users } from "lucide-react";
import { fetchRoster } from "../api/roster";
import type { RosterView } from "../types/roster";
import { filterMembers } from "./roster-utils";
import { useT } from "../hooks/useT";
import { RosterTable } from "./RosterTable";
import { RosterCards } from "./RosterCards";
import { RosterSplit } from "./RosterSplit";
import type { RosterOutletContext } from "./MemberPage";

interface RosterShellProps {
  /** The coach's name from /api/session. */
  name: string;
  /** The gym name from /api/session. */
  gym: string;
}

const VIEW_ICONS: Record<RosterView, typeof LayoutList> = {
  table: LayoutList,
  cards: LayoutGrid,
  split: Columns2,
};

function readInitialView(sp: URLSearchParams): RosterView {
  const v = sp.get("view");
  if (v === "table" || v === "cards" || v === "split") return v;
  return "table";
}

export function RosterShell({ name: _name, gym }: RosterShellProps) {
  const t = useT();
  const [sp] = useSearchParams();
  const [view, setView] = useState<RosterView>(() => readInitialView(sp));
  const [query, setQuery] = useState("");
  const [lapsedOpen, setLapsedOpen] = useState(false);
  const { memberId } = useParams<{ memberId: string }>();
  const selectedMemberId = memberId != null ? Number(memberId) : undefined;

  const { data, isLoading, error } = useQuery({
    queryKey: ["roster"],
    queryFn: fetchRoster,
    staleTime: 30_000,
  });

  const filtered = useMemo(() => {
    if (!data) return { active: [], lapsed: [] };
    return {
      active: filterMembers(data.active, query),
      lapsed: filterMembers(data.lapsed, query),
    };
  }, [data, query]);

  const lapsedShown: boolean = query ? filtered.lapsed.length > 0 ? true : lapsedOpen : lapsedOpen;
  const anyLapsedMatch: boolean = !!(query && filtered.lapsed.length > 0);

  // Auto-expand lapsed when query matches lapsed members
  const effectiveLapsedOpen = lapsedShown || anyLapsedMatch;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[200px] text-ink-2">
        Loading…
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex items-center justify-center min-h-[200px] text-coral">
        {t("no_sessions_yet") /* fallback: couldn't load roster */}
      </div>
    );
  }

  const totalActive = data.counts.active;
  const empty = data.active.length === 0 && data.lapsed.length === 0;
  const noMatch =
    !empty && filtered.active.length === 0 && filtered.lapsed.length === 0;

  const outletContext: RosterOutletContext = {
    members: data.active.concat(data.lapsed),
    rosterView: view,
  };

  return (
    <div className="min-h-screen bg-bg text-ink font-sans antialiased">
      {/* Top bar */}
      <header className="sticky top-0 z-20 flex items-center gap-2 flex-wrap min-h-[46px] px-gut py-1.5 bg-elevation-0 border-b border-elevation-0-stroke shadow-elevation-1">
        <h1 className="text-[17px] font-semibold tracking-[-0.01em] overflow-hidden text-ellipsis whitespace-nowrap min-w-0">
          {gym}
        </h1>

        {/* Member count */}
        <span className="count text-[13px] text-ink-2" id="members-count">
          {query
            ? t("match_count")
                .replace("{shown}", String(filtered.active.length))
                .replace("{total}", String(totalActive))
            : t("members_count").replace("{n}", String(totalActive))}
        </span>

        <span className="flex-1" />

        {/* View switcher */}
        <nav className="seg flex rounded-sm overflow-hidden border border-elevation-2-stroke" aria-label={t("nav_views")}>
          {(Object.keys(VIEW_ICONS) as RosterView[]).map((v) => {
            const Icon = VIEW_ICONS[v];
            const active = v === view;
            return (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`flex items-center gap-1 px-2.5 py-1.5 text-[12px] font-medium transition-colors duration-fast ${
                  active
                    ? "bg-ink text-bg"
                    : "text-ink-2 hover:text-ink hover:bg-elevation-2"
                }`}
                aria-current={active ? "page" : undefined}
                aria-label={t(`view_${v}`)}
              >
                <Icon className="w-3.5 h-3.5" />
                <span className="hidden sm:inline">{t(`view_${v}`)}</span>
              </button>
            );
          })}
        </nav>

        {/* Presets & Settings quick links */}
        <nav className="quick flex gap-2 text-[13px] text-ink-2" aria-label={t("nav_sections")}>
          <span>{t("presets")}</span>
          <span>{t("settings")}</span>
        </nav>
      </header>

      {/* Search bar */}
      <div className="px-gut py-2 border-b border-elevation-0-stroke">
        <div className="relative max-w-md">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-3 pointer-events-none" />
          <label className="sr" htmlFor="search">
            {t("search_placeholder")}
          </label>
          <input
            id="search"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("search_placeholder")}
            autoComplete="off"
            className="w-full pl-8 pr-3 py-2 bg-elevation-1 border border-elevation-1-stroke rounded-sm text-[14px] text-ink placeholder:text-ink-3 focus:outline-none focus:border-ink-2 transition-colors duration-fast"
          />
        </div>
      </div>

      {/* Main body */}
      <main className="roster-body">
        {empty ? (
          <div className="emptystate flex flex-col items-center justify-center min-h-[300px] text-center px-gut">
            <span className="chip-icon text-[32px] text-ink-3 mb-3" aria-hidden="true">
              ◎
            </span>
            <h2 className="text-[18px] font-semibold text-ink-2">
              {t("empty_roster_title")}
            </h2>
            <p className="text-[14px] text-ink-3 mt-2 max-w-sm">
              {t("empty_roster_body")}
            </p>
          </div>
        ) : (
          <>
            {/* Count bar */}
            <div className="countbar flex items-center gap-2 px-gut py-2 text-[13px] text-ink-2 border-b border-elevation-0-stroke">
              <span className="chip-icon" aria-hidden="true">
                ≡
              </span>
              {t("sorted_by_gap")}
              <span className="numeral text-[18px] font-mono font-bold text-ink ml-auto">
                {filtered.active.length}
              </span>
            </div>

            {/* Active roster */}
            {selectedMemberId != null ? (
              /* Nested route: /members/:id.  In Split view the rail stays
                 mounted and the member fills the right pane; in Table/Cards
                 the member renders full-page.  Always renders when a member
                 is selected — a non-matching search must not unmount an
                 open member page that the URL still points at. */
              view === "split" ? (
                <RosterSplit
                  members={filtered.active.concat(filtered.lapsed)}
                  selectedMemberId={selectedMemberId}
                />
              ) : (
                <Outlet context={outletContext} />
              )
            ) : (
              <>
                {/* No match state */}
                {noMatch && (
                  <p className="emptystate text-center py-12 text-ink-3">
                    {t("no_matches")}
                  </p>
                )}
                {!noMatch && view === "table" && <RosterTable members={filtered.active} />}
                {!noMatch && view === "cards" && <RosterCards members={filtered.active} />}
                {!noMatch && view === "split" && <RosterSplit members={filtered.active} />}
              </>
            )}

            {/* Lapsed tail */}
            {data.lapsed.length > 0 && (
              <details
                id="lapsed"
                className="mt-4 border-t border-elevation-0-stroke"
                open={effectiveLapsedOpen}
              >
                <summary
                  className="px-gut py-2 text-[13px] text-ink-2 cursor-pointer hover:text-ink transition-colors duration-fast"
                  onClick={(e) => {
                    e.preventDefault();
                    setLapsedOpen((prev) => !prev);
                  }}
                >
                  <Users className="inline w-3.5 h-3.5 mr-1.5" />
                  {t("lapsed_tail").replace("{n}", String(data.lapsed.length))}
                </summary>
                {effectiveLapsedOpen && (
                  <div className="border-t border-elevation-0-stroke">
                    <RosterTable members={filtered.lapsed} />
                  </div>
                )}
              </details>
            )}
          </>
        )}
      </main>
    </div>
  );
}
