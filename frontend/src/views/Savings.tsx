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
import { money, percent, titleCase } from "@/lib/format";
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
        source: source || undefined,
        search: search || undefined,
        limit: 500,
      }),
    [scanId, category, effort, source, search],
    { enabled },
  );

  const rows = useMemo(() => {
    const items = [...(findings.data?.items ?? [])];
    if (sort === "savings") {
      items.sort((a, b) => b.estimated_monthly_savings - a.estimated_monthly_savings);
    } else if (sort === "title") {
      items.sort((a, b) => a.title.localeCompare(b.title));
    } else {
      items.sort((a, b) => priority(b) - priority(a));
    }
    return items;
  }, [findings.data, sort]);

  const tco = scan?.tco;

  const waterfall = useMemo(() => {
    if (!tco) return [];
    let running = tco.monthly_run_rate;
    const steps = [{ name: "Current", base: 0, value: tco.monthly_run_rate, kind: "total" }];
    for (const item of tco.by_category) {
      running -= item.amount;
      steps.push({ name: item.key, base: Math.max(running, 0), value: item.amount, kind: "cut" });
    }
    steps.push({
      name: "Optimized",
      base: 0,
      value: tco.optimized_monthly_run_rate,
      kind: "total",
    });
    return steps;
  }, [tco]);

  if (loading && !scan) return <Spinner label="Loading findings…" />;
  if (!scan?.meta) return <EmptyState title="No scan data" description="Run a scan first." />;

  const filtered = rows.reduce((sum, finding) => sum + finding.estimated_monthly_savings, 0);

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
            rows.length !== (findings.data?.total ?? 0)
              ? `${rows.length} shown`
              : "across every category"
          }
        />
      </div>

      <Card
        title="Current spend to optimized spend"
        subtitle="Each step is the savings identified in one category"
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
        subtitle={`${money(filtered)}/mo in the current selection`}
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
                <Th className="w-44">Resource</Th>
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
              {rows.map((finding) => (
                <Fragment key={finding.id}>
                  <tr
                    onClick={() => setExpanded(expanded === finding.id ? null : finding.id)}
                    className="cursor-pointer hover:bg-surface-2/50"
                  >
                    <Td align="right" className="font-medium text-good">
                      {money(finding.estimated_monthly_savings)}
                    </Td>
                    <Td className="text-ink">
                      <span className="mr-1.5 text-ink-faint">
                        {expanded === finding.id ? "▾" : "▸"}
                      </span>
                      {finding.title}
                    </Td>
                    <Td className="truncate font-mono text-xs" >
                      {finding.resource_id ?? finding.region ?? "account"}
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
                  {expanded === finding.id && (
                    <tr className="bg-surface/40">
                      <td colSpan={5} className="border-b border-line/40 px-5 py-4">
                        <FindingDetail finding={finding} />
                        {finding.resource_arn && (
                          <div className="mt-4">
                            <Button onClick={() => setDrawerArn(finding.resource_arn ?? null)}>
                              Open resource
                            </Button>
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <ResourceDrawer arn={drawerArn} onClose={() => setDrawerArn(null)} />
    </div>
  );
}
