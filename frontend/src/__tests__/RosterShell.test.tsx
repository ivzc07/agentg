import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { RosterShell } from "../components/RosterShell";
import * as rosterApi from "../api/roster";
import * as memberApi from "../api/member";
import type { RosterResponse } from "../types/roster";

vi.mock("../hooks/useT", () => ({
  useT: () => (key: string): string => {
    const strings: Record<string, string> = {
      // Roster chrome
      search_placeholder: "Search by name",
      view_table: "Table",
      view_cards: "Cards",
      view_split: "Split",
      members_count: "Members ({n})",
      match_count: "{shown} of {total}",
      sorted_by_gap: "Sorted by days away",
      nav_views: "Views",
      nav_sections: "Sections",
      presets: "Presets",
      settings: "Settings",
      nav_roster: "Members",
      col_name: "Name",
      col_status: "Status",
      col_gap: "Days away",
      col_missed: "Missed",
      summary_hot: "{n} need you now",
      summary_warm: "{n} slipping",
      summary_flag: "{n} flagged",
      queue_label: "Coach queue",
      queue_counts: "{active} active · {lapsed} lapsed",
      queue_urgent_title: "Needs attention",
      queue_urgent_description: "Contact or review today",
      queue_watch_title: "Watch list",
      queue_watch_description: "Starting to slip",
      queue_steady_title: "On track",
      queue_steady_description: "No immediate action",
      queue_on_track: "On track",
      pick_a_member_body: "Sessions, last weights, and notes open here.",
      // Roster rows
      no_sessions_yet: "No sessions yet",
      trained_today: "trained today",
      one_day_away: "1 day away",
      days_away: "{n} days away",
      missed_one: "1 planned day missed",
      missed_n: "{n} planned days missed",
      new_tag: "new",
      snoozed_tag: "paused until {date}",
      flag_tag: "⚑ safety",
      lapsed_tail: "Lapsed ({n})",
      // Empty state
      empty_roster_title: "No members yet",
      empty_roster_body: "Share the invite link from Settings to add the first one.",
      no_matches: "No member matches the search.",
      // Cards
      grid_label: "last {n} weeks",
      legend_hit: "session",
      legend_miss: "planned, no session",
      band_hot: "Needs you now",
      band_warm: "Slipping",
      band_cool: "On track",
      band_new: "New",
      sr_missed: "missed {date}",
      // Split
      pick_a_member: "Pick a member",
      // Error state
      roster_error: "Couldn't load the roster.",
      roster_retry: "Retry",
      // Member page
      member_eyebrow: "Member",
      back_to_roster: "Back to roster",
    };
    return strings[key] ?? key;
  },
}));

const makeMember = (id: number, overrides: Partial<RosterResponse["active"][number]> = {}) => ({
  member_id: id,
  name: `Member ${id}`,
  gap_days: 3,
  has_sessions: true,
  is_new: false,
  snoozed_until: null,
  missed_days: 2,
  severity: "amber" as const,
  has_safety_flag: false,
  attendance: [],
  ...overrides,
});

const makeResponse = (overrides: Partial<RosterResponse> = {}): RosterResponse => ({
  active: [makeMember(1, { name: "Alice" }), makeMember(2, { name: "Bob" })],
  lapsed: [makeMember(3, { name: "Charlie", gap_days: 18 })],
  counts: { active: 2, lapsed: 1 },
  sortedBy: "gap_days",
  ...overrides,
});

function renderShell(
  response: RosterResponse | null = null,
  initialEntries: string[] = ["/"],
  reject = false,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  if (reject) {
    vi.spyOn(rosterApi, "fetchRoster").mockRejectedValue(new Error("/api/roster: 500"));
  } else if (response) {
    vi.spyOn(rosterApi, "fetchRoster").mockResolvedValue(response);
  }

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>
        <Routes>
          <Route path="/" element={<RosterShell name="Coach" gym="Iron Temple" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("RosterShell", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the gym name in the header", async () => {
    renderShell(makeResponse());
    await waitFor(() => {
      expect(screen.getByText("Iron Temple")).toBeInTheDocument();
    });
  });

  it("shows active and lapsed counts in the queue header", async () => {
    renderShell(makeResponse());
    await waitFor(() => {
      expect(screen.getByText("2 active · 1 lapsed")).toBeInTheDocument();
    });
  });

  it("shows member names in table view (default)", async () => {
    renderShell(makeResponse());
    await waitFor(() => {
      expect(screen.getByText("Alice")).toBeInTheDocument();
      expect(screen.getByText("Bob")).toBeInTheDocument();
    });
  });

  it("shows the lapsed tail", async () => {
    renderShell(makeResponse());
    await waitFor(() => {
      expect(screen.getByText("Lapsed (1)")).toBeInTheDocument();
    });
  });

  it("shows empty roster state when there are no members", async () => {
    renderShell(makeResponse({ active: [], lapsed: [], counts: { active: 0, lapsed: 0 } }));
    await waitFor(() => {
      expect(screen.getByText("No members yet")).toBeInTheDocument();
    });
  });

  it("filters members by search query", async () => {
    const user = userEvent.setup();
    renderShell(makeResponse());

    await waitFor(() => {
      expect(screen.getByText("Alice")).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText("Search by name");
    await user.type(input, "Alice");

    await waitFor(() => {
      expect(screen.getByText("Alice")).toBeInTheDocument();
      expect(screen.queryByText("Bob")).toBeNull();
    });
  });

  it("shows no-match message when search yields nothing", async () => {
    const user = userEvent.setup();
    renderShell(makeResponse());

    await waitFor(() => {
      expect(screen.getByPlaceholderText("Search by name")).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText("Search by name");
    await user.type(input, "Zorro");

    await waitFor(() => {
      expect(screen.getByText("No member matches the search.")).toBeInTheDocument();
    });
  });

  it("labels the table as a coach queue", async () => {
    renderShell(makeResponse());
    await waitFor(() => {
      expect(screen.getByText("Coach queue")).toBeInTheDocument();
    });
  });

  it("groups who needs attention from real roster data", async () => {
    renderShell(
      makeResponse({
        active: [
          makeMember(1, { name: "Alice", severity: "red", missed_days: 3, has_safety_flag: true }),
          makeMember(2, { name: "Bob", severity: "amber", missed_days: 1 }),
        ],
        counts: { active: 2, lapsed: 1 },
      }),
    );
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Needs attention" })).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Watch list" })).toBeInTheDocument();
      expect(screen.getByText("Alice")).toBeInTheDocument();
      expect(screen.getByText("Bob")).toBeInTheDocument();
    });
  });

  it("keeps the split rail attendance compact", async () => {
    const user = userEvent.setup();
    renderShell(makeResponse({
      active: [makeMember(1, {
        name: "Alice",
        attendance: Array.from({ length: 28 }, (_, i) => ({
          on: `2026-08-${String(i + 1).padStart(2, "0")}`,
          state: "plain" as const,
        })),
      })],
      counts: { active: 1, lapsed: 0 },
    }));
    await user.click(await screen.findByLabelText("Split"));
    expect(document.querySelectorAll(".rail .strip i")).toHaveLength(14);
  });

  it("shows the view switcher with all three views", async () => {
    renderShell(makeResponse());
    await waitFor(() => {
      expect(screen.getByLabelText("Table")).toBeInTheDocument();
      expect(screen.getByLabelText("Cards")).toBeInTheDocument();
      expect(screen.getByLabelText("Split")).toBeInTheDocument();
    });
  });

  it("opens the view named in the URL, like the server ?view= links did (#154)", async () => {
    renderShell(
      makeResponse({
        active: [makeMember(1, { name: "Alice", attendance: [] })],
        counts: { active: 1, lapsed: 0 },
      }),
      ["/?view=cards"]
    );

    await waitFor(() => {
      // Cards view shows the legend — without any click.
      expect(screen.getByText("session")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("Cards")).toHaveAttribute("aria-current", "page");
  });

  it("falls back to the table on an unknown ?view=, like the server did", async () => {
    renderShell(
      makeResponse({
        active: [makeMember(1, { name: "Alice", attendance: [] })],
        counts: { active: 1, lapsed: 0 },
      }),
      ["/?view=mosaic"]
    );

    await waitFor(() => {
      expect(screen.getByLabelText("Table")).toHaveAttribute("aria-current", "page");
    });
  });

  it("writes the picked view into the URL and clears it for the table (P3, PR #206 review round 2)", async () => {
    // A probe alongside the route observes the router's location, which
    // MemoryRouter keeps off window.location.
    function LocationProbe() {
      const location = useLocation();
      return <div data-testid="loc">{location.search}</div>;
    }
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.spyOn(rosterApi, "fetchRoster").mockResolvedValue(
      makeResponse({
        active: [makeMember(1, { name: "Alice", attendance: [] })],
        counts: { active: 1, lapsed: 0 },
      })
    );
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/"]}>
          <LocationProbe />
          <Routes>
            <Route path="/" element={<RosterShell name="Coach" gym="Iron Temple" />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
    const user = userEvent.setup();

    await waitFor(() => {
      expect(screen.getByLabelText("Cards")).toBeInTheDocument();
    });

    await user.click(screen.getByLabelText("Cards"));
    await waitFor(() => {
      expect(screen.getByTestId("loc")).toHaveTextContent("?view=cards");
    });

    // Back to the table: the param clears rather than lingering as
    // ?view=table — the server's canonical URLs never carried it either.
    await user.click(screen.getByLabelText("Table"));
    await waitFor(() => {
      expect(screen.getByTestId("loc")).toHaveTextContent(/^$/);
    });
  });

  it("switches to cards view", async () => {
    const user = userEvent.setup();
    renderShell(makeResponse({
      active: [
        makeMember(1, { name: "Alice", attendance: [] }),
      ],
      counts: { active: 1, lapsed: 0 },
    }));

    await waitFor(() => {
      expect(screen.getByLabelText("Cards")).toBeInTheDocument();
    });

    await user.click(screen.getByLabelText("Cards"));

    await waitFor(() => {
      // Cards view shows the legend
      expect(screen.getByText("session")).toBeInTheDocument();
      expect(screen.getByText("planned, no session")).toBeInTheDocument();
    });
  });

  it("switches to split view", async () => {
    const user = userEvent.setup();
    renderShell(makeResponse({
      active: [makeMember(1, { name: "Alice", attendance: [] })],
      counts: { active: 1, lapsed: 0 },
    }));

    await waitFor(() => {
      expect(screen.getByLabelText("Split")).toBeInTheDocument();
    });

    await user.click(screen.getByLabelText("Split"));

    await waitFor(() => {
      expect(screen.getByText("Pick a member")).toBeInTheDocument();
    });
  });

  it("renders member in split pane when a rail member is clicked", async () => {
    const user = userEvent.setup();
    renderShell(
      makeResponse({
        active: [makeMember(1, { name: "Alice", attendance: [] })],
        counts: { active: 1, lapsed: 0 },
      }),
      ["/"],
    );

    await waitFor(() => {
      expect(screen.getByLabelText("Split")).toBeInTheDocument();
    });

    // Switch to split view.
    await user.click(screen.getByLabelText("Split"));

    await waitFor(() => {
      expect(screen.getByText("Pick a member")).toBeInTheDocument();
    });

    // The pane loads the member's detail from /api/members/{id}.
    vi.spyOn(memberApi, "fetchMember").mockResolvedValue({
      member_id: 1,
      name: "Alice",
      member_since: "2026-06-01",
      weight_unit: "kg",
      session_count: 0,
      gap_days: 0,
      has_sessions: false,
      last_session_on: null,
      lapsed: false,
      snoozed_until: null,
      routine: [],
      routine_id: null,
      routine_preset_name: null,
      coach_authored: false,
      routine_author: null,
      sessions: [],
      page: 1,
      pages: 1,
      weights: [],
      notes: [],
      retired_notes: [],
      safety_flags: [],
    });

    // Click Alice in the rail.
    await user.click(screen.getByText("Alice"));

    await waitFor(() => {
      // Alice's name is in the pane heading (the h1 in MemberPage).
      const headings = screen.getAllByRole("heading", { level: 1 });
      expect(headings.some((h) => h.textContent === "Alice")).toBe(true);
    });
    // The pane is the bare member body: the rail stays mounted, so the
    // standalone page chrome (its own back link) must NOT be rendered —
    // that duplicated the roster header and dropped the coach out of Split.
    expect(screen.queryByText(/← /)).not.toBeInTheDocument();
    // Mobile split has an explicit way back to the rail; CSS keeps it hidden
    // beside the persistent desktop rail.
    expect(screen.getByRole("button", { name: "Back to roster" })).toBeInTheDocument();
  });

  it("shows filtered count when searching", async () => {
    const user = userEvent.setup();
    renderShell(makeResponse());

    await waitFor(() => {
      expect(screen.getByPlaceholderText("Search by name")).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText("Search by name");
    await user.type(input, "Alice");

    await waitFor(() => {
      expect(screen.getByText("1 of 2")).toBeInTheDocument();
    });
  });

  it("renders the Presets nav as a link to /presets", async () => {
    renderShell(makeResponse());

    await waitFor(() => {
      expect(screen.getByText("Presets")).toBeInTheDocument();
    });

    const link = screen.getByRole("link", { name: "Presets" });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute("href", "/presets");
  });

  it("renders the Settings nav as a link to /settings (#154 — the roster is the only home screen now)", async () => {
    renderShell(makeResponse());

    await waitFor(() => {
      expect(screen.getByText("Settings")).toBeInTheDocument();
    });

    const link = screen.getByRole("link", { name: "Settings" });
    expect(link).toHaveAttribute("href", "/settings");
  });

  it("shows a distinct error message when the roster fetch fails", async () => {
    renderShell(null, ["/"], true);

    await waitFor(() => {
      expect(screen.getByText("Couldn't load the roster.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    });
  });

  it("no-match search does not hide the active roster view area", async () => {
    // The no-match state renders in addition to the roster views, not
    // instead of them — so the count bar and view area stay on screen.
    const user = userEvent.setup();
    renderShell(makeResponse());

    await waitFor(() => {
      expect(screen.getByPlaceholderText("Search by name")).toBeInTheDocument();
    });

    const input = screen.getByPlaceholderText("Search by name");
    await user.type(input, "Zorro");

    await waitFor(() => {
      expect(screen.getByText("No member matches the search.")).toBeInTheDocument();
      // The queue header and filtered count are still present.
      expect(screen.getByText("0 of 2")).toBeInTheDocument();
    });
  });
});
