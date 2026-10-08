"""Turn human answers and decisions into company facts (plan §11.12, DECISIONS.md D11).

The fast model rewrites question + answer + task context as ONE standalone
statement with entities, or says the answer is not durable (only relevant to
this task). Every fact keeps its provenance: who said it, when, on which task
and question. Facts never override policies; humans can `forget` them.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, Field

from karya.company import Company
from karya.db import audit, loads
from karya.llm.base import LLMClient, LLMError, Message, structured
from karya.memory import store
from karya.memory.store import Fact
from karya.prompts import load_prompt, render


class LearnedFact(BaseModel):
    durable: bool = Field(description="True only for knowledge useful in future tasks")
    statement: str = Field(default="", description="One standalone statement naming entities fully (legal names, IDs)")
    entities: list[str] = Field(default=[], description="Entities the statement is about")


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _context_for_question(conn: sqlite3.Connection, question: sqlite3.Row) -> tuple[str, str, str]:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (question["task_id"],)).fetchone()
    facts = {r["key"]: loads(r["value_json"]) for r in conn.execute("SELECT key, value_json FROM task_facts WHERE task_id = ?", (question["task_id"],))}
    options = loads(question["options_json"], [])
    text = (
        f"TASK REQUEST ({task['id']}, from {task['requester']}): {task['request']}\n"
        f"QUESTION from the AI employee ({question['id']}): {question['question']}\n"
        + (f"OPTIONS it offered: {json.dumps(options, ensure_ascii=False)}\n" if options else "")
        + (f"ITS CONTEXT: {question['context']}\n" if question["context"] else "")
        + (f"FACTS IT HAD RECORDED: {json.dumps(facts, ensure_ascii=False, default=str)[:1500]}\n" if facts else "")
        + f"ANSWER from {question['answered_by']}: {question['answer']}"
    )
    return text, "human_answer", question["answered_by"]


def _context_for_approval(conn: sqlite3.Connection, approval: sqlite3.Row) -> tuple[str, str, str]:
    kind = "approval" if approval["status"] in ("approved", "consumed") else "rejection"
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (approval["task_id"],)).fetchone()
    text = (
        f"TASK REQUEST ({task['id']}): {task['request']}\n"
        f"HELD ACTION ({approval['id']}, rules {', '.join(loads(approval['rule_ids_json'], []))}): {approval['summary']}\n"
        f"DECISION: {approval['status']} by {approval['decided_by']}\n"
        f"{'NOTE' if kind == 'approval' else 'REJECTION REASON'} from {approval['decided_by']}: {approval['reason']}"
    )
    return text, kind, approval["decided_by"]


def learn(conn: sqlite3.Connection, company: Company, ref: str, llm: LLMClient | None = None) -> Fact | None:
    """Learn from a question (Q-...) or an approval decision (APR-...). Never raises:
    learning is best-effort; the answer/decision itself is already recorded."""
    if llm is None:
        return None
    if ref.startswith("Q-"):
        row = conn.execute("SELECT * FROM questions WHERE id = ?", (ref,)).fetchone()
        if row is None or row["answer"] is None or not row["remember"]:
            return None
        text, kind, by = _context_for_question(conn, row)
    elif ref.startswith("APR-"):
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (ref,)).fetchone()
        if row is None or not (row["reason"] or "").strip():
            return None
        text, kind, by = _context_for_approval(conn, row)
    else:
        return None
    messages = [
        Message("system", render(load_prompt("fact_learning"), company_name=company.name)),
        Message("user", text),
    ]
    try:
        learned, _usage = structured(llm, messages, LearnedFact, "submit_fact", "Return the durable fact, or durable=false.", model=llm.fast_model)
    except LLMError as exc:
        audit(conn, "system", "fact_learning_failed", {"ref": ref, "error": str(exc)}, row["task_id"])
        return None
    if not learned.durable or not learned.statement.strip():
        audit(conn, "system", "fact_not_durable", {"ref": ref}, row["task_id"])
        return None
    for existing in store.list_facts(conn):
        if _norm(existing.statement) == _norm(learned.statement):
            return existing  # already known
    return store.add_fact(conn, learned.statement.strip(), learned.entities, kind, by, row["task_id"], ref)


def learner_for(llm: LLMClient) -> Any:
    """A callable for inbox.answer_question / inbox.decide."""

    def _learn(conn: sqlite3.Connection, company: Company, ref: str) -> Fact | None:
        return learn(conn, company, ref, llm)

    return _learn
