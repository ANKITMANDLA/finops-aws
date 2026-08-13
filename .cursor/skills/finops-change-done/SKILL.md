---
name: finops-change-done
description: The verification gate for this FinOps repo — tests, ruff, TypeScript, and the dry-run and pricing checks that apply to what changed. Use before reporting any non-trivial change as finished, and when asked whether a change is done, verified, or ready to commit.
---

# Before calling a change done

Run all four. They are fast and need no AWS account, so there is no reason to report a change as
finished without them.

```bash
pytest
ruff check backend tests --fix
ruff format backend tests
cd frontend && npx tsc --noEmit
```

On PowerShell, run the frontend check as `cd frontend; npx tsc --noEmit`.

Ruff is configured in `pyproject.toml`. Run it through that config rather than ad hoc, and never
run Prettier on the frontend — it rewraps at 80 columns and fights the existing style.

## Then, depending on what changed

| Changed | Also run |
| --- | --- |
| A collector, rule, attribution, or metric spec | `finops scan --dry-run --no-advice`, narrowed with `--collectors` / `--rules` |
| A pricing lookup or `_UNIT_CONVERSIONS` | `finops prices --region us-west-2`, and read the rate and its source |
| The API or a Pydantic model | `pytest tests/test_api.py`, and check the mirrored type in `frontend/src/lib/types.ts` |
| A setting or threshold | `.env.example` gains an entry |
| A collector's AWS calls | `iam/finops-readonly-policy.json` covers them, `Describe`/`List`/`Get` only |
| The UI | `npm run build`, and a Playwright screenshot to `.artifacts/` with an absolute path if the change is visual |

A dry run exercises every collector, rule, and report against a moto-backed account in seconds.
Reading its output is the check — a scan that completes while your new resource shows no cost, or
your new rule produces nothing, has told you something.

## Reporting the result

Say what you ran and what it said. If a check is failing for a reason that predates the change,
say that too rather than leaving it unmentioned.

Do not commit unless asked. When asked, the tree should be clean and all four checks green
first.
