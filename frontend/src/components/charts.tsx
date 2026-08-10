import type { ReactNode } from "react";

import { colorFor, money } from "@/lib/format";

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

interface TreemapCellProps {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  name?: string;
  value?: number;
  depth?: number;
  root?: { value?: number };
}

/** Treemap tile: label and amount, but only when the tile is big enough to read. */
export function TreemapCell(props: TreemapCellProps) {
  const { x = 0, y = 0, width = 0, height = 0, name = "", value = 0, depth = 1, root } = props;

  // Recharts renders the root node too, and it spans the whole chart. Drawing it would stack
  // a "100%" label on top of the largest tile's own label.
  if (depth === 0) return <g />;

  const total = root?.value ?? 0;
  const share = total ? (value / total) * 100 : 0;
  const showLabel = width > 74 && height > 34;
  const showValue = width > 74 && height > 52;

  return (
    <g>
      <rect
        x={x}
        y={y}
        width={width}
        height={height}
        rx={6}
        fill={colorFor(name)}
        fillOpacity={0.22}
        stroke={colorFor(name)}
        strokeOpacity={0.55}
      />
      {showLabel && (
        <text x={x + 10} y={y + 20} fill="oklch(0.96 0.005 260)" fontSize={12} fontWeight={500}>
          {name.length > Math.floor(width / 8) ? `${name.slice(0, Math.floor(width / 8))}…` : name}
        </text>
      )}
      {showValue && (
        <text x={x + 10} y={y + 38} fill="oklch(0.72 0.015 260)" fontSize={11}>
          {money(value)} · {share.toFixed(0)}%
        </text>
      )}
    </g>
  );
}
