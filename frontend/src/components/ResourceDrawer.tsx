import { useEffect } from "react";

import { api } from "@/lib/api";
import { dateTime, humanizeType, metricValue, money, titleCase } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { COST_BASIS_LABELS } from "@/lib/types";
import { useScanId } from "@/state/ScanContext";

import FindingDetail from "./FindingDetail";
import { Badge, ErrorState, Spinner } from "./ui";

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 border-b border-line/40 py-1.5 text-sm last:border-0">
      <span className="text-ink-faint">{label}</span>
      <span className="tabular max-w-[60%] truncate text-right text-ink" title={String(value)}>
        {value}
      </span>
    </div>
  );
}

/** Slide-over with everything known about one resource, plus the findings against it. */
export default function ResourceDrawer({
  arn,
  onClose,
}: {
  arn: string | null;
  onClose: () => void;
}) {
  const scanId = useScanId();
  const open = Boolean(arn);

  const resource = useApi(() => api.resource(scanId, arn as string), [scanId, arn], {
    enabled: open,
  });
  const findings = useApi(
    () => api.findings(scanId, { search: arn?.split("/").pop() ?? "", limit: 20 }),
    [scanId, arn],
    { enabled: open },
  );

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const related = (findings.data?.items ?? []).filter((finding) => finding.resource_arn === arn);
  const data = resource.data;
  // A collector that could not read a field leaves it null; printing "null" tells nobody
  // anything, so those rows are dropped instead.
  const attributes = Object.entries(data?.attributes ?? {}).filter(
    ([, value]) => value !== null && value !== undefined && value !== "",
  );

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div
        className="absolute inset-0 bg-black/50"
        onClick={onClose}
        role="presentation"
        aria-hidden
      />
      <aside className="animate-slide-in relative flex h-full w-full max-w-xl flex-col overflow-y-auto border-l border-line bg-canvas shadow-2xl">
        <header className="sticky top-0 z-10 flex items-start justify-between gap-4 border-b border-line bg-canvas/95 px-5 py-4 backdrop-blur">
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-ink">
              {data?.name || data?.resource_id || "Resource"}
            </p>
            <p className="truncate font-mono text-xs text-ink-faint" title={arn ?? ""}>
              {arn}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-ink-faint hover:bg-surface-2 hover:text-ink"
            aria-label="Close"
          >
            ✕
          </button>
        </header>

        <div className="space-y-6 px-5 py-5">
          {resource.loading && <Spinner label="Loading resource…" />}
          {resource.error && <ErrorState message={resource.error.message} />}

          {data && (
            <>
              <section>
                <div className="mb-3 flex flex-wrap gap-1.5">
                  <Badge tone="accent">{humanizeType(data.resource_type)}</Badge>
                  {data.state && <Badge>{data.state}</Badge>}
                  <Badge>{data.region}</Badge>
                </div>
                <Row label="Monthly cost" value={money(data.monthly_cost)} />
                <Row
                  label="Cost basis"
                  value={data.cost_basis ? COST_BASIS_LABELS[data.cost_basis] : "Not priced"}
                />
                <Row label="Created" value={dateTime(data.created_at)} />
                {data.availability_zone && (
                  <Row label="Availability zone" value={data.availability_zone} />
                )}
                <Row label="Account" value={data.account_id} />
              </section>

              {Object.keys(data.metrics).length > 0 && (
                <section>
                  <p className="mb-1 text-xs font-medium tracking-wide text-ink-faint uppercase">
                    Utilization
                  </p>
                  {Object.entries(data.metrics).map(([key, value]) => (
                    <Row key={key} label={titleCase(key)} value={metricValue(key, value)} />
                  ))}
                </section>
              )}

              {attributes.length > 0 && (
                <section>
                  <p className="mb-1 text-xs font-medium tracking-wide text-ink-faint uppercase">
                    Configuration
                  </p>
                  {attributes.map(([key, value]) => (
                    <Row
                      key={key}
                      label={titleCase(key)}
                      value={typeof value === "object" ? JSON.stringify(value) : String(value)}
                    />
                  ))}
                </section>
              )}

              <section>
                <p className="mb-1 text-xs font-medium tracking-wide text-ink-faint uppercase">
                  Tags
                </p>
                {Object.keys(data.tags).length === 0 ? (
                  <p className="text-sm text-ink-faint">
                    Untagged. This spend cannot be allocated to an owner.
                  </p>
                ) : (
                  Object.entries(data.tags).map(([key, value]) => (
                    <Row key={key} label={key} value={value} />
                  ))
                )}
              </section>

              <section>
                <p className="mb-2 text-xs font-medium tracking-wide text-ink-faint uppercase">
                  Findings ({related.length})
                </p>
                {related.length === 0 ? (
                  <p className="text-sm text-ink-faint">
                    Nothing flagged against this resource.
                  </p>
                ) : (
                  <div className="space-y-4">
                    {related.map((finding) => (
                      <div
                        key={finding.id}
                        className="rounded-xl border border-line/70 bg-surface/60 p-4"
                      >
                        <p className="mb-2 text-sm font-medium text-ink">{finding.title}</p>
                        <FindingDetail finding={finding} />
                      </div>
                    ))}
                  </div>
                )}
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}
