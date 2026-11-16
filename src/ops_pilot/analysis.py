"""Deterministic incident analysis.

The LLM may explain these findings, but it must not invent metrics, logs,
deployments or arithmetic.  This module is the evidence engine.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from statistics import median

from .data import (
    Change,
    DataStore,
    Incident,
    LogEntry,
    MetricSeries,
    iso,
)

BASELINE_MINUTES = 30
RATIO_THRESHOLD = 1.8

# Absolute thresholds are useful for metrics whose baseline is near zero or
# where the ratio alone is not meaningful (for example disk usage).
ABSOLUTE_THRESHOLDS: dict[str, float] = {
    "error_rate_pct": 1.0,
    "disk_used_pct": 85.0,
    "pool_wait_ms": 100.0,
    "active_connections": 90.0,
}

CAUSE_LABELS = {
    "bad_deploy": "Bad deploy",
    "connection_pool_exhaustion": "Connection pool exhaustion",
    "feature_flag_regression": "Feature flag regression",
    "disk_pressure": "Disk pressure",
    "traffic_spike": "Traffic spike",
    "unknown": "Unknown",
}


def _fmt(value: float, unit: str = "") -> str:
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}".strip()


def _metric_stats(series: MetricSeries, incident: Incident) -> dict:
    baseline_points = [
        point
        for point in series.points
        if incident.baseline_start <= point.ts < incident.started_at
    ]
    window_points = [
        point
        for point in series.points
        if incident.started_at <= point.ts <= incident.window_end
    ]
    if not baseline_points or not window_points:
        return {
            "metric": series.metric,
            "unit": series.unit,
            "baseline_median": None,
            "peak": None,
            "peak_ts": None,
            "ratio": None,
            "threshold": ABSOLUTE_THRESHOLDS.get(series.metric),
            "is_anomaly": False,
            "evidence": f"metric:{series.service_id}:{series.metric}",
            "calculation": "Insufficient metric points for a baseline comparison.",
            "points": [point.to_dict() for point in series.points],
        }

    baseline = median(point.value for point in baseline_points)
    peak_point = max(window_points, key=lambda point: point.value)
    peak = peak_point.value
    ratio = (peak / baseline) if baseline else None
    threshold = ABSOLUTE_THRESHOLDS.get(series.metric)

    if threshold is not None:
        is_anomaly = peak >= threshold
        rule = f"peak >= {_fmt(threshold, series.unit)}"
    else:
        is_anomaly = ratio is not None and ratio >= RATIO_THRESHOLD
        rule = f"peak/baseline >= {RATIO_THRESHOLD:.2f}x"

    calculation = (
        f"{series.metric}: baseline median {_fmt(baseline, series.unit)} -> "
        f"peak {_fmt(peak, series.unit)} at {iso(peak_point.ts)} "
        f"({ratio:.2f}x; {rule})"
        if ratio is not None
        else f"{series.metric}: insufficient baseline"
    )
    return {
        "metric": series.metric,
        "unit": series.unit,
        "baseline_median": round(baseline, 4),
        "peak": round(peak, 4),
        "peak_ts": iso(peak_point.ts),
        "ratio": round(ratio, 4) if ratio is not None else None,
        "threshold": threshold,
        "is_anomaly": is_anomaly,
        "evidence": f"metric:{series.service_id}:{series.metric}",
        "calculation": calculation,
        "points": [point.to_dict() for point in series.points],
    }


def metric_report(
    store: DataStore,
    incident: Incident,
    metric: str | None = None,
) -> list[dict]:
    return [
        _metric_stats(series, incident)
        for series in store.metric_series(incident.service_id, metric)
    ]


def _normalise_signature(message: str) -> str:
    text = message.casefold()
    text = re.sub(r"\b[0-9a-f]{8,}\b", "#", text)
    text = re.sub(r"\d+(?:\.\d+)?", "#", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120]


def _log_patterns(logs: list[LogEntry]) -> list[dict]:
    groups: dict[str, list[LogEntry]] = defaultdict(list)
    for log in logs:
        groups[_normalise_signature(log.message)].append(log)

    patterns = []
    for signature, entries in groups.items():
        entries.sort(key=lambda entry: (entry.ts, entry.log_id))
        levels = sorted({entry.level for entry in entries})
        patterns.append(
            {
                "signature": signature,
                "count": len(entries),
                "levels": levels,
                "first_seen": iso(entries[0].ts),
                "last_seen": iso(entries[-1].ts),
                "evidence": [entry.log_id for entry in entries],
                "example": entries[0].message,
            }
        )
    return sorted(patterns, key=lambda item: (-item["count"], item["signature"]))


def _recent_change_text(change: Change, incident: Incident) -> str:
    delta = (incident.started_at - change.changed_at).total_seconds() / 60
    if delta >= 0:
        timing = f"{delta:.0f} min before the incident"
    else:
        timing = f"{abs(delta):.0f} min after the incident started"
    return f"{change.change_id} ({change.type} {change.target}) {timing}: {change.summary}"


def _hypothesis(
    cause: str,
    score: float,
    summary: str,
    calculation: str,
    evidence: list[str],
    *,
    action: dict | None = None,
) -> dict:
    if score >= 0.8:
        confidence = "high"
    elif score >= 0.5:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "cause": cause,
        "label": CAUSE_LABELS.get(cause, cause),
        "confidence": confidence,
        "score": round(score, 2),
        "summary": summary,
        "calculation": calculation,
        "evidence": list(dict.fromkeys(evidence)),
        "action": action,
    }


def analyze_incident(store: DataStore, incident_id: str) -> dict:
    incident = store.get_incident(incident_id)
    service = store.get_service(incident.service_id)
    runbook = store.get_runbook(service.service_id)

    metric_stats = metric_report(store, incident)
    anomalies = [item for item in metric_stats if item["is_anomaly"]]
    logs = store.query_logs(incident.service_id, incident.started_at, incident.window_end)
    patterns = _log_patterns(logs)
    changes = store.query_changes(
        incident.service_id,
        incident.started_at - timedelta(hours=2),
        incident.window_end,
    )
    logs_text = " ".join(log.message.casefold() for log in logs)
    change_text = " ".join(
        f"{change.type} {change.target} {change.summary}".casefold()
        for change in changes
    )

    deploy_changes = [change for change in changes if change.type == "deploy"]
    flag_changes = [change for change in changes if change.type == "feature_flag"]
    anomaly_metrics = {item["metric"]: item for item in anomalies}
    latency_like = [
        item
        for item in anomalies
        if item["metric"] in {"latency_p95_ms", "error_rate_pct"}
    ]
    pool_anomalies = [
        item
        for item in anomalies
        if item["metric"] in {"active_connections", "pool_wait_ms"}
    ]
    disk_anomalies = [
        item
        for item in anomalies
        if item["metric"] == "disk_used_pct" and (item["peak"] or 0) >= 85
    ]
    traffic_anomalies = [
        item
        for item in anomalies
        if item["metric"] in {"queries_per_sec", "request_rate_rps"}
        and (item["ratio"] or 0) >= 2
    ]
    pool_logs = [
        log
        for log in logs
        if "pool" in log.message.casefold() or "connection" in log.message.casefold()
    ]
    flag_logs = [log for log in logs if "flag" in log.message.casefold()]

    hypotheses: list[dict] = []

    if deploy_changes and latency_like:
        nearest = max(deploy_changes, key=lambda change: change.changed_at)
        minutes_before = (incident.started_at - nearest.changed_at).total_seconds() / 60
        within_hour = 0 <= minutes_before <= 60
        score = 0.95 if within_hour else 0.72
        metric_names = ", ".join(item["metric"] for item in latency_like)
        hypotheses.append(
            _hypothesis(
                "bad_deploy",
                score,
                (
                    f"Deploy {nearest.change_id} ({nearest.target}) landed "
                    f"{minutes_before:.0f} min before the spike, and {metric_names} "
                    "moved well outside the baseline."
                ),
                (
                    f"{_recent_change_text(nearest, incident)}; "
                    + "; ".join(item["calculation"] for item in latency_like)
                ),
                [nearest.change_id]
                + [item["evidence"] for item in latency_like]
                + [log.log_id for log in logs[:5]],
                action={
                    "action": "rollback_deploy",
                    "target": nearest.target,
                    "reason": (
                        f"Roll back {nearest.target} because deploy {nearest.change_id} "
                        "preceded the latency/error spike."
                    ),
                    "runbook_step": runbook.steps[2] if len(runbook.steps) > 2 else None,
                },
            )
        )

    if pool_logs and pool_anomalies:
        hypotheses.append(
            _hypothesis(
                "connection_pool_exhaustion",
                0.95,
                (
                    f"Connection pool logs ({len(pool_logs)} events) coincide with "
                    f"{', '.join(item['metric'] for item in pool_anomalies)} crossing "
                    "their thresholds."
                ),
                "; ".join(item["calculation"] for item in pool_anomalies)
                + f"; log evidence: {', '.join(log.log_id for log in pool_logs[:5])}",
                [item["evidence"] for item in pool_anomalies]
                + [log.log_id for log in pool_logs],
                action={
                    "action": "raise_db_pool",
                    "target": incident.service_id,
                    "reason": (
                        "Increase the database connection ceiling and recycle idle "
                        "connections while traffic is above baseline."
                    ),
                    "runbook_step": runbook.steps[2] if len(runbook.steps) > 2 else None,
                },
            )
        )
    elif pool_logs or pool_anomalies:
        hypotheses.append(
            _hypothesis(
                "connection_pool_exhaustion",
                0.7,
                "Only one of logs or pool metrics supports connection exhaustion.",
                "; ".join(item["calculation"] for item in pool_anomalies)
                or "Pool-related logs were found without a metric anomaly.",
                [item["evidence"] for item in pool_anomalies]
                + [log.log_id for log in pool_logs],
            )
        )

    if flag_changes and flag_logs:
        nearest = max(flag_changes, key=lambda change: change.changed_at)
        error_metrics = [
            item for item in anomalies if item["metric"] == "error_rate_pct"
        ]
        hypotheses.append(
            _hypothesis(
                "feature_flag_regression",
                0.95,
                (
                    f"Flag change {nearest.change_id} ({nearest.target}) precedes "
                    f"{len(flag_logs)} flag-related errors."
                ),
                (
                    f"{_recent_change_text(nearest, incident)}; "
                    f"error-rate evidence: "
                    f"{'; '.join(item['calculation'] for item in error_metrics) or 'n/a'}; "
                    f"log evidence: {', '.join(log.log_id for log in flag_logs[:5])}"
                ),
                [nearest.change_id]
                + [item["evidence"] for item in error_metrics]
                + [log.log_id for log in flag_logs],
                action={
                    "action": "disable_feature_flag",
                    "target": nearest.target,
                    "reason": (
                        f"Disable {nearest.target}; it was enabled before the 5xx "
                        "spike and flag errors reference the same flag."
                    ),
                    "runbook_step": runbook.steps[1] if len(runbook.steps) > 1 else None,
                },
            )
        )
    elif flag_changes or flag_logs:
        hypotheses.append(
            _hypothesis(
                "feature_flag_regression",
                0.7,
                "A flag change or flag-related logs are present, but not both.",
                (
                    f"flag changes: {', '.join(change.change_id for change in flag_changes) or 'none'}; "
                    f"flag logs: {', '.join(log.log_id for log in flag_logs) or 'none'}"
                ),
                [change.change_id for change in flag_changes]
                + [log.log_id for log in flag_logs],
            )
        )

    if disk_anomalies:
        item = disk_anomalies[0]
        hypotheses.append(
            _hypothesis(
                "disk_pressure",
                0.95,
                "Disk usage crossed the 85% safety threshold and log rotation is implicated.",
                item["calculation"]
                + (
                    f"; log evidence: {', '.join(log.log_id for log in logs if 'disk' in log.message.casefold() or 'rotation' in log.message.casefold())}"
                ),
                [item["evidence"]]
                + [
                    log.log_id
                    for log in logs
                    if "disk" in log.message.casefold()
                    or "rotation" in log.message.casefold()
                ],
                action={
                    "action": "rotate_logs",
                    "target": incident.service_id,
                    "reason": (
                        "Rotate logs and remove expired indices to bring disk usage "
                        "back under the safety threshold."
                    ),
                    "runbook_step": runbook.steps[2] if len(runbook.steps) > 2 else None,
                },
            )
        )

    if traffic_anomalies:
        item = traffic_anomalies[0]
        hypotheses.append(
            _hypothesis(
                "traffic_spike",
                0.75,
                (
                    f"{item['metric']} is {item['ratio']:.2f}x its baseline; this "
                    "explains pressure but not necessarily the root cause."
                ),
                item["calculation"],
                [item["evidence"]],
                action={
                    "action": "scale_service",
                    "target": incident.service_id,
                    "reason": f"Scale {incident.service_id} while traffic is above baseline.",
                    "runbook_step": None,
                },
            )
        )

    if not hypotheses:
        hypotheses.append(
            _hypothesis(
                "unknown",
                0.1,
                (
                    "No metric crossed its threshold, no recent change explains the "
                    "symptom, and the logs do not contain a strong failure signature."
                ),
                (
                    f"checked {len(metric_stats)} metric series; "
                    f"anomalies: {', '.join(item['metric'] for item in anomalies) or 'none'}; "
                    f"changes: {', '.join(change.change_id for change in changes) or 'none'}"
                ),
                [item["evidence"] for item in anomalies]
                + [change.change_id for change in changes]
                + [log.log_id for log in logs],
            )
        )

    hypotheses.sort(key=lambda item: (-item["score"], item["cause"]))
    top = hypotheses[0]
    recommended = top["action"] if top["confidence"] == "high" else None
    needs_human_input = recommended is None

    if needs_human_input:
        if top["cause"] == "unknown":
            question = (
                "The evidence does not support a high-confidence cause. Should I "
                "widen the time window, inspect a dependency, or look at traces?"
            )
        else:
            question = (
                f"The leading hypothesis is {top['label']} with {top['confidence']} "
                "confidence. Should I investigate further before proposing a change?"
            )
    else:
        question = None

    evidence = (
        [item["evidence"] for item in anomalies]
        + [change.change_id for change in changes]
        + [log.log_id for log in logs]
        + [ref for item in hypotheses for ref in item["evidence"]]
    )

    summary = (
        f"{top['label']} ({top['confidence']} confidence, score {top['score']:.2f}). "
        f"{top['summary']}"
    )

    return {
        "analysis_id": f"AN-{incident_id}",
        "as_of": iso(incident.detected_at),
        "incident": incident.to_dict(),
        "service": service.to_dict(),
        "window": {
            "baseline_start": iso(incident.baseline_start),
            "started_at": iso(incident.started_at),
            "ended_at": iso(incident.window_end),
            "baseline_minutes": BASELINE_MINUTES,
            "window_minutes": incident.window_minutes,
        },
        "metrics": metric_stats,
        "anomalies": anomalies,
        "log_patterns": patterns,
        "changes": [change.to_dict() for change in changes],
        "runbook": runbook.to_dict(),
        "hypotheses": hypotheses,
        "recommended_action": recommended,
        "needs_human_input": needs_human_input,
        "question": question,
        "evidence": list(dict.fromkeys(evidence)),
        "summary": summary,
        "assumptions": [
            f"Baseline is the {BASELINE_MINUTES} minutes immediately before the incident.",
            f"Incident window is {incident.window_minutes} minutes from started_at.",
            f"Metric ratio threshold is {RATIO_THRESHOLD:.2f}x; absolute thresholds are "
            f"{', '.join(f'{key}>={value}' for key, value in sorted(ABSOLUTE_THRESHOLDS.items()))}.",
            "Only the mock telemetry in data/ is used; no external monitoring is queried.",
            "Logs and changes are evidence, not proof of causation; low confidence means ask.",
        ],
    }
