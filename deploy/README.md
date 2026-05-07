# Deploy

Mender + FinPay deploy as two Cloud Run services from a single image,
plus a Cloud Scheduler job that fires Mender's heartbeat every 15
minutes. Secrets live in Secret Manager. Incident state lives in
Firestore.

## One-time setup

1. Enable the APIs (Phase 1 already enabled most of these; this is the
   superset Cloud Run + Scheduler need).

   ```bash
   gcloud services enable \
     run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
     cloudscheduler.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
     --project=rapid-agent-hackathon-495414
   ```

2. Create a Firestore database (Native mode, single region):

   ```bash
   gcloud firestore databases create \
     --location=us-central1 \
     --type=firestore-native \
     --project=rapid-agent-hackathon-495414
   ```

3. Copy `deploy/config.env.example` → `deploy/config.env` and edit
   `PROJECT_ID`, `PHOENIX_*` paths.

4. Push secrets + grant IAM:

   ```bash
   ./deploy/setup-secrets.sh
   ```

## Deploy

```bash
./deploy/deploy.sh
```

The script:

- ensures the Artifact Registry repo exists,
- builds + pushes the image with `gcloud builds submit`,
- deploys `finpay` (separate service, command=`finpay-serve`),
- deploys `mender` (web + heartbeat handler),
- updates the `mender-heartbeat` Cloud Scheduler job to hit
  `${MENDER_URL}/heartbeat` every 15 min,
- prints the public URLs.

The output's `mender` URL is what you submit to Devpost as the hosted
application. The Slack app's interactivity Request URL must point at
`${MENDER_URL}/api/approve-patch`.

## Subsequent deploys

`./deploy/deploy.sh` is idempotent — it bumps the image tag and rolls
the services. Use `--build-only` or `--skip-build` to split the cycle.

## Manually firing a heartbeat

```bash
curl -X POST "${MENDER_URL}/heartbeat" -H "Content-Length: 0"
```

(The endpoint takes ~10–20 minutes per cycle on `gemini-3-flash-preview`
right now; the Scheduler timeout is 900s which is enough for most runs
but too tight for full eval-set generation when traffic is sparse.)

## Layout

```
deploy/
├── README.md           # this file
├── config.env.example  # → copy to config.env, gitignored
├── deploy.sh           # build + deploy services + scheduler
└── setup-secrets.sh    # one-time secret + IAM setup
```
