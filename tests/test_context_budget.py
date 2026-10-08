"""The per-step prompt stays well under the plan's ~30k-token target (plan §11.4)."""

from __future__ import annotations

import pytest

from tests import scripted_flows as flows
from tests.test_e2e_scripted import make_ctx, run_until_rest

pytestmark = pytest.mark.browser


def test_step_prompts_stay_small(live_sandbox, fresh_sandbox):
    llm = flows.happy_llm(live_sandbox, fresh_sandbox)
    ctx = make_ctx(llm)
    run_until_rest(ctx)
    sizes = [sum(len(m.content) for m in r["messages"]) for r in llm.requests if r["tool_choice"] == "required"]
    assert sizes, "no step prompts captured"
    approx_tokens = max(sizes) / 4  # ~4 characters per token for English/markup
    print(f"step prompts: {len(sizes)}, max {max(sizes)} chars (~{approx_tokens:.0f} tokens), median {sorted(sizes)[len(sizes) // 2]} chars")
    assert approx_tokens < 30_000 / 2, "prompt budget regression"
