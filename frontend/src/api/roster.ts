import type { RosterResponse } from "../types/roster";

/** Fetch the roster from the backend. */
export async function fetchRoster(): Promise<RosterResponse> {
  const response = await fetch("/api/roster");
  if (!response.ok) {
    throw new Error(`/api/roster: ${response.status}`);
  }
  return response.json();
}
