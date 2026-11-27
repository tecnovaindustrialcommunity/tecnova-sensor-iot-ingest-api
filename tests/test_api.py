"""Local tests without Google Cloud: Pub/Sub is replaced by a fake that keeps messages in memory.

Run:  pip install fastapi pydantic httpx && python3 -m pytest tests -q   (or: python3 tests/test_api.py)
"""
import copy
import hashlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# --- fake google.cloud.pubsub_v1 -------------------------------------------
PUBLISHED: list[dict] = []


class _Future:
    def result(self, timeout=None):
        return "msg-id"


class _FakePublisher:
    def topic_path(self, project, topic):
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic, data, **attrs):
        PUBLISHED.append({"topic": topic, "data": json.loads(data), "attrs": attrs})
        return _Future()


pubsub_v1 = types.ModuleType("google.cloud.pubsub_v1")
pubsub_v1.PublisherClient = _FakePublisher
google = sys.modules.setdefault("google", types.ModuleType("google"))
cloud = types.ModuleType("google.cloud")
cloud.pubsub_v1 = pubsub_v1
google.cloud = cloud
sys.modules["google.cloud"] = cloud
sys.modules["google.cloud.pubsub_v1"] = pubsub_v1

# --- tokens for the example gateways ---------------------------------------
TOKENS = {gw: f"token-{gw}" for gw in ["gw-gh1-01", "gw-meteo-01"]}
tok_file = Path(tempfile.mkdtemp()) / "gateway-tokens.json"
tok_file.write_text(json.dumps({hashlib.sha256(t.encode()).hexdigest(): gw for gw, t in TOKENS.items()}))
os.environ.update(PROJECT_ID="test-project", TOKENS_FILE=str(tok_file), REGISTRY_MODE="off")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)
EXAMPLES = sorted((ROOT / "examples").glob("*.json"))


def post(payload, token=None):
    token = token or TOKENS.get(payload["gatewayId"], "nope")
    return client.post("/v1/readings", json=payload, headers={"Authorization": f"Bearer {token}"})


def load(name):
    return json.loads((ROOT / "examples" / name).read_text())


def test_all_examples_accepted():
    for f in EXAMPLES:
        PUBLISHED.clear()
        payload = json.loads(f.read_text())
        r = post(payload)
        assert r.status_code == 202, (f.name, r.text)
        assert r.json()["accepted"] == len(payload["readings"]) == len(PUBLISHED)


def test_catalog_is_air_station_only():
    assert set(main.CATALOG) == {"air_temperature", "air_relative_humidity", "global_radiation"}


def test_row_shape():
    PUBLISHED.clear()
    post(load("air-station.json"))
    rows = {p["data"]["property"]: p["data"] for p in PUBLISHED}
    assert set(rows) == set(main.CATALOG)
    t = rows["air_temperature"]
    assert t["sensor_id"] == "gh1-air01" and t["unit"] == "Cel" and t["value_num"] == 22.6
    assert isinstance(t["ts"], int)  # microseconds for BigQuery TIMESTAMP
    rad = rows["global_radiation"]
    assert rad["period_s"] == 300 and rad["aggregation"] == "mean"
    schema_cols = {c["name"] for c in json.loads((ROOT / "bq_schema.json").read_text())}
    assert set(t) == schema_cols  # every published row matches the table schema


def test_unit_defaults_to_catalog():
    PUBLISHED.clear()
    p = load("air-station.json")
    del p["readings"][1]["unit"]
    assert post(p).status_code == 202
    assert PUBLISHED[1]["data"]["unit"] == "%"


def test_same_reading_same_id():
    PUBLISHED.clear()
    post(load("air-station.json"))
    post(load("air-station.json"))
    ids = [p["data"]["reading_id"] for p in PUBLISHED]
    assert len(ids) == 2 * len(set(ids))


def _bad(mutate, status=422):
    p = load("air-station.json")
    mutate(p)
    r = post(p)
    assert r.status_code == status, r.text
    return r


def test_rejections():
    _bad(lambda p: p["readings"][0].update(ts="2026-11-26T09:00:00"))           # no timezone
    _bad(lambda p: p["readings"][0].update(property="air_temp"))                # not in catalog
    for removed in ["co2_concentration", "soil_vwc", "wind_speed"]:
        _bad(lambda p: p["readings"][0].update(property=removed))               # other station types are gone
    _bad(lambda p: p["readings"][0].update(unit="degF"))                        # wrong unit
    _bad(lambda p: p["readings"][1].update(value=130))                          # RH out of range
    _bad(lambda p: p["readings"][2].update(value=-5))                           # negative radiation
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
