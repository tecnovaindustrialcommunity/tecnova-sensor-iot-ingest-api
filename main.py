"""agroclima-ingest-api

Receives batches of readings from air stations (air temperature, relative
humidity and global radiation) over HTTPS, validates them against the
property catalog and the sensor registry, and stores them in Postgres (Cloud SQL),
table ingest.readings.
"""
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import registry
import storage

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ingest")

TOKENS_FILE = os.environ.get("TOKENS_FILE", "/secrets/gateway-tokens.json")
CATALOG_FILE = os.environ.get("CATALOG_FILE", str(Path(__file__).with_name("catalog.json")))

# {sha256(token): gatewayId}. Only hashes are stored, never the tokens themselves.
TOKEN_HASHES: dict[str, str] = json.loads(Path(TOKENS_FILE).read_text())
# {property: {"unit": str, "min": float, "max": float}}
CATALOG: dict[str, dict] = json.loads(Path(CATALOG_FILE).read_text())
DB = storage.from_env()
# Sensor -> gateway assignments, cached from DB (off / warn / enforce, see registry.py).
REGISTRY = registry.from_env(loader=lambda: DB.sensor_assignments())
MAX_REGISTRY_MESSAGES = 50


@asynccontextmanager
async def lifespan(app: FastAPI):
    DB.open()
    yield
    DB.close()


app = FastAPI(title="agroclima-ingest-api", version="1.0", lifespan=lifespan)


def _to_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp must include a timezone (use Z for UTC)")
    return v.astimezone(timezone.utc)


FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sensorId: str = Field(min_length=1, max_length=128)
    property: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    value: FiniteFloat
    unit: Optional[str] = Field(default=None, max_length=32)
    ts: datetime
    aggregation: Optional[Literal["mean", "min", "max", "last"]] = None
    period: Optional[timedelta] = None  # ISO 8601 duration, e.g. "PT10M"

    @field_validator("ts")
    @classmethod
    def ts_utc(cls, v: datetime) -> datetime:
        return _to_utc(v)

    @model_validator(mode="after")
    def check_catalog(self) -> "Reading":
        spec = CATALOG.get(self.property)
        if spec is None:
            raise ValueError(f"unknown property '{self.property}'")
        if self.unit is not None and self.unit != spec["unit"]:
            raise ValueError(f"'{self.property}' must be sent in '{spec['unit']}', got '{self.unit}'")
        if not (spec["min"] <= self.value <= spec["max"]):
            raise ValueError(f"'{self.property}'={self.value} outside [{spec['min']}, {spec['max']}]")
        if self.period is not None and self.period.total_seconds() <= 0:
            raise ValueError("period must be positive")
        return self


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: Literal["1.0"]
    gatewayId: str = Field(min_length=1, max_length=128)
    sentAt: datetime
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


def _check_registry(env: Envelope) -> None:
    """Every sensorId must have been assigned to this gateway at the time of each reading."""
    if REGISTRY.mode == "off":
        return
    try:
        problems = REGISTRY.problems(env.gatewayId, env.readings)
    except registry.RegistryUnavailable as e:
        if REGISTRY.mode == "enforce":
            log.error("registry unavailable, batch from %s rejected: %s", env.gatewayId, e)
            raise HTTPException(status_code=503, detail="sensor registry unavailable, retry later")
        log.warning("registry unavailable, batch from %s accepted unchecked: %s", env.gatewayId, e)
        return
    if not problems:
        return
    if REGISTRY.mode == "enforce":
        raise HTTPException(status_code=422, detail={"registry": problems[:MAX_REGISTRY_MESSAGES]})
    log.warning("registry problems gateway=%s: %s", env.gatewayId, problems[:MAX_REGISTRY_MESSAGES])


def _row(env: Envelope, r: Reading, received_at: datetime) -> dict:
    return {
        "gateway_id": env.gatewayId,
        "sensor_id": r.sensorId,
        "property": r.property,
        "value": r.value,
        "unit": r.unit or CATALOG[r.property]["unit"],
        "ts": r.ts,
        "aggregation": r.aggregation,
        "period_s": int(r.period.total_seconds()) if r.period else None,
        "sent_at": env.sentAt,
        "received_at": received_at,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "registry": REGISTRY.mode}


@app.post("/v1/readings", status_code=202)
def ingest(env: Envelope, authorization: Optional[str] = Header(default=None)) -> dict:
    _authenticate(authorization, env.gatewayId)
    _check_registry(env)
    received_at = datetime.now(timezone.utc)
    try:
        DB.insert_readings([_row(env, r, received_at) for r in env.readings])
    except Exception as e:
        # Nothing was stored (one transaction). The gateway retries the whole batch;
        # readings already stored (same sensor, property, ts) are skipped.
        log.error("could not store batch from %s: %s", env.gatewayId, e)
        raise HTTPException(status_code=503, detail="could not store readings, retry later")
    return {"accepted": len(env.readings)}
