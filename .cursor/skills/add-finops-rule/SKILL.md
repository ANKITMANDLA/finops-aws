---
name: add-finops-rule
description: Write or change a deterministic cost rule in this FinOps agent, including thresholds, evidence, remediation, overlap with other rules and with AWS's own checks, and rule tests. Use when asked to add a waste check, a saving recommendation, a finding, or to adjust when an existing rule fires.
---

# Adding a rule

A rule turns facts already on the `RuleContext` into a `Finding` with a dollar figure someone
will plan against. The figure is the product; everything below exists to keep it defensible.

```
- [ ] rule subclass with @register, module listed in _RULE_MODULES
- [ ] every threshold in config.Thresholds
- [ ] savings derived from a resolved rate or the resource's own cost, never invented
- [ ] savings <= what the resource costs
- [ ] overlap with other rules and AWS checks decided explicitly
- [ ] detail says what was measured, what to change, what the estimate assumes
- [ ] Evidence for the measurements, Remediation for the steps
- [ ] confidence / effort / risk / rollback_possible set honestly
- [ ] tests: fires, does not fire, and the guard cases
```

`rules/efs.py` is a good model: three rules, one per failure mode, each returning early on
anything it cannot measure.

## Shape

Subclass `Rule` in the right `rules/` module with `@register`, set `id`
(`<family>.<snake_case>`), `category`, `title`, and implement
`evaluate(ctx) -> Iterable[Finding]` as a generator over `ctx.of_type(...)`. New module goes in
`_RULE_MODULES`.

Rules never call AWS and never read CloudWatch. If the fact you need is not on the resource, it
belongs in the collector's `attributes` or in a `MetricSpec` — see `add-aws-service`.

## Getting the money right

- Prefer `ctx.monthly_cost(resource)` when the recommendation removes the resource; use
  `ctx.pricing` when it changes one dimension of it. If the rate is `None`, `continue`. Never
  substitute a plausible number.
- Charge only what AWS charges: subtract any included allowance (EFS provisioned throughput
  above the Standard baseline) before pricing the delta.
- `finding_for()` clamps at zero, but nothing clamps to the resource's cost — an estimate larger
  than the bill is a bug in the arithmetic, so check it.
- Return early rather than yield pennies. `merge_findings` drops anything under
  `thresholds.min_monthly_savings_usd` anyway, and a rule that leans on that is a rule whose
  own logic you cannot read.

## Not measured is not zero

A metric that is absent means the question was not answered. `if peak is None: continue`, and
say "none seen" in evidence rather than "0". Treating missing utilization as idle is how a
busy resource gets recommended for deletion.

Check for the states that look idle and are not: a replication destination has no client
connections by design, a stopped instance still pays for its volumes, a shared-in resource is
billed to its owner.

## Overlap

Two counted findings on one resource is a bug. Before adding a rule, ask what else already fires
on that resource type.

- Same action, different source (our rule and Compute Optimizer): `merge_findings` handles it by
  `finding.id`, which is `action + arn`. Use the same `ACTION_*` constant as the AWS check so
  they actually collide.
- Different actions, same resource (resize and Graviton): decide which wins, describe the other
  inside its `detail`, and have the loser defer. Defer on the condition the other rule really
  uses — not on a proxy for it, or both stand aside and the resource ends up with no
  recommendation from us at all.
- Genuinely rival claims are left to `mark_alternatives`, which counts the largest and keeps the
  rest visible and excluded from totals. Do not reach for that to avoid deciding.

## Thresholds

Every number that decides whether a rule fires goes in `config.Thresholds` with a `description`,
so it is tunable through `FINOPS_THRESHOLDS__*`. Constants that describe the *shape* of the
recommendation rather than its trigger — headroom multipliers, assumed cold fraction — stay in
the rule module as named constants with a comment saying why that value.

## Detail, evidence, remediation

`detail` is prose read by someone deciding whether to act. Say what was measured, what the
change is, and what the estimate assumes, including the assumption that would make it wrong.
Name the alternative you are not recommending and why.

`Evidence` is the measurements and the rate, each a label and a value with its unit.
`Remediation` is `summary` (including any constraint, like EFS allowing one throughput decrease
per 24 hours), `cli`, `terraform` where it means anything, and `console_path`.

Set `risk="high"` with `rollback_possible=False` for anything destructive; ranking uses these,
and the UI flags them.

## Tests

`tests/test_rules.py`, building resources with `make_resource` and a `RuleContext` directly —
no AWS mocking, because rules never call AWS. Supply rates through `FakePricingClient`.

Cover: the rule fires with the expected savings, it does not fire just under the threshold, and
each guard (missing metric, unresolved rate, deferral to another rule) holds. Names read as
sentences:

```python
def test_a_replication_destination_is_not_reported_as_unused(...)
def test_throughput_within_the_included_baseline_is_not_charged(...)
```

Then run the checks in `finops-change-done`.
