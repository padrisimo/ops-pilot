"""Model construction with OpenAI and OpenRouter support."""

from __future__ import annotations

import os

from dotenv import load_dotenv

DEFAULT_MODEL = "openai:gpt-4o-mini"


def build_model(model_spec: str | None = None):
    load_dotenv()
    spec = model_spec or os.getenv("MODEL", DEFAULT_MODEL)

    if spec.startswith("openrouter:"):
        from langchain_openai import ChatOpenAI

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "MODEL is set to OpenRouter but OPENROUTER_API_KEY is missing. "
                "Copy .env.example to .env and add the key."
            )
        return ChatOpenAI(
            model=spec.split(":", 1)[1],
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            temperature=0,
        )

    if spec.startswith("openai:") and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Copy .env.example to .env and add the key, "
            "or set MODEL to an OpenRouter model."
        )

    from langchain.chat_models import init_chat_model

    return init_chat_model(spec, temperature=0)
