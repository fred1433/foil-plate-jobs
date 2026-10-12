# foil-plate-jobs

A reference service for specialty print prepress jobs: a paid order becomes a job row in
Postgres, a dispatcher hands it to Cloud Tasks, and a private Cloud Run worker turns an
authored vector card into a vector foil plate and a proof that wait for a person to
approve them.

![Proof and plate from the recorded golden run](docs/img/proof-and-plate.png)

Outputs from the recorded golden run, made inside the pinned image:
[`plate.pdf`](goldens/card-foil-hairline/plate.pdf) (vector, no raster objects),
[`proof.png`](goldens/card-foil-hairline/proof.png),
[`preflight.json`](goldens/card-foil-hairline/preflight.json), from the input
[`card-foil-hairline.svg`](fixtures/card-foil-hairline.svg).

## Deployed and tested, not live
Deployed on Google Cloud (us-central1) on **12 October 2026**: infrastructure from 13:58 UTC,
first engine image by a merge at 14:18 UTC, scenarios
recorded between **14:19 and 14:46 UTC**, Cloud resources removed at **14:46:57 UTC**
the same day, test data and access removed from the database project at **14:47 UTC**.
The deploy workflow was disabled after teardown. Nothing here is running now; the
evidence below is what remains, each file tied to its commit, image digest, Cloud Run
revision, configuration, fixture checksum and time.

## Evidence
All scenarios except the two deployments ran against revision `foil-worker-00005-mpj`,
image `sha256:9bb60b91…98190f`, built from commit `6d64083` by a push to `main`
([record](evidence/deploy-of-tested-revision.json)). Queue: 2 deliveries, 10 s backoff,
5 concurrent dispatches. Worker: 1 request per instance, at most 6 instances, 480 s timeout.

| Scenario | What happened | Record |
|---|---|---|
| Merge deploys | Merging pull request #1 built the image, ran the 7 engine and golden tests inside it, pushed `sha256:d1c20744…`, and moved both services to it (`foil-worker-00003-82m`); a check confirmed the serving revisions run that digest | [merge-deploy](evidence/merge-deploy.json) |
| Private invocation | No token: **403**. Google-shaped token with a forged signature: **401**. Valid Google identity without invoke permission: **403**. Task identity: **200**. The three rejected probes left no line in the container's logs; the accepted one left one | [auth-matrix](evidence/auth-matrix.json) |
| Twenty-job burst | 20 paid orders, 20 s of simulated work each: 20 results, at most 5 attempts at once on 5 instances (the dispatch limit), queue wait 41 to 87 s (median 69 s, including the dispatcher's pickup), 150 s from first order to last result | [burst](evidence/burst.json) |
| Worker killed, retried | Attempt 1's process killed with SIGKILL; Cloud Tasks redelivered; attempt 2 committed. 26 s | [crash-retry](evidence/crash-retry.json) |
| Worker killed on the last delivery | Both processes killed; nobody alive to write. The reconciler saw the task gone and failed the job with *worker disappeared without a final result (attempt 2 of 2)*. 80 s | [crash-final](evidence/crash-final.json) |
| Never reached the worker | Task identity given the wrong audience: Cloud Run refused the deliveries before the handler (no delivery recorded). The reconciler failed the job with *delivery ended before any attempt reached the worker* | [never-started](evidence/never-started.json) |
| Duplicates | 5 simultaneous submissions of one order: 1 job. 3 simultaneous deliveries of it: 1 ran, 2 declined. Engine started once; one committed result | [duplicates](evidence/duplicates.json) |
| Lost acknowledgement | Killed right after committing: the redelivery found the result and did nothing; engine started once. Killed after uploading but before registering: the redelivery recomputed (engine started twice), two attempt folders exist in storage, one result is registered | [ack-loss](evidence/ack-loss.json) |
| Stale worker | Task deadline set to 60 s, attempt 1 paused 150 s before committing. The redelivery took over and committed; attempt 1's late commit was refused | [stale-commit](evidence/stale-commit.json) |
| Six-minute job | 360 s inside the engine process (a sleep: a deadline test, not prepress CPU), committed at 6 min 3 s | [deadline-6min](evidence/deadline-6min.json) |
| Over the deadline | 450 s requested: the engine was stopped at 420 s on both attempts; the job failed with *engine stopped at the 420 s application deadline (attempt 2 of 2)* | [deadline-exceeded](evidence/deadline-exceeded.json) |
| Paid test order to review | Outputs arrived as `pending_review`. Staff user: listed 3 files, downloaded the plate. Customer user: listed 0, download refused, no rows visible, own file pointer unchanged. After staff approved the proof, the customer could download the proof, still not the plate. No service key in these checks | [review-queue](evidence/review-queue.json) |
| Rollback | A deliberately faulty revision failed a job; traffic moved back to the healthy revision in 7.8 s; a fresh job committed on it **31.8 s** after the rollback started. The failed job, rows and stored objects are not undone by a revision switch | [rollback](evidence/rollback.json) |

The one guarantee, stated narrowly: **duplicate submissions and deliveries produce one
committed result. A crashed attempt may be recomputed; it cannot register the result
twice.** How often the engine *starts* is reported separately, because an engine that
pays for outside calls cares.

A note on the authentication criterion: Cloud Run answers **403** to a call with no
credentials, and **401** to a malformed or forged token. Both are rejections before any
code runs; a check written as "unauthenticated gets 401" would fail on a correctly
locked service.

## What still needs the production engine
The engine here is a stand-in: one authored vector fixture, one plate, one proof, one
advisory finding (a 0.12 mm foil hairline, under a 0.15 mm design target, reported and
left as drawn). It does not separate artwork from illustrations and calls no outside
service. Equivalence with a real engine needs that engine and approved references from
real jobs, run through the same harness (`python -m foilplate compare`, tolerance in
[`goldens/tolerance.json`](goldens/tolerance.json)). The harness shows it notices an
intentional change: moving the hairline by 0.5 mm fails the comparison.

## Example of an AI-authored change with independent automated review
[Pull request #1](https://github.com/fred1433/foil-plate-jobs/pull/1) was written by an
AI coding agent and reviewed by a second agent started fresh with only the diff and the
repository. The review found ten issues, among them a deploy trust that accepted any
workflow on `main`; the fixes are in the same pull request. This shows the development
loop. Who approves production pull requests, and how fast, is an arrangement to agree
with the people who run the service.

## Deployment window and cost
- Budget alert on the test project: $5, as a notification (not a cap). Safeguards were a
  finite test batch, 5 concurrent dispatches, at most 6 worker instances and 1
  dispatcher, minimum instances 0, and teardown right after the run.
- Cost reported by the billing account's budget for the project: **$0.00 as of
  2026-10-12 14:44:49 UTC**. Billing data lags by hours, so this is not the final figure.
- Usage estimate from the request logs: about 2,000 request-seconds on the worker and
  64 on the dispatcher, plus 33 cold starts, so roughly 2,300 vCPU-seconds: about
  **$0.06 before free allowances**, $0 within them.

## Read next
- [docs/architecture.md](docs/architecture.md): the outbox and dispatcher choice, who signs
  what, attempts and failures, the deadline ladder, queue policy.
- [docs/cost.md](docs/cost.md): monthly cost at 300 and 3,000 jobs, as formulas with dated rates.
- [docs/licensing.md](docs/licensing.md): the image inventory and the Ghostscript decision.
- [docs/operations.md](docs/operations.md): setup, replaying the scenarios, everyday actions, teardown.
- [CLAUDE.md](CLAUDE.md): invariants and working rules for coding agents.

## Layout
`foilplate/` engine and CLI · `service/` worker and dispatcher · `db/migrations/` ledger,
roles and policies · `infra/` bootstrap and teardown · `scripts/` scenario runner ·
`evidence/` recorded results · `goldens/` reference outputs and toolchain versions.

The database and storage side ran on a hosted Supabase free-tier test project, in its own
schema and private bucket, removed after the run.

Licence: Apache 2.0. Ghostscript and Poppler in the image carry their own licences; see
[docs/licensing.md](docs/licensing.md).
