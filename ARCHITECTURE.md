# Architecture

How the pieces fit, what each one is responsible for, and which technologies were chosen and
why. `README.md` covers what the agent does, `STATE.md` where it currently stands, and
`AGENTS.md` the conventions for changing it.

## Shape of the system

A local-first, single-account, read-only pipeline behind a FastAPI server, with a React
dashboard on top. Nothing is hosted, nothing is multi-tenant, and there is no message broker
or external database — a scan is one Python process reading AWS and writing SQLite.

```
                      ┌──────────────────────── AWS (read only) ───────────────────────┐
                      │ EC2 EBS EKS RDS S3 Lambda EFS ELB KMS SNS SQS ECR …            │
                      │ Cost Explorer · CloudWatch · Price List · Compute Optimizer     │
                      │ Cost Optimization Hub · Trusted Advisor                         │
                      └───────────────────────────────┬────────────────────────────────┘
                                                      │ boto3, adaptive retries
   ┌──────────────────────────────────────────────────▼─────────────────────────────────┐
   │ pipeline.run_scan — eight stages, each degrading independently                      │
   │                                                                                     │
   │  inventory ─► costs ─► metrics ─► pricing ─► native ─► rules ─► tco ─► advice        │
   │  28 coll.    Cost Expl. CloudWatch  attribution  AWS recs  44 rules  report  LLM     │
   │      │           │          │           │           │         │        │      │      │
   │      └───────────┴──────────┴───── NoteCollector: what was denied, and why ──┘      │
   └──────────────────────────────────────────────────┬─────────────────────────────────┘
                                                      │ one Scan object
                                          ┌───────────▼───────────┐
                                          │ SQLite (store.py)     │
                                          │ scans resources        │
                                          │ findings costs         │
                                          └───────────┬───────────┘
                                    reads only        │
   ┌──────────────────────────────────────────────────▼─────────────────────────────────┐
   │ FastAPI (api.py) — 20 JSON routes + SPA hosting   │  jobs.py: one scan at a time   │
   └──────────────────────────────────────────────────┬─────────────────────────────────┘
                                                      │ fetch
   ┌──────────────────────────────────────────────────▼─────────────────────────────────┐
   │ React SPA — Overview · Savings · Inventory · Architecture · Trends + chat widget    │
   └────────────────────────────────────────────────────────────────────────────────────┘
```

The CLI (`cli.py`) and the API are two front ends over the same `run_scan`. Neither has logic
of its own beyond presentation.

## The scan pipeline

`pipeline.py` is 173 lines and deliberately linear: eight named stages, in order, each one
handed the output of the last. The stage names are exported as `STAGES` and surface in the UI
as progress. The rule throughout is that a denied API costs you that stage's data, not the
scan — except failing to authenticate at all, which aborts immediately so that a bad profile
can never produce a confident report on an empty account.

**inventory** runs all 28 collectors across all target regions in a thread pool
(`max_workers`, default 8), one task per collector-region pair. Failures are isolated per pair
inside a `graceful()` context manager, so one denied service or one broken region cannot empty
the inventory. Collectors self-register with a decorator and are looked up in `REGISTRY`, so
adding a service means adding a module, not editing a dispatcher.

**costs** queries Cost Explorer for the authoritative bill: daily and monthly totals, service
and region breakdowns, forecast, commitment coverage and utilization, and — when the account
has resource-level data enabled — per-resource costs for the last 14 days.

**metrics** batches CloudWatch `GetMetricData` queries for the resources whose rules need
utilization. Specs are declared per resource type in `aws/metrics.py`, so a rule asking for a
new metric declares it rather than issuing its own API call.

**pricing** assigns each resource a monthly cost through a strict precedence: real billed cost
if Cost Explorer gave resource-level detail, then a list-price estimate from the Price List
API, then nothing. That third branch is the important one — an unpriceable resource stays
`None` and is reported as unpriced rather than being handed a fabricated figure.

**native** reads AWS's own verdicts from Compute Optimizer, Cost Optimization Hub, and Trusted
Advisor, normalizing three quite different response shapes into the same `Finding` model.

**rules** evaluates all 44 rules against the inventory, then merges. Merging does three things:
de-duplicates AWS-native findings against ours on `(resource_arn, action_type)` keeping the one
with executable remediation, marks competing findings on the same resource as
`alternative_to` the largest so nothing is counted twice, and drops findings below the minimum
savings threshold.

**tco** assembles the report: totals, breakdowns by service, region, category and effort, the
list-price breakdowns used when Cost Explorer is unavailable, and the finding ranking, which
sorts by value against effort rather than by raw dollars.

**advice** sends aggregated findings and an inventory summary to an LLM for the architectural
changes no per-resource rule can see. It never sees raw resource data, only the summary.

## Backend modules

| Module | Lines | Responsibility |
| --- | --- | --- |
| `aws/pricing.py` | 752 | List price lookups, disk-cached; the only source of rates |
| `aws/native_recs.py` | 717 | Compute Optimizer, Cost Optimization Hub, Trusted Advisor |
| `aws/metrics.py` | 570 | Declarative CloudWatch metric specs, batched |
| `aws/costs.py` | 407 | Cost Explorer: usage, forecast, commitments, resource-level |
| `aws/price_list.py` | 306 | AWS's published price files, for roles denied the pricing API |
| `aws/session.py` | 126 | boto3 sessions, client cache, retry policy, region discovery |
| `aws/collectors/` | ~2,200 | 13 modules, 28 collectors, self-registering |
| `rules/` | ~3,300 | 10 modules, 44 rules, self-registering |
| `attribution.py` | 404 | Cost per resource, with the precedence above |
| `tco.py` | 302 | The report: totals, breakdowns, ranking |
| `store.py` | 606 | SQLite persistence, schema migration, pruning |
| `api.py` | 372 | FastAPI routes and SPA hosting |
| `agent/` | ~1,600 | LLM providers, advisor, chat agent, MCP hub, scan tools |
| `demo.py` | 895 | A moto-backed mock account for offline dry runs |
| `model.py` | 292 | Every shape in the system, as Pydantic models |

Two registries carry the extensibility. Collectors and rules both self-register with a
`@register` decorator and are listed in their package's `__init__`, which means the pipeline
never names a service or a rule — it iterates. That is why adding EFS, or later transit
gateways and KMS, changed no orchestration code.

## Data model

`model.py` holds every shape, as Pydantic v2 models, and both the store and the API reuse them
rather than defining parallel DTOs:

**Resource** — ARN, type, service, region, account, state, tags, creation time, a free-form
`attributes` dict for service-specific detail, a `metrics` dict filled by the metrics stage,
and `monthly_cost` with a `cost_basis` recording where that number came from.

**Finding** — the unit of waste: rule id, title, category, an action type, estimated monthly
savings, `Evidence` items (what was measured), a `Remediation` (summary, CLI, Terraform,
console path), plus confidence, effort, risk, whether it can be rolled back, and
`alternative_to` when it is a competing recommendation excluded from totals.

**TcoReport** — totals, run rate, forecast, every breakdown, and the ranked findings.

**Scan** — everything above plus capability notes, the advice, and timings. One `Scan` is one
row in `scans` with its children in three other tables.

## Storage

SQLite, one file, four tables — `scans`, `resources`, `findings`, `costs` — with indexes on the
access patterns the dashboard actually uses (`scan_id` plus service, region, cost, category,
savings). `SCHEMA_VERSION` is tracked in `PRAGMA user_version` and migrations run on open, so
upgrading never asks you to delete history.

The choice is deliberate: scan history and trends need a real query engine, but a local
single-account tool should not require a server. Large nested structures are stored as JSON
columns; the fields worth filtering and sorting on are promoted to real columns.

## API and the dashboard

FastAPI serves 20 JSON routes under `/api` and hosts the built SPA at `/`, with a catch-all so
client-side routes survive a refresh. Reads come only from SQLite, never from AWS, which is
what keeps the dashboard instant.

Scans are the exception, and `jobs.py` exists for it: a scan takes minutes and costs money in
Cost Explorer requests, so `POST /api/scans` starts one background thread, returns `202`, and
streams stage progress into memory for `GET /api/scans/status` to poll. At most one scan runs
at a time, enforced with a lock, so an impatient double-click cannot double the bill.

The frontend is five views over one `ScanContext` that owns the selected scan, health, job
status, and polling. `lib/api.ts` is a thin typed fetch wrapper, `lib/types.ts` mirrors the
Pydantic models, `lib/groups.ts` holds the finding-grouping logic that collapses identical
auto-scaling group findings into one row for display only, and `components/ui.tsx` is the small
primitive set (Card, Badge, Table, Stat, Spinner, EmptyState) everything else is built from.

## The LLM layer

Four providers behind one small interface in `agent/provider.py`: Amazon Bedrock (default,
because it reuses the AWS credentials already in hand), Anthropic, OpenAI, and Gemini, selected
with `FINOPS_LLM_PROVIDER`. The interface is two methods — `complete` for the advisor and
`converse` for tool-using chat — plus a `NullProvider` so that with no LLM configured a scan
still produces advice, assembled deterministically from the findings.

The chat assistant (`agent/chat.py`) gets two classes of tools. Local `finops_*` tools read the
stored scan, so it can name the volume or cluster it is discussing. MCP tools reach outward:
AWS Knowledge over HTTP for documentation, Well-Architected guidance, and regional
availability, and the awslabs pricing server over stdio for list prices and what-if
comparisons. `agent/mcp_hub.py` connects them per turn, skips any server that will not start
inside the timeout, and exposes their tools alongside the local ones. Servers are declared in
`config.py`, not in any IDE's settings, so the assistant behaves identically however the app is
launched.

Two details are load-bearing. Chat is read-only by construction — every tool reads SQLite or a
public AWS API — so a confused turn wastes tokens and nothing else. And model output passes
through a LaTeX flattener, because models write cost arithmetic as `$$\text{Cost} = S \times
$0.10$$` no matter how firmly the prompt forbids it, and the dashboard renders markdown rather
than math.

## Tech stack

**Backend** — Python 3.11+. `boto3`/`botocore` for AWS with adaptive retries and a per-service
client cache. `pydantic` v2 for every model and `pydantic-settings` for `FINOPS_`-prefixed
configuration including nested rule thresholds. `FastAPI` and `uvicorn` for the server, `typer`
and `rich` for the CLI, `httpx` for the LLM HTTP providers, `mcp` for the tool protocol,
`anyio` to bridge async MCP work into the sync pipeline, and the standard library's `sqlite3`
and `concurrent.futures` rather than an ORM or a task queue.

**Frontend** — React 19 with TypeScript, Vite for build and dev server, Tailwind v4 via the
Vite plugin, `react-router-dom` v7 for routing, `recharts` for charts, and
`react-markdown` with `remark-gfm` for LLM output.

**Development** — `pytest` with `moto` mocking AWS, so the whole 323-test suite runs with no
credentials and no network. `ruff` for lint and format at 100 columns. `tsc --noEmit` for the
frontend. Playwright drives the UI for visual checks.

## Why it is built this way

**Local-first and read-only.** Everything runs on your machine against your credentials, and
every AWS call is a `Describe`, `List`, or `Get`. No data leaves except the aggregated summary
sent to whichever LLM you configured. This is what makes the tool safe to point at production
on the first afternoon.

**Rules and LLM do different jobs.** Deterministic rules produce every number, so savings are
reproducible and auditable, and the LLM never invents a figure. The LLM is given only what
rules cannot see: patterns across the estate, and architectural change rather than
per-resource change.

**Degrade, never guess.** Cost Explorer, Compute Optimizer, Cost Optimization Hub, Trusted
Advisor, resource-level billing, and even the Price List API are each optional. Every one that
is missing is recorded as a capability note, shown on the Overview page, and worked around if
there is an honest way to do so — list prices in place of the bill, published price files in
place of the pricing API — or left blank if there is not.

**Registries over configuration.** Collectors and rules are plugins in all but name, which is
why the service count grew from 17 to 28 without the pipeline changing.

**Presentation is separate from accounting.** Grouping, ranking, and labelling happen in the UI
and the report; the store always holds one finding per resource. Totals are therefore identical
however the dashboard chooses to display them.
