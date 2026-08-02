import type {
  PresetsResponse,
  CreatePresetResponse,
  ApplyPresetResponse,
  DefaultPresetResponse,
  RetirePresetResponse,
  PresetsError,
} from "../types/presets";

/** Error thrown on non-OK /api/presets responses. */
export class PresetsApiError extends Error {
  status: number;
  data: PresetsError;

  constructor(status: number, data: PresetsError) {
    super(`/api/presets: ${status} ${data.error}`);
    this.name = "PresetsApiError";
    this.status = status;
    this.data = data;
  }
}

async function handle(response: Response) {
  if (!response.ok) {
    let data: PresetsError;
    try {
      data = await response.json();
    } catch {
      data = { error: `HTTP ${response.status}` };
    }
    throw new PresetsApiError(response.status, data);
  }
  return response.json();
}

/** Fetch the presets list, eligible members, and default id. */
export async function fetchPresets(): Promise<PresetsResponse> {
  const response = await fetch("/api/presets");
  return handle(response);
}

/** Create a new preset. */
export async function createPreset(name: string): Promise<CreatePresetResponse> {
  const response = await fetch("/api/presets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  return handle(response);
}

/** Apply a preset to chosen members. */
export async function applyPreset(
  presetId: number,
  memberIds: number[],
  applyAll: boolean,
): Promise<ApplyPresetResponse> {
  const response = await fetch(`/api/presets/${presetId}/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ member_ids: memberIds, apply_all: applyAll }),
  });
  return handle(response);
}

/** Set or clear the default preset. */
export async function toggleDefaultPreset(
  presetId: number,
): Promise<DefaultPresetResponse> {
  const response = await fetch(`/api/presets/${presetId}/default`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  return handle(response);
}

/** Retire a preset. */
export async function retirePreset(
  presetId: number,
): Promise<RetirePresetResponse> {
  const response = await fetch(`/api/presets/${presetId}/retire`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  return handle(response);
}
