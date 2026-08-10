import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { AXIS, ChartTooltip, GRID_STROKE } from "@/components/charts";
import {
  Card,
  EmptyState,
  ErrorState,
  Spinner,
  Stat,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api } from "@/lib/api";
import { compactNumber, dateTime, money, signedPercent } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { useScanContext } from "@/state/ScanContext";

export default function Trends() {
  const { scan, scanId, scans, selectScan } = useScanContext();
  const enabled = Boolean(scan?.meta);

  const history = useApi(() => api.trends(30), [scanId], { enabled });
  const comparison = useApi(() => api.compare(scanId), [scanId], { enabled });

  if (!scan?.meta) return <EmptyState title="No scan data" description="Run a scan first." />;
  if (history.error) return <ErrorState message={history.error.message} onRetry={history.reload} />;
  if (!history.data) return <Spinner label="Loading history…" />;

  if (history.data.length < 2) {
    return (
      <EmptyState
        title="Only one scan so far"
        description="Trends compare scans against each other. Run another scan later to see whether waste is going up or down."
      />
    );
  }

  const series = history.data.map((meta) => ({
    label: dateTime(meta.started_at),
    runRate: meta.monthly_run_rate,
    savings: meta.identified_monthly_savings,
    optimized: Math.max(meta.monthly_run_rate - meta.identified_monthly_savings, 0),
  }));

  const delta = comparison.data;
  const first = history.data[0];
  const last = history.data[history.data.length - 1];
  const sinceFirst = last.monthly_run_rate - first.monthly_run_rate;

  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Run rate vs previous scan"
          value={delta?.run_rate_change != null ? money(delta.run_rate_change) : "—"}
          hint={signedPercent(delta?.run_rate_change_percent)}
          tone={
            delta?.run_rate_change == null
              ? "neutral"
              : delta.run_rate_change > 0
                ? "bad"
                : "good"
          }
        />
        <Stat
          label="Savings identified vs previous"
          value={delta?.savings_change != null ? money(delta.savings_change) : "—"}
          hint="More is better: the agent found more to cut"
          tone={delta?.savings_change && delta.savings_change > 0 ? "good" : "neutral"}
        />
        <Stat
          label="Since the first scan"
          value={money(sinceFirst)}
          hint={`${history.data.length} scans recorded`}
          tone={sinceFirst > 0 ? "bad" : "good"}
        />
        <Stat
          label="Current identified waste"
          value={money(last.identified_monthly_savings)}
          hint={`${compactNumber(last.finding_count)} findings`}
          tone="good"
        />
      </div>

      <Card title="Run rate over time" subtitle="Billed run rate against the optimized target">
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={series} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={GRID_STROKE} strokeOpacity={0.35} vertical={false} />
            <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} minTickGap={28} />
            <YAxis
              tick={AXIS}
              tickLine={false}
              axisLine={false}
              width={62}
              tickFormatter={(value: number) => money(value)}
            />
            <Tooltip content={<ChartTooltip />} cursor={{ stroke: GRID_STROKE }} />
            <Legend wrapperStyle={{ fontSize: 12, color: AXIS.stroke }} />
            <Line
              type="monotone"
              dataKey="runRate"
              name="Run rate"
              stroke="#38bdf8"
              strokeWidth={2}
              dot={{ r: 2 }}
            />
            <Line
              type="monotone"
              dataKey="optimized"
              name="Optimized target"
              stroke="#34d399"
              strokeWidth={2}
              strokeDasharray="5 4"
              dot={false}
            />
            <Line
              type="monotone"
              dataKey="savings"
              name="Identified savings"
              stroke="#fbbf24"
              strokeWidth={2}
              dot={{ r: 2 }}
            />
          </LineChart>
        </ResponsiveContainer>
      </Card>

      <Card title="Scan history" bodyClassName="p-0">
        <Table>
          <thead>
            <tr>
              <Th>Scan</Th>
              <Th className="w-32" align="right">
                Resources
              </Th>
              <Th className="w-32" align="right">
                Findings
              </Th>
              <Th className="w-36" align="right">
                Run rate/mo
              </Th>
              <Th className="w-36" align="right">
                Savings/mo
              </Th>
              <Th className="w-28" align="right">
                Duration
              </Th>
            </tr>
          </thead>
          <tbody>
            {[...scans].map((meta) => (
              <tr
                key={meta.scan_id}
                onClick={() => selectScan(meta.scan_id)}
                className={
                  meta.scan_id === scan.meta.scan_id
                    ? "cursor-pointer bg-accent/5"
                    : "cursor-pointer hover:bg-surface-2/50"
                }
              >
                <Td className="text-ink">
                  {dateTime(meta.started_at)}
                  <span className="ml-2 font-mono text-xs text-ink-faint">{meta.scan_id}</span>
                </Td>
                <Td align="right">{compactNumber(meta.resource_count)}</Td>
                <Td align="right">{compactNumber(meta.finding_count)}</Td>
                <Td align="right">{money(meta.monthly_run_rate)}</Td>
                <Td align="right" className="text-good">
                  {money(meta.identified_monthly_savings)}
                </Td>
                <Td align="right">{meta.duration_seconds.toFixed(0)}s</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </div>
  );
}
