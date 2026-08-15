import type { ReactNode } from "react";
import { AppHeader } from "./AppHeader";

interface PresetsShellProps {
  name: string;
  gym: string;
  children: ReactNode;
}

/**
 * Minimal top-bar shell for the Presets screen.
 * Matches the RosterShell chrome: gym name, presets/settings nav with active
 * state, and the coach's name.
 */
export function PresetsShell({ name: _name, gym, children }: PresetsShellProps) {
  return <AppHeader gym={gym}>{children}</AppHeader>;
}
