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
    from sandbox import run as sandbox_run

    state = sandbox_run.arm(app_name, fault, count, path)
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
def submit(request: str, requester: Optional[str] = None) -> None:
    """Enqueue a request; prints the task id."""
    _todo(5)


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
def worker(once: bool = False) -> None:
    """Run the background worker."""
    _todo(5)


@app.command()
def tasks() -> None:
    """List tasks."""
    _todo(5)


@app.command()
def show(task_id: str, trace: bool = False) -> None:
    """Show a task report or its full trace."""
    _todo(4)


@app.command()
def inbox() -> None:
    """Pending questions and approvals."""
    _todo(5)


@app.command()
def answer(question_id: str, text: str) -> None:
    """Answer a question."""
    _todo(5)


@app.command()
def approve(approval_id: str) -> None:
    """Approve a held action."""
    _todo(5)


@app.command()
def reject(approval_id: str) -> None:
    """Reject a held action."""
    _todo(5)


@app.command()
def audit(task_id: str) -> None:
    """Audit log for a task."""
    _todo(5)


@memory_app.command("list")
def memory_list() -> None:
    """List learned facts."""
    _todo(7)


@evals_app.command("run")
def evals_run() -> None:
    """Run the evaluation suite."""
    _todo(10)


if __name__ == "__main__":
    app()
