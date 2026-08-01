import { useState } from "react";
import type { RosterMember } from "../types/roster";
import { MemberPage } from "./MemberPage";
import { initials, gapText } from "./roster-utils";
import { useT } from "../hooks/useT";

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
      <div className="rail lg:w-80 lg:flex-shrink-0 lg:border-r lg:border-elevation-0-stroke lg:overflow-y-auto">
        {/* Count bar */}
        <div className="countbar flex items-center gap-2 px-gut py-2 text-[13px] text-ink-2 border-b border-elevation-0-stroke">
          <span className="chip-icon" aria-hidden="true">
            ≡
          </span>
          {t("sorted_by_gap")}
          {members.length > 0 && (
            <span className="numeral text-[18px] font-mono font-bold text-ink ml-auto">
              {members.length}
            </span>
          )}
        </div>

        <ul id="roster" role="list">
          {members.map((member) => (
            <li key={member.member_id} data-name={member.name}>
              <button
                type="button"
                onClick={() => setSelectedMemberId(member.member_id)}
                className={`w-full text-left flex items-center gap-3 px-gut py-2.5 hover:bg-elevation-1 transition-colors duration-fast border-b border-elevation-0-stroke ${
                  member.member_id === selectedMemberId
                    ? "bg-elevation-1 border-l-2 border-l-magenta"
                    : ""
                }`}
                aria-current={member.member_id === selectedMemberId ? "true" : undefined}
              >
                <span
                  className="flex-shrink-0 w-8 h-8 rounded-sm flex items-center justify-center text-[12px] font-semibold bg-elevation-2 text-ink-2"
                  aria-hidden="true"
                >
                  {initials(member.name)}
                </span>
                <span className="flex-1 min-w-0">
                  <span className="block text-[14px] font-medium truncate">
                    {member.name}
                  </span>
                  <span className="block text-[12px] text-ink-3 mt-0.5">
                    {gapText(member, t)}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      {/* Right pane: renders the member directly */}
      <div className="pane flex-1 min-h-0 lg:overflow-y-auto">
        {selectedMember != null ? (
          <MemberPage member={selectedMember} />
        ) : (
          <div className="pane-empty flex items-center justify-center min-h-[200px]">
            <div className="emptystate text-center">
              <h2 className="text-[18px] font-semibold text-ink-2">
                {t("pick_a_member")}
              </h2>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
