# Operations

## Set up a test deployment
1. Supabase: apply `db/migrations/001_prepress.sql` and `002_access.sql`, then run
   `infra/supabase_setup.py <creds file outside the repo>` (sets database role passwords,
   creates the backend, worker, staff and customer Auth users).
2. Write the three secret files (`worker-db-url`, `worker-storage-password`,
   `dispatcher-db-url`) into a directory outside the repository.
3. `infra/bootstrap.sh` with the variables listed at its top.
4. Set the project, provider and deployer values in `.github/workflows/deploy.yml`, then
   merge to `main`.

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
| Stop all processing | `gcloud tasks queues pause prepress-jobs`; pending tasks wait, the outbox keeps accepting jobs |
| Roll back the worker | `gcloud run services update-traffic foil-worker --to-revisions=<healthy revision>=100`, then run one short job and check it reaches `review`. Afterwards, `--to-latest` once a fixed revision is deployed, or later deploys will not receive traffic. A revision switch does not undo schema migrations, rows, stored objects or outside calls made meanwhile |

## Tear down
`PROJECT=<project> infra/teardown.sh` removes the scheduler job, the queue, both services,
the image repository and the secrets. Keep the budget until the final cost has been read.
