"""Ledger and worker tests on a real Postgres.

Uses LEDGER_DSN (an empty database, e.g. a CI service container) when set; otherwise
starts a throwaway local cluster with initdb/pg_ctl if they are on PATH; otherwise skips.
The Supabase pieces the migrations need are stubbed by tests/supabase_stub.sql.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
MIG = ROOT / "db" / "migrations"
FIXTURE = ROOT / "fixtures" / "card-foil-hairline.svg"
ALL = ["001_prepress.sql", "002_access.sql", "003_review_fixes.sql"]
RECORDED = ["001_prepress.sql", "002_access.sql"]      # what ran in the cloud recording


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    if os.environ.get("LEDGER_DSN"):
        yield os.environ["LEDGER_DSN"]
        return
    if not (shutil.which("initdb") and shutil.which("pg_ctl")):
        pytest.skip("no LEDGER_DSN and no local Postgres binaries")
    d = tmp_path_factory.mktemp("pg")
    port = _free_port()
    subprocess.run(["initdb", "-D", str(d / "data"), "-U", "postgres", "-A", "trust"],
                   check=True, capture_output=True)
    subprocess.run(["pg_ctl", "-D", str(d / "data"), "-l", str(d / "log"), "-w", "start",
                    "-o", f"-p {port} -k {d} -c listen_addresses=''"], check=True, capture_output=True)
    try:
        yield f"postgresql://postgres@/postgres?host={d}&port={port}"
    finally:
        subprocess.run(["pg_ctl", "-D", str(d / "data"), "-m", "immediate", "stop"], capture_output=True)


def _make_db(server_dsn, migrations):
    name = f"t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(server_dsn, autocommit=True) as c:
        c.execute(f"create database {name}")
    dsn = server_dsn.replace("/postgres?", f"/{name}?")
    with psycopg.connect(dsn, autocommit=True) as c:
        # Roles are cluster-wide: 002 creates them only if missing.
        c.execute((ROOT / "tests" / "supabase_stub.sql").read_text())
        for m in migrations:
            c.execute((MIG / m).read_text())
    return dsn


@pytest.fixture()
def dsn(server):
    return _make_db(server, ALL)


def q(dsn, query, *args):
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(query, args)
        return cur.fetchall() if cur.description else None


CUSTOMER = "11111111-1111-1111-1111-111111111111"
STAFF = "22222222-2222-2222-2222-222222222222"


def place(dsn, params=None, ref=None, path=None, sha=None):
    ref = ref or f"T-{uuid.uuid4().hex[:6]}"
    sha = sha or hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    path = path or f"inputs/{ref}/{sha[:16]}.svg"
    r = q(dsn, "select * from prepress.record_paid_order(%s, %s, %s, %s, %s::jsonb)",
          ref, CUSTOMER, path, sha, json.dumps(params or {}))[0]
    return str(r["job_id"]), path


def claim(dsn, job, task="task-A", retry=0):
    return q(dsn, "select * from prepress.claim_job(%s, %s, %s, 0, 'inst', 'rev', 480)", job, task, retry)[0]


def dispatched_long_ago(dsn, job):
    q(dsn, "update prepress.dispatch_outbox set dispatched_at = now() - interval '2 minutes', "
           "task_name = 'task-A' where job_id = %s", job)


FILES = json.dumps([{"kind": k, "path": f"outputs/x/{uuid.uuid4()}/{k}", "sha256": "0" * 64, "bytes": 1}
                    for k in ("plate_pdf", "proof_png", "preflight_json")])


def commit(dsn, job, token):
    return q(dsn, "select prepress.commit_result(%s, %s, '{}'::jsonb, %s::jsonb) as r", job, token, FILES)[0]["r"]


def _duplicate_then_reconcile(dsn):
    """The reviewer's sequence: delivery runs; a duplicate delivery of the same task is
    declined as busy (and acknowledged, so Cloud Tasks deletes the task); the reconciler
    runs after its grace period and finds the task gone; then attempt 1 commits."""
    job, _ = place(dsn)
    first = claim(dsn, job)
    dup = claim(dsn, job)
    dispatched_long_ago(dsn, job)
    due = [str(r["job_id"]) for r in q(dsn, "select * from prepress.jobs_to_reconcile(45)")]
    rec = q(dsn, "select prepress.reconcile_job(%s, false) as r", job)[0]["r"]
    result = commit(dsn, job, first["token"])
    return job, first, dup, due, rec, result


# ------------------------------------------------------------------ blocking finding

def test_recorded_schema_had_the_bug(server):
    """On migrations 001+002 (the cloud recording), the reconciler kills the live attempt."""
    dsn = _make_db(server, RECORDED)
    job, first, dup, due, rec, result = _duplicate_then_reconcile(dsn)
    assert (first["decision"], dup["decision"]) == ("run", "busy")
    assert job in due and rec == "requeued" and result == "rejected_stale"


def test_reconciler_never_declares_a_live_attempt_lost(dsn):
    job, first, dup, due, rec, result = _duplicate_then_reconcile(dsn)
    assert (first["decision"], dup["decision"]) == ("run", "busy")
    assert job in due
    assert rec == "in_flight"
    assert result == "committed"
    assert q(dsn, "select status from prepress.jobs where id = %s", job)[0]["status"] == "review"
    assert q(dsn, "select count(*) n from prepress.job_results where job_id = %s", job)[0]["n"] == 1
    assert [r["outcome"] for r in q(dsn, "select outcome from prepress.job_attempts where job_id = %s", job)] == ["committed"]


def test_expired_lease_with_task_gone_is_lost_then_requeued_then_failed(dsn):
    job, _ = place(dsn)
    claim(dsn, job)
    dispatched_long_ago(dsn, job)
    q(dsn, "update prepress.job_attempts set lease_until = now() - interval '1 second' where job_id = %s", job)
    assert q(dsn, "select prepress.reconcile_job(%s, false) as r", job)[0]["r"] == "requeued"
    a2 = claim(dsn, job, task="task-B")
    assert a2["decision"] == "run" and a2["attempt_no"] == 2
    q(dsn, "update prepress.dispatch_outbox set dispatched_at = now() - interval '2 minutes', task_name = 'task-B' "
           "where job_id = %s and seq = 2", job)
    q(dsn, "update prepress.job_attempts set lease_until = now() - interval '1 second' where job_id = %s", job)
    assert q(dsn, "select prepress.reconcile_job(%s, false) as r", job)[0]["r"] == "failed_attempts_exhausted"
    assert q(dsn, "select last_error from prepress.jobs where id = %s", job)[0]["last_error"] == \
        "worker disappeared without a final result (attempt 2 of 2)"


# ------------------------------------------------------------------ ledger integrity

def test_a_committed_attempt_is_never_rewritten(dsn):
    job, _ = place(dsn)
    a = claim(dsn, job)
    assert commit(dsn, job, a["token"]) == "committed"
    # Connection lost after the server committed: the worker reports an error for the same token.
    r = q(dsn, "select prepress.fail_attempt(%s, %s, 'ConnectError', false) as r", job, a["token"])[0]["r"]
    assert r == "already_committed"
    assert commit(dsn, job, a["token"]) == "committed"             # replay is harmless
    att = q(dsn, "select outcome, error from prepress.job_attempts where job_id = %s", job)
    assert att == [{"outcome": "committed", "error": None}]
    assert q(dsn, "select count(*) n from prepress.job_results where job_id = %s", job)[0]["n"] == 1


def test_an_input_path_cannot_be_reused_with_other_bytes(dsn):
    _, path = place(dsn, ref="T-same")
    with pytest.raises(psycopg.errors.RaiseException, match="different version"):
        place(dsn, ref="T-same", path=path, sha="b" * 64)


def test_simultaneous_duplicate_claims_run_once(dsn):
    job, _ = place(dsn)
    out, barrier = [], threading.Barrier(5)

    def go():
        barrier.wait()
        out.append(claim(dsn, job)["decision"])
    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == ["busy"] * 4 + ["run"]


def test_duplicate_submissions_make_one_job(dsn):
    sha = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    jobs = {place(dsn, ref="T-dup", sha=sha, path=f"inputs/T-dup/{sha[:16]}.svg")[0] for _ in range(5)}
    assert len(jobs) == 1


def test_row_policies_customer_and_staff(dsn):
    """Row policies evaluated as the `authenticated` role with each user's JWT claims, the
    way the API layer runs a signed-in user's query. Local stand-in, not a client session."""
    job, _ = place(dsn)
    a = claim(dsn, job)
    commit(dsn, job, a["token"])
    q(dsn, "insert into prepress.members values (%s, 'staff'), (%s, 'customer')", STAFF, CUSTOMER)

    def as_user(uid, query, *args):
        with psycopg.connect(dsn, row_factory=dict_row) as c:
            c.execute("set local role authenticated")
            c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": uid}),))
            return c.execute(query, args).fetchall()
    count = "select count(*) n from prepress.file_versions where job_id = %s"
    assert as_user(CUSTOMER, count, job)[0]["n"] == 0
    assert as_user(STAFF, count, job)[0]["n"] == 3
    proof = q(dsn, "select id from prepress.file_versions where job_id = %s and kind = 'proof_png'", job)[0]["id"]
    as_user(STAFF, "select prepress.approve_file_version(%s)", proof)
    assert as_user(CUSTOMER, "select kind from prepress.file_versions where job_id = %s", job) == [{"kind": "proof_png"}]


# ------------------------------------------------------------------ worker, end to end

@pytest.fixture()
def worker_env(dsn, monkeypatch):
    from service import config, storage, worker
    monkeypatch.setattr(config, "DB_URL", dsn)
    monkeypatch.setattr(config, "ALLOW_FAULTS", False)
    blobs = {}
    monkeypatch.setattr(storage, "signed_url", lambda path, expires_s=300: path)
    monkeypatch.setattr(storage, "download_signed", lambda url, dest: Path(dest).write_bytes(blobs[url]))
    monkeypatch.setattr(storage, "upload_new", lambda path, data, ct: blobs.__setitem__(path, data))
    return worker, blobs


def _deliver(worker, job, retry=0):
    return worker.handle(job, {"x-cloudtasks-taskname": "task-A", "x-cloudtasks-taskretrycount": str(retry)})


def test_worker_commits_and_ignores_simulated_work_outside_test_revisions(dsn, worker_env):
    worker, blobs = worker_env
    job, path = place(dsn, {"work_seconds": 30})
    blobs[path] = FIXTURE.read_bytes()
    status, body = _deliver(worker, job)
    assert status == 200 and body["result"] == "committed"
    manifest = q(dsn, "select manifest from prepress.job_results where job_id = %s", job)[0]["manifest"]
    assert manifest["params"]["work_seconds"] == 0.0


def test_worker_refuses_an_input_whose_checksum_changed(dsn, worker_env):
    worker, blobs = worker_env
    job, path = place(dsn)
    blobs[path] = FIXTURE.read_bytes() + b"<!-- changed after the order was recorded -->"
    status, body = _deliver(worker, job, retry=0)
    assert status == 200 and body["outcome"] == "failed"           # final on the first delivery
    assert "input checksum mismatch" in q(dsn, "select last_error from prepress.jobs where id = %s", job)[0]["last_error"]


def test_a_blocking_preflight_is_final_on_the_first_delivery(dsn, worker_env):
    worker, blobs = worker_env
    job, path = place(dsn, {"line_floor_mm": 0.13})
    blobs[path] = FIXTURE.read_bytes()
    status, body = _deliver(worker, job, retry=0)
    assert status == 200 and body["outcome"] == "failed"
    assert q(dsn, "select attempt_count from prepress.jobs where id = %s", job)[0]["attempt_count"] == 1
