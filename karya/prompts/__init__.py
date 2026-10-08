"""Prompt templates (Markdown files next to this module).

Rendering replaces `{name}` placeholders for the given keys only, so prompts
may contain literal braces (JSON examples) without escaping.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def render(template: str, **values: object) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    return re.sub(r"\{(\w+)\}", replace, template)
