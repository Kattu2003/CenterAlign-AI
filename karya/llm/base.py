"""The LLM client protocol and the types every provider client returns.

The runtime only ever talks to `LLMClient.chat(...)`. Each agent step is a
fresh, stateless prompt rebuilt from the ledger (system + one user message), so
clients only need text messages plus tool definitions; no provider-specific
tool-result transcripts (see DECISIONS.md D13).

Structured outputs (Goal Spec, verdicts, extraction, fact learning) use forced
function calling with a single function whose parameters are a pydantic schema
(DECISIONS.md D17): portable across OpenAI, Azure OpenAI and Anthropic.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, TypeVar, Union

from pydantic import BaseModel, ValidationError

ToolChoice = Union[Literal["required", "auto"], dict]  # {"name": "submit_goal_spec"} forces one function


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)


@dataclass
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage = field(default_factory=Usage)
    model: str = ""


class LLMError(RuntimeError):
    """A model call failed after retries, or returned something unusable."""


class LLMConfigError(LLMError):
    """The configured provider is missing settings (no API key, no model...)."""


class LLMClient(Protocol):
    model: str
    fast_model: str

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_choice: ToolChoice = "required",
        model: str | None = None,
    ) -> LLMResponse: ...


def new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def parse_arguments(raw: str | dict | None) -> dict[str, Any]:
    """Tool-call arguments arrive as a JSON string (OpenAI) or a dict (Anthropic)."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"__invalid_json__": raw[:500]}
    return value if isinstance(value, dict) else {"__invalid_json__": raw[:500]}


def with_retries(fn: Callable[[], Any], is_retryable: Callable[[Exception], bool], attempts: int = 3) -> Any:
    """Exponential backoff (1 s, 2 s) on rate limits / 5xx / connection errors.
    Anything else, or the last failure, is raised: errors are never swallowed."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - filtered by is_retryable
            if attempt == attempts - 1 or not is_retryable(exc):
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def schema_for(model_cls: type[BaseModel]) -> dict[str, Any]:
    """A pydantic model's JSON schema with $refs inlined and titles dropped
    (smaller, and accepted by every provider)."""
    schema = model_cls.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any, in_properties: bool = False) -> Any:
        if isinstance(node, list):
            return [resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        if in_properties:  # keys are property names, never schema keywords
            return {key: resolve(value) for key, value in node.items()}
        if "$ref" in node:
            target = resolve(defs[node["$ref"].split("/")[-1]])
            extra = {k: resolve(v) for k, v in node.items() if k not in ("$ref", "title")}
            return {**target, **extra}
        out = {}
        for key, value in node.items():
            if key == "title":
                continue
            out[key] = resolve(value, in_properties=(key == "properties"))
        return out

    return resolve(schema)


T = TypeVar("T", bound=BaseModel)


def structured(
    client: LLMClient,
    messages: list[Message],
    output_model: type[T],
    name: str,
    description: str,
    model: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> tuple[T, Usage]:
    """Forced function call -> validated pydantic object. On a validation failure,
    retry once with the error message appended (plan §11.1)."""
    spec = ToolSpec(name=name, description=description, parameters=parameters or schema_for(output_model))
    usage = Usage()
    history = list(messages)
    error = ""
    for _ in range(2):
        response = client.chat(history, [spec], tool_choice={"name": name}, model=model)
        usage = usage + response.usage
        call = next((c for c in response.tool_calls if c.name == name), None)
        if call is None:
            error = f"you did not call {name}"
            attempt_text = response.text or "(no function call)"
        else:
            try:
                return output_model.model_validate(call.arguments), usage
            except ValidationError as exc:
                error = _short_validation_error(exc)
                attempt_text = json.dumps(call.arguments)[:4000]
        history = history + [
            Message("assistant", attempt_text),
            Message("user", f"That was invalid: {error}. Call {name} again with corrected arguments."),
        ]
    raise LLMError(f"{name}: no valid structured output after a retry ({error})")


def _short_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:8]:
        location = ".".join(str(p) for p in err["loc"]) or "(root)"
        parts.append(f"{location}: {err['msg']}")
    return "; ".join(parts)
