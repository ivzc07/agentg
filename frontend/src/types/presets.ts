/** JSON contract for ``/api/presets`` (issue #152). */

export interface PresetMember {
  id: number;
  name: string;
}

export interface Preset {
  id: number;
  name: string;
  is_default: boolean;
  has_master: boolean;
}

export interface PresetsResponse {
  presets: Preset[];
  members: PresetMember[];
  default_preset_id: number | null;
}

export interface CreatePresetResponse {
  id: number;
  name: string;
}

export interface ApplyPresetResponse {
  applied: number;
}

export interface DefaultPresetResponse {
  default_preset_id: number | null;
}

export interface RetirePresetResponse {
  retired: boolean;
}

export interface PresetsError {
  error: string;
}
