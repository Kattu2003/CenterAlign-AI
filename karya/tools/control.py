"""Control tools: update_plan, finish (worker) and submit_verdicts (verifier).

`finish` and `submit_verdicts` are intercepted by the loop / verifier; their
run() only acknowledges, so the ledger shows them like any other step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from karya.runtime.models import FinishArgs, Observation, VerificationResult
from karya.tools.base import Tool, ToolArgs

if TYPE_CHECKING:
    from karya.tools.base import ToolSession


class PlanStepIn(BaseModel):
    id: str | None = Field(default=None, description="Keep existing ids (p1, p2...) when updating")
    description: str
    status: Literal["pending", "in_progress", "done", "skipped", "failed"] = "pending"
    note: str | None = None


class UpdatePlanArgs(ToolArgs):
    steps: list[PlanStepIn] = Field(min_length=1, description="The whole plan, replacing the current one")


class UpdatePlan(Tool):
    name = "update_plan"
    description = "Replace the plan (mark steps done/in_progress, add or remove steps) when reality differs from it."
    Args = UpdatePlanArgs

    def run(self, args: UpdatePlanArgs, session: "ToolSession") -> Observation:
        done = sum(1 for s in args.steps if s.status == "done")
        return Observation(ok=True, summary=f"plan updated: {len(args.steps)} steps, {done} done")


class FinishToolArgs(ToolArgs, FinishArgs):
    pass


class Finish(Tool):
    name = "finish"
    description = "End the task: completed, partial, escalated or cannot_complete. Give one claim per success criterion with evidence refs. An independent read-only auditor will check your claims."
    Args = FinishToolArgs

    def run(self, args: FinishToolArgs, session: "ToolSession") -> Observation:
        return Observation(ok=True, summary=f"finish requested: {args.status}")


class SubmitVerdictsArgs(ToolArgs, VerificationResult):
    pass


class SubmitVerdicts(Tool):
    name = "submit_verdicts"
    description = "Submit one verdict (pass / fail / unverifiable) per success criterion, with the evidence you observed."
    Args = SubmitVerdictsArgs
    roles = frozenset({"verifier"})

    def run(self, args: SubmitVerdictsArgs, session: "ToolSession") -> Observation:
        return Observation(ok=True, summary=f"verdicts submitted: overall {args.overall}")
