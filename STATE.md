# Current state

Where the project stands as of 13 August 2026. `README.md` says what the agent does and why,
`ARCHITECTURE.md` how it is built, and `AGENTS.md` how to work on it. This file is the
snapshot: what is built, what the last scan of the real account found, and what is knowingly
incomplete.

## Status

Working end to end against a live account. 28 collectors, 44 rules, 323 passing tests, and a
five-page dashboard with a chat assistant. Roughly 14,200 lines of Python across 74 files and
3,600 lines of TypeScript across 23. Clean tree on `main`, in sync with
`github.com/ANKITMANDLA/finops-aws`.

Nothing is stubbed or mocked in the production path. The only synthetic data is in `demo.py`,
which exists so `finops scan --dry-run` can exercise everything offline.

## What the last real scan found

Scan `20260812T205952Z-2539`, account REDACTED_ACCOUNT_ID, three regions
(`us-east-1`, `us-east-2`, `us-west-2`), 53 seconds.

| | |
| --- | --- |
| Resources inventoried | 956 |
| Priced from AWS list prices | 623 |
| Cost of ownership | $REDACTED/month |
| Identified savings | $REDACTED/month (51.2%) |
| Cost after the changes | $REDACTED/month |
| Findings | 190, of which 189 counted and 1 marked as an alternative |
| Untagged spend | $REDACTED/month |

Cost is stated at list price throughout, because Cost Explorer is denied on this role. That
figure knows nothing of commitments or negotiated discounts, so it reads higher than the
invoice for anything covered by a Savings Plan.

**Where the money is.** EC2 is 96% of the priced estate at $REDACTED/month, followed by EFS at
$1,889, EBS at $1,298, ELB at $772, EKS at $292, and KMS at $87. Everything else — S3,
snapshots, Secrets Manager, CloudWatch, Lambda, SNS — totals under $50.

**Where the savings are.** One rule accounts for 95% of the total: 72 underutilized EC2
instances worth $REDACTED/month, nearly all of them EKS worker nodes running m5.16xlarge at
around 7% CPU. After that the numbers drop sharply: over-provisioned EFS throughput at
$REDACTED across five file systems, load balancers with no healthy targets at $329 across 20,
over-provisioned EBS IOPS at $315 across 61 volumes, idle load balancers at $296 across 18,
and empty or sprawling EKS clusters at $438. Of the 189 counted findings, 188 come from our
own rules and 1 from Trusted Advisor.

By effort, $REDACTED is medium, $REDACTED is low, and $219 is high — a reminder that the headline
figure is really one decision about worker node sizing, not a hundred small cleanups.

The LLM advisor (Gemini 3.5 Flash Lite) produced one architectural recommendation, on
rightsizing the EKS worker nodes, plus two quick wins.

## What this role cannot see

23 capabilities were denied and recorded as notes; the Data coverage panel on the Overview
page lists them. They fall into four groups.

**Cost Explorer, entirely.** All twelve `ce:*` calls are denied, which is why there is no run
rate, no forecast, no month-over-month change, and no commitment coverage. The estate is
priced from the AWS Price List instead so the report still has a cost of ownership figure.
Granting `ce:Get*` would replace every list-price number with what you actually pay.

**AWS's own recommendation engines.** Compute Optimizer (six calls across three regions) and
Cost Optimization Hub are both denied, as is `pricing:GetProducts`. The pricing denial costs
nothing because rates fall back to AWS's published price list files, which need no
credentials. The other two mean AWS's rightsizing and idle-resource verdicts are missing, and
our rules are doing that work alone.

**Two services we cannot enumerate.** `sqs:ListQueues` and `dynamodb:ListTables` are denied in
all three regions. The KMS alias names make it clear queues do exist — several are named
`alias/REDACTED-...` — so this is a real blind spot rather than an empty service.
Both collectors are written and tested; they will populate the moment the permissions arrive.

**Trusted Advisor**, which answered, but returns almost nothing on this account because the
cost checks need Business or Enterprise Support.

## Coverage

28 collectors across these services: EC2, EBS and snapshots, AMIs, Auto Scaling, EKS, RDS
including clusters and snapshots, EFS, S3, Lambda, DynamoDB, CloudWatch Logs and alarms,
Elastic IPs, NAT Gateways, transit gateways, VPC endpoints, VPN and Client VPN, ELB classic
and v2, KMS, Secrets Manager, ACM, SNS, SQS, ECR.

44 rules: 11 storage, 8 network, 5 rightsizing, 5 database, 4 idle, 4 commitments, 4
governance, 3 containers.

Verified absent in this account rather than unimplemented: NAT gateways, Elastic IPs, VPC
endpoints, VPN connections, and RDS instances all return zero from the API in all three
regions. The 13 transit gateways present are owned by account REDACTED_ACCOUNT_ID and shared in
through Resource Access Manager, so the attachment charge lands on the owner's bill; they are
recorded at $0 with `owned_by_this_account: false`.

## Known gaps and rough edges

**333 resources are unpriced**, and all of them are unpriced for the same reason: no measured
usage to apply a rate to. 243 Lambda functions with no invocations in the metric window, 69 S3
buckets whose size CloudWatch has not reported, and 21 Auto Scaling groups, whose instances
are priced individually instead. These are genuinely near-zero rather than unknown, but the
dashboard says "unpriced" rather than claiming zero.

**Savings are dominated by one rule**, which makes the headline figure fragile: a single
threshold change to `ec2.underutilized_instance` moves the total by tens of thousands. Worth
keeping in mind before quoting $58.2k to anyone.

**The store holds 25 scans in a 43.8 MB SQLite file**, several of them near-duplicates from
the same afternoon of iteration. `finops prune` thins them out.

**Elastic throughput is not modelled as an EFS alternative.** The over-provisioned throughput
rule recommends a lower provisioned figure and mentions elastic in prose, but does not price
the elastic option, which would win on file systems that are idle most of the time.

**No multi-account support.** Single account by design, and cross-account rollup would be a
substantial change to the store schema and every view.

## If you want the numbers to get better

In order of value for effort: grant `ce:Get*` to replace list prices with real spend and
unlock forecasting and commitment analysis; grant `sqs:ListQueues` and `dynamodb:ListTables`
to close the two blind spots; enrol in Compute Optimizer and Cost Optimization Hub so AWS's
verdicts can be reconciled against ours; and enable resource-level cost data in billing
preferences so per-resource cost stops being an estimate.
