"""Task handler. Order of operations per delivery:

1. claim the job in Postgres (lease + attempt token); declined deliveries return 200
2. mint a signed URL for the immutable input and download it
3. run the engine in a subprocess under the application deadline
4. upload outputs to new, attempt-scoped paths (never overwritten)
5. register them in one transaction that checks the attempt token
6. only then answer 200, so Cloud Tasks stops delivering

An expired or superseded attempt reaches step 5 with a stale token and is refused.
"""
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config, db, storage

log = logging.getLogger("worker")
INSTANCE = f"{socket.gethostname()}:{os.getpid()}"
CONTENT_TYPES = {"plate.pdf": ("plate_pdf", "application/pdf"),
                 "proof.png": ("proof_png", "image/png"),
                 "preflight.json": ("preflight_json", "application/json")}


class EngineFailed(Exception):
    pass


def _fault(params, attempt_no):
    f = (params or {}).get("fault") or {}
    return f if config.ALLOW_FAULTS else {}


def _maybe_die(fault, point, attempt_no):
    if attempt_no in (fault.get("kill_at", {}).get(point) or []):
        log.warning(json.dumps({"event": "fault_injection_kill", "point": point, "attempt": attempt_no}))
        for h in logging.getLogger().handlers:
            h.flush()
        os.kill(os.getpid(), signal.SIGKILL)   # real process death, no cleanup, no response


def _run_engine(src: Path, outdir: Path, params: dict, deadline_s: int) -> dict:
    engine_params = {k: v for k, v in (params or {}).items() if k != "fault"}
    if config.ENGINE_BROKEN:
        engine_params["proof_dpi"] = "broken"           # deliberately faulty revision
    cmd = [sys.executable, "-m", "foilplate", "render", str(src), str(outdir),
           "--params", json.dumps(engine_params)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True)
    try:
        out, err = p.communicate(timeout=deadline_s)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
        raise EngineFailed(f"engine stopped at the {deadline_s} s application deadline")
    last = (out.strip().splitlines() or ["{}"])[-1]
    try:
        body = json.loads(last)
    except json.JSONDecodeError:
        body = {}
    if p.returncode != 0:
        msg = body.get("error") or (err.strip().splitlines() or ["no output"])[-1]
        raise EngineFailed(f"engine exited {p.returncode}: {msg}"[:500])
    return body


def handle(job_id: str, headers: dict) -> tuple[int, dict]:
    task = headers.get("x-cloudtasks-taskname", "")
    retry = int(headers.get("x-cloudtasks-taskretrycount", "0") or 0)
    execn = int(headers.get("x-cloudtasks-taskexecutioncount", "0") or 0)
    claim = db.claim_job(job_id, task, retry, execn, INSTANCE, config.REVISION, config.LEASE_S)
    base = {"job_id": job_id, "task": task, "retry": retry, "decision": claim["decision"],
            "revision": config.REVISION, "instance": INSTANCE}
    log.info(json.dumps({"event": "delivery", **base}))
    if claim["decision"] != "run":
        return 200, base                       # nothing to do; stop this delivery

    token, attempt_no, params = str(claim["token"]), claim["attempt_no"], claim["params"]
    final = retry + 1 >= config.MAX_ATTEMPTS or attempt_no >= claim["max_attempts"]
    fault = _fault(params, attempt_no)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory() as td:
            src, outdir = Path(td) / "input.svg", Path(td) / "out"
            storage.download_signed(storage.signed_url(claim["input_path"]), src)
            db.mark_engine_started(token)
            stall = (fault.get("stall_before_engine_s") or {}).get(str(attempt_no), 0)
            if stall:
                time.sleep(stall)
            _maybe_die(fault, "engine", attempt_no)
            manifest = _run_engine(src, outdir, params, config.APP_DEADLINE_S)
            files = []
            for name, (kind, ctype) in CONTENT_TYPES.items():
                path = f"outputs/{job_id}/{token}/{name}"
                data = (outdir / name).read_bytes()
                storage.upload_new(path, data, ctype)
                files.append({"kind": kind, "path": path, "bytes": len(data),
                              "sha256": manifest["outputs"][name]["sha256"]})
            _maybe_die(fault, "after_upload", attempt_no)
            stall = (fault.get("stall_before_commit_s") or {}).get(str(attempt_no), 0)
            if stall:
                time.sleep(stall)
            manifest["attempt_no"] = attempt_no
            manifest["revision"] = config.REVISION
            result = db.commit_result(job_id, token, manifest, files)
            _maybe_die(fault, "after_commit", attempt_no)
    except EngineFailed as e:
        outcome = db.fail_attempt(job_id, token, str(e), final)
        log.warning(json.dumps({"event": "engine_failed", **base, "attempt": attempt_no,
                                "final": final, "outcome": outcome, "error": str(e)}))
        # 200 when the job is settled (failed or superseded); 500 asks Cloud Tasks to retry.
        return (500 if outcome == "retrying" else 200), {**base, "outcome": outcome, "error": str(e)}
    except Exception as e:  # infrastructure error: report and let Cloud Tasks retry
        msg = f"{type(e).__name__}: {e}"[:300]
        outcome = db.fail_attempt(job_id, token, msg, final)
        log.error(json.dumps({"event": "attempt_error", **base, "outcome": outcome, "error": msg}))
        return (500 if outcome == "retrying" else 200), {**base, "outcome": outcome}
    took = round(time.monotonic() - started, 2)
    log.info(json.dumps({"event": "attempt_done", **base, "attempt": attempt_no,
                         "result": result, "seconds": took}))
    return 200, {**base, "attempt": attempt_no, "result": result, "seconds": took}
