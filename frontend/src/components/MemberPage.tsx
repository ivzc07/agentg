import { useParams, useSearchParams, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { useT } from "../hooks/useT";
import { getMonths, getWeekdayInitials, getDecimalMark, getLang } from "../lib/i18n";
import { AppHeader } from "./AppHeader";
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

/**
 * The small source-language tag a foreign quote carries ("EN · textual" /
 * "ES · as written") — the Member's own words never translate, so a quote
 * in another language than the page is marked instead (spec-dashboard
 * §Language; #154 carried this from the server renderer into React).
 */
function VerbatimTag({
  lang,
  t,
}: {
  lang: string | null;
  t: (key: string) => string;
}) {
  if (!lang || lang === getLang()) return null;
  return (
    <span className="langtag text-[10px] text-ink-3 uppercase ml-1 not-italic">
      {lang.toUpperCase()} · {t("verbatim_tag")}
    </span>
  );
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
    <section className="card card-elevated mb-5 rounded-xl border border-coral/30 bg-coral-tint p-5 shadow-shadow-1">
      <h2 className="text-[13px] font-semibold mb-3 tracking-[-0.01em]">{t("safety_section")}</h2>
      <div className="flex flex-col gap-3">
        {flags.map((flag) => (
          <div
            key={flag.note_id}
            className="flag flex items-start justify-between gap-3 rounded-lg border border-coral/20 bg-elevation-3 p-4"
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
    <section className="card card-elevated rounded-xl border border-elevation-0-stroke bg-elevation-3 p-5 shadow-shadow-1">
      <h2 className="mb-4 flex flex-wrap items-center gap-2 text-[20px] font-semibold tracking-[-0.02em]">
        {t("routine")}
        {data.routine_preset_name && (
          <span className="tag text-[10px]">
            {t("preset_chip").replace("{name}", data.routine_preset_name)}
          </span>
        )}
        {!data.routine_preset_name && (
          <span className="tag text-[10px]">
            {data.coach_authored
              ? data.routine_author
                ? t("chip_coach_named").replace("{name}", data.routine_author)
                : t("chip_coach")
              : t("chip_agent")}
          </span>
        )}
        <span className="hidden flex-1 sm:block" />
        {/* The Edit journey into the Routine editor — the entry point the
            server member page always had (#100); #154 carries it over. */}
        <Link
          to={`/members/${data.member_id}/routine`}
          className="edit ml-auto text-[12px] font-normal text-ink-2 transition-colors duration-fast hover:text-ink"
        >
          {t("edit")}
        </Link>
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

function RoutineBoard({
  data,
  t,
}: {
  data: MemberPageData;
  t: (key: string) => string;
}) {
  const weekdays = getWeekdayInitials();
  const source = data.routine_preset_name
    ? t("preset_chip").replace("{name}", data.routine_preset_name)
    : data.coach_authored
      ? data.routine_author
        ? t("chip_coach_named").replace("{name}", data.routine_author)
        : t("chip_coach")
      : t("chip_agent");

  return (
    <section aria-labelledby="routine-heading">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 id="routine-heading" className="text-[20px] font-semibold tracking-[-0.02em]">
          {t("routine")}
        </h2>
        <span className="tag text-[10px]">{source}</span>
      </div>

      {data.routine.length === 0 ? (
        <div className="rounded-xl border border-elevation-0-stroke bg-white p-5 text-[13px] text-ink-3 shadow-shadow-1">
          {t("no_routine")}
        </div>
      ) : (
        <div className="grid gap-px overflow-hidden rounded-xl border border-elevation-0-stroke bg-elevation-0-stroke shadow-shadow-1 sm:grid-cols-2 lg:grid-cols-3">
          {data.routine.map((day, index) => (
            <article
              key={`${day.weekday}-${index}`}
              className="bg-white p-4"
            >
              <div className="flex items-baseline justify-between gap-3">
                <b className="text-[14px]">{day.name}</b>
                <span className="font-mono text-[11px] text-ink-3">
                  {weekdays[day.weekday]}
                </span>
              </div>
              <ul className="mt-3 space-y-2">
                {day.exercises.map((exercise, exerciseIndex) => (
                  <li
                    key={`${exercise.name}-${exerciseIndex}`}
                    className="flex justify-between gap-3 text-[12px]"
                  >
                    <span className="text-ink-2">{exercise.name}</span>
                    {(exercise.sets != null || exercise.reps != null) && (
                      <span className="shrink-0 font-mono text-ink-3">
                        {[exercise.sets, exercise.reps].filter(Boolean).join(" × ")}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </article>
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
    <section className="card card-elevated rounded-xl border border-elevation-0-stroke bg-elevation-3 p-5 shadow-shadow-1" id="sessions">
      <h2 className="mb-4 flex items-center gap-2 text-[20px] font-semibold tracking-[-0.02em]">
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
            const collapsed: Map<
              string,
              {
                weight: number | null;
                repsList: number[];
                notes: { text: string; lang: string | null }[];
              }
            > = new Map();
            for (const s of session.sets) {
              const key = `${s.exercise}|${s.weight}`;
              const existing = collapsed.get(key);
              if (existing) {
                existing.repsList.push(s.reps);
                if (s.note && !existing.notes.some((n) => n.text === s.note)) {
                  existing.notes.push({ text: s.note, lang: s.note_lang });
                }
              } else {
                collapsed.set(key, {
                  weight: s.weight,
                  repsList: [s.reps],
                  notes: s.note ? [{ text: s.note, lang: s.note_lang }] : [],
                });
              }
            }

            return (
              <div key={i} className="sess pb-3 mb-3 border-b border-elevation-0-stroke last:border-0 last:pb-0 last:mb-0">
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
                            “{note.text}”
                            <VerbatimTag lang={note.lang} t={t} />
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
    <section className="card rounded-xl border border-elevation-0-stroke bg-elevation-3 p-5 shadow-shadow-1">
      <h2 className="mb-4 text-[20px] font-semibold tracking-[-0.02em]">
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
    <section className="card rounded-xl border border-elevation-0-stroke bg-elevation-3 p-5 shadow-shadow-1">
      <h2 className="mb-4 text-[20px] font-semibold tracking-[-0.02em]">{t("notes")}</h2>
      {notes.length === 0 && retired.length === 0 ? (
        <p className="muted text-[13px] text-ink-3">{t("no_notes")}</p>
      ) : (
        <>
          {notes.map((note, i) => (
            <div key={i} className="note text-[13px] mb-1.5">
              <span className="tag text-[10px] mr-1">{note.kind}</span>
              {note.text}
              <VerbatimTag lang={note.lang} t={t} />{" "}
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
                  {note.text}
                  <VerbatimTag lang={note.lang} t={t} />{" "}
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
  layout = "compact",
}: {
  data: MemberPageData;
  t: (key: string) => string;
  layout?: "compact" | "board";
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

  const facts = [
    { label: t("sessions"), value: countLabel },
    { label: t("col_gap"), value: gapLabel },
    { label: t("fact_since"), value: fmtDate(data.member_since) },
  ];
  if (data.last_session_on) {
    facts.push({
      label: t("fact_last"),
      value: fmtDate(data.last_session_on),
    });
  }

  if (layout === "board") {
    return (
      <div className="space-y-5">
        <section className="overflow-hidden rounded-xl bg-ink text-white shadow-shadow-1">
          <div className="flex flex-col gap-5 p-6 sm:flex-row sm:items-start sm:justify-between sm:p-8">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded-full bg-lime px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide text-ink">
                  {t("member_eyebrow")}
                </span>
                {chips.map((chip, index) => (
                  <span
                    key={index}
                    className="rounded-full border border-white/20 px-2.5 py-1 text-[11px] font-medium text-white/80"
                  >
                    {chip}
                  </span>
                ))}
              </div>
              <h1 className="mt-5 text-[34px] font-semibold leading-none tracking-[-0.035em]">
                {data.name}
              </h1>
            </div>
            <Link
              to={`/members/${data.member_id}/routine`}
              className="inline-flex min-h-10 items-center justify-center gap-2 rounded-md bg-lime px-4 text-[13px] font-semibold text-ink transition-colors duration-fast hover:bg-lime-hover"
            >
              <Pencil size={14} aria-hidden="true" />
              {t("edit")} {t("routine").toLocaleLowerCase(getLang())}
            </Link>
          </div>
          <dl className="grid grid-cols-2 gap-x-5 gap-y-4 border-t border-white/10 bg-white/[0.04] p-6 sm:grid-cols-4 sm:p-8">
            {facts.map((fact) => (
              <div key={fact.label} className="min-w-0 border-t border-white/20 pt-3">
                <dt className="text-[11px] font-medium text-white/60">
                  {fact.label}
                </dt>
                <dd className="mt-1 text-[14px] font-semibold text-white tabular-nums">
                  {fact.value}
                </dd>
              </div>
            ))}
          </dl>
        </section>

        <SafetyBanner
          flags={data.safety_flags}
          memberId={data.member_id}
          t={t}
        />

        <RoutineBoard data={data} t={t} />

        <div className="grid gap-5 lg:grid-cols-[minmax(0,1.7fr)_minmax(280px,.7fr)]">
          <SessionsCard data={data} t={t} />
          <aside className="flex flex-col gap-5">
            <WeightsCard data={data} t={t} />
            <NotesCard data={data} t={t} />
          </aside>
        </div>
      </div>
    );
  }

  return (
    <>
      <section className="rounded-xl border border-elevation-0-stroke bg-white p-5 shadow-shadow-1 sm:p-6">
        <p className="mb-3 inline-flex rounded-full bg-lime px-2.5 py-1 text-[11px] font-medium uppercase tracking-wide text-ink">
          {t("member_eyebrow")}
        </p>
        <h1 className="text-[27px] font-semibold leading-tight tracking-[-0.03em]">
          {data.name}
        </h1>

        {chips.length > 0 && (
          <div className="chips mt-4 flex flex-wrap gap-2">
            {chips.map((chip, i) => (
              <span key={i} className="tag bg-elevation-0 text-ink-2">
                {chip}
              </span>
            ))}
          </div>
        )}

        <dl className="facts mt-7 grid grid-cols-2 gap-x-4 gap-y-4 sm:grid-cols-4">
          {facts.map((fact) => (
            <div key={fact.label} className="min-w-0 border-t border-elevation-0-stroke pt-3">
              <dt className="text-[11px] font-medium text-ink-3">
                {fact.label}
              </dt>
              <dd className="mt-1 text-[14px] font-medium text-ink tabular-nums">
                {fact.value}
              </dd>
            </div>
          ))}
        </dl>
      </section>

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
      <AppHeader
        showNav={false}
        leading={
          <Link
            to={backTo}
            className="text-[13px] text-ink-2 hover:text-ink transition-colors duration-fast"
          >
            {t("back_to_roster")}
          </Link>
        }
      />

      <main className="mx-auto max-w-6xl px-gut py-6 lg:px-10 lg:py-8">
        <MemberPane data={data} t={t} layout="board" />
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

  // A mistyped id ("/members/abc") parses to NaN; the query never runs,
  // so without this it would land on the generic error branch. The spec's
  // rule is the same bare 404 for anything unreachable — mistyped included
  // (P3, PR #206 review).
  const idValid = Number.isInteger(id) && id > 0;

  const { data, isLoading, error } = useQuery({
    queryKey: ["member", id, page],
    queryFn: () => fetchMember(id, page),
    enabled: idValid,
    staleTime: 30_000,
  });

  if (!idValid) {
    return (
      <div className="flex items-center justify-center min-h-[200px] text-ink-3 text-[14px]">
        404
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[200px] text-ink-3 text-[14px]" aria-busy="true">
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
