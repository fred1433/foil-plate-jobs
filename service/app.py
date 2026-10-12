"""HTTP surface. One image, two Cloud Run services: ROLE=worker and ROLE=dispatcher.

Both services are private (IAM). Requests without a valid identity holding
run.invoker are rejected by Cloud Run before this code runs.
"""
import json
import logging
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger("app")
if config.DB_URL and config.SCHEMA_CHECK:
    from .schema import check_or_exit
    check_or_exit(config.DB_URL, config.ROLE)

app = FastAPI(title=f"foil-plate-jobs {config.ROLE}", docs_url=None, redoc_url=None)


@app.middleware("http")
async def trace_every_request(request: Request, call_next):
    # Any request that reaches the container is logged here. Probe requests carry
    # an x-probe-id header so the evidence can show they never arrived.
    log.info(json.dumps({"event": "request_reached_container", "path": request.url.path,
                         "probe_id": request.headers.get("x-probe-id"),
                         "revision": config.REVISION}))
    return await call_next(request)


@app.get("/health")   # Cloud Run reserves paths ending in "z", such as /healthz
def healthz():
    return {"ok": True, "role": config.ROLE, "revision": config.REVISION, "commit": config.COMMIT}


if config.ROLE == "worker":
    from . import worker

    @app.post("/tasks/run")
    def run_task(body: dict, request: Request):
        # Synchronous on purpose: the response is sent only after the outcome is persisted.
        status, payload = worker.handle(body["job_id"], {k.lower(): v for k, v in request.headers.items()})
        return JSONResponse(payload, status_code=status)

if config.ROLE == "dispatcher":
    from . import dispatcher

    @app.post("/dispatch")
    def dispatch():
        return dispatcher.run_once()
