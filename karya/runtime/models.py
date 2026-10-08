"""Typed models shared by the runtime (plan §11.2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class SuccessCriterion(BaseModel):
    id: str = Field(description='Short id, e.g. "c1"')
    description: str = Field(description="A checkable statement about the end state")
    verification: str = Field(description="Where and how it will be checked: name the system and the lookup")


class GoalSpec(BaseModel):
    """What "done" means, written before the first action (DECISIONS.md D8)."""

    objective: str
    deliverable: str
    entities: list[str] = []
    success_criteria: list[SuccessCriterion] = Field(min_length=1)
    constraints: list[str] = Field(default=[], description="Cite policy/handbook ids, e.g. AP-001, ap-01")
    assumptions: list[str] = []
    blocking_questions: list[str] = Field(default=[], description="Only questions that investigating the systems cannot answer")
    risk_notes: list[str] = []
    batch: bool = Field(default=False, description="True when the request covers many items")


class PlanStep(BaseModel):
    id: str
    description: str
    status: Literal["pending", "in_progress", "done", "skipped", "failed"] = "pending"
    note: str | None = None


class PolicyEvent(BaseModel):
    type: Literal[
        "denied", "approval_required", "approved_use", "duplicate_prevented",
        "write_outcome", "blocked_host", "read_only_block",
    ]
    rule_ids: list[str] = []
    approval_id: str | None = None
    intent_id: int | None = None
    status: str | None = None     # for write_outcome: committed | failed | unknown
    message: str


class Observation(BaseModel):
    ok: bool
    summary: str                  # one line, used in compacted history
    content: str = ""             # full text (snapshot, file text, JSON) — may be truncated
    untrusted: bool = False       # wrapped in <untrusted> when building context
    source: str | None = None     # e.g. "mail:/messages/2", "file:downloads/AI-2026-0917.pdf"
    artifacts: list[str] = []     # relative paths under runs/<task>/
    policy_events: list[PolicyEvent] = []
    flags: list[dict[str, Any]] = []   # e.g. {"type": "prompt_injection", "excerpt": "..."}
    error: str | None = None
    meta: dict[str, Any] = {}     # counters for reports: retries, relogin, downloads...


class Claim(BaseModel):
    criterion_id: str
    statement: str
    evidence: list[str] = Field(default=[], description='e.g. "screens/031.png", "erp:ERP-INV-000104", "downloads/AI-2026-0917.pdf"')


class ItemOutcome(BaseModel):
    item: str
    outcome: Literal["done", "skipped", "escalated", "failed"]
    ref: str | None = None
    note: str = ""


class Flag(BaseModel):
    type: Literal["prompt_injection", "possible_fraud", "data_mismatch", "duplicate", "other"]
    detail: str
    evidence: list[str] = []


class FinishArgs(BaseModel):
    status: Literal["completed", "partial", "escalated", "cannot_complete"]
    summary: str
    claims: list[Claim] = []
    items: list[ItemOutcome] = []
    flags: list[Flag] = []
    needs_attention: list[str] = []


class Verdict(BaseModel):
    criterion_id: str
    verdict: Literal["pass", "fail", "unverifiable"]
    evidence: list[str] = []
    note: str = ""


class VerificationResult(BaseModel):
    verdicts: list[Verdict]
    overall: Literal["pass", "fail"]
    notes: str = ""


class PlanDraft(BaseModel):
    """Initial plan step as written by intake (ids/status are assigned in code)."""

    description: str

    @field_validator("description")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class IntakeOutput(BaseModel):
    goal_spec: GoalSpec
    plan: list[PlanDraft] = Field(min_length=1, max_length=12, description="3-10 high-level steps, not individual clicks")
