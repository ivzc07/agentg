import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { MemberPage, MemberPageContent } from "../components/MemberPage";
import * as memberApi from "../api/member";
import type { MemberPageData } from "../types/member";

vi.mock("../hooks/useT", () => ({
  useT: () => (key: string): string => {
    const strings: Record<string, string> = {
      // Chrome
      back_to_roster: "← All members",
      member_eyebrow: "Member",
      // Status
      lapsed_tag: "lost",
      snoozed_tag: "paused until {date}",
      // Facts
      member_since: "Member since {date}",
      one_session: "1 session",
      n_sessions: "{n} sessions",
      last_session: "last session {date}",
      no_sessions_yet: "No sessions yet",
      trained_today: "trained today",
      one_day_away: "1 day away",
      days_away: "{n} days away",
      // Routine
      routine: "Routine",
      no_routine: "No active routine",
      chip_agent: "Agent",
      chip_coach: "Coach",
      chip_coach_named: "{name}",
      preset_chip: "Preset: {name}",
      // Sessions
      sessions: "Sessions",
      visit_no_sets: "visit recorded, no sets",
      one_set: "1 set",
      n_sets: "{n} sets",
      newer_page: "‹ newer",
      older_page: "older ›",
      page_x_of_y: "page {page} of {pages}",
      // Weights
      last_weights: "Last weights",
      nothing_logged: "Nothing logged yet",
      // Notes
      notes: "Notes",
      no_notes: "No notes",
      retired_tail: "Retired ({n})",
      retired_on: "retired on {date}",
      // Safety
      safety_section: "Safety flags",
      tick_off: "Tick off",
      flag_seen_by: "Seen by {who} on {date}",
      flag_expired_unseen: "expired, never seen",
    };
    return strings[key] ?? key;
  },
}));

const makeMember = (overrides: Partial<MemberPageData> = {}): MemberPageData => ({
  member_id: 1,
  name: "Alice",
  member_since: "2026-06-01",
  weight_unit: "kg",
  session_count: 2,
  gap_days: 3,
  has_sessions: true,
  last_session_on: "2026-07-12",
  lapsed: false,
  snoozed_until: null,
  routine: [
    {
      weekday: 2,
      name: "Legs",
      exercises: [{ name: "squat", sets: 4, reps: "8-10" }],
    },
  ],
  routine_id: 1,
  routine_preset_name: null,
  coach_authored: false,
  routine_author: null,
  sessions: [
    {
      on: "2026-07-12",
      sets: [
        { exercise: "squat", weight: 65, reps: 8, note: null },
        { exercise: "squat", weight: 65, reps: 8, note: null },
        { exercise: "squat", weight: 65, reps: 6, note: "felt heavy" },
      ],
    },
  ],
  page: 1,
  pages: 1,
  weights: [
    { exercise: "squat", weight: 65, reps: [8, 8, 6], on: "2026-07-12" },
  ],
  notes: [],
  retired_notes: [],
  safety_flags: [],
  ...overrides,
});

function mockT(key: string): string {
  const strings: Record<string, string> = {
    // Chrome
    back_to_roster: "← All members",
    member_eyebrow: "Member",
    // Status
    lapsed_tag: "lost",
    snoozed_tag: "paused until {date}",
    // Facts
    member_since: "Member since {date}",
    one_session: "1 session",
    n_sessions: "{n} sessions",
    last_session: "last session {date}",
    no_sessions_yet: "No sessions yet",
    trained_today: "trained today",
    one_day_away: "1 day away",
    days_away: "{n} days away",
    // Routine
    routine: "Routine",
    no_routine: "No active routine",
    chip_agent: "Agent",
    chip_coach: "Coach",
    chip_coach_named: "{name}",
    preset_chip: "Preset: {name}",
    // Sessions
    sessions: "Sessions",
    visit_no_sets: "visit recorded, no sets",
    one_set: "1 set",
    n_sets: "{n} sets",
    newer_page: "‹ newer",
    older_page: "older ›",
    page_x_of_y: "page {page} of {pages}",
    // Weights
    last_weights: "Last weights",
    nothing_logged: "Nothing logged yet",
    // Notes
    notes: "Notes",
    no_notes: "No notes",
    retired_tail: "Retired ({n})",
    retired_on: "retired on {date}",
    // Safety
    safety_section: "Safety flags",
    tick_off: "Tick off",
    flag_seen_by: "Seen by {who} on {date}",
    flag_expired_unseen: "expired, never seen",
    save_failed: "Failed to save.",
  };
  return strings[key] ?? key;
}

function renderPage(data: MemberPageData) {
  const t = mockT;
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <MemberPageContent data={data} t={t} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function renderPageWithRouter(
  initialEntries: string[] = ["/members/1"]
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={initialEntries}>
          <Routes>
            <Route path="members/:memberId" element={<MemberPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    ),
  };
}

describe("MemberPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the member name as h1", () => {
    renderPage(makeMember());
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Alice");
  });

  it("shows the member eyebrow", () => {
    renderPage(makeMember());
    expect(screen.getByText("Member")).toBeInTheDocument();
  });

  it("shows the back link", () => {
    renderPage(makeMember());
    const links = screen.getAllByRole("link");
    const backLink = links.find((l) => l.textContent?.includes("← All members"));
    expect(backLink).toBeInTheDocument();
    expect(backLink).toHaveAttribute("href", "/");
  });

  it("renders the routine card with exercises", () => {
    renderPage(makeMember());
    expect(screen.getByText("Routine")).toBeInTheDocument();
    expect(screen.getByText("squat")).toBeInTheDocument();
    expect(screen.getByText(/4 × 8-10/)).toBeInTheDocument();
  });

  it("shows no routine message when routine is empty", () => {
    renderPage(makeMember({ routine: [] }));
    expect(screen.getByText("No active routine")).toBeInTheDocument();
  });

  it("renders the sessions card", () => {
    renderPage(makeMember());
    expect(screen.getByText("Sessions")).toBeInTheDocument();
    // Collapsed set line: "squat 65 kg × 8,8,6" rendered in the sessions section
    const sessionsSection = document.getElementById("sessions");
    expect(sessionsSection).toBeInTheDocument();
    // The set line text is inside the sessions section
    expect(sessionsSection!.textContent).toContain("65 kg");
    expect(sessionsSection!.textContent).toContain("8,8,6");
  });

  it("shows session notes as quoted text", () => {
    renderPage(makeMember());
    expect(screen.getByText(/felt heavy/)).toBeInTheDocument();
  });

  it("renders the weights card", () => {
    renderPage(makeMember());
    expect(screen.getByText("Last weights")).toBeInTheDocument();
    expect(screen.getByText("65 kg")).toBeInTheDocument();
  });

  it("shows nothing logged when weights are empty", () => {
    renderPage(makeMember({ weights: [] }));
    expect(screen.getByText("Nothing logged yet")).toBeInTheDocument();
  });

  it("renders the notes card with no notes message", () => {
    renderPage(makeMember());
    expect(screen.getByText("Notes")).toBeInTheDocument();
    expect(screen.getByText("No notes")).toBeInTheDocument();
  });

  it("shows notes when present", () => {
    renderPage(
      makeMember({
        notes: [
          { kind: "injury", text: "Left knee", on: "2026-07-10", retired_on: null },
        ],
      })
    );
    expect(screen.getByText("Left knee")).toBeInTheDocument();
    expect(screen.getByText("injury")).toBeInTheDocument();
  });

  it("shows retired notes in collapsed details", () => {
    renderPage(
      makeMember({
        retired_notes: [
          { kind: "goal", text: "Run marathon", on: "2026-06-01", retired_on: "2026-07-01" },
        ],
      })
    );
    expect(screen.getByText(/Retired/)).toBeInTheDocument();
    expect(screen.getByText("Run marathon")).toBeInTheDocument();
  });

  it("shows facts line with session count and gap", () => {
    renderPage(makeMember());
    expect(screen.getByText(/Member since/)).toBeInTheDocument();
    expect(screen.getByText(/sessions/)).toBeInTheDocument();
    expect(screen.getByText(/days away/)).toBeInTheDocument();
    expect(screen.getByText(/last session/)).toBeInTheDocument();
  });

  it("shows lapsed tag when member is lapsed", () => {
    renderPage(makeMember({ lapsed: true }));
    expect(screen.getByText("lost")).toBeInTheDocument();
  });

  it("shows snoozed tag when snoozed", () => {
    renderPage(makeMember({ snoozed_until: "2026-08-15" }));
    expect(screen.getByText(/paused until/)).toBeInTheDocument();
  });

  it("shows safety flags banner when flags exist", () => {
    renderPage(
      makeMember({
        safety_flags: [
          {
            note_id: 1,
            text: "Dangerous deadlift form",
            on: "2026-07-10",
            status: "open",
            acknowledged_on: null,
            acknowledged_by: null,
          },
        ],
      })
    );
    expect(screen.getByText("Safety flags")).toBeInTheDocument();
    expect(screen.getByText("Dangerous deadlift form")).toBeInTheDocument();
    expect(screen.getByText("Tick off")).toBeInTheDocument();
  });

  it("shows acknowledged flag with coach name", () => {
    renderPage(
      makeMember({
        safety_flags: [
          {
            note_id: 1,
            text: "Bad squat depth",
            on: "2026-07-01",
            status: "acknowledged",
            acknowledged_on: "2026-07-02",
            acknowledged_by: "Coach Ana",
          },
        ],
      })
    );
    expect(screen.getByText("Bad squat depth")).toBeInTheDocument();
    // The flag_seen_by string should be visible
    expect(screen.getByText(/Seen by/)).toBeInTheDocument();
  });

  it("shows expired unacknowledged flag", () => {
    renderPage(
      makeMember({
        safety_flags: [
          {
            note_id: 1,
            text: "Old flag",
            on: "2025-01-01",
            status: "expired",
            acknowledged_on: null,
            acknowledged_by: null,
          },
        ],
      })
    );
    expect(screen.getByText("Old flag")).toBeInTheDocument();
    expect(screen.getByText("expired, never seen")).toBeInTheDocument();
  });

  it("shows pagination when pages > 1", () => {
    renderPage(makeMember({ page: 1, pages: 3 }));
    // Pagination nav is inside the #sessions section
    const sessionsSection = document.getElementById("sessions");
    expect(sessionsSection).toBeInTheDocument();
    expect(sessionsSection!.textContent).toContain("page 1 of 3");
    expect(sessionsSection!.textContent).toContain("older");
  });

  it("does not show pagination for a single page", () => {
    renderPage(makeMember({ page: 1, pages: 1 }));
    const sessionsSection = document.getElementById("sessions");
    expect(sessionsSection).toBeInTheDocument();
    expect(sessionsSection!.textContent).not.toContain("page");
  });

  it("shows the routine ownership chip for coach-authored", () => {
    renderPage(makeMember({ coach_authored: true, routine_author: "Coach Ana" }));
    expect(screen.getByText("Coach Ana")).toBeInTheDocument();
  });

  it("shows the agent chip when not coach-authored", () => {
    renderPage(makeMember({ coach_authored: false }));
    expect(screen.getByText("Agent")).toBeInTheDocument();
  });

  it("shows preset chip when routine has a preset", () => {
    renderPage(makeMember({ routine_preset_name: "Beginner Plan" }));
    expect(screen.getByText(/Preset: Beginner Plan/)).toBeInTheDocument();
  });
});

describe("MemberPage (fetch integration)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("fetches member data via the router param", async () => {
    const data = makeMember({ name: "Bob" });
    vi.spyOn(memberApi, "fetchMember").mockResolvedValue(data);
    renderPageWithRouter(["/members/42"]);

    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("Bob");
    expect(memberApi.fetchMember).toHaveBeenCalledWith(42);
  });

  it("shows loading state while fetching", () => {
    vi.spyOn(memberApi, "fetchMember").mockReturnValue(new Promise(() => {}));
    renderPageWithRouter(["/members/1"]);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows 404 on MemberNotFoundError", async () => {
    vi.spyOn(memberApi, "fetchMember").mockRejectedValue(
      new memberApi.MemberNotFoundError(99)
    );
    renderPageWithRouter(["/members/99"]);
    expect(await screen.findByText("404")).toBeInTheDocument();
  });

  it("shows error with retry on other errors", async () => {
    vi.spyOn(memberApi, "fetchMember").mockRejectedValue(new Error("Network error"));
    renderPageWithRouter(["/members/1"]);
    expect(await screen.findByText("Something went wrong.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("tick-off button fires the mutation", async () => {
    const data = makeMember({
      safety_flags: [
        {
          note_id: 1,
          text: "Bad form",
          on: "2026-07-10",
          status: "open",
          acknowledged_on: null,
          acknowledged_by: null,
        },
      ],
    });
    vi.spyOn(memberApi, "fetchMember").mockResolvedValue(data);
    const tickSpy = vi
      .spyOn(memberApi, "tickOffFlag")
      .mockResolvedValue({ note_id: 1, acknowledged: true });

    const user = userEvent.setup();
    renderPageWithRouter(["/members/1"]);

    expect(await screen.findByText("Tick off")).toBeInTheDocument();
    await user.click(screen.getByText("Tick off"));

    expect(tickSpy).toHaveBeenCalledWith(1, 1);
  });
});
