"""OpenAI and Azure OpenAI via chat completions + tools."""

from __future__ import annotations

from typing import Any

from karya.config import Settings
from karya.llm.base import LLMResponse, Message, Pacer, ToolCall, ToolChoice, ToolSpec, Usage, parse_arguments, with_retries

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIClient:
    def __init__(self, settings: Settings, azure: bool) -> None:
        import openai

        self._openai = openai
        if azure:
            # On Azure, `model` is the deployment name.
            self._client = openai.AzureOpenAI(
                azure_endpoint=settings.azure_endpoint,
                api_key=settings.azure_api_key,
                api_version=settings.azure_api_version,
                max_retries=0,  # retries are explicit (with_retries) so they are visible and bounded
            )
        else:
            # Passed explicitly so a blank or commented OPENAI_BASE_URL line in .env can't break it.
            self._client = openai.OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url or OPENAI_DEFAULT_BASE_URL, max_retries=0)
        self.model = settings.llm_model
        self.fast_model = settings.fast_model
        self._parallel_flag_supported = True
        self._pacer = Pacer(settings.llm_max_rpm)

    def _retryable(self, exc: Exception) -> bool:
        o = self._openai
        if isinstance(exc, (o.RateLimitError, o.APIConnectionError, o.APITimeoutError, o.InternalServerError)):
            return True
        return isinstance(exc, o.APIStatusError) and exc.status_code >= 500

    def chat(self, messages: list[Message], tools: list[ToolSpec], tool_choice: ToolChoice = "required", model: str | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
            if isinstance(tool_choice, dict):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": tool_choice["name"]}}
            else:
                kwargs["tool_choice"] = tool_choice
            if self._parallel_flag_supported:
                kwargs["parallel_tool_calls"] = False  # one tool call per step (DECISIONS.md D13)

        def call() -> Any:
            self._pacer.wait()
            try:
                return self._client.chat.completions.create(**kwargs)
            except self._openai.BadRequestError as exc:
                # Some models/deployments reject parallel_tool_calls; drop it once and remember.
                if "parallel_tool_calls" in str(exc) and kwargs.pop("parallel_tool_calls", None) is not None:
                    self._parallel_flag_supported = False
                    self._pacer.wait()
                    return self._client.chat.completions.create(**kwargs)
                raise

        response = with_retries(call, self._retryable)
        choice = response.choices[0].message
        calls = [ToolCall(id=c.id, name=c.function.name, arguments=parse_arguments(c.function.arguments)) for c in (choice.tool_calls or [])]
        usage = Usage(response.usage.prompt_tokens, response.usage.completion_tokens) if response.usage else Usage()
        return LLMResponse(text=choice.content or "", tool_calls=calls, usage=usage, model=response.model or kwargs["model"])
