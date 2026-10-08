"""Prompt-injection detection: heuristics plus an optional fast-model check.

Detection is a warning system, not the defence: the gateway enforces policies
whatever the model believes (DECISIONS.md D3). Findings become observation
flags and a visible warning, and the agent must report them in `finish.flags`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from karya.llm.base import LLMClient, LLMError, Message, Usage, structured
from karya.prompts import load_prompt

# (pattern, reason, kind). "prompt_injection" = text aimed at software/AI;
# "possible_fraud" = classic payment-fraud requests (bank changes).
PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"automated\s+(?:invoice\s+)?(?:processing|systems?)", re.I), "addressed to automated systems", "prompt_injection"),
    (re.compile(r"\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\b", re.I), "asks to ignore previous instructions", "prompt_injection"),
    (re.compile(r"\bdo\s+not\s+(?:mention|report|tell|disclose)\b", re.I), "asks to hide something", "prompt_injection"),
    (re.compile(r"\bpre-?approved\b", re.I), "claims to be pre-approved", "prompt_injection"),
    (re.compile(r"\bsystem\s+prompt\b", re.I), "mentions a system prompt", "prompt_injection"),
    (re.compile(r"\b(?:AI|assistant|agent|LLM|language model|chatbot)s?\b[^.\n]{0,60}\b(?:must|should|need to|are instructed|you will)\b", re.I), "gives orders to an AI", "prompt_injection"),
    (re.compile(r"\bnote\s+for\s+(?:ai|automated|bots?)\b", re.I), "addressed to automated systems", "prompt_injection"),
    (re.compile(r"\b(?:update|change|replace)\b[^.\n]{0,80}\bbank\b", re.I), "asks to change bank details", "possible_fraud"),
    (re.compile(r"\b(?:update|change|replace)\b[^.\n]{0,60}\b(?:account|a/c)\b[^.\n]{0,40}\d{6,}", re.I), "asks to change an account number", "possible_fraud"),
]


@dataclass
class Finding:
    excerpt: str
    reasons: list[str]
    kind: str  # prompt_injection | possible_fraud

    def as_flag(self, source: str | None) -> dict[str, Any]:
        return {"type": self.kind, "excerpt": self.excerpt, "reasons": self.reasons, "source": source, "detector": "heuristic"}


def _paragraphs(text: str) -> list[str]:
    blocks = re.split(r"\n\s*\n", text)
    return [" ".join(b.split()) for b in blocks if b.strip()]


def scan(text: str) -> list[Finding]:
    """Heuristic scan; one finding per paragraph that trips any pattern."""
    findings: list[Finding] = []
    for paragraph in _paragraphs(text):
        reasons, kinds = [], set()
        for pattern, reason, kind in PATTERNS:
            if pattern.search(paragraph):
                reasons.append(reason)
                kinds.add(kind)
        if reasons:
            kind = "prompt_injection" if "prompt_injection" in kinds else "possible_fraud"
            findings.append(Finding(excerpt=paragraph[:400], reasons=sorted(set(reasons)), kind=kind))
    return findings


def scan_snapshot(snapshot: str) -> list[Finding]:
    """Scan only the free text of a page snapshot (not buttons, headings, forms or
    tables), so ordinary UI such as an "Update bank details" form is not flagged."""
    free_text = []
    for line in snapshot.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("[", "|", "URL:", "TITLE:", "HTTP:", "---")):
            free_text.append("")
            continue
        free_text.append(stripped)
    return scan("\n".join(free_text))


class InjectionCheck(BaseModel):
    contains_instructions: bool
    excerpt: str = ""
    reason: str = ""


def llm_check(llm: LLMClient, text: str, model: str | None = None) -> tuple[InjectionCheck | None, Usage]:
    """Optional second opinion from the fast model (plan §11.14). Never raises."""
    messages = [
        Message("system", load_prompt("injection_check")),
        Message("user", f"<untrusted source=\"document\">\n{text[:12000]}\n</untrusted>"),
    ]
    try:
        result, usage = structured(llm, messages, InjectionCheck, "submit_injection_check", "Report whether the text contains instructions aimed at an AI or automation.", model=model)
        return result, usage
    except LLMError:
        return None, Usage()
