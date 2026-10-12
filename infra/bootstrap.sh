#!/usr/bin/env bash
# Creates everything the reference deployment needs in one GCP project.
# Re-runnable: "already exists" errors are ignored, bindings are additive.
#
#   PROJECT=my-test-project BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX \
#   SECRETS_DIR=/path/outside/repo SUPABASE_URL=https://<ref>.supabase.co \
#   SUPABASE_ANON_KEY=... GITHUB_REPO=owner/foil-plate-jobs GITHUB_REPO_ID=<numeric id> infra/bootstrap.sh
#
# SECRETS_DIR holds three files: worker-db-url, worker-storage-password,
# dispatcher-db-url. Their contents go to Secret Manager and are never echoed.
set -euo pipefail

: "${PROJECT:?}" "${BILLING_ACCOUNT:?}" "${SECRETS_DIR:?}" "${SUPABASE_URL:?}" "${SUPABASE_ANON_KEY:?}" "${GITHUB_REPO:?}" "${GITHUB_REPO_ID:?}"
REGION=${REGION:-us-central1}
QUEUE=prepress-jobs
AR=foil
PLACEHOLDER=us-docker.pkg.dev/cloudrun/container/hello
STORAGE_EMAIL=${STORAGE_EMAIL:-worker@prepress-test.example}
G="gcloud --project=$PROJECT --quiet"
PNUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
sa() { echo "$1@$PROJECT.iam.gserviceaccount.com"; }
ok() { "$@" >/dev/null 2>&1 || true; }

echo "== APIs"
$G services enable run.googleapis.com cloudtasks.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com billingbudgets.googleapis.com \
  logging.googleapis.com pubsub.googleapis.com

echo "== Artifact Registry"
ok $G artifacts repositories create $AR --repository-format=docker --location="$REGION"

echo "== Service accounts (one identity per job)"
for a in worker-runtime dispatcher-runtime task-invoker scheduler-invoker deployer probe-no-invoke; do
  ok $G iam service-accounts create $a --display-name="$a"
done
sleep 10   # new service accounts take a moment to become usable in IAM bindings

echo "== Secrets"
for s in worker-db-url worker-storage-password dispatcher-db-url; do
  ok $G secrets create $s --replication-policy=automatic
  $G secrets versions add $s --data-file="$SECRETS_DIR/$s" >/dev/null
done
for s in worker-db-url worker-storage-password; do
  $G secrets add-iam-policy-binding $s --member="serviceAccount:$(sa worker-runtime)" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done
$G secrets add-iam-policy-binding dispatcher-db-url --member="serviceAccount:$(sa dispatcher-runtime)" \
  --role=roles/secretmanager.secretAccessor >/dev/null

echo "== Queue: 2 deliveries in total, no time-based retry window, fixed 10 s backoff, bounded dispatch"
ok $G tasks queues create $QUEUE --location="$REGION"
$G tasks queues update $QUEUE --location="$REGION" \
  --max-attempts=2 --max-retry-duration=0s --min-backoff=10s --max-backoff=10s --max-doublings=0 \
  --max-concurrent-dispatches=5 --max-dispatches-per-second=5 >/dev/null
for r in roles/cloudtasks.enqueuer roles/cloudtasks.viewer; do
  $G tasks queues add-iam-policy-binding $QUEUE --location="$REGION" \
    --member="serviceAccount:$(sa dispatcher-runtime)" --role=$r >/dev/null
done
# The dispatcher may name task-invoker as the identity Cloud Tasks presents to the worker.
$G iam service-accounts add-iam-policy-binding "$(sa task-invoker)" \
  --member="serviceAccount:$(sa dispatcher-runtime)" --role=roles/iam.serviceAccountUser >/dev/null

echo "== Worker (private, one request per instance, bounded scale)"
if ! $G run services describe foil-worker --region="$REGION" >/dev/null 2>&1; then
  $G run deploy foil-worker --image=$PLACEHOLDER --region="$REGION" --no-allow-unauthenticated \
    --service-account="$(sa worker-runtime)" >/dev/null
fi
$G run services update foil-worker --region="$REGION" \
  --concurrency=1 --min-instances=0 --max-instances=6 --timeout=480 --cpu=1 --memory=1Gi --no-cpu-boost \
  --update-env-vars="ROLE=worker,APP_DEADLINE_S=420,RUN_TIMEOUT_S=480,DISPATCH_DEADLINE_S=540,MAX_ATTEMPTS=2,ALLOW_FAULTS=1,SUPABASE_URL=$SUPABASE_URL,SUPABASE_ANON_KEY=$SUPABASE_ANON_KEY,STORAGE_EMAIL=$STORAGE_EMAIL" \
  --update-secrets=DB_URL=worker-db-url:latest,STORAGE_PASSWORD=worker-storage-password:latest >/dev/null
WORKER_URL=$($G run services describe foil-worker --region="$REGION" --format='value(status.url)')
$G run services add-iam-policy-binding foil-worker --region="$REGION" \
  --member="serviceAccount:$(sa task-invoker)" --role=roles/run.invoker >/dev/null

echo "== Dispatcher (private, invoked by Cloud Scheduler)"
if ! $G run services describe foil-dispatcher --region="$REGION" >/dev/null 2>&1; then
  $G run deploy foil-dispatcher --image=$PLACEHOLDER --region="$REGION" --no-allow-unauthenticated \
    --service-account="$(sa dispatcher-runtime)" >/dev/null
fi
$G run services update foil-dispatcher --region="$REGION" \
  --concurrency=1 --min-instances=0 --max-instances=1 --timeout=120 --cpu=1 --memory=512Mi --no-cpu-boost \
  --update-env-vars="ROLE=dispatcher,GCP_PROJECT=$PROJECT,QUEUE_LOCATION=$REGION,QUEUE=$QUEUE,WORKER_URL=$WORKER_URL,TASK_SERVICE_ACCOUNT=$(sa task-invoker),DISPATCH_BATCH=25,RECONCILE_GRACE_S=45,ALLOW_FAULTS=1" \
  --update-secrets=DB_URL=dispatcher-db-url:latest >/dev/null
DISP_URL=$($G run services describe foil-dispatcher --region="$REGION" --format='value(status.url)')
$G run services add-iam-policy-binding foil-dispatcher --region="$REGION" \
  --member="serviceAccount:$(sa scheduler-invoker)" --role=roles/run.invoker >/dev/null

echo "== Scheduler: dispatch + reconcile once a minute, OIDC"
ok $G scheduler jobs create http prepress-dispatch --location="$REGION" --schedule="* * * * *" \
  --uri="$DISP_URL/dispatch" --http-method=POST \
  --oidc-service-account-email="$(sa scheduler-invoker)" --oidc-token-audience="$DISP_URL" \
  --attempt-deadline=120s

echo "== GitHub deploys through Workload Identity Federation (one workflow file, main branch, repository pinned by id)"
ok $G iam workload-identity-pools create github --location=global --display-name="GitHub Actions"
# workflow_ref names the workflow file that runs (job_workflow_ref is meant for reusable workflows).
WIF_MAPPING="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_id=assertion.repository_id,attribute.ref=assertion.ref,attribute.workflow_ref=assertion.workflow_ref"
WIF_CONDITION="assertion.repository_id=='$GITHUB_REPO_ID' && assertion.ref=='refs/heads/main' && assertion.workflow_ref=='$GITHUB_REPO/.github/workflows/deploy.yml@refs/heads/main'"
if $G iam workload-identity-pools providers describe github --location=global --workload-identity-pool=github >/dev/null 2>&1; then
  $G iam workload-identity-pools providers update-oidc github --location=global --workload-identity-pool=github \
    --attribute-mapping="$WIF_MAPPING" --attribute-condition="$WIF_CONDITION" >/dev/null
else
  $G iam workload-identity-pools providers create-oidc github --location=global --workload-identity-pool=github \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$WIF_MAPPING" --attribute-condition="$WIF_CONDITION" >/dev/null
fi
GOT=$($G iam workload-identity-pools providers describe github --location=global --workload-identity-pool=github \
        --format='value(attributeCondition)')
if [ "$GOT" != "$WIF_CONDITION" ]; then
  echo "WIF provider condition is not the expected one:"; echo "  got:      $GOT"; echo "  expected: $WIF_CONDITION"; exit 1
fi
$G iam service-accounts add-iam-policy-binding "$(sa deployer)" --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PNUM/locations/global/workloadIdentityPools/github/attribute.repository_id/$GITHUB_REPO_ID" >/dev/null
# Deployer: push to one repository, update two services, act as their two runtime identities.
$G artifacts repositories add-iam-policy-binding $AR --location="$REGION" \
  --member="serviceAccount:$(sa deployer)" --role=roles/artifactregistry.writer >/dev/null
for s in foil-worker foil-dispatcher; do
  $G run services add-iam-policy-binding $s --region="$REGION" \
    --member="serviceAccount:$(sa deployer)" --role=roles/run.developer >/dev/null
done
for a in worker-runtime dispatcher-runtime; do
  $G iam service-accounts add-iam-policy-binding "$(sa $a)" \
    --member="serviceAccount:$(sa deployer)" --role=roles/iam.serviceAccountUser >/dev/null
done

echo "== Budget alert at 5 USD (a notification, not a cap), with cost updates to Pub/Sub"
ok $G pubsub topics create budget-cost
ok $G pubsub subscriptions create budget-cost-pull --topic=budget-cost --message-retention-duration=7d
if ! gcloud billing budgets list --billing-project="$PROJECT" --billing-account="$BILLING_ACCOUNT" --format='value(displayName)' | grep -qx "foil-plate-jobs test"; then
  gcloud billing budgets create --billing-project="$PROJECT" --billing-account="$BILLING_ACCOUNT" --display-name="foil-plate-jobs test" \
    --budget-amount=5USD --filter-projects="projects/$PROJECT" \
    --threshold-rule=percent=0.2 --threshold-rule=percent=0.5 --threshold-rule=percent=1.0 \
    --notifications-rule-pubsub-topic="projects/$PROJECT/topics/budget-cost" >/dev/null
fi

echo "WORKER_URL=$WORKER_URL"
echo "DISPATCHER_URL=$DISP_URL"
echo "WIF_PROVIDER=projects/$PNUM/locations/global/workloadIdentityPools/github/providers/github"
echo "DEPLOYER=$(sa deployer)"
