import pytest

from ops_pilot.analysis import analyze_incident, metric_report
from ops_pilot.data import DataStore, IncidentNotFound


@pytest.mark.parametrize(
    ("incident_id", "cause", "action", "target"),
    [
        ("INC-1001", "bad_deploy", "rollback_deploy", "web@2026.11.14-3"),
        ("INC-1002", "connection_pool_exhaustion", "raise_db_pool", "payments-db"),
        ("INC-1003", "feature_flag_regression", "disable_feature_flag", "new-pricing"),
        ("INC-1004", "disk_pressure", "rotate_logs", "search-api"),
    ],
)
def test_high_confidence_incidents_recommend_one_action(
    store: DataStore, incident_id: str, cause: str, action: str, target: str
) -> None:
    result = analyze_incident(store, incident_id)
    top = result["hypotheses"][0]

    assert top["cause"] == cause
    assert top["confidence"] == "high"
    assert result["needs_human_input"] is False
    assert result["recommended_action"]["action"] == action
    assert result["recommended_action"]["target"] == target
    assert "baseline median" in top["calculation"]


def test_bad_deploy_evidence_cites_deploy_and_metric(store: DataStore) -> None:
    result = analyze_incident(store, "INC-1001")
    assert "CHG-2001" in result["evidence"]
    assert "L-1001" in result["evidence"]
    assert "metric:checkout-api:latency_p95_ms" in result["evidence"]
    assert result["recommended_action"]["target"] == "web@2026.11.14-3"


def test_pool_evidence_cites_connections_and_logs(store: DataStore) -> None:
    result = analyze_incident(store, "INC-1002")
    assert "L-1101" in result["evidence"]
    assert "metric:payments-db:active_connections" in result["evidence"]
    assert result["recommended_action"]["action"] == "raise_db_pool"


def test_flag_evidence_cites_flag_change_and_logs(store: DataStore) -> None:
    result = analyze_incident(store, "INC-1003")
    assert "CHG-2002" in result["evidence"]
    assert "L-1201" in result["evidence"]
    assert result["recommended_action"]["target"] == "new-pricing"


def test_disk_evidence_cites_metric_and_rotation_job(store: DataStore) -> None:
    result = analyze_incident(store, "INC-1004")
    assert "metric:search-api:disk_used_pct" in result["evidence"]
    assert "CHG-2003" in result["evidence"]
    assert result["recommended_action"]["action"] == "rotate_logs"


def test_healthy_incident_does_not_force_a_cause(store: DataStore) -> None:
    result = analyze_incident(store, "INC-1005")

    assert result["anomalies"] == []
    assert result["hypotheses"][0]["cause"] == "unknown"
    assert result["hypotheses"][0]["confidence"] == "low"
    assert result["recommended_action"] is None
    assert result["needs_human_input"] is True
    assert result["question"]
    assert "does not support" in result["summary"] or "Unknown" in result["summary"]


def test_metric_report_computes_baseline_and_peak(store: DataStore) -> None:
    incident = store.get_incident("INC-1001")
    latency = metric_report(store, incident, "latency_p95_ms")[0]
    assert latency["baseline_median"] == 221.5
    assert latency["peak"] == 1240.0
    assert latency["ratio"] == pytest.approx(5.5982)
    assert latency["is_anomaly"] is True

    traffic = metric_report(store, incident, "request_rate_rps")[0]
    assert traffic["is_anomaly"] is False


def test_unknown_incident_is_explicit(store: DataStore) -> None:
    with pytest.raises(IncidentNotFound, match="INC-9999"):
        analyze_incident(store, "INC-9999")
