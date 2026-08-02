import type { RosterMember } from "../types/roster";

/** Two-letter monogram from a member name. */
export function initials(name: string): string {
  const parts = name.split(" ");
  return parts
    .slice(0, 2)
    .map((p) => p[0] ?? "")
    .join("")
    .toUpperCase() || "?";
}

/** Gap text fragment for a roster row. */
export function gapText(member: RosterMember, t: (key: string) => string): string {
  if (!member.has_sessions) return t("no_sessions_yet");
  if (member.gap_days === 0) return t("trained_today");
  if (member.gap_days === 1) return t("one_day_away");
  return t("days_away").replace("{n}", String(member.gap_days));
}

/** Filter members by a name search query (accent-insensitive). */
export function filterMembers(
  members: RosterMember[],
  query: string
): RosterMember[] {
  const norm = (s: string) =>
    s
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase();
  const q = norm(query.trim());
  if (!q) return members;
  return members.filter((m) => norm(m.name).includes(q));
}
