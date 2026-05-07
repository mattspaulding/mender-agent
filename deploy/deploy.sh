#!/usr/bin/env bash
# Deploy Mender + FinPay to Cloud Run, plus the Scheduler heartbeat.
#
# Idempotent — re-running upgrades the services in place. Reads config
# from `deploy/config.env` (copy from `deploy/config.env.example`).
#
# Usage:
#   ./deploy/deploy.sh              # build, push, deploy everything
#   ./deploy/deploy.sh --build-only # only build + push the image
#   ./deploy/deploy.sh --skip-build # only deploy (use existing image tag)

set -euo pipefail

cd "$(dirname "$0")/.."

CONFIG="${CONFIG:-deploy/config.env}"
if [ ! -f "$CONFIG" ]; then
  echo "missing $CONFIG (copy deploy/config.env.example and edit)" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG"

: "${PROJECT_ID:?set in deploy/config.env}"
: "${REGION:=us-central1}"
: "${REPO:=mender}"
: "${IMAGE_NAME:=mender}"
: "${IMAGE_TAG:=$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d-%H%M%S)}"
: "${MENDER_SERVICE:=mender}"
: "${FINPAY_SERVICE:=finpay}"
: "${SCHEDULER_JOB:=mender-heartbeat}"
: "${SCHEDULER_CRON:=*/15 * * * *}"
: "${SECRET_PHOENIX_API_KEY:=phoenix-api-key}"
: "${SECRET_SLACK_WEBHOOK:=slack-incoming-webhook}"
: "${SECRET_SLACK_SIGNING:=slack-signing-secret}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${IMAGE_NAME}:${IMAGE_TAG}"

build_only=false; skip_build=false
for arg in "$@"; do
  case "$arg" in
    --build-only)  build_only=true ;;
    --skip-build)  skip_build=true ;;
  esac
done

echo "==> project   ${PROJECT_ID}"
echo "==> region    ${REGION}"
echo "==> image     ${IMAGE}"

if [ "$skip_build" = false ]; then
  # Make sure the Artifact Registry repo + APIs exist (idempotent).
  gcloud artifacts repositories describe "$REPO" \
    --project="$PROJECT_ID" --location="$REGION" >/dev/null 2>&1 \
    || gcloud artifacts repositories create "$REPO" \
        --project="$PROJECT_ID" --location="$REGION" \
        --repository-format=docker --description="Mender container repo"

  echo "==> building + pushing"
  gcloud builds submit \
    --project="$PROJECT_ID" \
    --tag="$IMAGE" \
    --region="$REGION"
fi

if [ "$build_only" = true ]; then
  echo "==> build-only; skipping deploy"
  exit 0
fi

# Common service-account flags — Cloud Run's default SA is fine for v1.
COMMON_FLAGS=(
  --project="$PROJECT_ID"
  --region="$REGION"
  --image="$IMAGE"
  --platform=managed
  --allow-unauthenticated
  --min-instances=0
  --max-instances=2
)

# Secrets are mounted as env vars from Secret Manager.
SECRET_FLAGS=(
  --update-secrets="PHOENIX_API_KEY=${SECRET_PHOENIX_API_KEY}:latest"
  --update-secrets="SLACK_INCOMING_WEBHOOK=${SECRET_SLACK_WEBHOOK}:latest"
  --update-secrets="SLACK_SIGNING_SECRET=${SECRET_SLACK_SIGNING}:latest"
)

echo "==> deploying ${FINPAY_SERVICE}"
gcloud run deploy "$FINPAY_SERVICE" "${COMMON_FLAGS[@]}" \
  --command="finpay-serve" \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},\
GOOGLE_CLOUD_LOCATION=${REGION},\
GOOGLE_GENAI_USE_VERTEXAI=true,\
FINPAY_PROMPT_VERSION=${FINPAY_PROMPT_VERSION:-v2},\
MENDER_USE_FIRESTORE_STATE=true,\
PHOENIX_COLLECTOR_ENDPOINT=${PHOENIX_COLLECTOR_ENDPOINT:-https://app.phoenix.arize.com}" \
  --update-secrets="PHOENIX_API_KEY=${SECRET_PHOENIX_API_KEY}:latest"

FINPAY_URL=$(gcloud run services describe "$FINPAY_SERVICE" \
  --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')
echo "==> ${FINPAY_SERVICE} -> ${FINPAY_URL}"

echo "==> deploying ${MENDER_SERVICE}"
gcloud run deploy "$MENDER_SERVICE" "${COMMON_FLAGS[@]}" \
  --command="mender-web" \
  --timeout=900 \
  --memory=1Gi \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},\
GOOGLE_CLOUD_LOCATION=${REGION},\
GOOGLE_GENAI_USE_VERTEXAI=true,\
PHOENIX_COLLECTOR_ENDPOINT=${PHOENIX_COLLECTOR_ENDPOINT:-https://app.phoenix.arize.com},\
PHOENIX_BASE_URL=${PHOENIX_BASE_URL:-https://app.phoenix.arize.com},\
MENDER_INCIDENTS_BACKEND=firestore,\
MENDER_USE_FIRESTORE_STATE=true,\
FINPAY_URL=${FINPAY_URL}" \
  "${SECRET_FLAGS[@]}"

MENDER_URL=$(gcloud run services describe "$MENDER_SERVICE" \
  --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')
echo "==> ${MENDER_SERVICE} -> ${MENDER_URL}"

# Update Cloud Run env again so the public URL is known to the service
# itself (used in Slack message links).
gcloud run services update "$MENDER_SERVICE" \
  --project="$PROJECT_ID" --region="$REGION" \
  --update-env-vars="MENDER_WEB_PUBLIC_URL=${MENDER_URL}" >/dev/null

echo "==> Cloud Scheduler job ${SCHEDULER_JOB}"
if gcloud scheduler jobs describe "$SCHEDULER_JOB" \
    --project="$PROJECT_ID" --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$SCHEDULER_JOB" \
    --project="$PROJECT_ID" --location="$REGION" \
    --schedule="$SCHEDULER_CRON" \
    --uri="${MENDER_URL}/heartbeat" \
    --http-method=POST \
    --attempt-deadline=900s >/dev/null
else
  gcloud scheduler jobs create http "$SCHEDULER_JOB" \
    --project="$PROJECT_ID" --location="$REGION" \
    --schedule="$SCHEDULER_CRON" \
    --uri="${MENDER_URL}/heartbeat" \
    --http-method=POST \
    --attempt-deadline=900s >/dev/null
fi

cat <<EOF

✓ deploy complete
  finpay : ${FINPAY_URL}
  mender : ${MENDER_URL}
  scheduler: ${SCHEDULER_JOB} (${SCHEDULER_CRON})

Next:
  - Open ${MENDER_URL} (Devpost's hosted-application URL).
  - Configure your Slack app's interactivity Request URL:
      ${MENDER_URL}/api/approve-patch
EOF
