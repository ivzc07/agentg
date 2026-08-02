import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { RosterCards } from "../components/RosterCards";
import type { RosterMember, DayCell } from "../types/roster";

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
      grid_label: "last {n} weeks",
      legend_hit: "session",
      legend_miss: "planned, no session",
      band_hot: "Needs you now",
      band_warm: "Slipping",
      band_cool: "On track",
      band_new: "New",
      sr_missed: "missed {date}",
    };
    return strings[key] ?? key;
  },
}));

const makeCells = (): DayCell[] =>
  Array.from({ length: 28 }, (_, i) => ({
    on: `2026-08-${String(i + 1).padStart(2, "0")}`,
    state: i < 3 ? "hit" : i < 6 ? "miss" : i < 15 ? "plain" : "future",
  })) as DayCell[];

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
  attendance: makeCells(),
  ...overrides,
});

function renderCards(members: RosterMember[]) {
  return render(
    <MemoryRouter>
      <RosterCards members={members} />
    </MemoryRouter>
  );
}

describe("RosterCards", () => {
  it("renders member cards with names", () => {
    renderCards([makeMember({ name: "Alice" }), makeMember({ member_id: 2, name: "Bob" })]);
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("Bob")).toBeInTheDocument();
  });

  it("renders severity bands with labels", () => {
    renderCards([
      makeMember({ member_id: 1, name: "Red", severity: "red", missed_days: 3 }),
      makeMember({ member_id: 2, name: "Amber", severity: "amber", missed_days: 1 }),
      makeMember({ member_id: 3, name: "Cool", severity: null }),
    ]);
    expect(screen.getByText("Needs you now")).toBeInTheDocument();
    expect(screen.getByText("Slipping")).toBeInTheDocument();
    expect(screen.getByText("On track")).toBeInTheDocument();
  });

  it("shows band member counts", () => {
    renderCards([
      makeMember({ member_id: 1, severity: "red", missed_days: 3 }),
    ]);
    // The hot band should show count 1
    const hotSection = document.getElementById("band-hot");
    expect(hotSection).toBeInTheDocument();
    expect(hotSection?.querySelector(".count")?.textContent).toBe("1");
  });

  it("renders the legend", () => {
    renderCards([makeMember()]);
    expect(screen.getByText("session")).toBeInTheDocument();
    expect(screen.getByText("planned, no session")).toBeInTheDocument();
  });

  it("renders attendance grid cells", () => {
    renderCards([makeMember()]);
    // The grid should have 28 cells (4 weeks × 7 days)
    // (Each day cell is an <i> in .grid — legend cells are separate with i.l-hit / i.l-miss)
    const gridCells = document.querySelectorAll(".daygrid .grid i");
    expect(gridCells.length).toBe(28);
  });

  it("shows gap text on each card", () => {
    renderCards([makeMember({ gap_days: 0 })]);
    expect(screen.getByText("trained today")).toBeInTheDocument();
  });

  it("shows new tag for members without routines", () => {
    renderCards([makeMember({ is_new: true })]);
    expect(screen.getByText("new")).toBeInTheDocument();
  });

  it("links to member page", () => {
    renderCards([makeMember({ member_id: 7, name: "Alice" })]);
    const link = screen.getByRole("link", { name: "Alice" });
    expect(link).toHaveAttribute("href", "/members/7");
  });

  it("omits empty new band", () => {
    renderCards([makeMember({ severity: "red", missed_days: 4, is_new: false })]);
    expect(screen.queryByText("New")).toBeNull();
  });
});
