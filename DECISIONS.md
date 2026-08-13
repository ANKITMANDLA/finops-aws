# Decisions

The choices that shaped this project, why each was made, and what it cost. Several were
reversals of an earlier decision that produced wrong numbers on a real account; those are the
most useful ones here, because the wrong version usually looks more reasonable than the right
one until you see what it does to a report.

Each entry is the decision, the reasoning, and the consequence you would notice if someone
undid it.

---

## Scope and shape

### Single account, local-first, read-only

Everything runs on your machine against your credentials, and every AWS call is a `Describe`,
`List`, or `Get`.

Cost tooling is only useful if people will point it at production, and they will not do that
with something that writes. Read-only is also what allows the IAM policy to be reviewed in a
minute rather than argued over for a week. Single account keeps the store schema and every view
simple; multi-account rollup would touch both.

The cost is real: no organization-wide view, and no automated remediation. Findings carry the
CLI and Terraform to make the change, and a human runs it.

### A hybrid of rules and an LLM, with a firm boundary

Deterministic rules produce every number. The LLM produces prose and architectural suggestions
and is never allowed to originate a figure.

An LLM asked to compute savings will confidently invent them, and a number you cannot
reproduce is worse than no number. Rules are reproducible, testable, and can show the
arithmetic. But rules only see one resource at a time, so anything that requires looking across
the estate — "these four clusters could be one" — needs the model.

Undo this and the dashboard's totals stop being auditable.

### An LLM is optional, not required

With `FINOPS_LLM_PROVIDER=none` a `NullProvider` takes over and advice is assembled
deterministically from the findings themselves.

A scan that fails because an API key is missing would make the whole tool contingent on a
third-party account. The advisor is the one part that is genuinely nice-to-have.

### SQLite rather than Postgres or JSON files

Scan history and trends need real queries, filtering, and sorting, but a local single-account
tool should not require a server to be running. Four tables, indexes on the access patterns the
dashboard uses, JSON columns for large nested structures, and a schema version in
`PRAGMA user_version` so upgrades migrate rather than asking you to delete history.

### Registries rather than configuration

Collectors and rules self-register with a decorator and are listed only in their package's
`__init__`. The pipeline iterates and never names a service.

This is why coverage grew from 17 services to 28, and rules from the low thirties to 44,
without `pipeline.py` changing at all.

### Scans run in a background thread, one at a time

A scan takes minutes and costs money in Cost Explorer requests, so `POST /api/scans` starts a
job, returns `202`, and streams progress for polling. A lock prevents a second concurrent scan.

Running it inline would freeze the dashboard for minutes; allowing two would double the API
bill on an impatient double-click.

---

## Cost and pricing

### No hardcoded prices, anywhere

This is the decision most worth defending, because it was made twice.

The first version shipped a `FALLBACK_PRICES` table so that a resource could always be given a
number. On a real account it quietly produced confident, wrong figures: a rate written down
months ago, applied to a region it was never measured in, and presented identically to a rate
fetched live. Nothing in the UI distinguished the two.

Now rates come from the AWS Price List API, falling back to the price list files AWS publishes
without authentication, and a resource whose rate cannot be resolved stays `None` and is
reported as unpriced. The table is gone and must not come back. The only exception is
`demo.py`, whose fake rates exist so dry runs work offline and which labels every figure
synthetic.

Consequence: 333 of 956 resources are unpriced on the current account. That is the honest
answer, and the dashboard says "unpriced" rather than "$0".

### Public price list files as the fallback, not an estimate

When `pricing:GetProducts` is denied — as it is on the account this was built against — the
same rates are read from AWS's published price list files, which need no credentials at all.

The alternative was to give up and show nothing, which would have made the tool useless on
exactly the restricted roles that most need it. These are AWS's own published rates, so the
numbers are identical to the API's; only the delivery mechanism differs. `finops prices` shows
which source each rate came from.

### Cost Explorer is the authority; list prices are the fallback for the whole report

Per-resource cost prefers real billed cost from `GetCostAndUsageWithResources`, then a
list-price estimate, then nothing. The account total always comes from Cost Explorer rather
than from summing per-resource estimates, so incomplete attribution understates detail without
corrupting the total.

When Cost Explorer is denied entirely there is no bill to divide, so the estate is priced per
resource from the Price List and the report is labelled as list prices throughout. Leaving the
Overview blank was the alternative, and it made the tool look broken rather than constrained.

List prices know nothing of commitments or negotiated discounts, so they read high for anything
covered by a Savings Plan. Saying so in the UI is part of the decision.

### Units are converted where AWS publishes them differently from how it advertises them

gp3 throughput showed as `$40.96` instead of `$0.04` because AWS publishes it per GiBps while
advertising it per MiBps. `_UNIT_CONVERSIONS` handles the general case.

A rate that is off by exactly 1024 or 1,000,000 is almost always this.

---

## Findings

### One counted recommendation per resource

An oversized x86 instance qualifies for both rightsizing and a Graviton migration. Showing both
put two rival dollar figures on one node and inflated the total.

Now the resize is the recommendation and the ARM option is described inside it, with what it
would save on top. Graviton is raised on its own only for instances that are already correctly
sized. Where two claims genuinely survive — one of our rules and an AWS check proposing
different actions — the largest counts and the rest stay visible marked `alternative`, with
their own figures, excluded from every total.

A user found this the hard way: instance `i-010434df3f395eac0` appeared with $1,023 of savings
from a Trusted Advisor termination recommendation and again inside a Graviton group worth $223.

### Savings come from whichever source has executable remediation

When our rule and an AWS check propose the same action, the merge keeps the finding whose
remediation you can actually run, and records the other source's estimate as evidence rather
than replacing the figure with it.

Earlier the highest number won, which meant a finding could show a dollar figure from AWS
alongside CLI commands that would achieve something different. The evidence line
("Trusted Advisor estimates $29.41/month for its own version of this change") exists so the
discrepancy is visible rather than hidden.

### Rules must not fall through the gap between each other

Rightsizing used to stand aside for the idle rule on any low-CPU instance, and the idle rule
then declined instances that were busy on the network. Those instances ended up with no
recommendation from us at all, leaving an AWS check's "stop it" figure — priced at nearly the
whole instance, with no commands attached — as the only claim on the node.

Rightsizing now defers only when `looks_idle` agrees the idle rule will actually take it.

### Grouping is presentation, never accounting

Five identical auto-scaling group workers produce five findings whose titles and figures are
word for word identical. The dashboard shows one row that expands; the store keeps all five.

Collapsing them in the backend would have been less code, and it would have broken attribution
and made totals depend on how the UI chose to display them.

### Savings never exceed what the resource costs

However enthusiastic a rule gets.

### Ranking is value against effort

A gp2-to-gp3 switch you can do this afternoon ranks above a Graviton migration with a bigger
headline number, because the ranking exists to answer "what should I do next", not "what is the
biggest number".

---

## Degradation and honesty

### Denied permissions are recorded, not swallowed

A collector that cannot read a service appends a capability note and returns what it has. The
scan continues and the Overview page lists every gap. Failing to authenticate at all is the one
case that aborts.

Without the abort, a bad profile produces a "successful" scan of an apparently empty account:
every collector reports AccessDenied and the report reads as zero spend and zero waste. Without
the notes, a missing Cost Optimization Hub enrolment silently removes a whole class of findings
and the report still looks complete.

### Absent is distinguished from unmeasured

An empty result and a denied API are different facts and are reported differently. The current
account genuinely has no NAT gateways or VPC endpoints, and genuinely cannot enumerate SQS
queues. Both would otherwise read as "nothing found".

### Ownership is checked before anything is billed to you

Transit gateways shared in through Resource Access Manager appear in `DescribeTransitGateways`
but their attachment charges land on the owner's bill. Charging you $36.50/month each for 13
gateways belonging to another account would have added roughly $475/month of fiction.

Collectors record `owned_by_this_account` and price accordingly.

### Ownership tags are matched by token, not by substring

`has_ownership_tag` tokenizes tag keys on non-alphanumeric boundaries, so
`company:cost-center` counts as an ownership tag.

Before this, namespaced tags were missed and untagged spend read $112k instead of $2.4k on the
same account — the kind of error that destroys trust in every other number on the page.

---

## Interface

### The chat assistant is a widget, not a page

A separate page meant leaving whatever you were looking at in order to ask about it. The widget
floats over every view, and is draggable and resizable with the size persisted, because a fixed
panel is either too small for a table or too large for a sentence.

### Model output is stripped of LaTeX

Models write cost arithmetic as `$$\text{Cost} = S \times $0.10$$` regardless of how firmly the
prompt forbids it. The dashboard renders markdown, not math, so the reader saw it verbatim.

The prompt asks for plain text and a flattener removes the handful of constructs that show up
in arithmetic, leaving anything else alone rather than mangling prose. Both halves are needed:
the prompt alone was not enough.

### MCP servers are declared by the app, not by the IDE

The chat assistant's servers live in `config.py`, so it behaves identically whether launched
from Cursor, Claude Code, a terminal, or nothing at all. Reading an editor's MCP configuration
would have tied a product feature to a development tool.

---

## Testing

### Tests never reach AWS, and never read your `.env`

`conftest.py` injects fake credentials and disables `.env` loading. `moto` mocks AWS surfaces
and `FakePricingClient` supplies rates.

The `.env` part was a real failure: setting `FINOPS_LLM_PROVIDER=gemini` locally broke the
advisor tests, because a real API key in the environment was deciding whether assertions held.

### A mock account, not just mocked calls

`demo.py` seeds a realistic estate through moto so `finops scan --dry-run` exercises every
collector, rule, report, and dashboard with no credentials. Where moto does not implement
something — `describe_client_vpn_endpoints`, EFS `SizeInBytes` — the gap is handled explicitly
rather than worked around inside the collector.

This is what makes it possible to develop the whole pipeline offline and to demonstrate it to
someone before they will give you access to anything.
