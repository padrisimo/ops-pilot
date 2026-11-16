"""JSON-backed sandbox for drafts, applied actions and the audit log.

The sandbox is deliberately separate from ``data/``.  A human approval creates
an append-only ledger entry; rollback appends a reversal and marks the original
action as rolled back.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data import DataStore, Incident, iso

DEFAULT_SANDBOX_DIR = Path(__file__).resolve().parents[2] / "sandbox"

SERVICE_ACTIONS = {"raise_db_pool", "rotate_logs", "scale_service"}
CHANGE_ACTIONS = {
    "rollback_deploy": "deploy",
    "disable_feature_flag": "feature_flag",
}


class SandboxError(Exception):
    """Base class for sandbox errors."""


class DraftNotFound(SandboxError):
    pass


class ActionNotFound(SandboxError):
    pass


class ValidationError(SandboxError):
    pass


def _now() -> str:
    return iso(datetime.now(timezone.utc))


class SandboxStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(
            root or os.getenv("OPS_PILOT_SANDBOX_DIR", DEFAULT_SANDBOX_DIR)
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.drafts_path = self.root / "drafts.json"
        self.ledger_path = self.root / "ledger.json"
        self.audit_path = self.root / "audit_log.json"
        self._lock = threading.RLock()
        for path in (self.drafts_path, self.ledger_path, self.audit_path):
            if not path.exists():
                self._write(path, [])

    # -- persistence ---------------------------------------------------

    def _read(self, path: Path) -> list[dict]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as exc:
            raise SandboxError(f"Invalid JSON in {path}: {exc}") from exc
        if not isinstance(raw, list):
            raise SandboxError(f"{path} must contain a list")
        return raw

    def _write(self, path: Path, rows: list[dict]) -> None:
        payload = json.dumps(rows, indent=2, sort_keys=False) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.root,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        temp_path.replace(path)

    def _next_id(self, prefix: str, rows: list[dict], key: str) -> str:
        numbers = []
        for row in rows:
            value = str(row.get(key, ""))
            if value.startswith(prefix) and value[len(prefix) :].isdigit():
                numbers.append(int(value[len(prefix) :]))
        return f"{prefix}{max(numbers, default=0) + 1:04d}"

    def _audit(
        self,
        event: str,
        *,
        draft_id: str | None = None,
        action_id: str | None = None,
        actor: str = "system",
        details: dict[str, Any] | None = None,
    ) -> dict:
        with self._lock:
            rows = self._read(self.audit_path)
            entry = {
                "event_id": self._next_id("EVT-", rows, "event_id"),
                "ts": _now(),
                "event": event,
                "draft_id": draft_id,
                "action_id": action_id,
                "actor": actor,
                "details": details or {},
            }
            rows.append(entry)
            self._write(self.audit_path, rows)
            return entry

    # -- reads ---------------------------------------------------------

    def read_drafts(self) -> list[dict]:
        with self._lock:
            return self._read(self.drafts_path)

    def get_draft(self, draft_id: str) -> dict:
        for draft in self.read_drafts():
            if draft["draft_id"] == draft_id:
                return draft
        raise DraftNotFound(f"Draft {draft_id!r} not found")

    def read_ledger(self) -> list[dict]:
        with self._lock:
            return self._read(self.ledger_path)

    def get_action(self, action_id: str) -> dict:
        for action in self.read_ledger():
            if action["action_id"] == action_id:
                return action
        raise ActionNotFound(f"Action {action_id!r} not found")

    def read_audit(self, limit: int | None = None) -> list[dict]:
        with self._lock:
            rows = self._read(self.audit_path)
        return rows[-limit:] if limit is not None else rows

    def pending_drafts(self, incident_id: str | None = None) -> list[dict]:
        return [
            draft
            for draft in self.read_drafts()
            if draft.get("status") == "pending"
            and (incident_id is None or draft.get("incident_id") == incident_id)
        ]

    # -- validation ----------------------------------------------------

    def validate_remediation(
        self,
        data: DataStore,
        incident_id: str,
        action: str,
        target: str,
    ) -> tuple[Incident, dict | None]:
        incident = data.get_incident(incident_id)
        action = (action or "").strip().lower()
        target = (target or "").strip()

        if not action:
            raise ValidationError("action is required")
        if not target:
            raise ValidationError("target is required")

        if action in SERVICE_ACTIONS:
            if target != incident.service_id:
                raise ValidationError(
                    f"{action} must target the incident service "
                    f"{incident.service_id!r}, not {target!r}"
                )
            return incident, None

        change_type = CHANGE_ACTIONS.get(action)
        if change_type is None:
            raise ValidationError(
                f"Unsupported action {action!r}. Allowed: "
                f"{', '.join(sorted(SERVICE_ACTIONS | set(CHANGE_ACTIONS)))}"
            )

        matches = [
            change
            for change in data.changes
            if change.service_id == incident.service_id
            and change.type == change_type
            and target in {change.target, change.change_id}
        ]
        if not matches:
            raise ValidationError(
                f"No {change_type} change for {incident.service_id!r} matches "
                f"target {target!r}"
            )
        return incident, max(matches, key=lambda change: change.changed_at).to_dict()

    # -- writes --------------------------------------------------------

    def propose_remediation(
        self,
        data: DataStore,
        incident_id: str,
        action: str,
        target: str,
        reason: str,
        *,
        actor: str = "agent",
        evidence: list[str] | None = None,
    ) -> dict:
        action = (action or "").strip().lower()
        target = (target or "").strip()
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("reason is required before proposing a remediation")

        incident, change = self.validate_remediation(data, incident_id, action, target)

        with self._lock:
            drafts = self._read(self.drafts_path)
            for existing in drafts:
                if (
                    existing.get("incident_id") == incident.incident_id
                    and existing.get("action") == action
                    and existing.get("target") == target
                    and existing.get("status") in {"pending", "applied"}
                ):
                    raise ValidationError(
                        f"Duplicate remediation: {existing['draft_id']} is "
                        f"{existing['status']} for {incident.incident_id} {action} {target}"
                    )

            draft = {
                "draft_id": self._next_id("RMD-", drafts, "draft_id"),
                "incident_id": incident.incident_id,
                "service_id": incident.service_id,
                "action": action,
                "target": target,
                "reason": reason,
                "status": "pending",
                "created_at": _now(),
                "created_by": actor,
                "evidence": list(dict.fromkeys(evidence or [])),
                "change_id": change["change_id"] if change else None,
                "change_summary": change["summary"] if change else None,
            }
            drafts.append(draft)
            self._write(self.drafts_path, drafts)

        self._audit(
            "propose",
            draft_id=draft["draft_id"],
            actor=actor,
            details={
                "incident_id": incident.incident_id,
                "action": action,
                "target": target,
                "reason": reason,
            },
        )
        return draft

    def reject_draft(self, draft_id: str, *, actor: str = "human") -> dict:
        with self._lock:
            drafts = self._read(self.drafts_path)
            draft = next(
                (item for item in drafts if item["draft_id"] == draft_id), None
            )
            if draft is None:
                raise DraftNotFound(f"Draft {draft_id!r} not found")
            if draft["status"] == "rejected":
                return draft
            if draft["status"] != "pending":
                raise ValidationError(
                    f"Draft {draft_id} is {draft['status']}, not pending"
                )
            draft["status"] = "rejected"
            draft["rejected_at"] = _now()
            draft["rejected_by"] = actor
            self._write(self.drafts_path, drafts)

        self._audit(
            "reject",
            draft_id=draft_id,
            actor=actor,
            details={"incident_id": draft["incident_id"], "action": draft["action"]},
        )
        return draft

    def apply_draft(
        self, data: DataStore, draft_id: str, *, actor: str = "human"
    ) -> dict:
        with self._lock:
            drafts = self._read(self.drafts_path)
            draft = next(
                (item for item in drafts if item["draft_id"] == draft_id), None
            )
            if draft is None:
                raise DraftNotFound(f"Draft {draft_id!r} not found")

            if draft["status"] == "applied":
                action = next(
                    (
                        item
                        for item in self._read(self.ledger_path)
                        if item.get("draft_id") == draft_id
                        and item.get("action") != "rollback"
                        and item.get("status") == "applied"
                    ),
                    None,
                )
                self._audit(
                    "apply_noop",
                    draft_id=draft_id,
                    action_id=action["action_id"] if action else draft.get("action_id"),
                    actor=actor,
                    details={"reason": "draft already applied"},
                )
                return {"status": "noop", "draft": draft, "action": action}
            if draft["status"] != "pending":
                raise ValidationError(
                    f"Draft {draft_id} is {draft['status']}, not pending"
                )

            self.validate_remediation(
                data, draft["incident_id"], draft["action"], draft["target"]
            )

            ledger = self._read(self.ledger_path)
            action = {
                "action_id": self._next_id("ACT-", ledger, "action_id"),
                "draft_id": draft_id,
                "incident_id": draft["incident_id"],
                "service_id": draft["service_id"],
                "action": draft["action"],
                "target": draft["target"],
                "status": "applied",
                "applied_at": _now(),
                "applied_by": actor,
                "reversal_of": None,
                "details": {
                    "reason": draft["reason"],
                    "evidence": draft.get("evidence", []),
                },
            }
            ledger.append(action)
            self._write(self.ledger_path, ledger)

            draft["status"] = "applied"
            draft["applied_at"] = action["applied_at"]
            draft["applied_by"] = actor
            draft["action_id"] = action["action_id"]
            self._write(self.drafts_path, drafts)

        self._audit(
            "apply",
            draft_id=draft_id,
            action_id=action["action_id"],
            actor=actor,
            details={
                "incident_id": action["incident_id"],
                "action": action["action"],
                "target": action["target"],
            },
        )
        return {"status": "applied", "draft": draft, "action": action}

    def rollback_action(self, action_id: str, *, actor: str = "human") -> dict:
        with self._lock:
            ledger = self._read(self.ledger_path)
            original = next(
                (item for item in ledger if item["action_id"] == action_id), None
            )
            if original is None:
                raise ActionNotFound(f"Action {action_id!r} not found")
            if original["status"] == "rolled_back":
                self._audit(
                    "rollback_noop",
                    action_id=action_id,
                    actor=actor,
                    details={"reason": "action already rolled back"},
                )
                return {"status": "noop", "action": original}
            if original["status"] != "applied" or original["action"] == "rollback":
                raise ValidationError(
                    f"Action {action_id} is {original['status']} and cannot be rolled back"
                )

            reversal = {
                "action_id": self._next_id("ACT-", ledger, "action_id"),
                "draft_id": original["draft_id"],
                "incident_id": original["incident_id"],
                "service_id": original["service_id"],
                "action": "rollback",
                "target": original["action_id"],
                "status": "applied",
                "applied_at": _now(),
                "applied_by": actor,
                "reversal_of": original["action_id"],
                "details": {"reason": f"Reversal of {original['action_id']}"},
            }
            ledger.append(reversal)
            original["status"] = "rolled_back"
            original["rolled_back_at"] = reversal["applied_at"]
            original["rollback_action_id"] = reversal["action_id"]
            self._write(self.ledger_path, ledger)

            drafts = self._read(self.drafts_path)
            for draft in drafts:
                if draft["draft_id"] == original["draft_id"]:
                    draft["status"] = "rolled_back"
                    draft["rolled_back_at"] = reversal["applied_at"]
                    draft["rollback_action_id"] = reversal["action_id"]
                    break
            self._write(self.drafts_path, drafts)

        self._audit(
            "rollback",
            draft_id=original["draft_id"],
            action_id=reversal["action_id"],
            actor=actor,
            details={
                "reversal_of": original["action_id"],
                "incident_id": original["incident_id"],
            },
        )
        return {"status": "rolled_back", "action": original, "reversal": reversal}

    def reject_rollback(self, action_id: str, *, actor: str = "human") -> dict:
        action = self.get_action(action_id)
        self._audit(
            "rollback_reject",
            draft_id=action.get("draft_id"),
            action_id=action_id,
            actor=actor,
            details={"reason": "human rejected rollback"},
        )
        return {"status": "rejected", "action": action}
