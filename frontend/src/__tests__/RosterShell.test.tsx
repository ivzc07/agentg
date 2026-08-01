import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { RosterShell } from "../components/RosterShell";
import * as rosterApi from "../api/roster";
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

function renderShell(response: RosterResponse) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  vi.spyOn(rosterApi, "fetchRoster").mockResolvedValue(response);

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <RosterShell name="Coach" gym="Iron Temple" />
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

  it("shows the member count", async () => {
    renderShell(makeResponse());
    await waitFor(() => {
      expect(screen.getByText("Members (2)")).toBeInTheDocument();
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

  it("shows the count bar with sort info", async () => {
    renderShell(makeResponse());
    await waitFor(() => {
      expect(screen.getByText("Sorted by days away")).toBeInTheDocument();
    });
  });

  it("shows the view switcher with all three views", async () => {
    renderShell(makeResponse());
    await waitFor(() => {
      expect(screen.getByLabelText("Table")).toBeInTheDocument();
      expect(screen.getByLabelText("Cards")).toBeInTheDocument();
      expect(screen.getByLabelText("Split")).toBeInTheDocument();
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
});
