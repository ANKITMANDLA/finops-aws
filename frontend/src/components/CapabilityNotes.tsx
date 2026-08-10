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

/**
 * Data sources the scan could not read. Showing these matters: a missing Cost
 * Optimization Hub enrollment silently removes a whole class of findings, and without
 * this panel the report would look complete when it is not.
 */
export default function CapabilityNotes({ notes }: { notes: CapabilityNote[] }) {
  const problems = notes.filter((note) => note.status !== "ok");

  return (
    <Card
      title="Data coverage"
      subtitle={
        problems.length
          ? `${problems.length} source(s) unavailable during this scan`
          : "Every data source responded"
      }
    >
      {problems.length === 0 ? (
        <p className="text-sm text-ink-faint">
          Inventory, cost, metrics, and AWS recommendation APIs all answered. The numbers below
          are as complete as this account allows.
        </p>
      ) : (
        <ul className="space-y-3">
          {problems.map((note, index) => (
            <li key={`${note.capability}-${note.region}-${index}`} className="flex gap-3">
              <Badge tone={STATUS_TONE[note.status] ?? "neutral"}>
                {titleCase(note.status)}
              </Badge>
              <div className="min-w-0">
                <p className="text-sm text-ink">
                  {note.capability}
                  {note.region && <span className="text-ink-faint"> · {note.region}</span>}
                </p>
                <p className="text-xs break-words text-ink-faint">{note.remedy ?? note.message}</p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
