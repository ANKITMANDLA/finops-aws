const currency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const currencyPrecise = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** Whole dollars for headline figures; nobody plans against cents. */
export function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (Math.abs(value) >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (Math.abs(value) >= 10_000) return `$${(value / 1000).toFixed(1)}k`;
  return currency.format(value);
}

export function moneyExact(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return currencyPrecise.format(value);
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(digits)}%`;
}

export function signedPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

export function compactNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en-US", { notation: "compact" }).format(value);
}

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

export function bytes(value: number): string {
  let scaled = value;
  let unit = 0;
  while (Math.abs(scaled) >= 1024 && unit < BYTE_UNITS.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return `${scaled.toFixed(unit === 0 ? 0 : 1)} ${BYTE_UNITS[unit]}`;
}

/** Metric values carry their unit in the name, so read it and format to match. */
export function metricValue(key: string, value: number): string {
  if (/bytes/.test(key)) {
    const suffix = /per_day/.test(key) ? "/day" : "";
    return bytes(value) + suffix;
  }
  if (/percent|_pct$/.test(key)) return percent(value);
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export function shortDate(value: string): string {
  const date = new Date(value);
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return "never";
  const seconds = (Date.now() - new Date(value).getTime()) / 1000;
  const units: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, "second"],
    [3600, "minute"],
    [86400, "hour"],
    [2592000, "day"],
  ];
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  let previous = 1;
  for (const [limit, unit] of units) {
    if (seconds < limit) return formatter.format(-Math.round(seconds / previous), unit);
    previous = limit;
  }
  return formatter.format(-Math.round(seconds / 2592000), "month");
}

/** "ec2:instance" reads better as "EC2 instance" in a table cell. */
export function humanizeType(resourceType: string): string {
  const [service, kind] = resourceType.split(":");
  const label = (kind ?? "").replace(/[-_]/g, " ");
  return `${service.toUpperCase()} ${label}`.trim();
}

export function titleCase(value: string): string {
  return value.replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Deterministic chart colors so a service keeps its color between views. */
const PALETTE = [
  "#38bdf8",
  "#34d399",
  "#fbbf24",
  "#f472b6",
  "#a78bfa",
  "#fb923c",
  "#22d3ee",
  "#4ade80",
  "#f87171",
  "#c084fc",
  "#facc15",
  "#94a3b8",
];

export function colorFor(key: string): string {
  let hash = 0;
  for (let index = 0; index < key.length; index += 1) {
    hash = (hash * 31 + key.charCodeAt(index)) >>> 0;
  }
  return PALETTE[hash % PALETTE.length];
}

export const PALETTE_COLORS = PALETTE;
