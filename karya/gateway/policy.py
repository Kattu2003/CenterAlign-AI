"""Policy engine: structured conditions over a write's typed fields.

Policies are data (`company/policies.yaml`), evaluated in code by the gateway
(see DECISIONS.md D3). There is no `eval`: a condition is (field, op, value),
conditions in a rule are ANDed, and effects combine with the precedence
deny > require_approval > allow.

Fail closed (DECISIONS.md D4): if a condition cannot be evaluated because the
field is missing or cannot be coerced, the rule is treated as matching, so a
malformed request can never slip past a deny or an approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

Op = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains"]
Effect = Literal["allow", "deny", "require_approval"]
PRECEDENCE: dict[str, int] = {"allow": 0, "require_approval": 1, "deny": 2}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Condition(_Strict):
    field: str
    op: Op
    value: Any


class RuleMatch(_Strict):
    connector: str
    action: str


class Rule(_Strict):
    id: str
    description: str
    match: RuleMatch
    when: list[Condition] = []
    effect: Effect
    approver_role: str | None = None

    @model_validator(mode="after")
    def _approver(self) -> "Rule":
        if self.effect == "require_approval" and not self.approver_role:
            raise ValueError(f"rule {self.id}: require_approval needs approver_role")
        return self


class PolicyDefaults(_Strict):
    unknown_host: Literal["deny"] = "deny"
    unknown_write: Literal["deny"] = "deny"
    reads: Literal["allow"] = "allow"


class PolicySet(_Strict):
    version: int = 1
    defaults: PolicyDefaults = PolicyDefaults()
    rules: list[Rule] = []

    def rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)


@dataclass
class PolicyDecision:
    effect: Effect
    rules: list[Rule] = field(default_factory=list)          # every matching rule
    deciding: list[Rule] = field(default_factory=list)       # matching rules with the winning effect
    notes: list[str] = field(default_factory=list)

    @property
    def rule_ids(self) -> list[str]:
        return [r.id for r in self.rules]

    @property
    def deciding_ids(self) -> list[str]:
        return [r.id for r in self.deciding]

    @property
    def approver_roles(self) -> list[str]:
        return sorted({r.approver_role for r in self.deciding if r.approver_role})

    def message(self) -> str:
        return "; ".join(f"{r.id}: {r.description}" for r in self.deciding)


def load_policies(path: Path) -> PolicySet:
    return PolicySet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


class _Unevaluable(Exception):
    pass


def _coerce_like(value: Any, sample: Any) -> Any:
    """Coerce a rule's literal to the type of the (already typed) field value."""
    try:
        if isinstance(sample, bool):
            return bool(value)
        if isinstance(sample, Decimal):
            return Decimal(str(value))
        if isinstance(sample, date):
            return value if isinstance(value, date) else date.fromisoformat(str(value))
        if isinstance(sample, int):
            return int(value)
        return str(value).casefold()
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise _Unevaluable(f"cannot compare {value!r} with {sample!r}") from exc


def _normalize(value: Any) -> Any:
    return value.casefold() if isinstance(value, str) else value


def _check(cond: Condition, fields: dict[str, Any]) -> bool:
    if cond.field not in fields or fields[cond.field] is None:
        raise _Unevaluable(f"field {cond.field!r} is missing")
    actual = fields[cond.field]
    if isinstance(actual, str) and cond.op not in ("contains", "in", "not_in", "eq", "ne"):
        raise _Unevaluable(f"field {cond.field!r} is not numeric/date: {actual!r}")
    actual_n = _normalize(actual)
    if cond.op in ("in", "not_in"):
        options = cond.value if isinstance(cond.value, list) else [cond.value]
        coerced = [_coerce_like(v, actual) for v in options]
        result = actual_n in coerced
        return result if cond.op == "in" else not result
    if cond.op == "contains":
        if isinstance(actual, (list, tuple, set)):
            return _normalize(cond.value) in [_normalize(v) for v in actual]
        return str(cond.value).casefold() in str(actual).casefold()
    expected = _coerce_like(cond.value, actual)
    try:
        return {
            "eq": actual_n == expected,
            "ne": actual_n != expected,
            "gt": actual_n > expected,
            "gte": actual_n >= expected,
            "lt": actual_n < expected,
            "lte": actual_n <= expected,
        }[cond.op]
    except TypeError as exc:
        raise _Unevaluable(str(exc)) from exc


def evaluate(policies: PolicySet, connector: str, action: str, fields: dict[str, Any]) -> PolicyDecision:
    """Evaluate every rule that matches (connector, action) against typed fields."""
    matching: list[Rule] = []
    notes: list[str] = []
    for rule in policies.rules:
        if rule.match.connector != connector or rule.match.action != action:
            continue
        try:
            if all(_check(cond, fields) for cond in rule.when):
                matching.append(rule)
        except _Unevaluable as exc:
            # Fail closed: an unevaluable condition counts as matching.
            matching.append(rule)
            notes.append(f"{rule.id}: condition could not be evaluated ({exc}); applying the rule")
    if not matching:
        return PolicyDecision(effect="allow", notes=notes)
    winner = max((r.effect for r in matching), key=lambda e: PRECEDENCE[e])
    return PolicyDecision(effect=winner, rules=matching, deciding=[r for r in matching if r.effect == winner], notes=notes)


def referenced_fields(rules: list[Rule]) -> set[str]:
    """Fields the given rules look at (they become part of an approval's scope)."""
    return {cond.field for rule in rules for cond in rule.when}
