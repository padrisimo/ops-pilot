import pytest

from ops_pilot.data import DataStore
from ops_pilot.sandbox import (
    ActionNotFound,
    DraftNotFound,
    SandboxStore,
    ValidationError,
)


def test_propose_creates_pending_draft(sandbox: SandboxStore, store: DataStore) -> None:
    draft = sandbox.propose_remediation(
        store,
        incident_id="INC-1001",
        action="rollback_deploy",
        target="web@2026.11.14-3",
        reason="Deploy preceded the latency spike.",
        evidence=["CHG-2001", "metric:checkout-api:latency_p95_ms"],
    )

    assert draft["draft_id"] == "RMD-0001"
    assert draft["status"] == "pending"
    assert draft["change_id"] == "CHG-2001"
    assert draft["change_summary"]
    assert sandbox.pending_drafts("INC-1001") == [draft]
    assert [row["event"] for row in sandbox.read_audit()] == ["propose"]


def test_duplicate_pending_draft_is_rejected(sandbox: SandboxStore, store: DataStore) -> None:
    kwargs = dict(
        incident_id="INC-1002",
        action="raise_db_pool",
        target="payments-db",
        reason="Pool exhausted.",
    )
    sandbox.propose_remediation(store, **kwargs)

    with pytest.raises(ValidationError, match="Duplicate remediation"):
        sandbox.propose_remediation(store, **kwargs)


def test_reason_is_required(sandbox: SandboxStore, store: DataStore) -> None:
    with pytest.raises(ValidationError, match="reason is required"):
        sandbox.propose_remediation(
            store,
            incident_id="INC-1002",
            action="raise_db_pool",
            target="payments-db",
            reason="   ",
        )


def test_action_validation(sandbox: SandboxStore, store: DataStore) -> None:
    with pytest.raises(ValidationError, match="Unsupported action"):
        sandbox.propose_remediation(
            store,
            incident_id="INC-1001",
            action="delete_everything",
            target="checkout-api",
            reason="nope",
        )

    with pytest.raises(ValidationError, match="must target the incident service"):
        sandbox.propose_remediation(
            store,
            incident_id="INC-1001",
            action="scale_service",
            target="search-api",
            reason="wrong service",
        )

    with pytest.raises(ValidationError, match="matches target"):
        sandbox.propose_remediation(
            store,
            incident_id="INC-1001",
            action="rollback_deploy",
            target="web@does-not-exist",
            reason="no such deploy",
        )


def test_apply_writes_ledger_and_marks_draft(sandbox: SandboxStore, store: DataStore) -> None:
    draft = sandbox.propose_remediation(
        store,
        incident_id="INC-1001",
        action="rollback_deploy",
        target="web@2026.11.14-3",
        reason="Deploy preceded the spike.",
    )

    result = sandbox.apply_draft(store, draft["draft_id"], actor="adrian")

    assert result["status"] == "applied"
    assert result["action"]["action_id"] == "ACT-0001"
    assert result["action"]["applied_by"] == "adrian"
    assert sandbox.get_draft(draft["draft_id"])["status"] == "applied"
    assert [row["event"] for row in sandbox.read_audit()] == ["propose", "apply"]


def test_apply_is_idempotent(sandbox: SandboxStore, store: DataStore) -> None:
    draft = sandbox.propose_remediation(
        store,
        incident_id="INC-1002",
        action="raise_db_pool",
        target="payments-db",
        reason="Pool exhausted.",
    )
    first = sandbox.apply_draft(store, draft["draft_id"])
    second = sandbox.apply_draft(store, draft["draft_id"])

    assert first["status"] == "applied"
    assert second["status"] == "noop"
    assert len(sandbox.read_ledger()) == 1
    assert sandbox.read_audit()[-1]["event"] == "apply_noop"


def test_reject_prevents_apply(sandbox: SandboxStore, store: DataStore) -> None:
    draft = sandbox.propose_remediation(
        store,
        incident_id="INC-1004",
        action="rotate_logs",
        target="search-api",
        reason="Disk above 85%.",
    )

    rejected = sandbox.reject_draft(draft["draft_id"], actor="adrian")
    assert rejected["status"] == "rejected"
    assert sandbox.read_ledger() == []

    with pytest.raises(ValidationError, match="not pending"):
        sandbox.apply_draft(store, draft["draft_id"])

    assert sandbox.reject_draft(draft["draft_id"])["status"] == "rejected"
    assert sandbox.read_audit()[-1]["event"] == "reject"


def test_rollback_is_append_only(sandbox: SandboxStore, store: DataStore) -> None:
    draft = sandbox.propose_remediation(
        store,
        incident_id="INC-1003",
        action="disable_feature_flag",
        target="new-pricing",
        reason="Flag caused 5xx.",
    )
    applied = sandbox.apply_draft(store, draft["draft_id"])
    original_id = applied["action"]["action_id"]

    rolled = sandbox.rollback_action(original_id, actor="adrian")

    assert rolled["status"] == "rolled_back"
    assert rolled["reversal"]["action"] == "rollback"
    assert rolled["reversal"]["reversal_of"] == original_id
    assert sandbox.get_action(original_id)["status"] == "rolled_back"
    assert sandbox.get_draft(draft["draft_id"])["status"] == "rolled_back"
    assert len(sandbox.read_ledger()) == 2

    again = sandbox.rollback_action(original_id)
    assert again["status"] == "noop"
    assert len(sandbox.read_ledger()) == 2
    assert sandbox.read_audit()[-1]["event"] == "rollback_noop"


def test_rollback_requires_applied_action(sandbox: SandboxStore, store: DataStore) -> None:
    draft = sandbox.propose_remediation(
        store,
        incident_id="INC-1002",
        action="raise_db_pool",
        target="payments-db",
        reason="Pool exhausted.",
    )
    applied = sandbox.apply_draft(store, draft["draft_id"])
    sandbox.rollback_action(applied["action"]["action_id"])

    # A second rollback is an explicit no-op, not a second reversal.
    assert sandbox.rollback_action(applied["action"]["action_id"])["status"] == "noop"

    with pytest.raises(ActionNotFound, match="ACT-9999"):
        sandbox.rollback_action("ACT-9999")

    with pytest.raises(DraftNotFound, match="RMD-9999"):
        sandbox.get_draft("RMD-9999")


def test_reject_rollback_keeps_action_applied(sandbox: SandboxStore, store: DataStore) -> None:
    draft = sandbox.propose_remediation(
        store,
        incident_id="INC-1004",
        action="rotate_logs",
        target="search-api",
        reason="Disk above 85%.",
    )
    applied = sandbox.apply_draft(store, draft["draft_id"])
    original_id = applied["action"]["action_id"]

    result = sandbox.reject_rollback(original_id, actor="adrian")

    assert result["status"] == "rejected"
    assert sandbox.get_action(original_id)["status"] == "applied"
    assert sandbox.read_audit()[-1]["event"] == "rollback_reject"


def test_state_persists_across_instances(tmp_path, store: DataStore) -> None:
    root = tmp_path / "sandbox"
    first = SandboxStore(root)
    draft = first.propose_remediation(
        store,
        incident_id="INC-1001",
        action="rollback_deploy",
        target="web@2026.11.14-3",
        reason="Deploy preceded the spike.",
    )
    first.apply_draft(store, draft["draft_id"])

    second = SandboxStore(root)

    assert second.get_draft(draft["draft_id"])["status"] == "applied"
    assert [action["action_id"] for action in second.read_ledger()] == ["ACT-0001"]
    assert [row["event"] for row in second.read_audit()] == ["propose", "apply"]
