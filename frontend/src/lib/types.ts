/** Mirrors the pydantic models in backend/finops/model.py. */

export type Effort = "low" | "medium" | "high";
export type Risk = "low" | "medium" | "high";
export type Confidence = "high" | "medium" | "low";

export type CostBasis =
  | "actual_resource_level"
  | "actual_service_level"
  | "list_price_estimate"
  | "aws_recommendation"
  | "heuristic";

export const COST_BASIS_LABELS: Record<CostBasis, string> = {
  actual_resource_level: "Billed cost (resource level)",
  actual_service_level: "Billed cost (allocated from service total)",
  list_price_estimate: "Estimated from list price",
  aws_recommendation: "AWS recommendation estimate",
  heuristic: "Heuristic estimate",
};

export interface Evidence {
  label: string;
  value: string;
}

export interface Remediation {
  summary: string;
  cli?: string | null;
  terraform?: string | null;
  console_path?: string | null;
}

export interface Resource {
  arn: string;
  resource_id: string;
  resource_type: string;
  service: string;
  region: string;
  account_id: string;
  name?: string | null;
  availability_zone?: string | null;
  state?: string | null;
  created_at?: string | null;
  tags: Record<string, string>;
  attributes: Record<string, unknown>;
  metrics: Record<string, number>;
  monthly_cost?: number | null;
  cost_basis?: CostBasis | null;
}

export interface Finding {
  id: string;
  rule_id: string;
  title: string;
  category: string;
  action_type: string;
  service: string;
  source: string;
  resource_arn?: string | null;
  resource_id?: string | null;
  resource_type?: string | null;
  region?: string | null;
  estimated_monthly_savings: number;
  currency: string;
  confidence: Confidence;
  implementation_effort: Effort;
  risk: Risk;
  cost_basis: CostBasis;
  rollback_possible: boolean;
  detail?: string | null;
  evidence: Evidence[];
  remediation?: Remediation | null;
  tags: Record<string, string>;
}

export interface BreakdownItem {
  key: string;
  amount: number;
  share: number;
  savings: number;
}

export interface TcoReport {
  period_start: string;
  period_end: string;
  metric: string;
  currency: string;
  total_cost: number;
  daily_run_rate: number;
  monthly_run_rate: number;
  month_to_date_cost: number;
  forecast_next_month?: number | null;
  forecast_lower?: number | null;
  forecast_upper?: number | null;
  previous_period_cost?: number | null;
  change_percent?: number | null;
  identified_monthly_savings: number;
  optimized_monthly_run_rate: number;
  savings_percent: number;
  by_service: BreakdownItem[];
  by_region: BreakdownItem[];
  by_usage_type: BreakdownItem[];
  by_category: BreakdownItem[];
  by_effort: BreakdownItem[];
  daily_trend: BreakdownItem[];
  untagged_monthly_cost: number;
  commitment_coverage_percent?: number | null;
}

export interface CapabilityNote {
  capability: string;
  status: "ok" | "denied" | "not_enrolled" | "unavailable" | "partial" | "error";
  message: string;
  region?: string | null;
  remedy?: string | null;
}

export interface ScanMeta {
  scan_id: string;
  account_id: string;
  account_alias?: string | null;
  started_at: string;
  finished_at?: string | null;
  duration_seconds: number;
  regions: string[];
  resource_count: number;
  finding_count: number;
  monthly_run_rate: number;
  identified_monthly_savings: number;
  dry_run: boolean;
}

export interface ScanDetail {
  meta: ScanMeta;
  tco?: TcoReport | null;
  notes: CapabilityNote[];
}

export interface ArchitectureRecommendation {
  title: string;
  summary: string;
  rationale: string;
  affected_services: string[];
  estimated_monthly_savings?: number | null;
  implementation_effort: Effort;
  risk: Risk;
  steps: string[];
  related_finding_ids: string[];
  tradeoffs?: string | null;
}

export interface Advice {
  generated_at: string;
  provider: string;
  model: string;
  executive_summary: string;
  recommendations: ArchitectureRecommendation[];
  quick_wins: string[];
  caveats: string[];
  error?: string | null;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface FilterOptions {
  services: string[];
  regions: string[];
  resource_types: string[];
  states: string[];
  finding_categories: string[];
  finding_sources: string[];
  efforts: string[];
}

export interface ScanJob {
  job_id?: string;
  status: "idle" | "queued" | "running" | "succeeded" | "failed";
  stage?: string | null;
  message?: string | null;
  started_at?: string;
  finished_at?: string | null;
  scan_id?: string | null;
  error?: string | null;
  log: string[];
}

export interface Health {
  status: string;
  version: string;
  database: string;
  latest_scan_id: string | null;
  llm_provider: string;
}

export interface Comparison {
  scan_id: string;
  baseline_scan_id: string | null;
  run_rate_change: number | null;
  run_rate_change_percent: number | null;
  savings_change: number | null;
}
