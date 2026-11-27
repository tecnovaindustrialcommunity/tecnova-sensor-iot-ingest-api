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
TOKENS = {gw: f"token-{gw}" for gw in ["gw-gh1-01", "gw-meteo-01", "gw-fert-01", "tutabot-01"]}
tok_file = Path(tempfile.mkdtemp()) / "gateway-tokens.json"
tok_file.write_text(json.dumps({hashlib.sha256(t.encode()).hexdigest(): gw for gw, t in TOKENS.items()}))
os.environ.update(PROJECT_ID="test-project", TOKENS_FILE=str(tok_file))

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


def test_row_shape():
    PUBLISHED.clear()
    post(load("04-irrigation.json"))
    row = PUBLISHED[0]["data"]
    assert row["sensor_id"] == "fert01-flow" and row["unit"] == "L"
    assert row["event_id"] == "irr-gh1-c3-20261126-0900"
    assert json.loads(row["extra"])["event"]["valveId"] == "gh1-c3-v2"
    assert isinstance(row["ts"], int)  # microseconds for BigQuery TIMESTAMP
    ec25 = next(p["data"] for p in PUBLISHED if p["data"]["property"] == "water_ec_25")
    assert json.loads(ec25["extra"])["compensation"]["refTemperature"] == 25


def test_period_iso8601_to_seconds():
    PUBLISHED.clear()
    post(load("02-external-climate.json"))
    rain = next(p["data"] for p in PUBLISHED if p["data"]["property"] == "precipitation")
    assert rain["period_s"] == 600 and rain["aggregation"] == "sum"


def test_location_row():
    PUBLISHED.clear()
    post(load("06-location.json"))
    row = PUBLISHED[0]["data"]
    assert row["value_num"] is None and row["unit"] is None
    assert json.loads(row["value_geojson"])["coordinates"] == [-2.463712, 36.838104]


def test_same_reading_same_id():
    PUBLISHED.clear()
    post(load("03-soil.json"))
    post(load("03-soil.json"))
    ids = [p["data"]["reading_id"] for p in PUBLISHED]
    assert len(ids) == 2 * len(set(ids))


def _bad(mutate, status=422):
    p = load("01-greenhouse-air.json")
    mutate(p)
    r = post(p)
    assert r.status_code == status, r.text
    return r


def test_rejections():
    _bad(lambda p: p["readings"][0].update(ts="2026-11-26T09:00:00"))           # no timezone
    _bad(lambda p: p["readings"][0].update(property="air_temp"))                # not in catalog
    _bad(lambda p: p["readings"][0].update(unit="degF"))                        # wrong unit
    _bad(lambda p: p["readings"][1].update(value=130))                          # RH out of range
    _bad(lambda p: p["readings"][0].update(heightM=1.5))                        # installation data belongs in the registry
    _bad(lambda p: p.update(siteId="farm-01"))                                  # same at envelope level
    _bad(lambda p: p.update(readings=[]))                                       # empty batch
    loc = load("06-location.json")
    for coords in ([36.838104, -200], [0, 0]):
        bad = copy.deepcopy(loc)
        bad["readings"][0]["value"]["coordinates"] = coords
        assert post(bad).status_code == 422
    bad = copy.deepcopy(loc)
    bad["readings"][0]["value"] = 12.3                                          # location must be a Point
    assert post(bad).status_code == 422


def test_auth():
    p = load("01-greenhouse-air.json")
    assert client.post("/v1/readings", json=p).status_code == 401              # no token
    assert post(p, token="wrong").status_code == 401                            # unknown token
    assert post(p, token=TOKENS["gw-meteo-01"]).status_code == 403             # someone else's token


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} tests passed")
