"""ScriptedLLM: replays predetermined tool calls (tests and the red-team scenario).

It proves the parts of the system that must not depend on the model behaving:
the gateway, reconciliation, approvals, resume. Script items are dicts
(`{"name": ..., "arguments": {...}}`) or callables that receive the messages
and return such a dict (useful to pick an element ref from the latest snapshot).
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Callable, Iterable

from karya.llm.base import LLMError, LLMResponse, Message, ToolCall, ToolChoice, ToolSpec, Usage, new_call_id

Step = dict[str, Any] | Callable[[list[Message]], dict[str, Any]]
ForcedResponse = dict[str, Any] | list[dict[str, Any]] | Callable[[list[Message]], dict[str, Any]]


class ScriptedLLM:
    model = "scripted"
    fast_model = "scripted"

    def __init__(self, steps: Iterable[Step] = (), forced: dict[str, ForcedResponse] | None = None) -> None:
        self.steps: deque[Step] = deque(steps)
        self.forced: dict[str, ForcedResponse] = dict(forced or {})
        self.requests: list[dict[str, Any]] = []  # what the runtime asked for (assertions in tests)

    def chat(self, messages: list[Message], tools: list[ToolSpec], tool_choice: ToolChoice = "required", model: str | None = None) -> LLMResponse:
        self.requests.append({"tool_choice": tool_choice, "tools": [t.name for t in tools], "messages": messages})
        if isinstance(tool_choice, dict):
            name = tool_choice["name"]
            if name not in self.forced:
                if self.steps and _peek_name(self.steps[0]) == name:
                    return self._next_step(messages)
                raise LLMError(f"ScriptedLLM has no scripted response for forced function {name!r}")
            handler = self.forced[name]
            if isinstance(handler, list):
                arguments = handler.pop(0) if len(handler) > 1 else handler[0]
            elif callable(handler):
                arguments = handler(messages)
            else:
                arguments = handler
            return LLMResponse(text="", tool_calls=[ToolCall(new_call_id(), name, dict(arguments))], usage=Usage(), model=self.model)
        return self._next_step(messages)

    def _next_step(self, messages: list[Message]) -> LLMResponse:
        if not self.steps:
            exhausted = {"why": "The test script has no more steps.", "status": "cannot_complete", "summary": "Script exhausted."}
            return LLMResponse(text="", tool_calls=[ToolCall(new_call_id(), "finish", exhausted)], usage=Usage(), model=self.model)
        step = self.steps.popleft()
        if callable(step):
            step = step(messages)
        return LLMResponse(text=step.get("text", ""), tool_calls=[ToolCall(new_call_id(), step["name"], dict(step.get("arguments", {})))], usage=Usage(), model=self.model)


def _peek_name(step: Step) -> str | None:
    return step.get("name") if isinstance(step, dict) else None


_REF_LINE = re.compile(r"\[(e\d+)\]\s+(\w+)\s+\"([^\"]*)\"(.*)")


def find_ref(text: str, kind: str, label: str | None = None, name: str | None = None) -> str:
    """Find an element ref in a snapshot, e.g. find_ref(snap, "button", "Post") or
    find_ref(snap, "number", name="amount"). Uses the LAST snapshot in the text."""
    last = text.rfind("URL: ")
    lines = text[last:].splitlines() if last >= 0 else text.splitlines()
    for line in lines:
        match = _REF_LINE.search(line)
        if not match:
            continue
        ref, element_kind, element_label, rest = match.groups()
        if element_kind != kind:
            continue
        if name is not None and f"name={name}" not in rest:
            continue
        if label is not None and label.lower() not in element_label.lower():
            continue
        return ref
    raise LLMError(f"no {kind} matching label={label!r} name={name!r} in the latest snapshot")


def last_user_text(messages: list[Message]) -> str:
    return next((m.content for m in reversed(messages) if m.role == "user"), "")


class ScriptedVerifier:
    """A scripted auditor for tests: submits verdicts straight away. Every criterion
    passes unless the code-made system evidence shows a mismatch; then the criteria
    listed in `fail_on_mismatch` fail. Optionally runs `checks` (tool calls) first."""

    model = "scripted-verifier"
    fast_model = "scripted-verifier"
    MISMATCH_MARKERS = ("MISMATCH", "NO matching record", "DUPLICATES")

    def __init__(self, fail_on_mismatch: Iterable[str] = (), checks: Iterable[dict[str, Any]] = ()) -> None:
        self.fail_on_mismatch = set(fail_on_mismatch)
        self.checks: deque[dict[str, Any]] = deque(checks)
        self.requests: list[list[Message]] = []

    def chat(self, messages: list[Message], tools: list[ToolSpec], tool_choice: ToolChoice = "required", model: str | None = None) -> LLMResponse:
        self.requests.append(messages)
        if self.checks and tool_choice == "required":
            check = self.checks.popleft()
            return LLMResponse(text="", tool_calls=[ToolCall(new_call_id(), check["name"], dict(check["arguments"]))], usage=Usage(), model=self.model)
        brief = last_user_text(messages)
        criteria = re.findall(r"^\s{2}(c\d+):", brief.split("WORKER'S FINISH")[0], flags=re.M)
        evidence = brief.split("SYSTEM EVIDENCE", 1)[-1].split("YOUR CHECKS SO FAR", 1)[0]
        mismatch = any(marker in evidence for marker in self.MISMATCH_MARKERS)
        verdicts = []
        for cid in criteria:
            failed = mismatch and cid in self.fail_on_mismatch
            verdicts.append({"criterion_id": cid, "verdict": "fail" if failed else "pass", "evidence": ["system evidence"], "note": "system evidence shows a mismatch" if failed else "confirmed by system evidence"})
        arguments = {"why": "All criteria checked.", "verdicts": verdicts, "overall": "fail" if any(v["verdict"] == "fail" for v in verdicts) else "pass", "notes": ""}
        return LLMResponse(text="", tool_calls=[ToolCall(new_call_id(), "submit_verdicts", arguments)], usage=Usage(), model=self.model)
