"""Dispatcher and reconciler, invoked by Cloud Scheduler (OIDC) once a minute.

Dispatch: read pending outbox rows (bounded batch), create one Cloud Task per row
with a deterministic name, then mark the row. A crash between "task created" and
"row marked" is repaired on the next run: the create returns AlreadyExists and the
row is marked.

Reconcile: for jobs whose task has been out for a while, ask Cloud Tasks whether
the task still exists. A job without a final result whose task is gone has lost
its delivery: it is requeued if attempts remain, otherwise failed with a readable
reason. Nothing is failed on age alone.
"""
import hashlib
import json
import logging

from google.api_core import exceptions as gexc
from google.cloud import tasks_v2
from google.protobuf import duration_pb2

from . import config, db

log = logging.getLogger("dispatcher")
_client = None


def client():
    global _client
    if _client is None:
        _client = tasks_v2.CloudTasksClient()
    return _client


def queue_path():
    return client().queue_path(config.GCP_PROJECT, config.QUEUE_LOCATION, config.QUEUE)


def task_name(job_id, seq):
    # Hashed prefix spreads names across the keyspace; job and seq keep it deterministic.
    h = hashlib.sha256(f"{job_id}:{seq}".encode()).hexdigest()[:10]
    return f"{queue_path()}/tasks/{h}-{job_id}-{seq}"


def create_task(job_id, seq, deadline_s, audience=None):
    name = task_name(job_id, seq)
    task = {
        "name": name,
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{config.WORKER_URL}/tasks/run",
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"job_id": str(job_id)}).encode(),
            "oidc_token": {"service_account_email": config.TASK_SERVICE_ACCOUNT,
                           "audience": audience or config.WORKER_URL},
        },
        "dispatch_deadline": duration_pb2.Duration(seconds=deadline_s),
    }
    try:
        client().create_task(parent=queue_path(), task=task)
        return name, "created"
    except gexc.AlreadyExists:
        return name, "already_existed"


def task_exists(name):
    try:
        client().get_task(name=name)
        return True
    except gexc.NotFound:
        return False


def run_once():
    summary = {"dispatched": [], "reconciled": []}
    for row in db.pending_dispatch(config.DISPATCH_BATCH):
        fault = ((row["params"] or {}).get("fault") or {}) if config.ALLOW_FAULTS else {}
        deadline = int(fault.get("dispatch_deadline_s") or row["dispatch_deadline_s"])
        name, how = create_task(row["job_id"], row["seq"], deadline, fault.get("task_audience"))
        db.mark_dispatched(row["outbox_id"], name)
        summary["dispatched"].append({"job_id": str(row["job_id"]), "seq": row["seq"], "how": how,
                                      "dispatch_deadline_s": deadline})
    for j in db.jobs_to_reconcile(config.RECONCILE_GRACE_S):
        exists = task_exists(j["task_name"])
        r = db.reconcile_job(j["job_id"], exists)
        if r not in ("in_flight", "waiting_for_delivery", "settled"):
            summary["reconciled"].append({"job_id": str(j["job_id"]), "task_exists": exists, "result": r})
    log.info(json.dumps({"event": "dispatch_run", **summary, "revision": config.REVISION}))
    return summary
