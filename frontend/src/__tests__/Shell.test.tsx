import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { Shell } from "../components/Shell";

describe("Shell", () => {
  beforeEach(() => {
    // Bootstrap window.__I18N__ as the server does (ADR 0004 §i18n 7a).
    window.__I18N__ = {
      member_eyebrow: "member",
      settings: "Settings",
    };
  });

  it("renders the coach's name and gym", () => {
    render(<Shell name="Ana" gym="Iron Temple" />);

    // The gym name appears in the header.
    expect(screen.getByText("Iron Temple")).toBeInTheDocument();
    // The coach's name appears in the header and main content.
    const headings = screen.getAllByText("Ana");
    expect(headings.length).toBeGreaterThanOrEqual(2);
  });

  it("resolves i18n strings via useT()", () => {
    render(<Shell name="Ana" gym="Iron Temple" />);

    // The eyebrow resolves from window.__I18N__.
    expect(screen.getByText("member")).toBeInTheDocument();
    // The settings label resolves from window.__I18N__.
    expect(screen.getByText(/Settings/)).toBeInTheDocument();
  });
});
