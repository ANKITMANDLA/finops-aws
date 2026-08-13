# Development

Setting up, the loop you will actually work in, how to add a service or a rule, and how the
tests are organized. `AGENTS.md` is the condensed version of the rules; this is the practical
guide. `TROUBLESHOOTING.md` covers what goes wrong.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"

cd frontend && npm install && npm run build && cd ..
```

The frontend build matters even if you never touch TypeScript: `frontend/dist` is gitignored,
and `finops serve` has nothing to serve until it exists.

Python 3.11 or newer. Node 20 or newer for the frontend.

## The loop

Work against the mock account, not AWS:

```bash
finops scan --dry-run --no-advice          # seconds, no credentials, no charges
finops scan --dry-run -o .artifacts/demo.json    # same, plus the full result as JSON
```

`--dry-run` seeds a realistic estate through moto and runs every collector, rule, and report
against it. Skipping the advisor with `--no-advice` keeps the loop fast and free.

Useful narrowing while iterating:

```bash
finops scan --dry-run --collectors efs,ec2 --no-advice
finops scan --dry-run --rules efs.no_lifecycle_policy --no-advice
finops scan --dry-run --skip-rules governance.untagged_resources --no-native --no-save
```

Then, when a real scan is warranted:

```bash
finops whoami                              # confirm identity and regions first
finops scan -o .artifacts/real.json
finops serve --port 8099
```

`--no-save` keeps experiments out of the store. Use it freely; a store full of near-duplicate
scans from one afternoon is the usual outcome otherwise, and `finops prune` then has to clean
up.

## Checks that must pass

```bash
pytest                                     # 323 tests, no AWS account needed
ruff check backend tests --fix
ruff format backend tests
cd frontend && npx tsc --noEmit
```

Ruff is configured at 100 columns for Python 3.11 with `E`, `F`, `I`, `UP`, `B`, `W` selected.
Run the formatter through the project config rather than ad hoc; an ad-hoc Prettier run on the
frontend will rewrap at 80 columns and fight the existing style.

## Adding a service

Six places, in this order. EFS and the connectivity services are the most recent worked
examples if you want something to copy.

**1. A collector** in `backend/finops/aws/collectors/`, subclassing `Collector` with
`@register`, then listed in `_COLLECTOR_MODULES` in that package's `__init__.py`. It needs a
`key`, a `service` label, and `collect(ctx, region) -> list[Resource]`. Use the helpers in
`base.py`: `paginate` for any list call, `tags_to_dict` to normalize AWS's several tag shapes,
`synthesize_arn` for services whose describe calls omit one. Put anything a rule or a price
lookup will need into `attributes`. Raising is fine — the runner records the failure as a
capability note and the rest of the scan continues.

**2. IAM permissions** in `iam/finops-readonly-policy.json`, in the same change. Every action
must be a `Describe`, `List`, or `Get`.

**3. A price lookup** in `aws/pricing.py`, returning a `Quote` or `None`. Never write a rate
down. Verify it with `finops prices`, which prints one rate per lookup and says whether it came
from the API or the published files, then add your lookup to the list in that command so the
next person can check it too.

**4. Cost attribution** in `attribution.py`: a function keyed by `resource_type` in
`_ESTIMATORS` that turns attributes into a monthly figure. Return `None` when the inputs are
missing rather than assuming zero — an ECR repository whose size could not be read is not the
same as an empty one.

**5. Metrics**, if a rule needs utilization. Declare a `MetricSpec` list in `aws/metrics.py`
keyed by resource type in `_SPEC_BUILDERS`, and derive whatever the rule wants in `_derive`.
Rules never call CloudWatch themselves.

**6. Tests.** See below.

A rule is optional. Collecting and pricing a service is useful on its own — it shows up in
inventory and in the cost of ownership breakdown.

## Adding a rule

Subclass `Rule` in the appropriate `rules/` module with `@register`, define `id`, `category`,
and `title`, and implement `evaluate(ctx) -> Iterable[Finding]`. Add the module to
`_RULE_MODULES` if it is new.

Rules never call AWS. Everything is on the `RuleContext`: `of_type()` for resources by type,
`age_days()`, `monthly_cost()`, `ctx.pricing` for rates, `ctx.thresholds` for tunables, plus
pre-computed sets like `image_ids_in_use` and `snapshot_ids_backing_images` that exist to stop
rules recommending the deletion of something another resource depends on.

Use `finding_for()` to build the finding. Things that are easy to get wrong:

- Every threshold belongs in `config.Thresholds`, not inline, so it can be tuned with
  `FINOPS_THRESHOLDS__*`.
- `detail` is read by someone deciding whether to act. Say what was measured, what the change
  is, and what the estimate assumes.
- `Evidence` carries the measurements; `Remediation` carries the summary, the CLI, Terraform
  where it makes sense, and the console path.
- Set `confidence`, `effort`, `risk`, and `rollback_possible` honestly — ranking uses them, and
  `risk="high"` with `rollback_possible=False` is how a destructive action gets flagged.
- Return early rather than yielding a finding worth pennies; the merge drops anything under
  `min_monthly_savings_usd` anyway.
- If your rule can compete with an existing one on the same resource, decide which wins and
  make the loser defer. Two findings on one resource is a bug, not a feature.

## Tests

18 files, 323 tests, no network and no credentials. The layout mirrors the pipeline:
`test_collectors.py`, `test_pricing_attribution.py`, `test_metrics.py`, `test_rules.py`,
`test_tco.py`, `test_native_recs.py`, `test_price_list.py`, `test_store.py`, `test_api.py`,
`test_pipeline.py`, `test_advisor.py`, `test_chat.py`, `test_cli.py`, `test_demo.py`,
`test_costs.py`.

Three fixtures carry most of the weight. `conftest.py` injects fake AWS credentials and
disables `.env` loading, both autouse, so nothing in your environment can decide whether an
assertion holds. `factories.py` builds `Resource` and `Finding` objects with sensible defaults.
`fakes.py` provides `FakePricingClient` and friends, so a rule test states the rates it assumes
instead of reaching for the network.

Collector tests use `moto` against a seeded account. Rule tests construct a `RuleContext`
directly from factory resources — no AWS mocking needed, because rules never call AWS.

Test names read as sentences, which is deliberate: they are the closest thing to a
specification for a rule's edge cases.

```python
def test_a_shared_transit_gateway_is_not_billed_to_this_account(...)
def test_a_registry_whose_size_could_not_be_read_stays_unpriced(...)
```

Run a subset while iterating:

```bash
pytest tests/test_rules.py -k efs
pytest tests/test_pricing_attribution.py -x -q
```

## Frontend

```bash
cd frontend
npm run dev            # Vite on :5173, proxying /api to 127.0.0.1:8000
npm run build          # what finops serve hosts
npx tsc --noEmit
```

The dev proxy targets port 8000, which is `finops serve`'s default. If you are serving on
another port, the dev server will not find the API until you change the target in
`vite.config.ts`.

Five views over one `ScanContext`, which owns the selected scan, health, job status, and
polling. Add data by extending `lib/api.ts` and mirroring the Pydantic model in `lib/types.ts`
— optional fields for anything a stored older scan will not have, or the Trends page breaks on
history. Build from the primitives in `components/ui.tsx` rather than fresh markup.
`lib/format.ts` holds the value formatting, including the metric-key patterns that decide
whether a number is shown as GB, MiB/s, or a percentage.

## Verifying against a real account

Two commands answer most questions:

```bash
finops prices --region us-west-2      # every rate, and where it came from
finops report latest                  # the stored report, in the terminal
```

For anything deeper, write a throwaway script under `.artifacts/` and run it with the venv
Python. That directory is gitignored and exists for exactly this. Scans written with
`-o .artifacts/whatever.json` are the full `Scan` model as JSON, so a dozen lines of Python
will answer "which types are unpriced" or "which findings overlap" faster than clicking around
the dashboard.

Playwright drives the UI when a change needs a visual check. Screenshots go to `.artifacts/`;
use absolute paths for the output, since a relative one resolves against the script's directory
and quietly writes somewhere unexpected.

## Committing

Nothing is committed unless you ask for it. The tree should be clean and all four checks green
before it is. Local state — `.env`, `data/`, `.artifacts/`, `.venv/`, `node_modules/`,
`frontend/dist/` — is gitignored; `.env.example` is the one env file that belongs in the repo,
and it should gain an entry whenever a setting is added.
