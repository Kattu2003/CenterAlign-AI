"""Verification of a finished task.

Phase 3 placeholder: accepts the worker's finish so the loop can run end to
end. The independent read-only verifier replaces this in phase 4.
"""

from __future__ import annotations

from typing import Any

from karya.runtime.ledger import TaskState
from karya.runtime.models import FinishArgs, Verdict, VerificationResult


def verify(state: TaskState, finish: FinishArgs, ctx: Any) -> tuple[VerificationResult, list[str]]:
    criteria = state.goal.success_criteria if state.goal else []
    verdicts = [Verdict(criterion_id=c.id, verdict="pass", note="not independently verified (phase 3 placeholder)") for c in criteria]
    return VerificationResult(verdicts=verdicts, overall="pass", notes="placeholder verifier"), []
