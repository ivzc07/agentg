/** ``/api/session`` JSON contract (issue #155). */

export interface SessionData {
  name: string;
  gym: string;
}

/** Fetch the current coach's session from the backend. */
export async function fetchSession(): Promise<SessionData> {
  const response = await fetch("/api/session");
  if (!response.ok) {
    throw new Error(`/api/session: ${response.status}`);
  }
  return response.json();
}
