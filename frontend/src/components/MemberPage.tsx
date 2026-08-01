import { useParams, useSearchParams, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "../hooks/useT";
import { getMonths, getWeekdayInitials, getDecimalMark } from "../lib/i18n";
import { fetchMember, MemberAuthError, MemberNotFoundError, tickOffFlag } from "../api/member";
import type { MemberPageData, SafetyFlag } from "../types/member";
import type { RosterMember } from "../types/roster";

/** Format a date string for display. Uses months from the server-injected
 *  i18n bootstrap (``_months`` in ``window.__I18N__``). */
function fmtDate(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  const day = d.getDate();
  const months = getMonths();
  const month = months[d.getMonth()];
  return `${day} ${month} ${d.getFullYear()}`;
}

/** Format a weight with the gym's unit. Uses the bodyweight key from
 *  the server i18n bootstrap and the language's decimal mark. */
function fmtWeight(weight: number | null, unit: string, t: (key: string) => string): string {
  if (weight == null) return t("bodyweight");
  const dm = getDecimalMark();
  const formatted = String(weight).replace(".", dm);
  return `${formatted} ${unit}`;
}

function SafetyBanner({
  flags,
  memberId,
  t,
}: {
  flags: SafetyFlag[];
  memberId: number;
  t: (key: string) => string;
}) {
  const queryClient = useQueryClient();
  const tickOff = useMutation({
    mutationFn: (noteId: number) => tickOffFlag(memberId, noteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["member", memberId] });
    },
  });

  if (flags.length === 0) return null;

  return (
    <section className="card card-elevated rounded-sm border border-elevation-0-stroke bg-elevation-1 p-4 mb-5">
      <h2 className="text-[15px] font-semibold mb-3">{t("safety_section")}</h2>
      <div className="flex flex-col gap-3">
        {flags.map((flag) => (
          <div
            key={flag.note_id}
            className="flag flex items-start justify-between gap-3 p-3 bg-coral-tint border border-coral/20 rounded-sm"
          >
            <div className="flag-body min-w-0">
              <b className="text-[14px] block">{flag.text}</b>
              <div className="flag-meta text-[12px] text-ink-3 mt-0.5">
                {fmtDate(flag.on)}
              </div>
            </div>
            {flag.status === "open" ? (
              <button
                type="button"
                onClick={() => tickOff.mutate(flag.note_id)}
                disabled={tickOff.isPending}
                className="flex-shrink-0 text-[13px] min-h-[36px] px-3 py-1 bg-elevation-2 border border-elevation-2-stroke rounded-sm hover:border-ink-2 transition-colors duration-fast"
              >
                {t("tick_off")}
              </button>
            ) : flag.status === "acknowledged" ? (
              <span className="flag-feedback ack text-[12px] text-ink-3 flex-shrink-0">
                {t("flag_seen_by")
                  .replace("{who}", flag.acknowledged_by ?? "—")
                  .replace(
                    "{date}",
                    flag.acknowledged_on ? fmtDate(flag.acknowledged_on) : ""
                  )}
              </span>
            ) : (
              <span className="flag-feedback exp text-[12px] text-ink-3 flex-shrink-0">
                {t("flag_expired_unseen")}
              </span>
            )}
          </div>
        ))}
      </div>
      {tickOff.isError && (
        <p className="text-coral text-[13px] mt-2">
          {t("save_failed")}
        </p>
      )}
    </section>
  );
}

function RoutineCard({
  data,
  t,
}: {
  data: MemberPageData;
  t: (key: string) => string;
}) {
  const weekdays = getWeekdayInitials();

  return (
    <section className="card card-elevated rounded-sm border border-elevation-0-stroke bg-elevation-1 p-4">
      <h2 className="text-[15px] font-semibold mb-3 flex items-center gap-2">
        <span className="opacity-60" aria-hidden="true">📋</span>
        {t("routine")}
        {data.routine_preset_name && (
          <span className="tag text-[10px] ml-2">
            {t("preset_chip").replace("{name}", data.routine_preset_name)}
          </span>
        )}
        {!data.routine_preset_name && (
          <span className="tag text-[10px] ml-2">
            {data.coach_authored
              ? data.routine_author
                ? t("chip_coach_named").replace("{name}", data.routine_author)
                : t("chip_coach")
              : t("chip_agent")}
          </span>
        )}
      </h2>
      {data.routine.length === 0 ? (
        <p className="muted text-[13px] text-ink-3">{t("no_routine")}</p>
      ) : (
        <div className="flex flex-col gap-2">
          {data.routine.map((day, i) => (
            <div key={i} className="day">
              <b className="text-[13px]">{weekdays[day.weekday]}</b>
              <span className="dayname text-[13px] text-ink-2 ml-2">
                {day.name}
              </span>
              <ul className="list-none m-0 p-0 mt-1">
                {day.exercises.map((ex, j) => (
                  <li
                    key={j}
                    className="text-[13px] text-ink-2 pl-3"
                  >
                    {ex.name}
                    {ex.sets != null || ex.reps != null
                      ? ` — ${[ex.sets, ex.reps].filter(Boolean).join(" × ")}`
                      : ""}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function SessionsCard({
  data,
  t,
}: {
  data: MemberPageData;
  t: (key: string) => string;
}) {
  const { sessions, page, pages, member_id: memberId } = data;

  return (
    <section className="card card-elevated rounded-sm border border-elevation-0-stroke bg-elevation-1 p-4" id="sessions">
      <h2 className="text-[15px] font-semibold mb-3 flex items-center gap-2">
        <span className="opacity-60" aria-hidden="true">📊</span>
        {t("sessions")}
      </h2>
      {sessions.length === 0 ? (
        <p className="muted text-[13px] text-ink-3">{t("no_sessions_yet")}</p>
      ) : (
        <div className="flex flex-col gap-3">
          {sessions.map((session, i) => {
            const count = session.sets.length;
            const headline =
              count === 0
                ? t("visit_no_sets")
                : count === 1
                  ? t("one_set")
                  : t("n_sets").replace("{n}", String(count));

            // Collapse sets by (exercise, weight)
            const collapsed: Map<string, { weight: number | null; repsList: number[]; notes: string[] }> = new Map();
            for (const s of session.sets) {
              const key = `${s.exercise}|${s.weight}`;
              const existing = collapsed.get(key);
              if (existing) {
                existing.repsList.push(s.reps);
                if (s.note && !existing.notes.includes(s.note)) {
                  existing.notes.push(s.note);
                }
              } else {
                collapsed.set(key, {
                  weight: s.weight,
                  repsList: [s.reps],
                  notes: s.note ? [s.note] : [],
                });
              }
            }

            return (
              <div key={i} className="sess">
                <b className="text-[13px]">{fmtDate(session.on)}</b>{" "}
                <span className="muted text-[12px] text-ink-3">{headline}</span>
                {Array.from(collapsed.entries()).map(
                  ([key, { weight, repsList, notes }]) => {
                    const exName = key.split("|")[0];
                    return (
                      <div key={key}>
                        <div className="set text-[13px] text-ink-2 mt-1">
                          {exName} {fmtWeight(weight, data.weight_unit, t)} × {" "}
                          {repsList.join(",")}
                        </div>
                        {notes.map((note, ni) => (
                          <div
                            key={ni}
                            className="said text-[12px] text-ink-3 italic mt-0.5 pl-2"
                          >
                            “{note}”
                          </div>
                        ))}
                      </div>
                    );
                  }
                )}
              </div>
            );
          })}
        </div>
      )}
      {pages > 1 && (
        <nav
          className="pages flex items-center gap-2 mt-3 pt-2 border-t border-elevation-0-stroke text-[13px]"
          aria-label={t("sessions")}
        >
          {page > 1 ? (
            <Link
              to={`/members/${memberId}?page=${page - 1}`}
              className="text-ink-2 hover:text-ink transition-colors duration-fast"
            >
              {t("newer_page")}
            </Link>
          ) : (
            <span />
          )}
          <span className="muted text-ink-3">
            {t("page_x_of_y")
              .replace("{page}", String(page))
              .replace("{pages}", String(pages))}
          </span>
          {page < pages ? (
            <Link
              to={`/members/${memberId}?page=${page + 1}`}
              className="text-ink-2 hover:text-ink transition-colors duration-fast"
            >
              {t("older_page")}
            </Link>
          ) : (
            <span />
          )}
        </nav>
      )}
    </section>
  );
}

function WeightsCard({
  data,
  t,
}: {
  data: MemberPageData;
  t: (key: string) => string;
}) {
  return (
    <section className="card rounded-sm border border-elevation-0-stroke bg-elevation-1 p-4">
      <h2 className="text-[15px] font-semibold mb-3 flex items-center gap-2">
        <span className="opacity-60" aria-hidden="true">⚖️</span>
        {t("last_weights")}
      </h2>
      {data.weights.length === 0 ? (
        <p className="muted text-[13px] text-ink-3">{t("nothing_logged")}</p>
      ) : (
        <ul className="list-none m-0 p-0 flex flex-col gap-1.5">
          {data.weights.map((w, i) => (
            <li key={i} className="weight-line text-[13px]">
              <b>{w.exercise}</b>{" "}
              <span className="numeral-sm font-mono font-bold">
                {fmtWeight(w.weight, data.weight_unit, t)}
              </span>{" "}
              × {w.reps.join(",")}{" "}
              <span className="muted text-ink-3">· {fmtDate(w.on)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function NotesCard({
  data,
  t,
}: {
  data: MemberPageData;
  t: (key: string) => string;
}) {
  const { notes, retired_notes: retired } = data;

  return (
    <section className="card rounded-sm border border-elevation-0-stroke bg-elevation-1 p-4">
      <h2 className="text-[15px] font-semibold mb-3">{t("notes")}</h2>
      {notes.length === 0 && retired.length === 0 ? (
        <p className="muted text-[13px] text-ink-3">{t("no_notes")}</p>
      ) : (
        <>
          {notes.map((note, i) => (
            <div key={i} className="note text-[13px] mb-1.5">
              <span className="tag text-[10px] mr-1">{note.kind}</span>
              {note.text}{" "}
              <span className="muted text-ink-3">
                · {fmtDate(note.on)}
              </span>
            </div>
          ))}
          {retired.length > 0 && (
            <details className="tail mt-2 pt-2 border-t border-elevation-0-stroke">
              <summary className="text-[12px] text-ink-2 cursor-pointer hover:text-ink transition-colors duration-fast">
                {t("retired_tail").replace("{n}", String(retired.length))}
              </summary>
              {retired.map((note, i) => (
                <div key={i} className="note text-[13px] mt-1.5 ml-2 opacity-60">
                  <span className="tag text-[10px] mr-1">{note.kind}</span>
                  {note.text}{" "}
                  <span className="muted text-ink-3">
                    · {t("retired_on").replace(
                      "{date}",
                      note.retired_on ? fmtDate(note.retired_on) : ""
                    )}
                  </span>
                </div>
              ))}
            </details>
          )}
        </>
      )}
    </section>
  );
}

/** Bare member body — headline, chips, facts, safety banner, cards.
 *  Used in Split's right pane and as the core of the standalone page. */
export function MemberPane({
  data,
  t,
}: {
  data: MemberPageData;
  t: (key: string) => string;
}) {
  // Status chips
  const chips: string[] = [];
  if (data.lapsed) chips.push(t("lapsed_tag"));
  if (data.snoozed_until) {
    chips.push(
      t("snoozed_tag").replace(
        "{date}",
        fmtDate(data.snoozed_until)
      )
    );
  }

  // Facts line
  const countLabel =
    data.session_count === 1
      ? t("one_session")
      : t("n_sessions").replace("{n}", String(data.session_count));

  const gapLabel = !data.has_sessions
    ? t("no_sessions_yet")
    : data.gap_days === 0
      ? t("trained_today")
      : data.gap_days === 1
        ? t("one_day_away")
        : t("days_away").replace("{n}", String(data.gap_days));

  const factsParts = [
    t("member_since").replace("{date}", fmtDate(data.member_since)),
    countLabel,
    gapLabel,
  ];
  if (data.last_session_on) {
    factsParts.push(
      t("last_session").replace("{date}", fmtDate(data.last_session_on))
    );
  }

  return (
    <>
      {/* Headline */}
      <span className="eyebrow">{t("member_eyebrow")}</span>
      <h1 className="text-[28px] leading-tight mt-1">{data.name}</h1>

      {/* Status chips */}
      {chips.length > 0 && (
        <div className="chips flex gap-2 mt-2">
          {chips.map((chip, i) => (
            <span key={i} className="tag text-[10px]">
              {chip}
            </span>
          ))}
        </div>
      )}

      {/* Facts line */}
      <div className="facts text-[13px] text-ink-2 mt-3 flex flex-wrap gap-x-3 gap-y-1">
        {factsParts.map((part, i) => (
          <span key={i}>
            {i > 0 && (
              <span className="mx-1.5 text-ink-3" aria-hidden="true">·</span>
            )}
            {part}
          </span>
        ))}
      </div>

      {/* Safety banner */}
      <div className="mt-5">
        <SafetyBanner
          flags={data.safety_flags}
          memberId={data.member_id}
          t={t}
        />
      </div>

      {/* Two-column layout */}
      <div className="columns grid grid-cols-1 md:grid-cols-2 gap-5 mt-5">
        <div className="col flex flex-col gap-5">
          <RoutineCard data={data} t={t} />
          <SessionsCard data={data} t={t} />
        </div>
        <div className="col flex flex-col gap-5">
          <WeightsCard data={data} t={t} />
          <NotesCard data={data} t={t} />
        </div>
      </div>
    </>
  );
}

/** Standalone member page — wraps MemberPane in chrome (sticky header,
 *  back link, min-h-screen) for the full-page route. */
export function MemberPageContent({
  data,
  t,
  rosterView,
}: {
  data: MemberPageData;
  t: (key: string) => string;
  rosterView?: string;
}) {
  const backTo = rosterView ? `/?view=${rosterView}` : "/";

  return (
    <div className="min-h-screen bg-bg text-ink font-sans antialiased">
      {/* Sticky header with back link */}
      <header className="sticky top-0 z-20 flex items-center gap-2 min-h-[46px] px-gut py-1.5 bg-elevation-0 border-b border-elevation-0-stroke shadow-elevation-1">
        <Link
          to={backTo}
          className="text-[13px] text-ink-2 hover:text-ink transition-colors duration-fast"
        >
          ← {t("back_to_roster")}
        </Link>
      </header>

      <main className="max-w-2xl mx-auto px-gut py-6">
        <MemberPane data={data} t={t} />
      </main>
    </div>
  );
}

/** Full member page — fetches from /api/members/{id} and renders
 *  Routine / Sessions / Weights / Notes cards plus safety flags.
 *  Works in Split's right pane and as a standalone deep link. */
export function MemberPage({ member: paneMember }: { member?: RosterMember } = {}) {
  const { memberId } = useParams<{ memberId: string }>();
  const [sp] = useSearchParams();
  const t = useT();
  // The Split rail passes the member it selected; a deep link carries it in
  // the URL. Either way the full detail comes from /api/members/{id}.
  const id = paneMember ? paneMember.member_id : memberId != null ? Number(memberId) : 0;
  const page = Number(sp.get("page") ?? 1);
  const rosterView = sp.get("view") ?? "table";

  const { data, isLoading, error } = useQuery({
    queryKey: ["member", id, page],
    queryFn: () => fetchMember(id, page),
    enabled: id > 0,
    staleTime: 30_000,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[200px] text-ink-3 text-[14px]">
        Loading…
      </div>
    );
  }

  if (error instanceof MemberAuthError) {
    return (
      <div className="flex items-center justify-center min-h-[200px] text-ink-3 text-[14px]">
        Not signed in.
      </div>
    );
  }

  if (error instanceof MemberNotFoundError) {
    return (
      <div className="flex items-center justify-center min-h-[200px] text-ink-3 text-[14px]">
        404
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[200px] text-ink-3 gap-4">
        <p className="text-[14px]">Something went wrong.</p>
        <button
          onClick={() => window.location.reload()}
          className="px-4 py-2 rounded-sm bg-elevation-1 border border-elevation-0-stroke text-ink hover:bg-elevation-2 transition-colors duration-fast text-[13px]"
        >
          Retry
        </button>
      </div>
    );
  }

  // In Split view the rail stays mounted and the member fills the right pane
  // without chrome; a deep link to /members/:id is the standalone screen and
  // brings its own header and back link.
  if (paneMember) {
    return <MemberPane data={data} t={t} />;
  }

  return (
    <MemberPageContent data={data} t={t} rosterView={rosterView} />
  );
}
