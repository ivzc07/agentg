import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AppHeader } from "../components/AppHeader";

vi.mock("../hooks/useT", () => ({
  useT: () => (key: string): string => {
    const strings: Record<string, string> = {
      presets: "Presets",
      settings: "Settings",
      nav_sections: "Sections",
      nav_dashboard: "Dashboard",
      nav_workspace: "Workspace",
      nav_language: "Language",
      mobile_navigation_item: "{label}, mobile navigation",
      nav_roster: "Members",
    };
    return strings[key] ?? key;
  },
}));

describe("AppHeader", () => {
  it("renders the gym as a heading on the roster", () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <AppHeader gym="Iron Temple" />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "Iron Temple" })).toBeInTheDocument();
  });

  it("renders roster / presets / settings in the sidebar", () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <AppHeader gym="Iron Temple" />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Members" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "Presets" })).toHaveAttribute("href", "/presets");
  });

  it("marks Settings as current on /settings", () => {
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <AppHeader gym="Iron Temple" />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});
