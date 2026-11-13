"""The root Typer application."""

from __future__ import annotations

from typing import Annotated

import typer

from .. import __version__

app = typer.Typer(
    name="code-graph",
    help="Local-first structural code graph with a SQLite query CLI.",
    no_args_is_help=True,
    add_completion=False,
)

@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Show version."
        ),
    ] = False,
) -> None:
    """Structural code graph builder and query engine."""


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"code-graph {__version__}")
        raise typer.Exit()
