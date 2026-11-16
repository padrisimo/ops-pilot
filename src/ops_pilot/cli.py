"""Small terminal REPL for OpsPilot."""

from __future__ import annotations

import json
import os
import uuid

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .context import default_context
from .graph import build_graph, pending_interrupts, resume, thread_config


def _preview(value, limit: int = 500) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def _render(messages: list, start: int = 0) -> None:
    for message in messages[start:]:
        if isinstance(message, HumanMessage):
            continue
        if isinstance(message, AIMessage):
            if message.content:
                print(f"\nOpsPilot: {message.content}")
            for call in message.tool_calls or []:
                print(f"  -> {call['name']}({_preview(call.get('args', {}), 240)})")
        elif isinstance(message, ToolMessage):
            print(f"  <- {message.name}: {_preview(message.content, 600)}")


def _handle_interrupt(graph, config, state) -> bool:
    interrupts = pending_interrupts(graph, config)
    if not interrupts:
        return False
    payload = interrupts[0].value or {}
    print("\n" + "=" * 60)
    print("HUMAN APPROVAL REQUIRED")
    print(json.dumps(payload, indent=2, default=str))
    answer = input("\nApprove? [y/N] ").strip().lower()
    decision = "approve" if answer in {"y", "yes", "s", "si", "sí"} else "reject"
    count = len(state.get("messages", []))
    new_state = resume(graph, config, decision)
    _render(new_state.get("messages", []), count)
    return True


def main() -> None:
    load_dotenv()
    try:
        ctx = default_context()
        graph = build_graph(ctx)
    except Exception as exc:  # pragma: no cover - configuration feedback
        print(f"Could not start OpsPilot: {exc}")
        print("Copy .env.example to .env and configure MODEL plus an API key.")
        return

    thread_id = os.getenv("OPS_PILOT_THREAD_ID", f"cli-{uuid.uuid4().hex[:8]}")
    config = thread_config(thread_id)
    state = graph.get_state(config)
    messages = state.values.get("messages", []) if state.values else []

    print("OpsPilot — approval-gated incident commander")
    print(f"Thread: {thread_id}")
    print("Commands: /incidents, /audit, /new, /quit")
    for incident in ctx.data.list_incidents(status="open"):
        print(
            f"  {incident.incident_id}  {incident.severity:<5} "
            f"{incident.service_id:<12} {incident.title}"
        )

    while True:
        if _handle_interrupt(graph, config, {"messages": messages}):
            state = graph.get_state(config)
            messages = state.values.get("messages", [])
            continue

        try:
            line = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            print("Bye.")
            return
        if line == "/new":
            thread_id = f"cli-{uuid.uuid4().hex[:8]}"
            config = thread_config(thread_id)
            messages = []
            print(f"New thread: {thread_id}")
            continue
        if line == "/incidents":
            for incident in ctx.data.list_incidents():
                print(
                    f"{incident.incident_id}  {incident.status:<8} "
                    f"{incident.severity:<5} {incident.service_id:<12} "
                    f"{incident.title}"
                )
            continue
        if line == "/audit":
            rows = ctx.sandbox.read_audit(limit=20)
            print(_preview(rows, 4000))
            continue

        count = len(messages)
        try:
            result = graph.invoke(
                {"messages": [HumanMessage(content=line)]}, config
            )
        except Exception as exc:  # pragma: no cover - provider/runtime feedback
            print(f"Error: {exc}")
            continue
        messages = result.get("messages", [])
        _render(messages, count)


if __name__ == "__main__":
    main()
