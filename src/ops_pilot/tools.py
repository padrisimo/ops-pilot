"""LangGraph tools exposed to the incident-commander agent."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from langchain_core.tools import tool
from langgraph.types import interrupt

from .analysis import _log_patterns, analyze_incident as run_analysis, metric_report
from .context import AppContext
from .data import DataError, iso
from .sandbox import SandboxError


def _error(exc: Exception, **extra: Any) -> dict:
    return {"status": "error", "error": str(exc), **extra}


def _decision(value: Any) -> str:
    if isinstance(value, bool):
        return "approve" if value else "reject"
    if isinstance(value, dict):
        value = value.get("decision", value.get("approved"))
        if isinstance(value, bool):
            return "approve" if value else "reject"
    text = str(value or "").strip().lower()
    if text in {"approve", "approved", "yes", "y", "ok", "apply"}:
        return "approve"
    if text in {"reject", "rejected", "no", "n", "cancel"}:
        return "reject"
    return "invalid"


def build_tools(ctx: AppContext) -> list:
    @tool
    def list_incidents(status: str = "open") -> dict:
        """List incidents, optionally filtered by status (open, resolved, ...)."""
        incidents = ctx.data.list_incidents(status=status or None)
        return {
            "status": "ok",
            "count": len(incidents),
            "incidents": [incident.to_dict() for incident in incidents],
        }

    @tool
    def get_incident(incident_id: str) -> dict:
        """Load one incident, its service and its runbook."""
        try:
            incident = ctx.data.get_incident(incident_id)
            service = ctx.data.get_service(incident.service_id)
            runbook = ctx.data.get_runbook(incident.service_id)
        except DataError as exc:
            return _error(exc, incident_id=incident_id)
        return {
            "status": "ok",
            "as_of": iso(incident.detected_at),
            "incident": incident.to_dict(),
            "service": service.to_dict(),
            "runbook": runbook.to_dict(),
        }

    @tool
    def query_metrics(incident_id: str, metric: str | None = None) -> dict:
        """Return metric series and baseline/peak calculations for an incident window."""
        try:
            incident = ctx.data.get_incident(incident_id)
            series = metric_report(ctx.data, incident, metric)
        except DataError as exc:
            return _error(exc, incident_id=incident_id)
        return {
            "status": "ok",
            "as_of": iso(incident.detected_at),
            "incident_id": incident.incident_id,
            "service_id": incident.service_id,
            "window": {
                "baseline_start": iso(incident.baseline_start),
                "started_at": iso(incident.started_at),
                "ended_at": iso(incident.window_end),
            },
            "series": series,
        }

    @tool
    def search_logs(
        incident_id: str,
        level: str | None = None,
        contains: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Search incident-window logs by level and substring; returns IDs and patterns."""
        try:
            incident = ctx.data.get_incident(incident_id)
            logs = ctx.data.query_logs(
                incident.service_id,
                incident.started_at,
                incident.window_end,
                level=level or None,
                contains=contains or None,
                limit=max(1, min(limit, 100)),
            )
        except DataError as exc:
            return _error(exc, incident_id=incident_id)
        return {
            "status": "ok",
            "as_of": iso(incident.detected_at),
            "incident_id": incident.incident_id,
            "service_id": incident.service_id,
            "count": len(logs),
            "logs": [log.to_dict() for log in logs],
            "patterns": _log_patterns(logs),
        }

    @tool
    def recent_changes(incident_id: str, lookback_minutes: int = 120) -> dict:
        """List deploys, flag changes and jobs around an incident."""
        try:
            incident = ctx.data.get_incident(incident_id)
            changes = ctx.data.query_changes(
                incident.service_id,
                incident.started_at - timedelta(minutes=max(1, lookback_minutes)),
                incident.window_end,
            )
        except DataError as exc:
            return _error(exc, incident_id=incident_id)
        return {
            "status": "ok",
            "as_of": iso(incident.detected_at),
            "incident_id": incident.incident_id,
            "service_id": incident.service_id,
            "count": len(changes),
            "changes": [change.to_dict() for change in changes],
        }

    @tool
    def get_runbook(service_id: str) -> dict:
        """Load the runbook for a service."""
        try:
            return {
                "status": "ok",
                "runbook": ctx.data.get_runbook(service_id).to_dict(),
            }
        except DataError as exc:
            return _error(exc, service_id=service_id)

    @tool
    def analyze_incident(incident_id: str) -> dict:
        """Run the deterministic analyser: metrics, logs, changes, hypotheses and action."""
        try:
            return {"status": "ok", **run_analysis(ctx.data, incident_id)}
        except DataError as exc:
            return _error(exc, incident_id=incident_id)

    @tool
    def propose_remediation(
        incident_id: str,
        action: str,
        target: str,
        reason: str,
        evidence: list[str] | None = None,
    ) -> dict:
        """Create a pending remediation draft. This does not change production."""
        try:
            draft = ctx.sandbox.propose_remediation(
                ctx.data,
                incident_id=incident_id,
                action=action,
                target=target,
                reason=reason,
                actor="agent",
                evidence=evidence,
            )
        except (DataError, SandboxError) as exc:
            return _error(
                exc, incident_id=incident_id, action=action, target=target
            )
        return {
            "status": "pending",
            "draft": draft,
            "message": (
                f"Draft {draft['draft_id']} created. Apply it only after a human "
                "approves; apply_remediation will pause for approval."
            ),
        }

    @tool
    def apply_remediation(draft_id: str) -> dict:
        """Apply a pending remediation draft after explicit human approval."""
        try:
            draft = ctx.sandbox.get_draft(draft_id)
            if draft["status"] == "applied":
                return ctx.sandbox.apply_draft(ctx.data, draft_id, actor="human")
            if draft["status"] != "pending":
                return _error(
                    SandboxError(
                        f"Draft {draft_id} is {draft['status']}, not pending"
                    ),
                    draft_id=draft_id,
                )
            # Pure validation before interrupt(): on resume this code runs again.
            ctx.sandbox.validate_remediation(
                ctx.data, draft["incident_id"], draft["action"], draft["target"]
            )
            decision = _decision(
                interrupt(
                    {
                        "type": "approval",
                        "action": "apply_remediation",
                        "draft": draft,
                        "question": (
                            f"Apply {draft['action']} to {draft['target']} for "
                            f"{draft['incident_id']}?"
                        ),
                    }
                )
            )
            if decision == "reject":
                rejected = ctx.sandbox.reject_draft(draft_id, actor="human")
                return {"status": "rejected", "draft": rejected}
            if decision != "approve":
                return _error(
                    SandboxError(f"Unknown approval decision: {decision!r}"),
                    draft_id=draft_id,
                )
            return ctx.sandbox.apply_draft(ctx.data, draft_id, actor="human")
        except (DataError, SandboxError) as exc:
            return _error(exc, draft_id=draft_id)

    @tool
    def rollback_remediation(action_id: str) -> dict:
        """Roll back an applied action after explicit human approval (append-only)."""
        try:
            action = ctx.sandbox.get_action(action_id)
            if action["status"] == "rolled_back":
                return ctx.sandbox.rollback_action(action_id, actor="human")
            if action["status"] != "applied" or action["action"] == "rollback":
                return _error(
                    SandboxError(
                        f"Action {action_id} is {action['status']} and cannot be rolled back"
                    ),
                    action_id=action_id,
                )
            decision = _decision(
                interrupt(
                    {
                        "type": "approval",
                        "action": "rollback_remediation",
                        "action_record": action,
                        "question": f"Roll back {action_id} for {action['incident_id']}?",
                    }
                )
            )
            if decision == "reject":
                return ctx.sandbox.reject_rollback(action_id, actor="human")
            if decision != "approve":
                return _error(
                    SandboxError(f"Unknown approval decision: {decision!r}"),
                    action_id=action_id,
                )
            return ctx.sandbox.rollback_action(action_id, actor="human")
        except (DataError, SandboxError) as exc:
            return _error(exc, action_id=action_id)

    @tool
    def get_audit_log(limit: int = 20) -> dict:
        """Read the latest audit-log entries from the sandbox."""
        rows = ctx.sandbox.read_audit(limit=max(1, min(limit, 100)))
        return {"status": "ok", "count": len(rows), "entries": rows}

    @tool
    def get_pending_drafts(incident_id: str | None = None) -> dict:
        """List remediation drafts waiting for approval."""
        drafts = ctx.sandbox.pending_drafts(incident_id)
        return {"status": "ok", "count": len(drafts), "drafts": drafts}

    return [
        list_incidents,
        get_incident,
        query_metrics,
        search_logs,
        recent_changes,
        get_runbook,
        analyze_incident,
        propose_remediation,
        apply_remediation,
        rollback_remediation,
        get_audit_log,
        get_pending_drafts,
    ]
