#!/usr/bin/env bash
# Removes everything in the test project that can cost money or keeps running.
# Kept: service accounts, the WIF pool (no cost), the budget and its Pub/Sub topic
# (needed to read the final reported cost). Evidence lives in the repository.
#
#   PROJECT=my-test-project infra/teardown.sh
set -euo pipefail
: "${PROJECT:?}"
REGION=${REGION:-us-central1}
G="gcloud --project=$PROJECT --quiet"
ok() { "$@" >/dev/null 2>&1 && echo "removed: ${*: -1}" || echo "absent or failed: $*"; }

ok $G scheduler jobs delete prepress-dispatch --location="$REGION"
ok $G tasks queues delete prepress-jobs --location="$REGION"
ok $G run services delete foil-worker --region="$REGION"
ok $G run services delete foil-dispatcher --region="$REGION"
ok $G artifacts repositories delete foil --location="$REGION"
for s in worker-db-url worker-storage-password dispatcher-db-url; do ok $G secrets delete $s; done
date -u +"teardown finished %Y-%m-%dT%H:%M:%SZ"
