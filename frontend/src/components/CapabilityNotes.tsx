import { titleCase } from "@/lib/format";
import type { CapabilityNote } from "@/lib/types";

import { Badge, Card, type Tone } from "./ui";

const STATUS_TONE: Record<string, Tone> = {
  denied: "bad",
  error: "bad",
  not_enrolled: "warn",
  unavailable: "warn",
  partial: "warn",
  ok: "good",
};

// Worst first: a denied API removed data, while "not enrolled" is a service you never had.
const STATUS_ORDER = ["denied", "error", "unavailable", "not_enrolled", "partial"];

interface Group {
  key: string;
  capability: string;
  status: string;
  detail: string;
  regions: string[];
}

/**
 * One row per capability, listing the regions it failed in.
 *
 * A restricted role denies the same API in every region, so the ungrouped list ran to nearly
 * sixty rows that repeated one sentence — long enough to bury the rest of the page. Grouping
 * is presentation only: the count in the subtitle is still the number of sources.
 */
function groupNotes(notes: CapabilityNote[]): Group[] {
  const groups = new Map<string, Group>();
  for (const note of notes) {
    const detail = note.remedy ?? note.message;
    const key = `${note.capability}|${note.status}|${detail}`;
    const existing = groups.get(key);
    if (existing) {
      if (note.region && !existing.regions.includes(note.region)) {
        existing.regions.push(note.region);
      }
      continue;
    }
    groups.set(key, {
      key,
      capability: note.capability,
      status: note.status,
      detail,
      regions: note.region ? [note.region] : [],
    });
  }

  return [...groups.values()].sort((a, b) => {
    const severity = STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status);
    return severity !== 0 ? severity : a.capability.localeCompare(b.capability);
  });
}

/**
 * Data sources the scan could not read. Showing these matters: a missing Cost
 * Optimization Hub enrollment silently removes a whole class of findings, and without
 * this panel the report would look complete when it is not.
 */
export default function CapabilityNotes({ notes }: { notes: CapabilityNote[] }) {
  const problems = notes.filter((note) => note.status !== "ok");
  const groups = groupNotes(problems);

  // One denied policy explains every row on a restricted role, so it is said once above the
  // list rather than repeated on each line.
  const details = new Set(groups.map((group) => group.detail));
  const sharedDetail = details.size === 1 && groups.length > 1 ? [...details][0] : null;

  return (
    <Card
      title="Data coverage"
      subtitle={
        problems.length
          ? `${problems.length} source(s) unavailable during this scan` +
            (groups.length < problems.length ? `, in ${groups.length} group(s)` : "")
          : "Every data source responded"
      }
    >
      {problems.length === 0 ? (
        <p className="text-sm text-ink-faint">
          Inventory, cost, metrics, and AWS recommendation APIs all answered. The numbers below
          are as complete as this account allows.
        </p>
      ) : (
        <>
          {sharedDetail && (
            <p className="mb-3 border-l-2 border-line pl-3 text-xs text-ink-muted">
              {sharedDetail}
            </p>
          )}
          {/* Capped so a restricted role cannot stretch the page by hundreds of pixels. */}
          <ul className="max-h-72 space-y-2.5 overflow-y-auto pr-2">
            {groups.map((group) => (
              <li key={group.key} className="flex gap-3">
                <Badge tone={STATUS_TONE[group.status] ?? "neutral"}>
                  {titleCase(group.status)}
                </Badge>
                <div className="min-w-0">
                  <p className="text-sm text-ink">
                    {group.capability}
                    {group.regions.length > 0 && (
                      <span className="text-ink-faint"> · {group.regions.join(", ")}</span>
                    )}
                  </p>
                  {!sharedDetail && (
                    <p className="text-xs break-words text-ink-faint">{group.detail}</p>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}
