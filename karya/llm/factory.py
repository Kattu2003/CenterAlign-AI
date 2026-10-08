"""Build the configured LLM client (one place to add a provider)."""

from __future__ import annotations

from karya.config import Settings, get_settings
from karya.llm.base import LLMClient, LLMConfigError


def build_llm(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    provider = settings.llm_provider
    if not settings.llm_model:
        raise LLMConfigError("LLM_MODEL is not set. Copy .env.example to .env and choose a tool-calling model.")
    if provider == "azure_openai":
        missing = [n for n, v in (("AZURE_OPENAI_ENDPOINT", settings.azure_endpoint), ("AZURE_OPENAI_API_KEY", settings.azure_api_key), ("AZURE_OPENAI_API_VERSION", settings.azure_api_version)) if not v]
        if missing:
            raise LLMConfigError(f"LLM_PROVIDER=azure_openai needs {', '.join(missing)} in .env")
        from karya.llm.openai_client import OpenAIClient

        return OpenAIClient(settings, azure=True)
    if provider == "openai":
        if not settings.openai_api_key:
            raise LLMConfigError("LLM_PROVIDER=openai needs OPENAI_API_KEY in .env")
        from karya.llm.openai_client import OpenAIClient

        return OpenAIClient(settings, azure=False)
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMConfigError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY in .env")
        from karya.llm.anthropic_client import AnthropicClient

        return AnthropicClient(settings)
    raise LLMConfigError(f"unknown LLM_PROVIDER {provider!r} (azure_openai | openai | anthropic)")
