import { useMemo, useState } from "react";

import ResourceDrawer from "@/components/ResourceDrawer";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  SearchInput,
  Select,
  Spinner,
  Stat,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api } from "@/lib/api";
import { compactNumber, humanizeType, money } from "@/lib/format";
import { useApi, useDebounced } from "@/lib/hooks";
import { COST_BASIS_LABELS } from "@/lib/types";
import { useScanContext } from "@/state/ScanContext";

const PAGE_SIZE = 100;

export default function Inventory() {
  const { scan, scanId, loading } = useScanContext();
  const [service, setService] = useState("");
  const [region, setRegion] = useState("");
  const [resourceType, setResourceType] = useState("");
  const [state, setState] = useState("");
  const [rawSearch, setRawSearch] = useState("");
  const [page, setPage] = useState(0);
  const [drawerArn, setDrawerArn] = useState<string | null>(null);

  const search = useDebounced(rawSearch);
  const enabled = Boolean(scan?.meta);

  const filters = useApi(() => api.filters(scanId), [scanId], { enabled });
  const resources = useApi(
    () =>
      api.resources(scanId, {
        service: service || undefined,
        region: region || undefined,
        resource_type: resourceType || undefined,
        state: state || undefined,
        search: search || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
    [scanId, service, region, resourceType, state, search, page],
    { enabled },
  );

  const pricedTotal = useMemo(
    () => (resources.data?.items ?? []).reduce((sum, item) => sum + (item.monthly_cost ?? 0), 0),
    [resources.data],
  );

  function reset(setter: (value: string) => void) {
    return (value: string) => {
      setter(value);
      setPage(0);
    };
  }

  if (loading && !scan) return <Spinner label="Loading inventory…" />;
  if (!scan?.meta) return <EmptyState title="No scan data" description="Run a scan first." />;

  const total = resources.data?.total ?? 0;
  const pages = Math.ceil(total / PAGE_SIZE);

  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Resources in scan" value={compactNumber(scan.meta.resource_count)} />
        <Stat label="Matching filters" value={compactNumber(total)} />
        <Stat label="Cost on this page" value={money(pricedTotal)} hint="Sum of priced resources" />
        <Stat
          label="Untagged spend"
          value={money(scan.tco?.untagged_monthly_cost)}
          hint="Cannot be allocated to an owner"
          tone={scan.tco?.untagged_monthly_cost ? "warn" : "neutral"}
        />
      </div>

      <Card
        title="Resource explorer"
        subtitle="Every resource the scan could see, most expensive first"
        bodyClassName="p-0"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <SearchInput
              value={rawSearch}
              onChange={reset(setRawSearch)}
              placeholder="Search id, name, tag"
            />
            <Select
              value={service}
              onChange={reset(setService)}
              placeholder="All services"
              options={(filters.data?.services ?? []).map((value) => ({ value, label: value }))}
            />
            <Select
              value={region}
              onChange={reset(setRegion)}
              placeholder="All regions"
              options={(filters.data?.regions ?? []).map((value) => ({ value, label: value }))}
            />
            <Select
              value={resourceType}
              onChange={reset(setResourceType)}
              placeholder="All types"
              options={(filters.data?.resource_types ?? []).map((value) => ({
                value,
                label: humanizeType(value),
              }))}
            />
            <Select
              value={state}
              onChange={reset(setState)}
              placeholder="Any state"
              options={(filters.data?.states ?? []).map((value) => ({ value, label: value }))}
            />
          </div>
        }
      >
        {resources.error && (
          <ErrorState message={resources.error.message} onRetry={resources.reload} />
        )}
        {resources.loading && !resources.data && (
          <div className="p-5">
            <Spinner label="Loading resources…" />
          </div>
        )}

        {resources.data && resources.data.items.length === 0 && (
          <p className="px-5 py-12 text-center text-sm text-ink-faint">
            No resources match these filters.
          </p>
        )}

        {resources.data && resources.data.items.length > 0 && (
          <>
            <Table>
              <thead>
                <tr>
                  <Th>Resource</Th>
                  <Th className="w-40">Type</Th>
                  <Th className="w-28">Region</Th>
                  <Th className="w-24">State</Th>
                  <Th className="w-32" align="right">
                    Cost/mo
                  </Th>
                  <Th className="w-24">Tags</Th>
                </tr>
              </thead>
              <tbody>
                {resources.data.items.map((resource) => (
                  <tr
                    key={resource.arn}
                    onClick={() => setDrawerArn(resource.arn)}
                    className="cursor-pointer hover:bg-surface-2/50"
                  >
                    <Td className="text-ink">
                      <span className="block truncate">{resource.name ?? resource.resource_id}</span>
                      {resource.name && (
                        <span className="block truncate font-mono text-xs text-ink-faint">
                          {resource.resource_id}
                        </span>
                      )}
                    </Td>
                    <Td>{humanizeType(resource.resource_type)}</Td>
                    <Td>{resource.region}</Td>
                    <Td>
                      {resource.state ? (
                        <Badge
                          tone={
                            resource.state === "running" || resource.state === "available"
                              ? "good"
                              : resource.state === "stopped"
                                ? "warn"
                                : "neutral"
                          }
                        >
                          {resource.state}
                        </Badge>
                      ) : (
                        "—"
                      )}
                    </Td>
                    <Td align="right">
                      <span
                        title={
                          resource.cost_basis
                            ? COST_BASIS_LABELS[resource.cost_basis]
                            : "No defensible price for this resource"
                        }
                        className={
                          resource.cost_basis?.startsWith("actual")
                            ? "text-ink"
                            : "text-ink-muted italic"
                        }
                      >
                        {money(resource.monthly_cost)}
                      </span>
                    </Td>
                    <Td>
                      {Object.keys(resource.tags).length === 0 ? (
                        <Badge tone="warn">untagged</Badge>
                      ) : (
                        <span className="text-xs">{Object.keys(resource.tags).length}</span>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>

            {pages > 1 && (
              <div className="flex items-center justify-between border-t border-line/60 px-5 py-3 text-sm text-ink-faint">
                <span>
                  Page {page + 1} of {pages}
                </span>
                <div className="flex gap-2">
                  <Button onClick={() => setPage((value) => value - 1)} disabled={page === 0}>
                    Previous
                  </Button>
                  <Button
                    onClick={() => setPage((value) => value + 1)}
                    disabled={page + 1 >= pages}
                  >
                    Next
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </Card>

      <p className="text-xs text-ink-faint">
        Italic costs are list-price estimates rather than billed amounts. Resource-level billing
        needs the Cost Explorer resource-level opt-in and only covers the last 14 days.
      </p>

      <ResourceDrawer arn={drawerArn} onClose={() => setDrawerArn(null)} />
    </div>
  );
}
