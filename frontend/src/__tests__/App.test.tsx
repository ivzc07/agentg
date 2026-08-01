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

// The roster screen behind the router does its own fetch; this suite is about
// Dashboard's session states, so keep the roster empty and predictable.
const { fetchRoster } = vi.hoisted(() => ({ fetchRoster: vi.fn() }));
vi.mock("../api/roster", () => ({ fetchRoster }));

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

    expect(
      await screen.findByText((content) =>
        content.includes("No estás autenticado"),
      ),
    ).toBeInTheDocument();
  });

  it("shows a retryable error state on a non-401 failure", async () => {
    fetchSession.mockRejectedValue(new Error("/api/session: 503"));

    renderDashboard();

    expect(
      await screen.findByText("Something went wrong loading your session."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("renders the roster screen on a successful fetch", async () => {
    fetchSession.mockResolvedValue({ name: "Ana", gym: "Iron Temple" });
    fetchRoster.mockResolvedValue({
      active: [],
      lapsed: [],
      counts: { active: 0, lapsed: 0 },
      sortedBy: "gap_days",
    });
    window.__I18N__ = { member_eyebrow: "member", settings: "Settings" };
    // The router mounts with basename="/dashboard", the URL aiohttp serves it at.
    window.history.pushState({}, "", "/dashboard/");

    renderDashboard();

    // The signed-in gym reaches the roster chrome, and none of the failure
    // branches above are showing.
    expect(await screen.findByText("Iron Temple")).toBeInTheDocument();
    expect(screen.queryByText("Not signed in.")).not.toBeInTheDocument();
    expect(
      screen.queryByText((content) =>
        content.includes("No estás autenticado"),
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Something went wrong loading your session."),
    ).not.toBeInTheDocument();
  });
});
