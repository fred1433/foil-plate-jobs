# Maintaining foil-plate-jobs with an AI agent

Read this before changing anything. It is written for coding agents and for the
people who review their pull requests.

## What this repository is
A reference service: a small foil-plate engine (`foilplate/`), a private Cloud Run
worker and dispatcher (`service/`), a Postgres job ledger (`db/migrations/`), and
recorded evidence of how it behaved on a real deployment (`evidence/`).

## Invariants. A change that breaks one of these is wrong, even if tests pass.
1. **A result is registered once.** Outputs are written to new, attempt-scoped paths
   (`outputs/<job>/<attempt token>/`) and registered by `prepress.commit_result`, which
   checks the attempt token inside one transaction. Never upsert an output object, never
   register outside that function, never drop the `unique (job_id, kind)` constraint.
2. **Persist, then acknowledge.** The worker answers 200 only after the outcome is in
   Postgres. Do not move work to a background thread and return early.
3. **Deadlines nest.** `APP_DEADLINE_S` (420) < Cloud Run `timeout` (480) <
   task `dispatch_deadline` (540). Change all three together and update `docs/architecture.md`.
4. **Queue retry policy matches the code.** Queue `maxAttempts` equals `MAX_ATTEMPTS` (2).
5. **Nothing customer-facing moves without a person.** Jobs write `pending_review`
   versions; only `prepress.approve_file_version` (staff) changes what a customer sees.
6. **No service keys at runtime.** The worker reaches storage as its own Auth user and
   the database as `prepress_worker`, which can call four functions and nothing else.
7. **The engine reports; it does not redraw.** Preflight findings never alter geometry.

## How to work
- Engine change: `python -m pytest -q tests` locally; the golden tests only run inside the
  image (`FOILPLATE_PINNED_IMAGE=1`), which CI does on every pull request. If an output
  change is intended, re-record goldens with the `record-goldens` workflow and say why in
  the pull request. Never re-record to make a failing comparison pass.
- Ledger change: new numbered file in `db/migrations/`; keep the previous worker revision
  compatible with the new schema, because a rollback swaps revisions, not schemas.
- Workflow or IAM change: say in the pull request which identity gains or loses what.
- Small pull requests, one concern each. Merging to `main` deploys.

## Never
- Commit credentials, connection strings, or tokens; they live in Secret Manager and in
  an operator file outside the repository.
- Enable `ALLOW_FAULTS` on a production revision; it exists for the recorded scenarios.
- Add an outside API call from the engine without an explicit decision about which data
  may leave the system.

## Useful commands
- Render one job: `python -m foilplate render fixtures/card-foil-hairline.svg /tmp/out`
- Compare with goldens (in the image): `python -m foilplate compare`
- Replay a recorded scenario: see `scripts/scenarios.py` and `docs/operations.md`.
