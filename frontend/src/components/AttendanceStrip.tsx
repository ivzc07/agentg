import type { DayCell } from "../types/roster";
import { useT } from "../hooks/useT";

interface AttendanceStripProps {
  cells: DayCell[];
  compact?: boolean;
}

/**
 * Last-weeks attendance as a Hevy-style strip of hits and misses.
 * Real member data only — no invented columns.
 */
export function AttendanceStrip({ cells, compact = false }: AttendanceStripProps) {
  const t = useT();
  if (cells.length === 0) return null;

  return (
    <span className={`strip inline-flex items-center ${compact ? "gap-px" : "gap-0.5"}`} aria-hidden="true">
      {cells.map((cell, i) => {
        const stateClass: Record<string, string> = {
          hit: "bg-cyan",
          miss: "bg-transparent box-border border border-coral",
          future: "bg-elevation-2/60",
          plain: "bg-elevation-2",
        };
        return (
          <i
            key={`${cell.on}-${i}`}
            title={cell.on}
            className={`block ${compact ? "w-1.5 h-3" : "w-2 h-3.5"} ${stateClass[cell.state] ?? "bg-elevation-2"}`}
          >
            {cell.state === "miss" && (
              <span className="sr">{t("sr_missed").replace("{date}", cell.on)}</span>
            )}
          </i>
        );
      })}
    </span>
  );
}
