"""Provider clients against fake SDK objects: request formatting and response parsing.
(No network: the real API is exercised by tests/test_llm_smoke.py when a key is configured.)"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from karya.config import load_settings
from karya.llm import base
from karya.llm.base import LLMError, Message, ToolSpec, structured

TOOLS = [ToolSpec("browser_open", "Open a URL.", {"type": "object", "properties": {"why": {"type": "string"}, "url": {"type": "string"}}, "required": ["why", "url"]})]
MESSAGES = [Message("system", "You are Karya."), Message("user", "Decide the next step.")]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(base.time, "sleep", lambda s: None)


def settings(monkeypatch, **env):
    for key, value in {"LLM_MODEL": "main-model", "LLM_MODEL_FAST": "fast-model", **env}.items():
        monkeypatch.setenv(key, value)
    return load_settings()


def openai_response(tool_calls=None, content=None):
    calls = [SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args))) for i, (name, args) in enumerate(tool_calls or [])]
    message = SimpleNamespace(content=content, tool_calls=calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30), model="main-model-2026")


def test_openai_request_and_response(monkeypatch):
    from karya.llm.openai_client import OpenAIClient

    client = OpenAIClient(settings(monkeypatch, OPENAI_API_KEY="sk-test"), azure=False)
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return openai_response([("browser_open", {"why": "look", "url": "http://x"})])

    monkeypatch.setattr(client._client.chat.completions, "create", create)
    response = client.chat(MESSAGES, TOOLS, tool_choice="required")
    assert seen["model"] == "main-model" and seen["tool_choice"] == "required" and seen["parallel_tool_calls"] is False
    assert seen["messages"][0] == {"role": "system", "content": "You are Karya."}
    assert seen["tools"][0]["function"]["name"] == "browser_open"
    assert response.tool_calls[0].name == "browser_open" and response.tool_calls[0].arguments["url"] == "http://x"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (120, 30)
    client.chat(MESSAGES, TOOLS, tool_choice={"name": "browser_open"}, model="fast-model")
    assert seen["tool_choice"] == {"type": "function", "function": {"name": "browser_open"}} and seen["model"] == "fast-model"


def test_openai_drops_parallel_flag_when_rejected_and_retries_rate_limits(monkeypatch):
    import openai

    from karya.llm.openai_client import OpenAIClient

    client = OpenAIClient(settings(monkeypatch, OPENAI_API_KEY="sk-test"), azure=False)
    request = httpx.Request("POST", "https://api.example/v1/chat/completions")
    calls = []

    def create(**kwargs):
        calls.append(dict(kwargs))
        if len(calls) == 1:
            raise openai.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)
        if "parallel_tool_calls" in kwargs:
            raise openai.BadRequestError("parallel_tool_calls is not supported with this model", response=httpx.Response(400, request=request), body=None)
        return openai_response([("browser_open", {"why": "w", "url": "u"})])

    monkeypatch.setattr(client._client.chat.completions, "create", create)
    response = client.chat(MESSAGES, TOOLS)
    assert response.tool_calls[0].name == "browser_open"
    assert "parallel_tool_calls" not in calls[-1] and client._parallel_flag_supported is False


def test_openai_invalid_json_arguments_are_flagged(monkeypatch):
    from karya.llm.openai_client import OpenAIClient

    client = OpenAIClient(settings(monkeypatch, OPENAI_API_KEY="sk-test"), azure=False)
    broken = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[SimpleNamespace(id="c", function=SimpleNamespace(name="browser_open", arguments="{not json"))]))], usage=None, model="m")
    monkeypatch.setattr(client._client.chat.completions, "create", lambda **kw: broken)
    assert "__invalid_json__" in client.chat(MESSAGES, TOOLS).tool_calls[0].arguments


def test_azure_client_uses_deployment_as_model(monkeypatch):
    from karya.llm.openai_client import OpenAIClient

    client = OpenAIClient(settings(monkeypatch, AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com", AZURE_OPENAI_API_KEY="k", AZURE_OPENAI_API_VERSION="2024-10-21"), azure=True)
    seen = {}
    monkeypatch.setattr(client._client.chat.completions, "create", lambda **kw: seen.update(kw) or openai_response([("browser_open", {"why": "w", "url": "u"})]))
    client.chat(MESSAGES, TOOLS)
    assert seen["model"] == "main-model"


def test_anthropic_request_and_response(monkeypatch):
    from karya.llm.anthropic_client import AnthropicClient

    client = AnthropicClient(settings(monkeypatch, ANTHROPIC_API_KEY="sk-ant-test"))
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Opening it."), SimpleNamespace(type="tool_use", id="toolu_1", name="browser_open", input={"why": "look", "url": "http://x"})],
            usage=SimpleNamespace(input_tokens=200, output_tokens=40),
            model="main-model",
        )

    monkeypatch.setattr(client._client.messages, "create", create)
    response = client.chat(MESSAGES, TOOLS, tool_choice="required")
    assert seen["system"][0]["text"] == "You are Karya." and seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["messages"] == [{"role": "user", "content": "Decide the next step."}]
    assert seen["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert seen["tools"][0]["input_schema"]["required"] == ["why", "url"]
    assert response.text == "Opening it." and response.tool_calls[0].arguments["url"] == "http://x"
    client.chat(MESSAGES, TOOLS, tool_choice={"name": "browser_open"})
    assert seen["tool_choice"] == {"type": "tool", "name": "browser_open", "disable_parallel_tool_use": True}


def test_factory_requires_configuration(monkeypatch):
    from karya.llm.base import LLMConfigError
    from karya.llm.factory import build_llm

    monkeypatch.setenv("LLM_PROVIDER", "azure_openai")
    monkeypatch.setenv("LLM_MODEL", "deployment")
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    with pytest.raises(LLMConfigError, match="AZURE_OPENAI_ENDPOINT"):
        build_llm(load_settings())
    monkeypatch.setenv("LLM_PROVIDER", "nonsense")
    with pytest.raises(LLMConfigError, match="unknown LLM_PROVIDER"):
        build_llm(load_settings())


class Pair(BaseModel):
    a: int
    b: str


class FlakyClient:
    model = fast_model = "fake"

    def __init__(self):
        self.calls = []

    def chat(self, messages, tools, tool_choice="required", model=None):
        self.calls.append(messages)
        args = {"a": "not a number", "b": "x"} if len(self.calls) == 1 else {"a": 3, "b": "x"}
        return base.LLMResponse(text="", tool_calls=[base.ToolCall("c", tool_choice["name"], args)], usage=base.Usage(10, 5))


def test_structured_retries_once_with_the_validation_error():
    client = FlakyClient()
    value, usage = structured(client, MESSAGES, Pair, "submit_pair", "Return a pair.")
    assert value == Pair(a=3, b="x") and usage.input_tokens == 20
    assert "That was invalid" in client.calls[1][-1].content and "a:" in client.calls[1][-1].content


def test_structured_gives_up_after_one_retry():
    class AlwaysBad(FlakyClient):
        def chat(self, messages, tools, tool_choice="required", model=None):
            self.calls.append(messages)
            return base.LLMResponse(text="no", tool_calls=[], usage=base.Usage())

    with pytest.raises(LLMError):
        structured(AlwaysBad(), MESSAGES, Pair, "submit_pair", "Return a pair.")


def test_tool_schemas_are_inlined_and_portable():
    from karya.tools import registry

    for spec in registry.specs("worker") + registry.specs("verifier"):
        text = json.dumps(spec.parameters)
        assert "$ref" not in text and "$defs" not in text, spec.name
        assert spec.parameters["type"] == "object" and "why" in spec.parameters["required"], spec.name
    names = [s.name for s in registry.specs("worker")]
    assert "submit_verdicts" not in names and "finish" in names
    assert "finish" not in [s.name for s in registry.specs("verifier")]
