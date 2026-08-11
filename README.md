# AWS FinOps Agent

A local-first FinOps agent for a single AWS account. It inventories every resource,
reconciles that inventory against what Cost Explorer says you actually paid, quantifies
the waste with deterministic rules, asks an LLM for the architectural changes no
per-resource rule can see, and shows all of it in a dashboard.

It is **strictly read-only** against AWS. Nothing it does can change your infrastructure.

```
Collect (read only)          Analyze                     Present
──────────────────────       ──────────────────────      ─────────────────
Collectors (17 services)  ┐  Rules engine ──► Findings   FastAPI ──► React
Cost Explorer             ├─► SQLite scan store          dashboard
CloudWatch GetMetricData  │  TCO report
Compute Optimizer / COH   ┘  LLM advisor ──► Architecture
```

## Quickstart

```bash
# 1. Install the backend (Python 3.11+)
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"

# 2. See it work with no AWS account at all
finops scan --dry-run

# 3. Build the dashboard and open it
cd frontend && npm install && npm run build && cd ..
finops serve                      # http://127.0.0.1:8000
```

`--dry-run` scans a realistic mock account, so you can exercise every rule, the report,
and the whole dashboard before pointing anything at AWS. Cost and utilization figures in
a dry run are synthetic and the scan says so, in the UI and in the CLI output. When you no
longer need them, `finops prune --demo` clears them out.

### Pointing it at a real account

1. Attach `iam/finops-readonly-policy.json` to the identity you will use. Every action in
   it is a `Describe`, `List`, or `Get`.
2. Configure access and preferences:

```bash
cp .env.example .env
# then set at least:
#   FINOPS_AWS_PROFILE=your-profile
#   FINOPS_REGIONS=us-east-1,eu-west-1     # omit to scan every opted-in region
```

3. Confirm the agent can authenticate, then scan:

```bash
finops whoami
finops scan
finops serve
```

A scan aborts immediately if it cannot authenticate, rather than reporting an account that
looks empty. Individual denied permissions are a different matter: those are recorded and
the scan continues.

A first scan of a mid-size account takes a few minutes. Cost Explorer bills roughly
$0.01 per request, so a scan costs a few cents; results are cached in SQLite and the
dashboard reads only from there.

## Commands

| Command | What it does |
| --- | --- |
| `finops whoami` | Show the AWS identity, regions, and settings that will be used |
| `finops scan` | Full read-only scan; stores the result and prints a report |
| `finops scan --dry-run` | Same pipeline against a mocked account; no credentials needed |
| `finops report [scan_id]` | Print a stored scan (`latest` by default, `--json` for raw) |
| `finops scans` | List stored scans |
| `finops advise [scan_id]` | Re-run only the LLM layer against a stored scan |
| `finops prune` | Delete stored scans: `--keep N` by age, `--demo` for dry-run scans, `--empty` for runs that collected nothing, `--id` for one specific scan |
| `finops serve` | Serve the API and, if built, the dashboard |
| `finops policy` | Print the read-only IAM policy |

Useful scan flags: `--regions us-east-1,eu-west-1`, `--collectors ec2,ebs`,
`--rules ebs.gp2_to_gp3`, `--no-metrics`, `--no-native`, `--no-advice`, `--output scan.json`.

## Dashboard

`finops serve` serves the built dashboard from the same origin as the API. For frontend
development, run the API and Vite side by side; Vite proxies `/api` to port 8000:

```bash
finops serve            # terminal 1
cd frontend && npm run dev   # terminal 2, http://localhost:5173
```

- **Overview** — run-rate, month-to-date, forecast and identified savings, the daily cost
  curve, a treemap of spend by service, and which data sources the scan could not read.
- **Savings** — a waterfall from current spend to the optimized target, plus every finding
  with its evidence, effort and risk badges, and a copyable CLI or Terraform snippet.
- **Inventory** — filterable resource explorer; click any row for the full drill-in.
- **Architecture** — the LLM's structural recommendations, each expandable to its
  rationale, steps, trade-offs, and the findings that justify it.
- **Trends** — scan-over-scan movement, so you can see whether waste is going up or down.

Starting a scan from the dashboard runs it in the background: the UI keeps serving the
previous scan while the new one collects.

## Where the numbers come from

This matters more than any feature, so the agent is explicit about it everywhere.

- **The headline total is the bill.** `ce:GetCostAndUsage` with the `AmortizedCost` metric,
  grouped by service, region, and usage type, filtered to exclude credits, refunds, and
  tax. Per-resource estimates are never summed into a headline figure.
- **Per-resource cost is best-effort.** `GetCostAndUsageWithResources` gives real billed
  cost per resource, but it is opt-in and retains only 14 days. Without it, the agent
  estimates from the Price List API. Every figure carries a `cost_basis` — billed,
  allocated, list-price estimate, AWS recommendation, or heuristic — and the UI shows it.
- **Savings come from rules, not from the model.** Each finding is produced by a
  deterministic rule with cited evidence (metric values, resource state, configuration).
  The LLM only ever sees aggregates and the ranked findings, and is told not to invent
  dollar figures.
- **No double counting.** Our rules overlap with Compute Optimizer and Cost Optimization
  Hub. Findings are keyed by `(action, resource)` and merged, preferring AWS's own
  estimate, so the same change is never counted twice.
- **Savings never exceed the bill**, however enthusiastic the rules get.
- **Ranking is value against effort.** A gp2-to-gp3 switch you can do this afternoon ranks
  above a Graviton migration with a bigger headline number.

## What it looks at

**Compute** — EC2 instances (idle, stopped but still paying for EBS, previous generation,
Graviton candidates, underutilized), Auto Scaling groups, Lambda (ARM candidates, unused
provisioned concurrency).
**Storage** — unattached EBS volumes, gp2-to-gp3, over-provisioned IOPS, stale snapshots
and unused AMIs, S3 lifecycle and Intelligent-Tiering gaps, incomplete multipart uploads,
versioning without expiry, log groups with unbounded retention.
**Network** — unassociated Elastic IPs, idle NAT Gateways, load balancers with no healthy
targets or negligible traffic.
**Containers** — empty EKS clusters, node groups with no Spot capacity, many small clusters
each paying a control plane fee.
**Databases** — idle RDS instances, unused read replicas, gp2 storage, Graviton candidates,
stale manual snapshots.
**Commitments** — Savings Plans and Reserved Instance coverage gaps, utilization waste, and
AWS's own purchase recommendations.
**Governance** — untagged resources and spend that cannot be allocated to an owner.

## Graceful degradation

Trusted Advisor cost checks need Business or Enterprise Support, Cost Optimization Hub
needs enrollment, Compute Optimizer needs opt-in, and resource-level cost data needs a
billing preference. Each of those is optional: the collector records a capability note
explaining what was unavailable and why, the scan continues, and the dashboard shows the
gap on the Overview page rather than pretending the report is complete.

## Configuration

Everything is environment-driven with the `FINOPS_` prefix; see `.env.example`. Rule
thresholds are tunable with a double underscore, for example:

```bash
FINOPS_THRESHOLDS__CPU_IDLE_PERCENT=3
FINOPS_THRESHOLDS__EBS_UNATTACHED_MIN_AGE_DAYS=14
FINOPS_THRESHOLDS__MIN_MONTHLY_SAVINGS_USD=5
```

### LLM provider

| `FINOPS_LLM_PROVIDER` | Needs | Notes |
| --- | --- | --- |
| `bedrock` (default) | `bedrock:InvokeModel` and model access | Reuses your AWS credentials |
| `anthropic` | `FINOPS_ANTHROPIC_API_KEY` | Messages API |
| `openai` | `FINOPS_OPENAI_API_KEY` | Any OpenAI-compatible endpoint via `FINOPS_OPENAI_BASE_URL` |
| `none` | — | Deterministic summary assembled from the findings |

If the model is unreachable, misconfigured, or returns something unparseable, the agent
falls back to a deterministic summary built from the findings and records why. A scan
never fails because of the LLM.

## Layout

```
backend/finops/
  config.py         settings and rule thresholds (pydantic-settings)
  model.py          Resource, CostRecord, Finding, TcoReport, Advice, Scan
  aws/session.py    boto3 sessions, adaptive retries, region discovery
  aws/collectors/   one module per service, registered in a pluggable registry
  aws/costs.py      Cost Explorer: usage, forecast, commitments, resource-level costs
  aws/metrics.py    batched CloudWatch GetMetricData
  aws/pricing.py    Price List API with a disk cache and static fallbacks
  aws/native_recs.py Compute Optimizer, Cost Optimization Hub, Trusted Advisor
  rules/            idle, rightsizing, storage, network, containers, database,
                    commitments, governance
  tco.py            the report: totals, breakdowns, ranking
  agent/            provider (Bedrock | Anthropic | OpenAI), prompts, advisor
  pipeline.py       one scan, end to end
  store.py          SQLite scan history
  api.py            FastAPI routes
  cli.py            typer commands
frontend/           Vite + React + TypeScript + Tailwind + Recharts
iam/                read-only policy
```

## Development

```bash
pytest                          # backend suite, no AWS account needed
ruff check . --fix
cd frontend && npm run typecheck && npm run build
```

Collectors are tested against `moto`; Cost Explorer, Pricing, Compute Optimizer, and the
LLM providers are tested against hand-written fakes, because those APIs are either
unsupported by `moto` or not worth the fidelity risk.

## Cost and safety notes

- Read-only: the IAM policy contains no mutating action, and the agent never calls one.
- Cost Explorer requests cost about $0.01 each. Scans are cached; the dashboard never
  triggers AWS calls except when you explicitly start a scan or regenerate advice.
- The SQLite database and the pricing cache live in `data/`, which is gitignored along
  with `.env`.
