"""Recorded scenarios against the deployed reference. Each writes evidence/<name>.json
with the deployment context (commit, image digest, revision, configuration, date).

    LAB_CREDS=/path/outside/repo/creds.json PROJECT=<gcp project> \
        python scripts/scenarios.py <scenario>

Scenarios: auth-matrix, burst, crash-retry, crash-final, duplicates, ack-loss,
deadline-6min, deadline-exceeded, stale-commit, never-started, review-queue,
rollback. Fault injection only works on a revision deployed with ALLOW_FAULTS=1.
"""
from __future__ import annotations

import base64
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(__file__))
import lab  # noqa: E402

PROJECT = os.environ["PROJECT"]
REGION = "us-central1"
SA = lambda n: f"{n}@{PROJECT}.iam.gserviceaccount.com"  # noqa: E731


def sh(*args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check).stdout.strip()


def worker_url():
    return sh("gcloud", "run", "services", "describe", "foil-worker", "--project", PROJECT,
              "--region", REGION, "--format=value(status.url)")


def kick_dispatcher():
    """Run the scheduled dispatch now instead of waiting up to a minute."""
    sh("gcloud", "scheduler", "jobs", "run", "prepress-dispatch", "--location", REGION, "--project", PROJECT)


def id_token(sa, audience):
    return sh("gcloud", "auth", "print-identity-token", f"--impersonate-service-account={sa}",
              f"--audiences={audience}", "--include-email", "--project", PROJECT)


def concurrency_profile(job_ids):
    rows = lab.observe(
        "select a.job_id, a.attempt_no, a.instance_id, a.started_at, a.engine_started_at, a.finished_at, "
        "a.outcome, j.created_at from prepress.job_attempts a join prepress.jobs j on j.id = a.job_id "
        "where a.job_id = any(%s::uuid[]) order by a.started_at", list(job_ids))
    events = []
    for r in rows:
        events += [(r["started_at"], 1), (r["finished_at"], -1)]
    cur = peak = 0
    for _, d in sorted(events, key=lambda e: (e[0], e[1])):
        cur += d
        peak = max(peak, cur)
    waits = sorted((r["started_at"] - r["created_at"]).total_seconds() for r in rows if r["attempt_no"] == 1)
    first = min(r["created_at"] for r in rows)
    last = max(r["finished_at"] for r in rows)
    return {
        "attempts": len(rows),
        "committed": sum(r["outcome"] == "committed" for r in rows),
        "peak_simultaneous_attempts": peak,
        "distinct_instances": len({r["instance_id"].split(":")[0] for r in rows}),
        "queue_wait_s": {"min": round(waits[0], 1), "median": round(waits[len(waits) // 2], 1),
                         "max": round(waits[-1], 1)},
        "drain_s_first_job_created_to_last_result": round((last - first).total_seconds(), 1),
    }


# ---------------------------------------------------------------- scenarios

def auth_matrix():
    url = worker_url()
    probes = []
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": "not-a-google-key", "typ": "JWT"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps({"iss": "https://accounts.google.com", "aud": url,
                                                "sub": "0", "exp": int(time.time()) + 3600}).encode()).decode().rstrip("=")
    cases = [
        ("no_token", None, "Rejected, typically 403"),
        ("invalid_token", f"{header}.{body}.c2lnbmF0dXJlLW5vdC12YWxpZA",
         "Google-shaped ID token, correct audience, signature not from Google"),
        ("valid_identity_without_invoker", id_token(SA("probe-no-invoke"), url),
         "Valid Google ID token, correct audience, identity has no run.invoker"),
        ("task_identity", id_token(SA("task-invoker"), url),
         "The identity Cloud Tasks presents, correct audience"),
    ]
    for name, tok, what in cases:
        pid = f"probe-{name}-{uuid.uuid4().hex[:6]}"
        h = {"x-probe-id": pid, "user-agent": f"foil-probe/{pid}"}
        if tok:
            h["authorization"] = f"Bearer {tok}"
        r = httpx.get(f"{url}/health", headers=h, timeout=30)
        probes.append({"case": name, "what": what, "probe_id": pid, "status": r.status_code,
                       "body_reached_handler": r.status_code == 200 and '"ok":true' in r.text.replace(" ", "")})
    time.sleep(45)   # let logs land
    for p in probes:
        app_logs = sh("gcloud", "logging", "read",
                      f'resource.type="cloud_run_revision" AND resource.labels.service_name="foil-worker" '
                      f'AND jsonPayload.probe_id="{p["probe_id"]}"', "--freshness=15m", "--project", PROJECT,
                      "--format=value(timestamp)")
        req_logs = sh("gcloud", "logging", "read",
                      f'resource.type="cloud_run_revision" AND resource.labels.service_name="foil-worker" '
                      f'AND httpRequest.userAgent="foil-probe/{p["probe_id"]}"', "--freshness=15m",
                      "--project", PROJECT, "--format=value(httpRequest.status)")
        p["container_log_lines"] = len([x for x in app_logs.splitlines() if x])
        p["platform_request_log_status"] = req_logs.splitlines()[:1]
    return {"ingress": sh("gcloud", "run", "services", "describe", "foil-worker", "--project", PROJECT,
                          "--region", REGION, "--format=value(metadata.annotations.'run.googleapis.com/ingress')"),
            "probes": probes}


def burst():
    orders = [lab.place_paid_order({"work_seconds": 20, "burst": i}) for i in range(20)]
    ids = [o["job_id"] for o in orders]
    kick_dispatcher()
    waited = lab.wait_for(ids, timeout_s=900)
    statuses = lab.observe("select status, count(*) from prepress.jobs where id = any(%s::uuid[]) group by 1", ids)
    return {"jobs": 20, "simulated_work_seconds_per_job": 20, "statuses": statuses,
            "observed": concurrency_profile(ids), "operator_wait_s": waited,
            "configured_capacity": {"queue_max_concurrent_dispatches": 5, "worker_max_instances": 6,
                                    "worker_concurrency": 1}}


def _single(params, timeout_s=900, kick=True, deadline_s=540):
    o = lab.place_paid_order(params, deadline_s=deadline_s)
    if kick:
        kick_dispatcher()
    t0 = time.time()
    while True:
        st = lab.job(o["job_id"])["status"]
        if st in ("review", "failed"):
            break
        if time.time() - t0 > timeout_s:
            break
        if int(time.time() - t0) % 60 < 5:
            kick_dispatcher()     # reconciliation runs on the dispatcher's schedule
        time.sleep(5)
    return o, lab.ledger(o["job_id"]), round(time.time() - t0, 1)


def crash_retry():
    o, led, t = _single({"fault": {"kill_at": {"engine": [1]}}})
    return {"order": o, "ledger": led, "seconds_to_settle": t,
            "expect": "attempt 1 process killed (SIGKILL), Cloud Tasks redelivers, attempt 2 commits"}


def crash_final():
    o, led, t = _single({"fault": {"kill_at": {"engine": [1, 2]}}})
    return {"order": o, "ledger": led, "seconds_to_settle": t,
            "expect": "both deliveries die; the reconciler sees the task gone and writes the failure"}


def duplicates():
    # 1. Duplicate submissions of the same paid order, at the same time.
    ref = f"T-dup-{uuid.uuid4().hex[:6]}"
    params = {"work_seconds": 25, "dup": True}
    with cf.ThreadPoolExecutor(5) as ex:
        subs = list(ex.map(lambda _: lab.place_paid_order(params, order_ref=ref), range(5)))
    job_ids = sorted({s["job_id"] for s in subs})
    jid = job_ids[0]
    # 2. Simultaneous duplicate deliveries: two extra tasks for the same job, plus the dispatcher's.
    url = worker_url()
    def extra(i):
        return sh("gcloud", "tasks", "create-http-task", f"dup{i}-{uuid.uuid4().hex[:8]}", "--queue=prepress-jobs",
                  f"--location={REGION}", f"--project={PROJECT}", f"--url={url}/tasks/run", "--method=POST",
                  "--header=content-type:application/json", f'--body-content={{"job_id": "{jid}"}}',
                  f"--oidc-service-account-email={SA('task-invoker')}", f"--oidc-token-audience={url}")
    with cf.ThreadPoolExecutor(3) as ex:
        list(ex.map(extra, range(2)))
        ex.submit(kick_dispatcher)
    lab.wait_for([jid], timeout_s=600)
    time.sleep(20)
    led = lab.ledger(jid)
    return {"submissions": len(subs), "distinct_jobs": len(job_ids),
            "created_flags": [s["created"] for s in subs], "ledger": led,
            "deliveries_received": len(led["deliveries"]),
            "engine_starts": sum(1 for a in led["attempts"] if a["engine_started_at"]),
            "committed_results": len(led["results"])}


def ack_loss():
    out = {}
    for name, point in (("killed_after_commit", "after_commit"), ("killed_after_upload", "after_upload")):
        o, led, t = _single({"fault": {"kill_at": {point: [1]}}, "case": name})
        listing = httpx.post(f"{lab.URL}/storage/v1/object/list/{lab.BUCKET}", headers=lab.hdr("staff"),
                             json={"prefix": f"outputs/{o['job_id']}/", "limit": 100}, timeout=30).json()
        out[name] = {"order": o, "ledger": led, "seconds_to_settle": t,
                     "engine_starts": sum(1 for a in led["attempts"] if a["engine_started_at"]),
                     "committed_results": len(led["results"]),
                     "attempt_output_folders_in_storage": sorted(x["name"] for x in listing),
                     "registered_versions": len(led["file_versions"])}
    return out


def deadline_6min():
    o, led, t = _single({"work_seconds": 360, "case": "deadline-6min"}, timeout_s=1200)
    return {"order": o, "ledger": led, "seconds_to_settle": t,
            "note": "work_seconds is a sleep inside the engine process: a deadline test, not 6 minutes of prepress CPU"}


def deadline_exceeded():
    o, led, t = _single({"work_seconds": 450, "case": "deadline-exceeded"}, timeout_s=1500)
    return {"order": o, "ledger": led, "seconds_to_settle": t,
            "expect": "engine process group stopped at 420 s on each attempt; second attempt writes the failure"}


def stale_commit():
    o, led, t = _single({"fault": {"stall_before_commit_s": {"1": 150}, "dispatch_deadline_s": 60},
                         "case": "stale-commit"}, timeout_s=900)
    time.sleep(120)   # let the stalled attempt wake up and try to commit
    led = lab.ledger(o["job_id"])
    return {"order": o, "ledger": led, "seconds_to_settle": t,
            "expect": "task dispatch deadline 60 s; attempt 1 stalls 150 s before committing; "
                      "the redelivery takes over; attempt 1's late commit is refused"}


def never_started():
    o, led, t = _single({"fault": {"task_audience": "https://wrong-audience.example"},
                         "case": "never-started"}, timeout_s=900)
    return {"order": o, "ledger": led, "seconds_to_settle": t,
            "expect": "both deliveries rejected by Cloud Run before the handler; reconciler fails the job"}


def _sql_as(user_id, query):
    """Run a query as the `authenticated` role with that user's JWT claims (row policies apply)."""
    claims = json.dumps({"sub": user_id, "role": "authenticated"})
    q = (f"begin; set local role authenticated; set local request.jwt.claims = '{claims}'; "
         f"{query}; commit;")
    out = sh("supabase", "db", "query", "--linked", "--project-ref", lab.CREDS["ref"], "-o", "json", q)
    return json.loads(out[out.index("{"):])["rows"]


def review_queue():
    o, led, t = _single({"case": "review-queue"})
    jid = o["job_id"]
    plate = next(f["storage_path"] for f in led["file_versions"] if f["kind"] == "plate_pdf")
    proof = next(f["storage_path"] for f in led["file_versions"] if f["kind"] == "proof_png")
    folder = plate.rsplit("/", 1)[0] + "/"
    def storage_view(user):
        lst = httpx.post(f"{lab.URL}/storage/v1/object/list/{lab.BUCKET}", headers=lab.hdr(user),
                         json={"prefix": folder, "limit": 100}, timeout=30)
        dl = httpx.get(f"{lab.URL}/storage/v1/object/authenticated/{lab.BUCKET}/{plate}",
                       headers=lab.hdr(user), timeout=30)
        sign = httpx.post(f"{lab.URL}/storage/v1/object/sign/{lab.BUCKET}/{plate}", headers=lab.hdr(user),
                          json={"expiresIn": 60}, timeout=30)
        return {"list_count": len(lst.json()) if lst.status_code == 200 else None,
                "list_status": lst.status_code, "download_status": dl.status_code,
                "download_bytes": len(dl.content) if dl.status_code == 200 else 0,
                "sign_url_status": sign.status_code}
    staff_id, cust_id = lab.CREDS["users"]["staff"]["id"], lab.CREDS["users"]["customer"]["id"]
    before = {
        "staff_storage": storage_view("staff"),
        "customer_storage": storage_view("customer"),
        "staff_rows": _sql_as(staff_id, f"select kind, status from prepress.file_versions where job_id = '{jid}' order by kind"),
        "customer_rows": _sql_as(cust_id, f"select kind, status from prepress.file_versions where job_id = '{jid}' order by kind"),
        "customer_pointer": lab.observe("select customer_file_version_id from prepress.orders where order_ref = %s",
                                        o["order_ref"])[0]["customer_file_version_id"],
    }
    # Staff approves the proof: only then does the customer pointer move.
    vid = lab.observe("select id from prepress.file_versions where job_id = %s and kind = 'proof_png'", jid)[0]["id"]
    _sql_as(staff_id, f"select prepress.approve_file_version('{vid}')")
    after = {
        "customer_proof_download_status": httpx.get(
            f"{lab.URL}/storage/v1/object/authenticated/{lab.BUCKET}/{proof}", headers=lab.hdr("customer"),
            timeout=30).status_code,
        "customer_plate_download_status": httpx.get(
            f"{lab.URL}/storage/v1/object/authenticated/{lab.BUCKET}/{plate}", headers=lab.hdr("customer"),
            timeout=30).status_code,
        "customer_rows": _sql_as(cust_id, f"select kind, status from prepress.file_versions where job_id = '{jid}' order by kind"),
        "customer_pointer_is_approved_proof": str(lab.observe(
            "select customer_file_version_id from prepress.orders where order_ref = %s",
            o["order_ref"])[0]["customer_file_version_id"]) == str(vid),
    }
    return {"order": o, "ledger": led, "before_approval": before, "after_staff_approval": after,
            "identities": "staff and customer are ordinary Supabase Auth users; no service key is used"}


def rollback():
    def rev():
        return sh("gcloud", "run", "services", "describe", "foil-worker", "--project", PROJECT, "--region", REGION,
                  "--format=value(status.latestReadyRevisionName)")
    good = rev()
    sh("gcloud", "run", "services", "update", "foil-worker", "--project", PROJECT, "--region", REGION,
       "--update-env-vars=ENGINE_BROKEN=1", "--quiet")
    bad = rev()
    o_bad, led_bad, _ = _single({"case": "rollback-bad-revision"})
    t0 = time.time()
    sh("gcloud", "run", "services", "update-traffic", "foil-worker", "--project", PROJECT, "--region", REGION,
       f"--to-revisions={good}=100", "--quiet")
    t_traffic = round(time.time() - t0, 1)
    o_new, led_new, _ = _single({"case": "rollback-fresh-job"})
    recovered = round(time.time() - t0, 1)
    # Housekeeping after the drill: remove the flag, then follow latest again.
    sh("gcloud", "run", "services", "update", "foil-worker", "--project", PROJECT, "--region", REGION,
       "--remove-env-vars=ENGINE_BROKEN", "--quiet")
    sh("gcloud", "run", "services", "update-traffic", "foil-worker", "--project", PROJECT, "--region", REGION,
       "--to-latest", "--quiet")
    return {"healthy_revision": good, "faulty_revision": bad,
            "job_on_faulty_revision": {"order": o_bad, "status": led_bad["job"]["status"],
                                       "last_error": led_bad["job"]["last_error"],
                                       "attempt_revisions": [a["revision"] for a in led_bad["attempts"]]},
            "traffic_switch_s": t_traffic,
            "fresh_job_after_rollback": {"order": o_new, "status": led_new["job"]["status"],
                                         "attempt_revisions": [a["revision"] for a in led_new["attempts"]]},
            "rollback_start_to_verified_fresh_job_s": recovered,
            "not_undone_by_rollback": ["the job that failed on the faulty revision stays failed (resubmittable)",
                                       "database rows and storage objects written meanwhile",
                                       "schema migrations, if any had shipped with the faulty revision"]}


SCENARIOS = {f.__name__.replace("_", "-"): f for f in (
    auth_matrix, burst, crash_retry, crash_final, duplicates, ack_loss, deadline_6min, deadline_exceeded,
    stale_commit, never_started, review_queue, rollback)}

if __name__ == "__main__":
    name = sys.argv[1]
    ctx = lab.deployment_context(PROJECT)
    started = lab.now()
    result = SCENARIOS[name]()
    lab.save(name, {"scenario": name, "started_at": started, "finished_at": lab.now(),
                    "context": ctx, "result": result})
