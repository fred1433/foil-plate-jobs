"""Operator helpers for the recorded scenarios: act as the application backend,
staff or customer, and read the ledger as the read-only observer.

Credentials come from the JSON file named by LAB_CREDS (kept outside the repo).
Nothing secret is printed or written to evidence files.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
CREDS = json.load(open(os.environ["LAB_CREDS"]))
URL = CREDS["supabase_url"]
ANON = CREDS["anon_key"]
POOLER = os.environ.get("LAB_POOLER", "aws-0-us-east-1.pooler.supabase.com")
BUCKET = "prepress-files"
FIXTURE = ROOT / "fixtures" / "card-foil-hairline.svg"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def dsn(role):
    return (f"postgresql://{role}.{CREDS['ref']}:{CREDS['db'][role]}@{POOLER}:6543/postgres"
            "?sslmode=require")


def db(role, query, *args):
    with psycopg.connect(dsn(role), prepare_threshold=None, row_factory=dict_row) as c:
        cur = c.execute(query, args)
        return cur.fetchall() if cur.description else None


def observe(query, *args):
    return db("prepress_observer", query, *args)


_tokens = {}


def token(user):
    if user not in _tokens:
        u = CREDS["users"][user]
        r = httpx.post(f"{URL}/auth/v1/token?grant_type=password", headers={"apikey": ANON},
                       json={"email": u["email"], "password": u["password"]}, timeout=20)
        r.raise_for_status()
        _tokens[user] = r.json()["access_token"]
    return _tokens[user]


def hdr(user, extra=None):
    h = {"apikey": ANON, "Authorization": f"Bearer {token(user)}"}
    h.update(extra or {})
    return h


def upload_input(data: bytes, order_ref: str) -> tuple[str, str]:
    """As the application backend: store the immutable input version."""
    sha = hashlib.sha256(data).hexdigest()
    path = f"inputs/{order_ref}/{sha[:16]}.svg"
    r = httpx.post(f"{URL}/storage/v1/object/{BUCKET}/{path}",
                   headers=hdr("backend", {"content-type": "image/svg+xml", "x-upsert": "false"}),
                   content=data, timeout=30)
    if r.status_code not in (200, 409) and "Duplicate" not in r.text:
        r.raise_for_status()
    return path, sha


def place_paid_order(params: dict | None = None, order_ref: str | None = None,
                     data: bytes | None = None, deadline_s: int = 540) -> dict:
    """As the application backend, after verifying a (test) payment event."""
    order_ref = order_ref or f"T-{uuid.uuid4().hex[:8]}"
    data = data if data is not None else FIXTURE.read_bytes()
    path, sha = upload_input(data, order_ref)
    rows = db("prepress_app", "select * from prepress.record_paid_order(%s, %s, %s, %s, %s::jsonb, %s)",
              order_ref, CREDS["users"]["customer"]["id"], path, sha, json.dumps(params or {}),
              deadline_s)
    return {"order_ref": order_ref, "job_id": str(rows[0]["job_id"]), "created": rows[0]["created"],
            "input_path": path, "input_sha256": sha}


def job(job_id):
    return observe("select * from prepress.jobs where id = %s", job_id)[0]


def wait_for(job_ids, statuses=("review", "failed"), timeout_s=900, poll_s=5):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        rows = observe("select id, status from prepress.jobs where id = any(%s::uuid[])", list(job_ids))
        if rows and all(r["status"] in statuses for r in rows) and len(rows) == len(job_ids):
            return round(time.time() - t0, 1)
        time.sleep(poll_s)
    raise TimeoutError(f"jobs not settled after {timeout_s}s")


def ledger(job_id):
    """Everything the evidence needs about one job, without secrets."""
    j = job(job_id)
    keep = ("id", "status", "attempt_count", "max_attempts", "last_error", "created_at",
            "finished_at", "idempotency_key")
    return {
        "job": {k: j[k] for k in keep},
        "outbox": observe("select seq, dispatch_deadline_s, created_at, dispatched_at, task_name "
                          "from prepress.dispatch_outbox where job_id = %s order by seq", job_id),
        "deliveries": observe("select task_name, task_retry_count, task_execution_count, instance_id, "
                              "revision, decision, received_at from prepress.deliveries "
                              "where job_id = %s order by id", job_id),
        "attempts": observe("select attempt_no, task_retry_count, revision, instance_id, started_at, "
                            "engine_started_at, finished_at, outcome, error from prepress.job_attempts "
                            "where job_id = %s order by attempt_no", job_id),
        "file_versions": observe("select kind, storage_path, sha256, bytes, status, created_at "
                                 "from prepress.file_versions where job_id = %s order by kind", job_id),
        "results": observe("select attempt_id, committed_at from prepress.job_results where job_id = %s",
                           job_id),
    }


def gcloud_json(*args):
    r = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def deployment_context(project, region="us-central1"):
    svc = gcloud_json("run", "services", "describe", "foil-worker", "--project", project, "--region", region)
    spec = svc["spec"]["template"]["spec"]
    q = gcloud_json("tasks", "queues", "describe", "prepress-jobs", "--project", project,
                    "--location", region)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                            cwd=ROOT).stdout.strip()
    return {
        "recorded_at": now(),
        "repo_commit_of_operator_checkout": commit,
        "worker_revision": svc["status"]["latestReadyRevisionName"],
        "worker_image": spec["containers"][0]["image"],
        "worker_commit_env": next((e.get("value") for e in spec["containers"][0].get("env", [])
                                   if e["name"] == "GIT_COMMIT"), None),
        "worker_config": {
            "timeoutSeconds": spec.get("timeoutSeconds"),
            "containerConcurrency": spec.get("containerConcurrency"),
            "maxScale": svc["spec"]["template"]["metadata"]["annotations"].get("autoscaling.knative.dev/maxScale"),
            "minScale": svc["spec"]["template"]["metadata"]["annotations"].get("autoscaling.knative.dev/minScale", "0"),
            "resources": spec["containers"][0].get("resources"),
            "env_non_secret": {e["name"]: e.get("value") for e in spec["containers"][0].get("env", [])
                               if "value" in e and e["name"] in (
                                   "APP_DEADLINE_S", "RUN_TIMEOUT_S", "DISPATCH_DEADLINE_S",
                                   "MAX_ATTEMPTS", "ALLOW_FAULTS", "ENGINE_BROKEN", "ROLE")},
        },
        "queue": {"retryConfig": q.get("retryConfig"), "rateLimits": q.get("rateLimits"),
                  "state": q.get("state")},
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
    }


def save(name, payload):
    out = ROOT / "evidence" / f"{name}.json"
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"evidence written: evidence/{name}.json")
    return out
