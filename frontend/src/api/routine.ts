import type {
  RoutineResponse,
  RoutineSaveRequest,
  RoutineSaveResponse,
  RoutineSaveError,
} from "../types/routine";

/** Fetch a member's routine and the exercise catalog. */
export async function fetchRoutine(
  memberId: number
): Promise<RoutineResponse> {
  const response = await fetch(`/api/members/${memberId}/routine`);
  if (!response.ok) {
    throw new Error(`/api/members/${memberId}/routine: ${response.status}`);
  }
  return response.json();
}

/** Save a member's routine. On success returns the fresh routine;
 *  on 409 (stale) returns the fresh version for the refusal UI;
 *  on 400 returns the error message. */
export async function saveRoutine(
  memberId: number,
  body: RoutineSaveRequest
): Promise<RoutineSaveResponse | RoutineSaveError> {
  const response = await fetch(`/api/members/${memberId}/routine`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    return data as RoutineSaveError;
  }
  return data as RoutineSaveResponse;
}

/**
 * The Preset master editor endpoints (#154) — same editor, different
 * subject. The responses are normalized to the member-editor shapes so
 * ``RoutineEditor`` renders either from one contract.
 */
export async function fetchPresetRoutine(
  presetId: number
): Promise<RoutineResponse> {
  const response = await fetch(`/api/presets/${presetId}/routine`);
  if (!response.ok) {
    throw new Error(`/api/presets/${presetId}/routine: ${response.status}`);
  }
  const data = await response.json();
  return {
    member_id: data.preset_id,
    name: data.name,
    routine: data.routine,
    routine_id: data.routine_id,
    coach_authored: true,
    routine_author: data.routine_author,
    routine_preset_name: null,
    catalog: data.catalog,
  };
}

/** Save a Preset's master routine; 409/400 come back as RoutineSaveError. */
export async function savePresetRoutine(
  presetId: number,
  body: RoutineSaveRequest
): Promise<RoutineSaveResponse | RoutineSaveError> {
  const response = await fetch(`/api/presets/${presetId}/routine`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    return data as RoutineSaveError;
  }
  return {
    ok: true,
    routine_id: data.routine_id,
    routine: data.routine,
    coach_authored: true,
    routine_author: data.routine_author,
    routine_preset_name: null,
    notified: data.notified > 0,
  };
}
