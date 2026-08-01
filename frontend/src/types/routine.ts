/** JSON contract for ``GET /api/members/{id}/routine`` (issue #151). */

export interface RoutineExercise {
  exercise: string;
  sets: number | null;
  reps: string | null;
}

export interface RoutineDay {
  weekday: number; // 0=Monday .. 6=Sunday
  name: string;
  exercises: RoutineExercise[];
}

export interface RoutineResponse {
  member_id: number;
  name: string;
  routine: RoutineDay[];
  routine_id: number | null;
  coach_authored: boolean;
  routine_author: string | null;
  routine_preset_name: string | null;
  catalog: string[];
}

/** JSON contract for ``PUT /api/members/{id}/routine`` request body. */

export interface RoutineSaveRequest {
  base_routine_id: number | null;
  workouts: RoutineDay[];
}

/** JSON contract for ``PUT /api/members/{id}/routine`` success response. */

export interface RoutineSaveResponse {
  ok: true;
  routine_id: number;
  routine: RoutineDay[];
  coach_authored: boolean;
  routine_author: string | null;
  routine_preset_name: string | null;
  notified: boolean;
}

/** JSON contract for ``PUT /api/members/{id}/routine`` error response. */

export interface RoutineSaveError {
  error: string;
  fresh_routine?: RoutineDay[];
  fresh_routine_id?: number;
}
