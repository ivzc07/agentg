/** ``/api/settings`` JSON contract (issue #153). */

import type {
  SettingsData,
  RegenerateInviteResponse,
  RegenerateCoachResponse,
  RenameGymResponse,
} from "../types/settings";

/** Fetch the gym settings from the backend. */
export async function fetchSettings(): Promise<SettingsData> {
  const response = await fetch("/api/settings");
  if (!response.ok) {
    throw new Error(`/api/settings: ${response.status}`);
  }
  return response.json();
}

/** Regenerate the member invite code. Requires the typed confirm word. */
export async function regenerateInvite(
  confirm: string,
): Promise<RegenerateInviteResponse> {
  const response = await fetch("/api/settings/regenerate-invite", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      (body as { error?: string }).error ||
        `/api/settings/regenerate-invite: ${response.status}`,
    );
  }
  return response.json();
}

/** Regenerate the coach invite code. Requires the typed confirm word. */
export async function regenerateCoach(
  confirm: string,
): Promise<RegenerateCoachResponse> {
  const response = await fetch("/api/settings/regenerate-coach", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      (body as { error?: string }).error ||
        `/api/settings/regenerate-coach: ${response.status}`,
    );
  }
  return response.json();
}

/** Rename the gym. Returns the new name. */
export async function renameGym(name: string): Promise<RenameGymResponse> {
  const response = await fetch("/api/settings/gym-name", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      (body as { error?: string }).error ||
        `/api/settings/gym-name: ${response.status}`,
    );
  }
  return response.json();
}
