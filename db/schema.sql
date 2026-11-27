-- Reference schema for the sensor registry. Run it only on an empty database;
-- if you already have your own registry, skip this file and adapt db/interface.sql.
--
--   psql "host=... dbname=agroclima user=postgres" -f db/schema.sql

-- Needed so the EXCLUDE constraint can compare sensor_id (text) with "=" in a GiST index.
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA IF NOT EXISTS registry;

-- A gateway is the device that sends the batches (one API token per gateway).
CREATE TABLE IF NOT EXISTS registry.gateways (
  gateway_id  text PRIMARY KEY,
  hardware_id text UNIQUE,              -- e.g. MAC or serial number of the board
  description text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- A physical air sensor (measures air_temperature, air_relative_humidity, global_radiation, ppfd).
CREATE TABLE IF NOT EXISTS registry.sensors (
  sensor_id     text PRIMARY KEY,
  model         text,
  serial_number text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Where and on which gateway a sensor was installed, and when.
-- Moving a sensor = closing the current row (valid_to) and inserting a new one.
CREATE TABLE IF NOT EXISTS registry.sensor_installations (
  installation_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sensor_id           text NOT NULL REFERENCES registry.sensors,
  gateway_id          text NOT NULL REFERENCES registry.gateways,
  site_id             text NOT NULL,     -- farm
  zone_id             text,              -- greenhouse / sector
  feature_of_interest text,              -- what is measured, e.g. 'greenhouse-1-air'
  height_m            real,              -- height above ground
  position            text,              -- free text, e.g. 'centre of row 3, above canopy'
  config              jsonb NOT NULL DEFAULT '{}',  -- e.g. radiation shield, pyranometer class
  valid_from          timestamptz NOT NULL,
  valid_to            timestamptz,       -- NULL = still installed
  CHECK (valid_to IS NULL OR valid_to > valid_from),
  -- A sensor cannot be in two places at the same time.
  EXCLUDE USING gist (sensor_id WITH =, tstzrange(valid_from, valid_to) WITH &&)
);

CREATE INDEX IF NOT EXISTS sensor_installations_gateway_idx
  ON registry.sensor_installations (gateway_id);
