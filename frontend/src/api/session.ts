/** ``/api/session`` JSON contract (issue #155). */

export interface SessionData {
  name: string;
  gym: string;
}

/**
 * Error thrown when the session endpoint returns 401 — the coach is not
 * signed in (or the session expired). Distinguished from transient failures
 * so the UI can render the right recovery path.
 */
export class SessionAuthError extends Error {
  constructor() {
    super("/api/session: 401");
    this.name = "SessionAuthError";
  }
}

/** Fetch the current coach's session from the backend. */
export async function fetchSession(): Promise<SessionData> {
  const response = await fetch("/api/session");
  if (response.status === 401) {
    throw new SessionAuthError();
  }
  if (!response.ok) {
    throw new Error(`/api/session: ${response.status}`);
  }
  return response.json();
}
