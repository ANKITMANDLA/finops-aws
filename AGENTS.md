# Working on this codebase

A FinOps agent for a single AWS account: it inventories resources, prices them, finds waste
with deterministic rules, and presents the result in a React dashboard. This file is the short
version an agent needs before changing anything; the rest of the documentation goes deeper:

| | |
| --- | --- |
| `README.md` | What it does, and how to run it |
| `ARCHITECTURE.md` | How the pieces fit, and the tech stack |
| `DECISIONS.md` | Why each significant choice was made, including the reversals |
| `DEVELOPMENT.md` | Adding a service or a rule, and how the tests are organized |
| `TROUBLESHOOTING.md` | What goes wrong, and the fix |
| `STATE.md` | Where the project stands, and what the last real scan found |

## Setup and the commands that must pass

```bash
python -m venv .venv && .venv/Scripts/activate   # source .venv/bin/activate elsewhere
pip install -e ".[dev]"
cd frontend && npm install && npm run build && cd ..
```

Before calling any change done:

```bash
pytest                                  # 323 tests, no AWS account needed
ruff check backend tests --fix
ruff format backend tests
cd frontend && npx tsc --noEmit
```

`finops scan --dry-run` exercises every collector, rule, and report against a moto-backed mock
account. Use it rather than a real scan while iterating; it needs no credentials and takes
seconds. `finops prices` prints a rate from every pricing lookup and says where each came
from, which is the fastest way to check a new pricing method.

## Rules that are deliberate, not accidental

Each of these looks like an omission until you know the history. Reverting one silently breaks
a number on the dashboard.

**Read-only against AWS, always.** Every AWS call is a `Describe`, `List`, or `Get`, and
`iam/finops-readonly-policy.json` contains nothing else. A new collector adds its permissions
to that file in the same change.

**No hardcoded prices, anywhere.** Rates come from the AWS Price List API, falling back to
AWS's published price list files when `pricing:GetProducts` is denied. There is no table of
default rates and there must not be one: a resource whose rate cannot be resolved is left
unpriced and reported as unpriced. The only exception is `demo.py`, whose fake rates exist so
dry runs produce plausible numbers offline.

**Units are converted where AWS publishes them differently from how it advertises them.**
gp3 throughput is published in GiBps and advertised in MiBps, which `_UNIT_CONVERSIONS` in
`aws/pricing.py` corrects; a rate that looks a thousand times off is almost always a missing
conversion there. Separately, `EFS_BASELINE_MIBPS_PER_GB` encodes the 50 KiB/s per GiB of
Standard storage that EFS includes free, so only provisioned throughput above that baseline is
ever charged or offered as a saving.

**One counted recommendation per resource.** An oversized x86 instance qualifies for both
rightsizing and a Graviton move; showing both puts two rival dollar figures on one node and
inflates the total. The resize wins and the ARM option is described inside it. Where a rule
and an AWS check genuinely disagree, `mark_alternatives` flags the smaller one
`alternative_to` the larger, and it is excluded from every total while staying on screen.

**Savings never exceed what the resource costs.**

**Grouping in the Savings table is presentation only.** Five identical auto-scaling group
workers are one row in the UI and five findings in the store. Totals must be identical either
way, so never collapse findings in the backend to make the UI simpler.

**Denied permissions are recorded, not swallowed.** A collector that cannot read a service
appends a capability note and returns what it has; the scan continues and the Overview page
shows the gap. Failing to authenticate at all is the one case that aborts.

## Layout

```
backend/finops/
  config.py         settings and rule thresholds (pydantic-settings, FINOPS_ prefix)
  model.py          Resource, CostRecord, Finding, TcoReport, Advice, Scan
  aws/collectors/   one module per service, self-registering
  aws/pricing.py    list price lookups, cached on disk; the only source of rates
  aws/price_list.py AWS's published files, for roles denied the pricing API
  aws/metrics.py    batched CloudWatch GetMetricData
  attribution.py    assigns a monthly cost to each resource
  rules/            one module per family, self-registering
  tco.py            totals, breakdowns, ranking
  agent/            LLM providers, advisor, chat agent, MCP hub
  pipeline.py       one scan, end to end
frontend/src/       Vite + React + TypeScript + Tailwind + Recharts
```

Adding a service touches six places: a collector, its IAM permissions, a pricing method, an
attribution function, CloudWatch metric specs if any rule needs them, and tests. Rules are
optional; collecting and pricing a service is useful on its own.

## Conventions

Comments explain a constraint or a trade-off the code cannot state itself — why a threshold is
what it is, why an API is called twice, why a value is excluded. They never narrate what the
next line does, and never explain a change to a reviewer.

Rule `detail` text is read by someone deciding whether to act, so it says what was measured,
what the change is, and what the estimate assumes. Rule and test names read as sentences:
`test_a_shared_transit_gateway_is_not_billed_to_this_account`.

Ruff at 100 columns, Python 3.11, `from __future__ import annotations` everywhere.

Tests never reach AWS: `conftest.py` injects fake credentials and disables `.env` loading, so
a real API key in your environment can never decide whether an assertion holds. Use `moto`
for AWS surfaces and `FakePricingClient` for rates. Some APIs moto does not implement
(`describe_client_vpn_endpoints`, EFS `SizeInBytes`) are handled explicitly rather than
worked around in the collector.

## Skills and rules in `.cursor/`

The multi-step work has a checklist, because the failure is always a step skipped rather than a
step done wrong. `.cursor/skills/` holds four:

| | |
| --- | --- |
| `add-aws-service` | The six places a service touches, from collector to a priced resource |
| `add-finops-rule` | Thresholds, evidence, remediation, and deciding overlap with other rules |
| `finops-change-done` | The four checks, plus what else to run given what changed |
| `finops-code-review` | Reviewing for the defects that leave a wrong number on the dashboard |

`.cursor/rules/` holds the file-scoped conventions that this file states in summary:
`python-style.mdc` for `backend/`, `tests.mdc` for `tests/`, `frontend.mdc` for `frontend/src/`.
They load with the files they cover, so this file stays the short version.

## Local state, none of it in git

`.env` holds the LLM key and AWS profile. `data/finops.db` is the scan history the Trends page
draws on. `.artifacts/` holds the price list cache and scratch output. All are ignored; the
first three matter when moving machines, the last is disposable.

The dashboard is served from `frontend/dist`, which is also ignored, so a fresh clone needs
`npm run build` once before `finops serve` shows anything.
