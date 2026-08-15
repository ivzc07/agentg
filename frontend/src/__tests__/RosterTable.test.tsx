import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { RosterTable } from "../components/RosterTable";
import type { RosterMember } from "../types/roster";

// Mock useT
vi.mock("../hooks/useT", () => ({
  useT: () => (key: string): string => {
    const strings: Record<string, string> = {
      no_sessions_yet: "No sessions yet",
      trained_today: "trained today",
      one_day_away: "1 day away",
      days_away: "{n} days away",
      missed_one: "1 planned day missed",
      missed_n: "{n} planned days missed",
      new_tag: "new",
      snoozed_tag: "paused until {date}",
      flag_tag: "⚑ safety",
      col_name: "Name",
      col_status: "Status",
      col_gap: "Days away",
      col_missed: "Missed",
    };
    return strings[key] ?? key;
  },
}));

const makeMember = (overrides: Partial<RosterMember> = {}): RosterMember => ({
  member_id: 1,
  name: "John Doe",
  gap_days: 3,
  has_sessions: true,
  is_new: false,
  snoozed_until: null,
  missed_days: 2,
  severity: "amber",
  has_safety_flag: false,
  attendance: [],
  ...overrides,
});

function renderTable(members: RosterMember[]) {
  return render(
    <MemoryRouter>
      <RosterTable members={members} />
    </MemoryRouter>
  );
}

describe("RosterTable", () => {
  it("renders member names", () => {
    renderTable([makeMember({ name: "Alice" }), makeMember({ member_id: 2, name: "Bob" })]);
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("Bob")).toBeInTheDocument();
  });

  it("renders member links pointing to /members/:id", () => {
    renderTable([makeMember({ member_id: 42, name: "Alice" })]);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/members/42");
  });

  it("shows gap text for each member", () => {
    renderTable([makeMember({ gap_days: 0 })]);
    expect(screen.getByText("trained today")).toBeInTheDocument();
  });

  it("shows new tag for members without routines", () => {
    renderTable([makeMember({ is_new: true })]);
    expect(screen.getByText("new")).toBeInTheDocument();
  });

  it("shows safety flag tag", () => {
    renderTable([makeMember({ has_safety_flag: true })]);
    expect(screen.getByText("⚑ safety")).toBeInTheDocument();
  });

  it("shows severity badge for members with severity", () => {
    renderTable([makeMember({ severity: "red", missed_days: 3 })]);
    expect(screen.getByText("3 planned days missed")).toBeInTheDocument();
  });

  it("shows snoozed tag with date", () => {
    renderTable([makeMember({ snoozed_until: "2026-08-15" })]);
    expect(screen.getByText("paused until 2026-08-15")).toBeInTheDocument();
  });

  it("renders an empty list when no members", () => {
    renderTable([]);
    expect(screen.getByRole("list")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("applies data-name attribute for search filtering", () => {
    renderTable([makeMember({ name: "Alice" })]);
    expect(screen.getByText("Alice").closest("li")).toHaveAttribute("data-name", "Alice");
  });

  it("renders an attendance strip from real day cells", () => {
    const { container } = renderTable([
      makeMember({
        attendance: [
          { on: "2026-08-01", state: "hit" },
          { on: "2026-08-02", state: "miss" },
          { on: "2026-08-03", state: "plain" },
        ],
      }),
    ]);
    expect(container.querySelectorAll(".strip i")).toHaveLength(3);
  });

  it("omits the redundant attendance micro-chart in coach-queue rows", () => {
    const { container } = render(
      <MemoryRouter>
        <RosterTable
          layout="queue"
          members={[makeMember({ attendance: [{ on: "2026-08-01", state: "hit" }] })]}
        />
      </MemoryRouter>
    );
    expect(container.querySelector(".strip")).toBeNull();
    expect(screen.getByText("2 planned days missed")).toBeInTheDocument();
    expect(screen.getByText("3 days away")).toBeInTheDocument();
  });
});
