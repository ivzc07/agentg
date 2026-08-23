import { Link } from "react-router-dom";
import type { RosterMember } from "../types/roster";
import { initials, gapText } from "./roster-utils";
import { useT } from "../hooks/useT";
import { AttendanceStrip } from "./AttendanceStrip";

interface RosterTableProps {
  members: RosterMember[];
  id?: string;
}

function Avatar({ member }: { member: RosterMember }) {
  return (
    <span
      className={`flex size-10 flex-shrink-0 items-center justify-center rounded-full text-[11px] font-semibold tracking-[0.03em] ${
        member.severity === "red"
          ? "bg-coral-tint text-coral"
          : member.severity === "amber"
            ? "bg-amber-tint text-amber"
            : "bg-ink text-lime"
      }`}
      aria-hidden="true"
    >
      {initials(member.name)}
    </span>
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
    <span className={`text-[12px] ${colors[severity] ?? "text-ink-2"}`}>
      {label}
    </span>
  );
}

function Tags({ member }: { member: RosterMember }) {
  const t = useT();
  return (
    <>
      {member.is_new && (
        <span className="tag text-ink-2">{t("new_tag")}</span>
      )}
      {member.snoozed_until && (
        <span className="tag text-ink-2">{t("snoozed_tag").replace("{date}", member.snoozed_until)}</span>
      )}
      {member.has_safety_flag && (
        <span className="tag text-purple">{t("flag_tag")}</span>
      )}
    </>
  );
}

export function RosterTable({ members, id = "roster" }: RosterTableProps) {
  const t = useT();

  return (
    <ul
      className="overflow-hidden rounded-xl border border-elevation-0-stroke bg-elevation-3 shadow-shadow-1"
      id={id}
      role="list"
    >
      {members.map((member) => {
        const hasAttendance = member.attendance.length > 0;
        return (
          <li
            key={member.member_id}
            data-name={member.name}
            className="border-b border-elevation-0-stroke last:border-b-0"
          >
            <Link
              to={`/members/${member.member_id}`}
              className="relative flex items-start gap-3 px-4 py-3.5 transition-colors duration-fast hover:bg-primary/20 sm:items-center sm:gap-3.5"
            >
              <Avatar member={member} />

              <span className="min-w-0 flex-1 pr-20 sm:pr-0">
                <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="truncate text-[15px] font-bold tracking-[-0.015em]">{member.name}</span>
                  <span className="flex flex-wrap gap-1">
                    <Tags member={member} />
                  </span>
                </span>
                {(hasAttendance || member.severity) && (
                  <span className="mt-1 flex min-w-0 flex-col items-start gap-1 sm:flex-row sm:items-center sm:gap-2">
                    {hasAttendance && <AttendanceStrip cells={member.attendance} compact />}
                    <SeverityBadge severity={member.severity} missed_days={member.missed_days} />
                  </span>
                )}
              </span>

              <span className="absolute top-4 right-4 max-w-20 flex-shrink-0 text-right text-[11px] leading-4 text-ink-2 tabular-nums sm:static sm:max-w-none sm:text-[13px]">
                {gapText(member, t)}
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
