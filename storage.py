"""Postgres (Cloud SQL) storage: writes readings and reads the sensor registry.

On Cloud Run it connects through the Cloud SQL socket /cloudsql/$CLOUDSQL_INSTANCE;
locally, set DB_HOST (and DB_PORT) instead.
"""
import os
from datetime import datetime
from typing import Optional

from psycopg_pool import ConnectionPool

INSERT_READING = """
INSERT INTO ingest.readings
  (gateway_id, sensor_id, property, value, unit, ts, aggregation, period_s, sent_at, received_at)
VALUES
  (%(gateway_id)s, %(sensor_id)s, %(property)s, %(value)s, %(unit)s, %(ts)s,
   %(aggregation)s, %(period_s)s, %(sent_at)s, %(received_at)s)
ON CONFLICT DO NOTHING
"""
# No conflict target on purpose: naming the columns would need SELECT on them, and ingest_api
# only has INSERT. The primary key (sensor_id, property, ts) is the table's only unique
# constraint, so this skips exactly the readings that are already stored.

SELECT_ASSIGNMENTS = """
SELECT sensor_id, gateway_id, valid_from, valid_to
FROM ingest.sensor_gateway
WHERE valid_to IS NULL OR valid_to > now() - make_interval(days => %s)
"""


class Database:
    def __init__(self, conninfo: dict, lookback_days: int = 30, max_size: int = 5):
        self.lookback_days = lookback_days
        # Not opened here: main.py opens it when the app starts, so importing never blocks.
        self.pool = ConnectionPool(
            kwargs=conninfo, min_size=1, max_size=max_size, timeout=10,
            open=False, check=ConnectionPool.check_connection, name="agroclima",
        )

    def open(self) -> None:
        self.pool.open(wait=False)  # connects in the background; a DB that is down does not stop startup

    def close(self) -> None:
        self.pool.close()

    def insert_readings(self, rows: list[dict]) -> None:
        """All rows in one transaction. Readings already stored (same sensor, property, ts) are skipped."""
        with self.pool.connection() as conn:  # commits on success, rolls back on error
            conn.cursor().executemany(INSERT_READING, rows)

    def sensor_assignments(self) -> list[tuple[str, str, datetime, Optional[datetime]]]:
        """Current assignments plus those closed in the last lookback_days days."""
        with self.pool.connection() as conn:
            return conn.execute(SELECT_ASSIGNMENTS, (self.lookback_days,)).fetchall()


def conninfo_from_env() -> dict:
    instance = os.environ.get("CLOUDSQL_INSTANCE")
    if not instance and not os.environ.get("DB_HOST"):
        raise RuntimeError("set CLOUDSQL_INSTANCE (Cloud Run) or DB_HOST (local) to reach Postgres")
    return {
        "host": f"/cloudsql/{instance}" if instance else os.environ["DB_HOST"],
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ.get("DB_NAME", "agroclima"),
        "user": os.environ.get("DB_USER", "ingest_api"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "connect_timeout": 5,
        "application_name": "agroclima-ingest-api",
    }


def from_env() -> Database:
    return Database(conninfo_from_env(), lookback_days=int(os.environ.get("REGISTRY_LOOKBACK_DAYS", "30")))
