from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from ops_pilot.graph import (
    DEFAULT_CHECKPOINT_DB,
    build_checkpointer,
    checkpoint_db_path,
)


class CounterState(TypedDict):
    value: int


def _counter_graph(checkpointer):
    builder = StateGraph(CounterState)
    builder.add_node("bump", lambda state: {"value": state.get("value", 0) + 1})
    builder.add_edge(START, "bump")
    builder.add_edge("bump", END)
    return builder.compile(checkpointer=checkpointer)


def test_default_checkpoint_is_sqlite_in_sandbox() -> None:
    assert checkpoint_db_path() == DEFAULT_CHECKPOINT_DB
    assert DEFAULT_CHECKPOINT_DB.name == "checkpoints.sqlite"
    assert DEFAULT_CHECKPOINT_DB.parent.name == "sandbox"


def test_env_overrides_checkpoint_path(tmp_path, monkeypatch) -> None:
    db = tmp_path / "custom" / "threads.sqlite"
    monkeypatch.setenv("OPS_PILOT_CHECKPOINT_DB", str(db))

    assert checkpoint_db_path() == db


def test_memory_sentinel_disables_persistence(monkeypatch) -> None:
    monkeypatch.setenv("OPS_PILOT_CHECKPOINT_DB", "memory")

    assert isinstance(build_checkpointer(), MemorySaver)


def test_sqlite_state_survives_a_new_checkpointer(tmp_path) -> None:
    db = tmp_path / "checkpoints.sqlite"
    config = {"configurable": {"thread_id": "persistence"}}

    first = _counter_graph(build_checkpointer(db))
    first.invoke({"value": 10}, config)
    assert db.exists()

    # A new saver and graph instance simulate a process restart.
    second = _counter_graph(build_checkpointer(db))
    snapshot = second.get_state(config)

    assert snapshot.values["value"] == 11
