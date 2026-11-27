-- Everything the API touches, in schema "ingest": it reads ingest.sensor_gateway and
-- inserts into ingest.readings. Run it always (after db/schema.sql if you use that).
-- If you already have your own registry, adapt the SELECT of the view to your tables
-- and keep the column names and types.
--
-- The user ingest_api must exist first:
--   gcloud sql users create ingest_api --instance=<instance> --password=...

CREATE SCHEMA IF NOT EXISTS ingest;

-- Sensor -> gateway assignments over time.
CREATE OR REPLACE VIEW ingest.sensor_gateway AS
SELECT
  sensor_id::text         AS sensor_id,
  gateway_id::text        AS gateway_id,
  valid_from::timestamptz AS valid_from,
  valid_to::timestamptz   AS valid_to    -- NULL = still assigned
FROM registry.sensor_installations;

-- One row per reading. A gateway that resends a batch sends the same
-- (sensor_id, property, ts) again; the primary key makes the API skip it.
CREATE TABLE IF NOT EXISTS ingest.readings (
  sensor_id   text             NOT NULL,
  property    text             NOT NULL,  -- code from catalog.json
  ts          timestamptz      NOT NULL,  -- measurement time
  value       double precision NOT NULL,
  unit        text             NOT NULL,  -- UCUM
  gateway_id  text             NOT NULL,
  aggregation text,                       -- mean | min | max | last
  period_s    integer,                    -- aggregation period in seconds
  sent_at     timestamptz,
  received_at timestamptz      NOT NULL DEFAULT now(),
  PRIMARY KEY (sensor_id, property, ts)
);

-- Time-range queries over all sensors (cheap index for append-only, time-ordered data).
CREATE INDEX IF NOT EXISTS readings_ts_brin ON ingest.readings USING brin (ts);

-- ingest_api reads only the view (a view runs with its owner's permissions, so it needs
-- no access to the registry tables) and can only add readings, not change or delete them.
GRANT USAGE ON SCHEMA ingest TO ingest_api;
GRANT SELECT ON ingest.sensor_gateway TO ingest_api;
GRANT INSERT ON ingest.readings TO ingest_api;
