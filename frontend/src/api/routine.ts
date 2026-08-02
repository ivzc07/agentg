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
