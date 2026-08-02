import { describe, it, expect } from "vitest";
import { filterMembers, initials, gapText } from "../components/roster-utils";
import type { RosterMember } from "../types/roster";

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

const t = (key: string): string => {
  const strings: Record<string, string> = {
    no_sessions_yet: "No sessions yet",
    trained_today: "trained today",
    one_day_away: "1 day away",
    days_away: "{n} days away",
  };
  return strings[key] ?? key;
};

describe("initials", () => {
  it("returns two-letter monogram for first name + last name", () => {
    expect(initials("John Doe")).toBe("JD");
  });

  it("returns first letter for single name", () => {
    expect(initials("Madonna")).toBe("M");
  });

  it("returns ? for empty string", () => {
    expect(initials("")).toBe("?");
  });

  it("uppercases output", () => {
    expect(initials("ana maria")).toBe("AM");
  });
});

describe("gapText", () => {
  it('returns "No sessions yet" for members with no sessions', () => {
    expect(gapText(makeMember({ has_sessions: false }), t)).toBe("No sessions yet");
  });

  it('returns "trained today" for gap_days 0', () => {
    expect(gapText(makeMember({ gap_days: 0 }), t)).toBe("trained today");
  });

  it('returns "1 day away" for gap_days 1', () => {
    expect(gapText(makeMember({ gap_days: 1 }), t)).toBe("1 day away");
  });

  it('returns "N days away" for gap_days > 1', () => {
    expect(gapText(makeMember({ gap_days: 5 }), t)).toBe("5 days away");
  });
});

describe("filterMembers", () => {
  const members: RosterMember[] = [
    makeMember({ member_id: 1, name: "Alice" }),
    makeMember({ member_id: 2, name: "Bob" }),
    makeMember({ member_id: 3, name: "Carlos Ávila" }),
  ];

  it("returns all members when query is empty", () => {
    expect(filterMembers(members, "")).toHaveLength(3);
  });

  it("filters by name substring", () => {
    expect(filterMembers(members, "Ali")).toHaveLength(1);
    expect(filterMembers(members, "Ali")[0].name).toBe("Alice");
  });

  it("is accent-insensitive", () => {
    expect(filterMembers(members, "Avila")).toHaveLength(1);
    expect(filterMembers(members, "Avila")[0].name).toBe("Carlos Ávila");
  });

  it("is case-insensitive", () => {
    expect(filterMembers(members, "bob")).toHaveLength(1);
  });

  it("returns empty array when no match", () => {
    expect(filterMembers(members, "Zoe")).toHaveLength(0);
  });
});
