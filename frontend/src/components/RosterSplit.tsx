import { useState } from "react";
import type { RosterMember } from "../types/roster";
import { MemberPage } from "./MemberPage";
import { initials, gapText } from "./roster-utils";
import { useT } from "../hooks/useT";
import { AttendanceStrip } from "./AttendanceStrip";

interface RosterSplitProps {
  members: RosterMember[];
}

export function RosterSplit({ members }: RosterSplitProps) {
  const t = useT();
  const [selectedMemberId, setSelectedMemberId] = useState<number | null>(null);
  const selectedMember = selectedMemberId != null
    ? members.find((m) => m.member_id === selectedMemberId)
    : undefined;

  return (
    <div className="split flex flex-col lg:flex-row gap-0 min-h-0">
      {/* Left rail: scrollable roster */}
      <div className={`rail lg:w-80 lg:flex-shrink-0 lg:border-r lg:border-elevation-0-stroke lg:overflow-y-auto lg:max-h-[calc(100vh-7.5rem)] ${selectedMember != null ? "hidden lg:block" : ""}`}>
        <ul id="roster" role="list">
          {members.map((member) => (
            <li key={member.member_id} data-name={member.name}>
              <button
                type="button"
                onClick={() => setSelectedMemberId(member.member_id)}
                className={`w-full text-left flex items-center gap-3 px-gut py-2.5 hover:bg-elevation-1 transition-colors duration-fast border-b border-elevation-0-stroke border-l-0 ${
                  member.member_id === selectedMemberId
                    ? "bg-elevation-1 text-ink"
                    : "bg-transparent"
                }`}
                aria-current={member.member_id === selectedMemberId ? "true" : undefined}
              >
                <span
                  className={`flex-shrink-0 w-8 h-8 rounded-sm flex items-center justify-center text-[11px] font-semibold tracking-[0.04em] ${
                    member.member_id === selectedMemberId
                      ? "bg-ink text-bg"
                      : "bg-elevation-2 text-ink"
                  }`}
                  aria-hidden="true"
                >
                  {initials(member.name)}
                </span>
                <span className="flex-1 min-w-0">
                  <span className="flex items-center gap-2 min-w-0">
                    <span className="block text-[14px] font-medium truncate">
                      {member.name}
                    </span>
                    <span className="ml-auto text-[12px] text-ink-3 tabular-nums whitespace-nowrap">
                      {gapText(member, t)}
                    </span>
                  </span>
                  <span className="flex items-center gap-2 mt-1 min-w-0">
                    {member.attendance.length > 0 && (
                      <AttendanceStrip cells={member.attendance.slice(-14)} compact />
                    )}
                    {member.has_safety_flag && (
                      <span className="tag text-purple">{t("flag_tag")}</span>
                    )}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      {/* Right pane: renders the member directly */}
      <div className="pane flex-1 min-h-0 lg:overflow-y-auto lg:max-h-[calc(100vh-7.5rem)]">
        {selectedMember != null ? (
          <div className="px-gut py-6">
            <button
              type="button"
              onClick={() => setSelectedMemberId(null)}
              className="lg:hidden min-h-0 border-0 bg-transparent p-0 mb-5 text-[13px] text-ink-2 hover:text-ink"
            >
              {t("back_to_roster")}
            </button>
            <MemberPage member={selectedMember} />
          </div>
        ) : (
          <div className="pane-empty flex items-center justify-center min-h-[280px]">
            <div className="emptystate text-center px-gut max-w-xs">
              <h2 className="text-[18px] font-semibold text-ink">
                {t("pick_a_member")}
              </h2>
              <p className="text-[13px] text-ink-2 mt-2 leading-relaxed">
                {t("pick_a_member_body")}
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
