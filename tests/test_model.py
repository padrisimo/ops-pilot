import pytest

from ops_pilot import model as model_module
from ops_pilot.model import build_model


def test_openrouter_free_alias(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    chat = build_model("openrouter:free")

    assert chat.model_name == "openrouter/free"
    assert chat.openai_api_base == "https://openrouter.ai/api/v1"


def test_openrouter_empty_model_uses_free_router(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    chat = build_model("openrouter:")

    assert chat.model_name == "openrouter/free"


def test_pinned_free_model_is_preserved(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    chat = build_model("openrouter:vendor/model:free")

    assert chat.model_name == "vendor/model:free"


def test_missing_openrouter_key_is_explicit(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(model_module, "load_dotenv", lambda: None)

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is missing"):
        build_model("openrouter:free")
