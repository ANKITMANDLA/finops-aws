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
| `finops prices` | Look up every list price the agent uses and show which source answered |
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

The assistant is docked in the bottom-right corner of every page rather than living on one
of its own; see below.

Starting a scan from the dashboard runs it in the background: the UI keeps serving the
previous scan while the new one collects.

## The assistant

The chat button in the corner of the dashboard opens an assistant that can look things up
rather than recall them. It stays with you as you move between pages, so you can ask about
a finding, go look at it, and carry on asking. It is given two sets of tools:

- **Your scan** (`finops_*`) — the cost breakdown, findings with their evidence and
  remediation, and the inventory with CloudWatch metrics and tags. This is what lets it
  answer with a cluster name and a real number instead of general advice.
- **AWS, over MCP** — the [AWS Knowledge MCP server](https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server)
  for documentation, Well-Architected guidance, and regional availability, and the
  [awslabs pricing server](https://awslabs.github.io/mcp/servers/aws-pricing-mcp-server)
  for list prices and cost comparisons.

So "my EKS clusters look idle, what do they cost and what does AWS charge for a control
plane?" becomes a query against your scan followed by a documentation lookup, and the
answer cites both. Every turn shows which sources it checked; expand the trace to see the
exact calls.

Both servers are read-only, and so are the scan tools, so a question can never change
anything in your account. The Knowledge server is hosted by AWS, needs no credentials, and
is free. The pricing server runs locally under `uvx` (install [uv](https://docs.astral.sh/uv/)
to enable it) and uses the same AWS profile as a scan, so it needs `pricing:GetProducts`,
`pricing:DescribeServices`, and `pricing:GetAttributeValues` — all in the bundled policy.
Anything that will not start is reported as an unavailable source and the conversation
continues without it.

Configure the servers with `FINOPS_MCP_SERVERS` (JSON), turn the whole thing off with
`FINOPS_MCP_ENABLED=false`, and cap how much work one question may do with
`FINOPS_CHAT_MAX_TOOL_CALLS`. Conversations live in the browser tab and are not stored, so
a reload starts fresh.

An answer with a table or a walked-through calculation needs more room than a one-line
reply, so the panel resizes: drag its top-left corner, or either of the top and left edges
for one dimension at a time. The size is kept per browser and survives a reload;
double-click the corner grip to go back to the default.

Tool use needs a provider that supports it: Bedrock, Anthropic, OpenAI, or Gemini. Child
process output from stdio servers goes to `mcp.log` beside the database.

## Where the numbers come from

This matters more than any feature, so the agent is explicit about it everywhere.

- **The headline total is the bill.** `ce:GetCostAndUsage` with the `AmortizedCost` metric,
  grouped by service, region, and usage type, filtered to exclude credits, refunds, and
  tax. Per-resource estimates are never summed into that headline figure.
- **Cost of ownership from AWS pricing is a second, clearly separate figure.** Overview and
  Savings each carry a "Cost of ownership from AWS pricing" frame: the inventory priced per
  resource from the AWS Price List, with how many resources carry a price and what the
  estate would list at after the identified changes. It is the only cost picture available
  to a role denied Cost Explorer, and unlike the bill it can be attributed to a resource.
  It excludes commitments, credits, negotiated discounts, and usage-priced services such as
  Lambda and S3, so it reads higher than an invoice covered by Savings Plans. The two never
  masquerade as each other.
- **Per-resource cost is best-effort.** `GetCostAndUsageWithResources` gives real billed
  cost per resource, but it is opt-in and retains only 14 days. Without it, the agent
  estimates from the Price List API. Every figure carries a `cost_basis` — billed,
  allocated, list-price estimate, AWS recommendation, or heuristic — and the UI shows it.
- **No price is ever hardcoded.** Every rate comes from AWS, for the resource's own region,
  matched to the exact usage type the charge is published under, and cached in
  `data/pricing-cache.json`. There is no built-in table of rates to fall back on, because a
  stale rate is indistinguishable from a real one once it reaches a dashboard. If AWS does
  not supply a rate the resource stays unpriced, the UI says "Not priced", and the scan
  records why.

  Rates are looked up two ways, in order:

  1. **`pricing:GetProducts`**, the Price List Query API. This is public data and needs no
     Cost Explorer access at all, so a role that cannot read your bill can still price your
     resources.
  2. **The price list files AWS publishes without authentication**, used when the API is
     denied or unreachable. Same rates from the same source, reached over plain HTTPS with
     no credentials. Each service and region is downloaded once, reduced to the on-demand
     charges a scan asks about, and cached under `data/price-list/`. Reading stops before
     the reserved instance terms, so even EC2 — a 450MB file — takes about ten seconds.
     A scan that used this route says so in its capability notes. Turn it off with
     `FINOPS_PUBLIC_PRICE_LIST=false` if you would rather see blanks than wait for a
     download.

  `finops prices --region eu-west-1` prints every rate the agent looks up, which source
  answered, and what is missing if anything is. It is the quickest way to confirm the
  pricing path works.
- **Savings come from rules, not from the model.** Each finding is produced by a
  deterministic rule with cited evidence (metric values, resource state, configuration).
  The LLM only ever sees aggregates and the ranked findings, and is told not to invent
  dollar figures.
- **No double counting.** Our rules overlap with Compute Optimizer, Cost Optimization Hub,
  and Trusted Advisor. Findings are keyed by `(action, resource)` and merged, so the same
  change is never counted twice.
- **The money always matches the steps.** When two sources describe one problem they often
  price different fixes: Trusted Advisor's low-utilization check quotes what you save by
  stopping an instance, while our rightsizing rule quotes what you save by halving it. The
  finding that supplies the commands supplies the figure, and the other source's number is
  shown as evidence of what it assumes. A saving is never larger than the change on screen
  would deliver.
- **Low CPU is not idle, and nothing falls between the rules.** An EKS node forwarding
  gigabytes a day sits near zero percent CPU while being entirely load bearing. Switching an
  instance off is only the better finding when it is quiet on the network as well, so the
  rightsizing rule stands aside for the idle rule on exactly the instances the idle rule will
  take, and resizes the rest. Instances used to fall through the gap between the two and end
  up with no recommendation of ours, leaving an AWS check's "stop it" figure — priced at close
  to the whole instance, with no commands — as the only claim on the node.
- **One recommendation per resource.** An oversized x86 instance qualifies for both
  rightsizing and a Graviton move, and showing both leaves two rival figures on one node.
  The resize is the recommendation; the ARM option is described inside it as the next step,
  with what it would save on top. Graviton is raised on its own only for instances that are
  already the right size. Where two claims on one resource do survive — a rule and an AWS
  check proposing different actions — the largest counts and the rest stay on screen marked
  `alternative`, with their own figures, excluded from every total.
- **One row per decision, not per node.** The nodes in an auto-scaling group share a `Name`
  tag, so five identical workers produce five findings whose titles and figures are word for
  word the same. The dashboard shows them as one row — the change, the fleet total, and the
  figure per node — expanding to every instance it covers. Nothing is dropped or merged in
  the store: the finding per resource is still there for attribution and for the API. This is
  presentation only, so the total is unchanged whether rows are grouped or not.
- **Savings never exceed the bill**, however enthusiastic the rules get.
- **Ranking is value against effort.** A gp2-to-gp3 switch you can do this afternoon ranks
  above a Graviton migration with a bigger headline number.

## What it looks at

**Compute** — EC2 instances (idle, stopped but still paying for EBS, previous generation,
Graviton candidates, underutilized), Auto Scaling groups, Lambda (ARM candidates, unused
provisioned concurrency).
**Storage** — unattached EBS volumes, gp2-to-gp3, over-provisioned IOPS, stale snapshots
and unused AMIs, S3 lifecycle and Intelligent-Tiering gaps, incomplete multipart uploads,
versioning without expiry, EFS file systems nothing mounts, cold EFS data never tiered out
of Standard, EFS throughput provisioned far above the busiest hour, log groups with
unbounded retention.
**Network** — unassociated Elastic IPs, idle NAT Gateways, load balancers with no healthy
targets or negligible traffic, interface VPC endpoints nothing calls, transit gateway
attachments carrying no traffic, site-to-site VPN connections whose tunnels are down,
Client VPN endpoints paying per associated subnet for access nobody uses.
**Keys, secrets, and certificates** — customer managed KMS keys (AWS managed ones are free
and are left out), keys still billing through their deletion window, Secrets Manager
secrets, ACM certificates, and private certificate authorities, which bill every month
until deleted whether they are enabled or not.
**Messaging and registries** — SNS topics, SQS queues, ECR repositories, and CloudWatch
alarms. Neither SNS nor SQS has a standing charge, so idle ones genuinely cost nothing;
they are priced from measured request volume rather than assumed to be free.
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

Cost Explorer is the largest of those gaps, because without it there is no bill to divide
up. In its place the estate is priced per resource from the AWS Price List, so the cost of
ownership figure, the split by service, and the split by region all still appear — labelled
as list prices, since they know nothing of your commitments or discounts.

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
| `gemini` | `FINOPS_GEMINI_API_KEY` | AI Studio key; defaults to `gemini-3.6-flash`, override with `FINOPS_GEMINI_MODEL` |
| `none` | — | Deterministic summary assembled from the findings, and no assistant |

On Gemini 3 and later, thought tokens are billed against the output budget, so unbounded
reasoning truncates the advice mid-JSON. Thinking is capped at `low` by default; raise it
with `FINOPS_GEMINI_THINKING_LEVEL` (and `FINOPS_LLM_MAX_OUTPUT_TOKENS` alongside it) if
you want deeper analysis.

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
  aws/pricing.py    list price lookups, cached on disk; the only source of rates
  aws/price_list.py AWS's published price list files, for roles denied the pricing API
  aws/native_recs.py Compute Optimizer, Cost Optimization Hub, Trusted Advisor
  rules/            idle, rightsizing, storage, efs, network, connectivity,
                    containers, database, commitments, governance
  tco.py            the report: totals, breakdowns, ranking
  agent/            providers (Bedrock | Anthropic | OpenAI | Gemini) with tool calling,
                    prompts, advisor, chat agent, MCP hub, scan lookup tools
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
