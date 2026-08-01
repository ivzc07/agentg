import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { PresetsShell } from "../components/PresetsShell";

vi.mock("../hooks/useT", () => ({
  useT: () => (key: string): string => {
    const strings: Record<string, string> = {
      presets: "Presets",
      settings: "Settings",
      nav_sections: "Sections",
      back_to_roster: "← All members",
    };
    return strings[key] ?? key;
  },
}));

describe("PresetsShell", () => {
  it("renders the gym name", () => {
    render(
      <MemoryRouter>
        <PresetsShell name="Coach" gym="Iron Temple">
          <div data-testid="child" />
        </PresetsShell>
      </MemoryRouter>,
    );

    expect(screen.getByText("Iron Temple")).toBeInTheDocument();
    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("renders a link back to the roster", () => {
    render(
      <MemoryRouter>
        <PresetsShell name="Coach" gym="Iron Temple">
          <div />
        </PresetsShell>
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", { name: "← All members" });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute("href", "/dashboard");
  });

  it("renders the Presets nav link as active on /presets", () => {
    render(
      <MemoryRouter initialEntries={["/presets"]}>
        <PresetsShell name="Coach" gym="Iron Temple">
          <div />
        </PresetsShell>
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", { name: "Presets" });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute("href", "/presets");
    expect(link).toHaveAttribute("aria-current", "page");
  });
});
