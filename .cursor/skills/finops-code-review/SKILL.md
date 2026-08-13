---
name: finops-code-review
description: Review changes to this FinOps agent for the defects that produce wrong numbers — invented rates, zero standing in for unknown, double-counted savings, swallowed permission errors — as well as style and test quality. Use when asked to review code, check a diff, or self-review before finishing work in this repo.
---

# Reviewing a change here

The bugs that matter in this codebase are not crashes. They are changes that leave a plausible,
confident, wrong number on the dashboard, which is worse than a blank. Review in the order
below and stop treating anything above as a nit.

## 1. Is every number defensible?

- **An invented rate.** Any literal dollar amount outside `demo.py` is a defect, including one
  called a default, a fallback, or a sensible estimate. Rates come from `PricingClient`; an
  unresolved rate means the resource stays unpriced.
- **Zero standing in for unknown.** `or 0.0`, `get(key, 0)`, and `float(x or 0)` in attribution
  or in a savings calculation turn "not measured" into "free". Attribution estimators return
  `None`; rules `continue`. In a collector, an attribute whose secondary call failed is `None`,
  not `0` or `False`.
- **A missing unit conversion.** A rate that looks off by 1024 or 1,000,000 belongs in
  `_UNIT_CONVERSIONS`, not divided at the call site.
- **An allowance charged twice, or not at all.** Included capacity is subtracted before pricing
  the delta, and the same helper does it in both attribution and the rule.
- **Savings above the bill.** `finding_for` clamps at zero only. Check the arithmetic against
  what the resource costs.

## 2. Does the change double-count?

- Two counted findings on one resource. Check what else fires on that resource type; the
  deferral must be conditioned on what the other rule actually does, not on a proxy that leaves
  the resource with no recommendation from either.
- A finding id that no longer collides with the AWS check it duplicates, because the
  `ACTION_*` constant changed.
- Findings collapsed in the backend to simplify the UI. Grouping is presentation; the store keeps
  every finding and totals must be identical either way.

## 3. Does it stay honest when AWS says no?

- A collector that catches `ClientError` broadly and returns `[]` has turned a denied permission
  into an empty service. Let it raise so the runner records a capability note, or catch the one
  secondary call and record the gap as `None`.
- New AWS calls appear in `iam/finops-readonly-policy.json`, in the same change, and are
  `Describe`, `List`, or `Get`.
- Absent and unmeasured stay distinguishable all the way to the UI.

## 4. Tests

- No test reaches AWS or depends on the developer's environment. `moto` for AWS surfaces,
  `FakePricingClient` for rates, factories for models.
- A pricing test states the rate it assumes rather than asserting a real market price as truth.
- The guard cases are covered, not just the happy path: missing metric, unresolved rate,
  threshold boundary, deferral. A rule test that only proves the rule fires is half a test.
- Names read as sentences and describe the case, not the function under test.

## 5. Readability and style

- Comments explain a constraint or a trade-off the code cannot state — why a threshold is that
  value, why an API is called twice, why something is excluded. A comment narrating the next
  line, or explaining the change to a reviewer, should be deleted.
- Thresholds that decide whether a rule fires live in `config.Thresholds`, not inline.
- Ruff clean at 100 columns, `from __future__ import annotations`, precise types over `Any`,
  `logger` over `print`.
- `detail` text reads as prose to someone deciding whether to act, and says what the estimate
  assumes.
- New behaviour that reverses a documented decision needs an entry in `DECISIONS.md`, or it will
  be reverted by the next person reading that file.

## Reporting

Lead with the defects that change a number, each with the file, the line, and what the user would
see on the dashboard as a result. Separate those from style points, and say plainly when the
change is clean.
