"""The human inbox: pending questions and approvals, and the actions on them.

Shared by the CLI (`karya inbox/answer/approve/reject`) and the eval harness's
auto-responder, so both go through exactly the same code: record the decision,
append it to the task's ledger, requeue the task, and (optionally) learn from it.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from karya.company import Company
from karya.db import audit, loads, now_iso, transaction
from karya.gateway import approvals
from karya.runtime import ledger
from karya.worker import queue

Learner = Callable[..., Any]


class InboxError(RuntimeError):
    pass


def pending_questions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM questions WHERE answer IS NULL ORDER BY created_at").fetchall()


def pending_approvals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return approvals.pending(conn)


def answer_question(
    conn: sqlite3.Connection,
    company: Company,
    question_id: str,
    answer: str,
    by: str | None = None,
    remember: bool = True,
    learn: Learner | None = None,
) -> tuple[sqlite3.Row, Any]:
    """Record an answer, append it to the ledger, requeue the task. Returns (question, learned fact or None)."""
    question = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if question is None:
        raise InboxError(f"no question {question_id}")
    if question["answer"] is not None:
        raise InboxError(f"{question_id} was already answered by {question['answered_by']}")
    task = queue.get(conn, question["task_id"])
    by = by or task["requester"]
    person = company.person(by)
    if person is None:
        raise InboxError(f"{by} is not a known person at {company.name}")
    if not answer.strip():
        raise InboxError("the answer is empty")
    with transaction(conn):
        conn.execute(
            "UPDATE questions SET answer = ?, answered_by = ?, remember = ?, answered_at = ? WHERE id = ?",
            (answer.strip(), person.name, 1 if remember else 0, now_iso(), question_id),
        )
        audit(conn, "human", "question_answered", {"question_id": question_id, "by": person.name, "answer": answer, "remember": remember}, question["task_id"])
    ledger.append(
        conn,
        question["task_id"],
        "human_answer",
        {"question_id": question_id, "question": question["question"], "options": loads(question["options_json"], []), "answer": answer.strip(), "by": person.name, "remember": remember},
    )
    queue.requeue(conn, question["task_id"])
    fact = None
    if remember and learn is not None:
        fact = learn(conn, company, question_id)
    return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone(), fact


def decide(
    conn: sqlite3.Connection,
    company: Company,
    approval_id: str,
    approve: bool,
    by: str,
    note: str = "",
    learn: Learner | None = None,
) -> tuple[sqlite3.Row, Any]:
    """Approve or reject (RBAC-checked); requeues the waiting task. Returns (approval, learned fact or None)."""
    try:
        decided = approvals.decide(conn, company, approval_id, approve, by, note)
    except approvals.ApprovalError as exc:
        raise InboxError(str(exc)) from exc
    fact = None
    if learn is not None and note.strip():
        fact = learn(conn, company, approval_id)
    return decided, fact
