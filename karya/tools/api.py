"""api_get: declared read-only API endpoints, through the gateway (DECISIONS.md D14)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import Field

from karya.runtime.models import Observation
from karya.tools.base import Tool, ToolArgs

if TYPE_CHECKING:
    from karya.tools.base import ToolSession

MAX_CHARS = 6000


class ApiGetArgs(ToolArgs):
    connector_id: str = Field(description="Connector id, e.g. erp or mail")
    endpoint: str = Field(description="A declared endpoint name, e.g. find_invoice")
    params: dict[str, str] = Field(default={}, description='Query and path parameters, e.g. {"vendor_id": "V001", "invoice_number": "AI/2026/0917"}')


class ApiGet(Tool):
    name = "api_get"
    description = "Call a declared read-only API endpoint of a company app (fast, exact lookups). The API key is added for you. Returns JSON."
    Args = ApiGetArgs
    roles = frozenset({"worker", "verifier"})

    def run(self, args: ApiGetArgs, session: "ToolSession") -> Observation:
        result = session.gateway.api_get(args.connector_id, args.endpoint, args.params)
        source = f"{args.connector_id}:api/{args.endpoint}"
        if result.status is None:
            return Observation(ok=False, summary=f"{source} failed", error=result.error, meta={"retries": result.retries})
        text = json.dumps(result.data, indent=1, ensure_ascii=False) if not isinstance(result.data, str) else result.data
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n…truncated; narrow the query with parameters."
        count = f", {len(result.data)} item(s)" if isinstance(result.data, list) else ""
        return Observation(
            ok=result.ok,
            summary=f"{source} {args.params or ''} → HTTP {result.status}{count}",
            content=text,
            untrusted=True,
            source=source,
            error=result.error,
            meta={"retries": result.retries},
        )
