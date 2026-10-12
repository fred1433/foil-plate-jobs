"""Runtime settings, all from the environment. Secrets arrive as env vars mounted
from Secret Manager by Cloud Run; they are never logged."""
import os

ROLE = os.environ.get("ROLE", "worker")                 # worker | dispatcher
REVISION = os.environ.get("K_REVISION", "local")
COMMIT = os.environ.get("GIT_COMMIT", "unknown")

# Deadline hierarchy (seconds): engine < Cloud Run request timeout < Cloud Tasks dispatch deadline.
APP_DEADLINE_S = int(os.environ.get("APP_DEADLINE_S", "420"))
RUN_TIMEOUT_S = int(os.environ.get("RUN_TIMEOUT_S", "480"))
DISPATCH_DEADLINE_S = int(os.environ.get("DISPATCH_DEADLINE_S", "540"))
LEASE_S = RUN_TIMEOUT_S                                 # an attempt's claim outlives its request
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "2"))  # must match the queue's maxAttempts

DB_URL = os.environ.get("DB_URL", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")   # public by design
STORAGE_EMAIL = os.environ.get("STORAGE_EMAIL", "")
STORAGE_PASSWORD = os.environ.get("STORAGE_PASSWORD", "")
BUCKET = os.environ.get("BUCKET", "prepress-files")

# Dispatcher
GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
QUEUE_LOCATION = os.environ.get("QUEUE_LOCATION", "us-central1")
QUEUE = os.environ.get("QUEUE", "prepress-jobs")
WORKER_URL = os.environ.get("WORKER_URL", "")
TASK_SERVICE_ACCOUNT = os.environ.get("TASK_SERVICE_ACCOUNT", "")
DISPATCH_BATCH = int(os.environ.get("DISPATCH_BATCH", "25"))  # bounded per invocation
RECONCILE_GRACE_S = int(os.environ.get("RECONCILE_GRACE_S", "45"))

# Test-only fault injection. Off unless this flag is set on the revision.
ALLOW_FAULTS = os.environ.get("ALLOW_FAULTS") == "1"
ENGINE_BROKEN = os.environ.get("ENGINE_BROKEN") == "1"   # used only by the rollback drill
