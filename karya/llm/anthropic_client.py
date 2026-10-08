"""Anthropic messages API + tools."""

from __future__ import annotations

from typing import Any

from karya.config import Settings
from karya.llm.base import LLMResponse, Message, Pacer, ToolCall, ToolChoice, ToolSpec, Usage, parse_arguments, with_retries


class AnthropicClient:
    def __init__(self, settings: Settings, max_tokens: int = 4096) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0)
        self.model = settings.llm_model
        self.fast_model = settings.fast_model
        self.max_tokens = max_tokens
        self._pacer = Pacer(settings.llm_max_rpm)

    def _retryable(self, exc: Exception) -> bool:
        a = self._anthropic
        if isinstance(exc, (a.RateLimitError, a.APIConnectionError, a.APITimeoutError, a.InternalServerError)):
            return True
        return isinstance(exc, a.APIStatusError) and exc.status_code >= 500

    def chat(self, messages: list[Message], tools: list[ToolSpec], tool_choice: ToolChoice = "required", model: str | None = None) -> LLMResponse:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        kwargs: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages if m.role != "system"],
        }
        if system:
            # The system prompt (instructions + company context) is stable across steps: cache it.
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
            if isinstance(tool_choice, dict):
                choice: dict[str, Any] = {"type": "tool", "name": tool_choice["name"]}
            elif tool_choice == "required":
                choice = {"type": "any"}
            else:
                choice = {"type": "auto"}
            choice["disable_parallel_tool_use"] = True  # one tool call per step (DECISIONS.md D13)
            kwargs["tool_choice"] = choice

        def call() -> Any:
            self._pacer.wait()
            return self._client.messages.create(**kwargs)

        response = with_retries(call, self._retryable)
        text = "".join(block.text for block in response.content if block.type == "text")
        calls = [ToolCall(id=b.id, name=b.name, arguments=parse_arguments(b.input)) for b in response.content if b.type == "tool_use"]
        usage = Usage(response.usage.input_tokens, response.usage.output_tokens)
        return LLMResponse(text=text, tool_calls=calls, usage=usage, model=response.model)
