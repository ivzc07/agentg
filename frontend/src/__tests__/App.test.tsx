import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Mock fetchSession and SessionAuthError before Dashboard is imported.
const { fetchSession, SessionAuthError } = vi.hoisted(() => ({
  fetchSession: vi.fn(),
  SessionAuthError: class SessionAuthError extends Error {
    constructor() {
      super("/api/session: 401");
      this.name = "SessionAuthError";
    }
  },
}));

vi.mock("../api/session", () => ({ fetchSession, SessionAuthError }));

// Import Dashboard (the inner component) directly so we control QueryClient.
import { Dashboard } from "../App";

function renderDashboard() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={qc}>
      <Dashboard />
    </QueryClientProvider>,
  );
}

describe("Dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    delete (window as any).__I18N__;
  });

  it("shows the loading state while fetchSession is pending", () => {
    // Never resolve — stay in loading.
    fetchSession.mockReturnValue(new Promise(() => {}));

    renderDashboard();

    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows the auth error state on a 401", async () => {
    fetchSession.mockRejectedValue(new SessionAuthError());

    renderDashboard();

    expect(await screen.findByText("Not signed in.")).toBeInTheDocument();
  });

  it("shows a retryable error state on a non-401 failure", async () => {
    fetchSession.mockRejectedValue(new Error("/api/session: 503"));

    renderDashboard();

    expect(
      await screen.findByText("Something went wrong loading your session."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("renders the Shell on a successful fetch", async () => {
    fetchSession.mockResolvedValue({ name: "Ana", gym: "Iron Temple" });
    window.__I18N__ = { member_eyebrow: "member", settings: "Settings" };

    renderDashboard();

    expect(await screen.findByText("Iron Temple")).toBeInTheDocument();
    // "Ana" appears in both the header and main content.
    expect(screen.getAllByText("Ana").length).toBeGreaterThanOrEqual(2);
  });
});
