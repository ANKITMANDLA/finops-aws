# Troubleshooting

Things that have actually gone wrong here, what caused them, and the fix. Ordered by where you
hit them: running a scan, pricing, the dashboard, the LLM, and development.

## Scanning

### "Unable to authenticate to AWS"

The scan aborts before collecting anything, deliberately: a bad profile would otherwise produce
a confident report on an apparently empty account.

Usually an expired SSO session. `aws sso login --profile your-profile`, then `finops whoami` to
confirm the identity and region list before scanning again. If `whoami` shows the wrong account,
`FINOPS_AWS_PROFILE` in `.env` is pointing somewhere else.

### The scan finishes but a whole service is missing

Check the Data coverage panel on the Overview page, or `finops report latest`. A denied
`List`/`Describe` is recorded as a capability note and the scan continues, so a missing service
is usually a missing permission rather than an empty account.

To be certain which it is, ask AWS directly with a few lines of boto3 in `.artifacts/`. On the
account this was built against, `sqs:ListQueues` and `dynamodb:ListTables` are denied while NAT
gateways and VPC endpoints are genuinely absent — indistinguishable in the dashboard until you
read the notes.

### Everything shows $0 and there is no run rate

Cost Explorer is denied. All twelve `ce:*` calls appear in the notes, and the Overview shows a
"No spend baseline" banner. The cost of ownership frame still works, priced from the AWS Price
List, and is labelled as list prices.

Granting `ce:Get*` replaces every list-price figure with what you actually pay, and unlocks
forecasting, month-over-month change, and commitment coverage.

### A resource shows no cost per month

Expected for anything whose rate needs measured usage that CloudWatch did not report: Lambda
functions with no invocations in the window, S3 buckets with no size metric, Auto Scaling groups
whose instances are priced individually instead. The UI says "unpriced" rather than "$0" on
purpose — a resource with no defensible number is never given a made-up one.

If something you expect to be priceable is not, run `finops prices --region <its region>` and
check whether that rate resolves at all.

### The savings total jumped by tens of thousands between scans

Almost always one rule. On the current account `ec2.underutilized_instance` is 95% of the total,
so any change to its thresholds moves the headline enormously. Compare rule by rule rather than
comparing totals:

```python
from collections import Counter
import json
scan = json.load(open(".artifacts/real.json", encoding="utf-8"))
money = Counter()
for f in scan["findings"]:
    if not f.get("alternative_to"):
        money[f["rule_id"]] += f.get("estimated_monthly_savings") or 0
print(money.most_common(10))
```

Two historical causes worth ruling out: the savings figure being taken from a source whose
remediation you cannot run, and two competing findings both counting on one resource. Both are
fixed, and both are the sort of thing to check first if a total looks impossible.

### The same resource appears twice in Savings

One finding should be marked `alternative` and excluded from totals. If both are counted, the
two rules involved are not deferring to each other — see the rule-competition note in
`AGENTS.md`. If they are identical rather than competing (five auto-scaling group workers with
word-for-word identical titles), they should be collapsing into one expandable row, which is
`lib/groups.ts` in the frontend.

### Untagged spend looks far too high

`has_ownership_tag` tokenizes tag keys on non-alphanumeric boundaries so `company:cost-center`
counts. This read $112k instead of $2.4k before that fix. If it regresses, that function is
where to look.

## Pricing

### A rate is off by a factor of 1024 or a million

AWS publishes some rates in different units from the ones it advertises. gp3 throughput is
published per GiBps and advertised per MiBps, which showed as `$40.96` instead of `$0.04`.
`_UNIT_CONVERSIONS` in `aws/pricing.py` holds the conversions; add the pair rather than scaling
at the call site.

### `pricing:GetProducts` is denied

Not fatal. With `FINOPS_PUBLIC_PRICE_LIST=true` (the default) the same rates come from the price
list files AWS publishes without authentication. `finops prices` shows the source of each rate,
so you can confirm which path is being used.

### A price lookup returns None in a script but works in the CLI

The script is probably constructing `PricingClient` directly, which does not wire up the public
price list fallback. Use `build_pricing(aws, notes)` instead — this exact mistake cost an hour
on the EFS work.

### Stale or corrupt cached price files

Cached files carry a `CACHE_VERSION`, and a mismatch triggers a re-download. If a file is
truncated or a parser change makes older caches unusable, delete `.artifacts/price-list-cache/`
and re-run; it is a cache and nothing depends on its contents surviving.

### The Python process dies while parsing a price list file

The EC2 file is enormous, and a naive parse used to exhaust memory. `extract_rates` streams it,
skips structural keys, stops at reserved-instance terms, and caps buffered block size with
`_MAX_BLOCK_LINES`. If you touch that parser, test it against the real EC2 file rather than a
fixture.

## Server and dashboard

### `[Errno 10048] only one usage of each socket address`

A previous `finops serve` is still holding the port, often one you thought you had stopped —
killing the launcher PID does not necessarily kill the `uvicorn` worker. Find the real listener
and stop that:

```powershell
netstat -ano | Select-String ":8099"
Stop-Process -Id <pid> -Force
```

Then confirm the port is free before restarting.

### The dashboard is blank, or 404s on every route

`frontend/dist` does not exist. `cd frontend && npm run build`. The API hosts the built SPA, so
a fresh clone has no UI until it is built once.

### A blank white screen with a working API

React 19 crashes on a `useEffect` that returns a value. An arrow body without braces returns the
result of its last expression implicitly, which looks harmless and takes down the page. Wrap
effect bodies in `{}`.

Check the browser console; the error names the component.

### The dashboard shows stale numbers after a scan

Reads come from SQLite, so a completed scan is visible immediately, but you may be looking at an
older scan in the selector — the header dropdown does not follow the newest scan on its own.
`GET /api/health` reports `latest_scan_id` if you want to confirm what the newest one is.

Restarting the server is only necessary if you changed backend code, since a long-running server
keeps the Python it started with.

### The Trends page breaks on older scans

A new field on `TcoReport` must be optional in `lib/types.ts`. Scans stored before the field
existed do not have it, and the frontend reads history.

## LLM and chat

### "LLM advisor returned unusable output: no JSON object in response"

The model ran out of output budget before finishing the JSON. Reasoning models spend part of the
budget thinking, which is why `llm_max_output_tokens` defaults to 8192 and
`FINOPS_GEMINI_THINKING_LEVEL` defaults to `low` rather than `default`. A `MAX_TOKENS` finish
reason is reported explicitly rather than being surfaced as a parse error.

If it recurs, raise the token budget or lower the thinking level.

### Advice generation fails immediately after changing a key or model

Check the model name first. `gemini-3.5-flash-light` fails where `gemini-3.5-flash-lite` works,
and the API's error for an unknown model is not obvious. Settings are read at startup, so restart
`finops serve` after editing `.env`.

`finops advise latest` re-runs only the LLM layer against a stored scan, so you can iterate
without paying for another Cost Explorer pass.

### Chat answers contain `$$\text{Cost} = S \times $0.10$$`

Models emit LaTeX for arithmetic no matter what the prompt says. The dashboard renders markdown,
not math, so a flattener strips the constructs that show up in arithmetic. If a new construct
gets through, add it to `_SYMBOLS` or the regexes beside it in `agent/chat.py` rather than
widening the prompt again — the prompt alone was already tried and was not enough.

### An MCP tool is unavailable

The pricing server runs locally over stdio and needs `uvx` on `PATH`. The AWS Knowledge server
is hosted over HTTP and needs outbound network. A server that will not start inside
`mcp_startup_timeout_seconds` is skipped for that turn and reported, rather than failing the
conversation. `GET /api/chat/capabilities` lists what connected.

An `AccessDeniedException` from the pricing server is an IAM matter on your role, not a bug; the
assistant continues with the sources it does have.

## Development

### Tests fail only on your machine

Your `.env` is leaking in. `conftest.py` unsets `FINOPS_*` and disables `.env` loading, both
autouse — a test that constructs `Settings` itself must also pass `_env_file=None`. This first
appeared as advisor tests failing purely because `FINOPS_LLM_PROVIDER=gemini` was set locally.

### `moto` does not implement an API

Some calls are missing (`describe_client_vpn_endpoints`) and some return incomplete data (EFS
`SizeInBytes` is always empty). Handle it explicitly: catch `ClientError`, `BotoCoreError`, and
`NotImplementedError` around the optional call in the collector, and patch missing data into
demo resources in `demo.py` rather than pretending the collector can cope.

### A dry-run scan writes no JSON file

`--output` needs `_write_scan`, which is called on both the dry-run and real paths. If a file is
missing, the flag is probably `--no-save` being confused with `--output`; `--no-save` skips the
store, `-o` writes the JSON.

### A test intermittently fails on scan ordering

Scans created in the same second sort ambiguously by timestamp alone. The store's queries use
`scan_id DESC` as a secondary key for exactly this reason; a new query that orders only by time
will flake.

### Ruff reformats far more than expected

Run the formatter through the project config. An ad-hoc Prettier invocation on the frontend
rewraps at 80 columns and fights the existing style; `ruff format backend tests` respects the
100-column setting in `pyproject.toml`.

### The store has grown large, or is full of junk scans

25 scans reached 43.8 MB on one afternoon of iteration. Thin them out:

```bash
finops prune --demo --empty        # dry runs and failed runs
finops prune --keep 5
finops prune --id 20260812T205952Z-2539
```

Use `--no-save` while iterating to avoid the problem.

### A Playwright script reports success but writes no screenshot

Relative output paths resolve against the script's own directory, so `.artifacts/shots/x.png`
run from inside `.artifacts` lands in `.artifacts/.artifacts/shots/`. Use absolute paths.

If an element is not found, it may be below the fold — call `scrollIntoViewIfNeeded()` — and
prefer a selector on the input itself (`input[placeholder='Search findings']`) over matching
label text.
