/** JSON contract for ``/api/members/{id}`` (issue #150). */

export interface RoutineExercise {
  name: string;
  sets: number | null;
  reps: string | null;
}

export interface RoutineDay {
  weekday: number; // 0=Mon … 6=Sun
  name: string;
  exercises: RoutineExercise[];
}

export interface SessionSet {
  exercise: string;
  weight: number | null;
  reps: number;
  note: string | null;
  /** Detected source language of the note ("en" | "es"), null without one. */
  note_lang: string | null;
}

export interface SessionView {
  on: string; // ISO date
  sets: SessionSet[];
}

export interface LastWeight {
  exercise: string;
  weight: number | null;
  reps: number[];
  on: string; // ISO date
}

export interface NoteView {
  kind: string;
  text: string;
  /** Detected source language of the Member's own words ("en" | "es"). */
  lang: string;
  on: string;
  retired_on: string | null;
}

export interface SafetyFlag {
  note_id: number;
  text: string;
  on: string;
  status: "open" | "acknowledged" | "expired";
  acknowledged_on: string | null;
  acknowledged_by: string | null;
}

export interface MemberPageData {
  member_id: number;
  name: string;
  member_since: string;
  weight_unit: string;
  session_count: number;
  gap_days: number;
  has_sessions: boolean;
  last_session_on: string | null;
  lapsed: boolean;
  snoozed_until: string | null;
  routine: RoutineDay[];
  routine_id: number | null;
  routine_preset_name: string | null;
  coach_authored: boolean;
  routine_author: string | null;
  sessions: SessionView[];
  page: number;
  pages: number;
  weights: LastWeight[];
  notes: NoteView[];
  retired_notes: NoteView[];
  safety_flags: SafetyFlag[];
}
