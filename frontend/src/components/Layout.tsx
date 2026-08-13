import { NavLink, Outlet } from "react-router-dom";

import { dateTime, money, relativeTime } from "@/lib/format";
import { useScanContext } from "@/state/ScanContext";

import ChatWidget from "./ChatWidget";
import { Badge, Button, Select, Spinner, cx } from "./ui";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/savings", label: "Savings" },
  { to: "/inventory", label: "Inventory" },
  { to: "/architecture", label: "Architecture" },
  { to: "/trends", label: "Trends" },
];

export default function Layout() {
  const { scan, scans, scanId, selectScan, health, job, scanning, startScan, startError } =
    useScanContext();

  const account = scan?.meta
    ? `${scan.meta.account_alias ?? scan.meta.account_id}`
    : (health?.latest_scan_id ?? "no scans yet");

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-56 shrink-0 flex-col border-r border-line/70 bg-surface/40 px-4 py-5">
        <div className="px-2">
          <p className="text-sm font-semibold tracking-tight text-ink">FinOps Agent</p>
          <p className="mt-0.5 text-xs text-ink-faint">AWS cost analysis</p>
        </div>

        <nav className="mt-7 flex flex-col gap-0.5">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cx(
                  "rounded-lg px-3 py-2 text-sm transition-colors",
                  isActive
                    ? "bg-accent/10 font-medium text-accent"
                    : "text-ink-muted hover:bg-surface-2 hover:text-ink",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="mt-auto space-y-2 px-2 pt-6 text-xs text-ink-faint">
          {scan?.meta && (
            <>
              <p title={scan.meta.account_id}>Account {account}</p>
              <p>{scan.meta.regions.length} region(s)</p>
              <p title={dateTime(scan.meta.started_at)}>
                Scanned {relativeTime(scan.meta.started_at)}
              </p>
            </>
          )}
          {health && <p>v{health.version}</p>}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center gap-3 border-b border-line/70 bg-surface/30 px-6 py-3">
          <div className="min-w-0">
            {scan?.tco ? (
              <>
                <p className="tabular flex items-center gap-2 text-sm font-medium text-ink">
                  {money(scan.tco.monthly_run_rate)}/mo run rate
                  <span className="font-normal text-good">
                    {money(scan.tco.identified_monthly_savings)}/mo identified
                  </span>
                  {scan.meta?.dry_run && (
                    <Badge tone="warn" title="Synthetic data from finops scan --dry-run">
                      Demo data
                    </Badge>
                  )}
                </p>
                <p className="text-xs text-ink-faint">
                  {scan.tco.period_start} to {scan.tco.period_end} · {scan.tco.metric}
                </p>
              </>
            ) : (
              <p className="text-sm text-ink-faint">No scan data yet</p>
            )}
          </div>

          <div className="ml-auto flex items-center gap-3">
            {scanning && <Spinner label={`${job?.stage ?? "scanning"}: ${job?.message ?? ""}`} />}
            {!scanning && job?.status === "failed" && (
              <span className="max-w-md truncate text-xs text-bad" title={job.error ?? ""}>
                Last scan failed: {job.error}
              </span>
            )}
            {scans.length > 0 && (
              <Select
                value={scanId}
                onChange={selectScan}
                options={[
                  { value: "latest", label: "Latest scan" },
                  ...scans.map((meta) => ({
                    value: meta.scan_id,
                    label:
                      `${dateTime(meta.started_at)} · ${money(meta.monthly_run_rate)}/mo` +
                      (meta.dry_run ? " · demo" : ""),
                  })),
                ]}
              />
            )}
            <Button variant="primary" onClick={startScan} disabled={scanning}>
              {scanning ? "Scanning…" : "Run scan"}
            </Button>
          </div>
        </header>

        {startError && (
          <div className="border-b border-bad/30 bg-bad/10 px-6 py-2 text-sm text-bad">
            {startError}
          </div>
        )}

        <main className="min-w-0 flex-1 px-6 py-6">
          <Outlet />
        </main>
      </div>

      {/* Outside <main> so a conversation is not remounted by route changes. */}
      <ChatWidget />
    </div>
  );
}
