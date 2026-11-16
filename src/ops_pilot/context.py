"""Application dependencies shared by the graph, CLI and Streamlit UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .data import DataStore, get_data
from .sandbox import SandboxStore


@dataclass
class AppContext:
    data: DataStore
    sandbox: SandboxStore


def default_context() -> AppContext:
    return AppContext(data=get_data(), sandbox=SandboxStore())


class LazyAppContext:
    """Build the context on first use instead of at graph construction.

    LangGraph's API server builds graphs inside an async event loop and flags
    synchronous I/O. Loading JSON and creating the sandbox directory is blocking,
    so it is deferred until a tool actually runs (sync tools execute in a worker
    thread). CLI and UI still pass a fully built ``AppContext``.
    """

    def __init__(self, factory: Callable[[], AppContext] = default_context) -> None:
        self._factory = factory
        self._context: AppContext | None = None

    def _get(self) -> AppContext:
        if self._context is None:
            self._context = self._factory()
        return self._context

    @property
    def data(self) -> DataStore:
        return self._get().data

    @property
    def sandbox(self) -> SandboxStore:
        return self._get().sandbox
