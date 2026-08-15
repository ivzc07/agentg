import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { AttendanceStrip } from "../components/AttendanceStrip";
import type { DayCell } from "../types/roster";

vi.mock("../hooks/useT", () => ({
  useT: () => (key: string): string =>
    key === "sr_missed" ? "Missed {date}." : key,
}));

describe("AttendanceStrip", () => {
  it("renders one mark per attendance cell", () => {
    const cells: DayCell[] = [
      { on: "2026-08-01", state: "hit" },
      { on: "2026-08-02", state: "miss" },
      { on: "2026-08-03", state: "plain" },
    ];
    const { container } = render(<AttendanceStrip cells={cells} />);
    expect(container.querySelectorAll("i")).toHaveLength(3);
    expect(container.querySelector(".strip")).toHaveAttribute("aria-hidden", "true");
    expect(container).toHaveTextContent("Missed 2026-08-02.");
  });

  it("renders nothing when there is no attendance", () => {
    const { container } = render(<AttendanceStrip cells={[]} />);
    expect(container.querySelector("i")).toBeNull();
  });
});
