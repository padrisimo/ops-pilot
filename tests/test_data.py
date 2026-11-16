import pytest

from ops_pilot.data import DataStore, IncidentNotFound, ServiceNotFound


def test_loads_all_mock_data(store: DataStore) -> None:
    assert len(store.services) == 5
    assert len(store.incidents) == 5
    assert len(store.metrics) == 11
    assert len(store.logs) == 25
    assert len(store.changes) == 4
    assert len(store.runbooks) == 5


def test_incident_lookup_and_window(store: DataStore) -> None:
    incident = store.get_incident("INC-1001")
    assert incident.service_id == "checkout-api"
    assert incident.started_at.isoformat() == "2026-11-14T14:05:00+00:00"
    assert incident.window_end.isoformat() == "2026-11-14T15:05:00+00:00"
    assert incident.baseline_start.isoformat() == "2026-11-14T13:35:00+00:00"


def test_missing_incident_and_service_are_explicit(store: DataStore) -> None:
    with pytest.raises(IncidentNotFound, match="INC-9999"):
        store.get_incident("INC-9999")
    with pytest.raises(ServiceNotFound, match="nope"):
        store.get_service("nope")


def test_metric_queries_and_log_filters(store: DataStore) -> None:
    series = store.metric_series("checkout-api")
    assert {item.metric for item in series} == {
        "error_rate_pct",
        "latency_p95_ms",
        "request_rate_rps",
    }

    incident = store.get_incident("INC-1002")
    logs = store.query_logs(
        "payments-db", incident.started_at, incident.window_end, level="ERROR"
    )
    assert [log.log_id for log in logs] == ["L-1101", "L-1102", "L-1104", "L-1105"]

    pool_logs = store.query_logs(
        "payments-db",
        incident.started_at,
        incident.window_end,
        contains="pool",
    )
    assert {log.log_id for log in pool_logs} == {"L-1102", "L-1104", "L-1105"}


def test_recent_changes_are_scoped_to_service_and_time(store: DataStore) -> None:
    incident = store.get_incident("INC-1001")
    changes = store.query_changes(
        "checkout-api",
        incident.started_at.replace(hour=12, minute=0),
        incident.window_end,
    )
    assert [change.change_id for change in changes] == ["CHG-2001"]


def test_runbook_lookup(store: DataStore) -> None:
    runbook = store.get_runbook("checkout-api")
    assert runbook.runbook_id == "RB-001"
    assert any("roll back" in step.lower() for step in runbook.steps)
