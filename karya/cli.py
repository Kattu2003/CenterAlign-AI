"""Karya command-line interface (typer).

Phase 0 skeleton: every command from plan §11.13 exists so `karya --help` shows
the whole surface; later phases fill them in.
"""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Karya: an AI employee that completes company work.", no_args_is_help=True)
sandbox_app = typer.Typer(help="Manage the Meridian sandbox apps.", no_args_is_help=True)
config_app = typer.Typer(help="Company configuration.", no_args_is_help=True)
memory_app = typer.Typer(help="Company memory governance.", no_args_is_help=True)
evals_app = typer.Typer(help="Evaluation harness.", no_args_is_help=True)
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(config_app, name="config")
app.add_typer(memory_app, name="memory")
app.add_typer(evals_app, name="evals")

console = Console()


def _todo(phase: int) -> None:
    console.print(f"[yellow]Not implemented yet (phase {phase}).[/yellow]")
    raise typer.Exit(code=2)


# --- sandbox (the fake company's systems; imported lazily so the runtime never depends on it) ---


@sandbox_app.command("up")
def sandbox_up() -> None:
    """Start the sandbox apps (ERP 8101, Mail 8102, HR 8103)."""
    from sandbox import run as sandbox_run

    for name, message in sandbox_run.up():
        console.print(f"[bold]{name}[/bold]: {message}")


@sandbox_app.command("down")
def sandbox_down() -> None:
    """Stop the sandbox apps started by `karya sandbox up`."""
    from sandbox import run as sandbox_run

    for name, message in sandbox_run.down() or [("sandbox", "nothing to stop")]:
        console.print(f"[bold]{name}[/bold]: {message}")


@sandbox_app.command("reset")
def sandbox_reset() -> None:
    """Re-seed sandbox databases, invoice PDFs and shared drive (never touches Karya's DB or memory)."""
    from sandbox import run as sandbox_run

    base = sandbox_run.reset()
    console.print(f"Sandbox reset. Seed dates are relative to D = [bold]{base.isoformat()}[/bold] (Asia/Kolkata).")


@sandbox_app.command("status")
def sandbox_status() -> None:
    """Show which sandbox apps respond."""
    from sandbox import run as sandbox_run

    table = Table(title="Sandbox apps")
    for column in ("App", "URL", "Status", "PID"):
        table.add_column(column)
    for row in sandbox_run.status():
        state = "[green]up[/green]" if row["up"] else "[red]down[/red]"
        table.add_row(row["app"], row["url"], state, str(row["pid"] or "—"))
    console.print(table)


@sandbox_app.command("chaos")
def sandbox_chaos(
    profile: str = typer.Argument(..., help="off | light | heavy"),
    seed: int = typer.Option(0, help="RNG seed (reproducible chaos)"),
) -> None:
    """Set the chaos profile on every running sandbox app."""
    from sandbox import run as sandbox_run
    from sandbox.common.chaos import PROFILES

    if profile not in PROFILES:
        console.print(f"[red]Unknown chaos profile {profile!r}.[/red] Choose one of: {', '.join(PROFILES)}")
        raise typer.Exit(code=2)
    result = sandbox_run.set_chaos(profile, seed)
    if not result:
        console.print("[yellow]No sandbox apps are running.[/yellow]")
    for name, state in result.items():
        console.print(f"[bold]{name}[/bold]: profile={state['profile']} seed={state['seed']}")


@sandbox_app.command("arm")
def sandbox_arm(
    fault: str = typer.Argument(..., help="http_500 | slow | session_expired | label_drift | commit_then_504 | write_conflict_409"),
    count: int = typer.Option(1, help="How many matching requests get the fault"),
    app_name: str = typer.Option("erp", "--app", help="Which sandbox app"),
    path: Optional[str] = typer.Option(None, help="Only requests to this path (glob), e.g. /invoices/new"),
) -> None:
    """Arm a deterministic fault: the next N matching requests get it."""
    import httpx

    from sandbox import run as sandbox_run
    from sandbox.common.chaos import FAULTS

    if fault not in FAULTS:
        console.print(f"[red]Unknown fault {fault!r}.[/red] Choose one of: {', '.join(FAULTS)}")
        raise typer.Exit(code=2)
    try:
        state = sandbox_run.arm(app_name, fault, count, path)
    except httpx.HTTPError as exc:
        console.print(f"[red]Could not arm {fault} on {app_name}: {exc}[/red] (is the sandbox up? `karya sandbox status`)")
        raise typer.Exit(code=1) from exc
    console.print(f"[bold]{app_name}[/bold]: armed {state['armed']}")


@config_app.command("check")
def config_check() -> None:
    """Validate the company configuration and print a summary."""
    import os

    from karya.company import load_company

    company = load_company()
    profile = company.profile
    console.print(f"[bold]{profile.name}[/bold] ({company.dir}) · {profile.timezone} · {profile.currency}")

    people = Table(title="People")
    for column in ("Name", "Title", "Roles"):
        people.add_column(column)
    for person in profile.people:
        people.add_row(person.name, person.title, ", ".join(person.roles))
    console.print(people)

    handbook = Table(title="Handbook")
    for column in ("Doc", "Title", "Chunks"):
        handbook.add_column(column)
    for doc in company.handbook:
        handbook.add_row(doc.id, doc.title, str(len(doc.chunks)))
    console.print(handbook)

    connectors = Table(title="Connectors")
    for column in ("Id", "Kind", "Location", "Purpose"):
        connectors.add_column(column)
    for spec in company.connectors.values():
        location = spec.base_url or f"{spec.root} (read {spec.read}, write {spec.write})"
        connectors.add_row(spec.id, spec.kind, location, " ".join(spec.purpose.split())[:70])
    console.print(connectors)

    actions = Table(title="Declared actions (everything else that writes is denied)")
    for column in ("Connector", "Action", "Request", "Risk", "Idempotency key", "Reconcile"):
        actions.add_column(column)
    for spec in company.connectors.values():
        for action in spec.actions:
            reconcile = f"{action.reconcile.api} ({action.reconcile.exists_if or 'match'})" if action.reconcile else "—"
            actions.add_row(spec.id, action.name, f"{action.method} {action.path}", action.risk, ", ".join(action.idempotency_key) or "—", reconcile)
    console.print(actions)

    endpoints = Table(title="Read-only API endpoints")
    for column in ("Connector", "Endpoint", "Path", "Params"):
        endpoints.add_column(column)
    for spec in company.connectors.values():
        for endpoint in spec.api.endpoints if spec.api else []:
            endpoints.add_row(spec.id, endpoint.name, endpoint.path, ", ".join(endpoint.params) or "—")
    console.print(endpoints)

    rules = Table(title=f"Policy rules (defaults: hosts {company.policies.defaults.unknown_host}, writes {company.policies.defaults.unknown_write})")
    for column in ("Rule", "Match", "When", "Effect", "Approver role"):
        rules.add_column(column)
    for rule in company.policies.rules:
        when = " AND ".join(f"{c.field} {c.op} {c.value}" for c in rule.when) or "always"
        rules.add_row(rule.id, f"{rule.match.connector}.{rule.match.action}", when, rule.effect, rule.approver_role or "—")
    console.print(rules)

    schemas = Table(title="Document schemas")
    for column in ("Schema", "Fields", "Checks"):
        schemas.add_column(column)
    for schema in company.schemas.values():
        schemas.add_row(schema.name, ", ".join(schema.fields), ", ".join(c["type"] for c in schema.checks))
    console.print(schemas)

    secrets = Table(title="Credentials referenced (values are never shown)")
    for column in ("Connector", "Env var", "Set"):
        secrets.add_column(column)
    for spec in company.connectors.values():
        names = []
        if spec.auth:
            names += [spec.auth.credentials.username_env, spec.auth.credentials.password_env]
        if spec.api:
            names.append(spec.api.auth.key_env)
        for name in names:
            secrets.add_row(spec.id, name, "[green]yes[/green]" if os.environ.get(name) else "[red]no[/red]")
    console.print(secrets)

    problems = company.problems()
    if problems:
        for problem in problems:
            console.print(f"[red]✗ {problem}[/red]")
        raise typer.Exit(code=1)
    console.print("[green]✓ Company configuration is valid.[/green]")


@config_app.command("llm")
def config_llm() -> None:
    """Smoke-test the configured model: one call that must use one dummy tool."""
    from karya.config import get_settings
    from karya.llm.base import LLMConfigError, LLMError, Message, ToolSpec
    from karya.llm.factory import build_llm

    settings = get_settings()
    try:
        llm = build_llm(settings)
        tool = ToolSpec("echo", "Echo a word back.", {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]})
        response = llm.chat([Message("system", "Call the echo tool with the word 'karya'."), Message("user", "Go.")], [tool], tool_choice="required")
    except (LLMConfigError, LLMError) as exc:
        console.print(f"[red]✗ {exc}[/red]")
        raise typer.Exit(code=1) from exc
    call = response.tool_calls[0] if response.tool_calls else None
    if call is None:
        console.print("[red]✗ The model answered without calling the tool.[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]✓ {settings.llm_provider} / {response.model or settings.llm_model} called {call.name}({call.arguments}) · {response.usage.input_tokens} in / {response.usage.output_tokens} out tokens[/green]")


@app.command()
def submit(
    request: str = typer.Argument(..., help="The work request, in plain English"),
    requester: Optional[str] = typer.Option(None, help="Who is asking (default: company default requester)"),
    max_steps: Optional[int] = typer.Option(None, "--max-steps", help="Step budget for this task"),
    at: Optional[str] = typer.Option(None, "--at", help="Run no earlier than this ISO time (e.g. 2026-10-09T09:00:00+05:30)"),
    priority: int = typer.Option(0, help="Higher runs first"),
) -> None:
    """Enqueue a request for the background worker; prints the task id."""
    from datetime import datetime

    from karya import db
    from karya.company import load_company
    from karya.config import get_settings
    from karya.db import to_iso
    from karya.worker import queue

    settings = get_settings()
    company = load_company(settings.company_dir)
    run_at = None
    if at:
        try:
            run_at = to_iso(datetime.fromisoformat(at))
        except ValueError as exc:
            console.print(f"[red]--at must be an ISO date-time: {exc}[/red]")
            raise typer.Exit(code=2) from exc
    conn = db.connect(settings.db_path)
    task_id = queue.enqueue(conn, request, requester or company.profile.default_requester, priority=priority, run_at=run_at, max_steps=max_steps)
    console.print(task_id)

def _run_context(worker_id: str):
    """Settings + company + LLM + DB for running tasks. Exits with a clear message if the LLM is not configured."""
    from karya import db
    from karya.company import load_company
    from karya.config import get_settings
    from karya.llm.base import LLMConfigError
    from karya.llm.factory import build_llm
    from karya.runtime.loop import RunContext

    settings = get_settings()
    try:
        llm = build_llm(settings)
    except LLMConfigError as exc:
        console.print(f"[red]LLM not configured:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    return RunContext(settings=settings, company=load_company(settings.company_dir), llm=llm, conn=db.connect(settings.db_path), worker_id=worker_id)


def _foreground(task_id: str, ctx, wait: bool) -> None:
    """Run a task in this process, streaming steps. While it waits for a person, keep
    polling: when an answer/approval requeues it, continue (or follow another worker)."""
    import time

    from karya.cli_display import StepPrinter
    from karya.runtime import ledger
    from karya.worker import queue
    from karya.worker.worker import process_task

    printer = StepPrinter(console)
    ledger.subscribe(printer)
    announced_wait = None
    try:
        while True:
            task = queue.claim(ctx.conn, ctx.worker_id, task_id=task_id)
            if task is not None:
                announced_wait = None
                result = process_task(task_id, ctx)
                if result.outcome == "suspended":
                    _print_waiting(ctx.conn, task_id)
                    if not wait:
                        return
                    announced_wait = result.status
                elif result.outcome == "interrupted":
                    console.print(f"[yellow]{task_id} returned to the queue: {result.detail}[/yellow]")
                    return
                else:
                    break
                continue
            current = queue.get(ctx.conn, task_id)
            if current["status"] in queue.TERMINAL:
                break
            if current["status"] in queue.WAITING:
                if not wait:
                    _print_waiting(ctx.conn, task_id)
                    return
                if announced_wait != current["status"]:
                    console.print(f"[dim]Waiting ({current['status']}); answer or approve in another terminal — this run continues automatically. Ctrl-C to detach.[/dim]")
                    announced_wait = current["status"]
                time.sleep(1)
                continue
            if current["status"] == "running" and current["lease_owner"] != ctx.worker_id:
                console.print(f"[dim]{task_id} is being run by worker {current['lease_owner']}; following its steps.[/dim]")
                last_seq = max((r.seq for r in ledger.rows(ctx.conn, task_id)), default=0)
                while queue.get(ctx.conn, task_id)["status"] == "running":
                    for row in ledger.rows(ctx.conn, task_id, after_seq=last_seq):
                        printer.render(task_id, row.kind, row.payload)
                        last_seq = row.seq
                    time.sleep(1)
                for row in ledger.rows(ctx.conn, task_id, after_seq=last_seq):
                    printer.render(task_id, row.kind, row.payload)
                continue
            if current["status"] == "queued":
                console.print(f"{task_id} is scheduled for {current['run_at']}; a worker will run it then.")
                return
            time.sleep(1)
    except KeyboardInterrupt:
        console.print(f"\n[yellow]Detached from {task_id}. Resume with `karya resume {task_id}` or a running `karya worker`.[/yellow]")
        return
    finally:
        ledger.unsubscribe(printer)
    report = ctx.settings.runs_dir / task_id / "report.md"
    if report.exists():
        console.print(f"Report: {report}")


def _print_waiting(conn, task_id: str) -> None:
    from karya.gateway import approvals as approvals_mod

    task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task["status"] == "waiting_approval":
        for apr in approvals_mod.for_task(conn, task_id):
            if apr["status"] == "pending":
                console.print(f"[bold yellow]{task_id} is waiting for approval {apr['id']}[/bold yellow]: {apr['summary']} (role: {apr['approver_role']})")
                console.print(f"  approve: karya approve {apr['id']} --by \"<name with role {apr['approver_role']}>\"")
    elif task["status"] == "waiting_human":
        for q in conn.execute("SELECT * FROM questions WHERE task_id = ? AND answer IS NULL", (task_id,)):
            console.print(f"[bold yellow]{task_id} is waiting for an answer to {q['id']}[/bold yellow]: {q['question']}")
            console.print(f"  answer: karya answer {q['id']} \"...\"")


@app.command()
def run(
    request: str = typer.Argument(..., help="The work request, in plain English"),
    requester: Optional[str] = typer.Option(None, help="Who is asking (default: company default requester)"),
    max_steps: Optional[int] = typer.Option(None, "--max-steps", help="Step budget for this task"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Keep waiting (and resume) when the task needs a person"),
) -> None:
    """Enqueue a request and run it in the foreground, streaming each step."""
    from karya.worker import queue

    ctx = _run_context("foreground")
    task_id = queue.enqueue(ctx.conn, request, requester or ctx.company.profile.default_requester, max_steps=max_steps)
    console.print(f"[bold]{task_id}[/bold] queued: {request}")
    _foreground(task_id, ctx, wait)


@app.command()
def resume(task_id: str, wait: bool = typer.Option(True, "--wait/--no-wait")) -> None:
    """Continue a queued or waiting task in the foreground."""
    ctx = _run_context("foreground")
    _foreground(task_id, ctx, wait)


@app.command()
def worker(
    once: bool = typer.Option(False, "--once", help="Run one task (until done or waiting) and exit"),
    worker_id: Optional[str] = typer.Option(None, "--id", help="Worker id (default: w-<pid>)"),
) -> None:
    """Background worker: claim -> run -> repeat. Ctrl-C finishes the current step and releases the task."""
    import os

    from karya.cli_display import StepPrinter
    from karya.runtime import ledger
    from karya.worker.worker import run_worker

    ctx = _run_context(worker_id or f"w-{os.getpid()}")
    ledger.subscribe(StepPrinter(console, show_task=True))
    console.print(f"[bold]Worker {ctx.worker_id}[/bold] started (company: {ctx.company.name}). Ctrl-C to stop.")
    try:
        processed = run_worker(ctx, once=once)
    except KeyboardInterrupt:
        console.print("[yellow]Stopped immediately (second Ctrl-C). A running task will be reclaimed after its lease expires.[/yellow]")
        raise typer.Exit(code=130)
    if ctx.stop.is_set():
        console.print(f"[yellow]Worker {ctx.worker_id} stopped; {processed} task run(s). Unfinished work was returned to the queue.[/yellow]")
    else:
        console.print(f"Worker {ctx.worker_id} exiting; {processed} task run(s).")

@app.command()
def cancel(task_id: str, by: str = typer.Option("cli", "--by", help="Who is cancelling (audited)")) -> None:
    """Cancel a queued or waiting task so no worker runs it."""
    from karya import db
    from karya.config import get_settings
    from karya.worker import queue

    conn = db.connect(get_settings().db_path)
    task = queue.get(conn, task_id)
    if task is None:
        console.print(f"[red]No task {task_id}.[/red]")
        raise typer.Exit(code=1)
    if not queue.cancel(conn, task_id, by):
        console.print(f"[yellow]{task_id} is {task['status']}; only queued or waiting tasks can be cancelled.[/yellow]")
        raise typer.Exit(code=1)
    console.print(f"{task_id} cancelled.")


@app.command()
def tasks(status: Optional[str] = typer.Option(None, help="Only tasks with this status")) -> None:
    """List tasks."""
    from karya import db
    from karya.config import get_settings
    from karya.worker import queue

    conn = db.connect(get_settings().db_path)
    table = Table(title="Tasks")
    for column in ("Task", "Status", "Steps", "Tokens", "Requester", "Created (UTC)", "Request"):
        table.add_column(column)
    styles = {"succeeded": "green", "failed": "red", "escalated": "magenta", "partial": "yellow", "waiting_approval": "yellow", "waiting_human": "yellow", "running": "cyan"}
    for task in queue.list_tasks(conn, status):
        style = styles.get(task["status"], "white")
        table.add_row(task["id"], f"[{style}]{task['status']}[/{style}]", str(task["step_count"]), f"{(task['tokens_in'] + task['tokens_out']) // 1000}k", task["requester"], task["created_at"][:19], task["request"][:70])
    console.print(table)

@app.command()
def show(
    task_id: str,
    trace: bool = typer.Option(False, "--trace", help="Full timeline: every step, its why, observation and policy events"),
) -> None:
    """Show a task's report (or its full trace)."""
    import json

    from rich.markdown import Markdown

    from karya import db
    from karya.cli_display import StepPrinter, short_args
    from karya.config import get_settings
    from karya.gateway.secrets import Secrets
    from karya.runtime import ledger
    from karya.worker import queue

    settings = get_settings()
    conn = db.connect(settings.db_path)
    task = queue.get(conn, task_id)
    if task is None:
        console.print(f"[red]No task {task_id}.[/red]")
        raise typer.Exit(code=1)
    if trace:
        from karya.company import load_company

        secrets = Secrets(load_company(settings.company_dir).connectors)
        console.print(f"[bold]{task_id}[/bold] · {task['status']} · {task['request']}")
        printer = StepPrinter(console, verbose=True)
        for row in ledger.rows(conn, task_id):
            payload = json.loads(secrets.redact(json.dumps(row.payload, default=str)))
            printer.render(task_id, row.kind, payload, at=row.created_at)
        for event in conn.execute("SELECT * FROM audit WHERE task_id = ? AND actor = 'gateway' ORDER BY id", (task_id,)):
            if event["event"] in ("policy_denied", "blocked_host", "unknown_write", "read_only_block", "reconcile"):
                console.print(f"[dim]audit {event['ts'][11:19]}[/dim] gateway {event['event']}")
        return
    report = settings.runs_dir / task_id / "report.md"
    if report.exists():
        console.print(Markdown(report.read_text(encoding="utf-8")))
    else:
        console.print(f"[bold]{task_id}[/bold] · {task['status']} · {task['step_count']} steps · no report yet")
        console.print(f"Request: {task['request']}")
        if task["last_error"]:
            console.print(f"[red]Last error: {task['last_error']}[/red]")
        _print_waiting(conn, task_id)


@app.command()
def inbox() -> None:
    """Pending questions and approvals, together."""
    import json

    from karya import db
    from karya.config import get_settings
    from karya.worker import inbox as inbox_mod

    conn = db.connect(get_settings().db_path)
    questions = inbox_mod.pending_questions(conn)
    approvals = inbox_mod.pending_approvals(conn)
    if not questions and not approvals:
        console.print("Inbox is empty.")
        return
    for q in questions:
        options = json.loads(q["options_json"] or "[]")
        console.print(f"[bold yellow]{q['id']}[/bold yellow] (task {q['task_id']}) {q['question']}")
        for option in options:
            console.print(f"    • {option}")
        if q["context"]:
            console.print(f"    [dim]{q['context']}[/dim]")
        console.print(f"    answer: karya answer {q['id']} \"...\"")
    for a in approvals:
        rules = ", ".join(json.loads(a["rule_ids_json"]))
        console.print(f"[bold yellow]{a['id']}[/bold yellow] (task {a['task_id']}) {a['summary']} — rule {rules}, needs role {a['approver_role']}")
        console.print(f"    approve: karya approve {a['id']} --by \"...\"   reject: karya reject {a['id']} --by \"...\" --reason \"...\"")


def _learner():
    """Fact learning uses the fast model; without a configured model, answers are still recorded."""
    from karya.llm.base import LLMConfigError
    from karya.llm.factory import build_llm

    try:
        llm = build_llm()
    except LLMConfigError:
        return None
    from karya.memory.learning import learner_for

    return learner_for(llm)


@app.command()
def answer(
    question_id: str,
    text: str,
    by: Optional[str] = typer.Option(None, "--by", help="Who is answering (default: the task's requester)"),
    remember: bool = typer.Option(True, "--remember/--no-remember", help="Learn a durable company fact from this answer"),
) -> None:
    """Answer a question; the task resumes."""
    from karya import db
    from karya.company import load_company
    from karya.config import get_settings
    from karya.worker import inbox as inbox_mod

    settings = get_settings()
    conn = db.connect(settings.db_path)
    try:
        question, fact = inbox_mod.answer_question(conn, load_company(settings.company_dir), question_id, text, by, remember, learn=_learner() if remember else None)
    except inbox_mod.InboxError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]{question_id} answered by {question['answered_by']}; task {question['task_id']} requeued.[/green]")
    if fact is not None:
        console.print(f"[cyan]Learned {fact.id}: {fact.statement}[/cyan]")


@app.command()
def approve(
    approval_id: str,
    by: str = typer.Option(..., "--by", help="The approver (must hold the rule's approver role)"),
    note: str = typer.Option("", "--note", help="Optional note"),
) -> None:
    """Approve a held action (RBAC: the approver must hold the rule's role)."""
    _decide(approval_id, True, by, note)


@app.command()
def reject(
    approval_id: str,
    by: str = typer.Option(..., "--by", help="The approver (must hold the rule's approver role)"),
    reason: str = typer.Option(..., "--reason", help="Why (reported, and learned from)"),
) -> None:
    """Reject a held action; the task resumes and escalates instead."""
    _decide(approval_id, False, by, reason)


def _decide(approval_id: str, approve_it: bool, by: str, note: str) -> None:
    from karya import db
    from karya.company import load_company
    from karya.config import get_settings
    from karya.worker import inbox as inbox_mod

    settings = get_settings()
    conn = db.connect(settings.db_path)
    try:
        decided, fact = inbox_mod.decide(conn, load_company(settings.company_dir), approval_id, approve_it, by, note, learn=_learner() if note else None)
    except inbox_mod.InboxError as exc:
        console.print(f"[red]Refused: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    style = "green" if approve_it else "red"
    console.print(f"[{style}]{approval_id} {decided['status']} by {decided['decided_by']}; task {decided['task_id']} requeued.[/{style}]")
    if fact is not None:
        console.print(f"[cyan]Learned {fact.id}: {fact.statement}[/cyan]")


@app.command()
def audit(task_id: str) -> None:
    """Audit log for a task (append-only)."""
    from karya import db
    from karya.config import get_settings

    conn = db.connect(get_settings().db_path)
    table = Table(title=f"Audit log · {task_id}")
    for column in ("Time (UTC)", "Actor", "Event", "Details"):
        table.add_column(column)
    styles = {"policy_denied": "red", "blocked_host": "red", "unknown_write": "red", "approval_required": "yellow", "reconcile": "cyan", "duplicate_prevented": "cyan"}
    for row in conn.execute("SELECT * FROM audit WHERE task_id = ? ORDER BY id", (task_id,)):
        style = styles.get(row["event"], "white")
        table.add_row(row["ts"][11:23], row["actor"], f"[{style}]{row['event']}[/{style}]", row["details_json"][:150])
    console.print(table)

@memory_app.command("list")
def memory_list(include_forgotten: bool = typer.Option(False, "--all", help="Include forgotten facts")) -> None:
    """Learned company facts, with provenance."""
    from karya import db
    from karya.config import get_settings
    from karya.memory import store

    conn = db.connect(get_settings().db_path)
    facts = store.list_facts(conn, include_inactive=include_forgotten)
    if not facts:
        console.print("No learned facts yet.")
        return
    table = Table(title="Company memory · learned facts")
    for column in ("Fact", "Statement", "Provenance", "Active"):
        table.add_column(column)
    for fact in facts:
        table.add_row(fact.id, fact.statement, fact.provenance(), "yes" if fact.active else "[dim]forgotten[/dim]")
    console.print(table)


@memory_app.command("search")
def memory_search(query: str) -> None:
    """Search the handbook, learned facts and past tasks (what the agent would retrieve)."""
    from karya import db
    from karya.company import load_company
    from karya.config import get_settings
    from karya.memory.retrieval import Retriever

    settings = get_settings()
    found = Retriever(load_company(settings.company_dir), db.connect(settings.db_path)).retrieve(query)
    for hit in found.handbook + found.facts + found.episodes:
        origin = f" — {hit.provenance}" if hit.provenance else ""
        console.print(f"[bold]{hit.id}[/bold] [dim]({hit.kind}, score {hit.score:.2f})[/dim] {hit.title}{origin}")
        console.print(f"   {hit.text[:300]}")
    if not found.ids():
        console.print("Nothing relevant found.")


@memory_app.command("forget")
def memory_forget(fact_id: str, by: str = typer.Option("cli", "--by", help="Who is removing it (audited)")) -> None:
    """Deactivate a learned fact (audited; history is kept)."""
    from karya import db
    from karya.config import get_settings
    from karya.memory import store

    conn = db.connect(get_settings().db_path)
    if store.forget_fact(conn, fact_id, by):
        console.print(f"[yellow]{fact_id} forgotten; it will no longer be used.[/yellow]")
    else:
        console.print(f"[red]No active fact {fact_id}.[/red]")
        raise typer.Exit(code=1)


@evals_app.command("run")
def evals_run(
    scenarios: Optional[str] = typer.Option(None, help="Comma-separated scenario ids (default: all)"),
    chaos: str = typer.Option("off", help="Comma-separated chaos profiles, e.g. off,heavy"),
    runs: int = typer.Option(1, help="Runs per scenario and profile"),
    suite: str = typer.Option("evals/suite.yaml", help="Suite file"),
    out: str = typer.Option("evals/results", help="Results folder"),
    verbose: bool = typer.Option(False, "--verbose", help="Stream every step"),
) -> None:
    """Run the evaluation suite against the sandbox (apps must be up) and write a results table."""
    from karya import db
    from karya.cli_display import StepPrinter
    from karya.company import load_company
    from karya.config import get_settings, resolve_path
    from karya.evals.harness import Harness, SandboxEnvironment, load_suite
    from karya.llm.base import LLMConfigError
    from karya.llm.factory import build_llm
    from karya.runtime import ledger

    settings = get_settings()
    all_scenarios = load_suite(resolve_path(suite))
    wanted = [x.strip() for x in scenarios.split(",")] if scenarios else [s.id for s in all_scenarios]
    unknown = sorted(set(wanted) - {s.id for s in all_scenarios})
    if unknown:
        console.print(f"[red]Unknown scenarios: {unknown}[/red]")
        raise typer.Exit(code=2)
    selected = [s for s in all_scenarios if s.id in wanted]
    try:
        llm = build_llm(settings)
    except LLMConfigError as exc:
        llm = None
        console.print(f"[yellow]No LLM configured ({exc}); only scripted scenarios will run.[/yellow]")
    if verbose:
        ledger.subscribe(StepPrinter(console, show_task=True))
    harness = Harness(settings, load_company(settings.company_dir), db.connect(settings.db_path), llm, SandboxEnvironment(), resolve_path(out), progress=console.print)
    try:
        data = harness.run(selected, [c.strip() for c in chaos.split(",") if c.strip()], runs)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    table = Table(title=f"Eval summary · {data['model']}")
    for column in ("Chaos", "Runs", "Passed", "Pass rate", "Verifier agreement", "Median steps", "Median tokens", "Skipped"):
        table.add_column(column)
    for row in data["summary"]:
        rate = "—" if row["pass_rate"] is None else f"{row['pass_rate'] * 100:.0f}%"
        agreement = "—" if row["verifier_agreement"] is None else f"{row['verifier_agreement'] * 100:.0f}%"
        table.add_row(row["chaos"], str(row["runs"]), str(row["passed"]), rate, agreement, f"{row['median_steps']:.0f}", f"{row['median_tokens'] / 1000:.0f}k", str(row["skipped"]))
    console.print(table)
    for path in data["files"]:
        console.print(f"wrote {path}")


@evals_app.command("list")
def evals_list(suite: str = typer.Option("evals/suite.yaml", help="Suite file")) -> None:
    """List the scenarios in the suite."""
    from karya.config import resolve_path
    from karya.evals.harness import load_suite

    table = Table(title="Eval scenarios")
    for column in ("Scenario", "Kind", "Human script", "Expected status", "Checks", "Request"):
        table.add_column(column)
    for sc in load_suite(resolve_path(suite)):
        human = sc.human.get("approvals", "none") if sc.human.get("approvals", "none") != "none" else ("answers" if sc.human.get("answers") else "—")
        table.add_row(sc.id, "scripted" if sc.scripted else "model", human, ", ".join(sc.expect.get("status", [])), str(len(sc.expect.get("checks", []))), sc.request[:60])
    console.print(table)


if __name__ == "__main__":
    app()
