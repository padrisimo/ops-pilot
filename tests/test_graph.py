import json

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from ops_pilot.context import AppContext
from ops_pilot.graph import build_checkpointer
from ops_pilot.tools import build_tools


def _tool(ctx: AppContext, name: str):
    return next(tool for tool in build_tools(ctx) if tool.name == name)


def _tool_graph(ctx: AppContext, name: str, checkpointer: MemorySaver):
    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([_tool(ctx, name)]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    return builder.compile(checkpointer=checkpointer)


def _payload(message) -> dict:
    content = message.content
    if isinstance(content, dict):
        return content
    return json.loads(content)


def _call(name: str, args: dict) -> dict:
    return {"messages": [AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1"}])]}


def _pending(graph, config) -> list:
    state = graph.get_state(config)
    return [item for task in state.tasks for item in task.interrupts]


def test_apply_pauses_for_approval_then_applies(ctx: AppContext) -> None:
    draft = ctx.sandbox.propose_remediation(
        ctx.data,
        incident_id="INC-1001",
        action="rollback_deploy",
        target="web@2026.11.14-3",
        reason="Deploy preceded the latency spike.",
    )
    graph = _tool_graph(ctx, "apply_remediation", MemorySaver())
    config = {"configurable": {"thread_id": "apply-approve"}}

    graph.invoke(_call("apply_remediation", {"draft_id": draft["draft_id"]}), config)

    interrupts = _pending(graph, config)
    assert len(interrupts) == 1
    payload = interrupts[0].value
    assert payload["action"] == "apply_remediation"
    assert payload["draft"]["draft_id"] == draft["draft_id"]
    # Nothing was written before approval.
    assert ctx.sandbox.read_ledger() == []

    graph.invoke(Command(resume="approve"), config)

    action = ctx.sandbox.read_ledger()[0]
    assert action["draft_id"] == draft["draft_id"]
    assert action["status"] == "applied"
    assert ctx.sandbox.get_draft(draft["draft_id"])["status"] == "applied"


def test_apply_rejection_writes_no_ledger_entry(ctx: AppContext) -> None:
    draft = ctx.sandbox.propose_remediation(
        ctx.data,
        incident_id="INC-1002",
        action="raise_db_pool",
        target="payments-db",
        reason="Pool exhausted.",
    )
    graph = _tool_graph(ctx, "apply_remediation", MemorySaver())
    config = {"configurable": {"thread_id": "apply-reject"}}

    graph.invoke(_call("apply_remediation", {"draft_id": draft["draft_id"]}), config)
    assert _pending(graph, config)

    graph.invoke(Command(resume="reject"), config)

    assert ctx.sandbox.read_ledger() == []
    assert ctx.sandbox.get_draft(draft["draft_id"])["status"] == "rejected"


def test_invalid_approval_decision_is_an_error(ctx: AppContext) -> None:
    draft = ctx.sandbox.propose_remediation(
        ctx.data,
        incident_id="INC-1004",
        action="rotate_logs",
        target="search-api",
        reason="Disk above 85%.",
    )
    graph = _tool_graph(ctx, "apply_remediation", MemorySaver())
    config = {"configurable": {"thread_id": "apply-invalid"}}

    graph.invoke(_call("apply_remediation", {"draft_id": draft["draft_id"]}), config)
    result = graph.invoke(Command(resume="maybe"), config)

    tool_message = result["messages"][-1]
    assert _payload(tool_message)["status"] == "error"
    assert "Unknown approval decision" in _payload(tool_message)["error"]
    assert ctx.sandbox.read_ledger() == []


def test_rollback_pauses_for_approval_then_reverses(ctx: AppContext) -> None:
    draft = ctx.sandbox.propose_remediation(
        ctx.data,
        incident_id="INC-1003",
        action="disable_feature_flag",
        target="new-pricing",
        reason="Flag caused the 5xx spike.",
    )
    applied = ctx.sandbox.apply_draft(ctx.data, draft["draft_id"])
    action_id = applied["action"]["action_id"]

    graph = _tool_graph(ctx, "rollback_remediation", MemorySaver())
    config = {"configurable": {"thread_id": "rollback-approve"}}

    graph.invoke(_call("rollback_remediation", {"action_id": action_id}), config)

    interrupts = _pending(graph, config)
    assert len(interrupts) == 1
    assert interrupts[0].value["action"] == "rollback_remediation"
    assert ctx.sandbox.get_action(action_id)["status"] == "applied"

    graph.invoke(Command(resume="approve"), config)

    assert ctx.sandbox.get_action(action_id)["status"] == "rolled_back"
    assert ctx.sandbox.read_ledger()[-1]["action"] == "rollback"


def test_rollback_can_be_rejected(ctx: AppContext) -> None:
    draft = ctx.sandbox.propose_remediation(
        ctx.data,
        incident_id="INC-1004",
        action="rotate_logs",
        target="search-api",
        reason="Disk above 85%.",
    )
    applied = ctx.sandbox.apply_draft(ctx.data, draft["draft_id"])
    action_id = applied["action"]["action_id"]

    graph = _tool_graph(ctx, "rollback_remediation", MemorySaver())
    config = {"configurable": {"thread_id": "rollback-reject"}}

    graph.invoke(_call("rollback_remediation", {"action_id": action_id}), config)
    graph.invoke(Command(resume="reject"), config)

    assert ctx.sandbox.get_action(action_id)["status"] == "applied"
    assert ctx.sandbox.read_audit()[-1]["event"] == "rollback_reject"


def test_apply_unknown_draft_does_not_interrupt(ctx: AppContext) -> None:
    graph = _tool_graph(ctx, "apply_remediation", MemorySaver())
    config = {"configurable": {"thread_id": "apply-missing"}}

    result = graph.invoke(_call("apply_remediation", {"draft_id": "RMD-9999"}), config)

    assert _pending(graph, config) == []
    assert _payload(result["messages"][-1])["status"] == "error"
    assert "RMD-9999" in _payload(result["messages"][-1])["error"]


def test_pending_approval_survives_a_restart(ctx: AppContext, tmp_path) -> None:
    draft = ctx.sandbox.propose_remediation(
        ctx.data,
        incident_id="INC-1001",
        action="rollback_deploy",
        target="web@2026.11.14-3",
        reason="Deploy preceded the latency spike.",
    )
    db = tmp_path / "checkpoints.sqlite"
    config = {"configurable": {"thread_id": "restart"}}

    first = _tool_graph(ctx, "apply_remediation", build_checkpointer(db))
    first.invoke(_call("apply_remediation", {"draft_id": draft["draft_id"]}), config)
    assert _pending(first, config)

    # New connection and graph instance: the approval is still pending on disk.
    second = _tool_graph(ctx, "apply_remediation", build_checkpointer(db))
    assert _pending(second, config)
    assert ctx.sandbox.read_ledger() == []

    second.invoke(Command(resume="approve"), config)

    assert ctx.sandbox.read_ledger()[0]["draft_id"] == draft["draft_id"]
    assert ctx.sandbox.get_draft(draft["draft_id"])["status"] == "applied"
