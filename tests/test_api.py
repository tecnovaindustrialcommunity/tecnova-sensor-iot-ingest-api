"""Local tests without Google Cloud or Postgres: the database is replaced by a fake in memory.

Run:  pip install -r requirements.txt httpx && python3 tests/test_api.py   (or: python3 -m pytest tests -q)
"""
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# --- tokens for the example gateways ---------------------------------------
TOKENS = {gw: f"token-{gw}" for gw in ["gw-gh1-01", "gw-meteo-01"]}
tok_file = Path(tempfile.mkdtemp()) / "gateway-tokens.json"
tok_file.write_text(json.dumps({hashlib.sha256(t.encode()).hexdigest(): gw for gw, t in TOKENS.items()}))
os.environ.update(TOKENS_FILE=str(tok_file), REGISTRY_MODE="off")
os.environ.setdefault("DB_HOST", "unused")  # main.DB is replaced by FakeDB below; its pool is never opened

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


class FakeDB:
    """Behaves like storage.Database: primary key (sensor_id, property, ts), all-or-nothing batches."""

    def __init__(self, assignments=()):
        self.rows: dict[tuple, dict] = {}
        self.assignments = list(assignments)
        self.down = False
        self.loads = 0

    def insert_readings(self, rows):
        if self.down:
            raise ConnectionError("db down")
        for r in rows:
            self.rows.setdefault((r["sensor_id"], r["property"], r["ts"]), r)

    def sensor_assignments(self):
        self.loads += 1
        if self.down:
            raise ConnectionError("db down")
        return list(self.assignments)


main.DB = FakeDB()
client = TestClient(main.app)  # not used as a context manager, so the app's lifespan (DB.open) does not run
EXAMPLES = sorted((ROOT / "examples").glob("*.json"))


def stored():
    return list(main.DB.rows.values())


def post(payload, token=None):
    token = token or TOKENS.get(payload["gatewayId"], "nope")
    return client.post("/v1/readings", json=payload, headers={"Authorization": f"Bearer {token}"})


def load(name):
    return json.loads((ROOT / "examples" / name).read_text())


def test_all_examples_accepted():
    for f in EXAMPLES:
        main.DB = FakeDB()
        payload = json.loads(f.read_text())
        r = post(payload)
        assert r.status_code == 202, (f.name, r.text)
        assert r.json()["accepted"] == len(payload["readings"]) == len(stored())


def test_catalog_is_air_station_only():
    assert set(main.CATALOG) == {"air_temperature", "air_relative_humidity", "global_radiation", "PPFD"}


def test_ppfd_accepted():
    main.DB = FakeDB()
    p = load("air-station.json")
    p["readings"].append({"sensorId": "gh1-air01", "property": "PPFD",
                          "value": 495.3, "unit": "umol/m2/s", "ts": "2026-11-26T09:00:00Z",
                          "aggregation": "mean", "period": "PT5M"})
    assert post(p).status_code == 202
    ppfd = {r["property"]: r for r in stored()}["PPFD"]
    assert ppfd["value"] == 495.3 and ppfd["unit"] == "umol/m2/s"


def test_row_shape():
    main.DB = FakeDB()
    post(load("air-station.json"))
    rows = {r["property"]: r for r in stored()}
    assert set(rows) == {"air_temperature", "air_relative_humidity", "global_radiation"}
    t = rows["air_temperature"]
    assert t["sensor_id"] == "gh1-air01" and t["gateway_id"] == "gw-gh1-01"
    assert t["value"] == 22.6 and t["unit"] == "Cel"
    assert t["ts"] == datetime(2026, 11, 26, 9, tzinfo=timezone.utc)
    assert t["received_at"].tzinfo is not None
    rad = rows["global_radiation"]
    assert rad["period_s"] == 300 and rad["aggregation"] == "mean"
    # Same columns as the INSERT in storage.py.
    assert set(t) == {"gateway_id", "sensor_id", "property", "value", "unit", "ts",
                      "aggregation", "period_s", "sent_at", "received_at"}


def test_timestamps_stored_in_utc():
    main.DB = FakeDB()
    p = load("air-station.json")
    p["readings"][0]["ts"] = "2026-11-26T10:00:00+01:00"
    assert post(p).status_code == 202
    assert stored()[0]["ts"] == datetime(2026, 11, 26, 9, tzinfo=timezone.utc)


def test_unit_defaults_to_catalog():
    main.DB = FakeDB()
    p = load("air-station.json")
    del p["readings"][1]["unit"]
    assert post(p).status_code == 202
    assert stored()[1]["unit"] == "%"


def test_resent_batch_is_not_duplicated():
    main.DB = FakeDB()
    assert post(load("air-station.json")).status_code == 202
    assert post(load("air-station.json")).status_code == 202
    assert len(stored()) == 3


def test_db_down_is_503():
    main.DB = FakeDB()
    main.DB.down = True
    r = post(load("air-station.json"))
    assert r.status_code == 503 and stored() == []


def _bad(mutate, status=422):
    main.DB = FakeDB()
    p = load("air-station.json")
    mutate(p)
    r = post(p)
    assert r.status_code == status, r.text
    assert stored() == []  # a batch is accepted or rejected whole
    return r


def test_rejections():
    _bad(lambda p: p["readings"][0].update(ts="2026-11-26T09:00:00"))           # no timezone
    _bad(lambda p: p["readings"][0].update(property="air_temp"))                # not in catalog
    for removed in ["co2_concentration", "soil_vwc", "wind_speed"]:
        _bad(lambda p: p["readings"][0].update(property=removed))               # other station types are gone
    _bad(lambda p: p["readings"][0].update(unit="degF"))                        # wrong unit
    _bad(lambda p: p["readings"][1].update(value=130))                          # RH out of range
    _bad(lambda p: p["readings"][2].update(value=-5))                           # negative radiation
    ppfd = {"property": "PPFD", "unit": "umol/m2/s"}
    _bad(lambda p: p["readings"][2].update(ppfd, value=3000))                   # PPFD out of range
    _bad(lambda p: p["readings"][2].update(ppfd, value=-1))                     # negative PPFD
    _bad(lambda p: p["readings"][2].update(ppfd, value=500, unit="µmol/m2/s"))  # UCUM micro is "u", not "µ"
    _bad(lambda p: p["readings"][2].update(property="ppfd", unit="umol/m2/s"))  # property names are case-sensitive
    _bad(lambda p: p["readings"][0].update(value="warm"))                       # numbers only
    _bad(lambda p: p["readings"][0].update(heightM=1.5))                        # installation data belongs in the registry
    _bad(lambda p: p["readings"][0].update(eventId="irr-1"))                    # no irrigation events any more
    _bad(lambda p: p.update(event={"eventId": "x", "type": "irrigation",
                                   "start": "2026-11-26T09:00:00Z", "end": "2026-11-26T09:01:00Z"}))
    _bad(lambda p: p.update(siteId="farm-01"))                                  # same at envelope level
    _bad(lambda p: p.update(readings=[]))                                       # empty batch


def test_auth():
    p = load("air-station.json")
    assert client.post("/v1/readings", json=p).status_code == 401              # no token
    assert post(p, token="wrong").status_code == 401                            # unknown token
    assert post(p, token=TOKENS["gw-meteo-01"]).status_code == 403             # someone else's token


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} tests passed")
