"""Tool base class and the per-task tool session.

A tool is a class with a `name`, a `description`, pydantic `Args` (always with
a required `why`) and `run(args, session) -> Observation`. JSON schemas for the
model come from `Args`. Adding a tool = one class + one line in registry.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from karya.company import Company
from karya.config import Settings, get_settings
from karya.llm.base import LLMClient, ToolCall, ToolSpec, Usage, schema_for
from karya.runtime.models import Observation

if TYPE_CHECKING:
    from karya.gateway.gateway import Gateway
    from karya.memory.retrieval import Retriever
    from karya.tools.browser import BrowserSession


class ToolArgs(BaseModel):
    """Every tool call carries `why`: logged, shown in traces and reports (DECISIONS.md D13)."""

    model_config = ConfigDict(extra="forbid")
    why: str = Field(description="One short sentence (max 200 characters): why this action, now.")

    @field_validator("why")
    @classmethod
    def _tidy_why(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("why must not be empty")
        return value[:200]


class Tool:
    name: ClassVar[str]
    description: ClassVar[str]
    Args: ClassVar[type[ToolArgs]] = ToolArgs
    roles: ClassVar[frozenset[str]] = frozenset({"worker"})

    def run(self, args: Any, session: "ToolSession") -> Observation:
        raise NotImplementedError

    @classmethod
    def spec(cls) -> ToolSpec:
        return ToolSpec(name=cls.name, description=cls.description, parameters=schema_for(cls.Args))


def short_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:6]:
        location = ".".join(str(p) for p in err["loc"]) or "(arguments)"
        parts.append(f"{location}: {err['msg']}")
    return "; ".join(parts)


class ToolSession:
    """Everything a tool may touch during one task run: the gateway, a browser,
    the run directory, the company config and the database."""

    def __init__(
        self,
        task_id: str,
        company: Company,
        conn: sqlite3.Connection,
        llm: LLMClient | None,
        gateway: "Gateway",
        *,
        settings: Settings | None = None,
        role: str = "worker",
        retriever: "Retriever | None" = None,
        run_dir: Path | None = None,
        requester: str | None = None,
        screens_dir: str = "screens",
    ) -> None:
        from karya.tools.browser import BrowserSession

        self.task_id = task_id
        self.company = company
        self.conn = conn
        self.llm = llm
        self.gateway = gateway
        self.secrets = gateway.secrets
        self.settings = settings or get_settings()
        self.role = role
        self.retriever = retriever
        self.requester = requester or company.profile.default_requester
        self.run_dir = run_dir or (self.settings.runs_dir / task_id)
        self.screens_dir = screens_dir            # relative to run_dir ("verify/screens" for the verifier)
        self.step = 0
        self.tool_usage = Usage()                 # tokens spent inside tools (extraction, checks)
        self.flagged: set[str] = set()            # injection excerpts already reported
        self.logged_out: set[str] = set()         # connectors where a login page appeared unexpectedly
        self.browser: BrowserSession = BrowserSession(self)

    def dir(self, name: str) -> Path:
        path = self.run_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def execute(self, call: ToolCall, step: int) -> Observation:
        """Validate arguments, run the tool, redact secrets. Never raises."""
        from karya.tools.registry import get_tool

        tool_cls = get_tool(call.name)
        if tool_cls is None or self.role not in tool_cls.roles:
            return Observation(ok=False, summary=f"unknown tool {call.name!r}", error=f"There is no tool named {call.name!r} for the {self.role}.")
        if "__invalid_json__" in call.arguments:
            return Observation(ok=False, summary=f"{call.name}: arguments were not valid JSON", error="Arguments must be a JSON object.")
        try:
            args = tool_cls.Args.model_validate(call.arguments)
        except ValidationError as exc:
            return Observation(ok=False, summary=f"{call.name}: invalid arguments", error=short_validation_error(exc))
        self.step = step
        try:
            observation = tool_cls().run(args, self)
        except Exception as exc:  # a tool bug must not kill the task; the model sees the error
            observation = Observation(ok=False, summary=f"{call.name} failed: {type(exc).__name__}", error=str(exc)[:800])
        return self.secrets.redact_observation(observation)

    def close(self) -> None:
        self.browser.close()
