"""Task report. Phase 3: a minimal report.json/report.md; the full report arrives in phase 4."""

from __future__ import annotations

import json
from typing import Any

from karya.runtime import ledger
from karya.runtime.ledger import TaskState
from karya.runtime.models import FinishArgs, VerificationResult


def write(state: TaskState, finish: FinishArgs, result: VerificationResult, status: str, ctx: Any) -> dict[str, Any]:
    run_dir = ctx.settings.runs_dir / state.task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    data = {"task_id": state.task_id, "status": status, "request": state.request, "summary": finish.summary, "finish": finish.model_dump(), "verification": result.model_dump(), "steps": state.step_count}
    (run_dir / "report.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    (run_dir / "report.md").write_text(f"# {state.task_id} · {status}\n\n**Request:** {state.request}\n\n**Outcome:** {finish.summary}\n", encoding="utf-8")
    ledger.append(ctx.conn, state.task_id, "report", {"status": status, "summary": finish.summary})
    return data
