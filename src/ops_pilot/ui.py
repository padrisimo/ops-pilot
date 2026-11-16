"""Streamlit chat UI with approval buttons and an audit-log viewer."""

from __future__ import annotations

import json
import os
import uuid

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .context import AppContext, default_context
from .graph import build_graph, pending_interrupts, resume, thread_config
from .model import DEFAULT_MODEL


@st.cache_resource
def get_app() -> tuple:
    ctx = default_context()
    return build_graph(ctx), ctx


def _jsonish(value):
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _render_messages(messages: list) -> None:
    for message in messages:
        if isinstance(message, HumanMessage):
            with st.chat_message("user"):
                st.markdown(message.content)
        elif isinstance(message, AIMessage):
            with st.chat_message("assistant"):
                if message.content:
                    st.markdown(message.content)
                for call in message.tool_calls or []:
                    with st.expander(f"Tool call · {call['name']}"):
                        st.json(call.get("args", {}))
        elif isinstance(message, ToolMessage):
            with st.expander(f"Evidence · {message.name}"):
                st.json(_jsonish(message.content))


def _render_approval(payload: dict) -> None:
    st.warning("Human approval required")
    st.markdown(
        f"**{payload.get('question', 'Approve this sandbox action?')}**"
    )
    if payload.get("draft"):
        draft = payload["draft"]
        st.markdown(
            f"- Draft: `{draft.get('draft_id')}`\n"
            f"- Incident: `{draft.get('incident_id')}`\n"
            f"- Action: `{draft.get('action')}`\n"
            f"- Target: `{draft.get('target')}`\n"
            f"- Reason: {draft.get('reason')}"
        )
        with st.expander("Full draft"):
            st.json(draft)
    elif payload.get("action_record"):
        with st.expander("Action to roll back"):
            st.json(payload["action_record"])


def _sidebar(ctx: AppContext) -> str | None:
    with st.sidebar:
        st.header("Incidents")
        for incident in ctx.data.list_incidents(status="open"):
            if st.button(
                f"{incident.incident_id} · {incident.title}",
                key=f"incident-{incident.incident_id}",
                use_container_width=True,
            ):
                st.session_state.pending_prompt = (
                    f"Investigate {incident.incident_id}. Start with get_incident "
                    "and analyze_incident, then explain the evidence."
                )
                st.rerun()

        st.divider()
        st.caption(f"Thread: `{st.session_state.thread_id}`")
        if st.button("New conversation", use_container_width=True):
            st.session_state.thread_id = f"ui-{uuid.uuid4().hex[:8]}"
            st.rerun()

        st.divider()
        st.caption(f"Model: `{os.getenv('MODEL', DEFAULT_MODEL)}`")
        with st.expander("Audit log"):
            st.json(ctx.sandbox.read_audit(limit=20))
        st.caption("Changes are sandbox-only. data/ is read-only.")

    return st.session_state.pop("pending_prompt", None)


def run() -> None:
    st.set_page_config(
        page_title="OpsPilot",
        page_icon="🚨",
        layout="wide",
    )
    st.title("🚨 OpsPilot")
    st.caption(
        "An approval-gated incident commander. Deterministic evidence first, "
        "LLM explanation second, human approval before every write."
    )

    if "thread_id" not in st.session_state:
        st.session_state.thread_id = f"ui-{uuid.uuid4().hex[:8]}"

    try:
        graph, ctx = get_app()
    except Exception as exc:
        st.error(f"Could not start OpsPilot: {exc}")
        st.code("cp .env.example .env\n# add MODEL and an API key\nuv run streamlit run app.py")
        return

    prompt_from_sidebar = _sidebar(ctx)
    config = thread_config(st.session_state.thread_id)

    try:
        state = graph.get_state(config)
        messages = state.values.get("messages", []) if state.values else []
        _render_messages(messages)
    except Exception as exc:
        st.error(f"Could not read graph state: {exc}")
        return

    interrupts = pending_interrupts(graph, config)
    if interrupts:
        payload = interrupts[0].value or {}
        _render_approval(payload)
        left, right = st.columns(2)
        if left.button("Approve", type="primary", use_container_width=True):
            try:
                resume(graph, config, "approve")
                st.rerun()
            except Exception as exc:
                st.error(f"Approval failed: {exc}")
        if right.button("Reject", use_container_width=True):
            try:
                resume(graph, config, "reject")
                st.rerun()
            except Exception as exc:
                st.error(f"Rejection failed: {exc}")
        return

    prompt = prompt_from_sidebar or st.chat_input("Ask about an incident…")
    if not prompt:
        return

    try:
        graph.invoke({"messages": [HumanMessage(content=prompt)]}, config)
        st.rerun()
    except Exception as exc:
        st.error(f"Agent error: {exc}")
