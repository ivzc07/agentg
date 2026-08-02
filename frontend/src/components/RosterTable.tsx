import { Link } from "react-router-dom";
import type { RosterMember } from "../types/roster";
import { initials, gapText } from "./roster-utils";
import { useT } from "../hooks/useT";

interface RosterTableProps {
  members: RosterMember[];
}

function SeverityBadge({ severity, missed_days }: { severity: string | null; missed_days: number }) {
  const t = useT();
  if (!severity) return null;
  const label =
    missed_days === 1
      ? t("missed_one")
      : t("missed_n").replace("{n}", String(missed_days));
  const colors: Record<string, string> = {
    red: "bg-coral-tint text-coral border-coral/30",
    amber: "bg-amber-tint text-amber border-amber/30",
  };
  return (
    <span
      className={`inline-block text-[11px] font-mono uppercase tracking-wider px-1.5 py-0.5 border ${colors[severity] ?? ""}`}
    >
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

export function RosterTable({ members }: RosterTableProps) {
  const t = useT();

  return (
    <ul className="space-y-0" id="roster" role="list">
      {members.map((member) => (
        <li key={member.member_id} data-name={member.name}>
          <Link
            to={`/members/${member.member_id}`}
            className="flex items-center gap-3 px-gut py-2.5 hover:bg-elevation-1 transition-colors duration-fast border-b border-elevation-0-stroke"
          >
            {/* Monogram tile */}
            <span
              className="flex-shrink-0 w-9 h-9 rounded-sm flex items-center justify-center text-[13px] font-semibold bg-elevation-2 text-ink-2"
              aria-hidden="true"
            >
              {initials(member.name)}
            </span>

            {/* Name + tags */}
            <span className="flex-1 min-w-0">
              <span className="block text-[15px] font-medium truncate">
                {member.name}
              </span>
              <span className="flex flex-wrap gap-1 mt-0.5">
                <Tags member={member} />
              </span>
            </span>

            {/* Gap + severity */}
            <span className="flex flex-col items-end gap-0.5 flex-shrink-0 text-right">
              <span className="text-[13px] text-ink-2">
                {gapText(member, t)}
              </span>
              <SeverityBadge severity={member.severity} missed_days={member.missed_days} />
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
