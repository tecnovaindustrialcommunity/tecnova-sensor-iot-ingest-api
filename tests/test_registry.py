"""Sensor registry tests. The first three need no database.

The last one runs against a real Postgres loaded with db/schema.sql, db/interface.sql and
db/seed.sql, only if DB_HOST is set (plus DB_PORT, DB_NAME, DB_USER, DB_PASSWORD).

Run:  python3 tests/test_registry.py   (or: python3 -m pytest tests -q)
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
DB_HOST = os.environ.pop("DB_HOST", None)  # keep test_api's registry off; the last test uses it directly

import test_api  # noqa: E402  (sets up the fake Pub/Sub, tokens and the app)
from test_api import PUBLISHED, load, main, post  # noqa: E402

import registry  # noqa: E402

UTC = timezone.utc
MOVED = datetime(2026, 11, 1, tzinfo=UTC)
ROWS = [
    ("gh1-air01", "gw-gh1-01", datetime(2026, 1, 15, tzinfo=UTC), None),
    ("gh1-air02", "gw-meteo-01", datetime(2026, 6, 1, tzinfo=UTC), MOVED),
    ("gh1-air02", "gw-gh1-01", MOVED, None),
]


def reading(sensor, ts):
    return SimpleNamespace(sensorId=sensor, ts=ts)


class FakeDB:
    def __init__(self, rows):
        self.rows, self.calls, self.down = rows, 0, False

    def __call__(self):
        self.calls += 1
        if self.down:
            raise ConnectionError("db down")
        return list(self.rows)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_modes_and_history():
    try:
        registry.Registry(None, "enforce")
        raise AssertionError("enforce without a database must fail")
    except ValueError:
        pass
    try:
        registry.Registry(FakeDB(ROWS), "strict")
        raise AssertionError("unknown mode must fail")
    except ValueError:
        pass

    reg = registry.Registry(FakeDB(ROWS), "enforce")
    before, after = datetime(2026, 10, 31, 23, 50, tzinfo=UTC), datetime(2026, 11, 1, 0, 10, tzinfo=UTC)
    assert reg.problems("gw-gh1-01", [reading("gh1-air01", after)]) == []
    # gh1-air02 moved from gw-meteo-01 to gw-gh1-01 at MOVED (valid_to is exclusive).
    assert reg.problems("gw-meteo-01", [reading("gh1-air02", before)]) == []
    assert reg.problems("gw-gh1-01", [reading("gh1-air02", after)]) == []
    assert reg.problems("gw-gh1-01", [reading("gh1-air02", MOVED)]) == []
    assert reg.problems("gw-meteo-01", [reading("gh1-air02", MOVED)]) != []
    assert reg.problems("gw-gh1-01", [reading("gh1-air02", before)]) == [
        "sensor 'gh1-air02' is not assigned to gateway 'gw-gh1-01' at 2026-10-31T23:50:00+00:00"]
    # Before the first installation.
    assert reg.problems("gw-gh1-01", [reading("gh1-air01", datetime(2026, 1, 1, tzinfo=UTC))]) != []
    # One message per sensor, however many readings it has.
    batch = [reading("ghost", after)] * 3 + [reading("gh1-air01", after)]
    assert reg.problems("gw-gh1-01", batch) == ["sensor 'ghost' is not in the registry"]


def test_cache_and_db_failures():
    db, clock = FakeDB(ROWS), Clock()
    reg = registry.Registry(db, "enforce", ttl_s=60, retry_s=10, clock=clock)
    now = [reading("gh1-air01", datetime(2026, 11, 26, tzinfo=UTC))]

    # Database down and no copy yet: unavailable, and no retry until retry_s has passed.
    db.down = True
    for _ in range(2):
        try:
            reg.problems("gw-gh1-01", now)
            raise AssertionError("expected RegistryUnavailable")
        except registry.RegistryUnavailable:
            pass
    assert db.calls == 1
    clock.t = 10
    db.down = False
    assert reg.problems("gw-gh1-01", now) == [] and db.calls == 2

    # Cached for ttl_s.
    clock.t = 69
    reg.problems("gw-gh1-01", now)
    assert db.calls == 2

    # Expired + database down: keep the old copy, retry after retry_s.
    clock.t = 70
    db.down = True
    assert reg.problems("gw-gh1-01", now) == [] and db.calls == 3
    clock.t = 79
    reg.problems("gw-gh1-01", now)
    assert db.calls == 3

    # Database back with a new sensor: picked up on the next reload.
    clock.t = 80
    db.down = False
    db.rows = ROWS + [("gh1-air03", "gw-gh1-01", datetime(2026, 11, 20, tzinfo=UTC), None)]
    assert reg.problems("gw-gh1-01", [reading("gh1-air03", now[0].ts)]) == [] and db.calls == 4


def test_api_modes():
    payload = load("air-station.json")
    unknown = load("air-station.json")
    unknown["readings"][0]["sensorId"] = "ghost"
    old = main.REGISTRY
    try:
        # enforce
        main.REGISTRY = registry.Registry(FakeDB(ROWS), "enforce")
        PUBLISHED.clear()
        assert post(payload).status_code == 202 and len(PUBLISHED) == 3
        PUBLISHED.clear()
        r = post(unknown)
        assert r.status_code == 422 and PUBLISHED == []
        assert r.json()["detail"] == {"registry": ["sensor 'ghost' is not in the registry"]}
        assert post(payload, token=test_api.TOKENS["gw-gh1-01"]).status_code == 202
        assert test_api.client.get("/healthz").json() == {"status": "ok", "registry": "enforce"}

        # warn: accepted anyway
        main.REGISTRY = registry.Registry(FakeDB(ROWS), "warn")
        PUBLISHED.clear()
        assert post(unknown).status_code == 202 and len(PUBLISHED) == 3

        # database down: 503 in enforce, accepted in warn
        down = FakeDB(ROWS)
        down.down = True
        main.REGISTRY = registry.Registry(down, "enforce")
        PUBLISHED.clear()
        assert post(payload).status_code == 503 and PUBLISHED == []
        main.REGISTRY = registry.Registry(down, "warn")
        assert post(payload).status_code == 202 and len(PUBLISHED) == 3

        # the registry is checked after the token: a bad token is still 401, not 503/422
        main.REGISTRY = registry.Registry(down, "enforce")
        assert post(payload, token="wrong").status_code == 401
    finally:
        main.REGISTRY = old


def test_postgres_seed():
    if not DB_HOST:
        print("     (skipped test_postgres_seed: DB_HOST not set)")
        return
    # The seed has an installation closed on 2026-11-01: use a wide lookback so this test
    # does not depend on today's date.
    os.environ.update(DB_HOST=DB_HOST, REGISTRY_LOOKBACK_DAYS="36500")
    try:
        reg = registry.Registry(registry.postgres_loader(), "enforce")
    finally:
        os.environ.pop("DB_HOST")
        os.environ.pop("REGISTRY_LOOKBACK_DAYS")
    ts = datetime(2026, 11, 26, 9, tzinfo=UTC)
    assert reg.problems("gw-gh1-01", [reading("gh1-air01", ts), reading("gh1-air02", ts)]) == []
    assert reg.problems("gw-meteo-01", [reading("meteo01-air", ts)]) == []
    assert reg.problems("gw-meteo-01", [reading("gh1-air02", datetime(2026, 10, 1, tzinfo=UTC))]) == []
    assert reg.problems("gw-meteo-01", [reading("gh1-air02", ts)]) != []
    assert reg.problems("gw-gh1-01", [reading("ghost", ts)]) == ["sensor 'ghost' is not in the registry"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} tests passed")
