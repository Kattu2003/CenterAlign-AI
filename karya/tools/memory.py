"""memory_search (company memory) and record_fact (task working memory)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import Field

from karya.runtime.models import Observation
from karya.tools.base import Tool, ToolArgs

if TYPE_CHECKING:
    from karya.tools.base import ToolSession


class SearchArgs(ToolArgs):
    query: str = Field(description="What to look for in the handbook, learned facts and past tasks")


class MemorySearch(Tool):
    name = "memory_search"
    description = "Search company memory: handbook procedures, learned facts (with who taught them) and past tasks."
    Args = SearchArgs
    roles = frozenset({"worker", "verifier"})

    def run(self, args: SearchArgs, session: "ToolSession") -> Observation:
        if session.retriever is None:
            return Observation(ok=False, summary="memory is not available", error="No retriever configured.")
        found = session.retriever.retrieve(args.query, k_handbook=5, k_facts=5, k_episodes=3)
        lines = []
        for hit in found.handbook:
            lines.append(f"[{hit.id}] {hit.title}\n{hit.text}")
        for hit in found.facts:
            lines.append(f"[{hit.id}] (learned fact; {hit.provenance}) {hit.text}")
        for hit in found.episodes:
            lines.append(f"[{hit.id}] {hit.title}: {hit.text}")
        ids = found.ids()
        return Observation(ok=True, summary=f"memory: {len(ids)} result(s): {', '.join(ids) or 'none'}", content="\n\n".join(lines) or "(nothing relevant found)")


class FactArgs(ToolArgs):
    key: str = Field(description="Short key, e.g. invoice_number or erp_ref")
    value: str = Field(description="The value, exactly as observed")


class RecordFact(Tool):
    name = "record_fact"
    description = "Record an important value in this task's working memory; recorded facts are shown to you at every step."
    Args = FactArgs

    def run(self, args: FactArgs, session: "ToolSession") -> Observation:
        from karya.runtime import ledger

        ledger.set_fact(session.conn, session.task_id, args.key.strip(), args.value, session.step)
        return Observation(ok=True, summary=f"recorded {args.key} = {json.dumps(args.value)[:120]}")
