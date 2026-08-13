import type { ReactNode } from "react";

import { money } from "@/lib/format";

export const AXIS = {
  stroke: "oklch(0.58 0.015 260)",
  fontSize: 11,
};

export const GRID_STROKE = "oklch(0.31 0.018 260)";

interface TooltipEntry {
  name?: ReactNode;
  value?: number | string;
  color?: string;
  dataKey?: string | number;
  payload?: Record<string, unknown>;
}

/** Shared tooltip so every chart reads the same and formats money the same way. */
export function ChartTooltip({
  active,
  payload,
  label,
  formatter = money,
  hideZero = false,
  omitKeys = [],
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: ReactNode;
  formatter?: (value: number) => string;
  hideZero?: boolean;
  /** Series that exist only to position a bar, e.g. the invisible base of a waterfall. */
  omitKeys?: string[];
}) {
  if (!active || !payload?.length) return null;
  const rows = payload.filter(
    (entry) =>
      !omitKeys.includes(String(entry.dataKey)) &&
      (!hideZero || (typeof entry.value === "number" && entry.value !== 0)),
  );
  if (!rows.length) return null;

  return (
    <div className="rounded-lg border border-line bg-canvas/95 px-3 py-2 text-xs shadow-lg">
      {label !== undefined && label !== "" && (
        <p className="mb-1 font-medium text-ink">{String(label)}</p>
      )}
      {rows.map((entry, index) => (
        <p key={index} className="tabular flex items-center gap-2 text-ink-muted">
          {entry.color && (
            <span
              className="inline-block size-2 rounded-full"
              style={{ backgroundColor: entry.color }}
            />
          )}
          <span>{entry.name}</span>
          <span className="ml-auto font-medium text-ink">
            {typeof entry.value === "number" ? formatter(entry.value) : entry.value}
          </span>
        </p>
      ))}
    </div>
  );
}