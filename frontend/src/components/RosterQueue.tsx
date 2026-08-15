import type { RosterMember } from "../types/roster";
import { useT } from "../hooks/useT";
import { RosterTable } from "./RosterTable";

interface RosterQueueProps {
  members: RosterMember[];
}

/**
 * The table view organised as a coach action queue. Membership in a group is
 * derived only from the roster contract: red/safety first, amber second, and
 * everyone else in the steady tail.
 */
export function RosterQueue({ members }: RosterQueueProps) {
  const t = useT();
  const groups = [
    {
      key: "urgent",
      title: t("queue_urgent_title"),
      description: t("queue_urgent_description"),
      tone: "text-coral",
      members: members.filter(
        (member) => member.severity === "red" || member.has_safety_flag,
      ),
    },
    {
      key: "watch",
      title: t("queue_watch_title"),
      description: t("queue_watch_description"),
      tone: "text-amber",
      members: members.filter(
        (member) => member.severity === "amber" && !member.has_safety_flag,
      ),
    },
    {
      key: "steady",
      title: t("queue_steady_title"),
      description: t("queue_steady_description"),
      tone: "text-ink-3",
      members: members.filter(
        (member) => member.severity == null && !member.has_safety_flag,
      ),
    },
  ].filter((group) => group.members.length > 0);

  return (
    <div className="space-y-8">
      {groups.map((group) => (
        <section key={group.key} aria-labelledby={`queue-${group.key}`}>
          <div className="mb-3 flex items-end justify-between gap-4">
            <div>
              <h2
                id={`queue-${group.key}`}
                className={`text-[16px] font-semibold ${group.tone}`}
              >
                {group.title}
              </h2>
              <p className="mt-0.5 text-[11px] text-ink-3">
                {group.description}
              </p>
            </div>
            <span className="font-mono text-[12px] text-ink-3">
              {group.members.length}
            </span>
          </div>
          <RosterTable
            members={group.members}
            id={`roster-${group.key}`}
            layout="queue"
          />
        </section>
      ))}
    </div>
  );
}
