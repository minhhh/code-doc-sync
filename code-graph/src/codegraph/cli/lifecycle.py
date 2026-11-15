"""Graph lifecycle and serialization: build, update, status, export."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from ..pipeline.export import GraphTypeError, build_payload, canonical_graph_type
from ..graph import GraphStore
from ..pipeline.incremental import get_db_path
from ..pipeline.build import build_or_update
from ..query.queries import list_graph_stats
from .app import app
from .common import _emit, _json_text, _root, _store
from .options import FormatOption, NoColorOption, PrettyOption, RootOption

@app.command("build")
def build(
    root: RootOption = None,
    postprocess: Annotated[str, typer.Option(help="full|minimal|none")] = "full",
    recurse_submodules: Annotated[
        Optional[bool], typer.Option(help="Include submodules.")
    ] = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Build the graph from scratch."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        result = build_or_update(
            store,
            repo_root,
            full_rebuild=True,
            postprocess=postprocess,
            recurse_submodules=recurse_submodules,
        )
    _emit(
        result,
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
@app.command("update")
def update(
    root: RootOption = None,
    base: Annotated[str, typer.Option(help="Git ref to diff against.")] = "HEAD~1",
    postprocess: Annotated[str, typer.Option(help="full|minimal|none")] = "full",
    recurse_submodules: Annotated[
        Optional[bool], typer.Option(help="Include submodules.")
    ] = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Incrementally update the graph for changed files."""
    repo_root = _root(root)
    with _store(repo_root) as store:
        result = build_or_update(
            store,
            repo_root,
            full_rebuild=False,
            base=base,
            postprocess=postprocess,
            recurse_submodules=recurse_submodules,
        )
    _emit(
        result,
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
@app.command(name="export")
def export_graph(
    format: Annotated[
        str, typer.Option("--format", "-f", help="Output format (json).")
    ] = "json",
    graph_type: Annotated[
        str,
        typer.Option(
            "--type",
            "-T",
            help="Graph type: call-graph, community (alias architecture), flows (alias flow).",
        ),
    ] = "call-graph",
    limit: Annotated[
        int, typer.Option("--limit", help="Cap rows per section (flows only).")
    ] = 50,
    out: Annotated[
        Optional[Path], typer.Option("--out", help="Output file (stdout if omitted).")
    ] = None,
    root: RootOption = None,
    pretty: PrettyOption = False,
) -> None:
    """Export a typed projection of the graph as JSON (stdout or a file)."""
    if format != "json":
        typer.echo("error: --format must be json", err=True)
        raise typer.Exit(code=2)
    repo_root = _root(root)
    try:
        canonical = canonical_graph_type(graph_type)
    except GraphTypeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    payload = _export_payload(repo_root, canonical, limit=limit)
    if out is None:
        typer.echo(_json_text(payload, pretty=pretty))
        return
    _write_json(payload, out, pretty=pretty)
    _emit(
        {
            "status": "ok",
            "summary": f"Exported {canonical} ({format}) to {out}",
            "type": canonical,
            "path": str(out),
        },
        pretty=pretty,
    )
@app.command("status")
def status(
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Show graph statistics."""
    repo_root = _root(root)
    _emit(
        list_graph_stats(repo_root=str(repo_root)),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )

def _export_payload(
    repo_root: Path,
    graph_type: str = "call-graph",
    *,
    limit: int = 50,
) -> dict[str, object]:
    """Projection of the graph for one ``--type``."""
    db_path = get_db_path(repo_root)
    if not db_path.exists():
        typer.echo("error: graph not built yet. Run `code-graph build` first.", err=True)
        raise typer.Exit(code=1)
    with GraphStore(db_path) as store:
        return build_payload(repo_root, store, graph_type, limit=limit)

def _write_json(payload: dict[str, object], out: Path, *, pretty: bool) -> None:
    rendered = _json_text(payload, pretty=pretty)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".tmp")
    temporary.write_text(rendered + "\n", encoding="utf-8")
    temporary.replace(out)
