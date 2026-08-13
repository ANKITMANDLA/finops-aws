---
name: add-aws-service
description: Add or extend AWS service coverage in this FinOps agent — a collector, its IAM permissions, a pricing lookup, cost attribution, CloudWatch metric specs, and tests. Use when asked to support a new AWS service, add or change a collector, price a new resource type, or when a resource appears in inventory unpriced.
---

# Adding an AWS service

Coverage is only finished when a resource reaches the dashboard **priced**. A collector on its
own puts a row in Inventory with a blank cost, which reads as broken rather than as progress.
Work through the six places below in order and tick them off; the frequent failure is stopping
after the collector.

```
- [ ] 1 collector, registered
- [ ] 2 IAM permissions, same change
- [ ] 3 pricing lookup, verified with `finops prices`
- [ ] 4 attribution estimator
- [ ] 5 metric specs, only if a rule needs them
- [ ] 6 tests: collector (moto) + pricing/attribution (FakePricingClient)
- [ ] `finops scan --dry-run --collectors <key> --no-advice` shows the resource with a cost
```

`efs.py` is the cleanest worked example across all six; copy its shape.

## 1. Collector

`backend/finops/aws/collectors/<service>.py`, subclassing `Collector` with `@register`, then
added to `_COLLECTOR_MODULES` in `collectors/__init__.py` (registration is by import; a module
not listed there simply never runs).

- `key` is the CLI name (`--collectors efs`), `service` is the display label, `scope = "global"`
  only for genuinely global services — those get one call in the default region and must filter
  their own results with `ctx.in_scope(region)`.
- Use `paginate` for every list call, `tags_to_dict` for tags, `synthesize_arn` when the API
  returns no ARN, `az_to_region` for AZ-scoped results.
- Put everything a rule or price lookup will need into `attributes`, in the units the rule wants
  (EFS stores both bytes and GB for exactly this reason). A rule cannot go back to AWS later.
- Raising is fine and often correct: the runner records a capability note per
  (collector, region) and the scan continues. Catch narrowly only when a *secondary* call fails
  and the resource is still worth reporting — return `None` for the unknown attribute, as
  `_lifecycle` does, so "unmeasured" stays distinguishable from "absent".
- Record `owned_by_this_account` for anything that can be shared in through RAM. Charges for
  someone else's resource are not your bill.

## 2. IAM permissions

`iam/finops-readonly-policy.json`, in the same commit. `Describe`, `List`, `Get` only. A
collector whose permissions land in a later change fails on every real account until then.

## 3. Pricing

A method on `PricingClient` in `aws/pricing.py` returning `Price | None`. Never write a rate
down — not as a default, not as a fallback, not "just for now".

- Filter on the narrowest attributes that identify the product, and pass `usage_type` when a
  product family holds several charges under one filter set.
- If AWS publishes the rate in a different unit from the one it advertises, add the pair to
  `_UNIT_CONVERSIONS` rather than dividing at the call site. A rate off by exactly 1024 or
  1,000,000 is this.
- Where a service includes an allowance, encode it as a named constant and expose a helper that
  returns only the billable part (`EFS_BASELINE_MIBPS_PER_GB` with
  `efs_billable_throughput_mibps`), so both attribution and rules charge the same thing.
- Add the lookup to the `prices` command in `cli.py`, then run `finops prices --region us-west-2`
  and confirm the rate and its source. A lookup absent from that list is a lookup nobody can
  check.

## 4. Attribution

A function in `attribution.py` registered in `_ESTIMATORS` under the `resource_type`, returning
a monthly figure or `None`. Return `None` — never `0.0` — when an input is missing or its rate
did not resolve. An unpriced resource is reported as unpriced; a zero silently understates the
estate.

## 5. Metrics

Only if a rule needs utilization. Declare a `MetricSpec` list in `aws/metrics.py` keyed by
resource type in `_SPEC_BUILDERS`, and derive whatever the rule reads in `_derive`. Name derived
keys so `lib/format.ts` can render the unit (`_mibps_`, `_percent`, `_bytes`). Rules never call
CloudWatch.

## 6. Tests

- Collector: `moto` against a seeded account in `tests/test_collectors.py`, asserting the
  attributes rules depend on, not just the count. Where moto does not implement the API, stub
  that one call explicitly rather than working around it in the collector.
- Pricing and attribution: `FakePricingClient` in `tests/test_pricing_attribution.py`, stating
  the rates the assertion assumes. Also assert the unpriced path: missing input or missing rate
  leaves `monthly_cost is None`.
- Add the service to `demo.py` if a dry run should show it, and keep `test_demo.py` passing.

Then run the checks in `finops-change-done`.
