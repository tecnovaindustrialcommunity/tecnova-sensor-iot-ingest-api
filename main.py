"""agroclima-ingest-api

Receives batches of sensor readings over HTTPS, validates them against the
property catalog and publishes one Pub/Sub message per reading. A BigQuery
subscription on the topic writes the messages to the `readings` table.
"""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from fastapi import FastAPI, Header, HTTPException
from google.cloud import pubsub_v1
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROJECT_ID = os.environ["PROJECT_ID"]
TOPIC_ID = os.environ.get("TOPIC_ID", "readings-raw")
TOKENS_FILE = os.environ.get("TOKENS_FILE", "/secrets/gateway-tokens.json")
CATALOG_FILE = os.environ.get("CATALOG_FILE", str(Path(__file__).with_name("catalog.json")))

# {sha256(token): gatewayId}. Only hashes are stored, never the tokens themselves.
TOKEN_HASHES: dict[str, str] = json.loads(Path(TOKENS_FILE).read_text())
# {property: {"unit": str | None, "kind": "number" | "geopoint", "min": float, "max": float}}
CATALOG: dict[str, dict] = json.loads(Path(CATALOG_FILE).read_text())

publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT_ID, TOPIC_ID)

app = FastAPI(title="agroclima-ingest-api", version="1.0")


def _to_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp must include a timezone (use Z for UTC)")
    return v.astimezone(timezone.utc)


def _micros(v: datetime) -> int:
    # BigQuery subscriptions accept TIMESTAMP as microseconds since the Unix epoch.
    return int(v.timestamp() * 1_000_000)


class GeoPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["Point"]
    coordinates: list[float] = Field(min_length=2, max_length=3)

    @field_validator("coordinates")
    @classmethod
    def check_range(cls, v: list[float]) -> list[float]:
        lon, lat = v[0], v[1]
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("coordinates must be [longitude, latitude] in WGS84")
        if lon == 0 and lat == 0:
            raise ValueError("[0, 0] is not a valid position; omit the reading instead")
        return v


class Compensation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refTemperature: float
    tempCoefficient: float
    method: str = "linear"


FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sensorId: str = Field(min_length=1, max_length=128)
    property: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    value: Union[GeoPoint, FiniteFloat]
    unit: Optional[str] = Field(default=None, max_length=32)
    ts: datetime
    aggregation: Optional[Literal["mean", "min", "max", "sum", "last"]] = None
    period: Optional[timedelta] = None  # ISO 8601 duration, e.g. "PT10M"
    eventId: Optional[str] = Field(default=None, max_length=128)
    basedOnEventIds: Optional[list[str]] = None
    derived: bool = False
    compensation: Optional[Compensation] = None

    @field_validator("ts")
    @classmethod
    def ts_utc(cls, v: datetime) -> datetime:
        return _to_utc(v)

    @model_validator(mode="after")
    def check_catalog(self) -> "Reading":
        spec = CATALOG.get(self.property)
        if spec is None:
            raise ValueError(f"unknown property '{self.property}'")
        if spec["kind"] == "geopoint":
            if not isinstance(self.value, GeoPoint):
                raise ValueError(f"'{self.property}' expects a GeoJSON Point")
            if self.unit is not None:
                raise ValueError(f"'{self.property}' takes no unit")
            return self
        if isinstance(self.value, GeoPoint):
            raise ValueError(f"'{self.property}' expects a number")
        if self.unit is not None and self.unit != spec["unit"]:
            raise ValueError(f"'{self.property}' must be sent in '{spec['unit']}', got '{self.unit}'")
        if not (spec["min"] <= self.value <= spec["max"]):
            raise ValueError(f"'{self.property}'={self.value} outside [{spec['min']}, {spec['max']}]")
        if self.period is not None and self.period.total_seconds() <= 0:
            raise ValueError("period must be positive")
        return self


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eventId: str = Field(min_length=1, max_length=128)
    type: Literal["irrigation"]
    valveId: Optional[str] = None
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def utc(cls, v: datetime) -> datetime:
        return _to_utc(v)

    @model_validator(mode="after")
    def order(self) -> "Event":
        if self.end < self.start:
            raise ValueError("event end is before start")
        return self


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: Literal["1.0"]
    gatewayId: str = Field(min_length=1, max_length=128)
    sentAt: datetime
    event: Optional[Event] = None
    readings: list[Reading] = Field(min_length=1, max_length=1000)

    @field_validator("sentAt")
    @classmethod
    def sent_utc(cls, v: datetime) -> datetime:
        return _to_utc(v)


def _authenticate(authorization: Optional[str], gateway_id: str) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    owner = TOKEN_HASHES.get(hashlib.sha256(token.encode()).hexdigest())
    if owner is None:
        raise HTTPException(status_code=401, detail="invalid token")
    if owner != gateway_id:
        raise HTTPException(status_code=403, detail="token does not belong to this gatewayId")
    # TODO: check against the sensor registry that every sensorId belongs to this gateway.


def _row(env: Envelope, r: Reading, received_at: datetime) -> dict:
    is_geo = isinstance(r.value, GeoPoint)
    extra = {}
    if r.compensation:
        extra["compensation"] = r.compensation.model_dump()
    if r.basedOnEventIds:
        extra["basedOnEventIds"] = r.basedOnEventIds
    event_id = r.eventId
    if env.event and (event_id is None or event_id == env.event.eventId):
        event_id = env.event.eventId
        extra["event"] = env.event.model_dump(mode="json")
    return {
        # Same sensor + property + ts => same id, so resent readings can be deduplicated.
        "reading_id": hashlib.sha256(f"{r.sensorId}|{r.property}|{r.ts.isoformat()}".encode()).hexdigest()[:32],
        "gateway_id": env.gatewayId,
        "sensor_id": r.sensorId,
        "property": r.property,
        "value_num": None if is_geo else r.value,
        "value_geojson": json.dumps(r.value.model_dump()) if is_geo else None,
        "unit": None if is_geo else (r.unit or CATALOG[r.property]["unit"]),
        "ts": _micros(r.ts),
        "aggregation": r.aggregation,
        "period_s": int(r.period.total_seconds()) if r.period else None,
        "event_id": event_id,
        "derived": r.derived,
        "extra": json.dumps(extra) if extra else None,
        "sent_at": _micros(env.sentAt),
        "received_at": _micros(received_at),
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/readings", status_code=202)
def ingest(env: Envelope, authorization: Optional[str] = Header(default=None)) -> dict:
    _authenticate(authorization, env.gatewayId)
    received_at = datetime.now(timezone.utc)
    futures = [
        publisher.publish(
            topic_path,
            json.dumps(_row(env, r, received_at)).encode(),
            gateway_id=env.gatewayId,
            property=r.property,
        )
        for r in env.readings
    ]
    try:
        for f in futures:
            f.result(timeout=30)
    except Exception:
        # The gateway retries the whole batch; duplicates are removed by reading_id.
        raise HTTPException(status_code=503, detail="could not queue readings, retry later")
    return {"accepted": len(env.readings)}
