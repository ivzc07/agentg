import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { LoginPage } from "../components/LoginPage";

function renderLoginPage(initialEntry: string) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/dashboard/login/:token" element={<LoginPage />} />
          <Route path="/dashboard/login" element={<LoginPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("LoginPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows loading state while the peek API is pending", () => {
    // Never resolve — stay in loading.
    vi.spyOn(globalThis, "fetch").mockReturnValue(
      new Promise(() => {}),
    );

    renderLoginPage("/dashboard/login/token-abc");

    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows the dead-link bounce when the peek API returns not-ok", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
    } as Response);

    renderLoginPage("/dashboard/login/expired-token");

    await waitFor(() => {
      expect(
        screen.getByText((content) =>
          content.includes("Este enlace ya no sirve"),
        ),
      ).toBeInTheDocument();
    });
  });

  it("shows the dead-link bounce when valid is false", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ valid: false }),
    } as Response);

    renderLoginPage("/dashboard/login/stale-token");

    await waitFor(() => {
      expect(
        screen.getByText((content) =>
          content.includes("Este enlace ya no sirve"),
        ),
      ).toBeInTheDocument();
    });
  });

  it("shows the valid-token interstitial with a sign-in form", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ valid: true }),
    } as Response);

    renderLoginPage("/dashboard/login/valid-token");

    await waitFor(() => {
      expect(
        screen.getByText((content) =>
          content.includes("Abriendo tu dashboard"),
        ),
      ).toBeInTheDocument();
    });

    // The form POSTs to the server-side redemption route (not the peek API).
    const form = screen.getByRole("button", { name: "Entrar al dashboard" })
      .closest("form")!;
    expect(form).toHaveAttribute("action", "/login/valid-token");
    expect(form).toHaveAttribute("method", "post");
  });

  it("shows the dead-link bounce for the token-less /login route", async () => {
    // The token-less route uses empty string as the token,
    // so peekToken fetches /api/login/ which is a distinct API path.
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
    } as Response);

    renderLoginPage("/dashboard/login");

    await waitFor(() => {
      expect(
        screen.getByText((content) =>
          content.includes("Este enlace ya no sirve"),
        ),
      ).toBeInTheDocument();
    });
  });

  it("shows the dead-link bounce on a network error from peek", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(
      new Error("Network error"),
    );

    renderLoginPage("/dashboard/login/net-fail-token");

    // react-query surfaces fetch rejections as !valid (error → data stays
    // undefined, so the !valid branch renders).
    await waitFor(() => {
      expect(
        screen.getByText((content) =>
          content.includes("Este enlace ya no sirve"),
        ),
      ).toBeInTheDocument();
    });
  });
});
