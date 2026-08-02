/** JSON contract for ``/api/roster`` (issue #149). */

export interface DayCell {
  on: string; // ISO date
  state: "hit" | "miss" | "future" | "plain";
}

export interface RosterMember {
  member_id: number;
  name: string;
  gap_days: number;
  has_sessions: boolean;
  is_new: boolean;
  snoozed_until: string | null;
  missed_days: number;
  severity: "amber" | "red" | null;
  has_safety_flag: boolean;
  attendance: DayCell[];
}

export interface RosterResponse {
  active: RosterMember[];
  lapsed: RosterMember[];
  counts: { active: number; lapsed: number };
  sortedBy: "gap_days";
}

export type RosterView = "table" | "cards" | "split";
