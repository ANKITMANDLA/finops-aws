import { useState } from "react";

import { COST_BASIS_LABELS } from "@/lib/types";
import type { Finding } from "@/lib/types";
import { money, titleCase } from "@/lib/format";

import { Badge, ConfidenceBadge, EffortBadge, RiskBadge, cx } from "./ui";

function CodeBlock({ label, code }: { label: string; code: string }) {
  const [copied, setCopied] = useState(false);

  return (
    <div className="mt-2">
      <div className="flex items-center justify-between">
        <p className="text-xs font-medium text-ink-faint">{label}</p>
        <button
          type="button"
          className="text-xs text-accent hover:underline"
          onClick={() => {
            navigator.clipboard?.writeText(code);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="mt-1 overflow-x-auto rounded-lg border border-line/70 bg-canvas/70 p-3 text-xs text-ink-muted">
        <code>{code}</code>
      </pre>
    </div>
  );
}

/** Everything behind a finding: why we think it, and exactly how to act on it. */
export default function FindingDetail({
  finding,
  className,
}: {
  finding: Finding;
  className?: string;
}) {
  return (
    <div className={cx("space-y-4", className)}>
      <div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone="accent">{titleCase(finding.category)}</Badge>
          <EffortBadge effort={finding.implementation_effort} />
          <RiskBadge risk={finding.risk} />
          <ConfidenceBadge confidence={finding.confidence} />
          <Badge tone={finding.rollback_possible ? "good" : "warn"}>
            {finding.rollback_possible ? "reversible" : "not reversible"}
          </Badge>
        </div>
        {finding.detail && <p className="mt-2 text-sm text-ink-muted">{finding.detail}</p>}
      </div>

      <div className="flex items-baseline gap-2">
        <span className="tabular text-xl font-semibold text-good">
          {money(finding.estimated_monthly_savings)}/mo
        </span>
        <span className="text-xs text-ink-faint">
          {money(finding.estimated_monthly_savings * 12)}/yr ·{" "}
          {COST_BASIS_LABELS[finding.cost_basis]}
        </span>
      </div>

      {finding.evidence.length > 0 && (
        <div>
          <p className="text-xs font-medium tracking-wide text-ink-faint uppercase">Evidence</p>
          <dl className="mt-1.5 space-y-1">
            {finding.evidence.map((item, index) => (
              <div key={index} className="flex justify-between gap-4 text-sm">
                <dt className="text-ink-faint">{item.label}</dt>
                <dd className="tabular text-right text-ink">{item.value}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {finding.remediation && (
        <div>
          <p className="text-xs font-medium tracking-wide text-ink-faint uppercase">Remediation</p>
          <p className="mt-1.5 text-sm text-ink-muted">{finding.remediation.summary}</p>
          {finding.remediation.console_path && (
            <p className="mt-1 text-xs text-ink-faint">Console: {finding.remediation.console_path}</p>
          )}
          {finding.remediation.cli && <CodeBlock label="AWS CLI" code={finding.remediation.cli} />}
          {finding.remediation.terraform && (
            <CodeBlock label="Terraform" code={finding.remediation.terraform} />
          )}
        </div>
      )}

      <p className="text-xs text-ink-faint">
        Rule <span className="font-mono">{finding.rule_id}</span> · source {finding.source}
      </p>
    </div>
  );
}
