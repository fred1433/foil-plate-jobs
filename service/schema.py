"""Startup check: the database must carry every function this code calls, including
those added by the latest migration. If one is missing the container exits, so a new
Cloud Run revision never becomes ready and the deploy step fails instead of the
dispatcher failing later, row after row."""
import psycopg

REQUIRED = {
    "worker": [
        "prepress.claim_job(uuid, text, integer, integer, text, text, integer)",
        "prepress.mark_engine_started(uuid)",
        "prepress.commit_result(uuid, uuid, jsonb, jsonb)",
        "prepress.fail_attempt(uuid, uuid, text, boolean)",
        # Marker for migration 003 (its reconciler and ledger fixes).
        "prepress.mark_dispatched_with_deadline(bigint, text, integer)",
    ],
    "dispatcher": [
        "prepress.pending_dispatch(integer)",
        "prepress.mark_dispatched_with_deadline(bigint, text, integer)",
        "prepress.jobs_to_reconcile(integer)",
        "prepress.reconcile_job(uuid, boolean)",
    ],
}


def missing(dsn: str, role: str) -> list[str]:
    with psycopg.connect(dsn, prepare_threshold=None, connect_timeout=10) as c:
        return [f for f in REQUIRED[role]
                if c.execute("select to_regprocedure(%s) is null", (f,)).fetchone()[0]]


def check_or_exit(dsn: str, role: str) -> None:
    gone = missing(dsn, role)
    if gone:
        raise SystemExit("database schema is behind this code; apply every migration in "
                         f"db/migrations in order. Missing: {', '.join(gone)}")
