"""Execution flows: list them, or show one with its steps."""

from __future__ import annotations

from typing import Annotated

import typer

from ..query.flows import get_flow_by_id, get_flows
from ..graph import GraphStore
from ..incremental import get_db_path
from .common import _emit, _root, _with_hints
from .options import FormatOption, NoColorOption, PrettyOption, RootOption

flows_app = typer.Typer(
    name="flows",
    help="Execution flows: list them, or show one with its steps.",
    no_args_is_help=True,
)

@flows_app.command("list")
def flows_list(
    sort_by: Annotated[str, typer.Option(help="criticality|depth|node_count|name")] = "criticality",
    limit: Annotated[int, typer.Option(help="Maximum flows.")] = 50,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """List execution flows sorted by criticality."""
    repo_root = _root(root)
    db_path = get_db_path(repo_root)
    if not db_path.exists():
        typer.echo("error: graph not built yet. Run `code-graph build` first.", err=True)
        raise typer.Exit(code=1)
    with GraphStore(db_path) as store:
        result = get_flows(store, sort_by=sort_by, limit=limit)
    _emit(
        _with_hints(
            {
                "status": "ok",
                "summary": f"{len(result)} flow(s)",
                "flows": result,
            },
            "flows list",
        ),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
@flows_app.command("show")
def flows_show(
    flow_id: Annotated[int, typer.Argument(help="Flow id.")],
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Show a single execution flow with its steps."""
    repo_root = _root(root)
    db_path = get_db_path(repo_root)
    if not db_path.exists():
        typer.echo("error: graph not built yet. Run `code-graph build` first.", err=True)
        raise typer.Exit(code=1)
    with GraphStore(db_path) as store:
        result = get_flow_by_id(store, flow_id)
    if result is None:
        typer.echo(f"error: no flow with id {flow_id}", err=True)
        raise typer.Exit(code=1)
    _emit(
        _with_hints(
            {
                "status": "ok",
                "summary": f"Flow {flow_id}: {result.get('name', '')}",
                "flow": result,
            },
            "flows show",
        ),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
