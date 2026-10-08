"""Scripted scenarios as data: turn a YAML list of tool calls into a ScriptedLLM.

Used for the red-team scenario (and any scenario that must not depend on a
model). Templates are resolved at run time:
  "{connector:erp}"                  -> the erp connector's base_url
  ref: {find: textbox, name: ifsc}   -> the ref of that element in the latest snapshot
  ref: {find: submit}                -> the last button in the latest snapshot (form submit)
"""

from __future__ import annotations

import re
from typing import Any, Callable

from karya.company import Company
from karya.llm.base import LLMError, Message
from karya.llm.scripted import ScriptedLLM, find_ref, last_user_text

_CONNECTOR = re.compile(r"\{connector:(\w+)\}")


def _resolve_text(value: Any, company: Company) -> Any:
    if isinstance(value, str):
        def base_url(match: re.Match[str]) -> str:
            connector = company.connectors.get(match.group(1))
            if connector is None or not connector.base_url:
                raise LLMError(f"scripted step references unknown connector {match.group(1)!r}")
            return connector.base_url.rstrip("/")

        return _CONNECTOR.sub(base_url, value)
    if isinstance(value, dict):
        return {k: _resolve_text(v, company) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_text(v, company) for v in value]
    return value


def _find(spec: dict[str, Any], messages: list[Message]) -> str:
    text = last_user_text(messages)
    if spec.get("find") == "submit":
        latest = text[text.rfind("URL: "):]
        buttons = re.findall(r"\[(e\d+)\] button", latest)
        if not buttons:
            raise LLMError("no button in the latest snapshot")
        return buttons[-1]
    return find_ref(text, spec["find"], label=spec.get("label"), name=spec.get("name"))


def build_step(spec: dict[str, Any], company: Company) -> Callable[[list[Message]], dict[str, Any]] | dict[str, Any]:
    arguments = _resolve_text(dict(spec.get("args", {})), company)
    arguments["why"] = spec.get("why", f"scripted step: {spec['tool']}")
    if isinstance(arguments.get("ref"), dict):
        ref_spec = arguments.pop("ref")

        def step(messages: list[Message]) -> dict[str, Any]:
            return {"name": spec["tool"], "arguments": {**arguments, "ref": _find(ref_spec, messages)}}

        return step
    return {"name": spec["tool"], "arguments": arguments}


def build_llm(scripted: dict[str, Any], company: Company) -> ScriptedLLM:
    """scripted: {goal: <IntakeOutput dict>, steps: [...], forced: {name: args}}"""
    forced = dict(scripted.get("forced", {}))
    forced["submit_goal_spec"] = scripted["goal"]
    forced.setdefault("submit_injection_check", {"contains_instructions": False, "excerpt": "", "reason": "scripted"})
    return ScriptedLLM([build_step(s, company) for s in scripted["steps"]], forced=forced)
