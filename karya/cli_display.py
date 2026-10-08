"""Rich rendering for the CLI: live step stream, traces, reports and tables.

Live streaming (karya run / karya worker): one line per step — step number,
tool, `why`, observation summary — with policy events highlighted: approval
held in yellow, denied in red, duplicate prevented and reconciliation in cyan.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.markup import escape

EVENT_STYLE = {
    "approval_required": "bold yellow",
    "denied": "bold red",
    "blocked_host": "bold red",
    "read_only_block": "red",
    "duplicate_prevented": "bold cyan",
    "approved_use": "green",
}


def event_style(event: dict[str, Any]) -> str:
    if event["type"] == "write_outcome":
        message = event.get("message", "")
        if "reconcil" in message or "the record exists" in message or "no record exists" in message or "unknown" in (event.get("status") or ""):
            return "bold cyan"
        return {"committed": "green", "failed": "red"}.get(event.get("status") or "", "yellow")
    return EVENT_STYLE.get(event["type"], "white")


def short_args(arguments: dict[str, Any]) -> str:
    keys = ("connector_id", "endpoint", "ref", "url", "path", "option_label", "value", "key", "query", "text", "status")
    parts = []
    for key in keys:
        if key in arguments and arguments[key] not in (None, ""):
            value = arguments[key]
            parts.append(str(value) if key in ("ref", "url", "path", "connector_id") else f"{key}={json.dumps(value, ensure_ascii=False)[:50]}")
    if arguments.get("params"):
        parts.append(json.dumps(arguments["params"], ensure_ascii=False)[:80])
    return " ".join(parts)


class StepPrinter:
    """Ledger listener: prints each step as it happens."""

    def __init__(self, console: Console, show_task: bool = False, verbose: bool = False) -> None:
        self.console = console
        self.show_task = show_task
        self.verbose = verbose  # --trace: full arguments and timestamps
        self._calls: dict[tuple[str, int], dict[str, Any]] = {}

    def __call__(self, task_id: str, seq: int, kind: str, payload: dict[str, Any]) -> None:
        self.render(task_id, kind, payload)

    def render(self, task_id: str, kind: str, p: dict[str, Any], at: str = "") -> None:
        c = self.console
        prefix = f"[dim]{task_id}[/dim] " if self.show_task else ""
        if self.verbose and at:
            prefix += f"[dim]{at[11:19]}[/dim] "
        if kind == "intake":
            goal = p["goal_spec"]
            c.print(f"{prefix}[bold]Goal:[/bold] {escape(goal['objective'])}")
            for criterion in goal["success_criteria"]:
                c.print(f"{prefix}  [bold]{criterion['id']}[/bold] {escape(criterion['description'])} [dim](check: {escape(criterion['verification'])})[/dim]")
            if goal.get("blocking_questions"):
                c.print(f"{prefix}  [yellow]Blocking questions:[/yellow] {escape('; '.join(goal['blocking_questions']))}")
        elif kind == "plan":
            label = "Plan" if p.get("source") == "intake" else "Plan updated"
            steps = " → ".join(f"{s['id']} {s['description']}" + ("" if s["status"] == "pending" else f" [{s['status']}]") for s in p["steps"])
            c.print(f"{prefix}[bold]{label}:[/bold] [dim]{escape(steps)}[/dim]")
        elif kind in ("llm", "tool_call"):
            self._calls[(task_id, p["step"])] = p
        elif kind == "observation":
            call = self._calls.pop((task_id, p["step"]), {})
            obs = p["observation"]
            arguments = call.get("arguments", {})
            mark = "[green]✓[/green]" if obs["ok"] else "[red]✗[/red]"
            c.print(f"{prefix}{mark} [bold]#{p['step']:02d} {p['tool']}[/bold] {escape(short_args(arguments))}  [dim]why: {escape(str(arguments.get('why', '')))}[/dim]")
            if self.verbose:
                shown = {k: v for k, v in arguments.items() if k != "why"}
                c.print(f"{prefix}     [dim]args: {escape(json.dumps(shown, ensure_ascii=False, default=str))}[/dim]")
            c.print(f"{prefix}     → {escape(obs['summary'])}")
            if self.verbose:
                for note in obs.get("meta", {}).get("notes", []):
                    c.print(f"{prefix}     [dim]note: {escape(note)}[/dim]")
                if obs.get("artifacts"):
                    c.print(f"{prefix}     [dim]artifacts: {', '.join(obs['artifacts'])}[/dim]")
            for event in obs.get("policy_events", []):
                c.print(f"{prefix}     [{event_style(event)}]● {event['type']}: {escape(event['message'])}[/{event_style(event)}]")
            for flag in obs.get("flags", []):
                c.print(f"{prefix}     [magenta]⚠ {flag.get('type')}: {escape(flag.get('excerpt', '')[:160])}[/magenta]")
            if obs.get("error"):
                c.print(f"{prefix}     [red]{escape(obs['error'][:300])}[/red]")
        elif kind == "system":
            if p.get("type") == "retrieval":
                return
            style = {"nudge": "yellow", "verifier_feedback": "yellow", "error": "red", "resume": "cyan"}.get(p.get("type", ""), "dim")
            c.print(f"{prefix}[{style}]· {escape(p.get('text', ''))}[/{style}]")
        elif kind == "human_answer":
            c.print(f"{prefix}[green]● {p['by']} answered {p['question_id']}: {escape(p['answer'])}[/green]")
        elif kind == "approval_decision":
            style = "green" if p["decision"] == "approved" else "red"
            c.print(f"{prefix}[{style}]● {p['approval_id']} {p['decision']} by {p['by']}{(': ' + escape(p['note'])) if p.get('note') else ''}[/{style}]")
        elif kind == "verify":
            result = p["result"]
            style = "green" if result["overall"] == "pass" else "red"
            c.print(f"{prefix}[bold {style}]Verifier (round {p['round']}): {result['overall']}[/bold {style}]")
            for verdict in result["verdicts"]:
                vstyle = {"pass": "green", "fail": "red"}.get(verdict["verdict"], "yellow")
                c.print(f"{prefix}  [{vstyle}]{verdict['criterion_id']} {verdict['verdict']}[/{vstyle}] {escape(verdict.get('note', ''))}")
        elif kind == "report":
            c.print(f"{prefix}[bold]Done: {p['status']}[/bold] — {escape(p.get('summary', ''))}")
