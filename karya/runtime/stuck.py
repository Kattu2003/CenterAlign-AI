"""Budgets and stuck detection (plan §11.10).

Checked before every step. "nudge" adds a system note; "escalate" forces a
finish with status `escalated` (still verified and reported), so a confused
agent stops safely instead of looping or burning budget.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from karya.config import Settings
from karya.runtime.ledger import StepRecord, TaskState

REPEAT_WINDOW = 6
REPEAT_THRESHOLD = 3
ERROR_STREAK = 5


@dataclass
class Guard:
    action: Literal["none", "nudge", "escalate"]
    reason: str = ""

    @property
    def message(self) -> str:
        return self.reason


def max_steps_for(state: TaskState, settings: Settings) -> int:
    if state.max_steps:
        return state.max_steps
    return settings.max_steps_batch if state.goal and state.goal.batch else settings.max_steps


def _signature(record: StepRecord) -> str:
    args = {k: v for k, v in record.arguments.items() if k != "why"}
    return record.tool + ":" + json.dumps(args, sort_keys=True, default=str)


def _obs_hash(record: StepRecord) -> str:
    obs = record.observation
    return hashlib.sha1(f"{obs.ok}|{obs.summary}|{obs.content[:4000]}".encode()).hexdigest()


def _repeating(history: list[StepRecord]) -> bool:
    """Same tool + same args 3 times within the last 6 steps, with the same result."""
    window = history[-REPEAT_WINDOW:]
    if not window:
        return False
    last = window[-1]
    key = (_signature(last), _obs_hash(last))
    return sum(1 for r in window if (_signature(r), _obs_hash(r)) == key) >= REPEAT_THRESHOLD


def _denied_twice(history: list[StepRecord]) -> str | None:
    seen: set[str] = set()
    for record in history:
        for event in record.observation.policy_events:
            if event.type == "denied" and event.rule_ids and event.rule_ids[0] not in ("blocked_path", "precheck_unavailable"):
                key = ",".join(event.rule_ids)
                if key in seen:
                    return key
                seen.add(key)
    return None


def check(state: TaskState, settings: Settings) -> Guard:
    limit = max_steps_for(state, settings)
    if state.step_count >= limit:
        return Guard("escalate", f"Step budget exhausted ({state.step_count} of {limit} steps).")
    tokens = state.tokens_in + state.tokens_out
    if tokens >= settings.task_token_budget:
        return Guard("escalate", f"Token budget exhausted ({tokens:,} of {settings.task_token_budget:,}).")
    if state.active_minutes() >= settings.task_wallclock_min:
        return Guard("escalate", f"Time budget exhausted ({state.active_minutes():.1f} of {settings.task_wallclock_min} minutes of work).")
    history = state.history
    if len(history) >= ERROR_STREAK and all(not r.observation.ok for r in history[-ERROR_STREAK:]):
        return Guard("escalate", f"{ERROR_STREAK} consecutive actions failed; escalating instead of continuing blindly.")
    denied = _denied_twice(history)
    if denied:
        return Guard("escalate", f"Policy {denied} blocks this; it was attempted twice. Escalating instead of retrying.")
    if _repeating(history):
        nudges_since_last_progress = len([n for n in state.notes_of("nudge") if n.get("step", 0) >= history[-REPEAT_WINDOW:][0].step])
        if nudges_since_last_progress >= 1:
            return Guard("escalate", "Still repeating the same action with the same result after a nudge; escalating.")
        return Guard(
            "nudge",
            "You are repeating an action and the result has not changed. Try a different approach, use the API, or ask the human.",
        )
    return Guard("none")
