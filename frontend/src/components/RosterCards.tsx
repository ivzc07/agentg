import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import type { RosterMember, DayCell } from "../types/roster";
import { gapText } from "./roster-utils";
import { useT } from "../hooks/useT";

interface RosterCardsProps {
  members: RosterMember[];
}

const WEEKDAY_INITIALS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

function AttendanceGrid({ cells }: { cells: DayCell[] }) {
  const t = useT();
  return (
    <div className="daygrid mt-2">
      {/* Weekday initials */}
      <div className="flex gap-0.5 mb-1">
        {WEEKDAY_INITIALS.map((wd) => (
          <span
            key={wd}
            className="wd flex-1 text-center text-[10px] font-mono uppercase text-ink-3"
            aria-hidden="true"
          >
            {wd}
          </span>
        ))}
      </div>
      {/* Grid squares — rows of 7 */}
      <div className="grid grid-cols-7 gap-0.5">
        {cells.map((cell, i) => {
          const stateClass: Record<string, string> = {
            hit: "bg-cyan border-cyan",
            miss: "border-coral bg-coral-tint",
            future: "border-dashed border-elevation-2-stroke",
            plain: "border-elevation-2-stroke bg-transparent",
          };
          return (
            <i
              key={i}
              className={`block aspect-square border text-[0px] ${stateClass[cell.state] ?? ""}`}
              title={cell.on}
              aria-hidden={cell.state !== "miss"}
            >
              {cell.state === "miss" && (
                <span className="sr">
                  {t("sr_missed").replace("{date}", cell.on)}
                </span>
              )}
            </i>
          );
        })}
      </div>
    </div>
  );
}

function SeverityBadge({ severity, missed_days }: { severity: string | null; missed_days: number }) {
  const t = useT();
  if (!severity) return null;
  const label =
    missed_days === 1
      ? t("missed_one")
      : t("missed_n").replace("{n}", String(missed_days));
  const colors: Record<string, string> = {
    red: "text-coral",
    amber: "text-amber",
  };
  return (
    <span className={`text-[11px] font-mono uppercase tracking-wider ${colors[severity] ?? ""}`}>
      {label}
    </span>
  );
}

function MemberCard({ member }: { member: RosterMember }) {
  const t = useT();

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
      className="mcard rounded-xl border border-elevation-0-stroke bg-elevation-3 p-4 shadow-shadow-1 transition-colors duration-fast hover:border-purple"
      data-name={member.name}
    >
      <div className="top-row flex justify-between items-start gap-2">
        <Link
          to={`/members/${member.member_id}`}
          className="name truncate text-base font-bold tracking-[-0.02em] transition-colors duration-fast hover:text-purple"
        >
          {member.name}
        </Link>
        <span className="away text-[13px] text-ink-2 whitespace-nowrap">
          {gapText(member, t)}
        </span>
      </div>

      {member.severity && (
        <div className="meta mt-1">
          <SeverityBadge severity={member.severity} missed_days={member.missed_days} />
        </div>
      )}

      <AttendanceGrid cells={member.attendance} />

      <div className="sparklab mt-1 text-[10px] font-mono uppercase text-ink-3">
        {t("grid_label").replace("{n}", "4")}
      </div>

      {/* Tags */}
      <div className="flex flex-wrap gap-1 mt-2">
        {member.is_new && (
          <span className="tag text-ink-2">{t("new_tag")}</span>
        )}
        {member.snoozed_until && (
          <span className="tag text-ink-2">
            {t("snoozed_tag").replace("{date}", member.snoozed_until)}
          </span>
        )}
        {member.has_safety_flag && (
          <span className="tag text-purple">{t("flag_tag")}</span>
        )}
      </div>
    </motion.div>
  );
}

export function RosterCards({ members }: RosterCardsProps) {
  const t = useT();

  // Group into severity bands (issue #98)
  const bands: { key: string; title: string; icon: string; members: RosterMember[] }[] = [
    { key: "hot", title: t("band_hot"), icon: "●", members: [] },
    { key: "warm", title: t("band_warm"), icon: "◐", members: [] },
    { key: "cool", title: t("band_cool"), icon: "○", members: [] },
    { key: "new", title: t("band_new"), icon: "✦", members: [] },
  ];

  for (const member of members) {
    if (member.severity === "red") {
      bands[0].members.push(member);
    } else if (member.severity === "amber") {
      bands[1].members.push(member);
    } else if (member.is_new) {
      bands[3].members.push(member);
    } else {
      bands[2].members.push(member);
    }
  }

  return (
    <div className="roster-body space-y-6">
      {bands.map((band) => {
        if (band.key === "new" && band.members.length === 0) return null;
        return (
          <section key={band.key} className={`band band-${band.key}`} id={`band-${band.key}`}>
            <h2 className="mb-3 flex items-center gap-2 font-heading text-lg font-black tracking-[-0.025em] text-ink">
              <span className="chip-icon text-[13px] text-purple" aria-hidden="true">
                {band.icon}
              </span>
              {band.title}
              <span className="count text-[12px] text-ink-3 font-normal tabular-nums">
                {band.members.length}
              </span>
            </h2>
            {band.members.length > 0 && (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {band.members.map((member) => (
                  <MemberCard key={member.member_id} member={member} />
                ))}
              </div>
            )}
          </section>
        );
      })}

      {/* Legend */}
      <div className="legend flex gap-3 text-[12px] text-ink-3 pb-2">
        <span className="flex items-center gap-1">
          <i className="l-hit inline-block w-3 h-3 bg-cyan border border-cyan" />
          {t("legend_hit")}
        </span>
        <span className="flex items-center gap-1">
          <i className="l-miss inline-block w-3 h-3 border border-coral bg-coral-tint" />
          {t("legend_miss")}
        </span>
      </div>
    </div>
  );
}
