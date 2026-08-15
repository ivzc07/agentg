import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { Search, LayoutList, LayoutGrid, Columns2, Users } from "lucide-react";
import { fetchRoster } from "../api/roster";
import type { RosterView } from "../types/roster";
import { filterMembers } from "./roster-utils";
import { useT } from "../hooks/useT";
import { AppHeader } from "./AppHeader";
import { RosterTable } from "./RosterTable";
import { RosterCards } from "./RosterCards";
import { RosterSplit } from "./RosterSplit";
import { RosterQueue } from "./RosterQueue";

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

export function RosterShell({ name: _name, gym }: RosterShellProps) {
  const t = useT();
  // The view rides in the URL (?view=cards|split) exactly as it did on the
  // server-rendered roster, so bookmarks and the deep links other screens
  // build keep working across the #154 cutover. An unknown value falls
  // back to the table, like the server's _view_of did.
  const [searchParams, setSearchParams] = useSearchParams();
  const rawView = searchParams.get("view");
  const view: RosterView =
    rawView === "cards" || rawView === "split" ? rawView : "table";
  const setView = (v: RosterView) =>
    setSearchParams(v === "table" ? {} : { view: v }, { replace: true });
  const [query, setQuery] = useState("");
  const [lapsedOpen, setLapsedOpen] = useState(false);

  const { data, isLoading, error, refetch } = useQuery({
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
      <AppHeader gym={gym}>
        <div className="px-gut py-8 lg:px-10 lg:py-9" aria-busy="true">
          <span className="sr">Loading…</span>
          <div className="h-8 w-40 skeleton rounded-sm mb-6" />
          <div className="h-9 max-w-md skeleton rounded-sm mb-6" />
          <ul className="space-y-0" aria-hidden="true">
            {[0, 1, 2, 3, 4].map((i) => (
              <li key={i} className="flex items-center gap-3 py-3 border-b border-elevation-0-stroke">
                <span className="w-7 h-7 rounded-sm skeleton" />
                <span className="flex-1 space-y-1.5">
                  <span className="block h-3.5 w-32 skeleton rounded-xs" />
                  <span className="block h-2.5 w-20 skeleton rounded-xs" />
                </span>
                <span className="h-3 w-16 skeleton rounded-xs" />
              </li>
            ))}
          </ul>
        </div>
      </AppHeader>
    );
  }

  if (error || !data) {
    return (
      <AppHeader gym={gym}>
        <div className="flex flex-col items-center justify-center min-h-[280px] text-coral gap-4 px-gut">
          <p>{t("roster_error")}</p>
          <button
            onClick={() => refetch()}
            className="px-4 py-2 rounded-sm bg-elevation-1 border border-elevation-0-stroke text-ink hover:bg-elevation-2 transition-colors"
          >
            {t("roster_retry")}
          </button>
        </div>
      </AppHeader>
    );
  }

  const totalActive = data.counts.active;
  const empty = data.active.length === 0 && data.lapsed.length === 0;
  const noMatch =
    !empty && filtered.active.length === 0 && filtered.lapsed.length === 0;
  const hot = filtered.active.filter((m) => m.severity === "red").length;
  const warm = filtered.active.filter((m) => m.severity === "amber").length;
  const flagged = filtered.active.filter((m) => m.has_safety_flag).length;
  const summaryBits = [
    hot > 0 ? { key: "hot", text: t("summary_hot").replace("{n}", String(hot)), tone: "text-coral" } : null,
    warm > 0 ? { key: "warm", text: t("summary_warm").replace("{n}", String(warm)), tone: "text-amber" } : null,
    flagged > 0 ? { key: "flag", text: t("summary_flag").replace("{n}", String(flagged)), tone: "text-purple" } : null,
  ].filter((bit): bit is { key: string; text: string; tone: string } => bit != null);

  return (
    <AppHeader gym={gym}>
      <main className="roster-body mx-auto w-full max-w-[1440px] px-4 py-5 sm:px-6 lg:px-8 lg:py-8">
        <div className="mb-7 flex flex-wrap items-end gap-x-4 gap-y-4 border-b border-elevation-0-stroke pb-6">
          <div className="min-w-0">
            <p className="text-[12px] font-medium text-ink-3">
              {t("queue_label")}
            </p>
            <h2 className="mt-1 text-[32px] font-semibold leading-tight tracking-[-0.035em]">
              {t("nav_roster")}
            </h2>
            <p className="count mt-2 text-[13px] text-ink-2 tabular-nums" id="members-count">
              {query
                ? t("match_count")
                    .replace("{shown}", String(filtered.active.length))
                    .replace("{total}", String(totalActive))
                : t("queue_counts")
                    .replace("{active}", String(totalActive))
                    .replace("{lapsed}", String(data.counts.lapsed))}
            </p>
          </div>
          <span className="flex-1" />
          <div className="relative w-full sm:w-56">
            <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-ink-2" />
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
              className="h-10 w-full rounded-md border border-elevation-0-stroke bg-white pr-3 pl-9 text-[13px] text-ink shadow-shadow-1 placeholder:text-ink-3 focus:border-ink-3 focus:outline-none transition-colors duration-fast"
            />
          </div>
          <nav
            className="seg flex h-10 overflow-hidden rounded-md border border-elevation-0-stroke bg-white p-1 shadow-shadow-1"
            aria-label={t("nav_views")}
          >
            {(Object.keys(VIEW_ICONS) as RosterView[]).map((v) => {
              const Icon = VIEW_ICONS[v];
              const active = v === view;
              return (
                <button
                  key={v}
                  onClick={() => setView(v)}
                  className={`flex min-h-0 h-8 items-center gap-1.5 rounded-sm border-0 px-3 text-[12px] font-medium transition-colors duration-fast ${
                    active
                      ? "bg-ink text-white"
                      : "bg-transparent text-ink-2 hover:bg-elevation-1 hover:text-ink"
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
        </div>
        {view !== "table" && summaryBits.length > 0 && (
          <p className="flex flex-wrap gap-x-3 gap-y-1 text-[13px] tabular-nums mb-6">
            {summaryBits.map((bit, i) => (
              <span key={bit.key} className={bit.tone}>
                {i > 0 && <span className="text-ink-3 mr-3" aria-hidden="true">·</span>}
                {bit.text}
              </span>
            ))}
          </p>
        )}
        {empty ? (
          <div className="emptystate flex flex-col items-center justify-center min-h-[360px] text-center px-gut">
            <span className="chip-icon flex items-center justify-center w-12 h-12 rounded-sm bg-elevation-1 text-ink-3 mb-4" aria-hidden="true">
              <Users className="w-5 h-5" />
            </span>
            <h2 className="text-[20px] font-semibold text-ink">
              {t("empty_roster_title")}
            </h2>
            <p className="text-[14px] text-ink-2 mt-2 max-w-sm leading-relaxed">
              {t("empty_roster_body")}
            </p>
          </div>
        ) : (
          <>
            {/* No match state */}
            {noMatch && (
              <p className="emptystate text-center py-12 text-ink-3">
                {t("no_matches")}
              </p>
            )}

            {/* Active roster — Table/Cards/Split views. Split keeps the
                rail and renders the selected member inline (local state);
                Table and Cards rows link to /members/:id, a sibling route
                that renders the member full-page without roster chrome. */}
            {!noMatch && (
              <>
                {view === "table" && <RosterQueue members={filtered.active} />}
                {view === "cards" && <RosterCards members={filtered.active} />}
                {view === "split" && <RosterSplit members={filtered.active} />}
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
                  className="py-2 text-[13px] text-ink-2 cursor-pointer hover:text-ink transition-colors duration-fast"
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
    </AppHeader>
  );
}
