import type { MemberPageData } from "../types/member";

/** Fetch a single member page from the backend. */
export async function fetchMember(
  memberId: number,
  page: number = 1
): Promise<MemberPageData> {
  const params = new URLSearchParams();
  if (page > 1) params.set("page", String(page));
  const qs = params.toString();
  const url = `/api/members/${memberId}${qs ? `?${qs}` : ""}`;
  const response = await fetch(url);
  if (response.status === 404) {
    throw new MemberNotFoundError(memberId);
  }
  if (!response.ok) {
    throw new Error(`/api/members/${memberId}: ${response.status}`);
  }
  return response.json();
}

/** Error thrown when the member endpoint returns 404. */
export class MemberNotFoundError extends Error {
  constructor(memberId: number) {
    super(`/api/members/${memberId}: not found`);
    this.name = "MemberNotFoundError";
  }
}

/** POST to acknowledge a safety flag. Returns the response body. */
export async function tickOffFlag(
  memberId: number,
  noteId: number
): Promise<{ note_id: number; acknowledged: boolean }> {
  const response = await fetch(
    `/api/members/${memberId}/flags/${noteId}/tick-off`,
    { method: "POST" }
  );
  if (!response.ok) {
    throw new Error(`/api/members/${memberId}/flags/${noteId}/tick-off: ${response.status}`);
  }
  return response.json();
}
