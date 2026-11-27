"""Sensor registry: which gateway each sensor was assigned to, and when.

The assignments live in Postgres, in the view ingest.sensor_gateway (see storage.py).
They are kept in memory and reloaded every REGISTRY_TTL_S seconds, so a request
never waits for the database except on the first load or after the cache expires.

Modes (REGISTRY_MODE):
  off      no check
  warn     batches are accepted; problems are written to the logs
  enforce  batches with problems are rejected (422); 503 if the registry cannot be loaded
"""
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

MODES = ("off", "warn", "enforce")

# Each row: (sensor_id, gateway_id, valid_from, valid_to or None)
Row = tuple[str, str, datetime, Optional[datetime]]
Loader = Callable[[], Iterable[Row]]


class RegistryUnavailable(Exception):
    """The registry could not be loaded and there is no previous copy to fall back on."""


def _aware(v: datetime) -> datetime:
    return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Assignment:
    gateway_id: str
    valid_from: datetime
    valid_to: Optional[datetime]  # None = still assigned

    def covers(self, ts: datetime) -> bool:
        return self.valid_from <= ts and (self.valid_to is None or ts < self.valid_to)


class Registry:
    def __init__(self, loader: Optional[Loader], mode: str, ttl_s: float = 60, retry_s: float = 10,
                 clock: Callable[[], float] = time.monotonic):
        if mode not in MODES:
            raise ValueError(f"REGISTRY_MODE must be one of {MODES}, got '{mode}'")
        if mode != "off" and loader is None:
            raise ValueError(f"REGISTRY_MODE={mode} needs a loader")
        self.loader = loader
        self.mode = mode
        self.ttl_s = ttl_s
        self.retry_s = retry_s
        self._clock = clock
        self._by_sensor: Optional[dict[str, list[Assignment]]] = None
        self._next_load = 0.0
        self._lock = threading.Lock()

    def _assignments(self) -> dict[str, list[Assignment]]:
        if self._by_sensor is not None and self._clock() < self._next_load:
            return self._by_sensor
        with self._lock:
            now = self._clock()
            if now < self._next_load:  # another thread reloaded, or a failure is still in its retry window
                if self._by_sensor is None:
                    raise RegistryUnavailable("registry not loaded yet; waiting before retrying")
                return self._by_sensor
            try:
                by_sensor: dict[str, list[Assignment]] = {}
                for sensor_id, gateway_id, valid_from, valid_to in self.loader():
                    by_sensor.setdefault(sensor_id, []).append(
                        Assignment(gateway_id, _aware(valid_from), _aware(valid_to) if valid_to else None))
            except Exception as e:
                self._next_load = now + self.retry_s
                if self._by_sensor is None:
                    raise RegistryUnavailable(str(e)) from e
                log.warning("registry reload failed, keeping previous copy: %s", e)
                return self._by_sensor
            self._by_sensor = by_sensor
            self._next_load = now + self.ttl_s
            log.info("registry loaded: %d sensors", len(by_sensor))
            return by_sensor

    def problems(self, gateway_id: str, readings) -> list[str]:
        """One message per sensor whose readings are not covered by an assignment to gateway_id.

        `readings` are objects with .sensorId and .ts (timezone-aware). Raises RegistryUnavailable.
        """
        by_sensor = self._assignments()
        out: list[str] = []
        seen: set[str] = set()
        for r in readings:
            if r.sensorId in seen:
                continue
            assignments = by_sensor.get(r.sensorId)
            if not assignments:
                out.append(f"sensor '{r.sensorId}' is not in the registry")
                seen.add(r.sensorId)
            elif not any(a.gateway_id == gateway_id and a.covers(r.ts) for a in assignments):
                out.append(f"sensor '{r.sensorId}' is not assigned to gateway '{gateway_id}' at {r.ts.isoformat()}")
                seen.add(r.sensorId)
        return out


def from_env(loader: Loader) -> Registry:
    return Registry(
        loader=loader,
        mode=os.environ.get("REGISTRY_MODE") or "warn",
        ttl_s=float(os.environ.get("REGISTRY_TTL_S", "60")),
    )
