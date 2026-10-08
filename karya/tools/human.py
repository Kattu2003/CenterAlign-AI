"""ask_human: one clear question to the requester; the task suspends until answered."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field

from karya.db import audit, dumps, next_id, now_iso, transaction
from karya.runtime.models import Observation
from karya.tools.base import Tool, ToolArgs

if TYPE_CHECKING:
    from karya.tools.base import ToolSession


class AskArgs(ToolArgs):
    question: str = Field(description="One clear question")
    options: list[str] = Field(default=[], description="Concrete options you found, with evidence (e.g. invoice numbers, amounts)")
    context: str = Field(default="", description="What you checked and what you will do with each answer")


class AskHuman(Tool):
    name = "ask_human"
    description = "Ask the requester one question when the answer changes what you will do and you cannot find it yourself. The task pauses until they answer."
    Args = AskArgs

    def run(self, args: AskArgs, session: "ToolSession") -> Observation:
        with transaction(session.conn):
            question_id = next_id(session.conn, "question", "Q")
            session.conn.execute(
                "INSERT INTO questions(id, task_id, question, options_json, context, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (question_id, session.task_id, args.question, dumps(args.options), args.context, now_iso()),
            )
            audit(session.conn, "agent", "question_asked", {"question_id": question_id, "question": args.question, "options": args.options}, session.task_id)
        return Observation(
            ok=True,
            summary=f"asked {question_id}: {args.question[:120]}",
            content=f"Question {question_id} was sent to {session.requester}. The task pauses until they answer.",
            meta={"question_id": question_id},
        )
