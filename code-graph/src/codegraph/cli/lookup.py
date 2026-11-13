"""Symbol lookup: predefined queries and full-text search."""

from __future__ import annotations

from typing import Annotated, Optional

import typer

from ..queries import query_graph
from ..search import semantic_search_nodes
from .app import app
from .common import _emit, _root, _with_hints
from .options import FormatOption, NoColorOption, PrettyOption, RootOption

@app.command("query")
def query(
    pattern: Annotated[
        str, typer.Argument(help="callers_of|callees_of|imports_of|importers_of|children_of|tests_for|inheritors_of|file_summary")
    ],
    target: Annotated[str, typer.Argument(help="Node name, qualified name, or file path.")],
    limit: Annotated[
        int, typer.Option("--limit", help="Maximum results returned.")
    ] = 20,
    detail: Annotated[
        str, typer.Option("--detail", help="standard|minimal.")
    ] = "standard",
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Run a predefined graph query."""
    if detail not in ("standard", "minimal"):
        typer.echo("error: --detail must be standard or minimal", err=True)
        raise typer.Exit(code=2)
    if limit < 1:
        typer.echo("error: --limit must be >= 1", err=True)
        raise typer.Exit(code=2)
    repo_root = _root(root)
    _emit(
        query_graph(
            pattern,
            target,
            repo_root=str(repo_root),
            detail_level=detail,
            limit=limit,
        ),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
@app.command("search")
def search(
    query: Annotated[str, typer.Argument(help="Search string.")],
    kind: Annotated[Optional[str], typer.Option(help="Filter by node kind.")] = None,
    limit: Annotated[int, typer.Option(help="Maximum results.")] = 20,
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Search nodes with FTS5 (BM25)."""
    repo_root = _root(root)
    _emit(
        _with_hints(
            semantic_search_nodes(query, kind=kind, limit=limit, repo_root=str(repo_root)),
            "search",
        ),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
