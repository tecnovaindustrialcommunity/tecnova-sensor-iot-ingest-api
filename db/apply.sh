#!/usr/bin/env bash
# Creates the database objects in Cloud SQL: runs db/schema.sql and db/interface.sql
# (and db/seed.sql with --seed) as the postgres user. Safe to run more than once.
#
# Run it from Cloud Shell, which has psql and gcloud:
#   ./db/apply.sh            tables, view and permissions
#   ./db/apply.sh --seed     the same plus the example sensors
#
# It asks for the password of the postgres user. "gcloud sql connect" allows Cloud Shell's
# IP to reach the instance for 5 minutes and then removes it.
#   INSTANCE=agroclima-pg  DB_NAME=agroclima   (defaults)
set -euo pipefail

INSTANCE="${INSTANCE:-agroclima-pg}"
DB_NAME="${DB_NAME:-agroclima}"
cd "$(dirname "$0")"

command -v psql >/dev/null || { echo "psql not found. Run this from Cloud Shell, or install the PostgreSQL client."; exit 1; }

files=(schema.sql interface.sql)
[[ "${1:-}" == "--seed" ]] && files+=(seed.sql)

{
  echo '\set ON_ERROR_STOP on'
  for f in "${files[@]}"; do
    echo "\\echo == ${f}"
    cat "$f"
    echo
  done
  echo '\echo == done'
  echo "SELECT table_schema, table_name, table_type FROM information_schema.tables
        WHERE table_schema IN ('registry', 'ingest') ORDER BY 1, 2;"
} | gcloud sql connect "$INSTANCE" --user=postgres --database="$DB_NAME" --quiet
