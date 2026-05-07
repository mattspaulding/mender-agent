#!/usr/bin/env bash
# One-time secret setup for Mender's Cloud Run deploy.
# Reads from .env (local), pushes to Secret Manager, grants the Cloud
# Run service agent secretAccessor.

set -euo pipefail
cd "$(dirname "$0")/.."

source deploy/config.env

# Helper: create-or-update a secret with a value from the local .env.
upsert_secret() {
  local name="$1"
  local value="$2"
  if [ -z "$value" ]; then
    echo "skipping $name (empty value)" >&2
    return
  fi
  if gcloud secrets describe "$name" --project="$PROJECT_ID" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" \
      --project="$PROJECT_ID" --data-file=- >/dev/null
    echo "updated $name"
  else
    printf '%s' "$value" | gcloud secrets create "$name" \
      --project="$PROJECT_ID" --replication-policy=automatic --data-file=- >/dev/null
    echo "created $name"
  fi
}

# Pull values from the LOCAL .env so we don't echo them.
PHOENIX_API_KEY="$(grep '^PHOENIX_API_KEY=' .env | cut -d= -f2-)"
SLACK_INCOMING_WEBHOOK="$(grep '^SLACK_INCOMING_WEBHOOK=' .env | cut -d= -f2-)"
SLACK_SIGNING_SECRET="$(grep '^SLACK_SIGNING_SECRET=' .env | cut -d= -f2-)"

upsert_secret "${SECRET_PHOENIX_API_KEY}" "$PHOENIX_API_KEY"
upsert_secret "${SECRET_SLACK_WEBHOOK}" "$SLACK_INCOMING_WEBHOOK"
upsert_secret "${SECRET_SLACK_SIGNING}" "$SLACK_SIGNING_SECRET"

# Grant Cloud Run's compute SA access. (Default service account is
# {project_number}-compute@developer.gserviceaccount.com.)
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for s in "$SECRET_PHOENIX_API_KEY" "$SECRET_SLACK_WEBHOOK" "$SECRET_SLACK_SIGNING"; do
  gcloud secrets add-iam-policy-binding "$s" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${SA}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null 2>&1 || true
done

# Same SA needs Firestore access.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" \
  --role="roles/datastore.user" >/dev/null 2>&1 || true

echo "✓ secrets + IAM ready for ${SA}"
