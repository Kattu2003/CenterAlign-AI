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


@app.command()
def submit(request: str, requester: Optional[str] = None) -> None:
    """Enqueue a request; prints the task id."""
    _todo(5)


@app.command()
def run(request: str) -> None:
    """Enqueue a request and run it in the foreground."""
    _todo(3)


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
