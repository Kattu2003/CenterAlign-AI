"""Smoke test against the configured model (skipped when no LLM is configured)."""

from __future__ import annotations

import pytest

from karya.config import get_settings
from karya.llm.base import LLMConfigError, Message, ToolSpec
from karya.llm.factory import build_llm


def test_configured_model_calls_one_tool():
    try:
        llm = build_llm(get_settings())
    except LLMConfigError as exc:
        pytest.skip(f"no LLM configured: {exc}")
    tool = ToolSpec("echo", "Echo a word back.", {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]})
    response = llm.chat([Message("system", "Call the echo tool with the word 'karya'."), Message("user", "Go.")], [tool], tool_choice="required")
    assert response.tool_calls and response.tool_calls[0].name == "echo"
    assert response.usage.input_tokens > 0
