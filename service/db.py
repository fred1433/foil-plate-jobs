"""Thin calls into the prepress.* SQL functions. One short connection per call:
the service runs one request per instance and goes through the transaction pooler."""
import json
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from . import config


@contextmanager
def conn():
    c = psycopg.connect(config.DB_URL, prepare_threshold=None, row_factory=dict_row,
                        connect_timeout=10, autocommit=False)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def one(query, *args):
    with conn() as c:
        return c.execute(query, args).fetchone()


def many(query, *args):
    with conn() as c:
        return c.execute(query, args).fetchall()


def claim_job(job_id, task, retry, execn, instance, revision, lease_s):
    return one("select * from prepress.claim_job(%s, %s, %s, %s, %s, %s, %s)",
               job_id, task, retry, execn, instance, revision, lease_s)


def mark_engine_started(token):
    one("select prepress.mark_engine_started(%s) as ok", token)


def commit_result(job_id, token, manifest, files):
    return one("select prepress.commit_result(%s, %s, %s::jsonb, %s::jsonb) as r",
               job_id, token, json.dumps(manifest), json.dumps(files))["r"]


def fail_attempt(job_id, token, error, final):
    return one("select prepress.fail_attempt(%s, %s, %s, %s) as r", job_id, token, error, final)["r"]


def pending_dispatch(limit):
    return many("select * from prepress.pending_dispatch(%s)", limit)


def mark_dispatched(outbox_id, task):
    one("select prepress.mark_dispatched(%s, %s) as ok", outbox_id, task)


def jobs_to_reconcile(grace_s):
    return many("select * from prepress.jobs_to_reconcile(%s)", grace_s)


def reconcile_job(job_id, task_exists):
    return one("select prepress.reconcile_job(%s, %s) as r", job_id, task_exists)["r"]
