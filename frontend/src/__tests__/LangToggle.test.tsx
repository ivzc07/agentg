import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { LangToggle } from "../components/LangToggle";

declare global {
  interface Window {
    __I18N__?: Record<string, string | string[]>;
  }
}

describe("LangToggle", () => {
  beforeEach(() => {
    window.history.pushState({}, "", "/?view=cards");
  });

  afterEach(() => {
    delete window.__I18N__;
  });

  it("renders EN and ES links to the server /lang route with a next back-link", () => {
    window.__I18N__ = { _lang: "es" };
    render(<LangToggle />);

    const en = screen.getByText("EN");
    const es = screen.getByText("ES");
    expect(en).toHaveAttribute(
      "href",
      `/lang/en?next=${encodeURIComponent("/?view=cards")}`
    );
    expect(es).toHaveAttribute(
      "href",
      `/lang/es?next=${encodeURIComponent("/?view=cards")}`
    );
  });

  it("marks the active language from the injected bootstrap", () => {
    window.__I18N__ = { _lang: "en" };
    render(<LangToggle />);

    expect(screen.getByText("EN")).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("ES")).not.toHaveAttribute("aria-current");
  });

  it("defaults to Spanish when no bootstrap exists (the no-signal default)", () => {
    render(<LangToggle />);

    expect(screen.getByText("ES")).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("EN")).not.toHaveAttribute("aria-current");
  });
});
