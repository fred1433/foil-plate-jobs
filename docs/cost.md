# Monthly running cost, as a calculation

Rates checked on 12 October 2026 on the public pricing pages (us-central1, USD):
[Cloud Run](https://cloud.google.com/run/pricing), [Cloud Tasks](https://cloud.google.com/tasks/pricing),
[Cloud Scheduler](https://cloud.google.com/scheduler/pricing),
[Secret Manager](https://cloud.google.com/secret-manager/pricing),
[Artifact Registry](https://cloud.google.com/artifact-registry/pricing).

| Item | Rate | Free allowance |
|---|---|---|
| Cloud Run CPU, request-based billing | $0.000024 per vCPU-second | 180,000 vCPU-seconds per month |
| Cloud Run memory | $0.0000025 per GiB-second | 360,000 GiB-seconds per month |
| Cloud Run requests | $0.40 per million | 2 million per month |
| Cloud Tasks operations (create, each delivery attempt; 32 KB chunks) | $0.40 per million | 1 million per month |
| Cloud Scheduler | $0.10 per job per month | 3 jobs |
| Secret Manager | about $0.06 per active version per month; $0.03 per 10,000 accesses | 6 versions; 10,000 accesses |
| Artifact Registry storage | about $0.10 per GiB-month | 0.5 GiB |
| Internet egress from Cloud Run | Google Cloud networking rates | 1 GiB per month within North America |

**Every free allowance above is per billing account, not per project.** If other projects
on the same billing account already use them, the "before allowance" column is the bill.

## Worker (the line that moves)
Assumptions: 1 vCPU, 1 GiB, concurrency 1, minimum instances 0, no retries, no startup
CPU boost. *Billed time* is the instance's billed wall time per job, including startup and
shutdown, and including any time spent waiting on an outside API: an engine that waits
is still billed. The reference engine's own timings are not a measurement of a real
engine.

| Jobs per month | Mean billed time per job | vCPU-s | Cost before allowance | With the full allowance |
|---|---|---|---|---|
| 300 | 60 s | 18,000 | $0.48 | $0.00 |
| 300 | 300 s | 90,000 | $2.39 | $0.00 |
| 3,000 | 60 s | 180,000 | $4.77 | $0.00 |
| 3,000 | 300 s | 900,000 | $23.85 | $18.63 |

Formula: `jobs x seconds x (0.000024 + 0.0000025 x GiB) + jobs / 1e6 x 0.40`, minus the
allowance. At 3,000 jobs of five minutes the worker alone uses almost all of a $20
budget even with the allowance, so "under $20" at that volume depends on the real
duration distribution, which has to be measured on real jobs first.

## The other lines
| Line | Basis | 300 jobs | 3,000 jobs |
|---|---|---|---|
| Dispatcher on a one-minute schedule | 43,200 invocations; measured in this deployment: mean request 0.81 s, about one cold start every five minutes (3 s billed each, assumed). Estimate about 60,000 vCPU-s and 30,000 GiB-s at 512 MiB | about $1.50 before allowance, $0 within it | same: it scales with time, not jobs |
| Cloud Tasks | about 2 operations per job (create plus one delivery), more with retries | ~600 ops: $0 | ~6,000 ops: $0 |
| Cloud Scheduler | one job | $0 within 3, else $0.10 | same |
| Secret Manager | 3 active versions; one access per secret per instance start | $0 within allowance | about $0, accesses grow with cold starts |
| Artifact Registry | about 0.12 GiB per image; keep a few with a cleanup policy | $0 to $0.05 | same |
| Logging | well under the per-project free ingestion | $0 | $0 |
| Egress to the database and storage provider | here about 90 KB of outputs per job; real art files are larger | ~0.03 GiB: $0 | ~0.3 GiB: $0; at 20 MB per job, ~60 GiB billed at networking rates |

Not included, and kept separate on purpose: the database and storage provider's plan,
any model or vectorisation API the engine calls, and commercial software licences
(see `licensing.md`).

## Total, with the free allowance counted once
The worker and the dispatcher draw on the same Cloud Run allowance, so it is applied
once, to their sum. Dispatcher on a one-minute schedule (about 60,000 vCPU-s and
30,000 GiB-s a month, the estimate above); the other lines add a few cents at most.

| Jobs per month | Mean billed time per job | vCPU-s (worker + dispatcher) | GiB-s | Before allowance | After the allowance, counted once |
|---|---|---|---|---|---|
| 300 | 60 s | 78,000 | 48,000 | $1.99 | $0.00 |
| 300 | 300 s | 150,000 | 120,000 | $3.90 | $0.00 |
| 3,000 | 60 s | 240,000 | 210,000 | $6.29 | **$1.44** |
| 3,000 | 300 s | 960,000 | 930,000 | $25.36 | **about $20.14** |

Read plainly: at 3,000 jobs of five minutes, this design costs about $20 a month, at the
top of a $0 to $20 hosting budget and not safely under it. The levers, in order: measure
the real duration distribution (five minutes may be the tail, not the mean); move the
dispatcher to a five-minute schedule or a webhook wake-up (about $18.93 at that volume);
size memory to what the engine needs; and remember that any allowance already used by
other projects on the billing account is not available here.

The dispatcher line is the surprise: a one-minute schedule costs the same at 30 jobs as
at 3,000. A five-minute schedule divides it by five; a webhook wake-up after commit
removes most of it while the outbox stays the recovery path.

## What this test deployment actually cost
See the README section "Deployment window and cost": it gives the cost the billing
account reported, with the time it was read. The $5 budget on the project is an alert,
not a cap; the safeguards were a finite test batch, a bounded queue (5 concurrent
dispatches), bounded instances (6 workers, 1 dispatcher) and teardown after the run.
