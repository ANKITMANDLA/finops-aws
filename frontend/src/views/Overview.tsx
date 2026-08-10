import { Link } from "react-router-dom";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  Treemap,
  XAxis,
  YAxis,
} from "recharts";

import { AXIS, ChartTooltip, GRID_STROKE, TreemapCell } from "@/components/charts";
import CapabilityNotes from "@/components/CapabilityNotes";
import NoBaselineNotice from "@/components/NoBaselineNotice";
import { Badge, Button, Card, EmptyState, ErrorState, Spinner, Stat, Table, Td, Th } from "@/components/ui";
import { api } from "@/lib/api";
import { colorFor, money, moneyExact, percent, shortDate, signedPercent } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { Finding } from "@/lib/types";
import { useScanContext } from "@/state/ScanContext";

export default function Overview() {
  const { scan, loading, error, scanId, startScan, scanning, refresh } = useScanContext();
  const findings = useApi(() => api.findings(scanId, { limit: 5 }), [scanId], {
    enabled: Boolean(scan?.tco),
  });

  if (loading && !scan) return <Spinner label="Loading scan…" />;

  if (error?.isMissing || (!loading && !scan)) {
    return (
      <EmptyState
        title="No scans yet"
        description="Run a read-only scan to inventory this AWS account, price every resource, and find savings. It takes a few minutes and never modifies anything."
        action={
          <Button variant="primary" onClick={startScan} disabled={scanning}>
            {scanning ? "Scanning…" : "Run the first scan"}
          </Button>
        }
      />
    );
  }

  if (error) return <ErrorState message={error.message} onRetry={refresh} />;

  const tco = scan?.tco;
  if (!tco) {
    return <EmptyState title="This scan has no cost report" description="Try running a new scan." />;
  }

  const trend = tco.daily_trend.map((item) => ({ day: shortDate(item.key), amount: item.amount }));
  const services = tco.by_service.map((item) => ({ name: item.key, value: item.amount }));
  const regions = tco.by_region.slice(0, 8);

  return (
    <div className="space-y-5">
      <NoBaselineNotice report={tco} />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Monthly run rate"
          value={money(tco.monthly_run_rate)}
          hint={`${moneyExact(tco.daily_run_rate)} per day`}
        />
        <Stat
          label="Month to date"
          value={money(tco.month_to_date_cost)}
          hint={
            tco.change_percent !== null && tco.change_percent !== undefined
              ? `${signedPercent(tco.change_percent)} vs previous period`
              : "No prior period to compare"
          }
          tone={
            tco.change_percent && tco.change_percent > 5
              ? "bad"
              : tco.change_percent && tco.change_percent < -5
                ? "good"
                : "neutral"
          }
        />
        <Stat
          label="Forecast next month"
          value={money(tco.forecast_next_month)}
          hint={
            tco.forecast_lower != null && tco.forecast_upper != null
              ? `${money(tco.forecast_lower)} – ${money(tco.forecast_upper)}`
              : "Cost Explorer forecast"
          }
        />
        <Stat
          label="Identified savings"
          value={`${money(tco.identified_monthly_savings)}/mo`}
          hint={
            tco.monthly_run_rate > 0
              ? `${percent(tco.savings_percent)} of spend · optimized ${money(tco.optimized_monthly_run_rate)}/mo`
              : "No spend baseline to compare against"
          }
          tone="good"
        />
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card
          title="Daily cost"
          subtitle={`${tco.metric} over ${tco.period_start} to ${tco.period_end}`}
          className="xl:col-span-2"
        >
          {trend.length > 1 ? (
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={trend} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="costFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#38bdf8" stopOpacity={0.45} />
                    <stop offset="100%" stopColor="#38bdf8" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke={GRID_STROKE} strokeOpacity={0.4} vertical={false} />
                <XAxis dataKey="day" tick={AXIS} tickLine={false} axisLine={false} minTickGap={24} />
                <YAxis
                  tick={AXIS}
                  tickLine={false}
                  axisLine={false}
                  width={58}
                  tickFormatter={(value: number) => money(value)}
                />
                <Tooltip content={<ChartTooltip />} cursor={{ stroke: GRID_STROKE }} />
                <ReferenceLine
                  y={tco.daily_run_rate}
                  stroke="#94a3b8"
                  strokeDasharray="4 4"
                  label={{ value: "average", position: "right", fill: AXIS.stroke, fontSize: 10 }}
                />
                <Area
                  type="monotone"
                  dataKey="amount"
                  name="Daily cost"
                  stroke="#38bdf8"
                  strokeWidth={2}
                  fill="url(#costFill)"
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <p className="py-10 text-center text-sm text-ink-faint">
              Not enough daily data points to draw a trend.
            </p>
          )}
        </Card>

        <Card title="Where the money goes" subtitle="Share of spend by service">
          {services.length ? (
            <ResponsiveContainer width="100%" height={260}>
              <Treemap
                data={services}
                dataKey="value"
                stroke="none"
                content={<TreemapCell />}
                isAnimationActive={false}
              >
                <Tooltip content={<ChartTooltip />} />
              </Treemap>
            </ResponsiveContainer>
          ) : (
            <p className="py-10 text-center text-sm text-ink-faint">No service costs returned.</p>
          )}
        </Card>
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card title="Cost by region" className="xl:col-span-1">
          {regions.length ? (
            <ResponsiveContainer width="100%" height={Math.max(180, regions.length * 32)}>
              <BarChart data={regions} layout="vertical" margin={{ left: 8, right: 16 }}>
                <CartesianGrid stroke={GRID_STROKE} strokeOpacity={0.3} horizontal={false} />
                <XAxis
                  type="number"
                  tick={AXIS}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(value: number) => money(value)}
                />
                <YAxis
                  type="category"
                  dataKey="key"
                  tick={AXIS}
                  tickLine={false}
                  axisLine={false}
                  width={92}
                />
                <Tooltip content={<ChartTooltip />} cursor={{ fill: "rgba(148,163,184,0.08)" }} />
                <Bar dataKey="amount" name="Cost" radius={[0, 4, 4, 0]}>
                  {regions.map((item) => (
                    <Cell key={item.key} fill={colorFor(item.key)} fillOpacity={0.75} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="py-10 text-center text-sm text-ink-faint">No regional split available.</p>
          )}
        </Card>

        <Card
          title="Biggest opportunities"
          subtitle="Ranked by value against effort"
          className="xl:col-span-2"
          actions={
            <Link
              to="/savings"
              className="text-xs font-medium text-accent hover:underline"
            >
              See all findings →
            </Link>
          }
          bodyClassName="p-0"
        >
          {findings.data?.items.length ? (
            <Table>
              <thead>
                <tr>
                  <Th align="right" className="w-28">
                    Savings/mo
                  </Th>
                  <Th>Finding</Th>
                  <Th className="w-40">Resource</Th>
                  <Th className="w-44">Effort</Th>
                </tr>
              </thead>
              <tbody>
                {findings.data.items.map((finding: Finding) => (
                  <tr key={finding.id} className="hover:bg-surface-2/50">
                    <Td align="right" className="font-medium text-good">
                      {money(finding.estimated_monthly_savings)}
                    </Td>
                    <Td className="text-ink">{finding.title}</Td>
                    <Td className="truncate font-mono text-xs">
                      {finding.resource_id ?? finding.region ?? "account"}
                    </Td>
                    <Td>
                      <div className="flex gap-1.5">
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
                        <Badge tone="neutral">{finding.source}</Badge>
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <p className="px-5 py-10 text-center text-sm text-ink-faint">
              No findings in this scan. Either the account is tidy, or the scan could not read
              enough data.
            </p>
          )}
        </Card>
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card title="Cost allocation">
          <dl className="space-y-3 text-sm">
            <div className="flex justify-between">
              <dt className="text-ink-faint">Untagged monthly cost</dt>
              <dd className="tabular text-ink">{money(tco.untagged_monthly_cost)}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-ink-faint">Commitment coverage</dt>
              <dd className="tabular text-ink">{percent(tco.commitment_coverage_percent)}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-ink-faint">Resources inventoried</dt>
              <dd className="tabular text-ink">{scan?.meta.resource_count ?? 0}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-ink-faint">Scan duration</dt>
              <dd className="tabular text-ink">{scan?.meta.duration_seconds.toFixed(1)}s</dd>
            </div>
          </dl>
        </Card>

        <div className="xl:col-span-2">
          <CapabilityNotes notes={scan?.notes ?? []} />
        </div>
      </div>
    </div>
  );
}
