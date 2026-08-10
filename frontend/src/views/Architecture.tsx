import { useState } from "react";

import FindingDetail from "@/components/FindingDetail";
import {
  Badge,
  Button,
  Card,
  EffortBadge,
  EmptyState,
  ErrorState,
  RiskBadge,
  Spinner,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { dateTime, money } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { Advice, Finding } from "@/lib/types";
import { useScanContext } from "@/state/ScanContext";

export default function Architecture() {
  const { scan, scanId, health, loading } = useScanContext();
  const [generated, setGenerated] = useState<Advice | null>(null);
  const [working, setWorking] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const enabled = Boolean(scan?.meta);
  const stored = useApi(() => api.advice(scanId), [scanId], { enabled });
  const findings = useApi(() => api.findings(scanId, { limit: 500 }), [scanId], { enabled });

  const advice = generated ?? stored.data;
  const findingsById = new Map((findings.data?.items ?? []).map((item) => [item.id, item]));

  async function generate() {
    setWorking(true);
    setFailure(null);
    try {
      setGenerated(await api.generateAdvice(scanId));
    } catch (cause) {
      setFailure(cause instanceof ApiError ? cause.message : "Could not generate advice");
    } finally {
      setWorking(false);
    }
  }

  if (loading && !scan) return <Spinner label="Loading…" />;
  if (!scan?.meta) return <EmptyState title="No scan data" description="Run a scan first." />;

  const generateButton = (
    <Button variant="primary" onClick={generate} disabled={working}>
      {working ? "Thinking…" : advice ? "Regenerate" : "Generate recommendations"}
    </Button>
  );

  if (!advice && !stored.loading) {
    return (
      <div className="space-y-4">
        <EmptyState
          title="No architectural advice yet"
          description={
            health?.llm_provider === "none"
              ? "No LLM provider is configured. Set FINOPS_LLM_PROVIDER to bedrock, anthropic, or openai in .env, or generate a deterministic summary from the findings."
              : `Ask ${health?.llm_provider} to review this scan and propose structural changes. It only sees aggregates and the ranked findings, never raw inventory.`
          }
          action={generateButton}
        />
        {failure && <ErrorState message={failure} />}
      </div>
    );
  }

  if (!advice) return <Spinner label="Loading advice…" />;

  return (
    <div className="space-y-5">
      {failure && <ErrorState message={failure} />}

      <Card
        title="Executive summary"
        subtitle={
          advice.provider === "none"
            ? "Generated from the findings; no model was available"
            : `${advice.provider} · ${advice.model} · ${dateTime(advice.generated_at)}`
        }
        actions={generateButton}
      >
        <p className="text-sm leading-relaxed text-ink-muted">{advice.executive_summary}</p>

        {advice.quick_wins.length > 0 && (
          <div className="mt-5">
            <p className="text-xs font-medium tracking-wide text-ink-faint uppercase">
              Do these first
            </p>
            <ul className="mt-2 space-y-1.5">
              {advice.quick_wins.map((win, index) => (
                <li key={index} className="flex gap-2 text-sm text-ink-muted">
                  <span className="text-good">✓</span>
                  {win}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Card>

      {advice.recommendations.length > 0 && (
        <div className="space-y-4">
          {advice.recommendations.map((recommendation, index) => (
            <RecommendationCard
              key={index}
              index={index + 1}
              recommendation={recommendation}
              findingsById={findingsById}
            />
          ))}
        </div>
      )}

      {advice.caveats.length > 0 && (
        <Card title="Caveats" subtitle="What this analysis could not see">
          <ul className="space-y-1.5">
            {advice.caveats.map((caveat, index) => (
              <li key={index} className="flex gap-2 text-sm text-ink-muted">
                <span className="text-warn">!</span>
                {caveat}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function RecommendationCard({
  index,
  recommendation,
  findingsById,
}: {
  index: number;
  recommendation: Advice["recommendations"][number];
  findingsById: Map<string, Finding>;
}) {
  const [open, setOpen] = useState(false);
  const linked = recommendation.related_finding_ids
    .map((id) => findingsById.get(id))
    .filter((finding): finding is Finding => Boolean(finding));

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <span className="text-ink-faint">{index}.</span>
          {recommendation.title}
        </span>
      }
      subtitle={recommendation.affected_services.join(" · ") || undefined}
      actions={
        <div className="flex items-center gap-2">
          {recommendation.estimated_monthly_savings ? (
            <span className="tabular text-sm font-semibold text-good">
              {money(recommendation.estimated_monthly_savings)}/mo
            </span>
          ) : (
            <Badge tone="neutral" title="The model could not derive a defensible figure">
              unquantified
            </Badge>
          )}
          <EffortBadge effort={recommendation.implementation_effort} />
          <RiskBadge risk={recommendation.risk} />
        </div>
      }
    >
      <p className="text-sm text-ink-muted">{recommendation.summary}</p>

      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="mt-3 text-xs font-medium text-accent hover:underline"
      >
        {open ? "Hide rationale" : "Why this matters"}
      </button>

      {open && (
        <div className="mt-3 space-y-4 border-t border-line/50 pt-4">
          {recommendation.rationale && (
            <p className="text-sm leading-relaxed text-ink-muted">{recommendation.rationale}</p>
          )}

          {recommendation.steps.length > 0 && (
            <div>
              <p className="text-xs font-medium tracking-wide text-ink-faint uppercase">Steps</p>
              <ol className="mt-1.5 list-inside list-decimal space-y-1 text-sm text-ink-muted">
                {recommendation.steps.map((step, stepIndex) => (
                  <li key={stepIndex}>{step}</li>
                ))}
              </ol>
            </div>
          )}

          {recommendation.tradeoffs && (
            <div>
              <p className="text-xs font-medium tracking-wide text-ink-faint uppercase">
                Trade-offs
              </p>
              <p className="mt-1 text-sm text-ink-muted">{recommendation.tradeoffs}</p>
            </div>
          )}

          {linked.length > 0 && (
            <div>
              <p className="text-xs font-medium tracking-wide text-ink-faint uppercase">
                Supporting findings
              </p>
              <div className="mt-2 space-y-3">
                {linked.map((finding) => (
                  <div key={finding.id} className="rounded-lg border border-line/60 bg-canvas/40 p-3">
                    <p className="mb-2 text-sm font-medium text-ink">{finding.title}</p>
                    <FindingDetail finding={finding} />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
