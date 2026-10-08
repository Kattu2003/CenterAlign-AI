"""Intake: request -> Goal Spec (checkable success criteria) + initial plan.

"Done" is defined before the first action (DECISIONS.md D8). The criteria are
what the independent verifier checks at the end, so each one must name the
system where it will be checked.
"""

from __future__ import annotations

from karya.company import Company
from karya.llm.base import LLMClient, Message, Usage, structured
from karya.memory.retrieval import Retrieved, Retriever
from karya.prompts import load_prompt, render
from karya.runtime.context import company_context, company_today
from karya.runtime.models import GoalSpec, IntakeOutput, PlanStep

GENERIC_SYSTEM_WORDS = ("shared drive", "downloads", "workspace", "audit", "report", "policy", "gateway", "api", "file")


def known_systems(company: Company) -> list[str]:
    words = list(GENERIC_SYSTEM_WORDS)
    for connector in company.connectors.values():
        words.append(connector.id.lower())
        words.append(connector.name.lower())
    return words


def problems(goal: GoalSpec, company: Company) -> list[str]:
    issues = []
    systems = known_systems(company)
    for criterion in goal.success_criteria:
        text = criterion.verification.lower()
        if not any(word in text for word in systems):
            issues.append(f"criterion {criterion.id}: `verification` must name the system where it is checked (one of: {', '.join(c.id for c in company.connectors.values())})")
    ids = [c.id for c in goal.success_criteria]
    if len(ids) != len(set(ids)):
        issues.append("criterion ids must be unique")
    return issues


def run(llm: LLMClient, company: Company, retriever: Retriever, request: str, requester: str) -> tuple[GoalSpec, list[PlanStep], Usage, Retrieved]:
    retrieved = retriever.retrieve(request, k_handbook=4, k_facts=3, k_episodes=2, expand=True)
    p = company.profile
    system = render(load_prompt("intake"), company_name=p.name, today=company_today(company), timezone=p.timezone)
    user = f"=== COMPANY CONTEXT ===\n{company_context(company, retrieved)}\n\n=== REQUEST from {requester} ===\n{request}"
    messages = [Message("system", system), Message("user", user)]
    output, usage = structured(llm, messages, IntakeOutput, "submit_goal_spec", "Submit the Goal Spec (success criteria first) and the initial high-level plan.")
    issues = problems(output.goal_spec, company)
    if issues:
        retry = messages + [Message("assistant", output.model_dump_json()), Message("user", "Fix these problems and call submit_goal_spec again: " + "; ".join(issues))]
        output, more = structured(llm, retry, IntakeOutput, "submit_goal_spec", "Submit the corrected Goal Spec and plan.")
        usage = usage + more
    plan = [PlanStep(id=f"p{i}", description=draft.description) for i, draft in enumerate(output.plan, 1)]
    return output.goal_spec, plan, usage, retrieved
