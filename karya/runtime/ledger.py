"""Append-only ledger of task steps; the task state is rebuilt from it.

Every intake, plan, model decision, observation, human answer, approval
decision and verification is appended as a row in `steps` (and mirrored to
runs/<task>/trace.jsonl). `load_state` replays the rows, so a resumed task —
after a crash, or days of waiting for an approval — continues from exactly the
same state as a live one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable

from karya.config import get_settings
from karya.db import dumps, loads, now_iso, transaction
from karya.runtime.models import GoalSpec, Observation, PlanStep

Listener = Callable[[str, int, str, dict[str, Any]], None]
_listeners: list[Listener] = []


def subscribe(listener: Listener) -> None:
    """Live streaming (CLI): called after every append."""
    _listeners.append(listener)


def unsubscribe(listener: Listener) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


def append(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict[str, Any]) -> int:
    created = now_iso()
    with transaction(conn):
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM steps WHERE task_id = ?", (task_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO steps(task_id, seq, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, seq, kind, dumps(payload), created),
        )
    trace = get_settings().runs_dir / task_id / "trace.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    with trace.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": seq, "kind": kind, "at": created, "payload": payload}, default=str, ensure_ascii=False) + "\n")
    for listener in list(_listeners):
        listener(task_id, seq, kind, payload)
    return seq


@dataclass
class StepRow:
    seq: int
    kind: str
    payload: dict[str, Any]
    created_at: str


def rows(conn: sqlite3.Connection, task_id: str, after_seq: int = 0) -> list[StepRow]:
    return [
        StepRow(r["seq"], r["kind"], loads(r["payload_json"], {}), r["created_at"])
        for r in conn.execute("SELECT * FROM steps WHERE task_id = ? AND seq > ? ORDER BY seq", (task_id, after_seq))
    ]


def set_fact(conn: sqlite3.Connection, task_id: str, key: str, value: Any, source_seq: int | None) -> None:
    conn.execute(
        "INSERT INTO task_facts(task_id, key, value_json, source_seq, created_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id, key) DO UPDATE SET value_json = excluded.value_json, source_seq = excluded.source_seq, created_at = excluded.created_at",
        (task_id, key, dumps(value), source_seq, now_iso()),
    )


def facts(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    return {r["key"]: loads(r["value_json"]) for r in conn.execute("SELECT key, value_json FROM task_facts WHERE task_id = ? ORDER BY created_at", (task_id,))}


@dataclass
class StepRecord:
    """One completed step: the model's tool call and what it observed."""

    step: int
    tool: str
    arguments: dict[str, Any]
    observation: Observation
    at: str

    @property
    def why(self) -> str:
        return str(self.arguments.get("why", ""))


@dataclass
class TaskState:
    task_id: str
    request: str
    requester: str
    status: str
    max_steps: int | None
    tokens_in: int = 0
    tokens_out: int = 0
    goal: GoalSpec | None = None
    plan: list[PlanStep] = field(default_factory=list)
    plan_version: int = 0
    history: list[StepRecord] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    answers: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    verifications: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] | None = None
    last_step: int = 0                       # highest step number used (a crash may leave one dangling)
    dangling: dict[str, Any] | None = None   # a decided call whose observation was never recorded
    timestamps: list[str] = field(default_factory=list)

    @property
    def step_count(self) -> int:
        return self.last_step

    @property
    def next_step(self) -> int:
        return self.last_step + 1

    @property
    def verify_rounds(self) -> int:
        return len(self.verifications)

    @property
    def has_started(self) -> bool:
        return bool(self.history or self.dangling)

    def notes_of(self, kind: str) -> list[dict[str, Any]]:
        return [n for n in self.notes if n.get("type") == kind]

    def active_minutes(self) -> float:
        """Working time: gaps between consecutive ledger rows, ignoring long waits (human/approval)."""
        from karya.db import parse_iso

        total = 0.0
        for earlier, later in zip(self.timestamps, self.timestamps[1:]):
            gap = (parse_iso(later) - parse_iso(earlier)).total_seconds()
            if gap < 300:
                total += gap
        return total / 60


def load_state(conn: sqlite3.Connection, task_id: str) -> TaskState:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise KeyError(f"task {task_id} not found")
    state = TaskState(
        task_id=task_id,
        request=task["request"],
        requester=task["requester"],
        status=task["status"],
        max_steps=task["max_steps"],
        tokens_in=task["tokens_in"],
        tokens_out=task["tokens_out"],
    )
    pending: dict[int, dict[str, Any]] = {}
    for row in rows(conn, task_id):
        p = row.payload
        state.timestamps.append(row.created_at)
        if row.kind == "intake":
            state.goal = GoalSpec.model_validate(p["goal_spec"])
        elif row.kind == "plan":
            state.plan = [PlanStep.model_validate(s) for s in p["steps"]]
            state.plan_version += 1
        elif row.kind in ("llm", "tool_call"):
            pending[p["step"]] = p
            state.last_step = max(state.last_step, int(p["step"]))
        elif row.kind == "observation":
            call = pending.pop(p["step"], {})
            state.history.append(
                StepRecord(step=p["step"], tool=p["tool"], arguments=call.get("arguments", {}), observation=Observation.model_validate(p["observation"]), at=row.created_at)
            )
        elif row.kind == "system":
            state.notes.append({**p, "seq": row.seq})
        elif row.kind == "human_answer":
            state.answers.append(p)
        elif row.kind == "approval_decision":
            state.decisions.append(p)
        elif row.kind == "verify":
            state.verifications.append(p)
        elif row.kind == "report":
            state.report = p
    if pending and max(pending) == state.last_step:
        state.dangling = {**pending[state.last_step], "step": state.last_step}
    state.facts = facts(conn, task_id)
    return state
