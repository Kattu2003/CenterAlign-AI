"""Karya command-line interface (typer).

Phase 0 skeleton: every command from plan §11.13 exists so `karya --help` shows
the whole surface; later phases fill them in.
"""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console

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


@sandbox_app.command("up")
def sandbox_up() -> None:
    """Start the sandbox apps."""
    _todo(1)


@sandbox_app.command("down")
def sandbox_down() -> None:
    """Stop the sandbox apps."""
    _todo(1)


@sandbox_app.command("reset")
def sandbox_reset() -> None:
    """Re-seed sandbox databases, PDFs and shared drive."""
    _todo(1)


@sandbox_app.command("status")
def sandbox_status() -> None:
    """Show which sandbox apps respond."""
    _todo(1)


@config_app.command("check")
def config_check() -> None:
    """Validate and print the company configuration."""
    _todo(2)


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
