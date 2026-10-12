# Architecture and decisions

```
 paid order (verified by the app backend)
        |
        v
 Postgres: record_paid_order()  --- one transaction --->  jobs row + dispatch_outbox row
        ^                                                        |
        |                         Cloud Scheduler, every minute, OIDC
        |                                                        v
        |                                   foil-dispatcher (private Cloud Run)
        |                                     - creates Cloud Tasks with deterministic names
        |                                     - reconciles jobs whose delivery is gone
        |                                                        |
        |                                 Cloud Tasks queue (2 deliveries, 10 s backoff,
        |                                 5 concurrent dispatches), OIDC as task-invoker
        |                                                        v
        +----- claim / commit / fail ----------------  foil-worker (private Cloud Run,
                                                       1 request per instance, max 6)
                                                         - signed URL for the input, minted now
                                                         - engine subprocess, 420 s deadline
                                                         - outputs to new paths, pending_review
```

## How a paid order reaches the worker
**Choice: a durable outbox in Postgres, drained by a scheduled dispatcher.** The job row
and its dispatch intent are written in the same transaction, so an order can never
exist without a pending dispatch. The dispatcher creates each Cloud Task with a name
derived from the job and a sequence number; if it crashes after creating the task and
before marking the row, the next run gets `AlreadyExists` and marks it.

Considered and not used as the queue:
- *Database webhook.* Useful as a wake-up call, not as the record: webhooks go through
  `pg_net`, whose request and response tables are unlogged and do not survive a crash.
- *Edge Function creating tasks directly.* It would need a Google credential stored on the
  database side. The scheduled dispatcher authenticates with its own attached identity.

Trade-off: up to a minute of dispatch latency (the scenarios trigger the scheduler job
by hand to skip the wait) and one dispatcher invocation per minute, which is a real line
in `docs/cost.md`. If starts must be immediate, a webhook can call the dispatcher right
after commit; the outbox stays the recovery path.

## Who signs what
| Operation | Identity | Authority it holds, and only that |
|---|---|---|
| Create the job and dispatch intent | application backend (`prepress_app` database role) | execute `record_paid_order`, `resubmit_job` |
| Read the outbox, create tasks, reconcile | `dispatcher-runtime` service account; `prepress_dispatcher` role | `cloudtasks.enqueuer` and `cloudtasks.viewer` on one queue; `actAs` on `task-invoker`; four ledger functions |
| Invoke the private worker | `task-invoker`, whose OIDC token Cloud Tasks mints at delivery time | `run.invoker` on `foil-worker` only |
| Invoke the private dispatcher | `scheduler-invoker` | `run.invoker` on `foil-dispatcher` only |
| Read inputs, write outputs, record results | `worker-runtime` service account; `prepress_worker` role; `worker` Auth user for storage | its two secrets; four ledger functions; storage policies: read `inputs/`, create under `outputs/` |
| Deploy | `deployer`, reached from GitHub Actions through Workload Identity Federation | the provider only accepts tokens from this repository's `main` branch; `artifactregistry.writer` on one repository, `run.developer` on two services, `actAs` on their two runtime accounts |
| Review outputs | `staff` Auth users | read every object in the bucket; approve a version |
| Customers | `customer` Auth users | read approved versions of their own orders, nothing else |

Task creation and worker invocation are separate authentications: the dispatcher's
permission to enqueue says nothing about who may call the worker, and no token is minted
at enqueue time and stored.

## Attempts, duplicates and failures
- **Claim.** `claim_job` locks the job row, records the delivery, and starts an attempt with
  a lease (480 s) and a fresh attempt token. A concurrent duplicate delivery finds a live
  lease and is declined (200, so Cloud Tasks drops it). A redelivery of the *same* task
  with a higher retry count takes over: Cloud Tasks only redelivers after the previous
  delivery failed or timed out.
- **Commit.** Outputs go to `outputs/<job>/<attempt token>/` with upsert disabled, then
  `commit_result` registers all of them and the result in one transaction, only if the
  token is still current. A worker that lost its claim gets `rejected_stale`.
- **Failure written by a live worker.** On an engine error the worker records it. On the
  last delivery the job becomes `failed` with the error and the attempt count.
- **Failure nobody was alive to write.** The reconciler asks Cloud Tasks whether the job's
  task still exists. If it is gone and the job has no final result, the job is requeued
  when attempts remain, or failed with `worker disappeared without a final result`, or
  with `delivery ended before any attempt reached the worker` when no attempt ever started.
  Jobs are never failed on age alone.
- **Resubmission.** A failed job keeps its attempt history; `resubmit_job` grants two more
  attempts and writes a new dispatch intent.

Public claim, and its limit: *duplicate submissions and deliveries produce one committed
result. A crashed attempt may be recomputed; it cannot register the result twice.*
Whether the engine *starts* once is reported separately in the evidence; it matters when
an engine makes chargeable outside calls.

## Deadlines
| Layer | Setting | Default | Here |
|---|---|---|---|
| Engine subprocess | `APP_DEADLINE_S` | none | 420 s, then SIGTERM to the process group, SIGKILL after 10 s |
| Cloud Run request | `timeout` | 300 s | 480 s |
| Cloud Tasks delivery | `dispatch_deadline` | 600 s | 540 s |
| Attempt lease | `LEASE_S` | | 480 s |

When Cloud Run or Cloud Tasks gives up on a request, the container is not stopped; the
code may keep running and try to commit late. The nesting gives the application time to
stop its engine and record an outcome first, and the attempt token makes a late commit
harmless when it happens anyway (`evidence/stale-commit.json`).

## Queue policy
`maxAttempts=2` (one first delivery and one retry), `maxRetryDuration=0s` (no time-based
limit, so the attempt count governs), `minBackoff=maxBackoff=10s` (the doubling setting
has no effect when both are equal), `maxConcurrentDispatches=5`, `maxDispatchesPerSecond=5`.
The worker's `max-instances=6` leaves one instance of headroom above the dispatch limit
for a takeover while a stalled attempt still holds an instance.

## Access to the service
Both services require IAM authentication and accept traffic from the internet, so that
the rejection of anonymous and unauthorised callers can be observed. Setting ingress to
internal is a further layer: Cloud Tasks in the same project still reaches the service,
and outside probes would then be refused before IAM is even consulted, which is a
different test.
