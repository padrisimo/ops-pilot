"""Read-only access to the mock service, incident and telemetry data."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class DataError(Exception):
    """Base class for data-layer errors."""


class ServiceNotFound(DataError):
    pass


class IncidentNotFound(DataError):
    pass


class RunbookNotFound(DataError):
    pass


def as_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DataError(f"Missing data file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DataError(f"Invalid JSON in {path}: {exc}") from exc


def _required(raw: dict, key: str, source: str):
    if key not in raw:
        raise DataError(f"{source}: missing required field {key!r}")
    return raw[key]


@dataclass(frozen=True)
class Service:
    service_id: str
    name: str
    owner: str
    tier: int
    dependencies: tuple[str, ...]
    runbook_id: str

    def to_dict(self) -> dict:
        return {
            "service_id": self.service_id,
            "name": self.name,
            "owner": self.owner,
            "tier": self.tier,
            "dependencies": list(self.dependencies),
            "runbook_id": self.runbook_id,
        }


@dataclass(frozen=True)
class Incident:
    incident_id: str
    title: str
    service_id: str
    severity: str
    status: str
    started_at: datetime
    detected_at: datetime
    window_minutes: int
    symptom: str

    @property
    def window_end(self) -> datetime:
        return self.started_at + timedelta(minutes=self.window_minutes)

    @property
    def baseline_start(self) -> datetime:
        return self.started_at - timedelta(minutes=30)

    def to_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "title": self.title,
            "service_id": self.service_id,
            "severity": self.severity,
            "status": self.status,
            "started_at": iso(self.started_at),
            "detected_at": iso(self.detected_at),
            "window_minutes": self.window_minutes,
            "symptom": self.symptom,
        }


@dataclass(frozen=True)
class MetricPoint:
    ts: datetime
    value: float

    def to_dict(self) -> dict:
        return {"ts": iso(self.ts), "value": self.value}


@dataclass(frozen=True)
class MetricSeries:
    service_id: str
    metric: str
    unit: str
    points: tuple[MetricPoint, ...]

    def to_dict(self) -> dict:
        return {
            "service_id": self.service_id,
            "metric": self.metric,
            "unit": self.unit,
            "points": [point.to_dict() for point in self.points],
        }


@dataclass(frozen=True)
class LogEntry:
    log_id: str
    ts: datetime
    service_id: str
    level: str
    message: str

    def to_dict(self) -> dict:
        return {
            "log_id": self.log_id,
            "ts": iso(self.ts),
            "service_id": self.service_id,
            "level": self.level,
            "message": self.message,
        }


@dataclass(frozen=True)
class Change:
    change_id: str
    service_id: str
    type: str
    target: str
    changed_at: datetime
    changed_by: str
    state: str
    summary: str

    def to_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "service_id": self.service_id,
            "type": self.type,
            "target": self.target,
            "changed_at": iso(self.changed_at),
            "changed_by": self.changed_by,
            "state": self.state,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class Runbook:
    runbook_id: str
    service_id: str
    title: str
    steps: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "runbook_id": self.runbook_id,
            "service_id": self.service_id,
            "title": self.title,
            "steps": list(self.steps),
        }


def _parse_service(raw: dict, source: str) -> Service:
    return Service(
        service_id=str(_required(raw, "service_id", source)),
        name=str(_required(raw, "name", source)),
        owner=str(_required(raw, "owner", source)),
        tier=int(_required(raw, "tier", source)),
        dependencies=tuple(raw.get("dependencies") or ()),
        runbook_id=str(_required(raw, "runbook_id", source)),
    )


def _parse_incident(raw: dict, source: str) -> Incident:
    return Incident(
        incident_id=str(_required(raw, "incident_id", source)),
        title=str(_required(raw, "title", source)),
        service_id=str(_required(raw, "service_id", source)),
        severity=str(_required(raw, "severity", source)),
        status=str(_required(raw, "status", source)),
        started_at=as_datetime(_required(raw, "started_at", source)),
        detected_at=as_datetime(_required(raw, "detected_at", source)),
        window_minutes=int(_required(raw, "window_minutes", source)),
        symptom=str(_required(raw, "symptom", source)),
    )


def _parse_metric(raw: dict, source: str) -> MetricSeries:
    points = tuple(
        sorted(
            (
                MetricPoint(
                    ts=as_datetime(_required(point, "ts", f"{source}.points")),
                    value=float(_required(point, "value", f"{source}.points")),
                )
                for point in raw.get("points") or ()
            ),
            key=lambda point: point.ts,
        )
    )
    return MetricSeries(
        service_id=str(_required(raw, "service_id", source)),
        metric=str(_required(raw, "metric", source)),
        unit=str(raw.get("unit") or ""),
        points=points,
    )


def _parse_log(raw: dict, source: str) -> LogEntry:
    return LogEntry(
        log_id=str(_required(raw, "log_id", source)),
        ts=as_datetime(_required(raw, "ts", source)),
        service_id=str(_required(raw, "service_id", source)),
        level=str(_required(raw, "level", source)).upper(),
        message=str(_required(raw, "message", source)),
    )


def _parse_change(raw: dict, source: str) -> Change:
    return Change(
        change_id=str(_required(raw, "change_id", source)),
        service_id=str(_required(raw, "service_id", source)),
        type=str(_required(raw, "type", source)),
        target=str(_required(raw, "target", source)),
        changed_at=as_datetime(_required(raw, "changed_at", source)),
        changed_by=str(raw.get("changed_by") or "unknown"),
        state=str(raw.get("state") or ""),
        summary=str(raw.get("summary") or ""),
    )


def _parse_runbook(raw: dict, source: str) -> Runbook:
    return Runbook(
        runbook_id=str(_required(raw, "runbook_id", source)),
        service_id=str(_required(raw, "service_id", source)),
        title=str(_required(raw, "title", source)),
        steps=tuple(raw.get("steps") or ()),
    )


class DataStore:
    """Indexed, read-only view of the mock production data."""

    def __init__(
        self,
        services: Iterable[Service],
        incidents: Iterable[Incident],
        metrics: Iterable[MetricSeries],
        logs: Iterable[LogEntry],
        changes: Iterable[Change],
        runbooks: Iterable[Runbook],
    ) -> None:
        self.services = {item.service_id: item for item in services}
        self.incidents = {item.incident_id: item for item in incidents}
        self.metrics = tuple(metrics)
        self.logs = tuple(logs)
        self.changes = tuple(changes)
        self.runbooks = {item.runbook_id: item for item in runbooks}

    @classmethod
    def load(cls, data_dir: str | Path | None = None) -> "DataStore":
        base = Path(data_dir or os.getenv("OPS_PILOT_DATA_DIR", DEFAULT_DATA_DIR))
        services = [
            _parse_service(item, f"services.json[{index}]")
            for index, item in enumerate(_load_json(base / "services.json"))
        ]
        incidents = [
            _parse_incident(item, f"incidents.json[{index}]")
            for index, item in enumerate(_load_json(base / "incidents.json"))
        ]
        metrics = [
            _parse_metric(item, f"metrics.json[{index}]")
            for index, item in enumerate(_load_json(base / "metrics.json"))
        ]
        logs = [
            _parse_log(item, f"logs.json[{index}]")
            for index, item in enumerate(_load_json(base / "logs.json"))
        ]
        changes = [
            _parse_change(item, f"changes.json[{index}]")
            for index, item in enumerate(_load_json(base / "changes.json"))
        ]
        runbooks = [
            _parse_runbook(item, f"runbooks.json[{index}]")
            for index, item in enumerate(_load_json(base / "runbooks.json"))
        ]

        store = cls(services, incidents, metrics, logs, changes, runbooks)
        missing_services = {
            incident.service_id
            for incident in store.incidents.values()
            if incident.service_id not in store.services
        }
        if missing_services:
            raise DataError(f"Incidents reference unknown services: {sorted(missing_services)}")
        return store

    # -- lookups -------------------------------------------------------

    def get_service(self, service_id: str) -> Service:
        try:
            return self.services[service_id]
        except KeyError as exc:
            raise ServiceNotFound(
                f"Service {service_id!r} not found. Available: "
                f"{', '.join(sorted(self.services))}"
            ) from exc

    def get_incident(self, incident_id: str) -> Incident:
        try:
            return self.incidents[incident_id]
        except KeyError as exc:
            raise IncidentNotFound(
                f"Incident {incident_id!r} not found. Available: "
                f"{', '.join(sorted(self.incidents))}"
            ) from exc

    def get_runbook(self, service_id: str) -> Runbook:
        service = self.get_service(service_id)
        try:
            return self.runbooks[service.runbook_id]
        except KeyError as exc:
            raise RunbookNotFound(
                f"Runbook {service.runbook_id!r} for {service_id!r} not found"
            ) from exc

    def list_incidents(self, status: str | None = None) -> list[Incident]:
        wanted = status.lower() if status else None
        result = [
            incident
            for incident in self.incidents.values()
            if wanted is None or incident.status.lower() == wanted
        ]
        return sorted(result, key=lambda incident: (incident.started_at, incident.incident_id))

    # -- telemetry -----------------------------------------------------

    def metric_series(
        self, service_id: str, metric: str | None = None
    ) -> list[MetricSeries]:
        return sorted(
            (
                series
                for series in self.metrics
                if series.service_id == service_id
                and (metric is None or series.metric == metric)
            ),
            key=lambda series: series.metric,
        )

    def query_logs(
        self,
        service_id: str,
        start: datetime,
        end: datetime,
        level: str | None = None,
        contains: str | None = None,
        limit: int | None = None,
    ) -> list[LogEntry]:
        wanted_level = level.upper() if level else None
        needle = contains.casefold() if contains else None
        result = [
            log
            for log in self.logs
            if log.service_id == service_id
            and start <= log.ts <= end
            and (wanted_level is None or log.level == wanted_level)
            and (needle is None or needle in log.message.casefold())
        ]
        result.sort(key=lambda log: (log.ts, log.log_id))
        return result[:limit] if limit is not None else result

    def query_changes(
        self,
        service_id: str,
        start: datetime,
        end: datetime,
    ) -> list[Change]:
        result = [
            change
            for change in self.changes
            if change.service_id == service_id and start <= change.changed_at <= end
        ]
        result.sort(key=lambda change: (change.changed_at, change.change_id))
        return result


@lru_cache(maxsize=1)
def get_data() -> DataStore:
    return DataStore.load()
