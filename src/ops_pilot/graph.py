"""Hand-built LangGraph incident-commander graph."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from langchain_core.messages import SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command

from .context import AppContext, default_context
from .model import build_model
from .sandbox import DEFAULT_SANDBOX_DIR
from .tools import build_tools

DEFAULT_CHECKPOINT_DB = DEFAULT_SANDBOX_DIR / "checkpoints.sqlite"
MEMORY_SENTINELS = {"memory", "in-memory", "none", "off"}

SYSTEM_PROMPT = """You are OpsPilot, an SRE incident commander.

Your job is to investigate production incidents, explain the evidence and propose
safe remediations. You are not allowed to invent metrics, logs, deploys or IDs.

Workflow:
1. Use list_incidents or get_incident to establish the incident and service.
2. Call analyze_incident before making a causal claim. It returns deterministic
   metrics, log patterns, recent changes, ranked hypotheses and a recommended action.
3. If needs_human_input is true, confidence is low, or the cause is unknown, ask the
   user a focused question. Never propose a remediation for an unknown cause.
4. If confidence is high, summarize the evidence and calculation, then call
   propose_remediation with the recommended action, target, reason and evidence IDs.
5. Show the draft_id and ask the user whether to apply it. Only call
   apply_remediation after the user explicitly says apply, approve, yes, or equivalent.
6. apply_remediation pauses for human approval. Do not claim an action was applied
   until the tool returns status=applied. If the user rejects, explain that nothing
   was written to the ledger.
7. After a successful apply, mention the action_id and offer rollback_remediation.
8. Use get_audit_log when the user asks what happened or asks for the audit trail.
9. Keep context across turns. "That incident", "it" and "the other action" refer to
   the most recently discussed incident or action.
10. State the as_of timestamp returned by tools. Cite evidence IDs (metric:...,
    L-..., CHG-...). Show the baseline -> peak calculation instead of guessing.
11. Reply in the user's language.

Safety rules:
- Never call apply_remediation without an existing pending draft.
- Never call apply_remediation on your own initiative; ask first.
- Never treat a log message as proof; say when the evidence is only a hypothesis.
- The sandbox is not production. Say that changes are sandbox changes.
"""


def checkpoint_db_path(db_path: str | Path | None = None) -> Path:
    """Resolve the checkpoint location without creating it."""
    value = db_path if db_path is not None else os.getenv("OPS_PILOT_CHECKPOINT_DB")
    if value is None or str(value).strip() == "":
        return DEFAULT_CHECKPOINT_DB
    return Path(value).expanduser()


def build_checkpointer(db_path: str | Path | None = None):
    """Build the default SQLite checkpointer, or MemorySaver when requested.

    Set ``OPS_PILOT_CHECKPOINT_DB=memory`` to opt out of persistence (useful for
    ephemeral runs and tests). The default is ``sandbox/checkpoints.sqlite``, so
    conversations and pending approvals survive a restart.
    """
    value = db_path if db_path is not None else os.getenv("OPS_PILOT_CHECKPOINT_DB")
    if value is not None and str(value).strip().lower() in MEMORY_SENTINELS:
        return MemorySaver()

    path = checkpoint_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver(connection)


def build_graph(
    ctx: AppContext | None = None,
    *,
    model=None,
    checkpointer=None,
):
    context = ctx or default_context()
    tools = build_tools(context)
    chat_model = model or build_model()
    model_with_tools = chat_model.bind_tools(tools)

    def agent_node(state: MessagesState) -> dict:
        response = model_with_tools.invoke(
            [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        )
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer or build_checkpointer())


def thread_config(thread_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 30,
    }


def pending_interrupts(graph, config: dict) -> list:
    state = graph.get_state(config)
    return [
        interrupt
        for task in state.tasks
        for interrupt in task.interrupts
    ]


def resume(graph, config: dict, decision: str):
    return graph.invoke(Command(resume=decision), config)
