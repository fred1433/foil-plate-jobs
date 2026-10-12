# Operations

## Set up a test deployment
1. Database: apply **every** file in `db/migrations/`, in numeric order
   (`001_prepress.sql`, `002_access.sql`, `003_review_fixes.sql`, and any later one), then run
   `infra/supabase_setup.py <creds file outside the repo>` (sets database role passwords,
   creates the backend, worker, staff and customer Auth users). The current code needs 003:
   at startup each service checks that the functions it calls exist (`service/schema.py`)
   and exits if one is missing, so a revision deployed against an older schema never
   becomes ready and the deploy step fails.
2. Write the three secret files (`worker-db-url`, `worker-storage-password`,
   `dispatcher-db-url`) into a directory outside the repository.
3. `infra/bootstrap.sh` with the variables listed at its top. It creates the Workload
   Identity provider or updates an existing one, then checks that the provider's condition
   is exactly the expected one (repository id, `main`, `deploy.yml` through
   `assertion.workflow_ref`) and stops otherwise.
4. Set the project, provider and deployer values in `.github/workflows/deploy.yml`, then
   merge to `main`. The deploy job runs the full suite (engine, goldens, ledger, worker)
   against a Postgres service inside the exact image it then pushes, and fails if any test
   is skipped.

## Replay the recorded scenarios
`LAB_CREDS=<creds file> PROJECT=<project> python scripts/scenarios.py <name>`.
Run `burst` and `rollback` on their own; the others can run side by side. Each writes
`evidence/<name>.json`, including the deployment it ran against.

## Everyday actions
| Need | Action |
|---|---|
| A job failed and the cause is fixed | `select prepress.resubmit_job('<job id>')` as the backend role: two more attempts, history kept |
| See why a job failed | `prepress.job_attempts` (one row per attempt, with outcome and error) and `prepress.deliveries` (every delivery, including declined ones) |
| Approve outputs | staff calls `prepress.approve_file_version('<version id>')`; approving the proof moves the customer pointer |
| Pause new deliveries | `gcloud tasks queues pause prepress-jobs`. Attempts already running can finish and commit; queued tasks wait; the outbox keeps accepting jobs and the dispatcher keeps creating tasks, which wait in the paused queue |
| Roll back the worker | `gcloud run services update-traffic foil-worker --to-revisions=<healthy revision>=100`, then run one short job and check it reaches `review`. Afterwards, `--to-latest` once a fixed revision is deployed, or later deploys will not receive traffic. A revision switch does not undo schema migrations, rows, stored objects or outside calls made meanwhile |

## Tear down
`PROJECT=<project> infra/teardown.sh` removes the scheduler job, the queue, both services,
the image repository and the secrets. Keep the budget until the final cost has been read.
It currently reports deletion failures without stopping; see below.

## Before production
Known limits of this reference, to settle when the real engine and application are wired in:

- **Fault injection off.** Bootstrap enables `ALLOW_FAULTS=1` for the test deployment.
  Production revisions run without `ALLOW_FAULTS` and without `ENGINE_BROKEN`.
- **Dispatch deadline validated at insertion**, and permanent task-creation errors settled
  per outbox row, so one invalid row cannot block dispatch and reconciliation of others.
- **Ownership checks** in `record_paid_order`: an existing order reference must belong to the
  supplied customer, and an existing input row to that order.
- **One budget for the whole attempt.** The 420 s limit covers the engine subprocess;
  sign-in, download and uploads run outside it. Use an overall attempt deadline and give
  each stage what remains, so slow storage cannot eat the margin before the 480 s timeout.
- **Teardown that verifies.** Make `infra/teardown.sh` check each resource is gone and exit
  non-zero on a real deletion failure.
- **Dated package snapshot.** Install Ghostscript, Poppler, Cairo and LittleCMS from a dated
  Debian snapshot for repeatable rebuilds (drift is already caught by the toolchain test).
- **Narrow input support.** The stand-in engine handles FOIL paint in attributes, inline
  `style` and inherited from groups; other SVG constructs (CSS classes, `<use>`, patterns)
  are not supported and should be rejected explicitly if inputs widen. The golden check
  compares the plate and the finding codes, not the proof image.
