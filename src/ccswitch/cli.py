"""Typer command-line surface for ccswitch.

Every subcommand wraps its underlying call so that the three expected
"user error" exception types — ``ValueError``, :class:`KeychainError`,
and ``FileNotFoundError`` — surface as a red one-line ``Error:`` message
plus exit code 1. Unanticipated exceptions still bubble up so the user
sees the traceback rather than a silent failure.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from ccswitch.exec_runner import run_with_account
from ccswitch.keychain import KeychainError
from ccswitch.switcher import Switcher

app = typer.Typer(
    name="ccswitch",
    help="macOS account switcher for Claude Code.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

_HANDLED_ERRORS = (ValueError, KeychainError, FileNotFoundError)


def _run(fn, *args, **kwargs) -> None:
    """Run ``fn`` with the user-error exceptions caught and rendered."""
    try:
        fn(*args, **kwargs)
    except _HANDLED_ERRORS as exc:
        Console(stderr=True).print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("add")
def add_cmd(
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            "-l",
            help="Label to save under. Defaults to a prompt suggesting <email-local>-<org-slug>.",
        ),
    ] = None,
) -> None:
    """Save the currently-active Claude Code account."""
    _run(Switcher().add, label_override=label)


@app.command("list")
def list_cmd() -> None:
    """List saved accounts with an active marker."""
    _run(Switcher().list_accounts)


@app.command("current")
def current_cmd() -> None:
    """Show the currently-active Claude account."""
    _run(Switcher().current)


@app.command("use")
def use_cmd(
    label: Annotated[str, typer.Argument(help="Saved account label to switch to.")],
) -> None:
    """Switch the global Claude Code account to the given label."""
    _run(Switcher().use, label)


@app.command("remove")
def remove_cmd(
    label: Annotated[str, typer.Argument(help="Saved account label to remove.")],
) -> None:
    """Remove a saved account after confirmation."""
    _run(Switcher().remove, label)


@app.command(
    "exec",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    help=(
        "Run a command scoped to a saved account via env-var injection. "
        "Example: ccswitch exec work -- claude -p 'hello'"
    ),
)
def exec_cmd(
    ctx: typer.Context,
    label: Annotated[str, typer.Argument(help="Saved account label to scope the command to.")],
) -> None:
    cmd_argv = list(ctx.args)
    try:
        exit_code = run_with_account(label, cmd_argv)
    except _HANDLED_ERRORS as exc:
        Console(stderr=True).print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=exit_code)


if __name__ == "__main__":  # pragma: no cover
    app()
