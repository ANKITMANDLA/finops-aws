import { Fragment, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { AXIS, ChartTooltip, GRID_STROKE } from "@/components/charts";
import FindingDetail from "@/components/FindingDetail";
import NoBaselineNotice from "@/components/NoBaselineNotice";
import ResourceDrawer from "@/components/ResourceDrawer";
import TcoFrame from "@/components/TcoFrame";
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
  cx,
} from "@/components/ui";
import { api } from "@/lib/api";
import { money, percent, titleCase } from "@/lib/format";
import { groupFindings, groupRegion, groupResources } from "@/lib/groups";
import { useApi, useDebounced } from "@/lib/hooks";
import type { Finding } from "@/lib/types";
import { useScanContext } from "@/state/ScanContext";

type SortKey = "savings" | "priority" | "title";

const EFFORT_WEIGHT = { low: 1, medium: 0.7, high: 0.4 } as const;
const RISK_WEIGHT = { low: 1, medium: 0.85, high: 0.6 } as const;
const CONFIDENCE_WEIGHT = { high: 1, medium: 0.85, low: 0.6 } as const;

/** Mirrors Finding.priority_score on the backend so client-side sorting agrees with it. */
function priority(finding: Finding): number {
  return (
    finding.estimated_monthly_savings *
    EFFORT_WEIGHT[finding.implementation_effort] *
    RISK_WEIGHT[finding.risk] *
    CONFIDENCE_WEIGHT[finding.confidence]
  );
}

export default function Savings() {
  const { scan, scanId, loading } = useScanContext();
  const [category, setCategory] = useState("");
  const [effort, setEffort] = useState("");
  const [region, setRegion] = useState("");
  const [source, setSource] = useState("");
  const [rawSearch, setRawSearch] = useState("");
  const [sort, setSort] = useState<SortKey>("priority");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [drawerArn, setDrawerArn] = useState<string | null>(null);

  const search = useDebounced(rawSearch);
  const enabled = Boolean(scan?.meta);

  const filters = useApi(() => api.filters(scanId), [scanId], { enabled });
  const findings = useApi(
    () =>
      api.findings(scanId, {
        category: category || undefined,
        effort: effort || undefined,
        region: region || undefined,
        source: source || undefined,
        search: search || undefined,
        limit: 500,
      }),
    [scanId, category, effort, region, source, search],
    { enabled },
  );

  // A fleet of identical nodes is one decision, so it is one row.
  const rows = useMemo(() => {
    const groups = groupFindings(findings.data?.items ?? []);
    if (sort === "savings") {
      groups.sort((a, b) => b.savings - a.savings);
    } else if (sort === "title") {
      groups.sort((a, b) => a.lead.title.localeCompare(b.lead.title));
    } else {
      groups.sort(
        (a, b) => priority(b.lead) * b.members.length - priority(a.lead) * a.members.length,
      );
    }
    return groups;
  }, [findings.data, sort]);

  const tco = scan?.tco;

  // Without Cost Explorer there is no run rate to walk down from, so the walk starts at what
  // the estate lists at instead. Starting at zero would draw cuts hanging off nothing.
  const billed = (tco?.monthly_run_rate ?? 0) > 0;
  const start = billed ? (tco?.monthly_run_rate ?? 0) : (tco?.list_price_monthly_cost ?? 0);
  const end = billed
    ? (tco?.optimized_monthly_run_rate ?? 0)
    : (tco?.list_price_optimized_monthly_cost ?? 0);

  const waterfall = useMemo(() => {
    if (!tco || start <= 0) return [];
    let running = start;
    const steps = [{ name: billed ? "Current" : "List price", base: 0, value: start, kind: "total" }];
    for (const item of tco.by_category) {
      running -= item.amount;
      steps.push({ name: item.key, base: Math.max(running, 0), value: item.amount, kind: "cut" });
    }
    steps.push({ name: "Optimized", base: 0, value: end, kind: "total" });
    return steps;
  }, [tco, start, end, billed]);

  if (loading && !scan) return <Spinner label="Loading findings…" />;
  if (!scan?.meta) return <EmptyState title="No scan data" description="Run a scan first." />;

  // Alternatives duplicate savings counted elsewhere, so they never join a total.
  const filtered = rows.reduce((sum, group) => sum + group.savings, 0);
  const alternatives = rows.reduce(
    (sum, group) => sum + (group.members.length - group.counted),
    0,
  );
  const shown = rows.reduce((sum, group) => sum + group.members.length, 0);

  return (
    <div className="space-y-5">
      {tco && <NoBaselineNotice report={tco} />}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label="Current run rate" value={money(tco?.monthly_run_rate)} />
        <Stat
          label="Identified savings"
          value={`${money(tco?.identified_monthly_savings)}/mo`}
          hint={`${money((tco?.identified_monthly_savings ?? 0) * 12)} per year`}
          tone="good"
        />
        <Stat
          label="Optimized run rate"
          value={(tco?.monthly_run_rate ?? 0) > 0 ? money(tco?.optimized_monthly_run_rate) : "—"}
          hint={
            (tco?.monthly_run_rate ?? 0) > 0
              ? `${percent(tco?.savings_percent)} reduction`
              : "Needs Cost Explorer access"
          }
          tone="accent"
        />
        <Stat
          label="Findings"
          value={findings.data?.total ?? 0}
          hint={
            shown !== rows.length
              ? `${shown} shown, grouped into ${rows.length} recommendations`
              : "across every category"
          }
        />
      </div>

      {tco && <TcoFrame report={tco} />}

      <Card
        title={billed ? "Current spend to optimized spend" : "List price to optimized list price"}
        subtitle={
          billed
            ? "Each step is the savings identified in one category"
            : "Each step is the savings identified in one category, walked down from AWS list prices"
        }
      >
        {waterfall.length > 2 ? (
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={waterfall} margin={{ top: 16, right: 12, bottom: 0, left: 0 }}>
              <CartesianGrid stroke={GRID_STROKE} strokeOpacity={0.35} vertical={false} />
              <XAxis
                dataKey="name"
                tick={AXIS}
                tickLine={false}
                axisLine={false}
                interval={0}
                angle={-12}
                textAnchor="end"
                height={54}
              />
              <YAxis
                tick={AXIS}
                tickLine={false}
                axisLine={false}
                width={62}
                tickFormatter={(value: number) => money(value)}
              />
              <Tooltip
                content={<ChartTooltip omitKeys={["base"]} />}
                cursor={{ fill: "rgba(148,163,184,0.06)" }}
              />
              <Bar dataKey="base" stackId="waterfall" fill="transparent" isAnimationActive={false} />
              <Bar dataKey="value" name="Amount" stackId="waterfall" radius={[4, 4, 0, 0]}>
                {waterfall.map((step, index) => (
                  <Cell
                    key={index}
                    fill={step.kind === "total" ? "#38bdf8" : "#34d399"}
                    fillOpacity={step.kind === "total" ? 0.85 : 0.7}
                  />
                ))}
                <LabelList
                  dataKey="value"
                  position="top"
                  fontSize={10}
                  fill={AXIS.stroke}
                  formatter={(value) => money(Number(value))}
                />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <p className="py-10 text-center text-sm text-ink-faint">
            No categorized savings to chart yet.
          </p>
        )}
      </Card>

      <Card
        title="Findings"
        subtitle={
          alternatives > 0
            ? `${money(filtered)}/mo in the current selection, excluding ${alternatives} alternative${alternatives === 1 ? "" : "s"}. Identical changes across a fleet share one row.`
            : `${money(filtered)}/mo in the current selection. Identical changes across a fleet share one row.`
        }
        bodyClassName="p-0"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <SearchInput value={rawSearch} onChange={setRawSearch} placeholder="Search findings" />
            <Select
              value={category}
              onChange={setCategory}
              placeholder="All categories"
              options={(filters.data?.finding_categories ?? []).map((value) => ({
                value,
                label: titleCase(value),
              }))}
            />
            <Select
              value={effort}
              onChange={setEffort}
              placeholder="Any effort"
              options={(filters.data?.efforts ?? []).map((value) => ({
                value,
                label: `${titleCase(value)} effort`,
              }))}
            />
            <Select
              value={region}
              onChange={setRegion}
              placeholder="Any region"
              options={(filters.data?.regions ?? []).map((value) => ({ value, label: value }))}
            />
            <Select
              value={source}
              onChange={setSource}
              placeholder="Any source"
              options={(filters.data?.finding_sources ?? []).map((value) => ({
                value,
                label: titleCase(value),
              }))}
            />
          </div>
        }
      >
        {findings.error && <ErrorState message={findings.error.message} onRetry={findings.reload} />}
        {findings.loading && !findings.data && (
          <div className="p-5">
            <Spinner label="Loading findings…" />
          </div>
        )}

        {findings.data && rows.length === 0 && (
          <p className="px-5 py-12 text-center text-sm text-ink-faint">
            Nothing matches these filters.
          </p>
        )}

        {rows.length > 0 && (
          <Table>
            <thead>
              <tr>
                <Th
                  align="right"
                  className="w-28"
                  onClick={() => setSort("savings")}
                  sorted={sort === "savings" ? "desc" : null}
                >
                  Savings/mo
                </Th>
                <Th onClick={() => setSort("title")} sorted={sort === "title" ? "asc" : null}>
                  Finding
                </Th>
                <Th className="w-36">Resource</Th>
                <Th className="w-28">Region</Th>
                <Th className="w-28">Category</Th>
                <Th
                  className="w-52"
                  onClick={() => setSort("priority")}
                  sorted={sort === "priority" ? "desc" : null}
                >
                  Effort / risk
                </Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((group) => {
                const finding = group.lead;
                const fleet = group.members.length > 1;
                const nothingCounts = group.counted === 0;
                return (
                  <Fragment key={group.key}>
                    <tr
                      onClick={() => setExpanded(expanded === group.key ? null : group.key)}
                      className="cursor-pointer hover:bg-surface-2/50"
                    >
                      <Td
                        align="right"
                        className={cx("font-medium", nothingCounts ? "text-ink-faint" : "text-good")}
                      >
                        <span
                          title={
                            nothingCounts
                              ? `Not added to the total; counted under: ${finding.alternative_to}`
                              : undefined
                          }
                        >
                          {money(nothingCounts ? group.each : group.savings)}
                        </span>
                        {fleet && (
                          <span className="mt-0.5 block text-xs font-normal text-ink-faint">
                            {money(group.each)} each
                          </span>
                        )}
                      </Td>
                      <Td className="text-ink">
                        <span className="mr-1.5 text-ink-faint">
                          {expanded === group.key ? "▾" : "▸"}
                        </span>
                        {finding.title}
                        {nothingCounts && (
                          <span className="ml-1.5 text-xs text-ink-faint">(alternative)</span>
                        )}
                        {!nothingCounts && group.counted < group.members.length && (
                          <span className="ml-1.5 text-xs text-ink-faint">
                            ({group.counted} of {group.members.length} counted)
                          </span>
                        )}
                      </Td>
                      <Td className="truncate font-mono text-xs">{groupResources(group)}</Td>
                      <Td className="font-mono text-xs text-ink-muted">
                        <span title={group.regions.join(", ") || undefined}>
                          {groupRegion(group)}
                        </span>
                      </Td>
                      <Td>
                        <Badge tone="accent">{titleCase(finding.category)}</Badge>
                      </Td>
                      <Td>
                        <div className="flex flex-wrap gap-1.5">
                          <Badge
                            tone={
                              finding.implementation_effort === "low"
                                ? "good"
                                : finding.implementation_effort === "medium"
                                  ? "warn"
                                  : "bad"
                            }
                          >
                            {finding.implementation_effort}
                          </Badge>
                          <Badge
                            tone={
                              finding.risk === "low"
                                ? "good"
                                : finding.risk === "medium"
                                  ? "warn"
                                  : "bad"
                            }
                          >
                            {finding.risk} risk
                          </Badge>
                        </div>
                      </Td>
                    </tr>
                    {expanded === group.key && (
                      <tr className="bg-surface/40">
                        <td colSpan={6} className="border-b border-line/40 px-5 py-4">
                          <FindingDetail finding={finding} />
                          {fleet ? (
                            <div className="mt-4">
                              <p className="text-xs font-medium tracking-wide text-ink-faint uppercase">
                                Applies to {group.members.length} resources · {money(group.each)}{" "}
                                each
                                {group.counted < group.members.length &&
                                  ` · ${group.members.length - group.counted} counted under another change`}
                              </p>
                              <div className="mt-2 flex flex-wrap gap-1.5">
                                {group.members.map((member) => (
                                  <button
                                    key={member.id}
                                    type="button"
                                    className={cx(
                                      "rounded-lg border px-2.5 py-1 font-mono text-xs hover:border-accent/40 hover:text-ink",
                                      member.alternative_to
                                        ? "border-line/40 bg-canvas/30 text-ink-faint line-through decoration-ink-faint/60"
                                        : "border-line/70 bg-canvas/60 text-ink-muted",
                                    )}
                                    title={
                                      member.alternative_to
                                        ? `Not counted here; its savings are counted under: ${member.alternative_to}`
                                        : undefined
                                    }
                                    onClick={() => setDrawerArn(member.resource_arn ?? null)}
                                  >
                                    {member.resource_id ?? member.region}
                                  </button>
                                ))}
                              </div>
                              <p className="mt-2 text-xs text-ink-faint">
                                The commands above name{" "}
                                <span className="font-mono">{finding.resource_id}</span>; the same
                                change applies to each of the others.
                                {group.counted < group.members.length &&
                                  " Struck-through resources have a larger change of their own, which is what the total counts for them."}
                              </p>
                            </div>
                          ) : (
                            finding.resource_arn && (
                              <div className="mt-4">
                                <Button onClick={() => setDrawerArn(finding.resource_arn ?? null)}>
                                  Open resource
                                </Button>
                              </div>
                            )
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>

      <ResourceDrawer arn={drawerArn} onClose={() => setDrawerArn(null)} />
    </div>
  );
}
