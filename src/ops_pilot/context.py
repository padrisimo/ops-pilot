"""Application dependencies shared by the graph, CLI and Streamlit UI."""

from __future__ import annotations

from dataclasses import dataclass

from .data import DataStore, get_data
from .sandbox import SandboxStore


@dataclass
class AppContext:
    data: DataStore
    sandbox: SandboxStore


def default_context() -> AppContext:
    return AppContext(data=get_data(), sandbox=SandboxStore())
