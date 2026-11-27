#!/usr/bin/env bash
# Deploys agroclima-ingest-api to Google Cloud.
#   Devices -> Cloud Run (validation + sensor registry) -> Cloud SQL Postgres, table ingest.readings
#
# Usage:
#   PROJECT_ID=my-project CLOUDSQL_INSTANCE=my-project:europe-southwest1:my-instance DB_PASSWORD=... ./deploy.sh
#
#   CLOUDSQL_INSTANCE  connection name of an existing Cloud SQL Postgres instance (PROJECT:REGION:INSTANCE)
#   DB_PASSWORD        password of the database user ingest_api. Required the first time; passing it
#                      again changes the password and adds a new version of the secret.
#   REGISTRY_MODE      warn (default) | enforce | off
#   DB_NAME=agroclima  DB_USER=ingest_api
#
# Requires gateway-tokens.json in this folder (create it with tools/new_token.py).
# The tables are created by db/*.sql (see README), not by this script.
# Safe to run more than once: steps that already exist are skipped.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID}"
CLOUDSQL_INSTANCE="${CLOUDSQL_INSTANCE:?Set CLOUDSQL_INSTANCE (PROJECT:REGION:INSTANCE)}"
REGION="${REGION:-europe-southwest1}"   # Madrid
DB_NAME="${DB_NAME:-agroclima}"
DB_USER="${DB_USER:-ingest_api}"
SERVICE="agroclima-ingest-api"
SECRET="gateway-tokens"
DB_SECRET="ingest-db-password"
SA_NAME="ingest-api"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
INSTANCE_NAME="${CLOUDSQL_INSTANCE##*:}"
[[ "$CLOUDSQL_INSTANCE" == *:*:* ]] || { echo "CLOUDSQL_INSTANCE must look like PROJECT:REGION:INSTANCE"; exit 1; }

cd "$(dirname "$0")"
[[ -f gateway-tokens.json ]] || { echo "Missing gateway-tokens.json. Run: python3 tools/new_token.py <gatewayId>"; exit 1; }

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== 1/5 Enabling APIs"
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

echo "== 2/5 Database ${DB_NAME} and user ${DB_USER} on ${INSTANCE_NAME}"
gcloud sql databases describe "$DB_NAME" --instance="$INSTANCE_NAME" >/dev/null 2>&1 || \
  gcloud sql databases create "$DB_NAME" --instance="$INSTANCE_NAME"
if gcloud sql users list --instance="$INSTANCE_NAME" --format='value(name)' | grep -qx "$DB_USER"; then
  if [[ -n "${DB_PASSWORD:-}" ]]; then
    gcloud sql users set-password "$DB_USER" --instance="$INSTANCE_NAME" --password="$DB_PASSWORD"
  fi
else
  [[ -n "${DB_PASSWORD:-}" ]] || { echo "User ${DB_USER} does not exist yet: set DB_PASSWORD"; exit 1; }
  gcloud sql users create "$DB_USER" --instance="$INSTANCE_NAME" --password="$DB_PASSWORD"
fi

echo "== 3/5 Secrets (token hashes, database password)"
if gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  gcloud secrets versions add "$SECRET" --data-file=gateway-tokens.json
else
  gcloud secrets create "$SECRET" --replication-policy=automatic --data-file=gateway-tokens.json
fi
if gcloud secrets describe "$DB_SECRET" >/dev/null 2>&1; then
  if [[ -n "${DB_PASSWORD:-}" ]]; then
    printf '%s' "$DB_PASSWORD" | gcloud secrets versions add "$DB_SECRET" --data-file=-
  fi
else
  [[ -n "${DB_PASSWORD:-}" ]] || { echo "Secret ${DB_SECRET} does not exist yet: set DB_PASSWORD"; exit 1; }
  printf '%s' "$DB_PASSWORD" | gcloud secrets create "$DB_SECRET" --replication-policy=automatic --data-file=-
fi

echo "== 4/5 Service account for the API"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name="agroclima ingest API"
for s in "$SECRET" "$DB_SECRET"; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
done
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" --role="roles/cloudsql.client" --condition=None >/dev/null
# "gcloud run deploy --source" builds the image with Cloud Build, which runs as the Compute Engine
# default service account. New projects no longer give it any role, so the build cannot read the
# uploaded source ("storage.objects.get denied"). This role lets it read the source, push the
# image to Artifact Registry and write build logs.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder" --condition=None >/dev/null

echo "== 5/5 Cloud Run"
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA" \
  --allow-unauthenticated \
  --add-cloudsql-instances "$CLOUDSQL_INSTANCE" \
  --set-env-vars "TOKENS_FILE=/secrets/gateway-tokens.json,CLOUDSQL_INSTANCE=${CLOUDSQL_INSTANCE},DB_NAME=${DB_NAME},DB_USER=${DB_USER},REGISTRY_MODE=${REGISTRY_MODE:-warn}" \
  --set-secrets "/secrets/gateway-tokens.json=${SECRET}:latest,DB_PASSWORD=${DB_SECRET}:latest" \
  --memory 512Mi --cpu 1 --concurrency 80 \
  --min-instances 0 --max-instances 3

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "Deployed: ${URL}"
echo "Test:     curl -s ${URL}/health"
