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
    // The router mounts at the site root — the URLs aiohttp serves it at (#154).
    window.history.pushState({}, "", "/");

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

  it("bypasses session states on login routes and renders LoginPage", async () => {
    // isLoginRoute is true → Dashboard returns <LoginRoutes /> immediately,
    // so the user always sees the login interstitial, never session
    // loading / auth-error / retryable-error states.
    // fetchSession still fires (React hooks always execute), but its
    // result is irrelevant because LoginRoutes is returned first.
    fetchSession.mockReturnValue(new Promise(() => {}));
    window.history.pushState({}, "", "/login/token-abc");

    // LoginPage inside LoginRoutes will fetch the peek API.
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ valid: true }),
    } as Response);

    renderDashboard();

    // The valid-token interstitial appears — never any session state.
    expect(
      await screen.findByText((content) =>
        content.includes("Abriendo tu dashboard"),
      ),
    ).toBeInTheDocument();

    // The session loading text must NOT appear even though
    // fetchSession is still pending.
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("redirects unknown deep links to the roster", async () => {
    fetchSession.mockResolvedValue({ name: "Ana", gym: "Iron Temple" });
    fetchRoster.mockResolvedValue({
      active: [
        {
          member_id: 1,
          name: "Zoe",
          gap_days: 3,
          has_sessions: true,
          is_new: false,
          snoozed_until: null,
          missed_days: 2,
          severity: "amber",
          has_safety_flag: false,
          attendance: [],
        },
      ],
      lapsed: [],
      counts: { active: 1, lapsed: 0 },
      sortedBy: "gap_days",
    });
    window.__I18N__ = {
      members_count: "Members ({n})",
      member_eyebrow: "member",
      settings: "Settings",
      search_placeholder: "Search by name",
      nav_views: "Views",
      nav_sections: "Sections",
      presets: "Presets",
      nav_roster: "Members",
      col_name: "Name",
      col_status: "Status",
      col_gap: "Days away",
      col_missed: "Missed",
      summary_hot: "{n} need you now",
      summary_warm: "{n} slipping",
      summary_flag: "{n} flagged",
      sorted_by_gap: "Sorted by days away",
      view_table: "Table",
      view_cards: "Cards",
      view_split: "Split",
    };
    // Unknown deep link — the catch-all must redirect to /.
    // /settings is a real route now (issue #153), so use
    // a truly unknown path.
    window.history.pushState({}, "", "/unknown-deep-link");

    renderDashboard();

    // After redirect, the roster (Iron Temple heading) is visible.
    expect(await screen.findByText("Iron Temple")).toBeInTheDocument();
  });
});
