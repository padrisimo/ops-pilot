import pytest

from ops_pilot.context import AppContext
from ops_pilot.data import DataStore
from ops_pilot.sandbox import SandboxStore


@pytest.fixture(scope="session")
def store() -> DataStore:
    return DataStore.load()


@pytest.fixture()
def sandbox(tmp_path) -> SandboxStore:
    return SandboxStore(tmp_path / "sandbox")


@pytest.fixture()
def ctx(store: DataStore, sandbox: SandboxStore) -> AppContext:
    return AppContext(data=store, sandbox=sandbox)
