"""Model construction with OpenRouter free models and OpenAI support."""

from __future__ import annotations

import os

from dotenv import load_dotenv

# OpenRouter's auto-router for free models. It supports tool calling, which the
# incident-commander graph requires.
DEFAULT_MODEL = "openrouter:openrouter/free"
OPENROUTER_FREE_ALIAS = "openrouter/free"


def _openrouter_model_name(spec: str) -> str:
    name = spec.split(":", 1)[1].strip()
    if not name or name.lower() == "free":
        return OPENROUTER_FREE_ALIAS
    return name


def build_model(model_spec: str | None = None):
    """Build the chat model.

    ``MODEL`` accepts ``openrouter:<model>`` or ``openai:<model>``. OpenRouter is
    the default so a free key is enough:

    - ``openrouter:free`` or ``openrouter:openrouter/free`` uses OpenRouter's free
      auto-router.
    - ``openrouter:<vendor>/<model>:free`` pins a specific free model.

    Free models are rate-limited by OpenRouter; tool calling is required and is
    supported by the free router.
    """
    load_dotenv()
    spec = (model_spec or os.getenv("MODEL", DEFAULT_MODEL)).strip()

    if spec.startswith("openrouter:"):
        from langchain_openai import ChatOpenAI

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "MODEL is set to OpenRouter but OPENROUTER_API_KEY is missing. "
                "Create a free key at https://openrouter.ai/keys and add it to .env. "
                "Use MODEL=openrouter:free or a pinned model such as "
                "MODEL=openrouter:<vendor>/<model>:free."
            )

        headers = {"X-Title": os.getenv("OPENROUTER_APP_NAME", "OpsPilot")}
        site_url = os.getenv("OPENROUTER_SITE_URL")
        if site_url:
            headers["HTTP-Referer"] = site_url

        return ChatOpenAI(
            model=_openrouter_model_name(spec),
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers=headers,
            temperature=0,
        )

    if spec.startswith("openai:") and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Copy .env.example to .env and add the key, "
            "or set MODEL=openrouter:free to use a free OpenRouter model."
        )

    from langchain.chat_models import init_chat_model

    return init_chat_model(spec, temperature=0)
