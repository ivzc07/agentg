/** JSON contract for ``/api/settings`` (issue #153). */

export interface SettingsData {
  gym_name: string;
  invite_code: string;
  invite_url: string;
  qr_svg: string;
  coach_invite_code: string;
  coach_invite_url: string;
  bot_username: string;
}

export interface RegenerateInviteResponse {
  invite_code: string;
  invite_url: string;
  qr_svg: string;
}

export interface RegenerateCoachResponse {
  coach_invite_code: string;
  coach_invite_url: string;
}

export interface RenameGymResponse {
  gym_name: string;
}
