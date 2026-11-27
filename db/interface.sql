-- The only thing the API reads. If you already have your own registry, adapt the
-- SELECT below to your tables and keep the column names and types.
-- The user ingest_api must exist first:
--   gcloud sql users create ingest_api --instance=<instance> --password=...

CREATE SCHEMA IF NOT EXISTS ingest;

CREATE OR REPLACE VIEW ingest.sensor_gateway AS
SELECT
  sensor_id::text         AS sensor_id,
  gateway_id::text        AS gateway_id,
  valid_from::timestamptz AS valid_from,
  valid_to::timestamptz   AS valid_to    -- NULL = still assigned
FROM registry.sensor_installations;

-- ingest_api only reads the view, not the registry tables
-- (a view runs with its owner's permissions).
GRANT USAGE ON SCHEMA ingest TO ingest_api;
GRANT SELECT ON ingest.sensor_gateway TO ingest_api;
