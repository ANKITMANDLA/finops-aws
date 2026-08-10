import type { TcoReport } from "@/lib/types";

/**
 * Cost Explorer access is a separate permission from everything else, and plenty of roles
 * (SSO PowerUser among them) lack it. Without it there is no bill to compare against, so
 * run rates read as $0 and every percentage as 0% - which looks like a bug rather than a
 * missing permission. Say so plainly instead.
 */
export default function NoBaselineNotice({ report }: { report: TcoReport }) {
  if (report.monthly_run_rate > 0 || report.identified_monthly_savings <= 0) return null;

  return (
    <div className="rounded-xl border border-warn/40 bg-warn/10 px-4 py-3 text-sm text-ink">
      <span className="font-medium">No spend baseline.</span> Cost Explorer returned no billed
      cost, so run rates, forecasts, and percentages are unavailable. Savings below are priced
      from list rates and AWS's own recommendations, and are still actionable. Grant{" "}
      <code className="font-mono text-xs text-ink-faint">ce:Get*</code> to this identity to see
      what you are actually paying.
    </div>
  );
}
