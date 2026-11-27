-- Example data matching examples/air-station.json. Safe to run more than once.

BEGIN;

INSERT INTO registry.gateways (gateway_id, hardware_id, description) VALUES
  ('gw-gh1-01',   'esp32-a4cf12e8b301', 'Greenhouse 1 gateway'),
  ('gw-meteo-01', 'rpi4-dca632a1f07e',  'Outdoor weather station gateway')
ON CONFLICT DO NOTHING;

INSERT INTO registry.sensors (sensor_id, model, serial_number) VALUES
  ('gh1-air01',   'air-station-v1', 'AS1-000101'),
  ('gh1-air02',   'air-station-v1', 'AS1-000102'),
  ('meteo01-air', 'air-station-v1', 'AS1-000201')
ON CONFLICT DO NOTHING;

INSERT INTO registry.sensor_installations
  (sensor_id, gateway_id, site_id, zone_id, feature_of_interest, height_m, position, config, valid_from, valid_to)
VALUES
  ('gh1-air01', 'gw-gh1-01', 'farm-01', 'gh1', 'greenhouse-1-air', 2.0,
   'centre of the greenhouse, above canopy',
   '{"radiation_shield": "aspirated", "pyranometer_class": "ISO 9060 class B"}',
   '2026-01-15T00:00:00Z', NULL),

  -- gh1-air02 was outdoors on gw-meteo-01 and moved into greenhouse 1 on 2026-11-01.
  ('gh1-air02', 'gw-meteo-01', 'farm-01', 'outdoor', 'outdoor-air', 2.0,
   'weather mast, temporary',
   '{"radiation_shield": "passive"}',
   '2026-06-01T00:00:00Z', '2026-11-01T00:00:00Z'),
  ('gh1-air02', 'gw-gh1-01', 'farm-01', 'gh1', 'greenhouse-1-air', 1.5,
   'north end, row 12, above canopy',
   '{"radiation_shield": "aspirated"}',
   '2026-11-01T00:00:00Z', NULL),

  ('meteo01-air', 'gw-meteo-01', 'farm-01', 'outdoor', 'outdoor-air', 2.0,
   'weather mast',
   '{"radiation_shield": "aspirated", "pyranometer_class": "ISO 9060 class A"}',
   '2026-01-15T00:00:00Z', NULL)
ON CONFLICT DO NOTHING;

COMMIT;
