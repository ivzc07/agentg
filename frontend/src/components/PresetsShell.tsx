import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useT } from "../hooks/useT";
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
  const t = useT();

  return (
    <AppHeader gym={gym}>
      <div className="sr-only">
        <Link to="/">{t("back_to_roster")}</Link>
      </div>
      {children}
    </AppHeader>
  );
}
