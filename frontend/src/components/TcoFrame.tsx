import type { TcoReport } from "@/lib/types";
import { money, percent } from "@/lib/format";

import { Card } from "./ui";

/**
 * Cost of ownership built from AWS list prices for the inventory a scan found.
 *
 * The bill and this figure answer different questions. Cost Explorer knows what was
 * charged, including commitments and credits, but not always which resource caused it —
 * and an account without `ce:GetCostAndUsage` has no bill to show at all. This is priced
 * per resource from the AWS Price List, so it survives that gap and can be attributed.
 */
export default function TcoFrame({ report }: { report: TcoReport }) {
  const total = report.list_price_monthly_cost;
  const counted = report.priced_resource_count + report.unpriced_resource_count;

  if (total <= 0) return null;

  return (
    <Card
      title="Cost of ownership from AWS pricing"
      subtitle="Priced per resource from the AWS Price List, independent of Cost Explorer"
    >
      <div className="grid gap-5 sm:grid-cols-3">
        <Figure
          label="Monthly TCO"
          value={`${money(total)}/mo`}
          hint={`${money(total * 12)} per year`}
        />
        <Figure
          label="Identified savings"
          value={`${money(report.identified_monthly_savings)}/mo`}
          hint={`${percent(report.list_price_savings_percent)} of the priced estate`}
          tone="text-good"
        />
        <Figure
          label="Optimized TCO"
          value={`${money(report.list_price_optimized_monthly_cost)}/mo`}
          hint="Once the identified changes are made"
          tone="text-accent"
        />
      </div>

      <p className="mt-4 border-t border-line/60 pt-3 text-xs text-ink-faint">
        On-demand list prices for {report.priced_resource_count} of {counted} resources.
        {report.unpriced_resource_count > 0 && (
          <>
            {" "}
            The other {report.unpriced_resource_count} are usage-priced services such as
            Lambda and S3, where a rate cannot be applied without measured usage, so they
            are absent from this total rather than estimated.
          </>
        )}{" "}
        Commitments, credits, and negotiated discounts are not reflected: this is what the
        estate lists at, so it reads higher than an invoice covered by Savings Plans.
      </p>
    </Card>
  );
}

function Figure({
  label,
  value,
  hint,
  tone = "text-ink",
}: {
  label: string;
  value: string;
  hint: string;
  tone?: string;
}) {
  return (
    <div>
      <p className="text-xs font-medium tracking-wide text-ink-faint uppercase">{label}</p>
      <p className={`tabular mt-1.5 text-2xl font-semibold ${tone}`}>{value}</p>
      <p className="mt-1 text-xs text-ink-faint">{hint}</p>
    </div>
  );
}
