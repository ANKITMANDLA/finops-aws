import { colorFor, money } from "@/lib/format";

type Slice = { name: string; value: number };

// Beyond this the rows stop being scannable, and the tail is a rounding error anyway.
const VISIBLE = 6;

/** Share below this reads as "0%" when rounded, which looks like nothing rather than a little. */
function shareLabel(share: number): string {
  if (share >= 10) return `${share.toFixed(0)}%`;
  if (share >= 1) return `${share.toFixed(1)}%`;
  return "<1%";
}

/**
 * Cost split by service, as a composition bar plus a ranked list.
 *
 * A treemap was here first and failed on real accounts: one service is routinely 90% of the
 * bill, so its tile filled the panel and every other service became an unlabelled sliver a
 * few pixels wide. Rows stay readable however skewed the split is.
 */
export default function ServiceShare({ services }: { services: Slice[] }) {
  const ranked = services.filter((item) => item.value > 0).sort((a, b) => b.value - a.value);
  const total = ranked.reduce((sum, item) => sum + item.value, 0);

  if (!ranked.length || total <= 0) {
    return <p className="py-10 text-center text-sm text-ink-faint">No service costs returned.</p>;
  }

  const tail = ranked.slice(VISIBLE);
  // The tail is summed rather than dropped, so the rows still add up to the total.
  const rows = [
    ...ranked.slice(0, VISIBLE),
    ...(tail.length
      ? [
          {
            name: `Other (${tail.length} service${tail.length > 1 ? "s" : ""})`,
            value: tail.reduce((sum, item) => sum + item.value, 0),
            detail: tail.map((item) => `${item.name} ${money(item.value)}`).join("\n"),
          },
        ]
      : []),
  ];

  return (
    <div>
      <div className="flex h-2 gap-px overflow-hidden rounded-full bg-surface-2">
        {rows.map((row) => (
          <span
            key={row.name}
            title={`${row.name} · ${money(row.value)}`}
            style={{
              // A hairline keeps a small service visible instead of rounding it out of the bar.
              width: `${(row.value / total) * 100}%`,
              minWidth: 3,
              backgroundColor: colorFor(row.name),
            }}
          />
        ))}
      </div>

      <ul className="mt-4 space-y-2">
        {rows.map((row) => (
          <li
            key={row.name}
            className="flex items-baseline gap-2.5 text-sm"
            title={"detail" in row ? (row.detail as string) : undefined}
          >
            <span
              className="size-2 shrink-0 rounded-full"
              style={{ backgroundColor: colorFor(row.name) }}
            />
            <span className="truncate text-ink-muted">{row.name}</span>
            <span className="tabular ml-auto text-ink">{money(row.value)}</span>
            <span className="tabular w-11 shrink-0 text-right text-xs text-ink-faint">
              {shareLabel((row.value / total) * 100)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
