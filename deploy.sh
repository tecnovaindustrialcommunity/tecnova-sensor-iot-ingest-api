#!/usr/bin/env bash
# Deploys agroclima-ingest-api to Google Cloud.
#   Devices -> Cloud Run (validation) -> Pub/Sub -> BigQuery subscription -> table agro.readings
#
# Usage:   PROJECT_ID=my-project ./deploy.sh
# Requires gateway-tokens.json in this folder (create it with tools/new_token.py).
# Safe to run more than once: steps that already exist are skipped.
#
# Optional sensor registry in Cloud SQL (Postgres), see README:
#   CLOUDSQL_INSTANCE=PROJECT:REGION:INSTANCE  connection name of the instance
#   DB_PASSWORD=...     password of ingest_api (required the first time; passing it again adds a new version)
#   REGISTRY_MODE=warn  warn (default) | enforce | off
#   DB_NAME=agroclima  DB_USER=ingest_api
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-europe-southwest1}"   # Madrid
SERVICE="agroclima-ingest-api"
DATASET="agro"
TABLE="readings"
TOPIC="readings-raw"
DLQ_TOPIC="readings-dlq"
BQ_SUB="readings-to-bq"
SECRET="gateway-tokens"
SA_NAME="ingest-api"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
CLOUDSQL_INSTANCE="${CLOUDSQL_INSTANCE:-}"
DB_SECRET="registry-db-password"

cd "$(dirname "$0")"
[[ -f gateway-tokens.json ]] || { echo "Missing gateway-tokens.json. Run: python3 tools/new_token.py <gatewayId>"; exit 1; }

gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

echo "== 1/7 Enabling APIs"
gcloud services enable run.googleapis.com pubsub.googleapis.com bigquery.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

echo "== 2/7 BigQuery dataset, table and views"
bq --location="$REGION" mk -f --dataset "${PROJECT_ID}:${DATASET}"
bq mk -f --table \
  --time_partitioning_field=ts --time_partitioning_type=DAY \
  --clustering_fields=sensor_id,property \
  "${PROJECT_ID}:${DATASET}.${TABLE}" bq_schema.json
sed "s/PROJECT_ID/${PROJECT_ID}/g" views.sql | bq query --use_legacy_sql=false --location="$REGION"

echo "== 3/7 Pub/Sub topics"
gcloud pubsub topics describe "$TOPIC" >/dev/null 2>&1 || gcloud pubsub topics create "$TOPIC"
gcloud pubsub topics describe "$DLQ_TOPIC" >/dev/null 2>&1 || gcloud pubsub topics create "$DLQ_TOPIC"
# Pull subscription to inspect messages BigQuery rejected.
gcloud pubsub subscriptions describe "${DLQ_TOPIC}-sub" >/dev/null 2>&1 || \
  gcloud pubsub subscriptions create "${DLQ_TOPIC}-sub" --topic="$DLQ_TOPIC" --message-retention-duration=7d

echo "== 4/7 Pub/Sub -> BigQuery subscription"
# The Pub/Sub service agent writes to BigQuery and handles the dead-letter topic.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PUBSUB_SA}" --role="roles/bigquery.dataEditor" --condition=None >/dev/null
gcloud pubsub topics add-iam-policy-binding "$DLQ_TOPIC" \
  --member="serviceAccount:${PUBSUB_SA}" --role="roles/pubsub.publisher" >/dev/null
gcloud pubsub subscriptions describe "$BQ_SUB" >/dev/null 2>&1 || \
  gcloud pubsub subscriptions create "$BQ_SUB" \
    --topic="$TOPIC" \
    --bigquery-table="${PROJECT_ID}:${DATASET}.${TABLE}" \
    --use-table-schema \
    --dead-letter-topic="$DLQ_TOPIC" \
    --max-delivery-attempts=5
gcloud pubsub subscriptions add-iam-policy-binding "$BQ_SUB" \
  --member="serviceAccount:${PUBSUB_SA}" --role="roles/pubsub.subscriber" >/dev/null

echo "== 5/7 Secret with the token hashes"
if gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  gcloud secrets versions add "$SECRET" --data-file=gateway-tokens.json
else
  gcloud secrets create "$SECRET" --replication-policy=automatic --data-file=gateway-tokens.json
fi

echo "== 6/7 Service account for the API"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name="agroclima ingest API"
gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
  --member="serviceAccount:${SA}" --role="roles/pubsub.publisher" >/dev/null
gcloud secrets add-iam-policy-binding "$SECRET" \
  --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" >/dev/null

ENV_VARS="PROJECT_ID=${PROJECT_ID},TOPIC_ID=${TOPIC},TOKENS_FILE=/secrets/gateway-tokens.json"
SECRETS="/secrets/gateway-tokens.json=${SECRET}:latest"
SQL_FLAGS=()
if [[ -n "$CLOUDSQL_INSTANCE" ]]; then
  echo "== 6b/7 Sensor registry (Cloud SQL ${CLOUDSQL_INSTANCE})"
  gcloud services enable sqladmin.googleapis.com
  if gcloud secrets describe "$DB_SECRET" >/dev/null 2>&1; then
    if [[ -n "${DB_PASSWORD:-}" ]]; then
      printf '%s' "$DB_PASSWORD" | gcloud secrets versions add "$DB_SECRET" --data-file=-
    fi
  else
    [[ -n "${DB_PASSWORD:-}" ]] || { echo "First deploy with CLOUDSQL_INSTANCE: set DB_PASSWORD (password of ${DB_USER:-ingest_api})"; exit 1; }
    printf '%s' "$DB_PASSWORD" | gcloud secrets create "$DB_SECRET" --replication-policy=automatic --data-file=-
  fi
  gcloud secrets add-iam-policy-binding "$DB_SECRET" \
    --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA}" --role="roles/cloudsql.client" --condition=None >/dev/null
  ENV_VARS+=",CLOUDSQL_INSTANCE=${CLOUDSQL_INSTANCE},DB_NAME=${DB_NAME:-agroclima},DB_USER=${DB_USER:-ingest_api},REGISTRY_MODE=${REGISTRY_MODE:-warn}"
  SECRETS+=",DB_PASSWORD=${DB_SECRET}:latest"
  SQL_FLAGS=(--add-cloudsql-instances "$CLOUDSQL_INSTANCE")
fi

echo "== 7/7 Cloud Run"
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA" \
  --allow-unauthenticated \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "$SECRETS" \
  ${SQL_FLAGS[@]+"${SQL_FLAGS[@]}"} \
  --memory 512Mi --cpu 1 --concurrency 80 \
  --min-instances 0 --max-instances 3

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "Deployed: ${URL}"
echo "Test:     curl -s ${URL}/healthz"
